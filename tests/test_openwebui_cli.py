import hashlib
import json
from copy import deepcopy
from unittest.mock import Mock

import pytest
import requests

from openwebui_import import build_parser, main


@pytest.fixture
def archive(tmp_path):
    files = {
        "chat.json": {
            "id": "source",
            "gizmo_id": "g-p-abc",
            "create_time": 1700000000,
            "update_time": 1700000001,
            "current_node": "u",
            "mapping": {
                "u": {
                    "parent": None,
                    "message": {"author": {"role": "user"}, "content": {"parts": ["Hello"]}},
                }
            },
        },
        "memories/memories.json": {"memories": [{"id": "m", "content": "Remember this"}]},
        "settings/custom_instructions.json": {"enabled": True, "about_model_message": "Be brief"},
    }
    for relative, data in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
    return tmp_path


def test_help_is_grouped_and_does_not_leak_environment(monkeypatch):
    monkeypatch.setenv("OPENWEBUI_URL", "https://private-instance.example")
    monkeypatch.setenv("OPENWEBUI_TOKEN", "secret-token")
    help_text = build_parser().format_help()
    for expected in [
        "what to import:",
        "execution:",
        "connection and resume:",
        "Examples:",
        "Environment",
        "Limits and safety:",
        "--all",
        "--dry-run",
        "OPENWEBUI_TOKEN",
        ".local/openwebui-import.sqlite3",
    ]:
        assert expected in help_text
    assert "private-instance" not in help_text
    assert "secret-token" not in help_text
    assert max(map(len, help_text.splitlines())) <= 88


def test_help_exits_successfully(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    assert "Examples:" in capsys.readouterr().out


@pytest.mark.parametrize("flags", [[], ["--dry-run"]])
def test_all_defaults_to_local_preview(archive, monkeypatch, capsys, flags):
    session = Mock(side_effect=AssertionError("Unexpected network"))
    monkeypatch.setattr(requests, "Session", session)
    state = archive / "journal.sqlite3"
    assert main([str(archive), "--all", "--state", str(state), *flags]) == 0
    output = capsys.readouterr().out
    assert "Projects: 1 folders; 1 assigned chats" in output
    assert "Validated 1 conversations" in output
    assert "Extras: 1 memory/prompt records; system prompt: True" in output
    assert "manifest_missing" in output
    assert "replacement deletes" not in output
    assert "existing chat moves" not in output
    assert not state.exists()
    session.assert_not_called()


def test_all_reusable_prompt_override(archive, capsys):
    assert main([str(archive), "--all", "--instructions", "prompt"]) == 0
    assert "Extras: 2 memory/prompt records; system prompt: False" in capsys.readouterr().out


def test_all_rejects_only_extras(archive, capsys):
    assert main([str(archive), "--all", "--only-extras"]) == 1
    assert "--all cannot be combined with --only-extras" in capsys.readouterr().err


def test_all_missing_data_fails_before_network(archive, monkeypatch):
    (archive / "memories/memories.json").unlink()
    session = Mock(side_effect=AssertionError("Unexpected network"))
    monkeypatch.setattr(requests, "Session", session)
    assert main([str(archive), "--all", "--upload"]) == 1
    session.assert_not_called()


def test_all_rejects_single_json_file(archive):
    assert main([str(archive / "chat.json"), "--all"]) == 1


def test_execution_modes_are_mutually_exclusive(archive):
    with pytest.raises(SystemExit) as exc:
        main([str(archive), "--upload", "--dry-run"])
    assert exc.value.code == 2


def test_all_upload_uses_selected_endpoints(archive, monkeypatch):
    session = Mock()
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=False)

    def respond(method, url, **kwargs):
        response = Mock(status_code=200)
        data: dict | list
        if url.endswith("/auths/"):
            data = {"id": "user"}
        elif url.endswith("/users/user/settings"):
            data = {"ui": {"system": "Old personal instructions", "theme": "dark"}}
        elif url.endswith("/users/user/settings/update"):
            assert kwargs["json"]["ui"]["theme"] == "dark"
            assert "Be brief" in kwargs["json"]["ui"]["system"]
            data = kwargs["json"]
        elif method == "GET":
            assert url.endswith("/folders/")
            data = []
        elif url.endswith("/chats/import"):
            data = [{"id": "chat", **kwargs["json"]["chats"][0]}]
        else:
            data = {"id": "remote", **kwargs["json"]}
        response.json.return_value = data
        return response

    session.request.side_effect = respond
    monkeypatch.setattr(requests, "Session", lambda: session)
    monkeypatch.setenv("OPENWEBUI_TOKEN", "test")
    assert (
        main(
            [
                str(archive),
                "--all",
                "--upload",
                "--url",
                "https://example.test",
                "--state",
                str(archive / "journal.sqlite3"),
            ]
        )
        == 0
    )
    posts = [call.args[1] for call in session.request.call_args_list if call.args[0] == "POST"]
    assert posts == [
        "https://example.test/api/v1/users/user/settings/update",
        "https://example.test/api/v1/memories/add",
        "https://example.test/api/v1/folders/",
        "https://example.test/api/v1/chats/import",
    ]


