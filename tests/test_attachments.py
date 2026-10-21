import json
import struct
import zipfile
import zlib
from dataclasses import replace

import pytest

from attachment_refs import ASSET_DIR, AttachmentRef
from attachment_transport import AttachmentHTTP, save_chunks
from attachments import AttachmentManager
from chatgpt_client import ChatGPTClient, ExportError, HTTPError
from chatgpt_exporter import export_conversations, main
from export_config import Config
from local_index import build_index


def png_bytes():
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\x12\x34\x56\xff"))
        + chunk(b"IEND", b"")
    )


PNG = png_bytes()


def image_chat(ident="c"):
    return {
        "id": ident,
        "title": "Private chat",
        "update_time": 1,
        "current_node": "m",
        "mapping": {
            "m": {
                "parent": None,
                "children": [],
                "message": {
                    "author": {"role": "user"},
                    "content": {
                        "content_type": "multimodal_text",
                        "parts": [{"asset_pointer": "sediment://file-image"}],
                    },
                    "metadata": {
                        "attachments": [
                            {"id": "file-image", "name": "picture.png", "mime_type": "image/png"}
                        ]
                    },
                },
            }
        },
    }


class Resolver(ChatGPTClient):
    def __init__(self):
        super().__init__(Config(request_delay=0, max_retries=0))
        self.resolutions = []
        self.token_calls = 0

    def access_token(self):
        self.token_calls += 1
        return "PRIVATE_ACCESS_TOKEN"

    def json(self, path, token, *, params=None, label):
        self.resolutions.append((path, params))
        return {
            "download_url": "https://files.oaiusercontent.com/blob?sig=PRIVATE_SIGNATURE",
            "file_name": "storage-user/same-name.png",
            "mime_type": "image/png",
        }


class Binary(AttachmentHTTP):
    def __init__(self, client):
        super().__init__(client)
        self.calls = 0

    def download(self, url, destination, token, *, max_bytes):
        self.calls += 1
        return save_chunks(
            [PNG],
            destination,
            total=len(PNG),
            limit=max_bytes,
            progress=self.client.progress,
            mime="image/png",
        )


def manager(root, **kwargs):
    client = Resolver()
    instance = AttachmentManager(root, client=client, **kwargs)
    instance.http = Binary(client)
    return instance, client


def test_attachment_manifest_dedup_integrity_and_sensitive_url_redaction(tmp_path, capsys):
    assets, client = manager(tmp_path)
    assets.process(image_chat())
    assert assets.finish() == 0 and assets.saved == 1
    assert len(client.resolutions) == 1
    entry = next(iter(assets.data["assets"].values()))
    target = tmp_path / entry["path"]
    assert target.read_bytes() == PNG
    assert target.stat().st_mode & 0o777 == 0o600
    serialized = assets.path.read_text()
    assert "PRIVATE_SIGNATURE" not in serialized and "PRIVATE_ACCESS_TOKEN" not in serialized
    assert "download_url" not in serialized
    assets.process(image_chat())
    assert len(client.resolutions) == 1 and assets.skipped == 1
    target.write_bytes(b"damaged")
    assets.process(image_chat())
    assert len(client.resolutions) == 2 and target.read_bytes() == PNG
    assert "PRIVATE_" not in capsys.readouterr().out


def test_colliding_names_are_distinguished_by_reference_identity(tmp_path):
    assets, _ = manager(tmp_path)
    assets.process(image_chat("a"))
    assets.process(image_chat("b"))
    paths = {entry["path"] for entry in assets.data["assets"].values()}
    assert len(paths) == 2 and all((tmp_path / path).is_file() for path in paths)


def test_sandbox_resolver_receives_message_and_path_not_arbitrary_urls(tmp_path):
    detail = image_chat()
    message = detail["mapping"]["m"]["message"]
    message["metadata"] = {}
    message["content"] = {"parts": ["[download](sandbox:/mnt/data/report%20file.csv)"]}
    assets, client = manager(tmp_path)
    assets.process(detail)
    path, params = client.resolutions[0]
    assert path == "conversation/c/interpreter/download"
    assert params == {"message_id": "m", "sandbox_path": "/mnt/data/report file.csv"}


