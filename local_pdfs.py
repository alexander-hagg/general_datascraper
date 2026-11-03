"""
local_pdfs.py — use a folder of PDFs as a search source.

The module scans a user-selected folder, reads basic metadata with pypdf, and
copies (or hardlinks) every usable PDF into ``output/pdfs/`` so extraction,
viewing and future workspaces stay self-contained.

Public API:

    normalize_doi(value) / normalize_title(value)
    validate_folder(folder) -> Path
    scan_folder(folder, recursive=False, max_files=None) -> (papers, stats)
    merge_local_papers(papers, local_papers) -> list
    load_index() / save_index(index) / write_index(papers, replace=True)
    attach_to_papers(papers) -> list
    existing_local_path(paper) -> Path | None

The persisted index (``output/local_pdfs.json``) maps a paper key to the
archived filename. It lets a later Extract run re-attach local PDFs to rows
loaded from ``search_results.xlsx`` without rescanning the source folder.

Scanned PDFs are never treated as online/open-access records: they carry
``sources=["local"]`` and local-only metadata. Matching against online results
uses normalized DOI first, then normalized title, so punctuation/case/whitespace
differences never create duplicate rows.
"""

import hashlib
import json
import os
import re
import shutil
import unicodedata
from pathlib import Path

from pypdf import PdfReader

import config
import searchers

PDF_MAGIC = b"%PDF-"
PDF_SUFFIX = ".pdf"
FIRST_PAGES_FOR_TEXT = 3
INDEX_FILENAME_DIGEST = 10

_DOI_RE = re.compile(r"10\.\d+/[^\s\"'<>]+", re.IGNORECASE)
_YEAR_RE = re.compile(r"(?<!\d)\d{4}(?!\d)")
_DOI_PREFIX_RE = re.compile(
    r"^(?:doi\s*:\s*|https?://(?:www\.)?(?:dx\.)?doi\.org/|doi\.org/)",
    re.IGNORECASE,
)
_SAFE_STEM_RE = re.compile(r"[^A-Za-z0-9._-]+")


# --------------------------------------------------------------------------
# Normalization helpers
# --------------------------------------------------------------------------

def normalize_doi(value):
    """Return a comparable lowercase DOI, or "" when the value is not a DOI.

    Accepts bare DOIs and common decorated forms such as ``doi:10.1/ABC`` or
    ``https://doi.org/10.1/ABC``. Trailing sentence punctuation is removed.
    """
    if value is None:
        return ""
    text = " ".join(str(value).split()).strip()
    if not text:
        return ""
    text = _DOI_PREFIX_RE.sub("", text).strip()
    text = text.lstrip("<>\"'([{").rstrip(".,;:").rstrip("<>\"'")
    # Drop closing brackets that only surround a DOI inside prose, but keep
    # balanced ones that are part of the DOI itself.
    for closing, opening in ((")", "("), ("]", "["), ("}", "{")):
        while text.endswith(closing) and text.count(closing) > text.count(opening):
            text = text[:-1].rstrip(".,;:")
    if not re.fullmatch(r"10\.\d+/\S+", text):
        return ""
    return text.casefold()


