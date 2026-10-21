"""Private, offline ZIP backups. Source directories and input archives are never removed."""

import os
import shutil
import stat
import tempfile
import unicodedata
import zipfile
from pathlib import Path, PurePosixPath

from chatgpt_client import ExportError
from progress_report import NO_PROGRESS, ProgressReporter

CHUNK_SIZE = 1024 * 1024
MAX_MEMBERS = 100_000
DEFAULT_MAX_UNPACKED = 64 * 1024**3


def _member_path(info: zipfile.ZipInfo) -> PurePosixPath:
    name = info.filename
    parts = name.rstrip("/").split("/")
    reserved = {"con", "prn", "aux", "nul"} | {
        f"{prefix}{i}" for prefix in ("com", "lpt") for i in range(1, 10)
    }
    mode = stat.S_IFMT(info.external_attr >> 16)
    if (
        info.orig_filename != name
        or name.startswith("/")
        or "\\" in name
        or any(
            part in {"", ".", ".."}
            or ":" in part
            or any(ord(c) < 32 for c in part)
            or part != part.rstrip(" .")
            or part.split(".")[0].casefold() in reserved
            for part in parts
        )
        or mode not in {0, stat.S_IFREG, stat.S_IFDIR}
        or info.flag_bits & 1
        or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
        or (mode == stat.S_IFDIR and not info.is_dir())
    ):
        raise ExportError(
            "ZIP contains an unsafe, encrypted, or unsupported entry; nothing was restored"
        )
    return PurePosixPath(*parts)


def pack_directory(
    source: Path, target: Path, *, overwrite: bool = False, progress: ProgressReporter = NO_PROGRESS
) -> int:
    source = source.expanduser()
    target = target.expanduser()
    if source.is_symlink() or not source.is_dir():
        raise ExportError("ZIP source must be an existing directory, not a symbolic link")
    source = source.resolve()
    if target.is_symlink() or target.resolve().is_relative_to(source):
        raise ExportError(
            "ZIP destination must be outside the source directory and not a symbolic link"
        )
    if target.exists() and (not overwrite or not target.is_file()):
        raise ExportError(
            "ZIP destination already exists; choose another --output or use --overwrite"
        )
    entries = []

    def fail_walk(error: OSError) -> None:
        raise error

    for base, dirs, files in os.walk(source, followlinks=False, onerror=fail_walk):
        for name in sorted(dirs + files):
            path = Path(base) / name
            if path.is_symlink() or not (path.is_file() or path.is_dir()):
                raise ExportError(
                    "ZIP source contains symbolic links or special files; source retained"
                )
            if (
                name in {".git", ".env", "mise.local.toml", ".ssh", ".aws"}
                or name.startswith(".env.")
                or (name.startswith("mise.") and name.endswith(".local.toml"))
            ):
                raise ExportError(
                    "ZIP source contains credentials or a Git checkout; choose the export directory"
                )
            entries.append(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".backup-", suffix=".zip", dir=target.parent)
    os.close(fd)
    try:
        with (
            zipfile.ZipFile(
                temporary, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True
            ) as archive,
            progress.stage("Create ZIP backup", total=len(entries)) as work,
        ):
            for path in entries:
                relative = path.relative_to(source).as_posix()
                if path.is_dir():
                    info = zipfile.ZipInfo(relative + "/")
                    info.external_attr = (stat.S_IFDIR | 0o700) << 16
                    archive.writestr(info, b"")
                else:
                    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                    with os.fdopen(descriptor, "rb") as stream:
                        before = os.fstat(stream.fileno())
                        if not stat.S_ISREG(before.st_mode):
                            raise ExportError(
                                "ZIP source changed during backup; retry with no concurrent writers"
                            )
                        info = zipfile.ZipInfo(relative)
                        info.external_attr = (stat.S_IFREG | 0o600) << 16
                        info.compress_type = zipfile.ZIP_DEFLATED
                        with (
                            archive.open(info, "w", force_zip64=True) as output,
                            progress.stage(
                                "Compress file", total=before.st_size, unit="bytes", transient=True
                            ) as transfer,
                        ):
                            while chunk := stream.read(CHUNK_SIZE):
                                output.write(chunk)
                                transfer.advance(len(chunk))
                        after = os.fstat(stream.fileno())
                        if (before.st_size, before.st_mtime_ns) != (
                            after.st_size,
                            after.st_mtime_ns,
                        ):
                            raise ExportError(
                                "ZIP source changed during backup; retry with no concurrent writers"
                            )
                work.advance()
        with open(temporary, "rb") as stream:
            os.fsync(stream.fileno())
        if overwrite:
            os.replace(temporary, target)
        else:
            # Reserve exclusively before atomic replacement; works without hard-link support.
            reservation = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(reservation)
            try:
                os.replace(temporary, target)
            except BaseException:
                target.unlink(missing_ok=True)
                raise
        return len(entries)
    finally:
        Path(temporary).unlink(missing_ok=True)


def unpack_directory(
    source: Path,
    target: Path,
    *,
    max_bytes: int = DEFAULT_MAX_UNPACKED,
    progress: ProgressReporter = NO_PROGRESS,
) -> int:
    source = source.expanduser()
    target = target.expanduser()
    if not source.is_file() or source.is_symlink():
        raise ExportError("Restore source must be an existing ZIP file, not a symbolic link")
    if target.exists() or target.is_symlink():
        raise ExportError("Restore destination already exists; choose a new --output directory")
    if max_bytes <= 0:
        raise ExportError("Restore size limit must be positive")
    with zipfile.ZipFile(source) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_MEMBERS or sum(entry.file_size for entry in entries) > max_bytes:
            raise ExportError("ZIP exceeds the restore entry/size limit; nothing was restored")
        names = {}
        for entry in entries:
            member = _member_path(entry)
            key = unicodedata.normalize("NFC", member.as_posix()).casefold()
            if key in names:
                raise ExportError("ZIP has duplicate or case-colliding paths; nothing was restored")
            names[key] = entry.is_dir()
        for name in names:
            if any(
                names.get(parent.as_posix()) is False
                for parent in PurePosixPath(name).parents
                if parent.as_posix() != "."
            ):
                raise ExportError("ZIP has conflicting file/directory paths; nothing was restored")
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".restore-", dir=target.parent))
        reserved = False
        succeeded = False
        received = 0
        try:
            with progress.stage("Restore ZIP backup", total=len(entries)) as work:
                for entry in entries:
                    path = staging / _member_path(entry)
                    if entry.is_dir():
                        path.mkdir(parents=True, exist_ok=True, mode=0o700)
                    else:
                        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                        with (
                            archive.open(entry) as stream,
                            path.open("xb") as output,
                            progress.stage(
                                "Restore file", total=entry.file_size, unit="bytes", transient=True
                            ) as transfer,
                        ):
                            os.chmod(path, 0o600)
                            while chunk := stream.read(CHUNK_SIZE):
                                received += len(chunk)
                                if received > max_bytes:
                                    raise ExportError("ZIP exceeds the restore size limit")
                                output.write(chunk)
                                transfer.advance(len(chunk))
                            output.flush()
                            os.fsync(output.fileno())
                    work.advance()
            # Reserve exclusively. Never merge into an existing archive or overwrite files.
            target.mkdir(mode=0o700)
            reserved = True
            for path in staging.iterdir():
                os.rename(path, target / path.name)
            succeeded = True
            return len(entries)
        finally:
            shutil.rmtree(staging)
            if reserved and not succeeded:
                shutil.rmtree(target)
