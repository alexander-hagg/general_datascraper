"""
main.py — run the whole pipeline, or just one stage.

    python main.py                 search, then fetch + extract everything
    python main.py --search-only   only search -> output/search_results.xlsx
    python main.py --extract-only  only fetch + extract (reads search_results.xlsx)
    python main.py --limit 5       extract at most 5 papers (quick demo)
    python main.py --mode table    one output row per row of a results table
    python main.py --year-from 2015 --year-to 2024   limit publication years
    python main.py --local-folder PATH [--recursive] [--local-only]
    python main.py --reset         clear the resume checkpoint, then run

See README.md for the full picture.
"""

import argparse
import sys

# Windows consoles default to cp1252, which can't print Greek/math symbols
# that appear in paper titles (α, β, γ, ...). Print as UTF-8 instead of crashing.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from openpyxl import load_workbook

import config
import extractor
import fetcher
import figures
import filters
import local_pdfs
import pdf_annotations
import pdf_fetcher
import searchers
import writer
from checkpoint import Checkpoint

SEARCH_COLS = [
    "paper_key", "source", "publication_type", "title", "authors", "doi",
    "pmid", "pmcid", "year", "journal", "is_open_access", "fulltext_url",
]
CLOSED_COLS = ["title", "authors", "doi", "source", "publication_type", "reason"]

# Columns that belong to the pipeline, not to the user's extracted fields.
# They are produced automatically and can never be supplied as web columns.
RESERVED_COLUMNS = (
    "paper_key", "row_index", "publication_type",
    "evidence", "evidence_page", "pdf_file", "pdf_source",
)
# `doi` and `source` are pre-existing metadata columns; letting a user-supplied
# field use those names would create duplicate Excel headers and overwrite the
# real metadata, so they are protected too.
PROTECTED_COLUMNS = RESERVED_COLUMNS + ("doi", "source")

EXTRACT_META_COLUMNS = (
    "paper_key", "row_index", "doi", "source", "publication_type",
    "pdf_file", "pdf_source", "evidence", "evidence_page",
)

EXTRACTION_MODES = ("records", "table")
MODE_LABELS = {"records": "Individual records", "table": "Table rows"}


def extract_columns(field_names):
    """Return the full extracted_data.xlsx layout for the given user fields."""
    return list(EXTRACT_META_COLUMNS) + [str(f) for f in field_names]


EXTRACT_COLS = extract_columns([e[0] for e in config.FIELDS])

# Closed-source reasons introduced by PDF archiving.
NO_PDF_REASON = "no PDF and no preprint found"
PDF_MANUAL_REASON = "PDF unavailable — download manually"
PDF_NO_TEXT_REASON = "PDF saved but no extractable text"


def normalize_extraction_mode(mode):
    """Validate and normalise an extraction mode ('records' or 'table')."""
    value = str(mode or "records").strip().lower()
    if value not in EXTRACTION_MODES:
        raise ValueError(
            f"Unknown extraction mode '{mode}'. Choose one of: {', '.join(EXTRACTION_MODES)}."
        )
    return value


