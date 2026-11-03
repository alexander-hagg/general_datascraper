"""
app.py — local web app for the search -> fetch -> extract pipeline.

Run it with:

    python app.py        (or double-click start.bat)

A browser opens at http://127.0.0.1:5000. No terminal needed after that.

This is a thin Flask layer over the same engine the CLI uses (searchers, fetcher,
extractor, writer, checkpoint), so both interfaces stay in sync.
"""

import json
import os
import subprocess
import sys
import threading
import uuid
import webbrowser
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_file
from openpyxl import load_workbook

import config
import extractor
import fetcher
import figures
import filters
import history
import local_pdfs
import pdf_annotations
import searchers
import suggester
import writer
from checkpoint import Checkpoint
from main import (
    CLOSED_COLS,
    NO_PDF_REASON,
    PDF_MANUAL_REASON,
    PDF_NO_TEXT_REASON,
    SEARCH_COLS,
    _closed_row,
    _load_search_results,
    _prepare_paper,
    build_extract_rows,
    extract_columns,
    extract_user_columns,
    make_instruction,
    normalize_extraction_mode,
    paper_identity,
    paper_to_row,
    validate_user_columns,
)

app = Flask(__name__)

# --------------------------------------------------------------------------
# Settings (persisted server-side so users don't retype email/model each time)
# --------------------------------------------------------------------------

SETTINGS_PATH = config.OUTPUT_DIR / "web_settings.json"


def _default_settings():
    raw_from = getattr(config, "YEAR_FROM", None)
    raw_to = getattr(config, "YEAR_TO", None)
    try:
        year_from, year_to = searchers.normalize_year_range(raw_from, raw_to)
    except ValueError:
        # A typo in config.py should not stop the web app from starting. Keep
        # the bad values visible so the search endpoint can report them.
        year_from, year_to = raw_from, raw_to
    return {
        "email": config.NCBI_EMAIL,
        "model": config.OLLAMA_MODEL,
        "max_results": config.DEFAULT_MAX_RESULTS,
        "excluded_journals": filters.parse_journal_terms(
            getattr(config, "EXCLUDED_JOURNALS", [])
        ),
        "year_from": year_from,
        "year_to": year_to,
        "local_folder": str(getattr(config, "LOCAL_PDF_DIR", "") or ""),
        "local_recursive": bool(getattr(config, "LOCAL_PDF_RECURSIVE", False)),
    }


def _as_bool(value):
    """Accept JSON booleans plus common string/number spellings."""
    if isinstance(value, str):
        return value.strip().casefold() in ("1", "true", "yes", "on")
    return bool(value)


def load_settings():
    if SETTINGS_PATH.exists():
        try:
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            d = _default_settings()
            d.update({k: v for k, v in data.items() if k in d})
            d["excluded_journals"] = filters.parse_journal_terms(
                d.get("excluded_journals")
            )
            try:
                d["year_from"], d["year_to"] = searchers.normalize_year_range(
                    d.get("year_from"), d.get("year_to")
                )
            except ValueError:
                # Keep the other saved settings; only fall back for the bad range.
                defaults = _default_settings()
                d["year_from"] = defaults["year_from"]
                d["year_to"] = defaults["year_to"]
            d["local_folder"] = str(d.get("local_folder") or "").strip()
            d["local_recursive"] = _as_bool(d.get("local_recursive"))
            return d
        except Exception:
            return _default_settings()
    return _default_settings()


def save_settings(s):
    SETTINGS_PATH.write_text(json.dumps(s, indent=2, ensure_ascii=False), encoding="utf-8")


def apply_settings(s):
    config.NCBI_EMAIL = s.get("email") or config.NCBI_EMAIL
    config.OLLAMA_MODEL = s.get("model") or config.OLLAMA_MODEL
    config.HTTP_HEADERS = {"User-Agent": f"general-datascraper/1.0 (mailto:{config.NCBI_EMAIL})"}
    if "year_from" in s or "year_to" in s:
        try:
            config.YEAR_FROM, config.YEAR_TO = searchers.normalize_year_range(
                s.get("year_from"), s.get("year_to")
            )
        except ValueError:
            pass  # keep the config values; the search request will report the error
    if "local_folder" in s:
        config.LOCAL_PDF_DIR = str(s.get("local_folder") or "")
    if "local_recursive" in s:
        config.LOCAL_PDF_RECURSIVE = _as_bool(s.get("local_recursive"))


