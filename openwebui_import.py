"""Import ChatGPT JSON archives into Open WebUI; dry-run unless --upload is given."""

import argparse
import hashlib
import json
import math
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests


def timestamp(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("Boolean timestamp")
    try:
        result = float(value)
    except (TypeError, ValueError):
        date = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        result = date.replace(tzinfo=UTC).timestamp() if date.tzinfo is None else date.timestamp()
    if not math.isfinite(result) or result < 0:
        raise ValueError("Timestamp must be a finite, nonnegative Unix time")
    return result


def render_content(content: dict) -> str:
    parts = content.get("parts") or []
    rendered = []
    for part in parts:
        if isinstance(part, str):
            rendered.append(part)
        elif isinstance(part, dict) and isinstance(part.get("text"), str):
            rendered.append(part["text"])
        else:
            rendered.append("[Non-text content retained in chatgpt_source; file not uploaded]")
    if not parts:
        if isinstance(content.get("text"), str):
            rendered.append(content["text"])
        elif content:
            rendered.append(
                "```json\n" + json.dumps(content, ensure_ascii=False, indent=2) + "\n```"
            )
    return "\n\n".join(rendered)


def convert(detail: dict) -> dict:
    """Build an import form. Raw source remains separate from executable model settings."""
    ident = detail.get("conversation_id") or detail.get("id")
    mapping = detail.get("mapping")
    if not isinstance(ident, str) or not ident or not isinstance(mapping, dict) or not mapping:
        raise ValueError("Conversation needs an ID and a nonempty mapping")
    # Validate every branch, including hidden nodes, before skipping anything.
    validated: set[str] = set()
    for start in mapping:
        seen: set[str] = set()
        node = start
        while node is not None and node not in validated:
            if node in seen or node not in mapping:
                raise ValueError(f"{ident}: cycle or missing parent")
            seen.add(node)
            node = mapping[node].get("parent")
        validated.update(seen)
    dates = [
        date
        for node in mapping.values()
        if (date := timestamp((node.get("message") or {}).get("create_time"))) is not None
    ]
    created = timestamp(detail.get("create_time"))
    updated = timestamp(detail.get("update_time"))
    if created is None:
        created = min(dates) if dates else updated
    if updated is None:
        updated = max([created, *dates]) if created is not None else None
    # Open WebUI treats zero as absent and replaces it with the current time.
    if created is None or updated is None or int(created) <= 0 or int(updated) <= 0:
        raise ValueError(f"{ident}: no usable positive conversation timestamps")
    messages: dict[str, dict] = {}
    models: list[str] = []
    for node_id, node in mapping.items():
        msg = node.get("message") or {}
        role = (msg.get("author") or {}).get("role")
        metadata = msg.get("metadata") or {}
        if role not in {"user", "assistant"} or metadata.get(
            "is_visually_hidden_from_conversation"
        ):
            continue
        content = render_content(msg.get("content") or {})
        if not content:
            continue
        date = timestamp(msg.get("create_time"))
        message = {
            "id": node_id,
            "parentId": None,
            "childrenIds": [],
            "role": role,
            "content": content,
            "timestamp": date if date is not None else created,
        }
        if role == "assistant":
            model = metadata.get("model_slug") or detail.get("default_model_slug") or "chatgpt"
            message.update(model=model, modelName=model, done=True)
            if model not in models:
                models.append(model)
        messages[node_id] = message
    if not messages:
        raise ValueError(f"{ident}: no visible messages")

    def nearest(node_id: str | None) -> str | None:
        while node_id is not None and node_id not in messages:
            node_id = mapping[node_id].get("parent")
        return node_id

    for node_id, message in messages.items():
        parent = nearest(mapping[node_id].get("parent"))
        message["parentId"] = parent
        if parent is not None:
            messages[parent]["childrenIds"].append(node_id)
    current = detail.get("current_node")
    if current is not None and current not in mapping:
        raise ValueError(f"{ident}: current_node missing from mapping")
    current = (
        nearest(current)
        if current is not None
        else max(
            (key for key, msg in messages.items() if not msg["childrenIds"]),
            key=lambda key: messages[key]["timestamp"],
        )
    )
    if current is None:
        raise ValueError(f"{ident}: selected branch has no visible messages")
    branch = []
    node_id = current
    while node_id is not None:
        branch.append(messages[node_id])
        node_id = messages[node_id]["parentId"]
    return {
        "chat": {
            "title": detail.get("title") or "Untitled",
            "models": models,
            "history": {"messages": messages, "currentId": current},
            "messages": list(reversed(branch)),
            "timestamp": int(created * 1000),
            "params": {},
            "chatgpt_source": detail,
        },
        "created_at": int(created),
        "updated_at": int(updated),
        "archived": bool(detail.get("is_archived")),
        "pinned": bool(detail.get("is_starred") or detail.get("pinned_time")),
        "meta": {"chatgpt_conversation_id": ident},
    }


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Cannot read JSON file: {path}") from exc


def load_conversations(source: Path):
    """Use the export index when present, avoiding stale files and auxiliary JSON."""
    if source.is_dir():
        index = source / ".export-index.json"
        if index.exists():
            entries = read_json(index)
            paths = [
                source / value["path"] for key, value in entries.items() if key.startswith("json:")
            ]
            if not paths:
                raise ValueError("Export index has no raw JSON conversations; re-export as JSON")
            for path in paths:
                if not path.resolve().is_relative_to(source.resolve()):
                    raise ValueError("Export index path escapes source directory")
        else:
            paths = [
                path
                for path in sorted(source.rglob("*.json"))
                if not any(part.startswith(".") for part in path.relative_to(source).parts)
            ]
    else:
        paths = [source]
    seen: dict[str, str] = {}
    for path in paths:
        data = read_json(path)
        for detail in data if isinstance(data, list) else [data]:
            if not isinstance(detail, dict) or "mapping" not in detail:
                continue
            form = convert(detail)
            ident = form["meta"]["chatgpt_conversation_id"]
            digest = hashlib.sha256(json.dumps(form, sort_keys=True).encode()).hexdigest()
            if ident in seen:
                if seen[ident] != digest:
                    raise ValueError(f"Conflicting copies of conversation {ident}")
                continue
            seen[ident] = digest
            yield ident, digest, form


def request(session: requests.Session, method: str, url: str, **kwargs):
    response = session.request(method, url, timeout=(15, 120), allow_redirects=False, **kwargs)
    if not 200 <= response.status_code < 300:
        # Do not echo response bodies: proxies may include credentials or private content.
        raise ValueError(f"Open WebUI returned HTTP {response.status_code}")
    return response.json()


def upload_one(
    db,
    session,
    base: str,
    scope: str,
    ident: str,
    digest: str,
    form: dict,
    *,
    resource: str = "chat",
) -> bool:
    # Commit a reservation BEFORE the POST. A timeout/crash must never trigger a blind retry.
    with db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT digest, remote_id FROM imports WHERE scope=? AND source_id=?", (scope, ident)
        ).fetchone()
        if row:
            if row[1] is None:
                raise ValueError(
                    f"Uncertain prior upload for {ident}; reconcile journal before retrying"
                )
            if row[0] != digest:
                raise ValueError(
                    f"Source changed since upload: {ident}; refusing to duplicate/overwrite"
                )
            return False
        db.execute("INSERT INTO imports VALUES (?, ?, ?, NULL)", (scope, ident, digest))
    endpoint = {
        "chat": "/api/v1/chats/import",
        "memory": "/api/v1/memories/add",
        "prompt": "/api/v1/prompts/create",
    }[resource]
    result = request(
        session, "POST", base + endpoint, json={"chats": [form]} if resource == "chat" else form
    )
    if resource == "chat":
        if not isinstance(result, list) or len(result) != 1 or not isinstance(result[0], dict):
            raise ValueError("Unexpected import response; journal left pending")
        remote = result[0]
        if any(remote.get(key) != form[key] for key in ("created_at", "updated_at")):
            raise ValueError("Server did not preserve conversation dates; journal left pending")
    else:
        remote = result
        if not isinstance(remote, dict) or remote.get("content") != form["content"]:
            raise ValueError("Server did not preserve imported content; journal left pending")
        # Older Open WebUI versions identify prompts by command, not UUID.
        if resource == "prompt" and not remote.get("id"):
            remote = dict(remote, id=remote.get("command"))
    if not isinstance(remote.get("id"), str) or not remote["id"]:
        raise ValueError("Import response has no ID; journal left pending")
    with db:
        db.execute(
            "UPDATE imports SET remote_id=? WHERE scope=? AND source_id=?",
            (remote["id"], scope, ident),
        )
    return True