def make_instruction(goal="", mode="records", extra=""):
    """Build the extraction prompt shared by the CLI and the web app.

    `goal` is the task in the user's words; `extra` appends a caller-supplied
    instruction (the CLI uses config.INSTRUCTION there). The records/table
    contract lives here so both interfaces ask the model for the same thing.
    """
    mode = normalize_extraction_mode(mode)
    goal = (goal or "").strip() or "Extract the relevant data described in the text."

    lines = [
        "You are an expert research assistant. Read the full paper TEXT below very carefully.",
        f"Task: {goal}",
        "",
        "IMPORTANT RULES:",
        "- NEVER invent, guess, or use outside knowledge. Only report what is explicitly",
        "  written in the TEXT. If it is not in the paper, do not put it in the answer.",
        "- If a value is missing, leave that field empty — do not make one up.",
        "- Keep wording and values consistent with how they appear in the paper.",
        "- If no record matches the task, return an empty list [].",
    ]

    if mode == "table":
        lines.extend([
            "- Extraction mode: TABLE ROWS. Treat every row of a results table as its",
            "  own record. A paper may produce many rows; return ONE JSON object per row.",
            "- NEVER merge, combine, aggregate, or summarize separate table rows into one",
            "  object, even when they share a condition or differ only in one value.",
            "- Ignore table headers, captions, footnotes and totals unless they are data",
            "  rows themselves.",
        ])
    else:
        lines.extend([
            "- Extraction mode: INDIVIDUAL RECORDS. Extract EVERY record that matches",
            "  the task. Do not skip any, and do not merge separate records into one row.",
            "- Return one JSON object per record.",
        ])

    if getattr(config, "EVIDENCE_ENABLED", True):
        lines.append(extractor.evidence_instruction(mode))

    extra = (extra or "").strip()
    if extra:
        lines.extend(["", "ADDITIONAL INSTRUCTIONS:", extra])
    return "\n".join(lines)


def validate_user_columns(columns):
    """Validate web-supplied column names.

    Returns `(cleaned, errors)`:
      cleaned — names with blanks removed and original spelling preserved
      errors  — list of human-readable problems; empty when the input is valid
    Reserved/technical names are rejected rather than silently ignored, and
    duplicates are rejected (case-insensitively) so the Excel header stays
    unambiguous.
    """
    if columns is None:
        return [], []
    if isinstance(columns, str):
        columns = columns.split(",")
    if not isinstance(columns, (list, tuple)):
        return [], ["Columns must be a list of names."]

    cleaned, seen, reserved, duplicates = [], set(), [], []
    protected = {name.lower(): name for name in PROTECTED_COLUMNS}
    for raw in columns:
        if raw is None:
            continue
        name = str(raw).strip()
        if not name:
            continue
        lowered = name.lower()
        if lowered in protected:
            reserved.append(name)
            continue
        if lowered in seen:
            duplicates.append(name)
            continue
        seen.add(lowered)
        cleaned.append(name)

    errors = []
    if reserved:
        errors.append(
            "These column names are reserved and added automatically: "
            + ", ".join(dict.fromkeys(reserved))
        )
    if duplicates:
        errors.append(
            "Duplicate column names: " + ", ".join(dict.fromkeys(duplicates))
        )
    return cleaned, errors


def extract_user_columns(header):
    """Non-technical columns from an extracted_data.xlsx header."""
    meta = set(EXTRACT_META_COLUMNS)
    return [
        str(name).strip()
        for name in (header or [])
        if name not in (None, "") and str(name).strip() not in meta
    ]


def paper_identity(paper):
    """Stored paper_key when present, else the canonical searchers.paper_key.

    Returns "" when the paper has no identifying metadata at all (so callers
    can put it in the "unlinked" bucket instead of inventing `title:`).
    """
    stored = str(paper.get("paper_key") or "").strip()
    if stored:
        return stored
    if paper.get("pmid") or paper.get("doi") or str(paper.get("title") or "").strip():
        return searchers.paper_key(paper)
    return ""


def paper_to_row(paper):
    """Convert a paper dict into a search_results.xlsx row."""
    return {
        "paper_key": paper_identity(paper),
        "source": ", ".join(paper.get("sources", [])),
        "publication_type": paper.get("publication_type", ""),
        "title": paper.get("title", ""),
        "authors": paper.get("authors", ""),
        "doi": paper.get("doi", ""),
        "pmid": paper.get("pmid", ""),
        "pmcid": paper.get("pmcid", ""),
        "year": paper.get("year", ""),
        "journal": paper.get("journal", ""),
        "is_open_access": paper.get("is_oa"),
        "fulltext_url": paper.get("fulltext_url", ""),
    }


