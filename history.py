"""
history.py — global prompt history for the web app.

Searches, extraction goals and extraction column lists are recorded in one
small JSON file (`output/prompt_history.json`) so they can be offered again in
the UI. The store is intentionally process-local (a lock, no database) because
the Flask app runs as a single local process with one job at a time.

Storage format:

    {"version": 1, "entries": [entry, ...]}

Each entry is:

    {"id": "<unique>", "kind": "search"|"goal"|"columns", "data": {...},
     "created_at": "<UTC ISO>", "last_used": "<UTC ISO>", "uses": <int>}

Entries are deduplicated by a canonical fingerprint of `kind` + `data`.
Recording the same prompt again only increments `uses`, refreshes `last_used`
and moves the existing entry to the front. Writes are atomic and the list is
kept newest-used first. Old/corrupt files are treated as empty instead of
breaking the web app.

Privacy: callers pass only prompt/search fields. This module never records the
NCBI email, API keys, model settings or any other backend secret.
"""

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import config

KINDS = ("search", "goal", "columns")
_KIND_SET = frozenset(KINDS)
_OPERATORS = frozenset(("AND", "OR", "NOT"))

# One lock covers the whole read-modify-write cycle. The web app is a single
# process, so this is enough to keep a search and an extract job from losing
# each other's history entries.
_LOCK = threading.RLock()


def _now():
    """Current UTC time in ISO 8601 form (microsecond precision)."""
    return datetime.now(timezone.utc).isoformat()


def _path():
    # Read the config value on every call so tests (and future settings)
    # can redirect the store without reloading this module.
    return Path(config.PROMPT_HISTORY_JSON)


def _empty():
    return {"version": 1, "entries": []}


def _validate_kind(kind):
    if kind not in _KIND_SET:
        allowed = ", ".join(KINDS)
        raise ValueError(f"Unknown history kind {kind!r}. Use one of: {allowed}.")
    return kind


def _int_cap(name, default):
    try:
        value = int(getattr(config, name, default))
    except (TypeError, ValueError):
        value = default
    return max(0, value)


def _json_copy(data, kind):
    """Return a plain JSON copy, rejecting anything JSON cannot represent."""
    try:
        return json.loads(
            json.dumps(data, ensure_ascii=False, allow_nan=False)
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"History data for kind {kind!r} must be JSON-serializable: {exc}"
        ) from exc


def _sanitize_tree(node):
    """Keep the documented query-tree shape and drop browser-only node ids."""
    if not isinstance(node, dict):
        return None
    node_type = node.get("type")
    if node_type == "term":
        text = node.get("text")
        if isinstance(text, str):
            stored_text = text
        elif text is None:
            stored_text = ""
        else:
            stored_text = str(text)
        op = node.get("op")
        return {
            "type": "term",
            "text": stored_text,
            "op": op if op in _OPERATORS else "AND",
        }
    if node_type == "group":
        children = node.get("children")
        cleaned = []
        if isinstance(children, list):
            for child in children:
                item = _sanitize_tree(child)
                if item is not None:
                    cleaned.append(item)
        op = node.get("op")
        return {
            "type": "group",
            "op": op if op in _OPERATORS else "AND",
            "children": cleaned,
        }
    return None


def _prepare_data(kind, data):
    if not isinstance(data, dict):
        raise ValueError("History data must be a JSON object (dict).")
    prepared = _json_copy(data, kind)
    if kind == "search" and "query_tree" in prepared:
        # The browser snapshot contains no ids; sanitising here also keeps
        # older clients (or hand-written requests) from creating duplicates
        # that only differ by freshly generated node ids.
        prepared["query_tree"] = _sanitize_tree(prepared.get("query_tree"))
    return prepared