apply_settings(load_settings())

# --------------------------------------------------------------------------
# Background jobs (one at a time)
# --------------------------------------------------------------------------

JOBS = {}
JOB_LOCK = threading.Lock()


def _new_job(job_type):
    job = {
        "id": uuid.uuid4().hex[:8],
        "type": job_type,
        "status": "running",
        "error": None,
        "progress": {"done": 0, "total": 0, "current": ""},
        "log": [],
        "result": None,
    }
    JOBS[job["id"]] = job
    return job


def _active_job():
    for j in list(JOBS.values()):
        if j["status"] == "running":
            return j
    return None


def _start_job(job_type, fn, *args):
    with JOB_LOCK:
        if _active_job():
            return None, "A task is already running. Wait for it to finish, then try again."
        job = _new_job(job_type)
    threading.Thread(target=fn, args=(job, *args), daemon=True).start()
    return job, None


def _job_public(job):
    if not job:
        return None
    return {
        "id": job["id"],
        "type": job["type"],
        "status": job["status"],
        "error": job["error"],
        "progress": job["progress"],
        "log": job["log"][-40:],
        "result": job["result"],
    }


# --------------------------------------------------------------------------
# Job bodies (mirror main.py, but report progress into the job dict)
# --------------------------------------------------------------------------

def _paper_to_row(p):
    return paper_to_row(p)


def _record_history(job, kind, data):
    """Record a prompt without ever failing the running job.

    History is a convenience feature: a corrupt file, a full disk or an
    unexpected value must not stop a search or an extraction run.
    """
    try:
        return history.record(kind, data)
    except Exception as exc:  # noqa: BLE001 - history is best-effort
        message = f"Could not save {kind} prompt history: {exc}"
        app.logger.warning(message)
        job["log"].append(f"Warning: {message}")
        return None


_YEARS_UNSET = object()
_LOCAL_UNSET = object()


