"""Build a private, file://-compatible index and readable pages from existing exports."""

import hashlib
import html
import math
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import quote

from attachment_view import attachment_entries, prepare_asset_views, render_attachments
from chatgpt_client import ExportError
from conversation_format import extract_messages, parse_timestamp
from export_io import decode_json, write_text_file
from progress_report import NO_PROGRESS, ProgressReporter

MARKER = "<!-- chatgpt-extractor local index v1 -->"
SITE = ".chatgpt-index"
MAX_FILE_BYTES = 64 * 1024 * 1024
TEMPLATES = Path(__file__).resolve().parent / "templates"


@dataclass
class Source:
    path: Path
    position: int | None = None
    updated: float | None = None


@dataclass
class Chat:
    key: str
    title: str
    project: str
    created: float | None
    updated: float | None
    raw: list[Source] = field(default_factory=list)
    markdown: list[Path] = field(default_factory=list)
    markdown_updates: dict[Path, float | None] = field(default_factory=dict)


@dataclass
class IndexResult:
    path: Path
    chats: int
    projects: int
    ignored: int
    warnings: list[str]


def timestamp(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError):
        try:
            dt = datetime.fromisoformat(
                str(value).strip().removesuffix(" UTC").replace("Z", "+00:00")
            )
            number = dt.replace(tzinfo=dt.tzinfo or UTC).timestamp()
        except (ValueError, OverflowError, OSError):
            return None
    try:
        datetime.fromtimestamp(number, UTC)
    except (ValueError, OverflowError, OSError):
        return None
    return number if math.isfinite(number) else None


def project_name(relative: Path) -> str:
    # Preserve folder identity: two projects can share a display name but differ by ID suffix.
    parts = list(relative.parent.parts)
    return "/".join(parts) if parts and parts != ["_Uncategorized"] else "Uncategorized"


def fallback_title(path: Path) -> str:
    return re.sub(r"--[a-f0-9]{32}$", "", path.stem) or "Untitled"


def read_text(path: Path) -> str:
    if path.is_symlink() or path.stat().st_size > MAX_FILE_BYTES:
        raise ExportError("Symlink or file exceeds the 64 MiB indexing limit")
    return path.read_text(encoding="utf-8-sig")


@lru_cache(maxsize=1)
def read_json(path: Path) -> Any:
    return decode_json(read_text(path))


def records(data: Any) -> list[tuple[dict, int | None]]:
    if isinstance(data, dict) and isinstance(data.get("mapping"), dict):
        return [(data, None)]
    if isinstance(data, dict):
        data = data.get("conversations")
    if isinstance(data, list):
        return [
            (item, i)
            for i, item in enumerate(data)
            if isinstance(item, dict) and isinstance(item.get("mapping"), dict)
        ]
    return []


def markdown_version_hints(root: Path) -> tuple[dict[str, dict], list[str]]:
    """Optional exact metadata; each Markdown file's SHA must match before it is used."""
    path = root / ".export-index.json"
    if not path.exists():
        return {}, []
    if path.is_symlink():
        return {}, [
            "Incremental checkpoint is a symlink; exact Markdown version hints were ignored"
        ]
    try:
        data = read_json(path)
        if not isinstance(data, dict):
            raise ExportError("Invalid checkpoint")
    except (OSError, ValueError, ExportError):
        return {}, ["Incremental checkpoint could not be read; using source-file metadata"]
    hints = {}
    for key, entry in data.items():
        if (
            isinstance(key, str)
            and key.startswith("markdown:")
            and key.removeprefix("markdown:")
            and isinstance(entry, dict)
            and isinstance(entry.get("path"), str)
            and isinstance(entry.get("sha256"), str)
        ):
            hints[entry["path"].replace("\\", "/")] = {
                **entry,
                "conversation_id": key.removeprefix("markdown:"),
            }
    return hints, []