def build_extract_rows(paper, records, field_names, pdf_file="", pdf_source="none"):
    """Build extracted_data.xlsx rows for one paper.

    `row_index` is 1-based within this paper's extraction batch and resets for
    every paper, so it is a stable within-paper row number. Reserved columns
    are filled by the pipeline; only the requested user fields come from the
    model's records.
    """
    rows = []
    base = {
        "paper_key": paper_identity(paper),
        "doi": paper.get("doi", ""),
        "source": ", ".join(paper.get("sources", [])),
        "publication_type": paper.get("publication_type", ""),
        "pdf_file": pdf_file,
        "pdf_source": pdf_source,
        "evidence": "",
        "evidence_page": "",
    }
    for index, record in enumerate(records, start=1):
        row = dict(base)
        row["row_index"] = index
        for field in field_names:
            row[field] = record.get(field, "") if isinstance(record, dict) else ""
        evidence = record.get("evidence", "") if isinstance(record, dict) else ""
        row["evidence"] = "" if evidence is None else str(evidence)
        rows.append(row)
    return rows


def run_search(year_from=None, year_to=None, local_folder=None, recursive=False,
               local_only=False):
    # Omitted CLI/API values fall back to config.py. An explicit blank string
    # ("") still means "no limit on this side" and overrides a configured year.
    if year_from is None:
        year_from = getattr(config, "YEAR_FROM", None)
    if year_to is None:
        year_to = getattr(config, "YEAR_TO", None)
    year_from, year_to = searchers.normalize_year_range(year_from, year_to)
    range_label = searchers.year_range_label(year_from, year_to)

    local_folder = str(local_folder or "").strip()
    if local_only and not local_folder:
        raise ValueError("--local-only requires --local-folder PATH.")

    print("== Step 1/2: searching ==")
    if range_label:
        print(f"Publication years: {range_label}")

    # Scan before the online search so an invalid folder fails fast, without
    # spending time on network requests.
    local_papers, local_stats = [], None
    if local_folder:
        print(
            f"Local PDF folder: {local_folder} "
            f"({'including subfolders' if recursive else 'top-level only'})"
        )
        local_papers, local_stats = local_pdfs.scan_folder(
            local_folder, recursive=recursive
        )
        print(
            f"Local PDFs: {local_stats['scanned']} scanned, {len(local_papers)} "
            f"unique, {local_stats['copied']} copied, {local_stats['linked']} linked, "
            f"{local_stats['duplicates']} duplicate(s), {local_stats['errors']} error(s)."
        )
        for detail in local_stats.get("error_details", [])[:5]:
            print(f"  !! {detail['file']}: {detail['error']}")

    if local_only:
        print("Skipping the online search (--local-only).")
        new_papers = []
    else:
        new_papers = searchers.search_all(year_from=year_from, year_to=year_to)

    # Load any papers already in search_results.xlsx so a fresh search ADDS
    # to them (deduped) instead of wiping previous rows. If you want a totally
    # different topic, just delete output/search_results.xlsx first — a fresh
    # one is created on the next run.
    existing = _load_search_results(required=False)
    papers = searchers.merge_papers(existing, new_papers)
    added = len(papers) - len(existing)

    if local_papers:
        before_local = len(papers)
        papers = local_pdfs.merge_local_papers(papers, local_papers)
        local_added = len(papers) - before_local
        print(
            f"Merged local PDFs: {len(local_papers) - local_added} matched existing "
            f"paper(s), {local_added} added as new paper(s)."
        )

    # Journal blocklist is applied to the MERGED set, not inside each source
    # client, so rows already in search_results.xlsx are also removed when they
    # start matching a newly-added journal name.
    terms = filters.parse_journal_terms(getattr(config, "EXCLUDED_JOURNALS", []))
    papers, excluded = filters.filter_papers(papers, terms)

    rows = [paper_to_row(p) for p in papers]
    writer.write_xlsx(config.SEARCH_RESULTS_XLSX, SEARCH_COLS, rows)
    if local_folder:
        # The CLI merged with the existing sheet, so this final set also retains
        # old local mappings. Replacing drops entries for papers that were
        # removed (for example by the journal blocklist).
        local_pdfs.write_index(papers, replace=True)
    print(f"Wrote {len(rows)} papers ({added} new) to {config.SEARCH_RESULTS_XLSX}")
    print(f"Excluded {len(excluded)} paper(s) by journal blocklist.")
    return papers


