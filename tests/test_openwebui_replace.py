import sqlite3
from unittest.mock import Mock

import pytest
import requests

from openwebui_import import OpenWebUIError
from openwebui_replace import finish_replacement, prepare_replacement


@pytest.fixture
def db():
    with sqlite3.connect(":memory:") as connection:
        connection.execute(
            "CREATE TABLE imports (scope TEXT, source_id TEXT, digest TEXT, "
            "remote_id TEXT, PRIMARY KEY(scope, source_id))"
        )
        connection.execute("INSERT INTO imports VALUES ('scope', 'source', 'hash', 'old')")
        connection.execute(
            "INSERT INTO imports VALUES ('other-account', 'source', 'hash', 'unrelated')"
        )
        connection.commit()
        yield connection


def old_chat():
    return {
        "id": "old",
        "user_id": "user",
        "meta": {"chatgpt_conversation_id": "source"},
        "chat": {"chatgpt_source": {"id": "source"}},
    }


def prepare(db, api, payload=None):
    prepare_replacement(
        db,
        None,
        "https://example.test",
        "scope",
        "source",
        payload or {"chat": {}},
        api,
        user_id="user",
    )


@pytest.mark.parametrize("fault", ["owner", "meta", "raw", "missing", "remote_id"])
def test_mismatch_never_deletes(db, fault):
    chat = old_chat()
    if fault == "owner":
        chat["user_id"] = "someone-else"
    elif fault == "meta":
        chat["meta"]["chatgpt_conversation_id"] = "other-source"
    elif fault == "raw":
        chat["chat"]["chatgpt_source"]["id"] = "other-source"
    elif fault == "missing":
        chat["meta"] = {}
        chat["chat"] = {}
    else:
        chat["id"] = "wrong-remote-id"
    api = Mock(return_value=chat)
    with pytest.raises(ValueError):
        prepare(db, api)
    assert api.call_count == 1
    assert api.call_args.args[1] == "GET"
    assert db.execute("SELECT count(*) FROM imports").fetchone()[0] == 2


def test_replacement_is_scoped_and_completed_run_is_not_repeated(db):
    api = Mock(side_effect=[old_chat(), True])
    prepare(db, api)
    assert api.call_args.args[1:] == ("DELETE", "https://example.test/api/v1/chats/old")
    assert db.execute("SELECT remote_id FROM imports").fetchall() == [("unrelated",)]
    with db:
        db.execute("INSERT INTO imports VALUES ('scope', 'source', 'hash', 'new')")
    finish_replacement(db, "scope", "source")
    api.side_effect = [dict(old_chat(), id="new")]
    prepare(db, api)
    assert api.call_count == 3
    assert api.call_args.args[1:] == ("GET", "https://example.test/api/v1/chats/new")


def test_timeout_after_server_deletion_recovers_via_404(db):
    api = Mock(side_effect=[old_chat(), requests.Timeout()])
    with pytest.raises(requests.Timeout):
        prepare(db, api)
    assert db.execute("SELECT state FROM chat_replacements").fetchone() == ("prepared",)
    api.side_effect = [OpenWebUIError("not found", status=404)]
    prepare(db, api)
    assert db.execute("SELECT state FROM chat_replacements").fetchone() == ("deleted",)
    assert db.execute("SELECT remote_id FROM imports WHERE scope='scope'").fetchone() is None


def test_unrecorded_404_is_not_assumed_safe(db):
    api = Mock(side_effect=OpenWebUIError("not found", status=404))
    with pytest.raises(OpenWebUIError):
        prepare(db, api)
    assert db.execute("SELECT count(*) FROM imports").fetchone()[0] == 2


def test_pending_chat_import_blocks_replacement(db):
    with db:
        db.execute("UPDATE imports SET remote_id=NULL WHERE scope='scope'")
    api = Mock()
    with pytest.raises(ValueError, match="Uncertain prior"):
        prepare(db, api)
    api.assert_not_called()


def test_source_changed_during_recovery_blocks(db):
    api = Mock(side_effect=[old_chat(), requests.Timeout()])
    with pytest.raises(requests.Timeout):
        prepare(db, api)
    with pytest.raises(ValueError, match="payload changed"):
        prepare(db, api, {"chat": {"title": "new"}})
    assert api.call_count == 2


def test_resume_after_reimport_before_completion(db):
    api = Mock(side_effect=[old_chat(), True])
    prepare(db, api)
    with db:
        db.execute("INSERT INTO imports VALUES ('scope', 'source', 'hash', 'new')")
    prepare(db, api)
    assert api.call_count == 2
    assert db.execute("SELECT state FROM chat_replacements").fetchone() == ("complete",)


def test_unknown_chat_never_causes_deletion(db):
    with db:
        db.execute("DELETE FROM imports WHERE scope='scope'")
    api = Mock()
    prepare(db, api)
    api.assert_not_called()
