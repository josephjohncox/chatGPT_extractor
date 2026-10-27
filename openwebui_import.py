"""Import ChatGPT JSON archives into Open WebUI; dry-run unless --upload is given."""

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import requests

from openwebui_activity import activity_kind, compact_history, tool_record
from openwebui_artifacts import (
    ArtifactPlan,
    chat_asset_links,
    enrich_chat,
    load_artifacts,
    update_existing_artifacts,
    upload_artifacts,
)
from openwebui_layouts import render_layouts
from openwebui_reasoning import render_reasoning
from openwebui_replace import finish_replacement, prepare_replacement


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


def render_content(content: dict, layout_mode: str = "markdown") -> str:
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
    text = "\n\n".join(rendered)
    return text if content.get("content_type") == "code" else render_layouts(text, layout_mode)


def convert(detail: dict, layout_mode: str = "markdown", activity_mode: str = "compact") -> dict:
    """Build an import form. Raw source remains separate from executable model settings."""
    if activity_mode not in {"compact", "nodes"}:
        raise ValueError("Unknown activity rendering mode")
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
        if role not in {"user", "assistant", "tool"} or metadata.get(
            "is_visually_hidden_from_conversation"
        ):
            continue
        content = render_content(
            msg.get("content") or {}, layout_mode if role == "assistant" else "source"
        )
        if role == "assistant":
            content = render_reasoning(msg, content)
        activity = activity_kind(msg, content)
        if activity in {"tool_call", "tool_result"}:
            content = tool_record(msg, content, activity)
        if not content:
            continue
        date = timestamp(msg.get("create_time"))
        message = {
            "id": node_id,
            "parentId": None,
            "childrenIds": [],
            "role": "assistant" if role == "tool" else role,
            "content": content,
            "timestamp": date if date is not None else created,
        }
        if activity:
            message["chatgpt_activity_kind"] = activity
        if role in {"assistant", "tool"}:
            model = metadata.get("model_slug") or detail.get("default_model_slug") or "chatgpt"
            message.update(model=model, modelName=model, done=True)
            if role == "assistant" and model not in models:
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
    if activity_mode == "compact":
        messages = compact_history(messages, current)
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


def project_for_chat(detail: dict, path: Path, source: Path) -> tuple[str, str] | None:
    raw = (
        detail.get("_project_id")
        or detail.get("project_id")
        or detail.get("gizmo_id")
        or detail.get("conversation_template_id")
    )
    match = re.match(r"^(g-p-[a-f0-9]+)(?:-|$)", raw) if isinstance(raw, str) else None
    if not match:
        return None  # Custom GPTs and workspaces are not projects.
    ident = match.group(1)
    name = detail.get("_folder_name")
    suffix = "--" + hashlib.sha256(ident.encode()).hexdigest()[:12]
    if not name and source.is_dir() and path.parent != source and path.parent.name.endswith(suffix):
        name = path.parent.name[: -len(suffix)]
    return ident, str(name or ident)


def load_conversations(
    source: Path,
    projects: dict | None = None,
    layout_mode: str = "markdown",
    activity_mode: str = "compact",
):
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
            form = convert(detail, layout_mode, activity_mode)
            ident = form["meta"]["chatgpt_conversation_id"]
            if projects is not None:
                project = project_for_chat(detail, path, source)
                if ident in projects and projects[ident] != project:
                    raise ValueError(f"Conflicting project assignment for {ident}")
                projects[ident] = project
            digest = hashlib.sha256(json.dumps(form, sort_keys=True).encode()).hexdigest()
            if ident in seen:
                if seen[ident] != digest:
                    raise ValueError(f"Conflicting copies of conversation {ident}")
                continue
            seen[ident] = digest
            yield ident, digest, form


class OpenWebUIError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        human_session_required: bool = False,
        status: int | None = None,
        chat_not_found: bool = False,
    ):
        super().__init__(message)
        self.human_session_required = human_session_required
        self.status = status
        self.chat_not_found = chat_not_found


