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
import runs
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
        "cancel_requested": False,
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
            return None, "A task is already running. Cancel it or wait for it to finish, then try again."
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
        "cancel_requested": bool(job.get("cancel_requested", False)),
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


def _cancel_requested(job):
    return bool(job.get("cancel_requested"))


def _runs_enabled():
    return bool(getattr(config, "RUNS_ENABLED", False))


def _run_name(query, local_folder):
    """A readable run name: the query, else the local folder, else None."""
    text = " ".join(str(query or "").split())
    if text:
        return text[:80]
    folder = str(local_folder or "").strip()
    if folder:
        name = Path(folder).name or folder
        return name[:80]
    return None


def _run_query_state(query, query_tree, query_selection):
    selection = runs.sanitize_query_selection(query_selection)
    return {
        "tree": query_tree if isinstance(query_tree, dict) else None,
        "rendered": str(query or ""),
        "selected_paths": selection["selected_paths"],
        "children_selected_paths": selection["children_selected_paths"],
    }


def _run_settings(settings):
    """Only the two non-secret app settings are saved in a run state."""
    return {
        "email": str((settings or {}).get("email") or getattr(config, "NCBI_EMAIL", "")),
        "model": str(
            (settings or {}).get("model") or getattr(config, "OLLAMA_MODEL", "")
        ),
    }


def _count_xlsx_rows(path):
    """Data-row count for an xlsx sheet; None when it cannot be read."""
    p = Path(path)
    if not p.exists():
        return 0
    try:
        wb = load_workbook(p, read_only=True)
        try:
            ws = wb.active
            return max(0, int(ws.max_row or 1) - 1)
        finally:
            wb.close()
    except Exception:
        return None


def _current_counts(run_id=None):
    """Read the active run's artifact counts, keeping old values on errors."""
    previous = {}
    if run_id:
        try:
            state = runs.load_state(run_id) or {}
            previous = state.get("counts") or {}
        except Exception:
            previous = {}

    def count(key, reader):
        try:
            value = reader()
        except Exception:
            value = None
        if value is None:
            try:
                return int(previous.get(key) or 0)
            except (TypeError, ValueError):
                return 0
        return int(value)

    return {
        "papers": count("papers", lambda: _count_xlsx_rows(config.SEARCH_RESULTS_XLSX)),
        "extracted": count("extracted", lambda: _count_xlsx_rows(config.EXTRACTED_XLSX)),
        "closed": count("closed", lambda: _count_xlsx_rows(config.CLOSED_SOURCE_XLSX)),
        "figures": count("figures", lambda: len(_figure_list())),
    }


def _save_run_state(job, run_id, **changes):
    """Best-effort state update: never lose search/extraction output over it."""
    if not run_id:
        return None
    try:
        return runs.update_state(run_id, **changes)
    except Exception as exc:  # noqa: BLE001 - state is a convenience
        message = f"Could not save run state: {exc}"
        app.logger.warning(message)
        job["log"].append(f"Warning: {message}")
        return None


def _finish_extract_run(job, run_id, status):
    """Record extraction bookkeeping on the active run after any outcome."""
    if not run_id:
        return
    _save_run_state(
        job,
        run_id,
        status=status,
        counts=_current_counts(run_id),
        last_extract_at=runs.now(),
    )


def _mark_search_cancelled(job, extra=None):
    """Finish a search job as cancelled without touching any output files."""
    job["status"] = "cancelled"
    job["error"] = None
    result = {"cancelled": True}
    if extra:
        result.update(extra)
    job["result"] = result
    job["log"].append("Search cancelled; previous results were kept.")


