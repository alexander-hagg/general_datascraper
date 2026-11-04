"""
pdf_annotations.py — locate evidence quotes in archived PDFs and annotate them.

The extractor asks the model for a short verbatim `evidence` quote for every
row. This module is the deterministic second half:

1. locate that quote in the archived PDF (exact first, then a fuzzy fallback);
2. write the 1-based page number into the row's `evidence_page` cell;
3. save a highlighted copy of the PDF under ``output/annotated_pdfs/``;
4. register the result in ``output/pdf_index.json`` so the web app can serve
   the annotated PDF (or the original when nothing could be located).

Everything here is best-effort: a broken PDF, an unreadable quote or a missing
PyMuPDF install must never fail an extraction run. ``annotate_paper_rows()``
returns the rows plus a small status dict and never raises.

The index stores only basenames. All source PDFs live under ``config.PDF_DIR``
and all annotated copies under ``config.ANNOTATED_PDF_DIR``; helpers here
resolve and validate those paths so an API caller can never request an
arbitrary file or traverse out of those folders.
"""

import hashlib
import json
import os
import re
import threading
import unicodedata
import uuid
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

import config

try:  # PyMuPDF >= 1.24 ships the modern `pymupdf` import name.
    import pymupdf
except Exception:  # pragma: no cover - exercised by the best-effort tests
    pymupdf = None

PYMUPDF_AVAILABLE = pymupdf is not None


def _pymupdf_available():
    return bool(PYMUPDF_AVAILABLE and pymupdf is not None)


EXACT = "exact"
FUZZY = "fuzzy"
UNRESOLVED = "unresolved"
STATUSES = (EXACT, FUZZY, UNRESOLVED)

# Every located quote is highlighted in the same explicit yellow, independent
# of whether it was an exact or fuzzy match.
HIGHLIGHT_COLOR = (1.0, 1.0, 0.0)

# Pipeline/meta columns are never copied into the annotation popup: they are
# identifiers or rendering metadata, not the extracted data the user asked for.
ANNOTATION_META_COLUMNS = frozenset({
    "paper_key",
    "row_index",
    "doi",
    "source",
    "publication_type",
    "pdf_file",
    "pdf_source",
    "evidence",
    "evidence_page",
})

# Keep popups readable instead of dumping an entire (possibly huge) row.
ANNOTATION_DATA_MAX_CHARS = 800
ANNOTATION_VALUE_MAX_CHARS = 300

_HYPHEN_BREAK_RE = re.compile(
    r"(?<=[^\W\d_])[-\u2010\u2011\u2012\u2013\u2014]\s*\n\s*(?=[^\W\d_])"
)
_SAFE_STEM_RE = re.compile(r"[^A-Za-z0-9._-]+")

_INDEX_LOCK = threading.RLock()


# --------------------------------------------------------------------------
# Text normalization and quote location
# --------------------------------------------------------------------------

def normalize_text(text):
    """Normalize text for reliable exact/fuzzy matching.

    Case is folded, typographic variants are NFKC-normalized, line-break
    hyphenation ("hyphen-\\nword") is joined, and all whitespace (including
    newlines and non-breaking spaces) collapses to a single space.
    """
    if text is None:
        return ""
    value = unicodedata.normalize("NFKC", str(text))
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = value.replace("\u00ad", "")  # soft hyphen
    value = _HYPHEN_BREAK_RE.sub("", value)
    value = " ".join(value.split())
    return value.casefold()


def _make_rect(values):
    """Build a PyMuPDF Rect from a bbox, or a plain tuple without PyMuPDF."""
    try:
        x0, y0, x1, y1 = (float(v) for v in values[:4])
    except (TypeError, ValueError, IndexError):
        return None
    if x1 < x0 or y1 < y0:
        return None
    if pymupdf is not None:
        return pymupdf.Rect(x0, y0, x1, y1)
    return (x0, y0, x1, y1)


