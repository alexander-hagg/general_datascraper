"""
figures.py — goal-driven figure/table search in archived PDFs.

During Extract, every paper that has an archived PDF is scanned with PyMuPDF:

1. caption blocks starting with ``Figure``/``Fig.`` or ``Table`` are located;
2. a crop around the caption is described (nearby images/drawings first, then a
   vertical band above/below, then the whole page as a last resort);
3. pages without captions contribute large embedded images;
4. all candidates are collected while the document is open;
5. the extraction goal is sent to the configured LLM together with the
   candidate metadata and only the selected candidates are rendered;
6. PNGs are stored under ``output/figures/<safe-paper-stem>/`` and registered in
   ``output/figures/index.json`` plus ``output/figures.xlsx`` with the model's
   reason.

Normal extraction is goal-filtered: if the goal does not ask for figures,
images, tables or visual data, no screenshots are saved. A ``goal`` of ``None``
keeps the legacy all-figures behaviour for direct callers/tests, and
``config.FIGURES_GOAL_FILTER = False`` restores it for the pipeline.

Everything here is best-effort. A broken PDF, a missing PyMuPDF install, an
unreachable LLM backend or an unreadable index must never fail an extraction
run. The index stores POSIX relative paths only; API helpers resolve and
validate those paths so a caller can never request an arbitrary file. Scope is
PDF only — there is no OCR and no HTML/PMC figure download. Orphan PNGs from
older versions may remain on disk; the index (not the folder) is the source of
truth.
"""

import hashlib
import json
import os
import re
import shutil
import threading
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path

import config
import suggester
import writer

try:  # PyMuPDF >= 1.24 ships the modern `pymupdf` import name.
    import pymupdf
except Exception:  # pragma: no cover - exercised by the disabled tests
    pymupdf = None

PYMUPDF_AVAILABLE = pymupdf is not None

INDEX_VERSION = 1
FIGURE_KINDS = ("caption", "image", "page")
FIGURE_COLUMNS = [
    "id", "paper_key", "title", "label", "caption", "reason", "page", "file",
    "kind", "width", "height", "dpi", "created_at",
]

# `^(figure|fig.?|table)\s*S?\d+[A-Za-z]?\b`, split so the label can be rebuilt.
_CAPTION_RE = re.compile(
    r"^(?P<word>figure|fig\.?|table)\s*(?P<number>S?\d+[A-Za-z]?)\b",
    re.IGNORECASE,
)
_SAFE_STEM_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_INDEX_LOCK = threading.RLock()

_MIN_CROP_WIDTH = 60.0
_MIN_CROP_HEIGHT = 40.0
_MAX_CAPTION_CHARS = 1000
_MAX_REASON_CHARS = 300
_MAX_PROMPT_CAPTION_CHARS = 500
_MAX_PROMPT_TITLE_CHARS = 300
_MAX_PROMPT_GOAL_CHARS = 2000

_FIGURE_SELECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "selected": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["id", "reason"],
            },
        },
    },
    "required": ["selected"],
}


# --------------------------------------------------------------------------
# Config helpers (read at call time so tests and the web settings can patch)
# --------------------------------------------------------------------------

def _enabled():
    return bool(getattr(config, "FIGURES_ENABLED", True))


def _pymupdf_available():
    return bool(PYMUPDF_AVAILABLE and pymupdf is not None)


def _dpi(value=None):
    try:
        dpi = int(value if value is not None else getattr(config, "FIGURE_DPI", 150))
    except (TypeError, ValueError):
        dpi = 150
    return min(600, max(30, dpi))


def _max_figures():
    try:
        value = int(getattr(config, "FIGURE_MAX_PER_PAPER", 50))
    except (TypeError, ValueError):
        value = 50
    return max(1, value)


def _max_candidates():
    try:
        value = int(getattr(config, "FIGURE_MAX_CANDIDATES", 100))
    except (TypeError, ValueError):
        value = 100
    return max(1, value)


def _goal_filter(value=None):
    """Resolve the goal filter: explicit argument first, then config."""
    if value is not None:
        return bool(value)
    return bool(getattr(config, "FIGURES_GOAL_FILTER", True))


def _figures_dir():
    return Path(getattr(config, "FIGURES_DIR", config.OUTPUT_DIR / "figures"))


def _index_path(path=None):
    return Path(path or getattr(config, "FIGURES_INDEX_JSON", _figures_dir() / "index.json"))


def _xlsx_path():
    return Path(getattr(config, "FIGURES_XLSX", config.OUTPUT_DIR / "figures.xlsx"))


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# Text / path helpers
# --------------------------------------------------------------------------

def _clean_text(value, limit=None):
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = " ".join(text.split())
    if limit is not None and len(text) > limit:
        text = text[:limit].rstrip()
    return text