def test_existing_json_backfill_never_reexports_conversations_or_changes_sources(tmp_path):
    source = tmp_path / "Old folder/chat.json"
    source.parent.mkdir()
    source.write_text(json.dumps(image_chat()))
    before = source.read_bytes()
    assets, client = manager(tmp_path)
    assert assets.backfill() == 0
    assert source.read_bytes() == before
    assert all(path.startswith("files/download/") for path, _ in client.resolutions)
    assert assets.data["conversations"]["c"]["references"]


def test_skipped_json_export_still_backfills_missing_attachments_without_refetching_chat(tmp_path):
    detail = image_chat()
    config = replace(Config(), output_dir=tmp_path)
    export_conversations([detail], lambda _: detail, config)
    assets, client = manager(tmp_path)

    def forbidden(_):
        pytest.fail("Unchanged JSON must be reused rather than refetching full conversation")

    assert export_conversations([detail], forbidden, config, assets=assets) == 0
    assert assets.finish() == 0 and len(client.resolutions) == 1


def test_offline_zip_import_restores_bundled_file_and_viewer_previews_without_network(tmp_path):
    source = tmp_path / "download.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("conversations.json", json.dumps([image_chat()]))
        archive.writestr("uploads/file-image-picture.png", PNG)
    output = tmp_path / "output"
    assert main(["import", str(source), "--output", str(output), "--no-progress"]) == 0
    manifest = json.loads((output / ASSET_DIR / "manifest.json").read_text())
    entry = next(iter(manifest["assets"].values()))
    assert (output / entry["path"]).read_bytes() == PNG
    page = next((output / ".chatgpt-index/chats").glob("*.html"))
    assert '<img class="attachment-preview"' in page.read_text()
    assert 'class="attachment-open"' in page.read_text()
    assert list((output / ".chatgpt-index/asset-views").glob("*.html"))
    assert (output / ".chatgpt-index/assets.html").is_file()
    assert "Attachments &amp; artifacts" in (output / "index.html").read_text()


def test_missing_offline_asset_is_reported_partial_without_network(tmp_path):
    source = tmp_path / "conversations.json"
    source.write_text(json.dumps([image_chat()]))
    output = tmp_path / "output"
    assert main(["import", str(source), "--output", str(output), "--no-progress"]) == 1
    manifest = json.loads((output / ASSET_DIR / "manifest.json").read_text())
    assert next(iter(manifest["assets"].values()))["error"] == "not_bundled_offline"
    assert (output / "index.html").is_file()


@pytest.mark.parametrize("unsafe", ["../file-image.png", "/file-image.png", "x\\file-image.png"])
def test_zip_unsafe_member_names_are_not_used(tmp_path, unsafe):
    source = tmp_path / "download.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("conversations.json", json.dumps([image_chat()]))
        archive.writestr(unsafe, PNG)
    output = tmp_path / "out"
    assert main(["import", str(source), "--output", str(output), "--no-progress"]) == 1
    assert not (tmp_path / "file-image.png").exists()


def test_zip_ambiguous_file_id_is_not_guessed(tmp_path):
    source = tmp_path / "download.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("conversations.json", json.dumps([image_chat()]))
        archive.writestr("a/file-image-a.png", PNG)
        archive.writestr("b/file-image-b.png", b"other")
    output = tmp_path / "out"
    assert main(["import", str(source), "--output", str(output), "--no-progress"]) == 1


def test_attachment_opt_out_leaves_conversation_references_intact(tmp_path):
    source = tmp_path / "chat.json"
    source.write_text(json.dumps([image_chat()]))
    output = tmp_path / "out"
    assert (
        main(["import", str(source), "--output", str(output), "--no-attachments", "--no-progress"])
        == 0
    )
    assert not (output / ASSET_DIR).exists()
    saved = next((output / "_Uncategorized").glob("*.json"))
    assert "sediment://file-image" in saved.read_text()