def normalize_title(value):
    """Normalize a title for matching: case, punctuation and whitespace free."""
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).casefold()
    text = re.sub(r"[\W_]+", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def _clean_text(value):
    if value is None:
        return ""
    return " ".join(str(value).replace("\x00", " ").split()).strip()


def _safe_stem(value):
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = _SAFE_STEM_RE.sub("_", text).strip("._-")
    return text[:100] or "paper"


def _year_from_text(value):
    text = str(value or "")
    # PDF creation dates look like D:20190501120000Z: the year is followed by
    # more digits, so a word-boundary regex alone would not see it.
    match = re.search(r"D:(\d{4})", text, re.IGNORECASE)
    if match:
        return match.group(1)
    match = _YEAR_RE.search(text)
    return match.group(0) if match else ""


def _title_from_filename(path):
    stem = Path(path).stem
    return _clean_text(re.sub(r"[_-]+", " ", stem))


# --------------------------------------------------------------------------
# Folder scanning
# --------------------------------------------------------------------------

def validate_folder(folder):
    """Expand and validate a local PDF folder.

    Raises ``ValueError`` with a user-facing message for a blank path, a missing
    path, or a path that is not a directory. Returns the expanded ``Path``.
    """
    if folder is None or not str(folder).strip():
        raise ValueError("Choose a local PDF folder.")
    path = Path(str(folder).strip()).expanduser()
    if not path.exists():
        raise ValueError(f"Local PDF folder does not exist: {path}")
    if not path.is_dir():
        raise ValueError(f"Local PDF path is not a folder: {path}")
    return path


def _is_under(path, parent):
    try:
        Path(path).resolve().relative_to(Path(parent).resolve())
        return True
    except (ValueError, OSError):
        return False


def _iter_pdf_paths(folder, recursive):
    iterator = folder.rglob("*") if recursive else folder.iterdir()
    paths = [
        path
        for path in iterator
        if path.is_file() and path.suffix.casefold() == PDF_SUFFIX
    ]
    return sorted(paths, key=lambda p: str(p).casefold())


def _has_pdf_magic(path):
    try:
        with open(path, "rb") as handle:
            return handle.read(1024).lstrip().startswith(PDF_MAGIC)
    except OSError:
        return False


def _metadata_dict(reader):
    try:
        metadata = reader.metadata
    except Exception:
        return {}
    if not metadata:
        return {}
    try:
        return dict(metadata)
    except Exception:
        return {}


def _metadata_doi(metadata):
    for value in (metadata or {}).values():
        match = _DOI_RE.search(str(value or ""))
        if match:
            doi = normalize_doi(match.group(0))
            if doi:
                return doi
    return ""


def _page_text(reader, limit=FIRST_PAGES_FOR_TEXT):
    parts = []
    for page in list(reader.pages)[:limit]:
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            continue
    return "\n".join(parts)


def _read_pdf_metadata(path):
    """Read one PDF's metadata/text, returning a local paper dict.

    Raises when the PDF cannot be parsed; the caller records that in stats and
    keeps scanning.
    """
    source = Path(path)
    reader = PdfReader(str(source))
    if getattr(reader, "is_encrypted", False):
        # pypdf can open encrypted files only after decrypting (even an empty
        # password). If that is not possible, treat the file as an error.
        if reader.decrypt("") == 0:
            raise ValueError("encrypted PDF (password required)")

    metadata = _metadata_dict(reader)
    text = _page_text(reader)
    doi = _metadata_doi(metadata) or _doi_from_text(text)

    title = _clean_text(
        metadata.get("/Title") or metadata.get("/title") or ""
    )
    if not title:
        title = _title_from_filename(source)
    authors = _clean_text(
        metadata.get("/Author") or metadata.get("/author") or ""
    )
    year = _year_from_text(
        metadata.get("/CreationDate") or metadata.get("/creationdate") or ""
    )
    if not year:
        year = _year_from_text(source.stem)

    paper = {
        "sources": ["local"],
        "title": title,
        "authors": authors,
        "abstract": "",
        "doi": doi,
        "pmid": "",
        "pmcid": "",
        "year": year,
        "journal": "",
        "publication_type": "local PDF",
        "is_oa": True,
        "fulltext_url": "",
        "local_pdf": "",
        "local_pdf_path": "",
        "local_pdf_original": str(source),
    }
    paper["paper_key"] = searchers.paper_key(paper)
    return paper


def _doi_from_text(text):
    for match in _DOI_RE.finditer(text or ""):
        doi = normalize_doi(match.group(0))
        if doi:
            return doi
    return ""


def _archive_filename(source):
    """Deterministic, collision-safe archived name for one source path."""
    raw = str(Path(source).expanduser().resolve())
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:INDEX_FILENAME_DIGEST]
    return f"local_{digest}_{_safe_stem(Path(source).stem)}.pdf"


def _archive_pdf(source, dest_dir):
    """Link or copy one PDF into ``dest_dir``; returns (filename, mode).

    ``mode`` is ``"linked"`` or ``"copied"``. The temporary file + os.replace
    sequence keeps an existing archived copy safe if the operation fails.
    """
    source = Path(source)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    filename = _archive_filename(source)
    dest = dest_dir / filename
    temp = dest.with_name(dest.name + ".tmp")

    mode = "linked"
    try:
        if temp.exists():
            temp.unlink()
        try:
            os.link(source, temp)
        except OSError:
            mode = "copied"
            shutil.copy2(source, temp)
        os.replace(temp, dest)
    finally:
        try:
            if temp.exists():
                temp.unlink()
        except OSError:
            pass
    return filename, mode


def _empty_stats(folder, recursive):
    return {
        "folder": str(folder),
        "recursive": bool(recursive),
        "found": 0,
        "scanned": 0,
        "copied": 0,
        "linked": 0,
        "skipped": 0,
        "duplicates": 0,
        "errors": 0,
        "error_details": [],
    }