def _block_items(page_or_blocks):
    """Return [{'text': ..., 'rect': ...}] from a page or a block list."""
    if page_or_blocks is None:
        return []
    raw = None
    if hasattr(page_or_blocks, "get_text"):
        try:
            raw = page_or_blocks.get_text("blocks")
        except Exception:
            raw = []
    elif isinstance(page_or_blocks, dict) and "blocks" in page_or_blocks:
        raw = page_or_blocks.get("blocks")
    elif isinstance(page_or_blocks, (list, tuple)):
        raw = list(page_or_blocks)
    if not isinstance(raw, (list, tuple)):
        return []

    items = []
    for block in raw:
        if isinstance(block, dict):
            # A "dict"-mode block: use its lines for tighter fuzzy rects.
            for line in block.get("lines") or []:
                if not isinstance(line, dict):
                    continue
                text = "".join(
                    str(span.get("text") or "")
                    for span in (line.get("spans") or [])
                    if isinstance(span, dict)
                )
                rect = _make_rect(line.get("bbox") or ())
                if text.strip() and rect is not None:
                    items.append({"text": text, "rect": rect})
            continue
        # Standard get_text("blocks") tuple: x0,y0,x1,y1,text,block_no,type
        text = ""
        bbox = ()
        if isinstance(block, (list, tuple)) and len(block) >= 5:
            bbox = block[:4]
            text = block[4]
        elif (
            isinstance(block, (list, tuple))
            and len(block) == 2
            and isinstance(block[0], str)
        ):
            text = block[0]
            bbox = block[1] if isinstance(block[1], (list, tuple)) else ()
        rect = _make_rect(bbox)
        if str(text or "").strip() and rect is not None:
            items.append({"text": str(text), "rect": rect})
    return items


def _page_number(page_or_blocks):
    number = getattr(page_or_blocks, "number", None)
    try:
        if number is not None:
            return int(number) + 1
    except (TypeError, ValueError):
        pass
    if isinstance(page_or_blocks, dict):
        try:
            return int(page_or_blocks.get("number", 0)) + 1
        except (TypeError, ValueError):
            pass
    return 1


def _block_texts(blocks):
    return [
        {
            "text": normalize_text(item["text"]),
            "raw": item["text"],
            "rect": item["rect"],
        }
        for item in blocks
    ]


def _spanning_rects(blocks, quote_norm):
    """Return the block rects that cover `quote_norm` in joined block text."""
    parts, starts = [], []
    offset = 0
    for item in blocks:
        text = item["text"]
        starts.append(offset)
        parts.append(text)
        offset += len(text) + 1
    haystack = " ".join(parts)
    if not haystack or not quote_norm:
        return []
    start = haystack.find(quote_norm)
    if start < 0:
        return []
    end = start + len(quote_norm)
    rects = []
    for index, item in enumerate(blocks):
        text = item["text"]
        block_start = starts[index]
        block_end = block_start + len(text)
        if block_start < end and block_end > start:
            rects.append(item["rect"])
    return rects


def _fuzzy_rects(blocks, quote_norm, threshold):
    """Best SequenceMatcher candidate over block and line-sized candidates."""
    if not quote_norm:
        return []
    best_ratio = 0.0
    best_rect = None
    for item in blocks:
        text = item["text"]
        raw = item.get("raw", text)
        rect = item["rect"]
        lines = [line for line in str(raw or "").splitlines() if line.strip()]
        candidates = []
        if len(lines) > 1:
            try:
                height = (rect.y1 - rect.y0) / len(lines)
            except AttributeError:
                height = 0.0
            for line_index, line in enumerate(lines):
                if height > 0 and pymupdf is not None:
                    line_rect = pymupdf.Rect(
                        rect.x0,
                        rect.y0 + line_index * height,
                        rect.x1,
                        rect.y0 + (line_index + 1) * height,
                    )
                else:
                    line_rect = rect
                candidates.append((normalize_text(line), line_rect))
        candidates.append((text, rect))

        for candidate, candidate_rect in candidates:
            if not candidate:
                continue
            ratio = SequenceMatcher(None, quote_norm, candidate).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_rect = candidate_rect

    if best_rect is not None and best_ratio >= threshold:
        return [best_rect]
    return []


def _default_threshold():
    try:
        value = float(getattr(config, "EVIDENCE_FUZZY_THRESHOLD", 0.75))
    except (TypeError, ValueError):
        value = 0.75
    return min(1.0, max(0.0, value))