def _run_search_job(job, query, sources, max_results, excluded_journals=None,
                    year_from=_YEARS_UNSET, year_to=_YEARS_UNSET,
                    local_folder=_LOCAL_UNSET, local_recursive=_LOCAL_UNSET,
                    query_tree=None, query_selection=None, merge=False):
    run_enabled = _runs_enabled()
    merge = bool(merge and run_enabled)
    previous_paths = None
    new_run_id = None
    run_id = None
    committed = False

    def rollback_new_run():
        """Undo a new run that never produced a complete dataset."""
        nonlocal new_run_id, committed
        if not new_run_id or committed:
            return
        try:
            if previous_paths is not None:
                runs.restore_paths(previous_paths)
            else:
                runs.apply_legacy_paths()
        except Exception as exc:  # noqa: BLE001 - rollback must keep going
            app.logger.warning("Could not restore previous run paths: %s", exc)
            job["log"].append(
                f"Warning: could not restore previous run paths: {exc}"
            )
        try:
            runs.delete_run(new_run_id)
            job["log"].append("Discarded the incomplete run folder.")
        except Exception as exc:  # noqa: BLE001 - rollback must keep going
            app.logger.warning("Could not delete incomplete run: %s", exc)
            job["log"].append(f"Warning: could not delete incomplete run: {exc}")
        new_run_id = None

    def cancel():
        rollback_new_run()
        extra = None
        if run_enabled:
            # A cancelled new search has no run any more; a cancelled refresh
            # still belongs to the active run.
            reported = run_id if merge else None
            extra = {"run_id": reported, "merged": merge, "added": 0}
        _mark_search_cancelled(job, extra)

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

        # ---- Run folder / refresh setup ----------------------------------
        # Everything below reads config paths dynamically, so switching them
        # here is enough to make the whole engine write into this run.
        if run_enabled:
            if merge:
                run_id = runs.current_run_id()
                if not run_id:
                    raise RuntimeError("No run is loaded to refresh.")
                runs.apply_run_paths(run_id)
                job["log"].append(
                    f"Refreshing run {run_id}; extraction outputs are kept."
                )
            else:
                previous_paths = runs.capture_paths()
                initial_state = {
                    "name": _run_name(query, local_folder),
                    "status": "searching",
                    "query": _run_query_state(query, query_tree, query_selection),
                    "sources": sources,
                    "max_results": max_results,
                    "year_from": year_from,
                    "year_to": year_to,
                    "excluded_journals": terms,
                    "local_folder": local_folder,
                    "local_recursive": local_recursive,
                    "local_only": not online,
                    "extraction": {
                        "goal": "",
                        "columns": [],
                        "mode": getattr(config, "EXTRACTION_MODE", "records"),
                    },
                    "settings": _run_settings(settings),
                    "counts": {
                        "papers": 0, "extracted": 0, "closed": 0, "figures": 0,
                    },
                }
                new_run_id = runs.create_run(
                    name=initial_state["name"], state=initial_state
                )
                if not new_run_id:
                    raise RuntimeError("Could not create a run folder.")
                run_id = new_run_id
                runs.apply_run_paths(run_id)
                saved_state = runs.load_state(run_id) or {}
                job["log"].append(
                    f"New run: {saved_state.get('name') or run_id} ({run_id})"
                )

        # Scan inside the job so a large folder reports progress instead of
        # blocking the request. Invalid folders are also rejected by /api/search.
        # Cancellation is cooperative: a scan that is already running finishes,
        # then the job stops before any online search or output write.
        local_papers, local_stats = [], None
        if _cancel_requested(job):
            cancel()
            return
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

        if _cancel_requested(job):
            cancel()
            return

        if online:
            try:
                papers = searchers.search(
                    query, sources, limit=max_results,
                    year_from=year_from, year_to=year_to,
                    cancel_check=lambda: _cancel_requested(job),
                )
            except searchers.SearchCancelled:
                cancel()
                return
        else:
            papers = []
            job["log"].append("Online search skipped (folder-only).")

        # The callback is checked between sources, so the last source may have
        # finished after a cancel click. Check once more before any existing
        # output is replaced.
        if _cancel_requested(job):
            cancel()
            return

        # On a refresh, merge with the sheet that already lives in this run so
        # the operation is additive and never starts a new dataset. Local PDFs
        # are merged first so a local copy matches a new online record.
        if merge:
            existing_papers = _load_search_results(required=False)
            if local_papers:
                before_local = len(papers)
                papers = local_pdfs.merge_local_papers(papers, local_papers)
                local_added = len(papers) - before_local
                job["log"].append(
                    f"Merged local PDFs: {len(local_papers) - local_added} matched "
                    f"existing paper(s), {local_added} added as new paper(s)."
                )
            before_merge = len(existing_papers)
            papers = searchers.merge_papers(existing_papers, papers)
            added = max(0, len(papers) - before_merge)
        else:
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
        if not merge:
            added = len(rows)

        if merge:
            # Keep the existing extraction/closed/checkpoint/figure outputs.
            writer.write_xlsx(config.SEARCH_RESULTS_XLSX, SEARCH_COLS, rows)
            local_pdfs.write_index(papers, replace=False)
            job["log"].append(
                "Refreshed the search sheet; extraction outputs and "
                "checkpoint were kept."
            )
        else:
            # A new search starts a fresh dataset: replace the search sheet AND
            # clear any previous extraction outputs + resume state, so Extract
            # only ever works on this search's papers. The index is replaced too,
            # so old local mappings can never point at rows from the previous
            # dataset.
            writer.write_xlsx(config.SEARCH_RESULTS_XLSX, SEARCH_COLS, rows)
            for path in (config.EXTRACTED_XLSX, config.CLOSED_SOURCE_XLSX):
                if Path(path).exists():
                    Path(path).unlink()
            Checkpoint(config.CHECKPOINT_JSON).reset()
            local_pdfs.write_index(papers, replace=True)
            # Figures belong to the previous dataset too: clear the index, xlsx
            # and cached PNGs so stale thumbnails can never appear.
            try:
                cleared = figures.clear()
                if (cleared or {}).get("warning"):
                    job["log"].append(f"Warning: {cleared['warning']}")
            except Exception as exc:  # noqa: BLE001 - cleanup is best-effort
                app.logger.warning("Could not clear figure gallery: %s", exc)
                job["log"].append(f"Warning: could not clear old figures: {exc}")

        # From here on the dataset is complete: a later error must not delete
        # the new run or restore the old paths.
        committed = True

        if run_enabled and run_id:
            if merge:
                counts = _current_counts(run_id)
                counts["papers"] = len(rows)
            else:
                counts = {
                    "papers": len(rows), "extracted": 0, "closed": 0, "figures": 0,
                }
            _save_run_state(
                job,
                run_id,
                status="ready",
                query=_run_query_state(query, query_tree, query_selection),
                sources=sources,
                max_results=max_results,
                year_from=year_from,
                year_to=year_to,
                excluded_journals=terms,
                local_folder=local_folder,
                local_recursive=local_recursive,
                local_only=not online,
                counts=counts,
                last_search_at=runs.now(),
                settings=_run_settings(settings),
            )
            try:
                runs.set_active(run_id)
            except Exception as exc:  # noqa: BLE001 - marker is best-effort
                message = f"Could not mark run as active: {exc}"
                app.logger.warning(message)
                job["log"].append(f"Warning: {message}")

        job["result"] = {
            "papers": rows,
            "added": added,
            "total": len(rows),
            "excluded": len(excluded),
            "year_from": year_from,
            "year_to": year_to,
            "year_range": range_label or "all years",
            "local_folder": local_folder,
            "local_recursive": local_recursive,
            "local_count": len(local_papers),
            "local_stats": local_stats,
            "run_id": run_id if run_enabled else None,
            "merged": bool(merge),
        }
        job["status"] = "done"
        job["log"].append(f"Excluded {len(excluded)} paper(s) by journal blocklist.")
        year_note = f"; years {range_label}" if range_label else ""
        local_note = (
            f"; {len(local_papers)} local PDF(s)" if local_folder else ""
        )
        if merge:
            job["log"].append(
                f"Done: {len(rows)} papers in the sheet "
                f"({added} new, {len(excluded)} excluded by journal"
                f"{year_note}{local_note}; extraction data kept)."
            )
        else:
            job["log"].append(
                f"Done: {len(rows)} papers in the sheet "
                f"({len(excluded)} excluded by journal{year_note}{local_note}; "
                "previous data cleared)."
            )
    except searchers.SearchCancelled:
        cancel()
    except Exception as e:
        rollback_new_run()
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
    run_id = None
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

        # Runs: extraction belongs to the loaded search dataset. Never create a
        # run here — with no active run and no legacy data, extraction keeps its
        # existing "no search results" behaviour on the global paths.
        if _runs_enabled():
            try:
                run_id = runs.ensure_active_run()
                if run_id:
                    _save_run_state(
                        job,
                        run_id,
                        status="extracting",
                        extraction={
                            "goal": goal,
                            "columns": list(columns or []),
                            "mode": mode,
                        },
                    )
            except Exception as exc:  # noqa: BLE001 - extraction must continue
                message = f"Could not activate the run folder: {exc}"
                app.logger.warning(message)
                job["log"].append(f"Warning: {message}")

        job["log"].append(f"Extraction mode: {mode}")

        papers = _load_search_results(required=False)
        if not papers:
            job["status"] = "done"
            job["result"] = {
                "extracted": 0,
                "closed": 0,
                "papers_processed": 0,
                "mode": mode,
                "cancelled": False,
            }
            job["log"].append("No search results yet — run a search first.")
            _finish_extract_run(job, run_id, "done")
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
        n_figures_candidates = n_figures_matched = n_figures_skipped = 0
        n_figure_candidate_capped = n_figure_warnings = 0
        first_write = True
        processed = 0
        cancelled = False
        for i, p in enumerate(todo):
            if _cancel_requested(job):
                cancelled = True
                break
            k = paper_identity(p)
            title = (p.get("title") or "")[:60]
            job["progress"]["done"] = processed
            job["progress"]["current"] = title
            job["log"].append(f"[{i + 1}/{len(todo)}] {title}")

            try:
                text, source, pdf = _prepare_paper(p)
            except Exception as e:
                cp.mark(k, "error", error=str(e))
                job["log"].append(f"   !! fetch error: {e}")
                processed += 1
                if _cancel_requested(job):
                    cancelled = True
                    break
                continue

            pdf_file = pdf.get("filename") or ""
            pdf_source = pdf.get("label") or "none"

            if not text:
                reason = PDF_NO_TEXT_REASON if pdf.get("path") else (pdf.get("reason") or NO_PDF_REASON)
                writer.append_xlsx(config.CLOSED_SOURCE_XLSX, CLOSED_COLS, [_closed_row(p, reason)])
                cp.mark(k, "closed", reason=reason, pdf_file=pdf_file, pdf_source=pdf_source)
                n_closed += 1
                job["log"].append(f"   -> closed source ({reason})")
                processed += 1
                if _cancel_requested(job):
                    cancelled = True
                    break
                continue

            try:
                rows = extractor.extract(text, fields=fields, instruction=instruction)
            except Exception as e:
                cp.mark(k, "error", error=str(e))
                job["log"].append(f"   !! extract error: {e}")
                processed += 1
                if _cancel_requested(job):
                    cancelled = True
                    break
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

            # Goal-driven figure search is independent of evidence and of the
            # number of extracted rows: only candidates matching the extraction
            # goal are saved. Best-effort: never fail the extraction job.
            if getattr(config, "FIGURES_ENABLED", True) and pdf.get("path"):
                try:
                    figure_summary = figures.process_paper(
                        pdf.get("path"),
                        k,
                        title=p.get("title", ""),
                        pdf_file=pdf_file,
                        pdf_source=pdf_source,
                        goal=goal,
                    )
                    n_figures_saved += int(
                        (figure_summary or {}).get("saved") or 0
                    )
                    n_figures_candidates += int(
                        (figure_summary or {}).get("candidates") or 0
                    )
                    n_figures_matched += int(
                        (figure_summary or {}).get("matched") or 0
                    )
                    n_figures_skipped += int(
                        (figure_summary or {}).get("skipped") or 0
                    )
                    n_figure_duplicates += int(
                        (figure_summary or {}).get("duplicates") or 0
                    )
                    if (figure_summary or {}).get("capped"):
                        n_figures_capped += 1
                    if (figure_summary or {}).get("candidate_capped"):
                        n_figure_candidate_capped += 1
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
            processed += 1
            # Cancellation is cooperative: the paper above always completes and
            # its rows/checkpoint entry are already saved before this check.
            if _cancel_requested(job):
                cancelled = True
                break

        result = {
            "extracted": n_extracted,
            "closed": n_closed,
            "papers_processed": processed if cancelled else len(todo),
            "mode": mode,
            "evidence_located": n_evidence_located,
            "evidence_unresolved": n_evidence_unresolved,
            "annotation_warnings": n_annotation_warnings,
            "figures_saved": n_figures_saved,
            "figures_candidates": n_figures_candidates,
            "figures_matched": n_figures_matched,
            "figures_skipped": n_figures_skipped,
            "figure_duplicates": n_figure_duplicates,
            "figures_capped": n_figures_capped,
            "figure_candidate_capped": n_figure_candidate_capped,
            "figure_warnings": n_figure_warnings,
            "cancelled": cancelled,
        }
        job["result"] = result
        if cancelled:
            job["progress"]["done"] = processed
            job["progress"]["current"] = ""
            job["status"] = "cancelled"
            job["log"].append(
                f"Extraction cancelled after {processed} paper(s); "
                "completed rows were kept."
            )
            _finish_extract_run(job, run_id, "cancelled")
            return

        job["progress"]["done"] = len(todo)
        job["progress"]["current"] = ""
        job["status"] = "done"
        job["log"].append(f"Done: {n_extracted} row(s) extracted, {n_closed} paper(s) closed.")
        _finish_extract_run(job, run_id, "done")
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        job["log"].append(f"Error: {e}")
        _finish_extract_run(job, run_id, "error")


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