def scan_folder(folder, recursive=False, max_files=None):
    """Scan a folder of PDFs and archive every unique valid file.

    Returns ``(papers, stats)``. ``papers`` are searcher-compatible dicts with
    ``sources=["local"]`` and local attachment fields. ``stats`` is JSON-friendly
    and always includes ``scanned``, ``copied``, ``linked``, ``skipped``,
    ``duplicates`` and ``errors``.

    Raises ``ValueError`` (before reading any PDF) for an invalid folder or when
    the number of candidate PDFs exceeds ``max_files``. Broken/encrypted PDFs
    are counted in ``stats`` and never abort the scan.
    """
    folder_path = validate_folder(folder)
    cap = config.MAX_LOCAL_PDFS if max_files is None else max_files
    try:
        cap = int(cap)
    except (TypeError, ValueError):
        raise ValueError("MAX_LOCAL_PDFS must be a whole number.")
    if cap < 1:
        raise ValueError("MAX_LOCAL_PDFS must be at least 1.")

    try:
        all_paths = _iter_pdf_paths(folder_path, recursive)
    except OSError as e:
        raise ValueError(f"Could not read local PDF folder {folder_path}: {e}")

    pdf_dir = getattr(config, "PDF_DIR", None)
    candidates = []
    stats = _empty_stats(folder_path, recursive)
    for path in all_paths:
        if pdf_dir and _is_under(path, pdf_dir):
            stats["skipped"] += 1
            continue
        candidates.append(path)
    stats["found"] = len(candidates)

    if len(candidates) > cap:
        raise ValueError(
            f"Found {len(candidates)} PDF files in {folder_path}, but the limit is "
            f"{cap}. Raise MAX_LOCAL_PDFS in config.py or choose a smaller folder."
        )

    papers = []
    by_doi, by_title = {}, {}
    for path in candidates:
        stats["scanned"] += 1
        if not _has_pdf_magic(path):
            stats["errors"] += 1
            stats["error_details"].append(
                {"file": str(path), "error": "not a valid PDF (missing %PDF- header)"}
            )
            continue
        try:
            paper = _read_pdf_metadata(path)
        except Exception as e:
            stats["errors"] += 1
            stats["error_details"].append({"file": str(path), "error": str(e)})
            continue

        doi = normalize_doi(paper.get("doi"))
        title = normalize_title(paper.get("title"))
        if (doi and doi in by_doi) or (title and title in by_title):
            stats["duplicates"] += 1
            continue

        try:
            filename, mode = _archive_pdf(path, pdf_dir)
        except Exception as e:
            stats["errors"] += 1
            stats["error_details"].append({"file": str(path), "error": str(e)})
            continue

        paper["local_pdf"] = filename
        paper["local_pdf_path"] = str(Path(pdf_dir) / filename)
        paper["local_pdf_original"] = str(path)
        papers.append(paper)
        stats[mode] += 1

        if doi:
            by_doi[doi] = paper
        if title:
            by_title[title] = paper

    stats["papers"] = len(papers)
    return papers, stats


# --------------------------------------------------------------------------
# Persisted index
# --------------------------------------------------------------------------

def _index_path(path=None):
    return Path(path or config.LOCAL_PDF_INDEX_JSON)


def load_index(path=None):
    """Load the local-PDF index; missing/corrupt files yield an empty mapping."""
    index_path = _index_path(path)
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    index = {}
    for key, entry in data.items():
        if not isinstance(entry, dict):
            continue
        filename = str(entry.get("filename") or "").strip()
        if not key or not filename:
            continue
        index[str(key)] = {
            "filename": Path(filename).name,
            "original_path": str(entry.get("original_path") or ""),
            "title": str(entry.get("title") or ""),
            "source": "local",
        }
    return index


