"""Atomic private output and credential-safe JSON decoding."""

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from chatgpt_client import ExportError


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