def authenticate_user(session: requests.Session, base: str) -> dict:
    try:
        user = request(session, "GET", base + "/api/v1/auths/")
    except OpenWebUIError as exc:
        if not exc.human_session_required:
            raise
        # Open WebUI 0.12.4 restricts /auths/ to human session JWTs. The status
        # route uses verified-user auth and returns the API key owner's stable ID.
        try:
            user = request(session, "GET", base + "/api/v1/users/user/status")
        except OpenWebUIError as fallback:
            raise ValueError(
                f"{fallback} API-key account identity could not be resolved. "
                "The user-status feature may be disabled or its route restricted. "
                "Use an Open WebUI browser session token in OPENWEBUI_TOKEN instead. "
                "No uploads have started in this run."
            ) from fallback
    if not isinstance(user, dict) or not isinstance(user.get("id"), str) or not user["id"]:
        raise ValueError("Cannot establish destination user identity")
    return user


def request(session: requests.Session, method: str, url: str, **kwargs):
    response = session.request(method, url, timeout=(15, 120), allow_redirects=False, **kwargs)
    if not 200 <= response.status_code < 300:
        # Do not echo response bodies, headers, or URL queries: they may contain secrets.
        path = urlsplit(url).path
        human_session_required = False
        error = None
        if response.status_code in {401, 403}:
            try:
                error = response.json()
            except ValueError:
                pass
        human_session_detail = "A human session is required."
        human_session_required = (
            response.status_code == 403
            and isinstance(error, dict)
            and error.get("detail") == human_session_detail
        )
        chat_route = method == "GET" and re.search(r"/api/v1/chats/[^/]+$", path) is not None
        chat_not_found = bool(
            chat_route
            and (
                response.status_code == 404
                or (
                    response.status_code == 401
                    and isinstance(error, dict)
                    and error.get("detail") == "We could not find what you're looking for :/"
                )
            )
        )
        message = f"{method} {path}: Open WebUI returned HTTP {response.status_code}."
        if chat_not_found:
            message += (
                " The journaled chat is missing or inaccessible, not necessarily a bad token. "
                "Use --replace-imported-chats to restore missing imports after an inventory check."
            )
        elif human_session_required:
            message += " This endpoint requires a human session token, not an API key."
        elif response.status_code == 403:
            message += (
                " Check the global API Keys toggle, your account/group API Keys permission,"
                " API-key endpoint restrictions, and permission for this feature."
            )
        elif response.status_code == 401:
            message += " Check OPENWEBUI_TOKEN: use a valid Open WebUI API key or session token."
        if method == "GET" and path.rstrip("/").endswith("/api/v1/auths"):
            message += " Account authentication failed. No uploads have started in this run."
        raise OpenWebUIError(
            message,
            human_session_required=human_session_required,
            status=response.status_code,
            chat_not_found=chat_not_found,
        )
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
    folder_id: str | None = None,
    artifact_chat: dict | None = None,
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
                    f"Source or rendering changed since upload: {ident}; refusing to duplicate/overwrite. "
                    "Use --replace-imported-chats to re-import with original dates, or restore prior rendering options."
                )
            return False
        db.execute("INSERT INTO imports VALUES (?, ?, ?, NULL)", (scope, ident, digest))
    endpoint = {
        "chat": "/api/v1/chats/import",
        "memory": "/api/v1/memories/add",
        "prompt": "/api/v1/prompts/create",
    }[resource]
    payload: dict[str, Any] = (
        dict(form, folder_id=folder_id) if folder_id is not None else dict(form)
    )
    if artifact_chat is not None:
        payload["chat"] = artifact_chat
    result = request(
        session,
        "POST",
        base + endpoint,
        json={"chats": [payload]} if resource == "chat" else payload,
    )
    if resource == "chat":
        if not isinstance(result, list) or len(result) != 1 or not isinstance(result[0], dict):
            raise ValueError("Unexpected import response; journal left pending")
        remote = result[0]
        if artifact_chat is not None and (remote.get("chat") or {}).get(
            "chatgpt_artifacts"
        ) != artifact_chat.get("chatgpt_artifacts"):
            raise ValueError("Server did not preserve artifact links; journal left pending")
        if folder_id is not None and remote.get("folder_id") != folder_id:
            raise ValueError("Server did not preserve folder assignment; journal left pending")
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