def _run_search_job(job, query, sources, max_results, excluded_journals=None,
                    year_from=_YEARS_UNSET, year_to=_YEARS_UNSET,
                    local_folder=_LOCAL_UNSET, local_recursive=_LOCAL_UNSET,
                    query_tree=None):
    try:
        settings = load_settings()
        apply_settings(settings)
        # Old callers/clients may not pass a blocklist: fall back to the saved
        # web setting, which itself defaults to config.EXCLUDED_JOURNALS.
        if excluded_journals is None:
            excluded_journals = settings.get("excluded_journals")
        # Both bounds omitted means "use the saved setting". The API always
        # passes both values (possibly normalized None/None after the user
        # cleared the boxes), and an explicit None is an open end, not a
        # request to reuse the saved range.
        if year_from is _YEARS_UNSET and year_to is _YEARS_UNSET:
            year_from = settings.get("year_from")
            year_to = settings.get("year_to")
        else:
            if year_from is _YEARS_UNSET:
                year_from = None
            if year_to is _YEARS_UNSET:
                year_to = None
        year_from, year_to = searchers.normalize_year_range(year_from, year_to)
        range_label = searchers.year_range_label(year_from, year_to)

        # Local settings follow the same omitted-vs-explicit rule as the years.
        if local_folder is _LOCAL_UNSET:
            local_folder = settings.get("local_folder")
        if local_recursive is _LOCAL_UNSET:
            local_recursive = settings.get("local_recursive")
        local_folder = str(local_folder or "").strip()
        local_recursive = _as_bool(local_recursive)

        query = str(query or "").strip()
        sources = [str(s) for s in (sources or []) if str(s).strip()]
        online = bool(query and sources)
        if local_folder and bool(query) != bool(sources):
            raise ValueError(
                "Choose at least one website, or clear the search terms for a "
                "folder-only search." if query else
                "Enter at least one search term, or deselect every website for "
                "a folder-only search."
            )
        if not online and not local_folder:
            raise ValueError(
                "Enter a search term and choose at least one website, "
                "or choose a local PDF folder."
            )

        terms = filters.parse_journal_terms(excluded_journals)
        job["log"].append(f"Query: {query or '(none — folder only)'}")
        job["log"].append(
            "Websites: " + (", ".join(sources) if sources else "(none)")
        )
        job["log"].append(f"Publication years: {range_label or 'all years'}")
        if local_folder:
            job["log"].append(
                f"Local PDF folder: {local_folder} "
                f"({'including subfolders' if local_recursive else 'top-level only'})"
            )
        if terms:
            job["log"].append("Journal blocklist: " + ", ".join(terms))

        # Save the normalized prompt after every check above and before any
        # folder scan or network call. Only prompt/search fields are stored;
        # email, API keys and model settings never enter the history file.
        _record_history(job, "search", {
            "query": query,
            "query_tree": query_tree,
            "sources": sources,
            "max_results": max_results,
            "year_from": year_from,
            "year_to": year_to,
            "excluded_journals": terms,
            "local_folder": local_folder,
            "local_recursive": local_recursive,
        })

        # Scan inside the job so a large folder reports progress instead of
        # blocking the request. Invalid folders are also rejected by /api/search.
        local_papers, local_stats = [], None
        if local_folder:
            local_papers, local_stats = local_pdfs.scan_folder(
                local_folder, recursive=local_recursive
            )
            job["log"].append(
                f"Local PDFs: {local_stats['scanned']} scanned, "
                f"{len(local_papers)} unique, {local_stats['copied']} copied, "
                f"{local_stats['linked']} linked, "
                f"{local_stats['duplicates']} duplicate(s), "
                f"{local_stats['errors']} error(s)."
            )
            for detail in local_stats.get("error_details", [])[:5]:
                job["log"].append(f"   !! {detail['file']}: {detail['error']}")

        if online:
            papers = searchers.search(
                query, sources, limit=max_results,
                year_from=year_from, year_to=year_to,
            )
        else:
            papers = []
            job["log"].append("Online search skipped (folder-only).")

        if local_papers:
            before_local = len(papers)
            papers = local_pdfs.merge_local_papers(papers, local_papers)
            local_added = len(papers) - before_local
            job["log"].append(
                f"Merged local PDFs: {len(local_papers) - local_added} matched "
                f"existing paper(s), {local_added} added as new paper(s)."
            )

        papers, excluded = filters.filter_papers(papers, terms)
        rows = [_paper_to_row(p) for p in papers]
        # A new search starts a fresh dataset: replace the search sheet AND clear
        # any previous extraction outputs + resume state, so Extract only ever
        # works on this search's papers. The index is replaced too, so old
        # local mappings can never point at rows from the previous dataset.
        writer.write_xlsx(config.SEARCH_RESULTS_XLSX, SEARCH_COLS, rows)
        for path in (config.EXTRACTED_XLSX, config.CLOSED_SOURCE_XLSX):
            if Path(path).exists():
                Path(path).unlink()
        Checkpoint(config.CHECKPOINT_JSON).reset()
        local_pdfs.write_index(papers, replace=True)
        # Figures belong to the previous dataset too: clear the index, xlsx and
        # cached PNGs so stale thumbnails can never appear in the new gallery.
        try:
            cleared = figures.clear()
            if (cleared or {}).get("warning"):
                job["log"].append(f"Warning: {cleared['warning']}")
        except Exception as exc:  # noqa: BLE001 - figure cleanup is best-effort
            app.logger.warning("Could not clear figure gallery: %s", exc)
            job["log"].append(f"Warning: could not clear old figures: {exc}")
        job["result"] = {
            "papers": rows,
            "added": len(rows),
            "total": len(rows),
            "excluded": len(excluded),
            "year_from": year_from,
            "year_to": year_to,
            "year_range": range_label or "all years",
            "local_folder": local_folder,
            "local_recursive": local_recursive,
            "local_count": len(local_papers),
            "local_stats": local_stats,
        }
        job["status"] = "done"
        job["log"].append(f"Excluded {len(excluded)} paper(s) by journal blocklist.")
        year_note = f"; years {range_label}" if range_label else ""
        local_note = (
            f"; {len(local_papers)} local PDF(s)" if local_folder else ""
        )
        job["log"].append(
            f"Done: {len(rows)} papers in the sheet "
            f"({len(excluded)} excluded by journal{year_note}{local_note}; "
            "previous data cleared)."
        )
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        job["log"].append(f"Error: {e}")


def _read_header(path):
    p = Path(path)
    if not p.exists():
        return None
    try:
        wb = load_workbook(p, read_only=True)
        ws = wb.active
        header = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
        wb.close()
        return header
    except Exception:
        return None


