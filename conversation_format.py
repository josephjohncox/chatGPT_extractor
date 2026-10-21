"""Pure rendering shared by exports and the offline local index."""

import re
from datetime import UTC, datetime
from typing import Any

from chatgpt_client import ExportError


def parse_timestamp(value: Any, fmt: str = "%Y-%m-%d %H:%M UTC") -> str:
    if value is None or value == "":
        return "Unknown"
    try:
        return datetime.fromtimestamp(float(value), UTC).strftime(fmt)
    except (ValueError, TypeError, OverflowError, OSError):
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt.astimezone(UTC).strftime(fmt)
        except ValueError:
            return "Unknown"


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(". ")
    # Leave room for the ID suffix even on filesystems with byte-counted limits.
    return name.encode("utf-8")[:100].decode("utf-8", errors="ignore") or "Untitled"


def extract_messages(detail: dict) -> list[dict]:
    """Render the selected branch (current_node ancestry), not the first regeneration."""
    mapping = detail.get("mapping", {})
    if not isinstance(mapping, dict) or not mapping:
        return []
    branch = []
    visited = set()
    node_id = detail.get("current_node")
    if node_id:
        if node_id not in mapping:
            raise ExportError("Conversation current_node is absent from mapping; use raw JSON")
        while node_id:
            if node_id in visited or node_id not in mapping:
                raise ExportError(
                    "Conversation ancestry contains a cycle or missing node; use raw JSON"
                )
            visited.add(node_id)
            branch.append(mapping[node_id])
            node_id = mapping[node_id].get("parent")
        branch.reverse()
    else:
        # Older archives can omit current_node. The fallback cannot recover selection.
        node_id = next((key for key, value in mapping.items() if value.get("parent") is None), None)
        while node_id:
            if node_id in visited or node_id not in mapping:
                raise ExportError(
                    "Conversation tree contains a cycle or missing node; use raw JSON"
                )
            visited.add(node_id)
            node = mapping[node_id]
            branch.append(node)
            node_id = next(iter(node.get("children", [])), None)
    messages = []
    for node in branch:
        msg = node.get("message") or {}
        role = msg.get("author", {}).get("role")
        if role not in {"user", "assistant"} or msg.get("metadata", {}).get(
            "is_visually_hidden_from_conversation"
        ):
            continue
        content = msg.get("content") or {}
        parts = content.get("parts", [])
        text_parts = []
        for part in parts:
            if isinstance(part, str) and part.strip():
                text_parts.append(part)
            elif isinstance(part, dict):
                if part.get("text"):
                    text_parts.append(str(part["text"]))
                else:
                    text_parts.append(
                        f"[Non-text content: {part.get('content_type', 'attachment')}; preserved in raw JSON]"
                    )
        if not parts and content.get("text"):
            text = str(content["text"])
            text_parts.append(
                f"```\n{text}\n```" if content.get("content_type") == "code" else text
            )
        if text_parts:
            messages.append(
                {
                    "role": role,
                    "content": "\n\n".join(text_parts),
                    "timestamp": parse_timestamp(msg.get("create_time")),
                }
            )
    return messages


def conversation_to_markdown(meta: dict, messages: list[dict]) -> str:
    lines = [
        f"# {meta.get('title') or 'Untitled'}",
        "",
        f"**Created:** {parse_timestamp(meta.get('create_time'))}  ",
        f"**Last updated:** {parse_timestamp(meta.get('update_time'))}",
        "",
        "---",
        "",
    ]
    for msg in messages:
        label = "You" if msg["role"] == "user" else "ChatGPT"
        lines.extend([f"**{label}** _{msg['timestamp']}_", "", msg["content"], "", "---", ""])
    return "\n".join(lines)
