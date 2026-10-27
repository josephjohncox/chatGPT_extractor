import hashlib
import json
import sqlite3
from unittest.mock import Mock

import pytest
import requests

from openwebui_import import (
    assign_existing_folder,
    ensure_project_folder,
    load_conversations,
    main,
    project_for_chat,
    upload_one,
)


def response(data):
    result = Mock(status_code=200)
    result.json.return_value = data
    return result


@pytest.fixture
def db():
    with sqlite3.connect(":memory:") as connection:
        connection.execute(
            "CREATE TABLE imports (scope TEXT, source_id TEXT, digest TEXT, "
            "remote_id TEXT, PRIMARY KEY(scope, source_id))"
        )
        yield connection


@pytest.fixture
def archive(tmp_path):
    project = "g-p-abc123"
    suffix = hashlib.sha256(project.encode()).hexdigest()[:12]
    path = tmp_path / f"Research--{suffix}" / "chat.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps(
            {
                "id": "source",
                "title": "Chat",
                "gizmo_id": project,
                "create_time": 1700000000,
                "update_time": 1700000001,
                "mapping": {
                    "u": {
                        "parent": None,
                        "message": {"author": {"role": "user"}, "content": {"parts": ["Question"]}},
                    }
                },
                "current_node": "u",
            }
        )
    )
    (tmp_path / ".export-index.json").write_text(
        json.dumps({"json:source": {"path": str(path.relative_to(tmp_path))}})
    )
    return tmp_path


def test_project_name_from_export_path_and_unchanged_digest(archive):
    original = list(load_conversations(archive))
    projects: dict[str, tuple[str, str] | None] = {}
    assert list(load_conversations(archive, projects)) == original
    assert projects == {"source": ("g-p-abc123", "Research")}


def test_only_real_projects_get_folders(tmp_path):
    for detail in [{}, {"gizmo_id": "g-customgpt"}, {"workspace_id": "workspace"}]:
        assert project_for_chat(detail, tmp_path / "chat.json", tmp_path) is None
    assert project_for_chat({"gizmo_id": "g-p-abc"}, tmp_path / "chat.json", tmp_path) == (
        "g-p-abc",
        "g-p-abc",
    )


def test_folder_creation_and_resume(db):
    remote = {"id": "folder", "meta": {"chatgpt_project_id": "g-p-abc"}}
    session = Mock()
    session.request.side_effect = [response([]), response(remote), response(remote)]
    args = (db, session, "https://example.test", "scope", "g-p-abc", "Research")
    assert ensure_project_folder(*args) == "folder"
    assert ensure_project_folder(*args) == "folder"
    calls = session.request.call_args_list
    assert [call.args[0] for call in calls] == ["GET", "POST", "GET"]
    assert calls[1].kwargs["json"] == {
        "name": "Research",
        "meta": {"chatgpt_project_id": "g-p-abc"},
    }


def test_existing_same_name_folder_reused_without_modifying_settings(db):
    session = Mock()
    folder = {
        "id": "existing",
        "name": "Research",
        "parent_id": None,
        "meta": {},
        "data": {"system": "Project instructions", "files": ["keep"]},
    }
    session.request.side_effect = [response([folder]), response(folder), response(folder)]
    args = (db, session, "https://example.test", "scope", "g-p-abc", "Research")
    assert ensure_project_folder(*args) == "existing"
    assert ensure_project_folder(*args) == "existing"
    assert all(call.args[0] == "GET" for call in session.request.call_args_list)
    assert db.execute("SELECT remote_id FROM project_folders").fetchone() == ("existing",)


def test_ambiguous_same_name_folders_do_not_create_duplicate(db):
    session = Mock()
    session.request.return_value = response(
        [
            {"id": "one", "name": "Research"},
            {"id": "two", "name": "Research", "parent_id": "parent"},
        ]
    )
    with pytest.raises(ValueError, match="Multiple existing"):
        ensure_project_folder(db, session, "https://example.test", "scope", "g-p-abc", "Research")
    assert session.request.call_count == 1