def scan_directory(
    root: Path, *, progress: ProgressReporter | None = None
) -> tuple[list[Chat], int, list[str]]:
    """Ignore auxiliary/hidden trees and never follow source symlinks."""
    chats: dict[str, Chat] = {}
    aliases: dict[Path, str] = {}
    markdown = []
    ignored = 0
    version_hints, warnings = markdown_version_hints(root)
    report = progress if progress is not None else NO_PROGRESS
    source_number = 0

    def walk_error(_: OSError) -> None:
        warnings.append("A subdirectory could not be read")

    with report.stage("Scan export directory") as work:
        for directory, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
            parent = Path(directory)
            dirs[:] = sorted(
                name
                for name in dirs
                if not name.startswith(".")
                and name != "node_modules"
                and not (parent == root and name in {"settings", "memories"})
                and not (parent / name).is_symlink()
            )
            for name in sorted(files):
                path = parent / name
                relative = path.relative_to(root)
                if name.startswith(".") or path.suffix.lower() not in {".json", ".md", ".markdown"}:
                    continue
                source_number += 1
                work.update(
                    completed=source_number,
                    detail=f"{len(chats)} chats discovered; {ignored} non-chat JSON; {len(warnings)} warnings",
                )
                if path.is_symlink() or not path.resolve().is_relative_to(root):
                    warnings.append(f"{relative}: skipped a source symlink or escaping path")
                    continue
                if path.suffix.lower() in {".md", ".markdown"}:
                    markdown.append(path)
                    continue
                try:
                    found = records(read_json(path))
                    if not found:
                        ignored += 1
                        continue
                    for detail, position in found:
                        ident = detail.get("id") or detail.get("conversation_id")
                        key = (
                            f"id:{ident}"
                            if isinstance(ident, str) and ident
                            else f"file:{relative.as_posix()}:{position}"
                        )
                        updated = timestamp(detail.get("update_time"))
                        if key not in chats:
                            chats[key] = Chat(
                                key,
                                str(detail.get("title") or fallback_title(path)),
                                project_name(relative),
                                timestamp(detail.get("create_time")),
                                updated,
                            )
                        chat = chats[key]
                        if updated is not None and (chat.updated is None or updated > chat.updated):
                            chat.title = str(detail.get("title") or fallback_title(path))
                            chat.project = project_name(relative)
                            chat.created = timestamp(detail.get("create_time"))
                            chat.updated = updated
                        chat.raw.append(Source(path, position, updated))
                        if position is None:
                            aliases[relative.with_suffix("")] = key
                except (OSError, ValueError, ExportError):
                    warnings.append(f"{relative}: unable to read conversation JSON")
        with report.stage(
            "Read Markdown sources", total=len(markdown), transient=True
        ) as markdown_work:
            for path in markdown:
                relative = path.relative_to(root)
                try:
                    text = read_text(path)
                    key = aliases.get(
                        relative.with_suffix(""), f"markdown:{relative.with_suffix('').as_posix()}"
                    )
                    heading = re.search(r"(?m)^#\s+(.+?)\s*$", text)
                    dates = dict(
                        re.findall(r"(?m)^\*\*(Created|Last updated):\*\*\s*([^\r\n]+)", text)
                    )
                    created = timestamp(dates.get("Created"))
                    updated = timestamp(dates.get("Last updated"))
                    hint = version_hints.get(relative.as_posix())
                    if hint and hashlib.sha256(path.read_bytes()).hexdigest() == hint["sha256"]:
                        key = "id:" + hint["conversation_id"]
                        precise = timestamp(hint.get("detail_update_time"))
                        if precise is None:
                            precise = timestamp(hint.get("update_time"))
                        if precise is not None:
                            updated = precise
                    if key not in chats:
                        chats[key] = Chat(
                            key,
                            heading[1] if heading else fallback_title(path),
                            project_name(relative),
                            created,
                            updated,
                        )
                    chat = chats[key]
                    if updated is not None and (chat.updated is None or updated > chat.updated):
                        chat.title = heading[1] if heading else fallback_title(path)
                        chat.project = project_name(relative)
                        chat.created = created if created is not None else chat.created
                        chat.updated = updated
                    chat.markdown.append(path)
                    chat.markdown_updates[path] = updated
                except (OSError, ValueError, ExportError):
                    warnings.append(f"{relative}: unable to read conversation Markdown")
                markdown_work.advance(detail=f"{len(chats)} chats; {len(warnings)} warnings")
        work.finish(
            f"{source_number} candidate files; {len(chats)} chats; {ignored} non-chat JSON; {len(warnings)} warnings"
        )
        read_json.cache_clear()
        return (
            sorted(
                chats.values(),
                key=lambda chat: (chat.project.casefold(), chat.title.casefold(), chat.key),
            ),
            ignored,
            warnings,
        )


