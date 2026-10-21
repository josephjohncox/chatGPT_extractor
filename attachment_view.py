"""Safe local attachment links and raster previews; never execute archived artifacts."""

import base64
import hashlib
import html
import json
import os
import re
from pathlib import Path
from urllib.parse import quote

from attachment_refs import ASSET_DIR, managed_path
from chatgpt_client import ExportError
from export_io import decode_json, write_text_file
from progress_report import NO_PROGRESS, ProgressReporter


def attachment_entries(root: Path) -> tuple[list[dict], list[str]]:
    try:
        manifest = managed_path(root, ASSET_DIR + "/manifest.json")
        if not manifest.exists():
            return [], []
        marker = managed_path(root, ASSET_DIR + "/.managed")
        if not marker.is_file() or marker.read_text() != "chatgpt-extractor attachments v1":
            raise ExportError("Unowned attachment manifest")
        data = decode_json(manifest.read_text())
        if (
            not isinstance(data, dict)
            or data.get("version") != 1
            or not isinstance(data.get("assets"), dict)
        ):
            raise ExportError("Invalid attachment manifest")
        entries = list(data["assets"].values())
        if not all(
            isinstance(item, dict)
            and isinstance(item.get("reference"), dict)
            and isinstance(item.get("source_paths", []), list)
            and all(isinstance(path, str) for path in item.get("source_paths", []))
            for item in entries
        ):
            raise ExportError("Invalid attachment entries")
        return entries, []
    except (OSError, ValueError, ExportError):
        return [], ["Attachment manifest could not be read safely; chat sources remain available"]


MAX_VIEWER_BYTES = 16 * 1024 * 1024


def prepare_asset_views(
    entries: list[dict], root: Path, site: Path, *, progress: ProgressReporter = NO_PROGRESS
) -> None:
    views = site / "asset-views"
    if views.is_symlink() or not views.resolve().is_relative_to(root.resolve()):
        raise ExportError("Attachment viewer directory escapes the output root")
    if not entries:
        return
    views.mkdir(mode=0o700, exist_ok=True)
    template = (Path(__file__).resolve().parent / "templates/asset-file.html").read_text()

    def make_view(relative: str, name: str, status: str) -> tuple[str | None, str]:
        path = managed_path(root, relative)
        if not path.is_file():
            return None, "Local attachment file is missing."
        if path.stat().st_size > MAX_VIEWER_BYTES:
            return (
                None,
                "Saved locally; browser download is disabled above the 16 MiB viewer cap. Use the original file from the export directory.",
            )
        with path.open("rb") as stream:
            data = stream.read(MAX_VIEWER_BYTES + 1)
        if len(data) > MAX_VIEWER_BYTES:
            return None, "File grew beyond the browser viewer cap; use its local copy."
        payload = (
            json.dumps({"name": name, "data": base64.b64encode(data).decode("ascii")})
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
            .replace("&", "\\u0026")
        )
        preview = "<p>Binary content is not interpreted here.</p>"
        if not raster_image(path):
            try:
                text = data.decode("utf-8")
                if "\x00" not in text:
                    preview = "<pre>" + html.escape(text) + "</pre>"
            except UnicodeError:
                preview = "<p>Binary content is not interpreted here.</p>"
        values = {
            "MARKER": "<!-- chatgpt-extractor local index v1 -->",
            "TITLE": html.escape(name),
            "STATUS": html.escape(status),
            "SIZE": str(len(data)),
            "PAYLOAD": payload,
            "PREVIEW": preview,
        }
        target = views / (hashlib.sha256(relative.encode()).hexdigest() + ".html")
        if target.is_symlink():
            raise ExportError("Attachment viewer must not be a symlink")
        write_text_file(
            target, re.sub(r"\{\{([A-Z_]+)\}\}", lambda match: values[match[1]], template)
        )
        return target.relative_to(root).as_posix(), ""

    with progress.stage("Build inert attachment viewers", total=len(entries)) as work:
        for entry in entries:
            for field in ("_viewer", "_history_viewer", "_viewer_note"):
                entry.pop(field, None)
            try:
                name = str(entry.get("name") or entry["reference"].get("name") or "attachment.bin")
                relative = entry.get("path")
                if isinstance(relative, str):
                    viewer, note = make_view(relative, name, str(entry.get("outcome", "unknown")))
                    if viewer:
                        entry["_viewer"] = viewer
                    if note:
                        entry["_viewer_note"] = note
                history = entry.get("history")
                if isinstance(history, str):
                    viewer, note = make_view(
                        history, name + ".history.json", "Canvas operation history"
                    )
                    if viewer:
                        entry["_history_viewer"] = viewer
                    elif note:
                        entry["_viewer_note"] = note
            except (OSError, ExportError, ValueError):
                entry["_viewer_note"] = (
                    "Attachment viewer unavailable; original local data was not changed."
                )
            finally:
                work.advance()