def _run_extract_job(job, goal, columns, mode="records"):
    try:
        apply_settings(load_settings())
        mode = normalize_extraction_mode(mode)
        columns, errors = validate_user_columns(columns)
        if errors:
            job["status"] = "error"
            job["error"] = " ".join(errors)
            job["log"].append(f"Error: {job['error']}")
            return

        fields = [(c, "text") for c in columns] if columns else list(config.FIELDS)
        field_names = [f[0] for f in fields]
        instruction = make_instruction(goal, mode=mode)
        extract_cols = extract_columns(field_names)

        # Inputs are valid at this point. Record them before touching the
        # spreadsheet/checkpoint so even a run with no papers leaves a prompt
        # the user can pick again.
        _record_history(job, "goal", {"text": goal})
        if columns:
            _record_history(job, "columns", {"text": ", ".join(columns)})

        job["log"].append(f"Extraction mode: {mode}")

        papers = _load_search_results(required=False)
        if not papers:
            job["status"] = "done"
            job["result"] = {
                "extracted": 0,
                "closed": 0,
                "papers_processed": 0,
                "mode": mode,
            }
            job["log"].append("No search results yet — run a search first.")
            return

        # Decide whether the existing sheet belongs to the same user fields.
        # Technical columns may grow/reorder; that is migrated in place. A real
        # change to the user's field list keeps the old documented behaviour:
        # the first write starts a fresh extracted_data.xlsx.
        existing_header = _read_header(config.EXTRACTED_XLSX)
        same_fields = bool(existing_header) and (
            extract_user_columns(existing_header) == field_names
        )
        fresh_output = bool(existing_header) and not same_fields
        if not fresh_output:
            writer.ensure_columns(config.EXTRACTED_XLSX, extract_cols)
            writer.backfill_paper_keys(config.EXTRACTED_XLSX)

        cp = Checkpoint(config.CHECKPOINT_JSON)
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

        job["progress"]["total"] = len(todo)
        job["log"].append(f"{len(todo)} paper(s) to process (already-done skipped).")

        n_extracted = n_closed = 0
        n_evidence_located = n_evidence_unresolved = 0
        n_annotation_warnings = 0
        n_figures_saved = n_figure_duplicates = n_figures_capped = 0
        n_figure_warnings = 0
        first_write = True
        for i, p in enumerate(todo):
            k = paper_identity(p)
            title = (p.get("title") or "")[:60]
            job["progress"]["done"] = i
            job["progress"]["current"] = title
            job["log"].append(f"[{i + 1}/{len(todo)}] {title}")

            try:
                text, source, pdf = _prepare_paper(p)
            except Exception as e:
                cp.mark(k, "error", error=str(e))
                job["log"].append(f"   !! fetch error: {e}")
                continue

            pdf_file = pdf.get("filename") or ""
            pdf_source = pdf.get("label") or "none"

            if not text:
                reason = PDF_NO_TEXT_REASON if pdf.get("path") else (pdf.get("reason") or NO_PDF_REASON)
                writer.append_xlsx(config.CLOSED_SOURCE_XLSX, CLOSED_COLS, [_closed_row(p, reason)])
                cp.mark(k, "closed", reason=reason, pdf_file=pdf_file, pdf_source=pdf_source)
                n_closed += 1
                job["log"].append(f"   -> closed source ({reason})")
                continue

            try:
                rows = extractor.extract(text, fields=fields, instruction=instruction)
            except Exception as e:
                cp.mark(k, "error", error=str(e))
                job["log"].append(f"   !! extract error: {e}")
                continue

            records = [r for r in rows if isinstance(r, dict)]
            out_rows = build_extract_rows(
                p, records, field_names, pdf_file=pdf_file, pdf_source=pdf_source
            )

            # Evidence is best-effort: locate the verbatim quotes and cache the
            # highlighted PDF without ever failing the extraction job.
            if getattr(config, "EVIDENCE_ENABLED", True) and pdf.get("path"):
                try:
                    out_rows, annotation_summary = pdf_annotations.annotate_paper_rows(
                        out_rows,
                        pdf.get("path"),
                        paper_key=k,
                        title=p.get("title", ""),
                        pdf_file=pdf_file,
                        pdf_source=pdf_source,
                    )
                    n_evidence_located += int(
                        (annotation_summary or {}).get("located") or 0
                    )
                    n_evidence_unresolved += int(
                        (annotation_summary or {}).get("unresolved") or 0
                    )
                    if (annotation_summary or {}).get("warning"):
                        n_annotation_warnings += 1
                    message = pdf_annotations.summary_message(annotation_summary)
                    if message:
                        job["log"].append(f"   -> {message}")
                except Exception as exc:  # noqa: BLE001 - never fail extraction
                    n_annotation_warnings += 1
                    job["log"].append(f"   !! evidence warning: {exc}")

            # Figure screenshots are independent of evidence and of the number
            # of extracted rows. Best-effort: never fail the extraction job.
            if getattr(config, "FIGURES_ENABLED", True) and pdf.get("path"):
                try:
                    figure_summary = figures.process_paper(
                        pdf.get("path"),
                        k,
                        title=p.get("title", ""),
                        pdf_file=pdf_file,
                        pdf_source=pdf_source,
                    )
                    n_figures_saved += int(
                        (figure_summary or {}).get("saved") or 0
                    )
                    n_figure_duplicates += int(
                        (figure_summary or {}).get("duplicates") or 0
                    )
                    if (figure_summary or {}).get("capped"):
                        n_figures_capped += 1
                    if (figure_summary or {}).get("warning"):
                        n_figure_warnings += 1
                    message = figures.summary_message(figure_summary)
                    if message:
                        job["log"].append(f"   -> {message}")
                except Exception as exc:  # noqa: BLE001 - never fail extraction
                    n_figure_warnings += 1
                    job["log"].append(f"   !! figure warning: {exc}")

            if out_rows:
                if fresh_output and first_write:
                    writer.write_xlsx(config.EXTRACTED_XLSX, extract_cols, out_rows)
                    job["log"].append("   (columns changed — started a fresh extracted_data.xlsx)")
                    first_write = False
                else:
                    writer.append_xlsx(config.EXTRACTED_XLSX, extract_cols, out_rows)
                n_extracted += len(out_rows)

            # Rule 3: text was extractable but no PDF/preprint could be archived.
            # Only when archiving is enabled — a user who disabled downloads should
            # not be nagged about a manual PDF gap.
            if not pdf.get("path") and getattr(config, "PDF_ARCHIVE_ENABLED", True):
                writer.append_xlsx(config.CLOSED_SOURCE_XLSX, CLOSED_COLS, [_closed_row(p, PDF_MANUAL_REASON)])
                n_closed += 1
                job["log"].append(f"   -> {PDF_MANUAL_REASON}")

            cp.mark(k, "extracted", n=len(out_rows), pdf_file=pdf_file, pdf_source=pdf_source)
            job["log"].append(f"   -> {len(out_rows)} row(s)")

        job["progress"]["done"] = len(todo)
        job["progress"]["current"] = ""
        job["result"] = {
            "extracted": n_extracted,
            "closed": n_closed,
            "papers_processed": len(todo),
            "mode": mode,
            "evidence_located": n_evidence_located,
            "evidence_unresolved": n_evidence_unresolved,
            "annotation_warnings": n_annotation_warnings,
            "figures_saved": n_figures_saved,
            "figure_duplicates": n_figure_duplicates,
            "figures_capped": n_figures_capped,
            "figure_warnings": n_figure_warnings,
        }
        job["status"] = "done"
        job["log"].append(f"Done: {n_extracted} row(s) extracted, {n_closed} paper(s) closed.")
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        job["log"].append(f"Error: {e}")


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    return jsonify({
        "ollama": extractor.backend_status(),
        "job": _job_public(_active_job()),
    })


