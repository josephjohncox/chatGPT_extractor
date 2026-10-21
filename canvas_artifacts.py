"""Materialize selected-branch Canvas documents; never execute their code or unbounded regex."""

import json
import time
from dataclasses import dataclass, field

import regex

from attachment_refs import AttachmentRef
from chatgpt_client import ExportError
from conversation_format import selected_nodes

EXTENSIONS = {
    "document": ".md",
    "code/python": ".py",
    "code/javascript": ".js",
    "code/typescript": ".ts",
    "code/html": ".html",
    "code/react": ".jsx",
    "code/css": ".css",
    "code/json": ".json",
    "code/java": ".java",
    "code/sql": ".sql",
}


@dataclass
class CanvasArtifact:
    ref: AttachmentRef
    content: str
    history: list[dict] = field(default_factory=list)
    complete: bool = True
    reason: str = ""


def payload(message: dict) -> dict | None:
    content = message.get("content") or {}
    text = content.get("text") or "\n".join(
        part for part in content.get("parts", []) if isinstance(part, str)
    )
    if not isinstance(text, str) or len(text) > 16 * 1024 * 1024:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def patch_text(text: str, updates: list) -> str:
    started = time.monotonic()
    if len(updates) > 200:
        raise ValueError("Canvas patch limit")
    for update in updates:
        if time.monotonic() - started > 1:
            raise TimeoutError("Canvas patch budget")
        if not isinstance(update, dict):
            raise ValueError("Canvas patch shape")
        pattern, replacement = update.get("pattern"), update.get("replacement")
        if not isinstance(pattern, str) or not isinstance(replacement, str) or len(pattern) > 16384:
            raise ValueError("Canvas patch shape")
        if len(replacement.encode()) > 16 * 1024 * 1024:
            raise ValueError("Canvas replacement size")
        if pattern in {".*", "(?s).*", r"[\s\S]*", "(?s)^.*$"}:
            text = replacement
        else:
            text, matches = regex.subn(
                pattern,
                replacement,
                text,
                count=0 if update.get("multiple") is True else 1,
                timeout=0.1,
            )
            if not matches:
                raise ValueError("Canvas patch did not match")
        if len(text.encode()) > 16 * 1024 * 1024:
            raise ValueError("Canvas content size")
    return text


def canvas_artifacts(detail: dict) -> list[CanvasArtifact]:
    cid = str(detail.get("id") or detail.get("conversation_id") or "")
    mapping = detail.get("mapping") or {}
    if not any(
        str((node.get("message") or {}).get("recipient", "")).startswith("canmore.")
        for node in mapping.values()
        if isinstance(node, dict)
    ):
        return []
    try:
        branch = selected_nodes(detail)
    except ExportError:
        unordered = []
        for key, node in mapping.items():
            message = node.get("message") or {}
            if str(message.get("recipient", "")).startswith("canmore."):
                unordered.append(
                    {
                        "message_id": str(key),
                        "operation": message["recipient"],
                        "payload": payload(message) or message.get("content"),
                        "scope": "unordered_mapping",
                    }
                )
        return [
            CanvasArtifact(
                AttachmentRef("canvas", cid, "invalid-branch", name="canvas.json"),
                "",
                history=unordered,
                complete=False,
                reason="broken_selected_branch",
            )
        ]
    targets = {}
    node_keys = {id(node): str(key) for key, node in mapping.items()}
    for node in branch:
        message = node.get("message") or {}
        reply = payload(message) or {}
        canvas = (message.get("metadata") or {}).get("canvas") or {}
        ident = canvas.get("textdoc_id") or reply.get("textdoc_id")
        if str((message.get("author") or {}).get("name", "")).startswith("canmore") and isinstance(
            ident, str
        ):
            targets[node.get("parent")] = ident
    artifacts: dict[str, CanvasArtifact] = {}
    replay_started = time.monotonic()
    for node in branch:
        message = node.get("message") or {}
        operation = str(message.get("recipient", ""))
        if not operation.startswith("canmore.") or operation == "canmore.comment_textdoc":
            continue
        mid = str(message.get("id") or node.get("id") or node_keys.get(id(node), ""))
        data = payload(message)
        history = {
            "message_id": mid,
            "operation": operation,
            "payload": data if data is not None else message.get("content"),
        }
        canvas = (message.get("metadata") or {}).get("canvas") or {}
        ident = (data or {}).get("textdoc_id") or canvas.get("textdoc_id") or targets.get(mid)
        if operation == "canmore.create_textdoc" and data and isinstance(data.get("content"), str):
            key = ident if isinstance(ident, str) else mid
            kind = str(data.get("type") or "document")
            name = str(data.get("name") or "canvas") + EXTENSIONS.get(kind, ".txt")
            artifacts[key] = CanvasArtifact(
                AttachmentRef("canvas", cid, key, mid, name, "text/plain"),
                data["content"],
                [history],
            )
            continue
        target = (
            artifacts.get(ident)
            if isinstance(ident, str)
            else next(iter(artifacts.values()))
            if len(artifacts) == 1
            else None
        )
        if target is None:
            for artifact in artifacts.values():
                artifact.complete = False
                artifact.reason = "ambiguous_canvas_target"
            key = "unresolved-" + mid
            artifacts[key] = CanvasArtifact(
                AttachmentRef(
                    "canvas", cid, key, mid, "unresolved-canvas.json", "application/json"
                ),
                "",
                [history],
                False,
                "unresolved_canvas_operation",
            )
            continue
        target.history.append(history)
        updates = (data or {}).get("updates")
        full_rewrite = (
            isinstance(updates, list)
            and bool(updates)
            and isinstance(updates[0], dict)
            and isinstance(updates[0].get("pattern"), str)
            and updates[0].get("pattern") in {".*", "(?s).*", r"[\s\S]*", "(?s)^.*$"}
        )
        if not full_rewrite and time.monotonic() - replay_started > 2:
            target.complete = False
            target.reason = "canvas_replay_budget_exceeded"
            continue
        if not target.complete and not full_rewrite:
            continue
        try:
            if operation != "canmore.update_textdoc" or not isinstance(updates, list):
                raise ValueError("Canvas operation shape")
            target.content = patch_text(target.content, updates)
            target.complete = True
            target.reason = ""
        except (ValueError, regex.error, TimeoutError, RecursionError):
            target.complete = False
            target.reason = "canvas_patch_not_applied"
    return list(artifacts.values())