def ensure_project_folder(db, session, base, scope, project_id, name) -> str:
    db.execute(
        "CREATE TABLE IF NOT EXISTS project_folders (scope TEXT, project_id TEXT, "
        "name TEXT, remote_id TEXT, PRIMARY KEY(scope, project_id))"
    )
    with db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT remote_id FROM project_folders WHERE scope=? AND project_id=?",
            (scope, project_id),
        ).fetchone()
        if row is None:
            db.execute(
                "INSERT INTO project_folders VALUES (?, ?, ?, NULL)", (scope, project_id, name)
            )
    created = False
    expected_id = row[0] if row else None
    if expected_id:
        remote = request(session, "GET", base + "/api/v1/folders/" + quote(expected_id, safe=""))
    else:
        # This endpoint lists the authenticated user's own folders, not shared folders.
        folders = request(session, "GET", base + "/api/v1/folders/")
        if not isinstance(folders, list) or not all(isinstance(folder, dict) for folder in folders):
            raise ValueError("Unexpected folder list; journal left pending")
        matches = [folder for folder in folders if folder.get("name") == name]
        if len(matches) > 1:
            raise ValueError(
                "Multiple existing folders have this name; refusing ambiguous assignment"
            )
        if matches:
            expected_id = matches[0].get("id")
            if not isinstance(expected_id, str) or not expected_id:
                raise ValueError("Existing folder has no valid ID")
            remote = request(
                session, "GET", base + "/api/v1/folders/" + quote(expected_id, safe="")
            )
        else:
            if row is not None:
                raise ValueError("Uncertain folder creation; reconcile project_folders journal")
            remote = request(
                session,
                "POST",
                base + "/api/v1/folders/",
                json={"name": name, "meta": {"chatgpt_project_id": project_id}},
            )
            created = True
    if (
        not isinstance(remote, dict)
        or not isinstance(remote.get("id"), str)
        or not remote["id"]
        or (expected_id is not None and remote["id"] != expected_id)
        or (remote.get("meta") or {}).get("chatgpt_project_id") not in {None, project_id}
        or (created and (remote.get("meta") or {}).get("chatgpt_project_id") != project_id)
    ):
        raise ValueError("Folder identity not confirmed; reconcile project_folders journal")
    other = db.execute(
        "SELECT project_id FROM project_folders WHERE scope=? AND remote_id=? AND project_id<>?",
        (scope, remote["id"], project_id),
    ).fetchone()
    if other:
        raise ValueError("Existing folder is already assigned to a different source project")
    with db:
        db.execute(
            "UPDATE project_folders SET remote_id=? WHERE scope=? AND project_id=?",
            (remote["id"], scope, project_id),
        )
    return remote["id"]