@app.route("/api/sources")
def api_sources():
    return jsonify([
        {"name": k, "label": v["label"]} for k, v in searchers.SOURCE_REGISTRY.items()
    ])


@app.route("/api/autocomplete")
def api_autocomplete():
    """Proxy PubMed's autocomplete endpoint (avoids browser CORS issues).

    Always answers 200 with a list (possibly empty) so a failed upstream lookup
    degrades silently to "no suggestions". `source` lets the UI badge every
    completion as coming from PubMed.
    """
    query = (request.args.get("q") or "").strip()
    try:
        limit = int(request.args.get("limit") or suggester.MAX_AUTOCOMPLETE)
    except (TypeError, ValueError):
        limit = suggester.MAX_AUTOCOMPLETE
    return jsonify({
        "query": query,
        "source": "pubmed",
        "suggestions": suggester.autocomplete(query, limit),
    })


@app.route("/api/suggest", methods=["POST"])
def api_suggest():
    """LLM-backed related-term expansion.

    Request body:
      {"term": "ENaC"}             -> {"term": ..., "source": "llm", "model": ...,
                                       "suggestions": [...]}
      {"terms": ["ENaC", "..."]}   -> {"source": "llm", "model": ...,
                                       "groups": [{"term", "source", "model",
                                                   "suggestions"}, ...]}
    """
    data = request.get_json(force=True, silent=True) or {}
    try:
        if isinstance(data.get("terms"), list):
            meta = suggester.llm_source()
            groups = [
                {**group, **meta}
                for group in suggester.suggest_related_many(data["terms"])
            ]
            return jsonify({**meta, "groups": groups})
        term = (data.get("term") or "").strip()
        meta = suggester.llm_source()
        return jsonify({
            "term": term,
            **meta,
            "suggestions": suggester.suggest_related(term),
        })
    except suggester.BackendUnavailable as e:
        return jsonify({"error": str(e)}), 503
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:  # unexpected LLM/parse failure: report, don't crash
        return jsonify({"error": f"Could not generate suggestions: {e}"}), 502


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "POST":
        s = load_settings()
        data = request.get_json(force=True, silent=True) or {}
        if data.get("email") is not None:
            s["email"] = str(data["email"]).strip()
        if data.get("model") is not None:
            s["model"] = str(data["model"]).strip()
        if data.get("max_results") is not None:
            try:
                s["max_results"] = int(data["max_results"])
            except (TypeError, ValueError):
                pass
        if data.get("excluded_journals") is not None:
            s["excluded_journals"] = filters.parse_journal_terms(
                data["excluded_journals"]
            )
        if "year_from" in data or "year_to" in data:
            candidate_from = (
                data["year_from"] if "year_from" in data else s.get("year_from")
            )
            candidate_to = (
                data["year_to"] if "year_to" in data else s.get("year_to")
            )
            try:
                s["year_from"], s["year_to"] = searchers.normalize_year_range(
                    candidate_from, candidate_to
                )
            except ValueError as e:
                return jsonify({"error": str(e)}), 400
        if data.get("local_folder") is not None:
            s["local_folder"] = str(data["local_folder"]).strip()
        if data.get("local_recursive") is not None:
            s["local_recursive"] = _as_bool(data["local_recursive"])
        save_settings(s)
        apply_settings(s)
        return jsonify(s)
    return jsonify(load_settings())