def test_conflicting_source_project_marker_is_not_merged(db):
    session = Mock()
    folder = {"id": "existing", "name": "Research", "meta": {"chatgpt_project_id": "g-p-other"}}
    session.request.side_effect = [response([folder]), response(folder)]
    with pytest.raises(ValueError, match="identity"):
        ensure_project_folder(db, session, "https://example.test", "scope", "g-p-abc", "Research")
    assert all(call.args[0] == "GET" for call in session.request.call_args_list)


def test_uncertain_creation_not_retried(db):
    session = Mock()
    session.request.side_effect = [response([]), requests.Timeout()]
    args = (db, session, "https://example.test", "scope", "g-p-abc", "Research")
    with pytest.raises(requests.Timeout):
        ensure_project_folder(*args)
    session.request.side_effect = [response([])]
    with pytest.raises(ValueError, match="Uncertain"):
        ensure_project_folder(*args)
    assert session.request.call_count == 3


def test_folder_is_part_of_initial_import_not_digest(db, archive):
    ident, digest, form = next(load_conversations(archive))
    session = Mock()
    session.request.return_value = response([{"id": "remote", **form, "folder_id": "folder"}])
    assert upload_one(
        db, session, "https://example.test", "scope", ident, digest, form, folder_id="folder"
    )
    assert "folder_id" not in form
    assert session.request.call_args.kwargs["json"]["chats"][0]["folder_id"] == "folder"
    assert db.execute("SELECT digest FROM imports").fetchone()[0] == digest


def test_server_silently_drops_folder(db, archive):
    ident, digest, form = next(load_conversations(archive))
    session = Mock()
    session.request.return_value = response([{"id": "remote", **form}])
    with pytest.raises(ValueError, match="folder assignment"):
        upload_one(
            db, session, "https://example.test", "scope", ident, digest, form, folder_id="folder"
        )
    assert db.execute("SELECT remote_id FROM imports").fetchone() == (None,)


def test_existing_chat_move_requires_explicit_consent(db):
    with db:
        db.execute("INSERT INTO imports VALUES ('scope', 'source', 'hash', 'remote')")
    session = Mock()
    session.request.return_value = response({"id": "remote", "folder_id": None})
    args = (db, session, "https://example.test", "scope", "source", "folder")
    with pytest.raises(ValueError, match="move-existing"):
        assign_existing_folder(*args, False)
    assert session.request.call_count == 1
    session.request.side_effect = [
        response({"id": "remote", "folder_id": None}),
        response({"id": "remote", "folder_id": "folder"}),
        response({"id": "remote", "folder_id": "folder"}),
    ]
    assign_existing_folder(*args, True)
    assign_existing_folder(*args, True)
    assert [call.args[0] for call in session.request.call_args_list] == [
        "GET",
        "GET",
        "POST",
        "GET",
    ]


def test_dry_run_projects(archive, monkeypatch, capsys):
    session = Mock(side_effect=AssertionError("Unexpected network"))
    monkeypatch.setattr(requests, "Session", session)
    assert main([str(archive), "--project-folders"]) == 0
    assert "Projects: 1 folders; 1 assigned chats" in capsys.readouterr().out
    session.assert_not_called()


def test_cli_folder_import_then_resume(archive, monkeypatch):
    session = Mock()
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=False)
    folder = {"id": "folder", "meta": {"chatgpt_project_id": "g-p-abc123"}}

    def respond(method, url, **kwargs):
        if url.endswith("/auths/"):
            return response({"id": "user"})
        if url.endswith("/folders/"):
            return response([] if method == "GET" else folder)
        if url.endswith("/folders/folder"):
            return response(folder)
        if url.endswith("/chats/import"):
            return response([{"id": "remote", **kwargs["json"]["chats"][0]}])
        if url.endswith("/chats/remote"):
            return response({"id": "remote", "folder_id": "folder"})
        raise AssertionError(url)

    session.request.side_effect = respond
    monkeypatch.setattr(requests, "Session", lambda: session)
    monkeypatch.setenv("OPENWEBUI_TOKEN", "test")
    args = [
        str(archive),
        "--project-folders",
        "--upload",
        "--url",
        "https://example.test",
        "--state",
        str(archive / "state.sqlite3"),
    ]
    assert main(args) == 0
    assert main(args) == 0
    assert [call.args[1] for call in session.request.call_args_list if call.args[0] == "POST"] == [
        "https://example.test/api/v1/folders/",
        "https://example.test/api/v1/chats/import",
    ]
