"""Turn-oriented presentation of archival reasoning/tool activity; never executable calls."""

import html
import json
import re
from copy import deepcopy

from openwebui_layouts import fence

PREVIEW_LIMIT = 65536
MERMAID_LIMIT = 65536
MERMAID_FENCE = re.compile(
    r"(?ms)^ {0,3}```mermaid[ \t]*\r?\n(.*?)^ {0,3}```[ \t]*(?:\r?\n|(?=<)|$)"
)
CONFLUENCE_PARAMETERS = re.compile(r'data-parameters="([^"]*)"')


def activity_kind(source: dict, rendered: str) -> str | None:
    role = (source.get("author") or {}).get("role")
    if role == "tool":
        return "tool_result"
    if role != "assistant":
        return None
    recipient = source.get("recipient")
    if isinstance(recipient, str) and recipient not in {"", "all"}:
        return "tool_call"
    if re.fullmatch(r'\s*(?:<details type="reasoning"[^>]*>.*?</details>\s*)+', rendered, re.S):
        return "reasoning"
    if source.get("channel") == "commentary" and source.get("end_turn") is not True:
        return "commentary"
    return None


def extract_historical_mermaid(body: str) -> list[str]:
    """Recognize only explicit Mermaid source, never arbitrary HTML or JavaScript."""

    def strings(value) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, dict):
            return [text for item in value.values() for text in strings(item)]
        if isinstance(value, list):
            return [text for item in value for text in strings(item)]
        return []

    try:
        parsed = json.loads(body)
    except (TypeError, ValueError):
        parsed = body
    found: list[str] = []
    for candidate in strings(parsed):
        found.extend(match.group(1).strip() for match in MERMAID_FENCE.finditer(candidate))
        for attribute in CONFLUENCE_PARAMETERS.findall(candidate):
            try:
                # Tool payload decoding can expose raw line breaks inside this HTML attribute.
                params = json.loads(
                    html.unescape(attribute).replace("\r\n", "\\n").replace("\n", "\\n")
                )
            except (TypeError, ValueError):
                continue
            guest = params.get("guestParams") if isinstance(params, dict) else None
            diagram = guest.get("__bodyContent") if isinstance(guest, dict) else None
            if guest and guest.get("syntax") == "Mermaid" and isinstance(diagram, str):
                found.append(diagram.strip())
    unique = []
    for diagram in found:
        if diagram and len(diagram) <= MERMAID_LIMIT and diagram not in unique:
            unique.append(diagram)
    return unique


def tool_record(source: dict, rendered: str, kind: str) -> str:
    content = source.get("content") or {}
    raw_text = content.get("text")
    body = raw_text if isinstance(raw_text, str) else rendered
    name = (
        source.get("recipient") if kind == "tool_call" else (source.get("author") or {}).get("name")
    )
    language = "text"
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        payload = None
    if isinstance(payload, (dict, list)):
        language = "json"
        body = json.dumps(payload, indent=2, ensure_ascii=False)
        if (
            kind == "tool_call"
            and isinstance(payload, dict)
            and isinstance(payload.get("path"), str)
        ):
            name = payload["path"]
    label = "Historical tool call" if kind == "tool_call" else "Historical tool result"
    if isinstance(name, str) and name:
        label += ": " + name[:240].replace("\n", " ").replace("\r", " ")
    suffix = ""
    if len(body) > PREVIEW_LIMIT:
        body = body[:PREVIEW_LIMIT]
        suffix = "\n\nPreview truncated; the complete record is retained in chatgpt_source."
    # Generic details, not live tool_calls/output/approval fields. Source HTML stays code.
    display = (
        "Historical record only; not executed during import.\n\n" + fence(body, language) + suffix
    )
    diagrams = extract_historical_mermaid(body)
    previews = "".join(
        '\n\n<details type="imported_diagram" done="true">\n'
        f"<summary>Historical Mermaid diagram {number}</summary>\n\n"
        + fence(diagram, "mermaid")
        + "\n</details>"
        for number, diagram in enumerate(diagrams, 1)
    )
    return (
        '<details type="imported_tool" done="true">\n'
        + "<summary>"
        + html.escape(label)
        + "</summary>\n\n"
        + html.escape(display, quote=False)
        + previews
        + "\n\n</details>"
    )


def compact_history(messages: dict[str, dict], current_id: str) -> dict[str, dict]:
    """Fold each branch's activity prefix into its next answer, without mixing sibling paths."""
    anchors = {
        key
        for key, message in messages.items()
        if not message.get("chatgpt_activity_kind")
        or key == current_id
        or not message["childrenIds"]
        or any(messages[child]["role"] == "user" for child in message["childrenIds"])
    }
    result = {}
    for key, source in messages.items():
        if key not in anchors:
            continue
        message = deepcopy(source)
        records = []
        if source.get("chatgpt_activity_kind"):
            records.append(source)
        parent = source["parentId"]
        while parent is not None and parent not in anchors:
            records.append(messages[parent])
            parent = messages[parent]["parentId"]
        records.reverse()
        message["parentId"] = parent
        message["childrenIds"] = []
        if records:
            # Escape child blocks for the outer details tokenizer's decode step.
            # Nested reasoning/tool blocks then retain their own body escaping.
            blocks = "\n\n".join(record["content"] for record in records)
            group = (
                '<details type="imported_activity" done="true">\n'
                f"<summary>Imported activity ({len(records)} archived records)</summary>\n\n"
                + html.escape(blocks, quote=False)
                + "\n\n</details>"
            )
            if source.get("chatgpt_activity_kind"):
                answer = "[No final assistant answer was recorded for this branch.]"
                message["chatgpt_incomplete_turn"] = True
            else:
                answer = source["content"]
            message["content"] = group + "\n\n" + answer
            message["chatgpt_activity"] = [
                {
                    "id": record["id"],
                    "kind": record["chatgpt_activity_kind"],
                    "timestamp": record["timestamp"],
                }
                for record in records
            ]
            message["chatgpt_source_node_ids"] = list(
                dict.fromkeys([record["id"] for record in records] + [key])
            )
            message.pop("chatgpt_activity_kind", None)
        result[key] = message
    for key, message in result.items():
        parent = message["parentId"]
        if parent is not None:
            result[parent]["childrenIds"].append(key)
    return result