def locate_quote(page_or_blocks, quote, threshold=None):
    """Locate one quote on a page or in a list of text blocks.

    Returns ``(page_number, rects, status)`` where ``page_number`` is 1-based
    and ``status`` is ``"exact"``, ``"fuzzy"`` or ``"unresolved"``. Exact
    matching tries PyMuPDF's native page search first (which handles repeated
    hits and precise word rectangles), then a whitespace/case-insensitive
    normalized substring match across blocks.
    """
    page_number = _page_number(page_or_blocks)
    quote_text = str(quote or "").strip()
    quote_norm = normalize_text(quote_text)
    if not quote_norm:
        return page_number, [], UNRESOLVED

    if threshold is None:
        threshold = _default_threshold()
    else:
        try:
            threshold = float(threshold)
        except (TypeError, ValueError):
            threshold = _default_threshold()
    threshold = min(1.0, max(0.0, threshold))

    if pymupdf is not None and hasattr(page_or_blocks, "search_for"):
        try:
            hits = page_or_blocks.search_for(quote_text)
        except Exception:
            hits = []
        rects = [_make_rect(hit) for hit in hits or []]
        rects = [rect for rect in rects if rect is not None]
        if rects:
            return page_number, rects, EXACT

    blocks = _block_texts(_block_items(page_or_blocks))
    if not blocks:
        return page_number, [], UNRESOLVED

    exact_rects = _spanning_rects(blocks, quote_norm)
    if exact_rects:
        return page_number, exact_rects, EXACT

    fuzzy_rects = _fuzzy_rects(blocks, quote_norm, threshold)
    if fuzzy_rects:
        return page_number, fuzzy_rects, FUZZY

    return page_number, [], UNRESOLVED


def _locate_in_document(document, quote):
    """Find `quote` anywhere in an open PDF; returns (page, rects, status)."""
    for page_index, page in enumerate(document):
        try:
            page_number, rects, status = locate_quote(page, quote)
        except Exception:
            continue
        if status != UNRESOLVED:
            return page_index + 1, rects, status
    return None, [], UNRESOLVED


# --------------------------------------------------------------------------
# Safe path helpers (only basenames inside the configured output folders)
# --------------------------------------------------------------------------

def _safe_basename(value):
    """Return a plain filename, or "" for blank/absolute/traversal values."""
    if value is None:
        return ""
    text = str(value).strip()
    if not text or "\x00" in text:
        return ""
    if "/" in text or "\\" in text or ":" in text:
        return ""
    if text in (".", "..") or Path(text).is_absolute():
        return ""
    return text


def _safe_path(base, filename, must_exist=True):
    """Resolve `filename` under `base`, rejecting traversal and escapes."""
    name = _safe_basename(filename)
    if not name:
        return None
    base_path = Path(base)
    candidate = base_path / name
    try:
        base_resolved = base_path.resolve()
        resolved = candidate.resolve()
        resolved.relative_to(base_resolved)
    except (OSError, ValueError, RuntimeError):
        return None
    if must_exist and not resolved.is_file():
        return None
    return resolved


def _safe_stem(value):
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = _SAFE_STEM_RE.sub("_", text).strip("._-")
    return text[:60] or "paper"


def _is_pdf_file(path):
    try:
        with open(path, "rb") as handle:
            return handle.read(5).lstrip() == b"%PDF-"
    except OSError:
        return False


def _file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_pdf_path(pdf_path=None, pdf_file=""):
    """Return an existing source PDF that is safely inside config.PDF_DIR."""
    candidates = []
    if pdf_path:
        candidates.append(Path(str(pdf_path)).expanduser())
    name = _safe_basename(pdf_file)
    if name:
        candidates.append(Path(config.PDF_DIR) / name)
    for candidate in candidates:
        path = _safe_path(config.PDF_DIR, Path(candidate).name)
        if path is not None:
            return path
    return None


# --------------------------------------------------------------------------
# Index persistence
# --------------------------------------------------------------------------

def _index_path(path=None):
    return Path(path or config.PDF_INDEX_JSON)


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean_row_index(value):
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return value
    text = str(value if value is not None else "").strip()
    if text.isdigit():
        return int(text)
    return text


