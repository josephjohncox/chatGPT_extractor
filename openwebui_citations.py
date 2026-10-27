"""Map ChatGPT's exported search IDs to Open WebUI source-backed rich citations."""

import re
from copy import deepcopy
from urllib.parse import urlsplit

MARKER = re.compile(r"\*\(citation: ([A-Za-z0-9_.:, -]+)\)\*")
REF = re.compile(r"^turn(\d+)([A-Za-z_]+)(\d+)$")


def source_id(entry: dict) -> str | None:
    ref = entry.get("ref_id")
    if not isinstance(ref, dict):
        return None
    turn, kind, index = ref.get("turn_index"), ref.get("ref_type"), ref.get("ref_index")
    if isinstance(turn, int) and isinstance(kind, str) and isinstance(index, int):
        return f"turn{turn}{kind}{index}"
    return None


def source_index(metadata: dict) -> dict[str, dict]:
    groups = metadata.get("search_result_groups") if isinstance(metadata, dict) else None
    index: dict[str, dict] = {}
    if not isinstance(groups, list):
        return index
    for group in groups:
        entries = group.get("entries") if isinstance(group, dict) else None
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            ident = source_id(entry)
            url = entry.get("url")
            parsed = urlsplit(url) if isinstance(url, str) else None
            if ident and parsed and parsed.scheme in {"http", "https"} and parsed.netloc:
                index.setdefault(ident, entry)
    return index


def text_value(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def source_entry(ident: str, entry: dict) -> dict:
    url = str(entry["url"])
    name = text_value(entry.get("title")) or url
    document = text_value(entry.get("snippet")) or name
    return {
        "source": {"id": ident, "name": name, "url": url},
        "document": [document],
        "metadata": [{"source": url, "name": name, "external": True, "chatgpt_ref_id": ident}],
        "distances": [],
    }


def enrich_citations(content: str, metadata: dict, existing_sources=None) -> tuple[str, list]:
    """Replace only resolvable archival markers with numbered citation tokens."""
    available = source_index(metadata)
    sources = deepcopy(existing_sources) if isinstance(existing_sources, list) else []
    known = {
        (item.get("source") or {}).get("id")
        for item in sources
        if isinstance(item, dict) and isinstance(item.get("source"), dict)
    }
    numbering = [item for item in sources if isinstance(item, dict)]

    def replace(match):
        refs = [value.strip() for value in match.group(1).split(",")]
        resolved = [ref for ref in refs if ref in available]
        if not resolved:
            return match.group(0)
        labels = []
        for ref in resolved:
            if ref not in known:
                sources.append(source_entry(ref, available[ref]))
                known.add(ref)
                numbering.append(sources[-1])
            labels.append(
                str(
                    next(
                        i
                        for i, item in enumerate(numbering, 1)
                        if (item.get("source") or {}).get("id") == ref
                    )
                )
            )
        unresolved = [ref for ref in refs if ref not in available]
        citation = "[" + ",".join(labels) + "]"
        if unresolved:
            citation += " *(citation: " + ", ".join(unresolved) + ")*"
        return citation

    return MARKER.sub(replace, content), sources