def render_template(name: str, values: dict[str, str]) -> str:
    template = (TEMPLATES / name).read_text(encoding="utf-8")
    # Substitute once in the template, never recursively in user-controlled text.
    return re.sub(r"\{\{([A-Z_]+)\}\}", lambda match: values[match[1]], template)


def local_link(path: Path, page_directory: Path) -> str:
    return "./" + quote(Path(os.path.relpath(path, page_directory)).as_posix(), safe="/")


def source_links(chat: Chat, directory: Path) -> str:
    links = []
    for label, paths in (
        ("JSON", [source.path for source in chat.raw]),
        ("Markdown", chat.markdown),
    ):
        for i, path in enumerate(dict.fromkeys(paths), 1):
            title = label if i == 1 else f"{label} {i}"
            links.append(
                f'<a href="{html.escape(local_link(path, directory), quote=True)}">{title}</a>'
            )
    return " · ".join(links)


def conversation_body(chat: Chat) -> str:
    def markdown_time(path: Path) -> float:
        value = chat.markdown_updates.get(path)
        return value if value is not None else -math.inf

    latest_markdown = max(chat.markdown, key=markdown_time, default=None)
    markdown_updated = (
        chat.markdown_updates.get(latest_markdown) if latest_markdown is not None else None
    )
    raw_updated = max(
        (source.updated if source.updated is not None else -math.inf for source in chat.raw),
        default=-math.inf,
    )
    if latest_markdown is not None and (
        not chat.raw or (markdown_updated is not None and markdown_updated > raw_updated)
    ):
        return f'<pre class="markdown">{html.escape(read_text(latest_markdown))}</pre>'
    source = max(chat.raw, key=lambda item: item.updated if item.updated is not None else -math.inf)
    detail = read_json(source.path)
    if source.position is not None:
        detail = (
            detail[source.position]
            if isinstance(detail, list)
            else detail["conversations"][source.position]
        )
    messages = extract_messages(detail)
    if not messages:
        return '<p class="notice">No visible text messages. Open the source JSON for structured content.</p>'
    return "\n".join(
        f'<section class="message {html.escape(message["role"])}"><h2>{"You" if message["role"] == "user" else "ChatGPT"}</h2>'
        f"<time>{html.escape(message['timestamp'])}</time><pre>{html.escape(message['content'])}</pre></section>"
        for message in messages
    )