def _load_search_results(required=True):
    if not config.SEARCH_RESULTS_XLSX.exists():
        if required:
            sys.exit(
                f"No {config.SEARCH_RESULTS_XLSX.name} yet. Run `python main.py --search-only` "
                "first (or run `python main.py` to do everything)."
            )
        return local_pdfs.attach_to_papers([])
    # Older search sheets predate publication_type. Add it in place with blank
    # values before reading so extract-only runs and the web table see the same
    # column as a fresh search, without touching any existing values.
    writer.ensure_columns(config.SEARCH_RESULTS_XLSX, SEARCH_COLS)
    wb = load_workbook(config.SEARCH_RESULTS_XLSX, read_only=True)
    ws = wb.active
    headers = [
        "" if c.value is None else str(c.value).strip()
        for c in next(ws.iter_rows(min_row=1, max_row=1))
    ]
    papers = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        d = dict(zip(headers, row))
        paper = {
            "sources": [s.strip() for s in str(d.get("source") or "").split(",") if s.strip()],
            "title": d.get("title") or "",
            "authors": d.get("authors") or "",
            "doi": d.get("doi") or "",
            "pmid": d.get("pmid") or "",
            "pmcid": d.get("pmcid") or "",
            "year": d.get("year") or "",
            "journal": d.get("journal") or "",
            "publication_type": d.get("publication_type") or "",
            "abstract": d.get("abstract") or "",
            "is_oa": d.get("is_open_access") in ("yes", "true", True, 1),
            "fulltext_url": d.get("fulltext_url") or "",
        }
        # Old search sheets predate paper_key: derive it in memory from
        # pmid > doi > title. Never lose the stored value when it exists.
        paper["paper_key"] = paper_identity({"paper_key": d.get("paper_key"), **paper})
        papers.append(paper)
    wb.close()
    # The workbook only stores search metadata. Re-attach archived local PDFs
    # from output/local_pdfs.json so a later Extract run does not have to rescan
    # the source folder (and can run with --extract-only).
    return local_pdfs.attach_to_papers(papers)


def _prepare_paper(p):
    """Fetch full text and archive the PDF for one paper.

    Returns (text, text_source, pdf). If full-text fetching failed but a PDF was
    saved, the PDF's own text is used so extraction can still proceed.

    A paper with an attached local PDF never touches the network: its archived
    copy is the only PDF considered. When the indexed file is missing, the
    normal online path runs instead.
    """
    local_path = local_pdfs.existing_local_path(p)
    if local_path is not None:
        pdf = {
            "path": local_path,
            "filename": local_path.name,
            "source": "local",
            "server": "folder",
            "label": "local:folder",
            "reason": "",
        }
        local_text = pdf_fetcher.pdf_text(local_path)
        if local_text and len(local_text.strip()) >= fetcher.MIN_TEXT_CHARS:
            return local_text, "pdf:local", pdf
        # No extractable text: do not silently try another PDF. The normal
        # closed-source flow records PDF_NO_TEXT_REASON for this saved file.
        return None, "local PDF has no extractable text", pdf

    text, source = fetcher.fetch_full_text(p)
    pdf = pdf_fetcher.acquire_pdf(p)
    if not text and pdf.get("path"):
        pdf_text = pdf_fetcher.pdf_text(pdf["path"])
        if pdf_text and len(pdf_text.strip()) >= fetcher.MIN_TEXT_CHARS:
            text = pdf_text
            source = f"pdf:{pdf.get('server') or 'unknown'}"
    return text, source, pdf


