"""Validated artifact preservation and chat links; never execute or index archived files."""

import hashlib
import html
import json
import re
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote


@dataclass
class AssetFile:
    key: str
    path: Path
    name: str
    size: int
    sha256: str
    metadata: dict


@dataclass
class ArtifactPlan:
    assets: dict[str, dict] = field(default_factory=dict)
    files: list[AssetFile] = field(default_factory=list)
    conversations: dict[str, list[str]] = field(default_factory=dict)
    gaps: dict[str, int] = field(default_factory=dict)
    notes: dict[str, str] = field(default_factory=dict)


def owned_path(root: Path, relative: str) -> Path:
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts or rel.parts[0] != ".chatgpt-assets":
        raise ValueError("Artifact path must stay inside .chatgpt-assets")
    current = root
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Artifact paths cannot contain symlinks")
    if not current.resolve().is_relative_to(root.resolve()):
        raise ValueError("Artifact path escapes export")
    return current


def verify_file(item: AssetFile) -> None:
    if item.path.is_symlink() or not item.path.is_file() or item.path.stat().st_size != item.size:
        raise ValueError(f"Missing or changed artifact: {item.key}")
    with item.path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != item.sha256:
        raise ValueError(f"Artifact checksum mismatch: {item.key}")


def load_artifacts(root: Path, *, canvas_notes: bool = False) -> ArtifactPlan:
    if not root.is_dir():
        raise ValueError("Artifact import requires an export directory")
    manifest = owned_path(root, ".chatgpt-assets/manifest.json")
    # Older exports may have no attachment inventory. Report that, not a false success.
    if not manifest.exists():
        return ArtifactPlan(gaps={"manifest_missing": 1})
    marker = owned_path(root, ".chatgpt-assets/.managed")
    if marker.read_text() != "chatgpt-extractor attachments v1":
        raise ValueError("Unowned artifact manifest")
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("Cannot read artifact manifest") from exc
    if (
        not isinstance(data, dict)
        or data.get("version") != 1
        or not isinstance(data.get("assets"), dict)
    ):
        raise ValueError("Invalid artifact manifest")
    plan = ArtifactPlan()
    for key, entry in data["assets"].items():
        if not isinstance(entry, dict) or not isinstance(entry.get("reference"), dict):
            raise ValueError("Invalid artifact entry")
        outcome = entry.get("outcome")
        if outcome not in {"saved", "partial"}:
            label = str(outcome or "unknown")
            plan.gaps[label] = plan.gaps.get(label, 0) + 1
            continue
        ref = entry["reference"]
        if ref.get("kind") not in {"file", "sandbox", "canvas"}:
            raise ValueError("Unsupported saved artifact kind")
        plan.assets[key] = entry
        for is_history in [False, True] if ref.get("kind") == "canvas" else [False]:
            path_key, size_key, hash_key = (
                ("history", "history_bytes", "history_sha256")
                if is_history
                else ("path", "bytes", "sha256")
            )
            if not isinstance(entry.get(path_key), str):
                raise ValueError(f"Artifact lacks {path_key}: {key}")
            path = owned_path(root, entry[path_key])
            size, sha = entry.get(size_key), entry.get(hash_key)
            if (
                not isinstance(size, int)
                or size < 0
                or not isinstance(sha, str)
                or not re.fullmatch(r"[a-f0-9]{64}", sha)
            ):
                raise ValueError(f"Artifact lacks valid checksum/size: {key}")
            name = str(entry.get("name") or path.name).replace("\\", "/").split("/")[-1]
            name = re.sub(r"[\x00-\x1f\x7f]", "_", name) or "artifact"
            if is_history:
                name += ".history.json"
            item = AssetFile(
                f"{key}:{'history' if is_history else 'file'}",
                path,
                name,
                size,
                sha,
                {
                    "chatgpt_asset_id": key,
                    "chatgpt_reference": ref,
                    "outcome": outcome,
                    "artifact_part": "history" if is_history else "file",
                },
            )
            verify_file(item)
            plan.files.append(item)
            # Only prose canvases become Notes. HTML and code remain downloadable source.
            if (
                canvas_notes
                and not is_history
                and ref.get("kind") == "canvas"
                and path.suffix.lower() in {".md", ".txt"}
            ):
                text = path.read_text(encoding="utf-8")
                warning = (
                    "PARTIAL reconstruction: some Canvas edits could not be replayed.\n\n"
                    if outcome == "partial"
                    else ""
                )
                # Preserve the original file separately; prevent raw HTML in the Note copy.
                plan.notes[key] = warning + html.escape(text, quote=False)
        cid = ref.get("conversation_id")
        if isinstance(cid, str) and cid:
            plan.conversations.setdefault(cid, []).append(key)
    conversations = data.get("conversations", {})
    if not isinstance(conversations, dict):
        raise ValueError("Invalid artifact conversation inventory")
    for cid, record in conversations.items():
        refs = record.get("references") if isinstance(record, dict) else None
        if not isinstance(refs, list) or not all(isinstance(key, str) for key in refs):
            raise ValueError("Invalid conversation artifact references")
        for key in refs:
            if key not in data["assets"]:
                raise ValueError("Conversation references an absent artifact")
            if key in plan.assets and key not in plan.conversations.setdefault(cid, []):
                plan.conversations[cid].append(key)
    return plan