def _clean_field_name(value):
    """Return a compact, non-empty field name for a popup data pair."""
    try:
        text = " ".join(str(value if value is not None else "").split())
    except Exception:
        return ""
    return text[:120]


def _stringify_data_value(value):
    """Turn any extracted value into a readable, bounded string.

    Lists/tuples/sets are joined with commas and dicts with ``key=value;``
    pairs, so unusual LLM output never crashes annotation creation. ``None``,
    booleans, numbers and arbitrary objects fall back to ``str()``.
    """
    try:
        if value is None:
            return ""
        if isinstance(value, str):
            text = value.strip()
        elif isinstance(value, bool):
            text = "true" if value else "false"
        elif isinstance(value, (int, float)):
            text = str(value)
        elif isinstance(value, (list, tuple, set, frozenset)):
            parts = []
            for item in value:
                part = _stringify_data_value(item)
                if part:
                    parts.append(part)
            text = ", ".join(parts)
        elif isinstance(value, dict):
            parts = []
            for key, item in value.items():
                name = _clean_field_name(key)
                part = _stringify_data_value(item)
                if name and part:
                    parts.append(f"{name}={part}")
            text = "; ".join(parts)
        else:
            text = str(value).strip()
    except Exception:
        return ""
    if not text:
        return ""
    text = " ".join(text.split())
    if len(text) > ANNOTATION_VALUE_MAX_CHARS:
        text = text[: ANNOTATION_VALUE_MAX_CHARS - 1].rstrip() + "…"
    return text


def _row_data(row):
    """Collect the non-empty extracted fields of one row for the popup.

    Pipeline/meta columns (``ANNOTATION_META_COLUMNS``) are skipped. Key order
    is preserved so the popup follows the user's extraction-field order.
    """
    if not isinstance(row, dict):
        return {}
    data = {}
    for key, value in row.items():
        name = _clean_field_name(key)
        if not name or name in ANNOTATION_META_COLUMNS:
            continue
        text = _stringify_data_value(value)
        if text:
            data[name] = text
    return data


def _clean_data(value):
    """Sanitize a stored data mapping (drop meta names/empty values)."""
    if not isinstance(value, dict):
        return {}
    clean = {}
    for key, item in value.items():
        name = _clean_field_name(key)
        if not name or name in ANNOTATION_META_COLUMNS:
            continue
        text = _stringify_data_value(item)
        if text:
            clean[name] = text
    return clean


def _format_data(value):
    """Render a data mapping as ``field=value; field=value`` (bounded)."""
    data = _clean_data(value)
    text = "; ".join(f"{key}={item}" for key, item in data.items())
    if len(text) > ANNOTATION_DATA_MAX_CHARS:
        text = text[: ANNOTATION_DATA_MAX_CHARS - 1].rstrip() + "…"
    return text


def _annotation_content(item):
    """Build one highlight popup containing the data point and the quote."""
    quote = str(item.get("text") or "").strip()
    data_text = _format_data(item.get("data"))
    if data_text:
        return f'Data: {data_text}\nEvidence: "{quote}"'
    row_index = str(item.get("row_index") or "").strip()
    label = f"Row {row_index}" if row_index else "ScrapeLite evidence"
    return f'{label}\nEvidence: "{quote}"'


def _clean_evidence(value):
    if not isinstance(value, list):
        return []
    cleaned = []
    for item in value:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        status = str(item.get("status") or UNRESOLVED).strip().casefold()
        if status not in STATUSES:
            status = UNRESOLVED
        page = item.get("page")
        try:
            page = int(page) if page is not None and str(page).strip() else None
        except (TypeError, ValueError):
            page = None
        if status == UNRESOLVED or (page is not None and page < 1):
            page = None
        cleaned.append({
            "row_index": _clean_row_index(item.get("row_index")),
            "text": text,
            "page": page,
            "status": status,
            "data": _clean_data(item.get("data")),
        })
    return cleaned