def raster_image(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            magic = stream.read(16)
        return magic.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")) or (
            magic.startswith(b"RIFF") and magic[8:12] == b"WEBP"
        )
    except OSError:
        return False


def render_attachments(
    entries: list[dict],
    root: Path,
    page_directory: Path,
    *,
    conversation_id: str | None = None,
    source_paths: list[str] | None = None,
) -> str:
    blocks = []
    for entry in entries:
        ref = entry["reference"]
        if conversation_id is not None or source_paths is not None:
            same_chat = bool(conversation_id) and ref.get("conversation_id") == conversation_id
            same_source = bool(set(source_paths or []) & set(entry.get("source_paths", [])))
            if not same_chat and not same_source:
                continue
        name = str(entry.get("name") or ref.get("name") or "Attachment")
        outcome = str(entry.get("outcome") or "unknown")
        content = f'<p><strong>{html.escape(name)}</strong> <span class="metadata">({html.escape(outcome)})</span></p>'
        if outcome != "saved":
            reason = entry.get("error") or entry.get("reconstruction")
            if reason:
                content += '<p class="notice">' + html.escape(str(reason)) + "</p>"
            if isinstance(entry.get("status"), int):
                content += f'<p class="metadata">HTTP {entry["status"]}</p>'
        relative = entry.get("path")
        try:
            path = managed_path(root, relative) if isinstance(relative, str) else None
            if path is not None and path.is_file():
                href = "./" + quote(
                    Path(os.path.relpath(path, page_directory)).as_posix(), safe="/"
                )
                href = html.escape(href, quote=True)
                viewer = entry.get("_viewer")
                if isinstance(viewer, str):
                    target = root / viewer
                    if (
                        not target.is_symlink()
                        and target.resolve().is_relative_to(
                            (root / ".chatgpt-index/asset-views").resolve()
                        )
                        and target.is_file()
                    ):
                        viewer_href = "./" + quote(
                            Path(os.path.relpath(target, page_directory)).as_posix(), safe="/"
                        )
                        content += f'<a class="attachment-open" href="{html.escape(viewer_href, quote=True)}">View / download</a>'
                if entry.get("_viewer_note"):
                    content += '<p class="notice">' + html.escape(entry["_viewer_note"]) + "</p>"
                content += '<p class="metadata">Local file: ' + html.escape(str(relative)) + "</p>"
                if outcome == "saved" and raster_image(path):
                    content += f'<p><img class="attachment-preview" loading="lazy" src="{href}" alt="{html.escape(name, quote=True)}"></p>'
            elif outcome in {"saved", "partial"}:
                content += '<p class="notice">Local file is missing; rerun attachment backfill.</p>'
            history = entry.get("history")
            if isinstance(history, str):
                history_path = managed_path(root, history)
                if history_path.is_file():
                    viewer = entry.get("_history_viewer")
                    history_target = root / viewer if isinstance(viewer, str) else None
                    if (
                        history_target is not None
                        and not history_target.is_symlink()
                        and history_target.resolve().is_relative_to(
                            (root / ".chatgpt-index/asset-views").resolve()
                        )
                        and history_target.is_file()
                    ):
                        href = "./" + quote(
                            Path(os.path.relpath(history_target, page_directory)).as_posix(),
                            safe="/",
                        )
                        content += f'<p><a href="{html.escape(href, quote=True)}">Canvas operation history</a></p>'
                    else:
                        content += '<p class="metadata">Canvas history saved locally; use the export directory.</p>'
        except (OSError, ExportError):
            content += (
                '<p class="notice">Unsafe or missing local attachment path was not linked.</p>'
            )
        blocks.append(f'<section class="attachment">{content}</section>')
    return (
        "<section><h2>Attachments &amp; artifacts</h2>" + "\n".join(blocks) + "</section>"
        if blocks
        else ""
    )