def build_index(directory: Path, *, progress: ProgressReporter | None = None) -> IndexResult:
    report = progress if progress is not None else NO_PROGRESS
    root = directory.expanduser().resolve()
    if not root.is_dir():
        raise ExportError("Index source must be an existing export directory")
    index = root / "index.html"
    if index.is_symlink() or (
        index.exists() and not index.read_text(encoding="utf-8").startswith(MARKER)
    ):
        raise ExportError(
            "index.html already exists and is not this tool's generated index; move it aside first"
        )
    site = root / SITE
    pages = site / "chats"
    for target in (site, pages):
        if target.is_symlink() or not target.resolve().is_relative_to(root):
            raise ExportError(
                "Generated index directory must not be a symlink or escape the export root"
            )
    ownership = site / ".managed"
    if site.exists() and (
        ownership.is_symlink() or not ownership.is_file() or ownership.read_text() != MARKER
    ):
        raise ExportError("The .chatgpt-index directory is not owned by this index generator")
    pages.mkdir(parents=True, exist_ok=True, mode=0o700)
    # mkdir(parents=True) doesn't propagate mode to intermediate parents.
    if not ownership.exists():
        site.chmod(0o700)
        write_text_file(ownership, MARKER)
    chats, ignored, warnings = scan_directory(root, progress=report)
    asset_entries, asset_warnings = attachment_entries(root)
    warnings.extend(asset_warnings)
    prepare_asset_views(asset_entries, root, site, progress=report)
    rows = []
    projects: dict[str, int] = {}
    try:
        with report.stage("Build readable chat pages", total=len(chats)) as work:
            for chat in chats:
                projects[chat.project] = projects.get(chat.project, 0) + 1
                page = pages / (hashlib.sha256(chat.key.encode()).hexdigest()[:32] + ".html")
                if page.is_symlink() or not page.resolve().is_relative_to(root):
                    raise ExportError("Generated chat page path escapes the export directory")
                try:
                    body = conversation_body(chat)
                except (OSError, ValueError, TypeError, KeyError, AttributeError, ExportError):
                    body = '<p class="notice">Unable to render this conversation. The source file links below are still available.</p>'
                    warnings.append(
                        f"{chat.title}: could not render messages; source files retained"
                    )
                write_text_file(
                    page,
                    render_template(
                        "conversation.html",
                        {
                            "MARKER": MARKER,
                            "TITLE": html.escape(chat.title),
                            "PROJECT": html.escape(chat.project),
                            "CREATED": html.escape(parse_timestamp(chat.created)),
                            "UPDATED": html.escape(parse_timestamp(chat.updated)),
                            "CONTENT": body,
                            "ASSETS": render_attachments(
                                asset_entries,
                                root,
                                pages,
                                conversation_id=chat.key.removeprefix("id:")
                                if chat.key.startswith("id:")
                                else "",
                                source_paths=[
                                    str(source.path.relative_to(root)) for source in chat.raw
                                ]
                                + [str(path.relative_to(root)) for path in chat.markdown],
                            ),
                            "SOURCES": source_links(chat, pages),
                        },
                    ),
                )
                search = " ".join(
                    [
                        chat.title,
                        chat.project,
                        *(str(source.path.relative_to(root)) for source in chat.raw),
                        *(str(path.relative_to(root)) for path in chat.markdown),
                    ]
                )
                updated = str(chat.updated) if chat.updated is not None else ""
                rows.append(
                    f'<tr data-project="{html.escape(chat.project, quote=True)}" data-search="{html.escape(search.casefold(), quote=True)}" data-title="{html.escape(chat.title.casefold(), quote=True)}" data-updated="{updated}">'
                    f'<td><a class="chat-title" href="{html.escape(local_link(page, root), quote=True)}">{html.escape(chat.title)}</a></td>'
                    f"<td>{html.escape(chat.project)}</td><td>{html.escape(parse_timestamp(chat.updated))}</td><td>{source_links(chat, root)}</td></tr>"
                )
                work.advance(detail=f"{work.completed + 1} pages written; {len(warnings)} warnings")
        project_names = sorted(projects, key=str.casefold)
        options = "".join(
            f'<option value="{html.escape(name, quote=True)}">{html.escape(name)} ({projects[name]})</option>'
            for name in project_names
        )
        buttons = (
            '<button type="button" class="project-button" data-project="">All chats</button>'
            + "".join(
                f'<button type="button" class="project-button" data-project="{html.escape(name, quote=True)}">{html.escape(name)} <span>{projects[name]}</span></button>'
                for name in project_names
            )
        )
        notes = "".join(f"<li>{html.escape(message)}</li>" for message in warnings)
        warning_html = (
            f'<details class="notice"><summary>{len(warnings)} indexing warnings</summary><ul>{notes}</ul></details>'
            if warnings
            else ""
        )
        if asset_entries:
            gallery = site / "assets.html"
            if gallery.is_symlink() or not gallery.resolve().is_relative_to(root):
                raise ExportError("Attachment gallery path escapes the output directory")
            with report.stage("Build attachment gallery", total=1) as asset_work:
                write_text_file(
                    gallery,
                    render_template(
                        "assets.html",
                        {
                            "MARKER": MARKER,
                            "DIRECTORY": html.escape(root.name),
                            "COUNT": str(len(asset_entries)),
                            "ASSET_ITEMS": render_attachments(asset_entries, root, site),
                        },
                    ),
                )
                asset_work.advance(detail=f"{len(asset_entries)} attachment references")
        with report.stage("Write HTML catalog", total=1) as catalog_work:
            write_text_file(
                index,
                render_template(
                    "index.html",
                    {
                        "MARKER": MARKER,
                        "DIRECTORY": html.escape(root.name),
                        "CHAT_COUNT": str(len(chats)),
                        "PROJECT_COUNT": str(len(projects)),
                        "BUILT_AT": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
                        "ROWS": "\n".join(rows),
                        "ASSETS_LINK": f'<p><a href="./.chatgpt-index/assets.html">Attachments &amp; artifacts ({len(asset_entries)})</a></p>'
                        if asset_entries
                        else "",
                        "OPTIONS": options,
                        "PROJECT_BUTTONS": buttons,
                        "WARNINGS": warning_html,
                    },
                ),
            )
            catalog_work.advance(
                detail=f"{len(chats)} chats; {len(projects)} folders; {len(warnings)} warnings"
            )
    finally:
        read_json.cache_clear()
    return IndexResult(index, len(chats), len(projects), ignored, warnings)