def load_extras(source: Path, memories: bool, instructions: str | None):
    extras: list[tuple[str, str, dict[str, Any]]] = []
    system = None
    if (memories or instructions) and not source.is_dir():
        raise ValueError("Memory/instruction import requires an export directory")
    if memories:
        data = read_json(source / "memories/memories.json")
        items = data.get("memories") if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise ValueError("Expected a memories list")
        seen = set()
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("Invalid memory entry")
            if item.get("status") in {"deleted", "removed"}:
                continue
            if item.get("status") not in {None, "warm", "cold", "active", "live"}:
                raise ValueError("Unknown memory status; inspect source before importing")
            content = item.get("content")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("Memory has no text")
            content = content.strip()
            ident = item.get("id") or hashlib.sha256(content.encode()).hexdigest()
            if not isinstance(ident, str) or ident in seen:
                raise ValueError("Invalid or duplicate memory ID")
            seen.add(ident)
            extras.append(("memory", ident, {"content": content, "type": "user"}))
    if instructions:
        data = read_json(source / "settings/custom_instructions.json")
        if not isinstance(data, dict):
            raise ValueError("Expected custom instructions object")
        if data.get("enabled") is False:
            raise ValueError("Source custom instructions are disabled; refusing to activate them")
        sections = []
        seen_text = set()
        for label, keys in [
            ("Name", ["name_user_message"]),
            ("About me", ["about_user_message", "other_user_message"]),
            ("Occupation", ["role_user_message"]),
            ("Response instructions", ["about_model_message", "traits_model_message"]),
        ]:
            for key in keys:
                value = data.get(key)
                if value is not None and not isinstance(value, str):
                    raise ValueError(f"Custom instruction {key} must be text")
                text = (value or "").strip()
                if text and text not in seen_text:
                    sections.append(f"## {label}\n{text}")
                    seen_text.add(text)
        content = "\n\n".join(sections)
        if not content:
            raise ValueError("No custom instruction text found")
        if instructions == "system":
            system = content
        else:
            extras.append(
                (
                    "prompt",
                    "chatgpt-custom-instructions",
                    {
                        "command": "chatgpt-custom-instructions",
                        "name": "ChatGPT custom instructions",
                        "title": "ChatGPT custom instructions",  # Older servers require title.
                        "content": content,
                        "access_grants": [],
                        "access_control": {},
                        "meta": {"chatgpt_source": data},
                    },
                )
            )
    return extras, system