@pytest.mark.parametrize("same_name", [False, True])
@pytest.mark.parametrize("instructions", ["prompt", "system"])
def test_all_routes_projects_and_extras_without_duplicate_uploads(
    archive, monkeypatch, same_name, instructions
):
    template = json.loads((archive / "chat.json").read_text())
    index = {}
    expected = {}
    source_dates = {}
    names = {"g-p-abc": "Research", "g-p-def": "Research" if same_name else "Writing"}
    # Two chats share one project; a second project must stay separate even if names match.
    # A custom GPT is not a project and must remain unfiled.
    for number, project in enumerate(["g-p-abc", "g-p-abc", "g-p-def", None, "g-custom"]):
        detail = deepcopy(template)
        ident = f"chat-{number}"
        detail.update(
            id=ident,
            gizmo_id=project,
            create_time=1700000000 + number,
            update_time=1700000100 + number,
            is_archived=number == 0,
            is_starred=number == 1,
        )
        folder = "_Uncategorized"
        if project in names:
            suffix = hashlib.sha256(project.encode()).hexdigest()[:12]
            folder = f"{names[project]}--{suffix}"
        path = archive / folder / f"{ident}.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(detail))
        index[f"json:{ident}"] = {"path": str(path.relative_to(archive))}
        expected[ident] = project if project in names else None
        source_dates[ident] = (detail["create_time"], detail["update_time"])
    (archive / ".export-index.json").write_text(json.dumps(index))

    session = Mock()
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=False)
    folders: dict[str, dict] = {}
    chats: dict[str, dict] = {}
    memories = []
    prompts = []
    settings = {"ui": {"theme": "dark"}}

    def respond(method, url, **kwargs):
        payload = deepcopy(kwargs.get("json", {}))
        assert isinstance(payload, dict)
        data: dict | list
        route = url.removeprefix("https://example.test/api/v1/")
        if route == "auths/":
            data = {"id": "user"}
        elif route == "users/user/settings":
            data = settings
        elif route == "users/user/settings/update":
            settings.update(payload)
            data = settings
        elif route == "memories/add":
            memories.append(payload)
            data = {"id": "memory", **payload}
        elif route == "prompts/create":
            prompts.append(payload)
            data = {"id": "prompt", **payload}
        elif route == "folders/" and method == "GET":
            data = list(folders.values())
        elif route == "folders/" and method == "POST":
            project = payload["meta"]["chatgpt_project_id"]
            remote_id = f"folder-{project}"
            assert remote_id not in folders
            folders[remote_id] = {"id": remote_id, **payload}
            data = folders[remote_id]
        elif route.startswith("folders/") and method == "GET":
            data = folders[route.removeprefix("folders/")]
        elif route == "chats/import":
            form = payload["chats"][0]
            ident = form["meta"]["chatgpt_conversation_id"]
            project = expected[ident]
            if project is not None:
                folder_id = form["folder_id"]
                # The folder must exist before this import, and belong to this project.
                assert folders[folder_id]["meta"]["chatgpt_project_id"] == project
            else:
                assert "folder_id" not in form
            assert ident not in chats
            assert (form["created_at"], form["updated_at"]) == source_dates[ident]
            chats[ident] = {"id": ident, **form}
            data = [chats[ident]]
        elif route.startswith("chats/") and method == "GET":
            data = chats[route.removeprefix("chats/")]
        else:
            raise AssertionError(f"Unexpected request: {method} {route}")
        response = Mock(status_code=200)
        response.json.return_value = deepcopy(data)
        return response

    session.request.side_effect = respond
    monkeypatch.setattr(requests, "Session", lambda: session)
    monkeypatch.setenv("OPENWEBUI_TOKEN", "test")
    args = [
        str(archive),
        "--all",
        "--upload",
        "--url",
        "https://example.test",
        "--state",
        str(archive / "journal.sqlite3"),
    ]
    if instructions == "prompt":
        args.extend(["--instructions", "prompt"])
    assert main(args) == 0
    post_count = sum(call.args[0] == "POST" for call in session.request.call_args_list)
    assert main(args) == 0
    assert sum(call.args[0] == "POST" for call in session.request.call_args_list) == post_count
    assert len(chats) == 5
    assert len(folders) == 2
    assert len({folder["name"] for folder in folders.values()}) == 2
    for project, name in names.items():
        expected_name = f"{name} [{project}]" if same_name else name
        assert folders[f"folder-{project}"]["name"] == expected_name
    assert chats["chat-0"]["folder_id"] == chats["chat-1"]["folder_id"]
    assert chats["chat-0"]["archived"] is True
    assert chats["chat-1"]["pinned"] is True
    assert memories == [{"content": "Remember this", "type": "user"}]
    if instructions == "prompt":
        assert len(prompts) == 1 and "Be brief" in prompts[0]["content"]
        assert "system" not in settings["ui"]
    else:
        assert prompts == []
        assert "Be brief" in settings["ui"]["system"]
        assert settings["ui"]["theme"] == "dark"
