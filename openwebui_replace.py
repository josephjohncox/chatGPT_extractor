"""Journal-scoped, resumable replacement; never enumerate or bulk-delete server chats."""

import hashlib
import json
from urllib.parse import quote


def list_owned_chat_ids(session, base, request):
    """Read every ordinary and archived chat ID; do not infer absence from a partial page."""
    ids: set[str] = set()
    for path, params in [
        ("/api/v1/chats/list", {"include_pinned": "true", "include_folders": "true"}),
        ("/api/v1/chats/archived", {}),
    ]:
        for page in range(1, 10001):
            records = request(session, "GET", base + path, params={**params, "page": page})
            if not isinstance(records, list) or not all(
                isinstance(record, dict) and isinstance(record.get("id"), str) and record["id"]
                for record in records
            ):
                raise ValueError(
                    "Cannot establish complete chat inventory; no journal entries cleared"
                )
            if not records:
                break
            ids.update(record["id"] for record in records)
        else:
            raise ValueError("Chat inventory pagination did not finish")
    return ids


def prepare_replacement(
    db,
    session,
    base,
    scope,
    ident,
    payload,
    request,
    *,
    user_id,
    known_chat_ids=None,
    inventory_cache=None,
):
    """Delete a verified old import once per desired payload, retaining crash recovery state."""
    desired = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    db.execute(
        "CREATE TABLE IF NOT EXISTS chat_replacements (scope TEXT, source_id TEXT, "
        "desired TEXT, old_remote_id TEXT, state TEXT, PRIMARY KEY(scope, source_id))"
    )
    previous = db.execute(
        "SELECT desired, old_remote_id, state FROM chat_replacements WHERE scope=? AND source_id=?",
        (scope, ident),
    ).fetchone()
    row = db.execute(
        "SELECT remote_id FROM imports WHERE scope=? AND source_id=?", (scope, ident)
    ).fetchone()
    if row is not None and not row[0]:
        raise ValueError("Uncertain prior chat import; reconcile it before replacement")
    if previous and previous[2] in {"prepared", "deleted"} and previous[0] != desired:
        raise ValueError(
            "Replacement payload changed during recovery; restore the original options/source"
        )
    completed = bool(previous and previous[0] == desired and previous[2] == "complete")
    if previous and previous[2] == "deleted":
        if row:
            # Import succeeded but the process stopped before recording completion.
            if row[0] == previous[1]:
                raise ValueError("Replacement journal conflicts with imported chat")
            finish_replacement(db, scope, ident)
        return
    if previous and previous[2] == "prepared":
        old_id = previous[1]
        if not row or row[0] != old_id:
            raise ValueError("Replacement journal conflicts with imported chat")
        prepared = True
    elif row is None:
        return  # New source chat: nothing to delete.
    else:
        old_id = row[0]
        prepared = False
    endpoint = base + "/api/v1/chats/" + quote(old_id, safe="")
    try:
        remote = request(session, "GET", endpoint)
    except ValueError as exc:
        missing = getattr(exc, "status", None) == 404 or getattr(exc, "chat_not_found", False)
        if not missing:
            raise
        if not prepared:
            if known_chat_ids is None:
                if inventory_cache is not None and "ids" in inventory_cache:
                    known_chat_ids = inventory_cache["ids"]
                else:
                    known_chat_ids = list_owned_chat_ids(session, base, request)
                    if inventory_cache is not None:
                        inventory_cache["ids"] = known_chat_ids
            if old_id in known_chat_ids:
                raise ValueError(
                    "Journaled chat is listed but inaccessible; refusing to duplicate it"
                ) from exc
        remote = None
    if remote is not None:
        if (
            not isinstance(remote, dict)
            or remote.get("id") != old_id
            or remote.get("user_id") != user_id
        ):
            raise ValueError("Cannot verify journaled chat before deletion")
        meta = remote.get("meta") or {}
        source = (remote.get("chat") or {}).get("chatgpt_source") or {}
        identifiers = [
            value
            for value in [
                meta.get("chatgpt_conversation_id"),
                source.get("conversation_id"),
                source.get("id"),
            ]
            if value is not None
        ]
        if not identifiers or any(value != ident for value in identifiers):
            raise ValueError("Server source ID does not match import journal; refusing deletion")
        if completed:
            return  # Same payload already replaced; its current remote chat still exists.
        with db:
            db.execute(
                "INSERT OR REPLACE INTO chat_replacements VALUES (?, ?, ?, ?, 'prepared')",
                (scope, ident, desired, old_id),
            )
        deleted = request(session, "DELETE", endpoint)
        if deleted is not True:
            raise ValueError("Server did not confirm deletion; replacement remains pending")
    with db:
        db.execute(
            "INSERT OR REPLACE INTO chat_replacements VALUES (?, ?, ?, ?, 'deleted')",
            (scope, ident, desired, old_id),
        )
        db.execute("DELETE FROM imports WHERE scope=? AND source_id=?", (scope, ident))
    print("Old import removed or confirmed absent; re-importing with original dates.", flush=True)


def finish_replacement(db, scope, ident):
    with db:
        db.execute(
            "UPDATE chat_replacements SET state='complete' "
            "WHERE scope=? AND source_id=? AND state='deleted'",
            (scope, ident),
        )