def assign_existing_folder(db, session, base, scope, ident, folder_id, allow_move):
    row = db.execute(
        "SELECT remote_id FROM imports WHERE scope=? AND source_id=?", (scope, ident)
    ).fetchone()
    if not row or not row[0]:
        raise ValueError("No completed chat import to assign")
    endpoint = base + "/api/v1/chats/" + quote(row[0], safe="")
    chat = request(session, "GET", endpoint)
    if not isinstance(chat, dict) or chat.get("id") != row[0]:
        raise ValueError("Cannot verify previously imported chat")
    if chat.get("folder_id") == folder_id:
        return
    if not allow_move:
        raise ValueError(
            "Existing chat needs moving. --move-existing-project-chats permits "
            "changing its update date and clearing pinned/archive state"
        )
    moved = request(session, "POST", endpoint + "/folder", json={"folder_id": folder_id})
    if not isinstance(moved, dict) or moved.get("folder_id") != folder_id:
        raise ValueError("Server did not confirm chat move")


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
    result = request(
        session, "POST", endpoint + "/update", json={**settings, "ui": {**ui, "system": content}}
    )
    if not isinstance(result, dict) or (result.get("ui") or {}).get("system") != content:
        raise ValueError("Server did not confirm the system prompt; inspect user settings")
    print(
        "Personal account system prompt updated; global/model prompts and other settings unchanged."
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import ChatGPT chats and optional account data into Open WebUI.\n"
        "Default: validate chats locally; no network requests or journal writes.",
        formatter_class=lambda prog: argparse.RawDescriptionHelpFormatter(prog, width=88),
        epilog="""Examples:
  python openwebui_import.py EXPORT_DIR --all
  python openwebui_import.py EXPORT_DIR --all --upload
  python openwebui_import.py EXPORT_DIR --all --instructions prompt --upload
  python openwebui_import.py EXPORT_DIR --all --replace-imported-chats --upload
  python openwebui_import.py EXPORT_DIR --project-folders --upload --limit 1
  python openwebui_import.py EXPORT_DIR --only-extras --include-memories --upload

Environment (needed only for --upload):
  OPENWEBUI_URL     Instance base URL, including any proxy prefix; omit /api/v1.
  OPENWEBUI_TOKEN   API key or session bearer token. Never passed on the command line.

Limits and safety:
  --all requires an export directory with memories and custom-instruction JSON.
  Missing memories/instructions or disabled instructions fail validation.
  --all includes files, Canvas histories, and editable text/Markdown Canvas Notes.
  Layout tags become Markdown by default; Mermaid/code fences are preserved.
  Reasoning/tool activity is folded into answer bubbles by default; nothing is replayed.
  --activity-rendering nodes keeps separate source-node bubbles for inspection.
  --layout-rendering html adds static HTML previews; source keeps markup unchanged.
  --all updates YOUR personal account system prompt, never global/model prompts.
  --all does NOT enable uploads or existing-chat mutations.
  --instructions prompt saves a reusable prompt instead of updating your personal one.
  --limit limits chats only; ALL selected extras and artifact files are processed.
  Files upload without extraction/embedding. Missing/partial assets are reported.
  Project instructions and general settings are excluded.
  --replace-imported-chats deletes verified journaled chats, then restores source dates.
  Replacements discard server-only edits/shares; export a server backup first.
  New chat imports retain dates. Files/Notes/memories/prompts receive new dates.
  Memories invoke the server's embedding service, possibly an external provider.
  Keep the same journal to resume. Uncertain uploads stop instead of blindly retrying.

Full guide: docs/openwebui-import.md
""",
    )
    parser.add_argument(
        "source",
        type=Path,
        metavar="SOURCE",
        help="Export directory or conversations.json (chats only)",
    )
    selection = parser.add_argument_group("what to import")
    selection.add_argument(
        "--all",
        action="store_true",
        help="Select chats, folders, memories, personal system prompt, artifacts, and Canvas Notes",
    )
    selection.add_argument(
        "--project-folders",
        action="store_true",
        help="Create project folders and assign chats during import",
    )
    selection.add_argument(
        "--include-memories",
        action="store_true",
        help="Include saved memories from the export directory",
    )
    selection.add_argument(
        "--instructions",
        choices=["prompt", "system"],
        help="prompt: save a reusable prompt; system: update your personal account system "
        "prompt (default with --all), never global/model prompts",
    )
    selection.add_argument(
        "--only-extras",
        action="store_true",
        help="Skip chats; use with memories/instructions, not --all or folders",
    )
    selection.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help="Process the first N chats (positive integer); extras are not limited",
    )
    selection.add_argument(
        "--include-artifacts",
        action="store_true",
        help="Upload available files and Canvas histories; add chat download links",
    )
    selection.add_argument(
        "--canvas-notes",
        action="store_true",
        help="Also create editable prose Canvas Notes; implies --include-artifacts",
    )
    selection.add_argument(
        "--layout-rendering",
        choices=["markdown", "html", "source"],
        default="markdown",
        help="Assistant layout tags: readable Markdown (default), plus static HTML artifacts, "
        "or unchanged source. Existing code/Mermaid blocks are preserved.",
    )
    selection.add_argument(
        "--activity-rendering",
        choices=["compact", "nodes"],
        default="compact",
        help="compact (default): fold historical tool/reasoning records into each answer; "
        "nodes: show separate source-node bubbles. Historical calls are never executed.",
    )
    execution = parser.add_argument_group("execution")
    mode = execution.add_mutually_exclusive_group()
    mode.add_argument("--upload", action="store_true", help="Write selected data to Open WebUI")
    mode.add_argument(
        "--dry-run",
        dest="upload",
        action="store_false",
        help="Validate locally without network requests or journal writes (default)",
    )
    parser.set_defaults(upload=False)
    execution.add_argument(
        "--move-existing-project-chats",
        action="store_true",
        help="With --project-folders, allow existing-chat moves to change update "
        "dates and clear pinned/archive state; NOT implied by --all",
    )
    execution.add_argument(
        "--update-existing-artifacts",
        action="store_true",
        help="Add artifact links to existing chats; permits update-date changes",
    )
    execution.add_argument(
        "--replace-imported-chats",
        action="store_true",
        help="Delete/re-import verified journaled chats to preserve source dates; "
        "discards server-only edits and shares; NOT implied by --all",
    )
    connection = parser.add_argument_group("connection and resume")
    connection.add_argument(
        "--url",
        default=os.getenv("OPENWEBUI_URL"),
        metavar="URL",
        help="Instance base URL; overrides OPENWEBUI_URL. Remote URLs need HTTPS",
    )
    connection.add_argument(
        "--state",
        type=Path,
        default=Path(".local/openwebui-import.sqlite3"),
        metavar="PATH",
        help="Resume journal (default: .local/openwebui-import.sqlite3)",
    )
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.all:
            if args.only_extras:
                raise ValueError("--all cannot be combined with --only-extras")
            args.project_folders = True
            args.include_memories = True
            args.instructions = args.instructions or "system"
            args.include_artifacts = True
            args.canvas_notes = True
        if args.canvas_notes:
            args.include_artifacts = True
        if args.update_existing_artifacts and not args.include_artifacts:
            raise ValueError("--update-existing-artifacts requires --include-artifacts or --all")
        if args.replace_imported_chats and (
            args.only_extras or args.update_existing_artifacts or args.move_existing_project_chats
        ):
            raise ValueError(
                "Replacement cannot be combined with --only-extras or existing-chat update/move flags"
            )
        if args.move_existing_project_chats and not args.project_folders:
            raise ValueError("--move-existing-project-chats requires --project-folders")
        if args.project_folders and args.only_extras:
            raise ValueError("--project-folders cannot be combined with --only-extras")
        if args.limit is not None and args.limit < 1:
            raise ValueError("--limit must be positive")
        # Preflight the entire source before any network writes; do not hold all chats in RAM.
        extras, system = load_extras(args.source, args.include_memories, args.instructions)
        artifacts = (
            load_artifacts(args.source, canvas_notes=args.canvas_notes)
            if args.include_artifacts
            else ArtifactPlan()
        )
        projects: dict[str, tuple[str, str] | None] = {}
        count = (
            0
            if args.only_extras
            else sum(
                1
                for _ in load_conversations(
                    args.source,
                    projects if args.project_folders else None,
                    args.layout_rendering,
                    args.activity_rendering,
                )
            )
        )
        project_names: dict[str, str] = {}
        for project in projects.values():
            if project is not None:
                project_id, name = project
                if project_id in project_names and project_names[project_id] != name:
                    raise ValueError("Conflicting names for the same project")
                project_names[project_id] = name
        # Distinct projects may share a display name; never collapse them.
        names = list(project_names.values())
        project_names = {
            key: name + (" [" + key + "]" if names.count(name) > 1 else "")
            for key, name in project_names.items()
        }
        if args.project_folders:
            print(
                f"Projects: {len(project_names)} folders; "
                f"{sum(p is not None for p in projects.values())} assigned chats."
            )
        if args.move_existing_project_chats:
            print(
                "WARNING: existing chat moves change update dates and clear pinned/archive state."
            )
        if args.replace_imported_chats:
            print(
                "WARNING: replacement deletes only verified journaled chats, including server-only edits/shares."
            )
        if not count and not extras and system is None and not artifacts.files:
            raise ValueError(
                "Nothing to import; select conversations, memories, instructions, or artifacts"
            )
        print(f"Validated {count} conversations.")
        if args.include_artifacts:
            print(
                f"Artifacts: {len(artifacts.files)} files ({sum(f.size for f in artifacts.files)} bytes), "
                f"{len(artifacts.notes)} Canvas Notes; file processing/embedding disabled."
            )
            partial = sum(entry["outcome"] == "partial" for entry in artifacts.assets.values())
            if artifacts.gaps or partial:
                print(
                    f"WARNING: archive gaps={artifacts.gaps}; partial Canvas snapshots={partial}. "
                    "Only available, checksum-verified files can be uploaded."
                )
        else:
            print("Attachment binaries will NOT be uploaded (use --include-artifacts or --all).")
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
            user = authenticate_user(session, base)
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
            artifact_links = upload_artifacts(artifacts, db, session, base, scope, request)
            folder_ids: dict[str, str] = {}
            replacement_inventory: dict = {}
            conversations = (
                []
                if args.only_extras
                else load_conversations(
                    args.source,
                    layout_mode=args.layout_rendering,
                    activity_mode=args.activity_rendering,
                )
            )
            for number, (ident, digest, form) in enumerate(conversations, 1):
                if args.limit is not None and number > args.limit:
                    break
                folder_id = None
                project = projects.get(ident)
                if project is not None:
                    project_id = project[0]
                    if project_id not in folder_ids:
                        folder_ids[project_id] = ensure_project_folder(
                            db, session, base, scope, project_id, project_names[project_id]
                        )
                    folder_id = folder_ids[project_id]
                links = chat_asset_links(
                    artifacts, artifact_links, ident, form["chat"]["chatgpt_source"]
                )
                artifact_chat = enrich_chat(form["chat"], links) if links else None
                if args.replace_imported_chats:
                    payload = dict(form, folder_id=folder_id, chat=artifact_chat or form["chat"])
                    prepare_replacement(
                        db,
                        session,
                        base,
                        scope,
                        ident,
                        payload,
                        request,
                        user_id=user["id"],
                        inventory_cache=replacement_inventory,
                    )
                if upload_one(
                    db,
                    session,
                    base,
                    scope,
                    ident,
                    digest,
                    form,
                    folder_id=folder_id,
                    artifact_chat=artifact_chat,
                ):
                    uploaded += 1
                else:
                    skipped += 1
                    if folder_id is not None:
                        assign_existing_folder(
                            db,
                            session,
                            base,
                            scope,
                            ident,
                            folder_id,
                            args.move_existing_project_chats,
                        )
                    if links:
                        update_existing_artifacts(
                            db,
                            session,
                            base,
                            scope,
                            ident,
                            links,
                            args.update_existing_artifacts,
                            request,
                        )
                if args.replace_imported_chats:
                    finish_replacement(db, scope, ident)
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