@app.route("/api/suggest-goal", methods=["POST"])
def api_suggest_goal():
    """LLM-backed rewrite of the extraction goal.

    Request body:
      {"goal": "...", "columns": ["mutation"], "mode": "records"}
    Success:
      {"source": "llm", "model": "...", "suggestions": ["...", ...]}

    This endpoint only reads the current goal/columns/mode. It never starts an
    extraction job and never records prompt history; those happen only when the
    user confirms a goal and clicks Extract data.
    """
    data = request.get_json(force=True, silent=True) or {}
    raw_goal = data.get("goal")
    goal = raw_goal.strip() if isinstance(raw_goal, str) else ""
    if not goal:
        return jsonify({"error": "Describe what data you want to extract first."}), 400

    try:
        suggestions = suggester.suggest_goal(
            goal,
            columns=data.get("columns"),
            mode=data.get("mode") or "records",
        )
        return jsonify({**suggester.llm_source(), "suggestions": suggestions})
    except suggester.BackendUnavailable as e:
        return jsonify({"error": str(e)}), 503
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:  # unexpected LLM/parse failure: report, don't crash
        return jsonify({"error": f"Could not improve the goal: {e}"}), 502


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


def _ensure_active_run_for_read():
    """Prepare the active run without switching paths under a running job.

    A search job has already switched to its (possibly brand-new) run paths.
    A page reload must not call ``ensure_active_run()`` and repoint them back at
    the previous marker while the job is still writing. The next list refresh
    after the job finishes repairs the marker normally.
    """
    with JOB_LOCK:
        if _active_job():
            return None
    try:
        return runs.ensure_active_run()
    except Exception as exc:  # noqa: BLE001 - reading must still work
        app.logger.warning("Could not prepare the active run: %s", exc)
        return None