def _clean_entry(key, entry):
    if not isinstance(entry, dict):
        return None
    key = str(key or "").strip()
    if not key:
        return None
    pdf_file = _safe_basename(entry.get("pdf_file"))
    if not pdf_file:
        return None
    raw_annotation = str(entry.get("annotation_file") or "").strip()
    annotation_file = _safe_basename(raw_annotation) if raw_annotation else ""
    if raw_annotation and not annotation_file:
        return None
    return {
        "paper_key": key,
        "pdf_file": pdf_file,
        "pdf_source": str(entry.get("pdf_source") or "none"),
        "title": str(entry.get("title") or ""),
        "annotation_file": annotation_file,
        "annotation_hash": str(entry.get("annotation_hash") or ""),
        "evidence": _clean_evidence(entry.get("evidence")),
        "updated_at": str(entry.get("updated_at") or _now()),
    }


def _sanitize_index(index):
    clean = {}
    if not isinstance(index, dict):
        return clean
    for key, entry in index.items():
        item = _clean_entry(key, entry)
        if item is not None:
            clean[item["paper_key"]] = item
    return clean


def load_index(path=None):
    """Load the PDF index; missing/corrupt files and invalid entries yield {}."""
    index_path = _index_path(path)
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return _sanitize_index(data)


def _write_index_unlocked(index, path=None):
    index_path = _index_path(path)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    temp = index_path.with_name(
        f".{index_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        temp.write_text(
            # Keep insertion order so an annotation's data pairs keep the
            # extraction-field order when the index is reloaded/regenerated.
            json.dumps(index, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(temp, index_path)
    finally:
        try:
            if temp.exists():
                temp.unlink()
        except OSError:
            pass
    return index


def save_index(index, path=None):
    """Write a sanitized index atomically under the module lock."""
    clean = _sanitize_index(index)
    with _INDEX_LOCK:
        _write_index_unlocked(clean, path)
    return clean


def _update_index_entry(entry, path=None):
    """Merge one entry into the on-disk index atomically."""
    if not isinstance(entry, dict):
        return load_index(path)
    key = str(entry.get("paper_key") or "").strip()
    clean_entry = _clean_entry(key, entry)
    if clean_entry is None:
        return load_index(path)
    with _INDEX_LOCK:
        current = load_index(path)
        current[clean_entry["paper_key"]] = clean_entry
        _write_index_unlocked(current, path)
    return current


def api_summary(index=None):
    """Return the API-safe annotations mapping (basenames only, no paths)."""
    clean = load_index() if index is None else _sanitize_index(index)
    summary = {}
    for key, entry in clean.items():
        source_path = _safe_path(config.PDF_DIR, entry.get("pdf_file"))
        annotation_path = _safe_path(
            config.ANNOTATED_PDF_DIR, entry.get("annotation_file")
        )
        summary[key] = {
            "available": bool(source_path or annotation_path),
            "pdf_file": entry["pdf_file"],
            "pdf_source": entry["pdf_source"],
            "title": entry["title"],
            "evidence": [
                {
                    "row_index": item["row_index"],
                    "text": item["text"],
                    "page": item["page"],
                    "status": item["status"],
                }
                for item in entry["evidence"]
            ],
            # The row data mapping stays internal: the viewer already gets the
            # extracted rows, and the API only exposes this fixed shape.
        }
    return summary


def resolve_paper_paths(paper_key, index=None):
    """Resolve a paper key to safe source/annotation paths.

    Returns ``{"paper_key", "entry", "source_path", "annotation_path"}`` or
    ``None`` for an unknown key or an entry with unsafe/missing metadata.
    """
    key = str(paper_key or "").strip()
    if not key:
        return None
    clean = load_index() if index is None else _sanitize_index(index)
    entry = clean.get(key)
    if entry is None:
        return None
    source_path = _safe_path(config.PDF_DIR, entry.get("pdf_file"))
    annotation_path = _safe_path(
        config.ANNOTATED_PDF_DIR, entry.get("annotation_file")
    )
    return {
        "paper_key": key,
        "entry": entry,
        "source_path": source_path,
        "annotation_path": annotation_path,
    }


# --------------------------------------------------------------------------
# Annotation creation / cache
# --------------------------------------------------------------------------

def _annotation_hash(paper_key, source_hash, evidence):
    payload = {
        "paper_key": str(paper_key),
        "source": str(source_hash),
        "evidence": [
            {
                "row_index": item.get("row_index"),
                "text": item.get("text"),
                "page": item.get("page"),
                "status": item.get("status"),
                "data": _clean_data(item.get("data")),
            }
            for item in (evidence or [])
        ],
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _annotation_filename(paper_key, annotation_hash):
    return f"annotated_{_safe_stem(paper_key)}_{annotation_hash[:20]}.pdf"


def _write_annotated(document, resolved, target):
    """Add yellow highlights/comments to `document` and save to `target`."""
    for item, rects in resolved:
        try:
            page = document[int(item["page"]) - 1]
        except (IndexError, TypeError, ValueError):
            continue
        content = _annotation_content(item)
        for rect in rects:
            try:
                annot = page.add_highlight_annot(rect)
                # Yellow for exact and fuzzy matches alike (the popup carries
                # the match status separately in the index, if needed).
                annot.set_colors(stroke=HIGHLIGHT_COLOR)
                annot.set_info(title="ScrapeLite evidence", content=content)
                annot.update()
            except Exception:
                # One malformed rectangle must not lose the rest of the PDF.
                continue

    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp.pdf")
    try:
        document.save(str(temp), garbage=3, deflate=True)
        os.replace(temp, target)
    finally:
        try:
            if temp.exists():
                temp.unlink()
        except OSError:
            pass
    return target


def _summary(paper_key="", **values):
    summary = {
        "paper_key": str(paper_key or ""),
        "available": False,
        "pdf_file": "",
        "pdf_source": "none",
        "title": "",
        "annotation_file": "",
        "annotation_hash": "",
        "evidence": [],
        "located": 0,
        "unresolved": 0,
        "warning": "",
    }
    summary.update(values)
    return summary


def summary_message(summary):
    """Short human-readable line for the CLI/web job log."""
    if not isinstance(summary, dict):
        return ""
    located = int(summary.get("located") or 0)
    unresolved = int(summary.get("unresolved") or 0)
    base = f"{located} evidence quote(s) located, {unresolved} unresolved"
    warning = str(summary.get("warning") or "").strip()
    if warning:
        if located or unresolved:
            return f"{base} ({warning})"
        return f"Evidence warning: {warning}"
    return base


def annotate_paper_rows(rows, pdf_path=None, paper_key="", title="",
                        pdf_file="", pdf_source="none", index_path=None):
    """Locate evidence, fill ``evidence_page`` and cache an annotated PDF.

    Returns ``(rows, summary)``. The rows are updated in place (and also
    returned for convenience). This function never raises: when evidence is
    disabled, PyMuPDF is unavailable, or the PDF/quote cannot be read, the rows
    are returned unchanged or with blank pages and the summary carries a
    warning.
    """
    result_rows = list(rows or [])
    key = str(paper_key or "").strip()

    if not getattr(config, "EVIDENCE_ENABLED", True):
        return result_rows, _summary(
            key, warning="Evidence annotations are disabled (EVIDENCE_ENABLED = False)."
        )
    if not _pymupdf_available():
        return result_rows, _summary(
            key, warning="PyMuPDF is not installed; evidence pages and annotated PDFs are unavailable."
        )
    if not key:
        return result_rows, _summary(
            key, warning="No paper key; the PDF cannot be indexed for the viewer."
        )

    source = _source_pdf_path(pdf_path, pdf_file)
    if source is None:
        for row in result_rows:
            if isinstance(row, dict):
                row["evidence_page"] = ""
        return result_rows, _summary(
            key,
            warning="No archived PDF found for this paper; only the evidence text is available.",
        )

    evidence = []
    resolved = []
    annotation_file = ""
    annotation_hash = ""
    warning = ""
    document = None
    # No PDF/quote may ever invent a page. Clear stale values before trying;
    # only a successful locate writes a page back.
    for row in result_rows:
        if isinstance(row, dict):
            row["evidence_page"] = ""
    try:
        source_hash = _file_sha256(source)
        document = pymupdf.open(str(source))
        for row in result_rows:
            if not isinstance(row, dict):
                continue
            quote = str(row.get("evidence") or "").strip()
            if not quote:
                continue
            page, rects, status = _locate_in_document(document, quote)
            item = {
                "row_index": _clean_row_index(row.get("row_index")),
                "text": quote,
                "page": page if status != UNRESOLVED else None,
                "status": status,
                "data": _row_data(row),
            }
            evidence.append(item)
            if status != UNRESOLVED:
                row["evidence_page"] = str(page)
                resolved.append((item, rects))

        annotation_hash = _annotation_hash(key, source_hash, evidence)
        target = Path(config.ANNOTATED_PDF_DIR) / _annotation_filename(
            key, annotation_hash
        )
        if resolved:
            if not (target.is_file() and _is_pdf_file(target)):
                _write_annotated(document, resolved, target)
            if target.is_file() and _is_pdf_file(target):
                annotation_file = target.name
            else:
                warning = "Could not write the annotated PDF; showing the original instead."
        else:
            warning = "No evidence quote could be located in the PDF."
    except Exception as exc:  # noqa: BLE001 - evidence must never break extraction
        warning = f"Evidence annotation failed: {exc}"
    finally:
        if document is not None:
            try:
                document.close()
            except Exception:
                pass

    located = sum(1 for item in evidence if item["status"] != UNRESOLVED)
    unresolved = sum(1 for item in evidence if item["status"] == UNRESOLVED)
    entry = {
        "paper_key": key,
        "pdf_file": source.name,
        "pdf_source": str(pdf_source or "none"),
        "title": str(title or ""),
        "annotation_file": annotation_file,
        "annotation_hash": annotation_hash if annotation_file else "",
        "evidence": evidence,
        "updated_at": _now(),
    }
    try:
        _update_index_entry(entry, index_path)
    except Exception as exc:  # noqa: BLE001 - the viewer index is best-effort too
        warning = (warning + " " if warning else "") + f"Could not update the PDF index: {exc}"

    return result_rows, _summary(
        key,
        available=True,
        pdf_file=source.name,
        pdf_source=entry["pdf_source"],
        title=entry["title"],
        annotation_file=annotation_file,
        annotation_hash=entry["annotation_hash"],
        evidence=evidence,
        located=located,
        unresolved=unresolved,
        warning=warning,
    )


def regenerate_annotation(paper_key, index=None):
    """Rebuild a missing annotated PDF from indexed source/evidence metadata.

    Returns the annotation ``Path`` on success, or ``None`` when the paper is
    unknown, the source is missing, or no quote can be located. Never raises.
    """
    if not _pymupdf_available():
        return None
    key = str(paper_key or "").strip()
    if not key:
        return None
    try:
        clean = load_index() if index is None else _sanitize_index(index)
        entry = clean.get(key)
        if entry is None:
            return None
        source = _safe_path(config.PDF_DIR, entry.get("pdf_file"))
        if source is None:
            return None
        evidence = entry.get("evidence") or []
        located_rows = [
            item for item in evidence
            if item.get("status") in (EXACT, FUZZY) and item.get("page")
        ]
        if not located_rows:
            return None

        source_hash = _file_sha256(source)
        digest = _annotation_hash(key, source_hash, evidence)
        target = Path(config.ANNOTATED_PDF_DIR) / _annotation_filename(key, digest)
        if target.is_file() and _is_pdf_file(target):
            return target

        resolved = []
        document = pymupdf.open(str(source))
        try:
            for item in located_rows:
                page_number = int(item["page"])
                rects = []
                status = UNRESOLVED
                if 1 <= page_number <= document.page_count:
                    _num, rects, status = locate_quote(
                        document[page_number - 1], item["text"]
                    )
                if status == UNRESOLVED:
                    found_page, rects, status = _locate_in_document(
                        document, item["text"]
                    )
                    page_number = found_page
                if status == UNRESOLVED or page_number is None:
                    continue
                resolved.append(({**item, "page": page_number, "status": status}, rects))
            if not resolved:
                return None
            _write_annotated(document, resolved, target)
        finally:
            try:
                document.close()
            except Exception:
                pass

        _update_index_entry({
            **entry,
            "annotation_file": target.name,
            "annotation_hash": digest,
            "updated_at": _now(),
        })
        return target
    except Exception:
        return None
