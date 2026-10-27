import sqlite3
from unittest.mock import Mock

import pytest

from openwebui_import import OpenWebUIError, request
from openwebui_replace import list_owned_chat_ids, prepare_replacement


def test_not_found_401_is_not_reported_as_bad_token():
    session = Mock()
    session.request.return_value.status_code = 401
    session.request.return_value.json.return_value = {
        "detail": "We could not find what you're looking for :/"
    }
    with pytest.raises(OpenWebUIError) as caught:
        request(session, "GET", "http://localhost:3080/api/v1/chats/remote-id")
    assert caught.value.chat_not_found
    assert "missing or inaccessible" in str(caught.value)
    assert "Check OPENWEBUI_TOKEN" not in str(caught.value)


@pytest.mark.parametrize("status", [401, 404])
def test_inventory_confirmed_missing_import_can_be_restored_without_delete(status):
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE imports (scope TEXT, source_id TEXT, digest TEXT, remote_id TEXT)")
        db.execute("INSERT INTO imports VALUES ('scope', 'source', 'hash', 'old')")
        db.execute("INSERT INTO imports VALUES ('other', 'source', 'hash', 'untouched')")
        db.commit()
        missing = OpenWebUIError("missing", status=status, chat_not_found=True)
        api = Mock(side_effect=missing)
        prepare_replacement(
            db,
            None,
            "http://localhost:3080",
            "scope",
            "source",
            {},
            api,
            user_id="user",
            known_chat_ids={"unrelated"},
        )
        assert api.call_count == 1
        assert api.call_args.args[1] == "GET"
        assert db.execute("SELECT remote_id FROM imports").fetchall() == [("untouched",)]
        assert db.execute("SELECT state FROM chat_replacements").fetchone() == ("deleted",)


def test_invalid_token_never_clears_journal_even_when_inventory_omits_chat():
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE imports (scope TEXT, source_id TEXT, digest TEXT, remote_id TEXT)")
        db.execute("INSERT INTO imports VALUES ('scope', 'source', 'hash', 'old')")
        db.commit()
        api = Mock(side_effect=OpenWebUIError("invalid token", status=401))
        with pytest.raises(OpenWebUIError):
            prepare_replacement(
                db,
                None,
                "http://localhost:3080",
                "scope",
                "source",
                {},
                api,
                user_id="user",
                known_chat_ids=set(),
            )
        assert db.execute("SELECT remote_id FROM imports").fetchone() == ("old",)


def test_inventory_includes_every_page_and_archived_chats():
    api = Mock(
        side_effect=[[{"id": "ordinary"}], [{"id": "folder-chat"}], [], [{"id": "archived"}], []]
    )
    assert list_owned_chat_ids(None, "http://localhost:3080", api) == {
        "ordinary",
        "folder-chat",
        "archived",
    }
    calls = api.call_args_list
    assert calls[0].kwargs["params"] == {
        "include_pinned": "true",
        "include_folders": "true",
        "page": 1,
    }
    assert calls[1].kwargs["params"]["page"] == 2
    assert calls[3].args[2].endswith("/archived")


def test_listed_but_inaccessible_chat_is_not_reimported():
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE imports (scope TEXT, source_id TEXT, digest TEXT, remote_id TEXT)")
        db.execute("INSERT INTO imports VALUES ('scope', 'source', 'hash', 'old')")
        db.commit()
        api = Mock(side_effect=OpenWebUIError("missing", status=401, chat_not_found=True))
        with pytest.raises(ValueError, match="listed but inaccessible"):
            prepare_replacement(
                db,
                None,
                "http://localhost:3080",
                "scope",
                "source",
                {},
                api,
                user_id="user",
                known_chat_ids={"old"},
            )
        assert db.execute("SELECT remote_id FROM imports").fetchone() == ("old",)
