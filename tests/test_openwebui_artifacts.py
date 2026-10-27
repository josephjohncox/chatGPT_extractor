import hashlib
import json
import sqlite3
from copy import deepcopy
from unittest.mock import Mock

import pytest
import requests

from openwebui_artifacts import (
    chat_asset_links,
    enrich_chat,
    load_artifacts,
    update_existing_artifacts,
    upload_artifacts,
)
from openwebui_import import convert, main


@pytest.fixture
def artifact_archive(tmp_path):
    assets = tmp_path / ".chatgpt-assets"
    assets.mkdir()
    (assets / ".managed").write_text("chatgpt-extractor attachments v1")
    entries = {}
    for key, kind, name, content in [
        ("download", "sandbox", "report.csv", b"a,b\n1,2\n"),
        ("canvas", "canvas", "Draft.md", b"# Draft\n<script>bad()</script>\n"),
        ("html", "canvas", "Page.html", b"<script>bad()</script>"),
    ]:
        path = assets / name
        path.write_bytes(content)
        entry = {
            "reference": {
                "kind": kind,
                "conversation_id": "source",
                "message_id": "u",
                "locator": "/mnt/data/report.csv" if kind == "sandbox" else key,
            },
            "outcome": "partial" if key == "canvas" else "saved",
            "path": str(path.relative_to(tmp_path)),
            "name": name,
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        if kind == "canvas":
            history = assets / f"{key}.history.json"
            history.write_bytes(b"[]")
            entry.update(
                history=str(history.relative_to(tmp_path)),
                history_bytes=2,
                history_sha256=hashlib.sha256(b"[]").hexdigest(),
            )
        entries[key] = entry
    entries["lost"] = {"reference": {"kind": "file"}, "outcome": "unavailable"}
    (assets / "manifest.json").write_text(
        json.dumps(
            {
                "version": 1,
                "assets": entries,
                "conversations": {"source": {"references": list(entries)}},
            }
        )
    )
    detail = {
        "id": "source",
        "title": "Original",
        "gizmo_id": "g-p-abc",
        "create_time": 1700000000,
        "update_time": 1700000001,
        "is_archived": True,
        "is_starred": True,
        "current_node": "u",
        "mapping": {
            "u": {
                "parent": None,
                "message": {
                    "author": {"role": "user"},
                    "content": {"parts": ["[report](sandbox:/mnt/data/report.csv)"]},
                },
            }
        },
    }
    (tmp_path / "chat.json").write_text(json.dumps(detail))
    for path, data in [
        ("memories/memories.json", {"memories": [{"id": "m", "content": "Preference"}]}),
        ("settings/custom_instructions.json", {"enabled": True, "about_model_message": "Be brief"}),
    ]:
        file = tmp_path / path
        file.parent.mkdir(parents=True)
        file.write_text(json.dumps(data))
    return tmp_path


def test_manifest_files_histories_notes_and_partial_warning(artifact_archive):
    plan = load_artifacts(artifact_archive, canvas_notes=True)
    assert len(plan.files) == 5
    assert list(plan.notes) == ["canvas"]  # HTML is not rendered into Notes.
    assert "PARTIAL" in plan.notes["canvas"]
    assert "&lt;script&gt;" in plan.notes["canvas"]
    assert plan.gaps == {"unavailable": 1}
    assert set(plan.conversations["source"]) == {"canvas", "html", "download"}


@pytest.mark.parametrize("change", ["checksum", "missing", "symlink", "traversal"])
def test_invalid_artifact_fails_preflight(artifact_archive, change):
    path = artifact_archive / ".chatgpt-assets/report.csv"
    if change == "checksum":
        path.write_bytes(b"x" * path.stat().st_size)
    elif change == "missing":
        path.unlink()
    elif change == "symlink":
        path.unlink()
        path.symlink_to(artifact_archive / "chat.json")
    else:
        manifest = artifact_archive / ".chatgpt-assets/manifest.json"
        data = json.loads(manifest.read_text())
        data["assets"]["download"]["path"] = "../secret"
        manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        load_artifacts(artifact_archive)


def fake_uploads(session, method, url, **kwargs):
    assert method == "POST"
    if url.endswith("/files/"):
        assert kwargs["params"] == {"process": "false"}
        name, stream, mime = kwargs["files"]["file"]
        assert mime == "application/octet-stream"
        content = stream.read()
        return {
            "id": name,
            "meta": {"size": len(content), "file_hash": hashlib.sha256(content).hexdigest()},
        }
    assert url.endswith("/notes/create")
    assert kwargs["json"]["access_grants"] == []
    return {"id": "note", **kwargs["json"]}


def test_uploads_are_verified_journaled_and_links_are_idempotent(artifact_archive):
    plan = load_artifacts(artifact_archive, canvas_notes=True)
    api = Mock(side_effect=fake_uploads)
    with sqlite3.connect(":memory:") as db:
        links = upload_artifacts(plan, db, None, "https://example.test/prefix", "scope", api)
        assert len(links) == 3
        assert api.call_count == 6
        assert (
            upload_artifacts(plan, db, None, "https://example.test/prefix", "scope", api) == links
        )
        assert api.call_count == 6
    detail = json.loads((artifact_archive / "chat.json").read_text())
    chat = convert(detail)["chat"]
    associated = chat_asset_links(plan, links, "source", detail)
    enriched = enrich_chat(chat, associated)
    assert enriched["chatgpt_source"] == detail
    assert "sandbox:/mnt/data/report.csv" not in enriched["history"]["messages"]["u"]["content"]
    assert "attachment=true" in enriched["messages"][0]["content"]
    assert "Partial reconstruction" in enriched["messages"][0]["content"]
    assert "Editable Note" in enriched["messages"][0]["content"]
    assert enrich_chat(enriched, associated) == enriched
    assert "chatgpt_artifacts" not in chat


def test_uncertain_file_upload_not_retried(artifact_archive):
    plan = load_artifacts(artifact_archive)
    api = Mock(side_effect=requests.Timeout())
    with sqlite3.connect(":memory:") as db:
        with pytest.raises(requests.Timeout):
            upload_artifacts(plan, db, None, "https://example.test", "scope", api)
        with pytest.raises(ValueError, match="Uncertain"):
            upload_artifacts(plan, db, None, "https://example.test", "scope", api)
    assert api.call_count == 1


def test_server_checksum_failure_leaves_pending(artifact_archive):
    plan = load_artifacts(artifact_archive)
    api = Mock(return_value={"id": "wrong", "meta": {"size": 1, "file_hash": "bad"}})
    with sqlite3.connect(":memory:") as db:
        with pytest.raises(ValueError, match="checksum"):
            upload_artifacts(plan, db, None, "https://example.test", "scope", api)
        assert db.execute("SELECT remote_id FROM artifact_uploads").fetchall() == [(None,)]


def test_all_replacement_includes_artifacts_and_preserves_dates(artifact_archive, monkeypatch):
    session = Mock()
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=False)
    chats = {}
    folders = {}
    deleted = []
    settings: dict = {}
    next_id = 0

    def respond(method, url, **kwargs):
        nonlocal next_id
        route = url.removeprefix("https://example.test/api/v1/")
        data: dict | list | bool
        if route == "auths/":
            data = {"id": "user"}
        elif route == "users/user/settings":
            data = settings
        elif route == "users/user/settings/update":
            settings.update(kwargs["json"])
            data = settings
        elif route in {"files/", "notes/create"}:
            data = fake_uploads(session, method, url, **kwargs)
        elif route in {"memories/add", "prompts/create"}:
            data = {"id": "extra", **kwargs["json"]}
        elif route == "folders/" and method == "POST":
            folders["folder"] = {"id": "folder", **kwargs["json"]}
            data = folders["folder"]
        elif route == "folders/":
            data = list(folders.values())
        elif route == "folders/folder":
            data = folders["folder"]
        elif route == "chats/import":
            next_id += 1
            ident = f"remote-{next_id}"
            chats[ident] = {"id": ident, "user_id": "user", **deepcopy(kwargs["json"]["chats"][0])}
            data = [chats[ident]]
        elif route.startswith("chats/"):
            ident = route.removeprefix("chats/")
            if method == "DELETE":
                deleted.append(ident)
                del chats[ident]
                data = True
            else:
                assert method == "GET"  # No chat updates/moves: dates must stay original.
                data = chats[ident]
        else:
            raise AssertionError((method, route))
        response = Mock(status_code=200)
        response.json.return_value = deepcopy(data)
        return response

    session.request.side_effect = respond
    monkeypatch.setattr(requests, "Session", lambda: session)
    monkeypatch.setenv("OPENWEBUI_TOKEN", "test")
    args = [
        str(artifact_archive),
        "--upload",
        "--url",
        "https://example.test",
        "--state",
        str(artifact_archive / "journal.sqlite3"),
    ]
    assert main(args) == 0  # Simulate an old chats-only import.
    chats["unrelated"] = {"id": "unrelated", "user_id": "user"}
    assert main([*args, "--all", "--replace-imported-chats"]) == 0
    assert deleted == ["remote-1"]
    assert set(chats) == {"unrelated", "remote-2"}
    result = chats["remote-2"]
    assert (result["created_at"], result["updated_at"]) == (1700000000, 1700000001)
    assert result["archived"] is True and result["pinned"] is True
    assert result["folder_id"] == "folder"
    assert len(result["chat"]["chatgpt_artifacts"]) == 3
    writes = sum(call.args[0] != "GET" for call in session.request.call_args_list)
    assert main([*args, "--all", "--replace-imported-chats"]) == 0
    assert sum(call.args[0] != "GET" for call in session.request.call_args_list) == writes