def apply_system_prompt(session, base: str, content: str) -> None:
    endpoint = base + "/api/v1/users/user/settings"
    settings = request(session, "GET", endpoint)
    if settings is None:
        settings = {}
    if not isinstance(settings, dict) or not isinstance(settings.get("ui", {}), dict):
        raise ValueError("Unexpected user settings response")
    ui = settings.get("ui", {})
    existing = ui.get("system")
    if existing == content:
        print("System prompt already matches; skipped.")
        return
    if existing:
        raise ValueError("Existing system prompt differs; refusing to overwrite it")
    result = request(
        session, "POST", endpoint + "/update", json={**settings, "ui": {**ui, "system": content}}
    )
    if not isinstance(result, dict) or (result.get("ui") or {}).get("system") != content:
        raise ValueError("Server did not confirm the system prompt; inspect user settings")
    print("System prompt applied; unrelated settings preserved.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Export directory or conversations.json")
    parser.add_argument("--url", default=os.getenv("OPENWEBUI_URL"), help="Instance base URL")
    parser.add_argument("--upload", action="store_true", help="Actually upload (default: dry-run)")
    parser.add_argument("--limit", type=int, help="Process only the first N conversations")
    parser.add_argument("--state", type=Path, default=Path(".local/openwebui-import.sqlite3"))
    parser.add_argument(
        "--include-memories",
        action="store_true",
        help="Import saved memories (invokes server embedding service)",
    )
    parser.add_argument(
        "--instructions",
        choices=["prompt", "system"],
        help="Save custom instructions as a prompt or apply as account system prompt",
    )
    parser.add_argument("--only-extras", action="store_true", help="Skip conversation import")
    args = parser.parse_args(argv)
    try:
        if args.limit is not None and args.limit < 1:
            raise ValueError("--limit must be positive")
        # Preflight the entire source before any network writes; do not hold all chats in RAM.
        extras, system = load_extras(args.source, args.include_memories, args.instructions)
        count = 0 if args.only_extras else sum(1 for _ in load_conversations(args.source))
        if not count and not extras and system is None:
            raise ValueError("Nothing to import; select conversations, memories, or instructions")
        print(f"Validated {count} conversations. Attachment binaries will NOT be uploaded.")
        if args.include_memories or args.instructions:
            print(
                f"Extras: {len(extras)} memory/prompt records; system prompt: {system is not None}."
            )
            print(
                "Memory/prompt dates are assigned by the server; memories invoke its embedding service."
            )
        if not args.upload:
            print("Dry-run complete; no network requests or journal writes.")
            return 0
        base = (args.url or "").rstrip("/")
        parsed = urlsplit(base)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "Supply --url as an http(s) instance URL without credentials/query/fragment"
            )
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Use HTTPS for remote instances (or a localhost tunnel)")
        token = os.getenv("OPENWEBUI_TOKEN")
        if not token:
            raise ValueError("Set OPENWEBUI_TOKEN to an API key or session bearer token")
        args.state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with requests.Session() as session, sqlite3.connect(args.state) as db:
            os.chmod(args.state, 0o600)
            session.headers.update(Authorization=f"Bearer {token}")
            user = request(session, "GET", base + "/api/v1/auths/")
            if not isinstance(user, dict) or not user.get("id"):
                raise ValueError("Cannot establish destination user identity")
            scope = json.dumps([base, user["id"]])
            db.execute(
                "CREATE TABLE IF NOT EXISTS imports (scope TEXT, source_id TEXT, "
                "digest TEXT, remote_id TEXT, PRIMARY KEY(scope, source_id))"
            )
            uploaded = skipped = 0
            if system is not None:
                apply_system_prompt(session, base, system)
            for resource, ident, form in extras:
                digest = hashlib.sha256(json.dumps(form, sort_keys=True).encode()).hexdigest()
                extra_scope = json.dumps([base, user["id"], resource])
                changed = upload_one(
                    db, session, base, extra_scope, ident, digest, form, resource=resource
                )
                print(f"{resource}: {'imported' if changed else 'already imported'}", flush=True)
            conversations = [] if args.only_extras else load_conversations(args.source)
            for number, (ident, digest, form) in enumerate(conversations, 1):
                if args.limit is not None and number > args.limit:
                    break
                if upload_one(db, session, base, scope, ident, digest, form):
                    uploaded += 1
                else:
                    skipped += 1
                print(
                    f"Processed {number}: uploaded={uploaded}, already imported={skipped}",
                    flush=True,
                )
        return 0
    except requests.RequestException:
        print(
            "Network failure; any pending upload must be reconciled before retrying.",
            file=sys.stderr,
        )
        return 1
    except (ValueError, OSError, sqlite3.Error, KeyError, TypeError) as exc:
        print(f"Import stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