def _safe_stem(value):
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = _SAFE_STEM_RE.sub("_", text).strip("._-")
    return text[:60] or "paper"


def _safe_relative(value):
    """Return a normalized POSIX relative path, or "" for unsafe values."""
    if value is None:
        return ""
    text = str(value).strip().replace("\\", "/")
    if not text or "\x00" in text or ":" in text or text.startswith("~"):
        return ""
    path = Path(text)
    if path.is_absolute():
        return ""
    parts = [part for part in path.parts if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        return ""
    return "/".join(parts)


def _safe_figure_path(value, must_exist=True):
    """Resolve a relative index path under FIGURES_DIR, rejecting escapes."""
    relative = _safe_relative(value)
    if not relative:
        return None
    base = _figures_dir()
    candidate = base / relative
    try:
        base_resolved = base.resolve()
        resolved = candidate.resolve()
        resolved.relative_to(base_resolved)
    except (OSError, ValueError, RuntimeError):
        return None
    if resolved == base_resolved:
        return None
    if must_exist and not resolved.is_file():
        return None
    return resolved


def _rect(values):
    if values is None:
        return None
    try:
        if hasattr(values, "x0"):
            x0, y0, x1, y1 = values.x0, values.y0, values.x1, values.y1
        else:
            x0, y0, x1, y1 = (float(v) for v in values[:4])
    except (TypeError, ValueError, IndexError):
        return None
    if x1 < x0 or y1 < y0:
        return None
    if pymupdf is not None:
        return pymupdf.Rect(x0, y0, x1, y1)
    return (x0, y0, x1, y1)


def _rect_union(rects):
    rects = [rect for rect in rects if rect is not None]
    if not rects:
        return None
    x0 = min(rect.x0 for rect in rects)
    y0 = min(rect.y0 for rect in rects)
    x1 = max(rect.x1 for rect in rects)
    y1 = max(rect.y1 for rect in rects)
    return pymupdf.Rect(x0, y0, x1, y1)


def _clamp(rect, page_rect, padding=0.0):
    if rect is None:
        return None
    return pymupdf.Rect(
        max(page_rect.x0, rect.x0 - padding),
        max(page_rect.y0, rect.y0 - padding),
        min(page_rect.x1, rect.x1 + padding),
        min(page_rect.y1, rect.y1 + padding),
    )


def _overlaps_horizontally(left, right):
    return min(left.x1, right.x1) - max(left.x0, right.x0) > 0


# --------------------------------------------------------------------------
# Index persistence
# --------------------------------------------------------------------------

def _clean_int(value, default=0, minimum=0):
    if isinstance(value, bool):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, number)


def _clean_entry(entry):
    if not isinstance(entry, dict):
        return None
    figure_id = str(entry.get("id") or "").strip()
    if not _SAFE_ID_RE.fullmatch(figure_id):
        return None
    paper_key = str(entry.get("paper_key") or "").strip()
    if not paper_key:
        return None
    relative = _safe_relative(entry.get("file"))
    if not relative:
        return None
    kind = str(entry.get("kind") or "caption").strip().casefold()
    if kind not in FIGURE_KINDS:
        kind = "caption"
    page = _clean_int(entry.get("page"), default=1, minimum=1)
    created_at = str(entry.get("created_at") or "").strip() or _now()
    return {
        "id": figure_id,
        "paper_key": paper_key,
        "title": _clean_text(entry.get("title"), 500),
        "label": _clean_text(entry.get("label"), 200),
        "caption": _clean_text(entry.get("caption"), _MAX_CAPTION_CHARS),
        "reason": _clean_text(entry.get("reason"), _MAX_REASON_CHARS),
        "page": page,
        "file": relative,
        "kind": kind,
        "width": _clean_int(entry.get("width"), default=0),
        "height": _clean_int(entry.get("height"), default=0),
        "dpi": _clean_int(entry.get("dpi"), default=150, minimum=1),
        "created_at": created_at,
    }


def _sort_entries(entries):
    return sorted(
        entries,
        key=lambda item: (
            item["paper_key"], item["page"], item["label"], item["id"]
        ),
    )


def _sanitize_entries(entries):
    clean, seen = [], set()
    if isinstance(entries, dict):
        entries = entries.get("figures")
    if not isinstance(entries, (list, tuple)):
        return clean
    for entry in entries:
        item = _clean_entry(entry)
        if item is None or item["id"] in seen:
            continue
        seen.add(item["id"])
        clean.append(item)
    return _sort_entries(clean)