def journaled_upload(db, scope, key, digest, send):
    db.execute(
        "CREATE TABLE IF NOT EXISTS artifact_uploads (scope TEXT, source_id TEXT, "
        "digest TEXT, remote_id TEXT, PRIMARY KEY(scope, source_id))"
    )
    with db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT digest, remote_id FROM artifact_uploads WHERE scope=? AND source_id=?",
            (scope, key),
        ).fetchone()
        if row:
            if row[0] != digest:
                raise ValueError(f"Artifact changed since upload: {key}")
            if not row[1]:
                raise ValueError(
                    f"Uncertain artifact upload: {key}; reconcile artifact_uploads journal"
                )
            return row[1]
        db.execute("INSERT INTO artifact_uploads VALUES (?, ?, ?, NULL)", (scope, key, digest))
    result = send()
    if not isinstance(result, dict) or not isinstance(result.get("id"), str) or not result["id"]:
        raise ValueError("Artifact upload response lacks ID; journal left pending")
    with db:
        db.execute(
            "UPDATE artifact_uploads SET remote_id=? WHERE scope=? AND source_id=?",
            (result["id"], scope, key),
        )
    return result["id"]


def upload_artifacts(
    plan: ArtifactPlan, db, session, base: str, scope: str, request
) -> dict[str, dict]:
    """Upload one file at a time, with processing disabled; create private Canvas Notes."""
    remote: dict[str, str] = {}
    for number, item in enumerate(plan.files, 1):

        def send_file(item=item):
            # Revalidate just before upload, not only during preflight.
            verify_file(item)
            with item.path.open("rb") as stream:
                result = request(
                    session,
                    "POST",
                    base + "/api/v1/files/",
                    params={"process": "false"},
                    files={"file": (item.name, stream, "application/octet-stream")},
                    data={"metadata": json.dumps(item.metadata)},
                )
            meta = result.get("meta", {}) if isinstance(result, dict) else {}
            if meta.get("size") != item.size or meta.get("file_hash") != item.sha256:
                raise ValueError("Server file size/checksum not confirmed; journal left pending")
            return result

        digest = hashlib.sha256(
            json.dumps(
                {"sha256": item.sha256, "name": item.name, "meta": item.metadata}, sort_keys=True
            ).encode()
        ).hexdigest()
        remote[item.key] = journaled_upload(db, scope, item.key, digest, send_file)
        print(f"Artifact files: {number}/{len(plan.files)} ready", flush=True)
    links = {}
    for key, entry in plan.assets.items():
        file_id = remote[f"{key}:file"]
        download = base + "/api/v1/files/" + quote(file_id, safe="") + "/content?attachment=true"
        link: dict[str, Any] = {
            "asset_id": key,
            "name": str(entry.get("name") or "Artifact"),
            "url": download,
            "file_id": file_id,
            "outcome": entry["outcome"],
            "reference": entry["reference"],
            "sha256": entry["sha256"],
        }
        history_id = remote.get(f"{key}:history")
        if history_id:
            link["history_url"] = (
                base + "/api/v1/files/" + quote(history_id, safe="") + "/content?attachment=true"
            )
        if key in plan.notes:
            title = ("[Partial] " if entry["outcome"] == "partial" else "") + link["name"]
            form = {
                "title": title,
                "data": {"content": {"md": plan.notes[key], "html": "", "json": None}},
                "meta": {
                    "chatgpt_asset_id": key,
                    "chatgpt_reference": entry["reference"],
                    "outcome": entry["outcome"],
                    "source_url": download,
                    "history_url": link.get("history_url"),
                },
                "access_grants": [],
            }

            def send_note(form=form):
                result = request(session, "POST", base + "/api/v1/notes/create", json=form)
                content = (
                    ((result.get("data") or {}).get("content") or {})
                    if isinstance(result, dict)
                    else {}
                )
                if content.get("md") != form["data"]["content"]["md"]:
                    raise ValueError("Canvas Note content not confirmed; journal left pending")
                return result

            digest = hashlib.sha256(json.dumps(form, sort_keys=True).encode()).hexdigest()
            note_id = journaled_upload(db, scope, f"{key}:note", digest, send_note)
            link["note_url"] = base + "/notes/" + quote(note_id, safe="")
        links[key] = link
    return links


