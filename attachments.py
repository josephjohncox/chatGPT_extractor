"""Attachment snapshots, resumable manifests, Canvas extraction, and local backfill."""

import hashlib
import json
import mimetypes
import os
import re
import stat
import zipfile
from dataclasses import fields, replace
from pathlib import Path, PurePosixPath
from types import TracebackType
from typing import Any

from attachment_refs import ASSET_DIR, AttachmentRef, discover_references, managed_path
from attachment_transport import (
    AttachmentHTTP,
    LibraryCookieError,
    checked_download_url,
    save_chunks,
)
from canvas_artifacts import canvas_artifacts
from chatgpt_client import ChatGPTClient, ExportError, HTTPError
from conversation_format import sanitize_filename
from export_io import decode_json, write_json_file, write_text_file
from progress_report import NO_PROGRESS, ProgressReporter

OWNER = "chatgpt-extractor attachments v1"


def checksum(path: Path, progress: ProgressReporter = NO_PROGRESS) -> str:
    digest = hashlib.sha256()
    with (
        path.open("rb") as source,
        progress.stage(
            "Verify cached attachment", total=path.stat().st_size, transient=True
        ) as work,
    ):
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            work.advance(len(chunk))
    return digest.hexdigest()


def asset_name(value: str, fallback: str = "attachment.bin") -> str:
    # Resolver filenames may be storage paths. Do not retain directory/query components.
    name = value.replace("\\", "/").rsplit("/", 1)[-1].split("?", 1)[0].split("#", 1)[0]
    return sanitize_filename(name or fallback)


class LocalAssets:
    """Read bundled files in place; no ZIP extraction or arbitrary message paths."""

    def __init__(self, source: Path):
        self.source = source
        self.zip = zipfile.ZipFile(source) if zipfile.is_zipfile(source) else None
        self.entries: list[str] = []
        if self.zip:
            for info in self.zip.infolist():
                zipped_path = PurePosixPath(info.filename)
                if (
                    info.is_dir()
                    or bool(info.flag_bits & 1)
                    or zipped_path.is_absolute()
                    or ".." in zipped_path.parts
                    or "\\" in info.filename
                    or stat.S_ISLNK(info.external_attr >> 16)
                ):
                    continue
                self.entries.append(info.filename)
        else:
            root = source.parent.resolve()
            for directory in ("files", "assets", "attachments"):
                folder = root / directory
                if folder.is_symlink() or not folder.is_dir():
                    continue
                for base, dirs, files in os.walk(folder, followlinks=False):
                    dirs[:] = [name for name in dirs if not (Path(base) / name).is_symlink()]
                    for name in files:
                        path = Path(base) / name
                        if not path.is_symlink() and path.resolve().is_relative_to(root):
                            self.entries.append(str(path.relative_to(root)))

    def find(self, ref: AttachmentRef) -> str | None:
        if ref.kind == "file":
            pattern = re.compile(r"(?:^|/)" + re.escape(ref.locator) + r"(?:[._/-]|$)")
            matches = [name for name in self.entries if pattern.search(name)]
        elif ref.kind == "sandbox" and self.zip:
            # Only an explicitly supplied ZIP may use the unique sandbox basename fallback.
            matches = [
                name
                for name in self.entries
                if PurePosixPath(name).name == PurePosixPath(ref.locator).name
            ]
        else:
            matches = []
        return matches[0] if len(matches) == 1 else None

    def copy(self, name: str, target: Path, *, limit: int, progress: ProgressReporter):
        if self.zip:
            size = self.zip.getinfo(name).file_size
            with self.zip.open(name) as stream:
                return save_chunks(
                    iter(lambda: stream.read(256 * 1024), b""),
                    target,
                    total=size,
                    limit=limit,
                    progress=progress,
                )
        path = self.source.parent.resolve() / name
        if path.is_symlink() or not path.resolve().is_relative_to(self.source.parent.resolve()):
            raise ExportError("Unsafe local attachment source")
        with path.open("rb") as stream:
            return save_chunks(
                iter(lambda: stream.read(256 * 1024), b""),
                target,
                total=path.stat().st_size,
                limit=limit,
                progress=progress,
            )

    def close(self) -> None:
        if self.zip:
            self.zip.close()


