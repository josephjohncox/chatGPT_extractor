import copy
import json
import sqlite3
from unittest.mock import Mock

import pytest
import requests

from openwebui_import import convert, load_conversations, main, request, timestamp, upload_one


@pytest.fixture
def conversation():
    def node(parent, role, text, date=1700000001):
        return {
            "parent": parent,
            "message": {
                "author": {"role": role},
                "content": {"parts": [text]},
                "create_time": date,
            },
        }

    return {
        "id": "source",
        "title": "Example",
        "create_time": 1700000000.25,
        "update_time": 1700000010.75,
        "current_node": "b",
        "is_archived": True,
        "mapping": {
            "root": {"parent": None, "message": None},
            "u": node("root", "user", "Question"),
            "tool": node("u", "tool", "Tool result"),
            "a": node("tool", "assistant", "First answer"),
            "b": node("tool", "assistant", "Alternate answer"),
        },
    }


def test_dates_branches_and_raw_source(conversation):
    before = copy.deepcopy(conversation)
    form = convert(conversation)
    assert form["created_at"] == 1700000000
    assert form["updated_at"] == 1700000010
    assert form["archived"] is True
    chat = form["chat"]
    assert chat["timestamp"] == 1700000000250
    assert chat["chatgpt_source"] == before == conversation
    history = chat["history"]
    assert history["currentId"] == "b"
    assert history["messages"]["u"]["childrenIds"] == ["a", "b"]
    assert history["messages"]["a"]["parentId"] == "u"
    assert history["messages"]["b"]["done"] is True
    assert [msg["id"] for msg in chat["messages"]] == ["u", "b"]


def test_missing_dates_and_nontext(conversation):
    conversation.pop("create_time")
    conversation.pop("update_time")
    conversation["mapping"]["b"]["message"]["create_time"] = None
    conversation["mapping"]["b"]["message"]["content"] = {
        "parts": [{"content_type": "image_asset_pointer", "asset_pointer": "private"}]
    }
    form = convert(conversation)
    assert form["created_at"] == 1700000001
    message = form["chat"]["history"]["messages"]["b"]
    assert message["timestamp"] == 1700000001
    assert "file not uploaded" in message["content"]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, True, "invalid"])
def test_bad_timestamps(value):
    with pytest.raises(ValueError):
        timestamp(value)


def test_iso_timestamp():
    assert timestamp("2023-11-14T22:13:20Z") == 1700000000
    assert timestamp("2023-11-14T23:13:20+01:00") == 1700000000


@pytest.mark.parametrize("parent", ["missing", "b"])
def test_invalid_tree(conversation, parent):
    conversation["mapping"]["tool"]["parent"] = parent
    with pytest.raises(ValueError):
        convert(conversation)


def test_zero_date_rejected(conversation):
    conversation["create_time"] = 0
    with pytest.raises(ValueError, match="positive"):
        convert(conversation)


def test_index_ignores_stale_files_and_prevents_escape(tmp_path, conversation):
    (tmp_path / "one.json").write_text(json.dumps(conversation))
    (tmp_path / "stale.json").write_text("not JSON")
    index = tmp_path / ".export-index.json"
    index.write_text(json.dumps({"json:source": {"path": "one.json"}}))
    assert len(list(load_conversations(tmp_path))) == 1
    index.write_text(json.dumps({"json:source": {"path": "../outside.json"}}))
    with pytest.raises(ValueError, match="escapes"):
        list(load_conversations(tmp_path))


def test_duplicate_conflict(tmp_path, conversation):
    path = tmp_path / "conversations.json"
    path.write_text(json.dumps([conversation, conversation]))
    assert len(list(load_conversations(path))) == 1
    changed = dict(conversation, title="Changed")
    path.write_text(json.dumps([conversation, changed]))
    with pytest.raises(ValueError, match="Conflicting"):
        list(load_conversations(path))


@pytest.fixture
def db():
    with sqlite3.connect(":memory:") as connection:
        connection.execute(
            "CREATE TABLE imports (scope TEXT, source_id TEXT, digest TEXT, "
            "remote_id TEXT, PRIMARY KEY(scope, source_id))"
        )
        yield connection


def session_response(form):
    session = Mock()
    session.request.return_value.status_code = 200
    session.request.return_value.json.return_value = [{"id": "remote", **form}]
    return session


def test_upload_resume_and_changed_source(db, conversation):
    form = convert(conversation)
    session = session_response(form)
    assert upload_one(db, session, "https://example.test", "user", "source", "hash", form)
    assert not upload_one(db, session, "https://example.test", "user", "source", "hash", form)
    assert session.request.call_count == 1
    assert session.request.call_args.kwargs["json"] == {"chats": [form]}
    with pytest.raises(ValueError, match="changed"):
        upload_one(db, session, "https://example.test", "user", "source", "new", form)


def test_timeout_is_not_retried(db, conversation):
    form = convert(conversation)
    session = Mock()
    session.request.side_effect = requests.Timeout()
    with pytest.raises(requests.Timeout):
        upload_one(db, session, "https://example.test", "user", "source", "hash", form)
    with pytest.raises(ValueError, match="Uncertain"):
        upload_one(db, session, "https://example.test", "user", "source", "hash", form)
    assert session.request.call_count == 1


def test_server_date_mismatch_stops(db, conversation):
    form = convert(conversation)
    session = session_response(dict(form, created_at=1))
    with pytest.raises(ValueError, match="preserve"):
        upload_one(db, session, "https://example.test", "user", "source", "hash", form)
    assert db.execute("SELECT remote_id FROM imports").fetchone() == (None,)


def test_redirect_not_followed():
    session = Mock()
    session.request.return_value.status_code = 307
    with pytest.raises(ValueError, match="307"):
        request(session, "GET", "https://example.test")
    assert session.request.call_args.kwargs["allow_redirects"] is False


def test_dry_run_no_network_or_state(tmp_path, conversation, monkeypatch):
    path = tmp_path / "conversations.json"
    path.write_text(json.dumps([conversation]))
    session = Mock(side_effect=AssertionError("Unexpected network"))
    monkeypatch.setattr(requests, "Session", session)
    state = tmp_path / "state.sqlite3"
    assert main([str(path), "--state", str(state)]) == 0
    assert not state.exists()
    session.assert_not_called()


def test_cli_upload_then_resume(tmp_path, conversation, monkeypatch):
    path = tmp_path / "conversations.json"
    path.write_text(json.dumps([conversation]))
    state = tmp_path / "state.sqlite3"
    monkeypatch.setenv("OPENWEBUI_TOKEN", "test-token")
    session = Mock()
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=False)

    def respond(method, url, **kwargs):
        response = Mock(status_code=200)
        if method == "GET":
            assert url == "https://example.test/base/api/v1/auths/"
            response.json.return_value = {"id": "account"}
        else:
            assert url == "https://example.test/base/api/v1/chats/import"
            response.json.return_value = [{"id": "remote", **kwargs["json"]["chats"][0]}]
        return response

    session.request.side_effect = respond
    monkeypatch.setattr(requests, "Session", lambda: session)
    args = [
        str(path),
        "--state",
        str(state),
        "--url",
        "https://example.test/base",
        "--upload",
        "--limit",
        "1",
    ]
    assert main(args) == 0
    assert main(args) == 0
    assert [call.args[0] for call in session.request.call_args_list] == ["GET", "POST", "GET"]
    session.headers.update.assert_called_with(Authorization="Bearer test-token")
    with sqlite3.connect(state) as connection:
        assert connection.execute("SELECT remote_id FROM imports").fetchall() == [("remote",)]
