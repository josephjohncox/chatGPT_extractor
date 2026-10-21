"""Recognize ChatGPT-owned attachment references without following arbitrary message URLs."""

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

ASSET_DIR = ".chatgpt-assets"


def managed_path(root: Path, relative: str) -> Path:
    from chatgpt_client import ExportError

    parts = PurePosixPath(relative).parts
    if (
        not parts
        or PurePosixPath(relative).is_absolute()
        or parts[0] != ASSET_DIR
        or ".." in parts
        or "\\" in relative
    ):
        raise ExportError("Invalid local attachment path")
    path = root
    for part in parts:
        path = path / part
        if path.is_symlink():
            raise ExportError("Attachment paths must not follow symlinks")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ExportError("Attachment path escapes the output directory")
    return path


FILE_ID = re.compile(r"file[-_][A-Za-z0-9_-]{1,240}\Z")
SANDBOX = re.compile(r"sandbox:(/mnt/data/[^\s<>\]\)\"'`]+)")


@dataclass(frozen=True)
class AttachmentRef:
    kind: str
    conversation_id: str
    locator: str
    message_id: str = ""
    name: str = ""
    mime: str = ""
    project_id: str = ""
    reason: str = ""

    @property
    def key(self) -> str:
        # A sandbox path can represent different versions in different messages.
        message = self.message_id if self.kind == "sandbox" else ""
        return hashlib.sha256(
            json.dumps(
                [
                    self.kind,
                    self.conversation_id,
                    self.project_id if not self.conversation_id else "",
                    message,
                    self.locator,
                ]
            ).encode()
        ).hexdigest()

    def record(self) -> dict[str, str]:
        return asdict(self)


def sandbox_path(value: str) -> str | None:
    path = unquote(value)
    if (
        not path.startswith("/mnt/data/")
        or path.endswith("/")
        or any(ord(c) < 32 for c in path)
        or "\\" in path
    ):
        return None
    if any(part in {".", ".."} for part in path.split("/")) or re.search(r"%[0-9a-fA-F]{2}", path):
        return None
    return path if PurePosixPath(path).name else None


def discover_references(detail: dict, *, project_id: str = "") -> list[AttachmentRef]:
    cid = detail.get("id") or detail.get("conversation_id")
    if not isinstance(cid, str) or not cid:
        raise ValueError("Attachment discovery needs a conversation ID")
    refs: dict[str, AttachmentRef] = {}

    def add(
        kind: str, locator: str, mid: str, name: str = "", mime: str = "", reason: str = ""
    ) -> None:
        ref = AttachmentRef(kind, cid, locator, mid, name, mime, project_id, reason)
        previous = refs.get(ref.key)
        if previous is None or (not previous.name and name):
            refs[ref.key] = ref

    def pointer(value: str, mid: str) -> None:
        if value.startswith(("sediment://", "file-service://")):
            parsed = urlsplit(value)
            # Rendered sediment references can put the canonical file ID before a second #.
            candidates = [parsed.netloc, parsed.fragment.split("#", 1)[0]]
            ident = next(
                (candidate for candidate in candidates if FILE_ID.fullmatch(candidate)), None
            )
            if ident is not None:
                add("file", ident, mid)
            else:
                add(
                    "unsupported",
                    hashlib.sha256(value.encode()).hexdigest(),
                    mid,
                    reason="unrecognized_asset_pointer",
                )

    def walk(value: Any, mid: str) -> None:
        if isinstance(value, dict):
            ident = value.get("file_id")
            if isinstance(ident, str) and FILE_ID.fullmatch(ident):
                add(
                    "file",
                    ident,
                    mid,
                    str(value.get("name") or ""),
                    str(value.get("mime_type") or ""),
                )
            elif isinstance(ident, str) and ident:
                add(
                    "unsupported",
                    hashlib.sha256(ident.encode()).hexdigest(),
                    mid,
                    reason="unrecognized_file_id",
                )
            for key, item in value.items():
                if (
                    key in {"asset_pointer", "audio_asset_pointer", "video_container_asset_pointer"}
                    and isinstance(item, str)
                    and item
                    and not item.startswith(("sediment://", "file-service://"))
                ):
                    add(
                        "unsupported",
                        hashlib.sha256(item.encode()).hexdigest(),
                        mid,
                        reason="unsupported_asset_pointer_scheme",
                    )
                if key == "attachments" and isinstance(item, list):
                    for attachment in item:
                        if not isinstance(attachment, dict):
                            continue
                        ident = attachment.get("file_id") or attachment.get("id")
                        if isinstance(ident, str) and FILE_ID.fullmatch(ident):
                            add(
                                "file",
                                ident,
                                mid,
                                str(attachment.get("name") or ""),
                                str(attachment.get("mime_type") or ""),
                            )
                        else:
                            add(
                                "unsupported",
                                hashlib.sha256(
                                    json.dumps(attachment, sort_keys=True).encode()
                                ).hexdigest(),
                                mid,
                                reason="unrecognized_attachment_id",
                            )
                if key == "file_ids" and isinstance(item, list):
                    for ident in item:
                        if isinstance(ident, str) and FILE_ID.fullmatch(ident):
                            add("file", ident, mid)
                        elif ident:
                            add(
                                "unsupported",
                                hashlib.sha256(str(ident).encode()).hexdigest(),
                                mid,
                                reason="unrecognized_file_id",
                            )
                walk(item, mid)
        elif isinstance(value, list):
            for item in value:
                walk(item, mid)
        elif isinstance(value, str):
            pointer(value, mid)
            for match in SANDBOX.finditer(value):
                path = sandbox_path(urlsplit("sandbox:" + match[1]).path)
                if path and mid:
                    add("sandbox", path, mid, PurePosixPath(path).name)
                else:
                    add(
                        "unsupported",
                        hashlib.sha256(match[0].encode()).hexdigest(),
                        mid,
                        reason="unsafe_or_unscoped_sandbox_reference",
                    )

    mapping = detail.get("mapping") or {}
    for node_id, node in mapping.items():
        if not isinstance(node, dict):
            continue
        message = node.get("message") or {}
        mid = message.get("id") or node_id
        if not isinstance(mid, str):
            mid = ""
        walk(message.get("content") or {}, mid)
        walk(message.get("metadata") or {}, mid)
    return list(refs.values())