class AttachmentManager:
    def __init__(
        self,
        root: Path,
        *,
        client: ChatGPTClient | None = None,
        access_token: str | None = None,
        offline_source: Path | None = None,
        max_bytes: int = 512 * 1024 * 1024,
        refresh: bool = False,
        progress: ProgressReporter | None = None,
    ):
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.client = client
        self.progress = progress if progress is not None else NO_PROGRESS
        self.limit = max_bytes
        self.refresh = refresh
        self.local = LocalAssets(offline_source) if offline_source else None
        self._token: str | None = access_token
        self.http = AttachmentHTTP(client) if client is not None else None
        directory = managed_path(self.root, ASSET_DIR)
        marker = managed_path(self.root, ASSET_DIR + "/.managed")
        if directory.exists() and (not marker.is_file() or marker.read_text() != OWNER):
            raise ExportError("Existing attachment directory is not owned by this exporter")
        directory.mkdir(mode=0o700, exist_ok=True)
        if not marker.exists():
            write_text_file(marker, OWNER)
        self.path = managed_path(self.root, ASSET_DIR + "/manifest.json")
        self.data: dict[str, Any] = (
            decode_json(self.path.read_text())
            if self.path.exists()
            else {"version": 1, "assets": {}, "conversations": {}}
        )
        if (
            not isinstance(self.data, dict)
            or self.data.get("version") != 1
            or not isinstance(self.data.get("assets"), dict)
            or not isinstance(self.data.get("conversations"), dict)
            or not all(
                isinstance(v, dict)
                and isinstance(v.get("reference"), dict)
                and isinstance(v.get("source_paths", []), list)
                and all(isinstance(path, str) for path in v.get("source_paths", []))
                for v in self.data["assets"].values()
            )
        ):
            raise ExportError("Invalid attachment manifest; no files were downloaded")
        self.seen: set[str] = set()
        self.saved = self.skipped = self.unavailable = self.failed = self.partial = (
            self.unsupported
        ) = 0
        self.interrupted = 0

    def __enter__(self) -> "AttachmentManager":
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if self.local:
            self.local.close()

    def checkpoint(self) -> None:
        write_json_file(self.path, self.data)

    def register(self, refs: list[AttachmentRef]) -> None:
        for ref in refs:
            self.seen.add(ref.key)
            self.data["assets"].setdefault(
                ref.key, {"reference": ref.record(), "outcome": "not_attempted"}
            )
        self.checkpoint()

    def token(self) -> str:
        if self.client is None:
            raise ExportError("Attachment is not bundled with this offline archive")
        if self._token is None:
            self._token = self.client.access_token()
        return self._token

    def valid(self, entry: dict) -> bool:
        if entry.get("outcome") != "saved" or not isinstance(entry.get("path"), str):
            return False
        try:
            path = managed_path(self.root, entry["path"])
            valid = (
                path.is_file()
                and path.stat().st_size == entry.get("bytes")
                and checksum(path, self.progress) == entry.get("sha256")
            )
            if valid and (entry.get("reference") or {}).get("kind") == "canvas":
                history = entry.get("history")
                if not isinstance(history, str):
                    return False
                history_path = managed_path(self.root, history)
                valid = (
                    history_path.is_file()
                    and history_path.stat().st_size == entry.get("history_bytes")
                    and checksum(history_path, self.progress) == entry.get("history_sha256")
                )
            return valid
        except (OSError, ExportError):
            return False

    def conversation_complete(self, ident: str, updated: Any) -> bool:
        source = self.data["conversations"].get(ident)
        return (
            not self.refresh
            and isinstance(source, dict)
            and source.get("update_time") == updated
            and source.get("complete") is True
            and all(
                self.valid(self.data["assets"].get(key, {})) for key in source.get("references", [])
            )
        )

    def _entry(self, ref: AttachmentRef) -> dict:
        old = self.data["assets"].get(ref.key, {})
        entry = {**old, "reference": ref.record(), "outcome": "in_progress"}
        self.data["assets"][ref.key] = entry
        self.seen.add(ref.key)
        self.checkpoint()
        return entry

    def _binary(self, ref: AttachmentRef) -> None:
        previous = self.data["assets"].get(ref.key, {})
        self.seen.add(ref.key)
        if not self.refresh and self.valid(previous):
            self.skipped += 1
            return
        entry = self._entry(ref)
        try:
            if ref.kind == "unsupported":
                entry.update(outcome="unsupported", error=ref.reason)
                self.unsupported += 1
                return
            local = self.local
            candidate = local.find(ref) if local is not None else None
            if candidate and local is not None:
                name = asset_name(ref.name or candidate)
                relative = f"{ASSET_DIR}/files/{ref.key}--{name}"
                result = local.copy(
                    candidate,
                    managed_path(self.root, relative),
                    limit=self.limit,
                    progress=self.progress,
                )
            elif self.client is not None and self.http is not None:
                from urllib.parse import quote

                if ref.kind == "file":
                    params = {"inline": "false"}
                    if ref.conversation_id:
                        params["conversation_id"] = ref.conversation_id
                    info = self.client.json(
                        f"files/download/{quote(ref.locator, safe='')}",
                        self.token(),
                        params=params,
                        label="attachment file resolver",
                    )
                else:
                    info = self.client.json(
                        f"conversation/{quote(ref.conversation_id, safe='')}/interpreter/download",
                        self.token(),
                        params={"message_id": ref.message_id, "sandbox_path": ref.locator},
                        label="attachment sandbox resolver",
                    )
                if info.get("status") == "error":
                    raise ExportError("Attachment resolver reported an error; details withheld")
                url = info.get("download_url") or info.get("url")
                if not isinstance(url, str) or not url:
                    raise ExportError("Attachment resolver did not return a download URL")
                checked_download_url(url)
                name = asset_name(
                    ref.name or str(info.get("file_name") or ""),
                    ref.locator if ref.kind == "file" else "artifact.bin",
                )
                if not Path(name).suffix:
                    mime = str(info.get("mime_type") or info.get("mimedata") or ref.mime)
                    name += mimetypes.guess_extension(mime) or ".bin"
                relative = f"{ASSET_DIR}/files/{ref.key}--{name}"
                result = self.http.download(
                    url, managed_path(self.root, relative), self.token(), max_bytes=self.limit
                )
            else:
                entry.update(outcome="unavailable", error="not_bundled_offline")
                self.unavailable += 1
                return
            entry.update(
                outcome="saved",
                path=relative,
                name=name,
                bytes=result.size,
                sha256=result.sha256,
                mime=result.mime,
            )
            entry.pop("error", None)
            self.saved += 1
        except HTTPError as exc:
            fatal = exc.status == 401 or exc.browser_challenge
            unavailable = exc.status in {403, 404, 405, 410} and not fatal
            entry.update(
                outcome="unavailable" if unavailable else "failed",
                status=exc.status,
                error="library_session_cookie_required_or_rejected"
                if isinstance(exc, LibraryCookieError)
                else "browser_challenge"
                if exc.browser_challenge
                else "http_error",
            )
            if unavailable:
                self.unavailable += 1
            else:
                self.failed += 1
            if fatal:
                raise
        except KeyboardInterrupt:
            entry.update(outcome="interrupted", error="interrupted")
            self.interrupted += 1
            raise
        except (ExportError, OSError, ValueError, zipfile.BadZipFile, NotImplementedError):
            entry.update(outcome="failed", error="attachment_transfer_failed_details_withheld")
            self.failed += 1
        finally:
            self.checkpoint()

    def process(self, detail: dict, meta: dict | None = None) -> None:
        meta = meta or detail
        cid = str(detail.get("id") or detail.get("conversation_id") or meta.get("id") or "")
        has_id = bool(cid)
        if not cid:
            identity = str(meta.get("_attachment_source") or json.dumps(detail, sort_keys=True))
            cid = "local-" + hashlib.sha256(identity.encode()).hexdigest()
        detail = dict(detail, id=cid)
        project = meta.get("_project_id") or detail.get("gizmo_id") or ""
        project = project if isinstance(project, str) and project.startswith("g-p-") else ""
        try:
            refs = discover_references(detail, project_id=project)
            if not has_id:
                refs = [
                    AttachmentRef(
                        "unsupported", cid, ref.key, name=ref.name, reason="missing_conversation_id"
                    )
                    for ref in refs
                ]
            canvases = canvas_artifacts(detail)
        except (ExportError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
            refs = [
                AttachmentRef(
                    "unsupported",
                    cid,
                    "malformed-asset-metadata",
                    reason="malformed_asset_metadata",
                )
            ]
            canvases = []
        canvas_refs = [replace(artifact.ref, project_id=project) for artifact in canvases]
        previous_canvases = {
            ref.key: dict(self.data["assets"].get(ref.key, {})) for ref in canvas_refs
        }
        for ref in canvas_refs:
            if ref.key in self.data["assets"]:
                self.data["assets"][ref.key]["outcome"] = "not_attempted"
        all_refs = refs + canvas_refs
        keys = [ref.key for ref in all_refs]
        self.register(all_refs)
        source_path = meta.get("_attachment_source")
        if isinstance(source_path, str):
            for key in keys:
                paths = self.data["assets"][key].setdefault("source_paths", [])
                if source_path not in paths:
                    paths.append(source_path)
        self.data["conversations"][cid] = {
            "update_time": meta.get("update_time"),
            "references": keys,
            "complete": False,
        }
        self.checkpoint()

        with self.progress.stage(
            "Conversation attachments", total=len(refs) + len(canvases), transient=True
        ) as work:
            for ref in refs:
                try:
                    self._binary(ref)
                finally:
                    work.advance(detail=self.summary())
            for artifact in canvases:
                ref = replace(artifact.ref, project_id=project)
                self.seen.add(ref.key)
                content = artifact.content.encode("utf-8")
                history_content = (
                    json.dumps(artifact.history, ensure_ascii=False, sort_keys=True, indent=2)
                    + "\n"
                ).encode("utf-8")
                previous = previous_canvases.get(ref.key, {})
                if (
                    not self.refresh
                    and artifact.complete
                    and self.valid(previous)
                    and previous.get("sha256") == hashlib.sha256(content).hexdigest()
                    and previous.get("history_sha256")
                    == hashlib.sha256(history_content).hexdigest()
                ):
                    self.data["assets"][ref.key]["outcome"] = "saved"
                    self.skipped += 1
                    work.advance(detail=self.summary())
                    continue
                entry = self._entry(ref)
                name = asset_name(ref.name, "canvas.txt")
                relative = f"{ASSET_DIR}/canvas/{ref.key}--{'partial--' if not artifact.complete else ''}{name}"
                history = f"{ASSET_DIR}/canvas/{ref.key}--history.json"
                try:
                    history_result = save_chunks(
                        [history_content],
                        managed_path(self.root, history),
                        total=len(history_content),
                        limit=self.limit,
                        progress=self.progress,
                        mime="application/json",
                    )
                    if not artifact.complete and artifact.reason in {
                        "unresolved_canvas_operation",
                        "broken_selected_branch",
                    }:
                        for field in ("path", "bytes", "sha256"):
                            entry.pop(field, None)
                        entry.update(
                            outcome="partial",
                            history=history,
                            history_bytes=history_result.size,
                            history_sha256=history_result.sha256,
                            name="partial--" + name,
                            mime="application/json",
                            reconstruction=artifact.reason,
                        )
                        self.partial += 1
                        self.checkpoint()
                        work.advance(detail=self.summary())
                        continue
                    result = save_chunks(
                        [content],
                        managed_path(self.root, relative),
                        total=len(content),
                        limit=self.limit,
                        progress=self.progress,
                        mime="text/plain",
                    )
                    entry.update(
                        outcome="saved" if artifact.complete else "partial",
                        path=relative,
                        history=history,
                        history_bytes=history_result.size,
                        history_sha256=history_result.sha256,
                        name=name if artifact.complete else "partial--" + name,
                        bytes=result.size,
                        sha256=result.sha256,
                        mime=result.mime,
                        reconstruction="selected_branch" if artifact.complete else artifact.reason,
                    )
                    entry.pop("error", None)
                    if artifact.complete:
                        self.saved += 1
                    else:
                        self.partial += 1
                except KeyboardInterrupt:
                    entry.update(outcome="interrupted", error="interrupted")
                    self.interrupted += 1
                    self.checkpoint()
                    work.advance(detail=self.summary())
                    raise
                except (ExportError, OSError):
                    entry.update(outcome="failed", error="canvas_materialization_failed")
                    self.failed += 1
                self.checkpoint()
                work.advance(detail=self.summary())
        self.data["conversations"][cid]["complete"] = all(
            self.data["assets"][key].get("outcome") == "saved" for key in keys
        )
        self.checkpoint()

    def projects(self, records: dict[str, list[dict]]) -> None:
        refs = []
        from attachment_refs import FILE_ID

        for project, files in records.items():
            for item in files:
                ident = item.get("file_id") or item.get("id")
                if isinstance(ident, str) and FILE_ID.fullmatch(ident):
                    refs.append(
                        AttachmentRef(
                            "file",
                            "",
                            ident,
                            name=str(item.get("name") or ""),
                            mime=str(item.get("type") or ""),
                            project_id=project,
                        )
                    )
                else:
                    refs.append(
                        AttachmentRef(
                            "unsupported",
                            "",
                            hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest(),
                            project_id=project,
                            reason="unrecognized_project_file_id",
                        )
                    )
        self.register(refs)
        with self.progress.stage("Project attachments", total=len(refs)) as work:
            for ref in refs:
                self._binary(ref)
                work.advance(detail=self.summary())

    def summary(self) -> str:
        return f"saved={self.saved}; skipped={self.skipped}; unavailable={self.unavailable}; failed={self.failed}; partial={self.partial}; unsupported={self.unsupported}; interrupted={self.interrupted}"

    def finish(self) -> int:
        self.checkpoint()
        self.progress.notice("Attachment summary: " + self.summary())
        missing = sum(self.data["assets"][key].get("outcome") != "saved" for key in self.seen)
        if missing:
            self.progress.warning(
                f"{missing} attachment references are incomplete; inspect the private attachment manifest"
            )
        return int(bool(missing))

    def resume_unseen(self) -> None:
        from attachment_refs import FILE_ID, sandbox_path

        for key, entry in list(self.data["assets"].items()):
            if key in self.seen:
                continue
            raw = entry.get("reference")
            if not isinstance(raw, dict) or not all(
                isinstance(value, str) for value in raw.values()
            ):
                entry.update(outcome="unsupported", error="invalid_manifest_reference")
                self.seen.add(key)
                self.unsupported += 1
                continue
            try:
                ref = AttachmentRef(
                    **{
                        field.name: raw[field.name]
                        for field in fields(AttachmentRef)
                        if field.name in raw
                    }
                )
            except TypeError:
                entry.update(outcome="unsupported", error="invalid_manifest_reference")
                self.seen.add(key)
                self.unsupported += 1
                continue
            safe = ref.key == key and (
                (ref.kind == "file" and FILE_ID.fullmatch(ref.locator))
                or (
                    ref.kind == "sandbox"
                    and sandbox_path(ref.locator) == ref.locator
                    and ref.message_id
                    and ref.conversation_id
                )
                or ref.kind in {"canvas", "unsupported"}
            )
            if not safe:
                entry.update(outcome="unsupported", error="invalid_manifest_reference")
                self.seen.add(key)
                self.unsupported += 1
            elif ref.kind == "canvas":
                self.seen.add(key)
                if self.valid(entry):
                    self.skipped += 1
                else:
                    entry.update(
                        outcome="unavailable", error="raw_json_required_for_canvas_rebuild"
                    )
                    self.unavailable += 1
            else:
                self._binary(ref)
        for source in self.data["conversations"].values():
            if isinstance(source, dict) and isinstance(source.get("references"), list):
                source["complete"] = all(
                    isinstance(key, str)
                    and self.data["assets"].get(key, {}).get("outcome") == "saved"
                    for key in source["references"]
                )
        self.checkpoint()

    def backfill(self) -> int:
        from local_index import read_json, scan_directory

        chats, _, warnings = scan_directory(self.root, progress=self.progress)
        with self.progress.stage("Backfill attachments from local JSON", total=len(chats)) as work:
            for chat in chats:
                related = any(
                    set(entry.get("source_paths", []))
                    & {path.relative_to(self.root).as_posix() for path in chat.markdown}
                    for entry in self.data["assets"].values()
                )
                if not chat.raw and not related:
                    self.progress.warning(
                        "A Markdown-only conversation has no raw mapping for attachment recovery"
                    )
                    warnings.append("markdown_only")
                for source in sorted(chat.raw, key=lambda item: item.updated or 0):
                    detail = read_json(source.path)
                    if source.position is not None:
                        detail = (
                            detail[source.position]
                            if isinstance(detail, list)
                            else detail["conversations"][source.position]
                        )
                    self.process(
                        detail,
                        {
                            **detail,
                            "_attachment_source": source.path.relative_to(self.root).as_posix(),
                        },
                    )
                work.advance(detail=self.summary())
        self.resume_unseen()
        read_json.cache_clear()
        return max(self.finish(), int(bool(warnings)))
