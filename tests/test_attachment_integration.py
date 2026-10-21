import json
import zipfile

import pytest
from test_attachments import PNG, Resolver, image_chat, manager

from attachment_refs import ASSET_DIR
from attachment_transport import AttachmentHTTP, save_chunks
from attachments import AttachmentManager
from chatgpt_client import ChatGPTClient
from chatgpt_exporter import main
from export_config import Config
from local_index import build_index


@pytest.mark.parametrize(
    "flag,setting,included",
    [
        (None, None, True),
        ("--no-attachments", None, False),
        (None, "false", False),
        ("--attachments", "false", True),
        ("--no-attachments", "true", False),
    ],
)
def test_attachment_defaults_config_and_cli_override(
    tmp_path, monkeypatch, flag, setting, included
):
    archive_path = tmp_path / "source.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("conversations.json", json.dumps([image_chat()]))
        archive.writestr("files/file-image-picture.png", PNG)
    if setting is not None:
        monkeypatch.setenv("CHATGPT_ATTACHMENTS", setting)
    output = tmp_path / "out"
    args = ["import", str(archive_path), "--output", str(output), "--no-progress"]
    if flag:
        args.append(flag)
    assert main(args) == 0
    assert (output / ASSET_DIR / "manifest.json").exists() is included


def test_attachments_command_uses_existing_json_not_conversation_downloads(tmp_path, monkeypatch):
    source = tmp_path / "chat.json"
    source.write_text(json.dumps(image_chat()))
    original = source.read_bytes()
    client = Resolver()

    def create(config, *, progress):
        client.config = config
        client.progress = progress
        return client

    def download(self, url, destination, token, *, max_bytes):
        return save_chunks(
            [PNG], destination, total=len(PNG), limit=max_bytes, progress=self.client.progress
        )

    monkeypatch.setattr("chatgpt_exporter.ChatGPTClient", create)
    monkeypatch.setattr(AttachmentHTTP, "download", download)
    assert main(["attachments", str(tmp_path), "--no-progress"]) == 0
    assert source.read_bytes() == original
    assert client.resolutions and all(
        path.startswith("files/download/") for path, _ in client.resolutions
    )
    assert (tmp_path / "index.html").exists()
    with pytest.raises(SystemExit):
        main(["attachments", str(tmp_path), "--no-attachments"])


def test_project_upload_metadata_is_retained_and_downloaded_without_fabricated_conversation_id(
    tmp_path,
):
    file_info = {
        "id": "project-record-id",
        "file_id": "file-project",
        "name": "project.png",
        "type": "image/png",
    }

    class Client(ChatGPTClient):
        def json(self, path, token, **kwargs):
            return {
                "items": [
                    {
                        "gizmo": {
                            "gizmo": {"id": "g-p-abc", "display": {"name": "Project"}},
                            "files": [file_info],
                        }
                    }
                ]
            }

    discovery = Client(Config())
    assert discovery.projects("fake") == {"g-p-abc": "Project"}
    assert discovery.project_files == {"g-p-abc": [file_info]}
    assets, resolver = manager(tmp_path)
    assets.projects(discovery.project_files)
    assert assets.finish() == 0
    assert resolver.resolutions[0][1] == {"inline": "false"}
    ref = next(iter(assets.data["assets"].values()))["reference"]
    assert ref["project_id"] == "g-p-abc" and ref["conversation_id"] == ""


def test_interrupted_assets_record_pending_references_before_transfers(tmp_path):
    detail = image_chat()
    detail["mapping"]["m"]["message"]["metadata"]["attachments"].append(
        {"id": "file-next", "name": "next.png"}
    )
    assets, client = manager(tmp_path)

    class Interrupted(AttachmentHTTP):
        def download(self, url, destination, token, *, max_bytes):
            raise KeyboardInterrupt

    assets.http = Interrupted(client)
    with pytest.raises(KeyboardInterrupt):
        assets.process(detail, {**detail, "_attachment_source": "chat.md"})
    entries = list(assets.data["assets"].values())
    assert len(entries) == 2
    assert {entry["outcome"] for entry in entries} == {"interrupted", "not_attempted"}
    assert all(entry["source_paths"] == ["chat.md"] for entry in entries)
    assert assets.interrupted == 1
    assert len(assets.data["conversations"]["c"]["references"]) == 2


def test_canvas_materializes_offline_with_history_and_partial_status(tmp_path):
    def node(parent, recipient, payload):
        return {
            "parent": parent,
            "children": [],
            "message": {
                "author": {"role": "assistant"},
                "recipient": recipient,
                "content": {"text": json.dumps(payload)},
            },
        }

    data = {
        "id": "canvas-chat",
        "mapping": {
            "create": node(
                None,
                "canmore.create_textdoc",
                {"name": "Code", "type": "code/python", "content": "print(1)"},
            ),
            "update": node(
                "create",
                "canmore.update_textdoc",
                {"updates": [{"pattern": ".*", "replacement": "print(2)", "multiple": True}]},
            ),
        },
        "current_node": "update",
    }
    with AttachmentManager(tmp_path) as assets:
        assets.process(data)
        assert assets.finish() == 0
        entry = next(iter(assets.data["assets"].values()))
        assert (tmp_path / entry["path"]).read_text() == "print(2)"
        assert len(json.loads((tmp_path / entry["history"]).read_text())) == 2
        data["mapping"]["update"]["message"]["content"]["text"] = json.dumps(
            {"updates": [{"pattern": "missing", "replacement": "x"}]}
        )
        assets.process(data)
        assert assets.finish() == 1
        current = next(iter(assets.data["assets"].values()))
        assert current["outcome"] == "partial"
        assert (tmp_path / entry["path"]).exists()