def _closed_row(p, reason):
    return {
        "title": p.get("title", ""),
        "authors": p.get("authors", ""),
        "doi": p.get("doi", ""),
        "source": ", ".join(p.get("sources", [])),
        "publication_type": p.get("publication_type", ""),
        "reason": reason,
    }


def run_extract(limit=None, papers=None, mode="records"):
    mode = normalize_extraction_mode(mode)
    print("== Step 2/2: fetching + extracting ==")
    print(f"Extraction mode: {MODE_LABELS[mode]} ({mode})")
    extractor.check_backend()
    cp = Checkpoint(config.CHECKPOINT_JSON)

    if papers is None:
        papers = _load_search_results()

    # Migrate older workbooks in place (new reserved columns, DOI-based
    # paper_key backfill) before any row is appended. Existing values and
    # user-defined field columns are preserved.
    writer.ensure_columns(config.EXTRACTED_XLSX, EXTRACT_COLS)
    writer.backfill_paper_keys(config.EXTRACTED_XLSX)
    instruction = make_instruction(mode=mode, extra=config.INSTRUCTION)
    field_names = [e[0] for e in config.FIELDS]

    # dedup + skip already-done
    todo, seen = [], set()
    for p in papers:
        k = paper_identity(p)
        if not k:
            continue
        if k in seen:
            continue
        seen.add(k)
        if cp.is_done(k):
            continue
        todo.append(p)
    if limit:
        todo = todo[:limit]

    print(f"{len(todo)} paper(s) to process.")
    for p in todo:
        k = paper_identity(p)
        title = (p.get("title") or "")[:60]
        print(f"\n[{k}] {title}")

        try:
            text, source, pdf = _prepare_paper(p)
        except Exception as e:
            cp.mark(k, "error", error=str(e))
            print(f"  !! fetch error: {e}")
            continue

        pdf_file = pdf.get("filename") or ""
        pdf_source = pdf.get("label") or "none"

        if not text:
            reason = PDF_NO_TEXT_REASON if pdf.get("path") else (pdf.get("reason") or NO_PDF_REASON)
            writer.append_xlsx(config.CLOSED_SOURCE_XLSX, CLOSED_COLS, [_closed_row(p, reason)])
            cp.mark(k, "closed", reason=reason, pdf_file=pdf_file, pdf_source=pdf_source)
            print(f"  -> closed source: {reason}")
            continue

        print(f"  -> got full text ({len(text)} chars) via {source}; extracting ...")
        try:
            rows = extractor.extract(
                text,
                fields=config.FIELDS,
                instruction=instruction,
            )
        except Exception as e:
            cp.mark(k, "error", error=str(e))
            print(f"  !! extract error: {e}")
            continue

        records = [r for r in rows if isinstance(r, dict)]
        out_rows = build_extract_rows(
            p, records, field_names, pdf_file=pdf_file, pdf_source=pdf_source
        )

        # Locate evidence quotes and create the cached highlighted PDF. This is
        # deliberately best-effort: a missing/broken PDF or a bad quote must
        # never lose the extracted rows.
        if getattr(config, "EVIDENCE_ENABLED", True) and pdf.get("path"):
            try:
                out_rows, summary = pdf_annotations.annotate_paper_rows(
                    out_rows,
                    pdf.get("path"),
                    paper_key=paper_identity(p),
                    title=p.get("title", ""),
                    pdf_file=pdf_file,
                    pdf_source=pdf_source,
                )
                message = pdf_annotations.summary_message(summary)
                if message:
                    print(f"  -> {message}")
            except Exception as exc:  # noqa: BLE001 - evidence is best-effort
                print(f"  !! evidence warning: {exc}")

        # Search the archived PDF for figures/tables that help answer the
        # configured extraction instruction. Independent of evidence and of the
        # number of extracted rows; an empty instruction saves no figures when
        # goal filtering is enabled. Never fatal.
        if getattr(config, "FIGURES_ENABLED", True) and pdf.get("path"):
            try:
                figure_summary = figures.process_paper(
                    pdf.get("path"),
                    paper_identity(p),
                    title=p.get("title", ""),
                    pdf_file=pdf_file,
                    pdf_source=pdf_source,
                    goal=config.INSTRUCTION,
                )
                message = figures.summary_message(figure_summary)
                if message:
                    print(f"  -> {message}")
            except Exception as exc:  # noqa: BLE001 - figures are best-effort
                print(f"  !! figure warning: {exc}")

        writer.append_xlsx(config.EXTRACTED_XLSX, EXTRACT_COLS, out_rows)

        # Rule 3: text was extractable but no PDF/preprint could be archived.
        # Keep the extracted data, but also flag the PDF gap for the user.
        # Only when archiving is enabled — a user who disabled downloads should
        # not be nagged about a manual PDF gap.
        if not pdf.get("path") and getattr(config, "PDF_ARCHIVE_ENABLED", True):
            writer.append_xlsx(config.CLOSED_SOURCE_XLSX, CLOSED_COLS, [_closed_row(p, PDF_MANUAL_REASON)])
            print(f"  -> {PDF_MANUAL_REASON}")

        cp.mark(k, "extracted", n=len(out_rows), pdf_file=pdf_file, pdf_source=pdf_source)
        print(f"  -> wrote {len(out_rows)} row(s)")

    print(f"\nDone. See {config.EXTRACTED_XLSX} and {config.CLOSED_SOURCE_XLSX}.")