def _fingerprint(kind, data):
    return json.dumps(
        {"kind": kind, "data": data},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def _timestamp_key(entry):
    raw = entry.get("last_used") or entry.get("created_at") or ""
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        parsed = datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _sorted_entries(entries):
    # ISO timestamps sort correctly, but parsing also handles hand-edited
    # files and old offsets. Python's stable sort keeps file order on ties.
    return sorted(entries, key=_timestamp_key, reverse=True)


def _clean_entry(raw):
    if not isinstance(raw, dict):
        return None
    kind = raw.get("kind")
    if kind not in _KIND_SET:
        return None
    data = raw.get("data")
    if not isinstance(data, dict):
        return None
    try:
        # Reject entries whose data cannot be written back as JSON.
        _json_copy(data, kind)
    except ValueError:
        return None
    entry_id = raw.get("id")
    if not isinstance(entry_id, str) or not entry_id.strip():
        return None
    created_at = raw.get("created_at")
    last_used = raw.get("last_used")
    if not isinstance(created_at, str) or not isinstance(last_used, str):
        return None
    try:
        uses = int(raw.get("uses", 1))
    except (TypeError, ValueError):
        uses = 1
    if uses < 1:
        uses = 1
    return {
        "id": entry_id,
        "kind": kind,
        "data": data,
        "created_at": created_at,
        "last_used": last_used,
        "uses": uses,
    }


def _read_document():
    path = _path()
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return _empty()
    except OSError:
        # An unreadable store should not stop searches/extractions. Recording
        # a new entry will replace it with a valid document.
        return _empty()
    try:
        doc = json.loads(raw)
    except (TypeError, ValueError):
        return _empty()
    if not isinstance(doc, dict) or doc.get("version") != 1:
        return _empty()
    raw_entries = doc.get("entries")
    if not isinstance(raw_entries, list):
        return _empty()
    entries = []
    for raw_entry in raw_entries:
        entry = _clean_entry(raw_entry)
        if entry is not None:
            entries.append(entry)
    return {"version": 1, "entries": entries}


def _write_document(entries):
    """Atomically replace the store with `entries`."""
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "entries": entries}
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def _trim(entries):
    """Keep the most recently used entries up to the configured caps."""
    total_cap = _int_cap("PROMPT_HISTORY_MAX_ENTRIES", 100)
    per_kind_cap = _int_cap("PROMPT_HISTORY_MAX_PER_KIND", 50)
    counts = {}
    kept = []
    for entry in entries:
        if len(kept) >= total_cap:
            break
        kind = entry["kind"]
        if counts.get(kind, 0) >= per_kind_cap:
            continue
        kept.append(entry)
        counts[kind] = counts.get(kind, 0) + 1
    return kept


def record(kind, data):
    """Store one prompt and return the resulting entry.

    A duplicate (same kind + canonical data) keeps its id/created_at, gets a
    higher `uses`, a fresh `last_used`, and moves to the front of the list.
    """
    kind = _validate_kind(kind)
    prepared = _prepare_data(kind, data)
    fingerprint = _fingerprint(kind, prepared)

    with _LOCK:
        now = _now()
        entries = _sorted_entries(_read_document()["entries"])
        for index, entry in enumerate(entries):
            if _fingerprint(entry["kind"], entry["data"]) != fingerprint:
                continue
            entry["uses"] = int(entry.get("uses") or 1) + 1
            entry["last_used"] = now
            entries.pop(index)
            entries.insert(0, entry)
            entries = _trim(entries)
            _write_document(entries)
            return dict(entry)

        entry = {
            "id": uuid.uuid4().hex,
            "kind": kind,
            "data": prepared,
            "created_at": now,
            "last_used": now,
            "uses": 1,
        }
        entries.insert(0, entry)
        entries = _trim(entries)
        _write_document(entries)
        return dict(entry)


def list_entries(kind=None):
    """Return entries newest-used first, optionally filtered to one kind."""
    if kind is not None:
        kind = _validate_kind(kind)
    with _LOCK:
        entries = _trim(_sorted_entries(_read_document()["entries"]))
        if kind is not None:
            entries = [entry for entry in entries if entry["kind"] == kind]
        return [dict(entry) for entry in entries]


def clear(kind=None):
    """Delete all entries (or one kind) and return the number removed."""
    if kind is not None:
        kind = _validate_kind(kind)
    with _LOCK:
        entries = _read_document()["entries"]
        if kind is None:
            removed = len(entries)
            remaining = []
        else:
            remaining = [entry for entry in entries if entry["kind"] != kind]
            removed = len(entries) - len(remaining)
        if removed:
            _write_document(remaining)
        return removed