def test_unresolved_canvas_preserves_history_without_fake_empty_document(tmp_path):
    data = {
        "id": "canvas",
        "current_node": "update",
        "mapping": {
            "update": {
                "parent": None,
                "message": {
                    "recipient": "canmore.update_textdoc",
                    "content": {
                        "text": json.dumps(
                            {"updates": [{"pattern": ".*", "replacement": "unknown target"}]}
                        )
                    },
                },
            }
        },
    }
    assets = AttachmentManager(tmp_path)
    assets.process(data)
    assert assets.finish() == 1
    entry = next(iter(assets.data["assets"].values()))
    assert entry["outcome"] == "partial" and "path" not in entry
    assert len(json.loads((tmp_path / entry["history"]).read_text())) == 1


def test_canvas_history_updates_even_when_final_text_is_unchanged(tmp_path):
    def op(parent, recipient, payload):
        return {
            "parent": parent,
            "message": {
                "author": {"role": "assistant"},
                "recipient": recipient,
                "content": {"text": json.dumps(payload)},
            },
        }

    data = {
        "id": "canvas",
        "current_node": "create",
        "mapping": {
            "create": op(
                None,
                "canmore.create_textdoc",
                {"name": "Doc", "type": "document", "content": "same"},
            )
        },
    }
    assets = AttachmentManager(tmp_path)
    assets.process(data)
    entry = next(iter(assets.data["assets"].values()))
    old_history_hash = entry["history_sha256"]
    data["mapping"]["update"] = op(
        "create", "canmore.update_textdoc", {"updates": [{"pattern": ".*", "replacement": "same"}]}
    )
    data["current_node"] = "update"
    assets.process(data)
    entry = next(iter(assets.data["assets"].values()))
    assert entry["history_sha256"] != old_history_hash
    assert len(json.loads((tmp_path / entry["history"]).read_text())) == 2
    assert len(assets.data["conversations"]["canvas"]["references"]) == 1
    saved = assets.saved
    assets.process(data)
    assert assets.saved == saved and assets.skipped == 1


def test_canvas_interrupt_does_not_mark_old_cached_version_as_current(tmp_path, monkeypatch):
    data = {
        "id": "canvas",
        "update_time": 1,
        "current_node": "create",
        "mapping": {
            "create": {
                "parent": None,
                "message": {
                    "recipient": "canmore.create_textdoc",
                    "content": {"text": json.dumps({"name": "Doc", "content": "old"})},
                },
            }
        },
    }
    assets = AttachmentManager(tmp_path)
    assets.process(data)
    data["update_time"] = 2
    data["mapping"]["create"]["message"]["content"]["text"] = json.dumps(
        {"name": "Doc", "content": "new"}
    )

    def interrupt(_):
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(assets, "valid", interrupt)
        with pytest.raises(KeyboardInterrupt):
            assets.process(data)
        assert assets.data["conversations"]["canvas"]["complete"] is False
        assert next(iter(assets.data["assets"].values()))["outcome"] == "not_attempted"
    assets.process(data)
    entry = next(iter(assets.data["assets"].values()))
    assert (tmp_path / entry["path"]).read_text() == "new"
    assert assets.conversation_complete("canvas", 2)


def test_large_assets_remain_saved_without_unbounded_browser_wrapper(tmp_path, monkeypatch):
    assets, _ = manager(tmp_path)
    assets.process(image_chat())
    assets.finish()
    source = tmp_path / "chat.json"
    source.write_text(json.dumps(image_chat()))
    monkeypatch.setattr("attachment_view.MAX_VIEWER_BYTES", 1)
    result = build_index(tmp_path)
    assert not result.warnings
    page = next((tmp_path / ".chatgpt-index/chats").glob("*.html")).read_text()
    assert "viewer cap" in page and 'class="attachment-open"' not in page
    assert next((tmp_path / ASSET_DIR / "files").iterdir()).read_bytes() == PNG


def test_unsafe_viewer_manifest_does_not_break_chat_index_or_link_outside_root(tmp_path):
    assets, _ = manager(tmp_path)
    assets.process(image_chat())
    assets.finish()
    entry = next(iter(assets.data["assets"].values()))
    entry["path"] = "../../outside.html"
    assets.checkpoint()
    (tmp_path / "chat.json").write_text(json.dumps(image_chat()))
    result = build_index(tmp_path)
    page = next((tmp_path / ".chatgpt-index/chats").glob("*.html")).read_text()
    assert result.chats == 1
    assert 'href="../../outside.html"' not in page
    assert "Unsafe or missing" in page