@app.route("/api/history")
def api_history():
    """Return prompt history, newest-used first.

    `?kind=search|goal|columns` filters to one kind. Invalid values are
    rejected instead of silently returning the whole store.
    """
    kind = request.args.get("kind")
    if kind is not None:
        kind = kind.strip()
        if kind not in history.KINDS:
            return jsonify({
                "error": "Unknown history kind. Use search, goal or columns."
            }), 400
    return jsonify({"entries": history.list_entries(kind)})


@app.route("/api/history/clear", methods=["POST"])
def api_history_clear():
    """Clear all history, or only one kind when `kind` is supplied."""
    data = request.get_json(force=True, silent=True) or {}
    if not isinstance(data, dict):
        data = {}
    kind = data.get("kind")
    if kind is not None:
        if not isinstance(kind, str) or kind.strip() not in history.KINDS:
            return jsonify({
                "error": "Unknown history kind. Use search, goal or columns."
            }), 400
        kind = kind.strip()
    return jsonify({"cleared": history.clear(kind)})


@app.route("/api/search", methods=["POST"])
def api_search():
    data = request.get_json(force=True, silent=True) or {}
    query = (data.get("query") or "").strip()
    sources = data.get("sources") or []
    if isinstance(sources, str):
        sources = [sources]
    sources = [str(s).strip() for s in sources if str(s).strip()]
    settings = load_settings()
    max_results = data.get("max_results") or settings.get("max_results") or config.DEFAULT_MAX_RESULTS

    # Local folder: an omitted key falls back to the saved setting, while an
    # explicitly blank folder disables local PDFs for this search.
    raw_local = "local_folder" in data or "local_recursive" in data
    if "local_folder" in data:
        local_folder = str(data.get("local_folder") or "").strip()
    else:
        local_folder = str(settings.get("local_folder") or "").strip()
    if "local_recursive" in data:
        local_recursive = _as_bool(data.get("local_recursive"))
    else:
        local_recursive = _as_bool(settings.get("local_recursive"))
    local_only = _as_bool(data.get("local_only"))
    if local_only and not local_folder:
        return jsonify({
            "error": "Choose a local PDF folder for a folder-only search."
        }), 400

    if local_folder:
        try:
            local_pdfs.validate_folder(local_folder)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        if local_only:
            # An explicit folder-only flag wins over any stale query/source
            # fields an older client may have left in the form.
            query, sources = "", []
        if bool(query) != bool(sources):
            if query:
                return jsonify({
                    "error": "Choose at least one website, or clear the search "
                             "terms for a folder-only search."
                }), 400
            return jsonify({
                "error": "Enter at least one search term, or deselect every "
                         "website for a folder-only search."
            }), 400
    else:
        if not query:
            return jsonify({"error": "Enter at least one search term."}), 400
        if not sources:
            return jsonify({"error": "Choose at least one website."}), 400

    try:
        max_results = int(max_results)
    except (TypeError, ValueError):
        max_results = config.DEFAULT_MAX_RESULTS

    # New clients send the textarea contents. Old clients omit the key and get
    # the saved setting (which defaults to config.EXCLUDED_JOURNALS).
    raw_journals = data.get("excluded_journals")
    excluded_journals = filters.parse_journal_terms(
        settings.get("excluded_journals") if raw_journals is None else raw_journals
    )

    # The year range is a separate request parameter, never part of `query`.
    # Old clients omit both keys and fall back to saved settings (which default
    # to config.YEAR_FROM/YEAR_TO). Presence of either key makes the request
    # explicit; a blank side then means "no limit", not "use the saved year".
    raw_years = "year_from" in data or "year_to" in data
    if raw_years:
        requested_from, requested_to = data.get("year_from"), data.get("year_to")
    else:
        requested_from, requested_to = (
            settings.get("year_from"), settings.get("year_to")
        )
    try:
        year_from, year_to = searchers.normalize_year_range(
            requested_from, requested_to
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    job, err = _start_job(
        "search", _run_search_job, query, sources, max_results,
        excluded_journals, year_from, year_to, local_folder, local_recursive,
        data.get("query_tree"),
    )
    if err:
        return jsonify({"error": err}), 409
    # A search is an implicit save for the blocklist, an explicitly supplied
    # year range, and the local folder settings: the user should not have to
    # click "Save settings" for values they just typed to be used again next time.
    if raw_journals is not None or raw_years or raw_local:
        if raw_journals is not None:
            settings["excluded_journals"] = excluded_journals
        if raw_years:
            settings["year_from"] = year_from
            settings["year_to"] = year_to
        if raw_local:
            settings["local_folder"] = local_folder
            settings["local_recursive"] = local_recursive
        save_settings(settings)
    return jsonify({"job_id": job["id"]})


@app.route("/api/extract", methods=["POST"])
def api_extract():
    data = request.get_json(force=True, silent=True) or {}
    goal = (data.get("goal") or "").strip()
    columns = data.get("columns") or []
    if not goal:
        return jsonify({"error": "Describe what data you want to extract."}), 400

    try:
        mode = normalize_extraction_mode(data.get("mode") or "records")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    columns, errors = validate_user_columns(columns)
    if errors:
        return jsonify({"error": " ".join(errors)}), 400

    job, err = _start_job("extract", _run_extract_job, goal, columns, mode)
    if err:
        return jsonify({"error": err}), 409
    return jsonify({"job_id": job["id"], "mode": mode})


@app.route("/api/job/<job_id>")
def api_job(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "unknown job"}), 404
    return jsonify(_job_public(job))