def test_dry_run_replace_never_contacts_server(artifact_archive, monkeypatch):
    monkeypatch.setattr(requests, "Session", Mock(side_effect=AssertionError("network")))
    state = artifact_archive / "journal.sqlite3"
    assert (
        main([str(artifact_archive), "--all", "--replace-imported-chats", "--state", str(state)])
        == 0
    )
    assert not state.exists()


def test_existing_artifact_update_requires_consent_and_keeps_remote_edits(artifact_archive):
    plan = load_artifacts(artifact_archive)
    with sqlite3.connect(":memory:") as db:
        links = upload_artifacts(plan, db, None, "https://example.test", "scope", fake_uploads)
        db.execute("CREATE TABLE imports (scope TEXT, source_id TEXT, digest TEXT, remote_id TEXT)")
        db.execute("INSERT INTO imports VALUES ('scope', 'source', 'hash', 'remote')")
        db.commit()
        chat = convert(json.loads((artifact_archive / "chat.json").read_text()))["chat"]
        chat["history"]["messages"]["u"]["content"] = "User edit on server"
        remote = {"id": "remote", "chat": chat}
        api = Mock(return_value=remote)
        args = (db, None, "https://example.test", "scope", "source", list(links.values()))
        with pytest.raises(ValueError, match="update-existing-artifacts"):
            update_existing_artifacts(*args, False, api)
        assert api.call_count == 1
        enriched = enrich_chat(chat, list(links.values()))
        api.side_effect = [remote, {"chat": enriched}]
        update_existing_artifacts(*args, True, api)
        patch = api.call_args.kwargs["json"]["chat"]
        assert patch["history"]["messages"]["u"]["content"].startswith("User edit on server")
        assert "chatgpt_source" not in patch
