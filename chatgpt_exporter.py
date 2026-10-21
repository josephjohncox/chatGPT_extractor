#!/usr/bin/env python3
"""Export/import ChatGPT conversations and build offline chat/project webpages."""

import argparse
import hashlib
import json
import os
import sys
import time
import zipfile
from pathlib import Path
from typing import Any

from chatgpt_client import ChatGPTClient, ExportError, HTTPError
from conversation_format import (
    conversation_to_markdown,
)
from conversation_format import (
    extract_messages as extract_messages,
)
from conversation_format import (
    parse_timestamp as parse_timestamp,
)
from conversation_format import (
    sanitize_filename as sanitize_filename,
)
from export_config import Config, ConfigError, boolean, normalize_project_id, output_directory
from export_io import decode_json, write_text_file
from export_io import write_json_file as write_json_file
from local_index import build_index
from progress_report import NO_PROGRESS, ProgressReporter, human_bytes


def export_auxiliary_data(
    client: ChatGPTClient, token: str, root: Path, *, progress: ProgressReporter | None = None
) -> None:
    """Snapshot optional endpoints with per-endpoint progress and interruption-safe status."""
    report = progress if progress is not None else client.progress
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
    statuses: dict[str, dict[str, Any]] = {
        filename: {"available": False, "attempted": False, "outcome": "not_attempted"}
        for filename in endpoints
    }
    status_path = safe_output_path(root, "auxiliary-status.json")
    write_json_file(status_path, statuses)

    def summary() -> str:
        counts = {
            name: sum(item["outcome"] == name for item in statuses.values())
            for name in (
                "saved",
                "unavailable",
                "failed",
                "interrupted",
                "in_progress",
                "not_attempted",
            )
        }
        return "; ".join(f"{name}={count}" for name, count in counts.items())

    try:
        with report.stage("Auxiliary snapshots", total=len(endpoints)) as work:
            for i, (filename, (endpoint, params)) in enumerate(endpoints.items(), 1):
                fatal = None
                statuses[filename] = {
                    "available": False,
                    "attempted": True,
                    "outcome": "in_progress",
                }
                write_json_file(status_path, statuses)
                work.update(detail=f"{i}/{len(endpoints)}: {endpoint}; {summary()}", force=True)
                try:
                    payload = client.json(endpoint, token, params=params, label=endpoint)
                    target = safe_output_path(root, filename)
                    write_json_file(target, payload)
                    statuses[filename] = {
                        "status": 200,
                        "available": True,
                        "attempted": True,
                        "outcome": "saved",
                    }
                    report.notice(
                        f"Auxiliary {endpoint}: saved {human_bytes(target.stat().st_size)}"
                    )
                except HTTPError as exc:
                    outcome = (
                        "unavailable"
                        if exc.status in {403, 404, 405} and not exc.browser_challenge
                        else "failed"
                    )
                    statuses[filename] = {
                        "status": exc.status,
                        "available": False,
                        "attempted": True,
                        "outcome": outcome,
                    }
                    report.warning(
                        f"Auxiliary {endpoint}: {outcome} (HTTP {exc.status}); earlier snapshot retained"
                    )
                    if exc.status == 401 or exc.browser_challenge:
                        fatal = exc
                except ExportError as exc:
                    statuses[filename] = {
                        "available": False,
                        "attempted": True,
                        "outcome": "failed",
                        "error": str(exc),
                    }
                    report.warning(
                        f"Auxiliary {endpoint}: failed; response/request details withheld; earlier snapshot retained"
                    )
                except KeyboardInterrupt:
                    statuses[filename] = {
                        "available": False,
                        "attempted": True,
                        "outcome": "interrupted",
                    }
                    work.update(detail=summary(), force=True)
                    raise
                except OSError:
                    statuses[filename] = {
                        "available": False,
                        "attempted": True,
                        "outcome": "failed",
                        "error": "Local snapshot write failed (details withheld)",
                    }
                    work.update(detail=summary(), force=True)
                    raise
                write_json_file(status_path, statuses)
                work.advance(detail=summary(), force=True)
                if fatal is not None:
                    work.finish(summary(), state="stopped")
                    raise fatal
            work.finish(
                summary(),
                state="warnings"
                if any(not item["available"] for item in statuses.values())
                else "done",
            )
    finally:
        # Entries never reached remain explicitly not_attempted instead of reusing stale status.
        for item in statuses.values():
            if item["outcome"] == "in_progress":
                item["outcome"] = "interrupted"
        write_json_file(status_path, statuses)
        report.notice("Auxiliary summary: " + summary())
    unavailable = sum(not item["available"] for item in statuses.values())
    report.result(
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
    metadata: list[dict],
    fetch_detail: Any,
    config: Config,
    *,
    refresh: bool = False,
    progress: ProgressReporter | None = None,
) -> int:
    report = progress if progress is not None else NO_PROGRESS
    unique: dict[str, dict] = {}
    for meta in metadata:
        ident = meta.get("id") or meta.get("conversation_id")
        if not isinstance(ident, str) or not ident:
            raise ExportError("Conversation without an ID cannot be exported safely")
        unique.setdefault(ident, meta)
    if len(metadata) != len(unique):
        report.notice(f"Export inventory: {len(metadata) - len(unique)} duplicate entries removed")
    root = config.output_dir
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    index_path = safe_output_path(root, ".export-index.json")
    index = decode_json(index_path.read_text()) if index_path.exists() else {}
    if not isinstance(index, dict) or not all(isinstance(entry, dict) for entry in index.values()):
        raise ExportError("Invalid export index; move .export-index.json aside to rebuild")
    saved = skipped = failed = written = 0

    def summary() -> str:
        pending = len(unique) - saved - skipped - failed
        return f"saved={saved}; skipped={skipped}; failed={failed}; pending={pending}; written={human_bytes(written)}"

    with report.stage(
        "Export conversations",
        total=len(unique),
        detail=f"format={config.output_format}; refresh={refresh}",
    ) as work:
        try:
            for position, (ident, meta) in enumerate(unique.items(), 1):
                work.update(detail=f"checking {position}/{len(unique)}; {summary()}")
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
                    work.advance(detail="unchanged; " + summary())
                    continue
                try:
                    work.update(detail=f"fetching {position}/{len(unique)}; {summary()}")
                    detail = fetch_detail(ident)
                    if not isinstance(detail, dict) or not detail:
                        raise ExportError("Conversation detail is empty or not an object")
                    detail_id = detail.get("id") or detail.get("conversation_id")
                    if detail_id and detail_id != ident:
                        raise ExportError("Conversation detail ID does not match its listing")
                    if not detail_id:
                        detail = dict(detail, conversation_id=ident)
                    work.update(detail=f"rendering/writing {position}/{len(unique)}; {summary()}")
                    text = (
                        json.dumps(detail, indent=2, ensure_ascii=False) + "\n"
                        if config.output_format == "json"
                        else conversation_to_markdown({**detail, **meta}, extract_messages(detail))
                    )
                    write_text_file(path, text)
                except HTTPError as exc:
                    if exc.status in {401, 403}:
                        failed += 1
                        work.advance(detail="access stopped; " + summary(), force=True)
                        report.notice(
                            "Authentication/access stopped the export; already saved files and checkpoints are retained"
                        )
                        raise
                    report.error(f"Conversation export failed: {exc}")
                    failed += 1
                    work.advance(detail=summary(), force=True)
                    continue
                except ExportError as exc:
                    report.error(f"Conversation export failed: {exc}")
                    failed += 1
                    work.advance(detail=summary(), force=True)
                    continue
                except (OSError, ValueError, TypeError, KeyError):
                    failed += 1
                    work.advance(detail="render/write failed; " + summary(), force=True)
                    raise
                try:
                    content = path.read_bytes()
                    index[key] = {
                        "path": relative,
                        "update_time": updated,
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
                    write_json_file(index_path, index)
                except (OSError, ValueError, TypeError):
                    failed += 1
                    work.advance(detail="checkpoint failed; " + summary(), force=True)
                    raise
                saved += 1
                written += len(content)
                work.advance(detail=summary())
            work.finish(summary(), state="partial" if failed else "done")
        finally:
            report.notice("Conversation summary: " + summary())
    report.result(
        f"Saved: {saved}; unchanged: {skipped}; failed: {failed}; output: {root.resolve()}"
    )
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


def generate_local_index(directory: Path, *, progress: ProgressReporter | None = None) -> int:
    report = progress if progress is not None else NO_PROGRESS
    result = build_index(directory, progress=report)
    report.result(
        f"Local index: {result.chats} chats; {result.projects} folders; {len(result.warnings)} warnings; {result.ignored} non-chat JSON files ignored"
    )
    report.result(f"Open: {result.path}")
    return int(bool(result.warnings))


def run_command(args: argparse.Namespace, report: ProgressReporter) -> int:
    from dataclasses import replace

    if args.command == "index":
        report.notice("Index mode: existing local files only; no authentication or API calls")
        directory = (
            args.archive
            or args.output
            or output_directory(os.environ.get("CHATGPT_OUTPUT_DIR", "chatgpt_export"))
        )
        return generate_local_index(directory, progress=report)
    config = Config.from_env()
    config = replace(
        config,
        output_format=args.format or config.output_format,
        output_dir=output_directory(str(args.output or config.output_dir)),
        auxiliary=config.auxiliary if args.auxiliary is None else args.auxiliary,
    )
    if args.command != "probe":
        report.notice(
            f"Output: {config.output_dir.resolve()}; format={config.output_format}; HTML={'enabled' if config.build_index and not args.no_index else 'disabled'}"
        )
    if args.command == "import":
        assert args.archive is not None
        with report.stage("Read offline archive", total=1) as work:
            conversations = load_archive(args.archive)
            work.advance(detail=f"{len(conversations)} conversation records loaded")
        by_id = {}
        for item in conversations:
            ident = item.get("id") or item.get("conversation_id")
            if not isinstance(ident, str) or not ident or ident in by_id:
                raise ExportError("Archive has a missing or duplicate conversation ID")
            by_id[ident] = item
        result = export_conversations(
            conversations, by_id.__getitem__, config, refresh=args.refresh, progress=report
        )
    else:
        client = ChatGPTClient(config, progress=report)
        if args.command == "probe":
            report.notice("Probe mode: read-only metadata requests; JSON result stays on stdout")
            result_report = client.probe()
            report.result(json.dumps(result_report, indent=2))
            return int(any("error" in endpoint for endpoint in result_report["endpoints"].values()))
        report.notice(
            f"Export scope: auto_projects={config.auto_projects}; archived={config.include_archived}; auxiliary={config.auxiliary}; request_delay={config.request_delay:.1f}s; timeout={config.timeout:.1f}s"
        )
        token = client.access_token()
        conversations = client.all_conversations(token)
        report.notice(
            f"Discovered {len(conversations)} conversations (including configured/discovered projects)"
        )
        result = export_conversations(
            conversations,
            lambda ident: client.detail(ident, token),
            config,
            refresh=args.refresh,
            progress=report,
        )
        if config.auxiliary:
            export_auxiliary_data(client, token, config.output_dir, progress=report)
        else:
            report.notice(
                "Auxiliary snapshots explicitly disabled; pass --auxiliary or set CHATGPT_AUXILIARY=true to include memories/settings checks"
            )
    if config.build_index and not args.no_index:
        result = max(result, generate_local_index(config.output_dir, progress=report))
    else:
        report.notice(
            "HTML generation disabled; run mise run index later to build it from local files"
        )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", nargs="?", choices=["export", "probe", "import", "index"], default="export"
    )
    parser.add_argument(
        "archive",
        nargs="?",
        type=Path,
        help="Archive file (import) or existing export directory (index)",
    )
    parser.add_argument("--format", choices=["json", "markdown"])
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="Disable live progress/status messages (results and errors remain)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Export output directory; for index, an alternative to the directory argument",
    )
    parser.add_argument(
        "--no-index",
        action="store_true",
        help="Do not build HTML after export/import (CHATGPT_BUILD_INDEX=false also disables this)",
    )
    parser.add_argument(
        "--refresh", action="store_true", help="Refetch even if metadata is unchanged"
    )
    parser.add_argument(
        "--auxiliary",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Snapshot memories/settings with web exports (enabled by default; --no-auxiliary disables it; overrides CHATGPT_AUXILIARY)",
    )
    args = parser.parse_args(argv)
    if args.command == "import" and args.archive is None:
        parser.error("import requires an archive path")
    if args.command not in {"import", "index"} and args.archive is not None:
        parser.error("a source path is valid only for import or index")
    if args.command == "index" and args.archive is not None and args.output is not None:
        parser.error("index accepts a directory argument or --output, not both")
    report = None
    started = time.monotonic()
    try:
        enabled = not args.no_progress and boolean(os.environ, "CHATGPT_PROGRESS", True)
        with ProgressReporter(enabled=enabled) as report:
            report.notice(
                f"Starting {args.command}; progress on stderr; titles, IDs, credentials and response bodies are not logged"
            )
            with report.stage(f"Run {args.command}", total=1) as work:
                result = run_command(args, report)
                work.advance(detail=f"exit status {result}")
                work.finish(
                    state="partial" if result else "warnings" if report.warnings else "done"
                )
            report.notice(
                f"Run finished: exit status {result}; warnings={report.warnings}; elapsed {time.monotonic() - started:.1f}s"
            )
            return result
    except KeyboardInterrupt:
        if report is not None:
            report.notice(
                f"Interrupted after {time.monotonic() - started:.1f}s; existing saved files/checkpoints retained"
            )
        print(
            "Interrupted: saved files and checkpoints are retained; rerun to continue.",
            file=sys.stderr,
        )
        return 130
    except (ConfigError, ExportError) as exc:
        if report is not None:
            report.notice(f"Run stopped after {time.monotonic() - started:.1f}s")
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError, zipfile.BadZipFile, TypeError, KeyError):
        if report is not None:
            report.notice(f"Run failed after {time.monotonic() - started:.1f}s")
        print(
            "Error: unable to read/write an archive or received an invalid data shape (details withheld)",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