def chat_asset_links(plan: ArtifactPlan, links: dict, ident: str, detail: dict) -> list[dict]:
    keys = list(plan.conversations.get(ident, []))
    project = detail.get("gizmo_id") or detail.get("project_id") or detail.get("_project_id")
    if isinstance(project, str) and project.startswith("g-p-"):
        for key, entry in plan.assets.items():
            ref = entry["reference"]
            if (
                ref.get("project_id") == project
                and not ref.get("conversation_id")
                and key not in keys
            ):
                keys.append(key)
    return [links[key] for key in keys]


def escape_label(value: str) -> str:
    value = html.escape(value.replace("\n", " ").replace("\r", " "), quote=False)
    return re.sub(r"([\\`*_{}\[\]()!])", r"\\\1", value)


def enrich_chat(chat: dict, links: list[dict]) -> dict:
    """Append manifest-backed links, preserving existing branches and original source JSON."""
    result = deepcopy(chat)
    existing = {item["asset_id"]: item for item in result.get("chatgpt_artifacts", [])}
    history = result.get("history") or {}
    messages = history.get("messages") or {}
    source = result.get("chatgpt_source") or {}
    mapping = source.get("mapping") or {}
    for link in links:
        key = link["asset_id"]
        if key in existing:
            if existing[key] != link:
                raise ValueError("Existing artifact links differ; refusing to overwrite")
            continue
        target = link["reference"].get("message_id")
        if target not in mapping and target not in messages:
            target = next(
                (
                    node
                    for node, value in mapping.items()
                    if (value.get("message") or {}).get("id") == target
                ),
                None,
            )
        seen = set()
        while target not in messages and target in mapping and target not in seen:
            seen.add(target)
            target = mapping[target].get("parent")
        if target not in messages:
            target = history.get("currentId")
        if target not in messages:
            raise ValueError("No visible message available for artifact links")
        label = escape_label(link["name"])
        partial = " **[Partial reconstruction]**" if link["outcome"] == "partial" else ""
        text = f"\n\nImported artifact{partial}: [{label}](<{link['url']}>)"
        if link.get("history_url"):
            text += f" · [Canvas history](<{link['history_url']}>)"
        if link.get("note_url"):
            text += f" · [Editable Note](<{link['note_url']}>)"
        content = messages[target].get("content", "")
        locator = link["reference"].get("locator")
        # Rewrite only a matching Markdown link destination, in its source message.
        if link["reference"].get("kind") == "sandbox" and isinstance(locator, str):
            old = locator if locator.startswith("sandbox:") else "sandbox:" + locator
            content = content.replace("](" + old + ")", "](<" + link["url"] + ">)")
        messages[target]["content"] = content + text
        existing[key] = link
    if links:
        result["chatgpt_artifacts"] = list(existing.values())
        for message in result.get("messages", []):
            if message.get("id") in messages:
                message["content"] = messages[message["id"]]["content"]
    return result


def update_existing_artifacts(db, session, base, scope, ident, links, allow_update, request):
    row = db.execute(
        "SELECT remote_id FROM imports WHERE scope=? AND source_id=?", (scope, ident)
    ).fetchone()
    if not row or not row[0]:
        raise ValueError("No completed chat import for artifact links")
    endpoint = base + "/api/v1/chats/" + quote(row[0], safe="")
    remote = request(session, "GET", endpoint)
    if (
        not isinstance(remote, dict)
        or remote.get("id") != row[0]
        or not isinstance(remote.get("chat"), dict)
    ):
        raise ValueError("Cannot verify existing chat for artifact links")
    chat = remote["chat"]
    enriched = enrich_chat(chat, links)
    if enriched == chat:
        return
    if not allow_update:
        raise ValueError(
            "Existing chat needs artifact links. Use --update-existing-artifacts to "
            "allow changing its last-update date; avoid editing chats during migration"
        )
    # Send only the message nodes we changed, allowing server history merging to retain others.
    messages = enriched["history"]["messages"]
    changed = {
        key: value
        for key, value in messages.items()
        if value != chat["history"]["messages"].get(key)
    }
    patch = {"history": {"messages": changed}, "chatgpt_artifacts": enriched["chatgpt_artifacts"]}
    if "messages" in enriched:
        patch["messages"] = enriched["messages"]
    result = request(session, "POST", endpoint, json={"chat": patch})
    if (
        not isinstance(result, dict)
        or (result.get("chat") or {}).get("chatgpt_artifacts") != enriched["chatgpt_artifacts"]
    ):
        raise ValueError("Server did not confirm artifact links; inspect chat before retrying")