def test_symlink_managed_directory_cannot_escape_root(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / ASSET_DIR).symlink_to(outside, target_is_directory=True)
    with pytest.raises(ExportError, match="symlink"):
        AttachmentManager(root)
    assert not list(outside.iterdir())


def test_disallowed_signed_url_records_failure_without_exposing_credentials(tmp_path):
    class BadResolver(Resolver):
        def json(self, path, token, **kwargs):
            return {"download_url": "http://127.0.0.1/secret?sig=PRIVATE_SIGNATURE"}

    assets = AttachmentManager(tmp_path, client=BadResolver())
    assets.process(image_chat())
    assert assets.finish() == 1
    assert "PRIVATE_SIGNATURE" not in assets.path.read_text()
    assert next(iter(assets.data["assets"].values()))["outcome"] == "failed"


def test_expired_assets_do_not_disappear_and_auth_challenges_stop(tmp_path):
    class Missing(Resolver):
        def json(self, path, token, **kwargs):
            raise HTTPError(410, "attachment resolver")

    assets = AttachmentManager(tmp_path, client=Missing())
    assets.process(image_chat())
    assert assets.finish() == 1 and assets.unavailable == 1

    class Challenged(Resolver):
        def json(self, path, token, **kwargs):
            raise HTTPError(403, "attachment resolver", browser_challenge=True)

    with AttachmentManager(tmp_path / "challenge", client=Challenged()) as challenged:
        with pytest.raises(HTTPError):
            challenged.process(image_chat())
        assert next(iter(challenged.data["assets"].values()))["outcome"] == "failed"


def test_unknown_project_file_ids_are_reported_not_ignored(tmp_path):
    assets, _ = manager(tmp_path)
    assets.projects({"g-p-abc": [{"id": "unknown-file-shape"}]})
    assert assets.finish() == 1 and assets.unsupported == 1


def test_html_artifacts_are_download_only_never_embedded_or_executed(tmp_path):
    assets = AttachmentManager(tmp_path)
    ref = AttachmentRef("canvas", "c", "document", name="danger<script>.html")
    path = f"{ASSET_DIR}/canvas/test.html"
    target = tmp_path / path
    target.parent.mkdir()
    target.write_text('<script>window.evil=true</script><img src="https://evil.invalid/x">')
    assets.data["assets"][ref.key] = {
        "reference": ref.record(),
        "path": path,
        "name": ref.name,
        "outcome": "saved",
        "mime": "text/html",
    }
    assets.checkpoint()
    (tmp_path / "chat.json").write_text(json.dumps(image_chat()))
    build_index(tmp_path)
    page = next((tmp_path / ".chatgpt-index/chats").glob("*.html")).read_text()
    assert "window.evil" not in page and "https://evil.invalid" not in page
    assert "danger&lt;script&gt;" in page and 'class="attachment-open"' in page
    wrappers = list((tmp_path / ".chatgpt-index/asset-views").glob("*.html"))
    assert wrappers and "&lt;script&gt;window.evil=true&lt;/script&gt;" in wrappers[0].read_text()
    assert "<iframe" not in page


def test_markdown_asset_source_association_and_manifest_resume(tmp_path):
    detail = image_chat()
    assets, client = manager(tmp_path)
    config = replace(Config(), output_dir=tmp_path, output_format="markdown")
    assert export_conversations([detail], lambda _: detail, config, assets=assets) == 0
    assets.finish()
    build_index(tmp_path)
    page = next((tmp_path / ".chatgpt-index/chats").glob("*.html")).read_text()
    assert "attachment-preview" in page
    manifest_entry = next(iter(assets.data["assets"].values()))
    (tmp_path / manifest_entry["path"]).unlink()
    restored, resolver = manager(tmp_path)
    assert restored.backfill() == 0 and len(resolver.resolutions) == 1
