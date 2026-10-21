import json

import pytest

from chatgpt_client import ChatGPTClient, HTTPError
from chatgpt_exporter import export_auxiliary_data, main
from export_config import Config


@pytest.fixture
def auxiliary_calls(monkeypatch):
    calls = []

    class Client(ChatGPTClient):
        def access_token(self):
            return "fake-access"

        def all_conversations(self, token):
            return []

        def json(self, path, token, **kwargs):
            calls.append(path)
            return {"items": []}

    monkeypatch.setattr("chatgpt_exporter.ChatGPTClient", Client)
    return calls


@pytest.mark.parametrize(
    "args,setting,included",
    [
        ([], None, True),
        (["--no-auxiliary"], None, False),
        ([], "false", False),
        (["--no-auxiliary"], "true", False),
        (["--auxiliary"], "false", True),
        (["--auxiliary"], "true", True),
    ],
)
def test_default_snapshot_and_explicit_overrides(
    tmp_path, monkeypatch, auxiliary_calls, args, setting, included
):
    if setting is not None:
        monkeypatch.setenv("CHATGPT_AUXILIARY", setting)
    assert main(["export", "--no-progress", "--output", str(tmp_path), *args]) == 0
    if included:
        assert len(auxiliary_calls) == 8
        assert {"memories", "user_system_messages", "settings/user", "compliance"} <= set(
            auxiliary_calls
        )
        statuses = json.loads((tmp_path / "auxiliary-status.json").read_text())
        assert len(statuses) == 8 and all(item["available"] for item in statuses.values())
    else:
        assert auxiliary_calls == []
        assert not (tmp_path / "auxiliary-status.json").exists()
    assert (tmp_path / "index.html").is_file()


@pytest.mark.parametrize("command", ["probe", "index", "import"])
def test_non_export_commands_do_not_fetch_auxiliary_data(
    tmp_path, monkeypatch, auxiliary_calls, command
):
    def forbidden(*args, **kwargs):
        pytest.fail("Auxiliary snapshots must run only during web exports")

    monkeypatch.setattr("chatgpt_exporter.export_auxiliary_data", forbidden)
    args = [command, "--no-progress"]
    if command == "index":
        args.append(str(tmp_path))
    elif command == "import":
        source = tmp_path / "archive.json"
        source.write_text("[]")
        args.extend([str(source), "--output", str(tmp_path / "imported")])
    assert main(args) == 0
    assert "memories" not in auxiliary_calls
    if command == "probe":
        assert set(auxiliary_calls) == {"conversations", "gizmos/snorlax/sidebar", "settings/user"}
    else:
        assert auxiliary_calls == []


def test_optional_forbidden_endpoint_is_unavailable_not_global_auth_failure(tmp_path):
    old = tmp_path / "settings/settings.json"
    old.parent.mkdir()
    old.write_text('{"previous": true}')

    class Client(ChatGPTClient):
        def json(self, path, token, **kwargs):
            if path == "memories":
                return {"items": []}
            raise HTTPError(403, path)

    export_auxiliary_data(Client(Config()), "fake", tmp_path)
    statuses = json.loads((tmp_path / "auxiliary-status.json").read_text())
    assert statuses["memories/memories.json"]["outcome"] == "saved"
    unavailable = [item for item in statuses.values() if not item["available"]]
    assert len(unavailable) == 7
    assert all(item["outcome"] == "unavailable" and item["attempted"] for item in unavailable)
    assert old.read_text() == '{"previous": true}'


@pytest.mark.parametrize("status,challenge", [(401, False), (403, True)])
def test_auth_expiry_or_challenge_still_aborts_and_records_unattempted(tmp_path, status, challenge):
    class Client(ChatGPTClient):
        def json(self, path, token, **kwargs):
            raise HTTPError(status, path, browser_challenge=challenge)

    with pytest.raises(HTTPError):
        export_auxiliary_data(Client(Config()), "fake", tmp_path)
    statuses = json.loads((tmp_path / "auxiliary-status.json").read_text())
    assert statuses["memories/memories.json"]["outcome"] == "failed"
    assert statuses["settings/settings.json"]["outcome"] == "not_attempted"