@app.route("/api/runs")
def api_runs():
    """List saved runs and the currently loaded one.

    The response only contains state metadata (never filesystem paths). When
    runs are enabled, ``ensure_active_run()`` is called first so a missing or
    corrupt marker is repaired best-effort.
    """
    if not _runs_enabled():
        return jsonify({"enabled": False, "active": None, "runs": []})
    _ensure_active_run_for_read()
    active_id = runs.current_run_id()
    items = runs.list_runs()
    active = next((item for item in items if item["id"] == active_id), None)
    return jsonify({"enabled": True, "active": active, "runs": items})


@app.route("/api/run")
def api_run():
    """Return the active run's metadata and its full (non-secret) state."""
    if not _runs_enabled():
        return jsonify({"enabled": False, "run": None, "state": None})
    _ensure_active_run_for_read()
    active_id = runs.current_run_id()
    if not active_id:
        return jsonify({"enabled": True, "run": None, "state": None})
    state = runs.load_state(active_id)
    if state is None:
        return jsonify({"enabled": True, "run": None, "state": None})
    active = next(
        (item for item in runs.list_runs() if item["id"] == active_id), None
    )
    return jsonify({"enabled": True, "run": active, "state": state})


def _job_running_response():
    return jsonify({
        "error": "A task is already running. Cancel it or wait for it to "
                 "finish, then try again."
    }), 409


