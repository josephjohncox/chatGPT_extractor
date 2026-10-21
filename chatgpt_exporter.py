#!/usr/bin/env python3
"""Export ChatGPT web conversations or convert an official data export locally."""

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from chatgpt_client import ChatGPTClient, ExportError, HTTPError
from export_config import Config, ConfigError, normalize_project_id


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


def write_text_file(path: Path, text: str) -> None:
    """Atomic replacement with owner-only permissions; never leave partial exports."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".export-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def write_json_file(path: Path, payload: Any) -> None:
    write_text_file(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def decode_json(data: str | bytes) -> Any:
    try:
        return json.loads(data)
    except (ValueError, UnicodeError):
        raise ExportError("Invalid JSON in archive or export index (content withheld)") from None


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


def export_auxiliary_data(client: ChatGPTClient, token: str, root: Path) -> None:
    """Keep local enhancements: memories and settings, with explicit availability records."""
    endpoints = {
        "memories/memories.json": ("memories", {"include_memory_entries": "true"}),
        "settings/custom_instructions.json": ("user_system_messages", None),
        "settings/settings.json": ("settings/user", None),
        "settings/beta_features.json": ("settings/beta_features", None),
        "settings/models.json": ("models", None),
        "settings/account.json": ("accounts/check/v4-2023-04-27", None),
        "settings/codex_usage.json": ("codex/usage", None),
        "settings/compliance.json": ("compliance", None),
    }
    statuses: dict[str, dict[str, Any]] = {}
    for filename, (endpoint, params) in endpoints.items():
        try:
            payload = client.json(endpoint, token, params=params, label=endpoint)
        except HTTPError as exc:
            if exc.status in {401, 403}:
                raise
            statuses[filename] = {"status": exc.status, "available": False}
            continue
        except ExportError as exc:
            statuses[filename] = {"available": False, "error": str(exc)}
            continue
        write_json_file(safe_output_path(root, filename), payload)
        statuses[filename] = {"status": 200, "available": True}
    write_json_file(safe_output_path(root, "auxiliary-status.json"), statuses)
    unavailable = sum(not item["available"] for item in statuses.values())
    print(
        f"Auxiliary snapshots: {len(statuses) - unavailable} available, {unavailable} unavailable (see auxiliary-status.json)"
    )


def project_folder(meta: dict) -> str:
    ident = (
        meta.get("_project_id")
        or meta.get("project_id")
        or meta.get("gizmo_id")
        or meta.get("conversation_template_id")
    )
    # A workspace is an account boundary, NOT a project. Custom GPTs are not projects either.
    if not isinstance(ident, str) or not ident.startswith("g-p-"):
        return "_Uncategorized"
    ident = normalize_project_id(ident)
    name = meta.get("_folder_name") or ident
    return f"{sanitize_filename(str(name))}--{hashlib.sha256(ident.encode()).hexdigest()[:12]}"


def safe_output_path(root: Path, relative: str) -> Path:
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()):
        raise ExportError("Output path escapes the export directory (check names or symlinks)")
    return path


def export_conversations(
    metadata: list[dict], fetch_detail: Any, config: Config, *, refresh: bool = False
) -> int:
    root = config.output_dir
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    index_path = safe_output_path(root, ".export-index.json")
    index = decode_json(index_path.read_text()) if index_path.exists() else {}
    if not isinstance(index, dict) or not all(isinstance(entry, dict) for entry in index.values()):
        raise ExportError("Invalid export index; move .export-index.json aside to rebuild")
    saved = skipped = failed = 0
    seen = set()
    for meta in metadata:
        ident = meta.get("id") or meta.get("conversation_id")
        if not isinstance(ident, str) or not ident:
            raise ExportError("Conversation without an ID cannot be exported safely")
        if ident in seen:
            continue
        seen.add(ident)
        suffix = hashlib.sha256(ident.encode()).hexdigest()[:32]
        extension = "json" if config.output_format == "json" else "md"
        relative = f"{project_folder(meta)}/{sanitize_filename(str(meta.get('title') or 'Untitled'))}--{suffix}.{extension}"
        path = safe_output_path(root, relative)
        key = f"{config.output_format}:{ident}"
        previous = index.get(key, {})
        updated = meta.get("update_time")
        if (
            not refresh
            and updated is not None
            and previous.get("update_time") == updated
            and previous.get("path") == relative
            and path.is_file()
            and previous.get("sha256") == hashlib.sha256(path.read_bytes()).hexdigest()
        ):
            skipped += 1
            continue
        try:
            detail = fetch_detail(ident)
            if not isinstance(detail, dict) or not detail:
                raise ExportError("Conversation detail is empty or not an object")
            detail_id = detail.get("id") or detail.get("conversation_id")
            if detail_id and detail_id != ident:
                raise ExportError("Conversation detail ID does not match its listing")
            if not detail_id:
                detail = dict(detail, conversation_id=ident)
            text = (
                json.dumps(detail, indent=2, ensure_ascii=False) + "\n"
                if config.output_format == "json"
                else conversation_to_markdown({**detail, **meta}, extract_messages(detail))
            )
            write_text_file(path, text)
        except HTTPError as exc:
            if exc.status in {401, 403}:
                raise
            print(f"Conversation export failed: {exc}", file=sys.stderr)
            failed += 1
            continue
        except ExportError as exc:
            print(f"Conversation export failed: {exc}", file=sys.stderr)
            failed += 1
            continue
        index[key] = {
            "path": relative,
            "update_time": updated,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        write_json_file(index_path, index)
        saved += 1
    print(f"Saved: {saved}; unchanged: {skipped}; failed: {failed}; output: {root.resolve()}")
    return 1 if failed else 0


def load_archive(path: Path) -> list[dict]:
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            candidates = [
                name for name in archive.namelist() if Path(name).name == "conversations.json"
            ]
            if len(candidates) != 1:
                raise ExportError(
                    "ZIP must contain exactly one conversations.json; no files were extracted"
                )
            if archive.getinfo(candidates[0]).file_size > 512 * 1024 * 1024:
                raise ExportError(
                    "conversations.json exceeds 512 MiB; provide a smaller JSON archive"
                )
            data = decode_json(archive.read(candidates[0]))
    else:
        data = decode_json(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("conversations", [data] if "mapping" in data else None)
    if not isinstance(data, list) or not all(
        isinstance(item, dict) and isinstance(item.get("mapping"), dict) for item in data
    ):
        raise ExportError(
            "Expected conversations.json containing conversations with message mappings"
        )
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", nargs="?", choices=["export", "probe", "import"], default="export"
    )
    parser.add_argument(
        "archive",
        nargs="?",
        type=Path,
        help="conversations.json or official export ZIP (import only)",
    )
    parser.add_argument("--format", choices=["json", "markdown"])
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--refresh", action="store_true", help="Refetch even if metadata is unchanged"
    )
    parser.add_argument(
        "--auxiliary", action="store_true", help="Also snapshot memories/settings (web export only)"
    )
    args = parser.parse_args(argv)
    if args.command == "import" and args.archive is None:
        parser.error("import requires an archive path")
    if args.command != "import" and args.archive is not None:
        parser.error("archive path is valid only for import")
    try:
        from dataclasses import replace

        config = Config.from_env()
        config = replace(
            config,
            output_format=args.format or config.output_format,
            output_dir=args.output or config.output_dir,
        )
        if args.command == "import":
            assert args.archive is not None
            conversations = load_archive(args.archive)
            by_id = {}
            for item in conversations:
                ident = item.get("id") or item.get("conversation_id")
                if not isinstance(ident, str) or not ident or ident in by_id:
                    raise ExportError("Archive has a missing or duplicate conversation ID")
                by_id[ident] = item
            return export_conversations(
                conversations, by_id.__getitem__, config, refresh=args.refresh
            )
        client = ChatGPTClient(config)
        if args.command == "probe":
            report = client.probe()
            print(json.dumps(report, indent=2))
            return int(any("error" in endpoint for endpoint in report["endpoints"].values()))
        token = client.access_token()
        conversations = client.all_conversations(token)
        print(
            f"Discovered {len(conversations)} conversations (including configured/discovered projects)"
        )
        result = export_conversations(
            conversations, lambda ident: client.detail(ident, token), config, refresh=args.refresh
        )
        if args.auxiliary:
            export_auxiliary_data(client, token, config.output_dir)
        return result
    except (ConfigError, ExportError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError, zipfile.BadZipFile, TypeError, KeyError):
        # Do not echo arbitrary file content, JSON fragments, or server responses.
        print(
            "Error: unable to read/write an archive or received an invalid data shape (details withheld)",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