def _read_xlsx(path):
    p = Path(path)
    if not p.exists():
        return None
    try:
        wb = load_workbook(p, read_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        wb.close()
    except Exception:
        return None
    if not rows:
        return None
    header = ["" if c is None else c for c in rows[0]]
    data = [["" if c is None else c for c in r] for r in rows[1:]]
    return {"header": header, "rows": data}


def _sheet_with_paper_keys(sheet, allow_title=False):
    """Add paper_key values to a search/extracted sheet response.

    Files written by older versions have no paper_key column. This derives it
    in memory (PMID/DOI only for extracted rows; title is also allowed for
    search rows) without touching the workbook, so the API shape stays
    `{header, rows}` and legacy data remains usable.
    """
    if not sheet:
        return sheet
    header = list(sheet.get("header") or [])
    rows = [list(row) for row in (sheet.get("rows") or [])]

    if "paper_key" in header:
        key_index = header.index("paper_key")
    else:
        key_index = len(header)
        header.append("paper_key")
        for row in rows:
            row.append("")

    def derive(values):
        pmid = str(values.get("pmid") or "").strip()
        if pmid:
            return f"pmid:{pmid}"
        doi = str(values.get("doi") or "").strip()
        if doi:
            return f"doi:{doi.lower()}"
        if allow_title:
            return paper_identity(values)
        return ""

    for row in rows:
        if len(row) <= key_index:
            row.extend([""] * (key_index + 1 - len(row)))
        if str(row[key_index] or "").strip():
            continue
        row[key_index] = derive(dict(zip(header, row)))

    return {"header": header, "rows": rows}


def _annotation_summary():
    """Never let a corrupt/missing index break the results endpoint."""
    try:
        return pdf_annotations.api_summary()
    except Exception:
        return {}


def _figure_list(paper_key=None):
    """Never let a corrupt/missing figure index break a results endpoint."""
    try:
        return figures.api_list(paper_key)
    except Exception:
        return []


@app.route("/api/results")
def api_results():
    return jsonify({
        "search": _sheet_with_paper_keys(_read_xlsx(config.SEARCH_RESULTS_XLSX), allow_title=True),
        "extracted": _sheet_with_paper_keys(_read_xlsx(config.EXTRACTED_XLSX)),
        "closed": _read_xlsx(config.CLOSED_SOURCE_XLSX),
        "annotations": _annotation_summary(),
        "figures": _figure_list(),
    })


@app.route("/api/figures")
def api_figures():
    """List figure metadata, optionally filtered by ``?paper_key=``."""
    paper_key = (request.args.get("paper_key") or "").strip()
    return jsonify({"figures": _figure_list(paper_key or None)})


@app.route("/api/figure/<figure_id>")
def api_figure(figure_id):
    """Serve one cached figure PNG by index id.

    Only ids present in output/figures/index.json are accepted, and the stored
    relative path is validated under FIGURES_DIR. No caller-supplied filename
    or path is ever used, so this route cannot serve arbitrary files.
    """
    try:
        path = figures.resolve_figure_path(figure_id)
    except Exception:
        path = None
    if path is None:
        abort(404)
    return send_file(str(path), mimetype="image/png", as_attachment=False)


@app.route("/api/annotated-pdf/<path:paper_key>")
def api_annotated_pdf(paper_key):
    """Serve an annotated PDF, regenerating it or falling back to the original.

    Only keys present in output/pdf_index.json are accepted. The index lookup
    and path helpers reject absolute paths, traversal and files outside the
    configured PDF folders, so this route can never serve arbitrary files.
    """
    try:
        resolved = pdf_annotations.resolve_paper_paths(paper_key)
    except Exception:
        resolved = None
    if not resolved:
        abort(404)

    annotation = resolved.get("annotation_path")
    if annotation is not None:
        return send_file(
            str(annotation), mimetype="application/pdf", as_attachment=False
        )

    source = resolved.get("source_path")
    if source is not None:
        try:
            regenerated = pdf_annotations.regenerate_annotation(paper_key)
        except Exception:
            regenerated = None
        if regenerated is not None:
            return send_file(
                str(regenerated), mimetype="application/pdf", as_attachment=False
            )
        return send_file(
            str(source), mimetype="application/pdf", as_attachment=False
        )

    abort(404)


@app.route("/download/<name>")
def download(name):
    mapping = {
        "search_results.xlsx": config.SEARCH_RESULTS_XLSX,
        "extracted_data.xlsx": config.EXTRACTED_XLSX,
        "closed_source_papers.xlsx": config.CLOSED_SOURCE_XLSX,
        "figures.xlsx": config.FIGURES_XLSX,
    }
    path = mapping.get(name)
    if not path or not Path(path).exists():
        abort(404)
    return send_file(str(path), as_attachment=True, download_name=name)


def _open_folder(path):
    if sys.platform == "win32":
        os.startfile(str(path))
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


@app.route("/open-output", methods=["POST"])
def open_output():
    # The files already live in output/ — this just opens that folder in the
    # OS file manager so users don't have to hunt for it.
    try:
        _open_folder(config.OUTPUT_DIR)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True})


if __name__ == "__main__":
    if config.APP_AUTO_OPEN:
        url = f"http://{config.APP_HOST}:{config.APP_PORT}"
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(
        host=config.APP_HOST,
        port=config.APP_PORT,
        debug=False,
        use_reloader=False,
        threaded=True,
    )