def save_index(index, path=None):
    """Write the index atomically as UTF-8 JSON."""
    index_path = _index_path(path)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    clean = {}
    for key, entry in (index or {}).items():
        if not isinstance(entry, dict):
            continue
        filename = str(entry.get("filename") or "").strip()
        if not key or not filename:
            continue
        clean[str(key)] = {
            "filename": Path(filename).name,
            "original_path": str(entry.get("original_path") or ""),
            "title": str(entry.get("title") or ""),
            "source": "local",
        }
    temp = index_path.with_name(index_path.name + ".tmp")
    try:
        temp.write_text(
            json.dumps(clean, indent=2, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temp, index_path)
    finally:
        try:
            if temp.exists():
                temp.unlink()
        except OSError:
            pass
    return clean


def _paper_index_key(paper):
    stored = str(paper.get("paper_key") or "").strip()
    if stored:
        return stored
    if paper.get("pmid") or paper.get("doi") or str(paper.get("title") or "").strip():
        return searchers.paper_key(paper)
    return ""


def _index_entry(paper):
    return {
        "filename": Path(str(paper.get("local_pdf"))).name,
        "original_path": str(paper.get("local_pdf_original") or ""),
        "title": str(paper.get("title") or ""),
        "source": "local",
    }


def attach_to_papers(papers, index=None, pdf_dir=None):
    """Attach archived local PDFs to papers loaded from Excel/search results.

    Only entries whose archived file still exists are applied, so a deleted or
    stale index entry never makes a paper look like it has a local PDF.
    """
    if index is None:
        index = load_index()
    pdf_dir = Path(pdf_dir or config.PDF_DIR)
    papers = list(papers or [])
    for paper in papers:
        if not isinstance(paper, dict):
            continue
        key = _paper_index_key(paper)
        entry = index.get(key) if key else None
        if not entry:
            continue
        filename = Path(str(entry.get("filename") or "")).name
        if not filename:
            continue
        path = pdf_dir / filename
        if not path.is_file():
            continue
        paper["local_pdf"] = filename
        paper["local_pdf_path"] = str(path)
        if entry.get("original_path"):
            paper["local_pdf_original"] = entry["original_path"]
    return papers


def existing_local_path(paper, pdf_dir=None):
    """Return an existing local PDF path for `paper`, or None.

    Prefers the stored absolute path but falls back to the archived filename in
    ``config.PDF_DIR`` (useful when the output folder moved).
    """
    if not isinstance(paper, dict):
        return None
    pdf_dir = Path(pdf_dir or config.PDF_DIR)
    filename = Path(str(paper.get("local_pdf") or "")).name
    candidates = []
    raw_path = str(paper.get("local_pdf_path") or "").strip()
    if raw_path:
        candidates.append(Path(raw_path).expanduser())
    if filename:
        candidates.append(pdf_dir / filename)
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def write_index(papers, path=None, replace=True, pdf_dir=None):
    """Write the local-PDF index for a final paper set.

    ``replace=True`` clears stale entries (web searches replace the dataset);
    ``replace=False`` merges into the existing index, preserving entries for
    papers that are not in `papers` (useful when only new local papers are
    known). Papers whose archived file is missing are not indexed.
    """
    pdf_dir = Path(pdf_dir or config.PDF_DIR)
    entries = {}
    for paper in papers or []:
        if not isinstance(paper, dict):
            continue
        filename = Path(str(paper.get("local_pdf") or "")).name
        if not filename:
            continue
        if not (pdf_dir / filename).is_file():
            continue
        key = _paper_index_key(paper)
        if not key:
            continue
        entries[key] = _index_entry(paper)

    index = entries if replace else {**load_index(path), **entries}
    return save_index(index, path)


# --------------------------------------------------------------------------
# Matching local PDFs to online papers
# --------------------------------------------------------------------------

def _merge_local_into(existing, local):
    for field in ("title", "authors", "doi", "year", "journal"):
        if not existing.get(field) and local.get(field):
            existing[field] = local[field]

    existing["local_pdf"] = local.get("local_pdf") or existing.get("local_pdf", "")
    existing["local_pdf_path"] = (
        local.get("local_pdf_path") or existing.get("local_pdf_path", "")
    )
    existing["local_pdf_original"] = (
        local.get("local_pdf_original") or existing.get("local_pdf_original", "")
    )

    existing["sources"] = sorted(
        set(existing.get("sources") or []) | {"local"}
    )


def merge_local_papers(papers, local_papers):
    """Merge local PDFs into online papers by DOI first, then normalized title.

    Matched online rows keep their own metadata/paper_key and gain the local
    attachment plus any missing fields. Unmatched local papers are appended.
    The input lists are not mutated beyond the paper dicts returned by the
    caller (the online dicts themselves are updated in place, matching
    ``searchers.merge_papers`` style).
    """
    result = list(papers or [])
    by_doi, by_title = {}, {}
    for index, paper in enumerate(result):
        doi = normalize_doi(paper.get("doi"))
        title = normalize_title(paper.get("title"))
        if doi and doi not in by_doi:
            by_doi[doi] = index
        if title and title not in by_title:
            by_title[title] = index

    for local in local_papers or []:
        if not isinstance(local, dict):
            continue
        doi = normalize_doi(local.get("doi"))
        title = normalize_title(local.get("title"))
        index = by_doi.get(doi) if doi else None
        if index is None and title:
            index = by_title.get(title)

        if index is None:
            result.append(local)
            index = len(result) - 1
        else:
            _merge_local_into(result[index], local)

        target = result[index]
        target_doi = normalize_doi(target.get("doi"))
        target_title = normalize_title(target.get("title"))
        if target_doi and target_doi not in by_doi:
            by_doi[target_doi] = index
        if target_title and target_title not in by_title:
            by_title[target_title] = index

    return result