def load_index(path=None):
    """Load the figure index as a sorted list; missing/corrupt files yield []."""
    try:
        data = json.loads(_index_path(path).read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    return _sanitize_entries(data.get("figures"))


def _write_index_unlocked(entries, path=None):
    index_path = _index_path(path)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    temp = index_path.with_name(
        f".{index_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    payload = {"version": INDEX_VERSION, "figures": entries}
    try:
        temp.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(temp, index_path)
    finally:
        try:
            if temp.exists():
                temp.unlink()
        except OSError:
            pass
    return entries


def save_index(entries, path=None):
    """Sanitize and atomically write the index; returns the cleaned list."""
    clean = _sanitize_entries(entries)
    with _INDEX_LOCK:
        _write_index_unlocked(clean, path)
    return clean


def clear_index(path=None):
    """Remove the index file (best-effort)."""
    index_path = _index_path(path)
    with _INDEX_LOCK:
        try:
            index_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            return False
    return True


def clear():
    """Clear the figure index, spreadsheet and cached PNGs.

    Used when the web app replaces the whole dataset with a new search. The CLI
    never calls this: a CLI search merges into the existing dataset, so its
    figures must survive. Orphan files from older versions may remain.
    """
    result = {"cleared": True, "files": 0, "warning": ""}
    with _INDEX_LOCK:
        clear_index()
        try:
            _xlsx_path().unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            result["cleared"] = False
            result["warning"] = f"Could not remove figures.xlsx: {exc}"

        base = _figures_dir()
        try:
            if base.is_dir():
                base_resolved = base.resolve()
                for child in list(base.iterdir()):
                    try:
                        if child.is_dir() and not child.is_symlink():
                            resolved = child.resolve()
                            resolved.relative_to(base_resolved)
                            shutil.rmtree(resolved)
                            result["files"] += 1
                        elif child.suffix.casefold() == ".png":
                            child.unlink()
                            result["files"] += 1
                    except Exception:
                        continue
        except OSError as exc:
            result["cleared"] = False
            result["warning"] = (
                (result["warning"] + " " if result["warning"] else "")
                + f"Could not remove cached figures: {exc}"
            )
    return result


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------

def _page_blocks(page):
    """Return [{'text', 'rect'}] for text blocks on a page."""
    try:
        data = page.get_text("dict")
    except Exception:
        return []
    blocks = []
    for block in (data or {}).get("blocks") or []:
        if not isinstance(block, dict) or block.get("type") not in (0, None):
            continue
        lines = []
        line_rects = []
        for line in block.get("lines") or []:
            if not isinstance(line, dict):
                continue
            text = "".join(
                str(span.get("text") or "")
                for span in (line.get("spans") or [])
                if isinstance(span, dict)
            ).strip()
            if not text:
                continue
            lines.append(text)
            rect = _rect(line.get("bbox"))
            if rect is not None:
                line_rects.append(rect)
        if not lines:
            continue
        rect = _rect(block.get("bbox")) or _rect_union(line_rects)
        if rect is None:
            continue
        blocks.append({"text": _clean_text(" ".join(lines)), "rect": rect})
    return blocks


def _caption_info(text):
    """Return {'label', 'caption'} for a caption-like block, else None."""
    caption = _clean_text(text, _MAX_CAPTION_CHARS)
    match = _CAPTION_RE.match(caption)
    if not match:
        return None
    word = match.group("word").casefold().rstrip(".")
    prefix = "Table" if word == "table" else "Figure"
    number = match.group("number").upper()
    return {"label": f"{prefix} {number}", "caption": caption}


def _image_rects(page):
    rects = []
    seen = set()
    try:
        images = page.get_images(full=True) or []
    except Exception:
        images = []
    for image in images:
        try:
            xref = image[0] if isinstance(image, (list, tuple)) else image.get("xref")
        except Exception:
            continue
        if not xref:
            continue
        try:
            found = page.get_image_rects(xref)
        except Exception:
            continue
        for raw in found or []:
            rect = _rect(raw)
            if rect is None or rect.width <= 1 or rect.height <= 1:
                continue
            key = tuple(round(value, 1) for value in (rect.x0, rect.y0, rect.x1, rect.y1))
            if key in seen:
                continue
            seen.add(key)
            rects.append(rect)
    return rects


def _drawing_rects(page):
    rects = []
    try:
        drawings = page.get_drawings() or []
    except Exception:
        drawings = []
    page_area = page.rect.width * page.rect.height or 1.0
    for drawing in drawings:
        if not isinstance(drawing, dict):
            continue
        rect = _rect(drawing.get("rect"))
        if rect is None or rect.width < 12 or rect.height < 12:
            continue
        # A white page-sized background is not a figure.
        if rect.width * rect.height >= page_area * 0.92:
            continue
        rects.append(rect)
    return rects


def _component_rects(page, blocks, caption_block):
    """Images/drawings plus very close text around one caption.

    Components are tagged so a real image or vector drawing always wins over a
    neighbouring paragraph that merely happens to sit next to the caption.
    """
    components = [(rect, "image") for rect in _image_rects(page)]
    components.extend((rect, "drawing") for rect in _drawing_rects(page))
    caption_rect = caption_block["rect"]
    for block in blocks:
        if block is caption_block:
            continue
        rect = block["rect"]
        if rect.width < 16 or rect.height < 8:
            continue
        gap = min(
            abs(rect.y1 - caption_rect.y0),
            abs(rect.y0 - caption_rect.y1),
        )
        if gap <= 16 and _overlaps_horizontally(rect, caption_rect):
            components.append((rect, "text"))
    return components


def _choose_caption_crop(page, caption_rect, components):
    """Pick an image/drawing crop near the caption, else a vertical band."""
    page_rect = page.rect
    if caption_rect is None:
        return None

    # Prefer actual graphics; only fall back to nearby text when the page has
    # no image/drawing candidate at all (a text-only table, for example).
    graphical = [item for item in components if item[1] != "text"]
    if graphical:
        components = graphical

    above, below = [], []
    max_gap = max(80.0, min(200.0, page_rect.height * 0.25))
    for rect, _source in components:
        if rect is None or rect.width <= 1 or rect.height <= 1:
            continue
        if not _overlaps_horizontally(rect, caption_rect):
            # A wide figure/table can still be relevant without direct overlap.
            if (
                rect.width < page_rect.width * 0.5
                and caption_rect.width < page_rect.width * 0.5
            ):
                continue
        if rect.y1 <= caption_rect.y0 + 2:
            gap = caption_rect.y0 - rect.y1
            if gap <= max_gap:
                above.append((gap, rect))
        elif rect.y0 >= caption_rect.y1 - 2:
            gap = rect.y0 - caption_rect.y1
            if gap <= max_gap:
                below.append((gap, rect))

    candidates = []
    if above:
        candidates.append((min(gap for gap, _rect_ in above), "above", above))
    if below:
        candidates.append((min(gap for gap, _rect_ in below), "below", below))

    if candidates:
        _gap, _side, near = min(candidates, key=lambda item: item[0])
        nearest_gap = min(gap for gap, _ in near)
        chosen = []
        for gap, rect in near:
            if gap > nearest_gap + 30:
                continue
            if chosen and not _overlaps_horizontally(rect, chosen[0][1]):
                chosen_rect = _rect_union([r for _g, r in chosen])
                if chosen_rect is None or not _overlaps_horizontally(rect, chosen_rect):
                    continue
            chosen.append((gap, rect))
        union = _rect_union([rect for _gap, rect in chosen])
        if union is not None:
            crop = _clamp(union, page_rect, padding=8.0)
            if _valid_crop(crop, page_rect):
                return crop

    # No usable component: take a band above the caption when it sits in the
    # lower half (typical figure caption), otherwise below it (typical table
    # caption). The band is capped so we never turn every caption into a page.
    center = (caption_rect.y0 + caption_rect.y1) / 2
    band_height = min(page_rect.height * 0.45, 320.0)
    margin_x = max(6.0, page_rect.width * 0.04)
    if center >= page_rect.height / 2:
        y1 = caption_rect.y0 - 6
        y0 = max(page_rect.y0 + 4, y1 - band_height)
    else:
        y0 = caption_rect.y1 + 6
        y1 = min(page_rect.y1 - 4, y0 + band_height)
    crop = _clamp(
        pymupdf.Rect(margin_x, y0, page_rect.width - margin_x, y1),
        page_rect,
    )
    if _valid_crop(crop, page_rect):
        return crop
    return None


def _valid_crop(rect, page_rect):
    if rect is None:
        return False
    if rect.width < _MIN_CROP_WIDTH or rect.height < _MIN_CROP_HEIGHT:
        return False
    if rect.x0 < page_rect.x0 - 0.5 or rect.x1 > page_rect.x1 + 0.5:
        return False
    if rect.y0 < page_rect.y0 - 0.5 or rect.y1 > page_rect.y1 + 0.5:
        return False
    return True


def _large_image_candidates(page, page_number):
    """Large embedded images on pages that have no caption."""
    page_rect = page.rect
    page_area = page_rect.width * page_rect.height or 1.0
    candidates = []
    for rect in _image_rects(page):
        if rect.width < 120 or rect.height < 100:
            if rect.width * rect.height < page_area * 0.06:
                continue
        crop = _clamp(rect, page_rect, padding=6.0)
        if not _valid_crop(crop, page_rect):
            continue
        candidates.append({
            "kind": "image",
            "rect": crop,
            "label": f"Image p.{page_number}",
            "caption": "",
        })
    return candidates


def _page_candidates(page, page_number, blocks):
    """All figure candidates for one page, in reading/definition order."""
    candidates = []
    caption_blocks = []
    for block in blocks:
        info = _caption_info(block["text"])
        if info is None:
            continue
        caption_blocks.append(block)
        crop = None
        try:
            components = _component_rects(page, blocks, block)
            crop = _choose_caption_crop(page, block["rect"], components)
        except Exception:
            crop = None
        kind = "caption"
        if crop is None:
            crop = page.rect
            kind = "page"
        candidates.append({
            "kind": kind,
            "rect": crop,
            "label": info["label"],
            "caption": info["caption"],
        })

    if not caption_blocks:
        candidates.extend(_large_image_candidates(page, page_number))
    return candidates


def _render_png(page, rect, dpi):
    scale = dpi / 72.0
    matrix = pymupdf.Matrix(scale, scale)
    clip = pymupdf.Rect(rect.x0, rect.y0, rect.x1, rect.y1)
    pixmap = page.get_pixmap(matrix=matrix, clip=clip, alpha=False)
    if pixmap.width <= 0 or pixmap.height <= 0:
        return None, 0, 0
    png = pixmap.tobytes("png")
    return png, pixmap.width, pixmap.height


def _figure_id(paper_key, page_number, kind, png_bytes):
    digest = hashlib.sha256()
    digest.update(str(paper_key).encode("utf-8", "replace"))
    digest.update(b"|")
    digest.update(str(page_number).encode("ascii"))
    digest.update(b"|")
    digest.update(str(kind).encode("ascii"))
    digest.update(b"|")
    digest.update(png_bytes)
    return digest.hexdigest()[:24]


def _write_png(target, png_bytes):
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_bytes(png_bytes)
        os.replace(temp, target)
    finally:
        try:
            if temp.exists():
                temp.unlink()
        except OSError:
            pass
    return target


# --------------------------------------------------------------------------
# Goal-driven selection
# --------------------------------------------------------------------------

def _candidate_prompt(candidates, goal, title=""):
    """Build the figure-selection prompt from candidate metadata only."""
    clean_goal = _clean_text(goal, _MAX_PROMPT_GOAL_CHARS)
    clean_title = _clean_text(title, _MAX_PROMPT_TITLE_CHARS)
    lines = [
        "You are selecting figures and tables from one research paper for a "
        "data-extraction task.",
        f'Extraction goal: "{clean_goal}"',
        f'Paper title: "{clean_title}"',
        "",
        "Candidate figures/tables:",
    ]
    for candidate in candidates:
        candidate_id = str(candidate.get("id") or "").strip()
        label = _clean_text(candidate.get("label"), 200) or "(no label)"
        page = candidate.get("page_number")
        if page is None:
            page = candidate.get("page")
        kind = _clean_text(candidate.get("kind"), 40) or "caption"
        caption = _clean_text(
            candidate.get("caption"), _MAX_PROMPT_CAPTION_CHARS
        ) or "(no caption text)"
        lines.append(
            f"- id: {candidate_id} | label: {label} | page: {page} | "
            f"kind: {kind} | caption: {caption}"
        )
    lines.extend([
        "",
        "Rules:",
        "- Select only candidates that directly help answer the extraction goal.",
        "- Do not select figures merely because they exist.",
        "- If the goal does not ask for figures/images/tables/visual data, "
        "return an empty list.",
        "- Return an empty list if no figures are requested by the goal.",
        "- Use only the ids listed above; never invent an id.",
        "- Keep each reason to one short sentence.",
        f"- Select at most {_max_figures()} candidate(s).",
        "",
        'Return a JSON object with one key, "selected", whose value is an array '
        'of objects like {"id": "c1", "reason": "why it helps answer the goal"}. '
        "Return ONLY the JSON object (no prose, no markdown).",
    ])
    return "\n".join(lines)


def select_figures_for_goal(candidates, goal, title=""):
    """Ask the configured LLM which candidates help answer the extraction goal.

    Returns ``{"selected": [{"id", "reason"}], "warning": str, "capped": bool,
    "goal_empty": bool}``. Model output is validated against the candidate ids,
    deduped and capped at ``FIGURE_MAX_PER_PAPER``. Backend/parse failures are
    reported as a warning with an empty selection; this function never raises.
    """
    result = {
        "selected": [],
        "warning": "",
        "capped": False,
        "goal_empty": False,
    }
    clean_goal = _clean_text(goal, _MAX_PROMPT_GOAL_CHARS)
    if not clean_goal:
        result["goal_empty"] = True
        result["warning"] = (
            "No figures were requested because the extraction goal is empty."
        )
        return result

    known = {}
    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        candidate_id = str(candidate.get("id") or "").strip()
        if candidate_id and candidate_id not in known:
            known[candidate_id] = candidate
    if not known:
        return result

    try:
        suggester._require_backend()
        raw = suggester._call_llm(
            _candidate_prompt(list(known.values()), clean_goal, title),
            _FIGURE_SELECTION_SCHEMA,
        )
    except suggester.BackendUnavailable as exc:
        result["warning"] = (
            "Figure selection was skipped because the LLM backend is "
            f"unavailable: {exc}"
        )
        return result
    except Exception as exc:  # noqa: BLE001 - figures must never fail extraction
        result["warning"] = f"Figure selection failed: {exc}"
        return result

    try:
        data = suggester._load_json_object(raw or "")
    except Exception as exc:  # noqa: BLE001 - malformed output is non-fatal
        result["warning"] = f"Could not parse the figure selection: {exc}"
        return result
    if not isinstance(data, dict):
        result["warning"] = "The figure-selection response was not valid JSON."
        return result
    items = data.get("selected")
    if not isinstance(items, list):
        result["warning"] = (
            'The figure-selection response did not contain a "selected" list.'
        )
        return result

    selected, seen = [], set()
    unknown = duplicates = 0
    for item in items:
        if isinstance(item, str):
            candidate_id, reason = item, ""
        elif isinstance(item, dict):
            candidate_id = item.get("id")
            reason = item.get("reason")
        else:
            continue
        candidate_id = str(candidate_id or "").strip()
        if not candidate_id:
            continue
        if candidate_id in seen:
            duplicates += 1
            continue
        seen.add(candidate_id)
        if candidate_id not in known:
            unknown += 1
            continue
        selected.append({
            "id": candidate_id,
            "reason": _clean_text(reason, _MAX_REASON_CHARS),
        })

    notes = []
    if unknown:
        notes.append(f"Ignored {unknown} unknown figure id(s) from the model.")
    if duplicates:
        notes.append(f"Ignored {duplicates} duplicate figure id(s) from the model.")
    result["warning"] = " ".join(notes)

    cap = _max_figures()
    if len(selected) > cap:
        selected = selected[:cap]
        result["capped"] = True
    result["selected"] = selected
    return result


def _detect_stats(**values):
    stats = {
        "candidates": 0,
        "matched": 0,
        "skipped": 0,
        "duplicates": 0,
        "capped": False,
        "candidate_capped": False,
        "goal_filtered": False,
        "goal_empty": False,
        "warning": "",
    }
    stats.update(values)
    return stats


def _detect(pdf_path, paper_key, title="", dpi=None, goal=None, goal_filter=None):
    """Internal detector: returns ``(entries, stats)`` and may raise.

    Candidates are collected while the PyMuPDF document is open, the extraction
    goal selects a subset (unless ``goal`` is None or filtering is disabled),
    and only selected candidates are rendered.
    """
    key = str(paper_key or "").strip()
    filtering = _goal_filter(goal_filter)
    goal_text = None if goal is None else _clean_text(goal, _MAX_PROMPT_GOAL_CHARS)
    goal_filtered = bool(filtering and goal is not None)
    goal_empty = bool(goal_filtered and not goal_text)
    if not _enabled() or not _pymupdf_available() or not key:
        return [], _detect_stats(goal_filtered=goal_filtered)
    source = Path(str(pdf_path)).expanduser()
    if not source.is_file():
        return [], _detect_stats(
            goal_filtered=goal_filtered,
            warning="PDF file not found; no figures were detected.",
        )
    if goal_empty:
        return [], _detect_stats(goal_filtered=True, goal_empty=True)

    dpi = _dpi(dpi)
    max_figures = _max_figures()
    max_candidates = _max_candidates() if goal_filtered else None
    stem = _safe_stem(key)
    seen = set()
    entries = []
    duplicates = 0
    capped = False
    candidate_capped = False
    scanning_warnings = []
    candidates = []

    document = pymupdf.open(str(source))
    try:
        for page_index, page in enumerate(document):
            page_number = page_index + 1
            try:
                blocks = _page_blocks(page)
                page_candidates = _page_candidates(page, page_number, blocks)
            except Exception as exc:  # one malformed page must not lose the rest
                scanning_warnings.append(
                    f"Could not scan page {page_number}: {exc}"
                )
                continue

            for item in page_candidates:
                if max_candidates is not None and len(candidates) >= max_candidates:
                    candidate_capped = True
                    break
                candidates.append({
                    "id": f"c{len(candidates) + 1}",
                    "page_number": page_number,
                    "page": page,
                    "rect": item["rect"],
                    "kind": item["kind"],
                    "label": _clean_text(item.get("label"), 200),
                    "caption": _clean_text(
                        item.get("caption"), _MAX_CAPTION_CHARS
                    ),
                    "text": _clean_text(item.get("text"), _MAX_CAPTION_CHARS),
                })
            if candidate_capped:
                break

        selection = {"selected": [], "warning": "", "capped": False}
        if goal_filtered:
            selection = select_figures_for_goal(
                candidates, goal_text, title=title
            )
            selected_ids = [item["id"] for item in selection["selected"]]
        else:
            selected_ids = [candidate["id"] for candidate in candidates]
        reasons = {
            item["id"]: item.get("reason", "")
            for item in selection.get("selected", [])
        }
        selected_set = set(selected_ids)
        selected_candidates = [
            candidate for candidate in candidates
            if candidate["id"] in selected_set
        ]

        for candidate in selected_candidates:
            if len(entries) >= max_figures:
                capped = True
                break
            page = candidate["page"]
            page_number = candidate["page_number"]
            try:
                png, width, height = _render_png(
                    page, candidate["rect"], dpi
                )
            except Exception as exc:
                scanning_warnings.append(
                    f"Could not render page {page_number}: {exc}"
                )
                continue
            if not png:
                continue
            figure_id = _figure_id(key, page_number, candidate["kind"], png)
            if figure_id in seen:
                duplicates += 1
                continue
            seen.add(figure_id)
            relative = f"{stem}/fig_{figure_id}.png"
            target = _safe_figure_path(relative, must_exist=False)
            if target is None:
                scanning_warnings.append(
                    "Could not resolve the figure output path."
                )
                continue
            if target.is_file():
                duplicates += 1
            else:
                try:
                    _write_png(target, png)
                except OSError as exc:
                    scanning_warnings.append(
                        f"Could not save a figure PNG: {exc}"
                    )
                    continue
            entries.append({
                "id": figure_id,
                "paper_key": key,
                "title": _clean_text(title, 500),
                "label": candidate["label"],
                "caption": candidate["caption"],
                "reason": reasons.get(candidate["id"], ""),
                "page": page_number,
                "file": relative,
                "kind": candidate["kind"],
                "width": width,
                "height": height,
                "dpi": dpi,
                "created_at": _now(),
            })
    finally:
        try:
            document.close()
        except Exception:
            pass

    warnings = scanning_warnings
    if selection.get("warning"):
        warnings.append(str(selection["warning"]))
    matched = len(selected_ids)
    return _sort_entries(entries), _detect_stats(
        candidates=len(candidates),
        matched=matched,
        skipped=max(len(candidates) - matched, 0),
        duplicates=duplicates,
        capped=bool(capped or selection.get("capped")),
        candidate_capped=candidate_capped,
        goal_filtered=goal_filtered,
        goal_empty=goal_empty,
        warning=" ".join(warnings),
    )


def detect_figures(pdf_path, paper_key, title="", dpi=None, goal=None,
                   goal_filter=None):
    """Detect and render figures for one PDF.

    Returns the list of unique figure entries (including entries whose PNG was
    already cached). A non-empty ``goal`` selects only matching candidates;
    ``goal=None`` keeps the legacy all-figures behaviour. Never raises:
    disabled detection, a missing PDF or an unreadable document all yield
    ``[]``.
    """
    try:
        entries, _stats = _detect(
            pdf_path,
            paper_key,
            title=title,
            dpi=dpi,
            goal=goal,
            goal_filter=goal_filter,
        )
        return entries
    except Exception:
        return []


# --------------------------------------------------------------------------
# Public summary / API helpers
# --------------------------------------------------------------------------

def _summary(paper_key="", **values):
    summary = {
        "paper_key": str(paper_key or ""),
        "saved": 0,
        "candidates": 0,
        "selected": 0,
        "matched": 0,
        "skipped": 0,
        "duplicates": 0,
        "capped": False,
        "candidate_capped": False,
        "goal_filtered": False,
        "goal_empty": False,
        "warning": "",
        "kinds": {},
    }
    summary.update(values)
    return summary


def _kind_counts(entries):
    counts = {}
    for entry in entries or []:
        kind = entry.get("kind") or "caption"
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def process_paper(pdf_path, paper_key, title="", pdf_file="", pdf_source="",
                  goal=None, goal_filter=None):
    """Select goal-relevant figures, replace this paper's index and write Excel.

    ``goal=None`` keeps the legacy all-figures behaviour. With goal filtering
    enabled (the default), a non-empty goal is sent to the configured LLM and
    only matching candidates are rendered; an empty goal saves no figures.
    Returns a summary dict; every failure is reported as a warning instead of
    being raised into the extraction job.
    """
    key = str(paper_key or "").strip()
    summary = _summary(
        key,
        pdf_file=str(pdf_file or ""),
        pdf_source=str(pdf_source or "none"),
        goal_filtered=bool(_goal_filter(goal_filter) and goal is not None),
    )
    if not _enabled():
        summary["warning"] = "Figure detection is disabled (FIGURES_ENABLED = False)."
        return summary
    if not _pymupdf_available():
        summary["warning"] = (
            "PyMuPDF is not installed; figure screenshots are unavailable."
        )
        return summary
    if not key:
        summary["warning"] = "No paper key; figures cannot be indexed."
        return summary
    if not Path(str(pdf_path or "")).expanduser().is_file():
        summary["warning"] = "PDF file not found; no figures were detected."
        return summary

    try:
        entries, stats = _detect(
            pdf_path,
            key,
            title=title,
            goal=goal,
            goal_filter=goal_filter,
        )
    except Exception as exc:  # noqa: BLE001 - figures must never fail extraction
        summary["warning"] = f"Figure detection failed: {exc}"
        return summary

    summary["candidates"] = int(stats.get("candidates") or 0)
    summary["selected"] = int(stats.get("matched") or 0)
    summary["matched"] = int(stats.get("matched") or 0)
    summary["skipped"] = int(stats.get("skipped") or 0)
    summary["duplicates"] = int(stats.get("duplicates") or 0)
    summary["capped"] = bool(stats.get("capped"))
    summary["candidate_capped"] = bool(stats.get("candidate_capped"))
    summary["goal_filtered"] = bool(stats.get("goal_filtered"))
    summary["goal_empty"] = bool(stats.get("goal_empty"))
    if stats.get("warning"):
        summary["warning"] = str(stats["warning"])

    try:
        with _INDEX_LOCK:
            current = [
                entry for entry in load_index()
                if entry.get("paper_key") != key
            ]
            current.extend(entries)
            current = save_index(current)
        write_figures_xlsx(current)
    except Exception as exc:  # noqa: BLE001 - index/xlsx are best-effort
        message = f"Could not update the figure index: {exc}"
        summary["warning"] = (
            summary["warning"] + " " if summary["warning"] else ""
        ) + message

    summary["saved"] = len(entries)
    summary["kinds"] = _kind_counts(entries)
    return summary


def summary_message(summary):
    """Compact human-readable line for the CLI/web job log."""
    if not isinstance(summary, dict):
        return ""
    saved = int(summary.get("saved") or 0)
    duplicates = int(summary.get("duplicates") or 0)
    kinds = summary.get("kinds") or {}
    candidates = int(summary.get("candidates") or 0)
    matched = int(summary.get("matched") or 0)
    filtered = bool(summary.get("goal_filtered"))
    goal_empty = bool(summary.get("goal_empty"))

    if saved:
        detail = ", ".join(
            f"{count} {kind}" for kind, count in sorted(kinds.items()) if count
        )
        if filtered:
            text = (
                f"{saved} figure screenshot(s) matched the extraction goal "
                f"(from {candidates} candidate(s))"
            )
        else:
            text = f"{saved} figure screenshot(s) saved"
        if detail:
            text += f" ({detail})"
    elif goal_empty and filtered:
        text = (
            "No figures saved: the extraction goal is empty, so no figures "
            "were requested."
        )
    elif filtered and candidates and not matched:
        text = (
            f"No figures matched the extraction goal "
            f"({candidates} candidate(s) skipped)."
        )
    else:
        text = "No figures detected."
    if duplicates:
        text = text.rstrip(".") + f", {duplicates} duplicate(s) skipped"
    if summary.get("capped"):
        text = text.rstrip(".") + f", capped at {_max_figures()}"
    if summary.get("candidate_capped"):
        text = (
            text.rstrip(".")
            + f", candidate list capped at {_max_candidates()}"
        )
    warning = str(summary.get("warning") or "").strip()
    if warning:
        return f"Figure warning: {text} — {warning}" if saved else f"Figure warning: {warning}"
    return text[0].upper() + text[1:] if text else ""


def api_list(paper_key=None):
    """Return index entries whose PNG exists, optionally filtered by paper."""
    key = str(paper_key or "").strip()
    items = []
    for entry in load_index():
        if key and entry["paper_key"] != key:
            continue
        if _safe_figure_path(entry["file"]) is None:
            continue
        items.append(dict(entry))
    return items


def resolve_figure_path(figure_id):
    """Resolve a figure id to a safe, existing PNG path (or None)."""
    value = str(figure_id or "").strip()
    if not value or not _SAFE_ID_RE.fullmatch(value):
        return None
    for entry in load_index():
        if entry["id"] != value:
            continue
        return _safe_figure_path(entry["file"])
    return None


def write_figures_xlsx(entries=None):
    """Write figures.xlsx from the supplied entries or the current index."""
    items = load_index() if entries is None else _sanitize_entries(entries)
    writer.write_xlsx(_xlsx_path(), FIGURE_COLUMNS, items)
    return _xlsx_path()
