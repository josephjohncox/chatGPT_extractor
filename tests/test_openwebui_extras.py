import json
import sqlite3
from unittest.mock import Mock

import pytest
import requests

from openwebui_import import apply_system_prompt, load_extras, main, upload_one


def save(root, relative, data):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


@pytest.fixture
def archive(tmp_path):
    save(
        tmp_path,
        "memories/memories.json",
        {
            "memories": [
                {"id": "m1", "content": "A saved preference", "status": "warm"},
                {"id": "m2", "content": "Deleted preference", "status": "deleted"},
            ]
        },
    )
    save(
        tmp_path,
        "settings/custom_instructions.json",
        {
            "enabled": True,
            "about_user_message": "About me",
            "other_user_message": "About me",
            "about_model_message": "Be concise",
            "traits_model_message": "Be concise",
            "role_user_message": "Engineer",
        },
    )
    return tmp_path


def test_load_memories_and_private_prompt(archive):
    extras, system = load_extras(archive, True, "prompt")
    assert system is None
    assert [item[0] for item in extras] == ["memory", "prompt"]
    assert extras[0][2] == {"content": "A saved preference", "type": "user"}
    prompt = extras[1][2]
    assert prompt["content"].count("Be concise") == 1
    assert "Engineer" in prompt["content"]
    assert prompt["access_grants"] == []
    assert prompt["access_control"] == {}
    assert prompt["meta"]["chatgpt_source"]["enabled"] is True


def test_system_is_explicit_and_disabled_source_rejected(archive):
    extras, system = load_extras(archive, False, "system")
    assert extras == []
    assert system is not None and "Be concise" in system
    save(archive, "settings/custom_instructions.json", {"enabled": False})
    with pytest.raises(ValueError, match="disabled"):
        load_extras(archive, False, "system")


def test_unknown_memory_status_rejected(archive):
    save(
        archive,
        "memories/memories.json",
        {"memories": [{"id": "m1", "content": "text", "status": "unexpected"}]},
    )
    with pytest.raises(ValueError, match="status"):
        load_extras(archive, True, None)


def response(data):
    result = Mock(status_code=200)
    result.json.return_value = data
    return result


def test_system_preserves_settings_and_is_idempotent():
    session = Mock()
    settings = {"ui": {"theme": "dark"}, "other": {"keep": True}}
    expected = {"ui": {"theme": "dark", "system": "Instructions"}, "other": {"keep": True}}
    session.request.side_effect = [response(settings), response(expected), response(expected)]
    apply_system_prompt(session, "https://example.test", "Instructions")
    assert session.request.call_args.kwargs["json"] == expected
    apply_system_prompt(session, "https://example.test", "Instructions")
    assert session.request.call_count == 3
    assert settings == {"ui": {"theme": "dark"}, "other": {"keep": True}}


def test_personal_system_prompt_updates_without_touching_global_or_model_config():
    session = Mock()
    before = {"ui": {"system": "Existing instructions", "theme": "dark"}}
    after = {"ui": {"system": "New instructions", "theme": "dark"}}
    session.request.side_effect = [response(before), response(after)]
    apply_system_prompt(session, "https://example.test", "New instructions")
    assert session.request.call_args.kwargs["json"] == after
    assert [call.args for call in session.request.call_args_list] == [
        ("GET", "https://example.test/api/v1/users/user/settings"),
        ("POST", "https://example.test/api/v1/users/user/settings/update"),
    ]


def test_system_permission_failure_detected():
    session = Mock()
    session.request.side_effect = [response({}), response({})]
    with pytest.raises(ValueError, match="confirm"):
        apply_system_prompt(session, "https://example.test", "New instructions")


@pytest.mark.parametrize(
    "resource,endpoint", [("memory", "/api/v1/memories/add"), ("prompt", "/api/v1/prompts/create")]
)
def test_extra_upload_resume(resource, endpoint):
    with sqlite3.connect(":memory:") as db:
        db.execute(
            "CREATE TABLE imports (scope TEXT, source_id TEXT, digest TEXT, "
            "remote_id TEXT, PRIMARY KEY(scope, source_id))"
        )
        session = Mock()
        form = {"content": "Example"}
        session.request.return_value = response({"id": "remote", **form})
        args = (db, session, "https://example.test", resource, "source", "hash", form)
        assert upload_one(*args, resource=resource)
        assert not upload_one(*args, resource=resource)
        assert session.request.call_count == 1
        assert session.request.call_args.args == ("POST", "https://example.test" + endpoint)
        assert session.request.call_args.kwargs["json"] == form


def test_extras_dry_run_no_network(archive, monkeypatch):
    session = Mock(side_effect=AssertionError("Unexpected network"))
    monkeypatch.setattr(requests, "Session", session)
    assert (
        main([str(archive), "--only-extras", "--include-memories", "--instructions", "prompt"]) == 0
    )
    session.assert_not_called()


def test_extras_cli_upload(archive, monkeypatch):
    session = Mock()
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=False)

    def respond(method, url, **kwargs):
        return response(
            {"id": "account"} if method == "GET" else {"id": "remote", **kwargs["json"]}
        )

    session.request.side_effect = respond
    monkeypatch.setattr(requests, "Session", lambda: session)
    monkeypatch.setenv("OPENWEBUI_TOKEN", "test")
    args = [
        str(archive),
        "--only-extras",
        "--include-memories",
        "--instructions",
        "prompt",
        "--upload",
        "--url",
        "https://example.test",
        "--state",
        str(archive / "state.db"),
    ]
    assert main(args) == 0
    assert main(args) == 0
    assert [call.args[0] for call in session.request.call_args_list] == [
        "GET",
        "POST",
        "POST",
        "GET",
    ]