@app.route("/api/runs/<run_id>/load", methods=["POST"])
def api_run_load(run_id):
    """Load a saved run. This restores state/paths but never starts a job."""
    if not _runs_enabled():
        return jsonify({"error": "Run folders are disabled."}), 404
    with JOB_LOCK:
        if _active_job():
            return _job_running_response()
        try:
            state = runs.activate_run(run_id)
        except ValueError:
            return jsonify({"error": "Run does not exist."}), 404
        if state is None:
            return jsonify({"error": "Run does not exist."}), 404
        active = next(
            (item for item in runs.list_runs() if item["id"] == state["id"]),
            None,
        )
    return jsonify({"run": active, "state": state})


@app.route("/api/runs/<run_id>/delete", methods=["POST"])
def api_run_delete(run_id):
    """Delete a non-active run folder. The active run must be switched first."""
    if not _runs_enabled():
        return jsonify({"error": "Run folders are disabled."}), 404
    with JOB_LOCK:
        if _active_job():
            return _job_running_response()
        active_id = runs.current_run_id()
        if active_id and str(run_id) == active_id:
            return jsonify({
                "error": "Cannot delete the active run. Load another run first."
            }), 409
        try:
            deleted = runs.delete_run(run_id)
        except ValueError:
            return jsonify({"error": "Run does not exist."}), 404
    if not deleted:
        return jsonify({"error": "Run does not exist."}), 404
    return jsonify({"deleted": run_id})