def _year_arg(value):
    """Argparse type for one optional four-digit publication year."""
    if value is None:
        return None
    if not str(value).strip():
        return ""
    try:
        return searchers.normalize_year_range(value, None)[0]
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Generalized search -> extract pipeline.")
    ap.add_argument("--search-only", action="store_true", help="only search (skip extraction)")
    ap.add_argument("--extract-only", action="store_true", help="only fetch + extract from existing search results")
    ap.add_argument("--limit", type=int, default=None, help="max papers to extract (quick demo)")
    ap.add_argument("--reset", action="store_true", help="clear the resume checkpoint before running")
    ap.add_argument(
        "--year-from",
        type=_year_arg,
        default=None,
        metavar="YYYY",
        help="earliest publication year (default: config.YEAR_FROM)",
    )
    ap.add_argument(
        "--year-to",
        type=_year_arg,
        default=None,
        metavar="YYYY",
        help="latest publication year (default: config.YEAR_TO)",
    )
    ap.add_argument(
        "--mode",
        choices=EXTRACTION_MODES,
        default=getattr(config, "EXTRACTION_MODE", "records"),
        help="extraction mode: individual records (default) or table rows",
    )
    ap.add_argument(
        "--local-folder",
        default=None,
        metavar="PATH",
        help="also use PDFs from this local folder (copied into output/pdfs/)",
    )
    ap.add_argument(
        "--recursive",
        action="store_true",
        help="include subfolders when scanning --local-folder",
    )
    ap.add_argument(
        "--local-only",
        action="store_true",
        help="skip the online search and use only --local-folder PDFs",
    )
    args = ap.parse_args(argv)
    if args.local_only and not args.local_folder:
        ap.error("--local-only requires --local-folder PATH")
    return args


def main():
    args = parse_args()
    if args.reset:
        Checkpoint(config.CHECKPOINT_JSON).reset()
        print("Checkpoint cleared.")

    if args.extract_only:
        run_extract(limit=args.limit, mode=args.mode)
        return

    try:
        papers = run_search(
            year_from=args.year_from,
            year_to=args.year_to,
            local_folder=args.local_folder,
            recursive=args.recursive,
            local_only=args.local_only,
        )
    except ValueError as e:
        sys.exit(f"Error: {e}")

    if not args.search_only:
        run_extract(limit=args.limit, papers=papers, mode=args.mode)


if __name__ == "__main__":
    main()