@app.route("/api/search", methods=["POST"])
def api_search():
    data = request.get_json(force=True, silent=True) or {}
    query = (data.get("query") or "").strip()
    sources = data.get("sources") or []
    if isinstance(sources, str):
        sources = [sources]
    sources = [str(s).strip() for s in sources if str(s).strip()]
    merge = _as_bool(data.get("merge"))
    query_selection = data.get("query_selection")
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

    if merge:
        if not _runs_enabled():
            return jsonify({
                "error": "Refresh search requires run folders, which are disabled."
            }), 400
        if not runs.current_run_id():
            return jsonify({"error": "No run is loaded to refresh."}), 400

    # Keep the legacy positional shape when runs are disabled: older clients
    # (and the existing API tests) do not know about the two run arguments.
    search_args = (
        query, sources, max_results, excluded_journals, year_from, year_to,
        local_folder, local_recursive, data.get("query_tree"),
    )
    if _runs_enabled():
        search_args += (query_selection, merge)
    job, err = _start_job("search", _run_search_job, *search_args)
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


@app.route("/api/job/<job_id>/cancel", methods=["POST"])
def api_job_cancel(job_id):
    """Request cooperative cancellation of a running job.

    The job stays `running` until its body reaches the next cancellation check;
    an in-flight HTTP or LLM call always finishes first. Repeated requests while
    the job is still running are accepted.
    """
    with JOB_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "unknown job"}), 404
        if job["status"] != "running":
            return jsonify({
                "error": "job is not running",
                "status": job["status"],
            }), 409
        first_request = not job.get("cancel_requested")
        job["cancel_requested"] = True
        if first_request:
            job["log"].append(
                "Cancel requested; stopping after the current step."
            )
        return jsonify({
            "cancelled": True,
            "status": "running",
            "cancel_requested": True,
        })


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


def _sheet_with_publication_type(sheet):
    """Expose the pipeline's publication_type column for legacy workbooks.

    The file itself is migrated by writer.ensure_columns the next time a search
    or extraction runs. Reading an old sheet must still return a consistent
    shape for the web table, so a missing column is appended in memory with
    blank values (never losing or reordering existing data).
    """
    if not sheet:
        return sheet
    header = list(sheet.get("header") or [])
    if "publication_type" in header:
        return sheet
    old_width = len(header)
    header.append("publication_type")
    rows = []
    for row in (sheet.get("rows") or []):
        values = list(row)
        if len(values) < old_width:
            values.extend([""] * (old_width - len(values)))
        values.append("")
        rows.append(values)
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
        "search": _sheet_with_publication_type(
            _sheet_with_paper_keys(_read_xlsx(config.SEARCH_RESULTS_XLSX), allow_title=True)
        ),
        "extracted": _sheet_with_publication_type(
            _sheet_with_paper_keys(_read_xlsx(config.EXTRACTED_XLSX))
        ),
        "closed": _sheet_with_publication_type(_read_xlsx(config.CLOSED_SOURCE_XLSX)),
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
    # The files live in output/ or in the active run folder — this just opens
    # that folder in the OS file manager so users don't have to hunt for it.
    try:
        _open_folder(config.ACTIVE_RUN_DIR or config.OUTPUT_DIR)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True})


if __name__ == "__main__":
    # `python app.py` / start.sh use per-run folders. Importing app (tests, CLI
    # helpers) keeps RUNS_ENABLED=False and the legacy global output paths.
    config.RUNS_ENABLED = True
    try:
        runs.ensure_active_run()
    except Exception as exc:  # noqa: BLE001 - app must still start
        print(f"Warning: could not prepare run folders: {exc}")
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
