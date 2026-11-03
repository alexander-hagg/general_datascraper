"""
runs.py — one folder per web search dataset, plus savable application state.

A **run** is everything that belongs to one search dataset:

    output/runs/<run-id>/
        state.json                 search form, extraction form, counts
        search_results.xlsx        papers found by Search
        extracted_data.xlsx        rows written by Extract
        closed_source_papers.xlsx  papers without extractable full text
        checkpoint.json            resume state for Extract
        local_pdfs.json            local-PDF index
        pdf_index.json             evidence/annotation index
        pdfs/                      archived article/preprint/local PDFs
        annotated_pdfs/            cached highlighted copies
        figures/                   figure index + cached PNGs
        figures.xlsx               figure metadata

The web app enables runs at startup (`python app.py` / the start scripts). The
CLI and unit tests use the legacy global paths under `config.OUTPUT_DIR` unless
`config.RUNS_ENABLED` is explicitly turned on. Every public entry point checks
that flag first and returns ``None``/``[]``/``False`` without touching paths when
runs are disabled.

Switching a run does **not** move or copy its files. `apply_run_paths()` points
the existing config path attributes at the run's files; every engine module reads
those attributes at call time. `web_settings.json` and `prompt_history.json`
deliberately stay global, and `OUTPUT_DIR` itself is never changed.

`output/active_run.json` is a small marker with the loaded run id. If it is
missing or corrupt, `ensure_active_run()` falls back to the newest valid run. If
there is no run yet, the first legacy `output/` artifacts are imported once into
a run named "Imported run"; the source files are copied/hardlinked, never moved
or deleted.
"""

import json
import os
import re
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import config

RUN_STATE_VERSION = 1
RUN_NAME_MAX = 120

# One lock covers every read-modify-write cycle. The Flask app runs as a single
# process with one job at a time, so an RLock is enough.
_LOCK = threading.RLock()

# A run id is also a directory name. Keep it conservative: no separators, no
# absolute paths, no "..", and a bounded length.
_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
_OPERATORS = frozenset(("AND", "OR", "NOT"))
_EXTRACTION_MODES = frozenset(("records", "table"))

# config attribute -> path inside a run folder. Every module reads these
# attributes dynamically, so switching a run is just this mapping.
_PATH_RELS = {
    "SEARCH_RESULTS_XLSX": "search_results.xlsx",
    "CLOSED_SOURCE_XLSX": "closed_source_papers.xlsx",
    "EXTRACTED_XLSX": "extracted_data.xlsx",
    "CHECKPOINT_JSON": "checkpoint.json",
    "LOCAL_PDF_INDEX_JSON": "local_pdfs.json",
    "PDF_INDEX_JSON": "pdf_index.json",
    "ANNOTATED_PDF_DIR": "annotated_pdfs",
    "PDF_DIR": "pdfs",
    "FIGURES_DIR": "figures",
    "FIGURES_INDEX_JSON": "figures/index.json",
    "FIGURES_XLSX": "figures.xlsx",
}

_RUN_SUBDIRS = ("pdfs", "annotated_pdfs", "figures")

# Legacy artifacts that may be imported into the first run. web_settings.json,
# prompt_history.json and runs/ are intentionally absent.
_LEGACY_FILES = (
    "search_results.xlsx",
    "extracted_data.xlsx",
    "closed_source_papers.xlsx",
    "checkpoint.json",
    "local_pdfs.json",
    "pdf_index.json",
    "figures.xlsx",
)
_LEGACY_DIRS = ("pdfs", "annotated_pdfs", "figures")


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _enabled():
    return bool(getattr(config, "RUNS_ENABLED", False))


def _runs_dir():
    return Path(getattr(config, "RUNS_DIR", config.OUTPUT_DIR / "runs"))


def _marker_path():
    return Path(
        getattr(config, "ACTIVE_RUN_JSON", config.OUTPUT_DIR / "active_run.json")
    )


def now():
    """Current UTC timestamp used in state/marker files (ISO 8601)."""
    return datetime.now(timezone.utc).isoformat()


def validate_run_id(run_id):
    """Return a safe run id or raise ValueError.

    Accepts only ``^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$``. Absolute paths,
    separators and ``..`` are therefore rejected before they reach the
    filesystem.
    """
    if not isinstance(run_id, str):
        raise ValueError("Invalid run id.")
    value = run_id
    if value in (".", "..") or "/" in value or "\\" in value:
        raise ValueError("Invalid run id.")
    if not _RUN_ID_RE.fullmatch(value):
        raise ValueError(
            "Invalid run id. Use 1-80 letters, digits, dots, dashes or underscores."
        )
    return value


def _run_folder(run_id):
    return _runs_dir() / validate_run_id(run_id)


def _write_json_atomic(path, payload):
    """Write JSON through a temp file in the same directory, then replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(
                payload, handle, indent=2, ensure_ascii=False, allow_nan=False
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def _copy_or_link(source, destination):
    """Hardlink when possible, otherwise copy. Never touches the source."""
    source = Path(source)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        if destination.exists():
            destination.unlink()
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)
    return destination


# --------------------------------------------------------------------------
# State schema
# --------------------------------------------------------------------------

def _clean_text(value, limit=None):
    text = "" if value is None else str(value)
    if limit is not None and len(text) > limit:
        text = text[:limit]
    return text


def _clean_int(value, default=0):
    if isinstance(value, bool):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, number)


def _clean_year(value):
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) == 4 and text.isdigit():
        return text
    return None


def _clean_string_list(value, limit=200):
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    out = []
    for item in value[:limit]:
        if item is None:
            continue
        text = str(item).strip()
        if text:
            out.append(text)
    return out


def _sanitize_tree(node):
    """Keep the documented query-tree shape and drop browser-only node ids."""
    if not isinstance(node, dict):
        return None
    node_type = node.get("type")
    if node_type == "term":
        text = node.get("text")
        stored = text if isinstance(text, str) else ("" if text is None else str(text))
        op = node.get("op")
        return {
            "type": "term",
            "text": stored,
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


def _clean_path(raw):
    """One selection path: a list of non-negative child indices, or None."""
    if not isinstance(raw, (list, tuple)):
        return None
    path = []
    for value in raw:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        path.append(value)
    return path


def sanitize_query_selection(value):
    """Return ``{selected_paths, children_selected_paths}`` with only valid paths.

    Malformed values are ignored rather than failing a search: the state file is
    a convenience snapshot, not an authority over the query tree.
    """
    result = {"selected_paths": [], "children_selected_paths": []}
    if not isinstance(value, dict):
        return result
    for key in ("selected_paths", "children_selected_paths"):
        raw = value.get(key)
        if not isinstance(raw, (list, tuple)):
            continue
        seen = set()
        for item in raw:
            path = _clean_path(item)
            if path is None:
                continue
            marker = tuple(path)
            if marker in seen:
                continue
            seen.add(marker)
            result[key].append(path)
    return result


def _blank_state(run_id, name=None):
    stamp = now()
    try:
        mode = str(
            getattr(config, "EXTRACTION_MODE", "records") or "records"
        ).strip().lower()
    except (TypeError, ValueError):
        mode = "records"
    if mode not in _EXTRACTION_MODES:
        mode = "records"
    return {
        "schema_version": RUN_STATE_VERSION,
        "id": run_id,
        "name": _clean_text(name or f"Run {stamp}", RUN_NAME_MAX).strip()
        or f"Run {stamp}",
        "created_at": stamp,
        "updated_at": stamp,
        "status": "created",
        "query": {
            "tree": None,
            "rendered": "",
            "selected_paths": [],
            "children_selected_paths": [],
        },
        "sources": [],
        "max_results": _clean_int(
            getattr(config, "DEFAULT_MAX_RESULTS", 50), 50
        ),
        "year_from": None,
        "year_to": None,
        "excluded_journals": [],
        "local_folder": "",
        "local_recursive": False,
        "local_only": False,
        "extraction": {"goal": "", "columns": [], "mode": mode},
        "settings": {"email": "", "model": ""},
        "counts": {"papers": 0, "extracted": 0, "closed": 0, "figures": 0},
        "last_search_at": None,
        "last_extract_at": None,
    }


def _sanitize_state(run_id, raw, name=None):
    """Build a schema-v1 state from ``raw``.

    Unknown keys are dropped. In particular, arbitrary settings (API keys,
    backend URLs) and output paths can never reach state.json.
    """
    state = _blank_state(run_id, name)
    if not isinstance(raw, dict):
        return state

    state["name"] = _clean_text(
        name if name is not None else raw.get("name") or state["name"],
        RUN_NAME_MAX,
    ).strip() or state["name"]
    state["created_at"] = _clean_text(raw.get("created_at")) or state["created_at"]
    state["updated_at"] = _clean_text(raw.get("updated_at")) or state["updated_at"]
    state["status"] = _clean_text(raw.get("status") or state["status"], 40)

    query = raw.get("query")
    if isinstance(query, dict):
        selection = sanitize_query_selection(query)
        state["query"] = {
            "tree": _sanitize_tree(query.get("tree")),
            "rendered": _clean_text(query.get("rendered"), 4000),
            "selected_paths": selection["selected_paths"],
            "children_selected_paths": selection["children_selected_paths"],
        }

    state["sources"] = _clean_string_list(raw.get("sources"))
    state["max_results"] = _clean_int(
        raw.get("max_results"), state["max_results"]
    )
    state["year_from"] = _clean_year(raw.get("year_from"))
    state["year_to"] = _clean_year(raw.get("year_to"))
    state["excluded_journals"] = _clean_string_list(
        raw.get("excluded_journals")
    )
    state["local_folder"] = _clean_text(raw.get("local_folder"))
    state["local_recursive"] = bool(raw.get("local_recursive"))
    state["local_only"] = bool(raw.get("local_only"))

    extraction = raw.get("extraction")
    if isinstance(extraction, dict):
        mode = _clean_text(extraction.get("mode") or state["extraction"]["mode"])
        if mode not in _EXTRACTION_MODES:
            mode = "records"
        state["extraction"] = {
            "goal": _clean_text(extraction.get("goal")),
            "columns": _clean_string_list(extraction.get("columns")),
            "mode": mode,
        }

    settings = raw.get("settings")
    if isinstance(settings, dict):
        # Only these two non-secret app settings are ever stored.
        state["settings"] = {
            "email": _clean_text(settings.get("email")),
            "model": _clean_text(settings.get("model")),
        }

    counts = raw.get("counts")
    if isinstance(counts, dict):
        state["counts"] = {
            key: _clean_int(counts.get(key), 0)
            for key in ("papers", "extracted", "closed", "figures")
        }

    state["last_search_at"] = _clean_text(raw.get("last_search_at")) or None
    state["last_extract_at"] = _clean_text(raw.get("last_extract_at")) or None
    return state


def _metadata(state):
    """API-safe run metadata. Deliberately contains no filesystem paths."""
    query = _clean_text((state.get("query") or {}).get("rendered"))
    return {
        "id": state["id"],
        "name": state.get("name") or state["id"],
        "created_at": state.get("created_at"),
        "updated_at": state.get("updated_at"),
        "status": state.get("status"),
        "counts": dict(state.get("counts") or {}),
        "query": query,
        "rendered_query": query,
        "last_search_at": state.get("last_search_at"),
        "last_extract_at": state.get("last_extract_at"),
    }


# --------------------------------------------------------------------------
# Reading / writing runs
# --------------------------------------------------------------------------

def run_dir(run_id):
    """Return the validated folder for ``run_id`` (None when runs are off)."""
    if not _enabled():
        return None
    return _run_folder(run_id)


def load_state(run_id):
    """Load one run's state, or None when missing/corrupt/disabled."""
    if not _enabled():
        return None
    try:
        folder = _run_folder(run_id)
    except ValueError:
        return None
    try:
        raw = (folder / "state.json").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return _sanitize_state(folder.name, data)


def save_state(run_id, state):
    """Atomically save one run's state and return the normalised copy."""
    if not _enabled():
        return None
    with _LOCK:
        folder = _run_folder(run_id)
        if not folder.is_dir():
            raise FileNotFoundError(f"Run folder does not exist: {folder.name}")
        cleaned = _sanitize_state(folder.name, state)
        _write_json_atomic(folder / "state.json", cleaned)
        return cleaned


def update_state(run_id, **changes):
    """Merge ``changes`` into a run's state and save it.

    The top-level dicts ``query``, ``extraction``, ``settings`` and ``counts``
    are merged, so callers can update one nested field without rebuilding the
    whole document. Unknown keys are dropped by the schema sanitiser.
    """
    if not _enabled():
        return None
    with _LOCK:
        state = load_state(run_id)
        if state is None:
            return None
        for key, value in changes.items():
            if key in ("query", "extraction", "settings", "counts") and isinstance(
                value, dict
            ):
                merged = dict(state.get(key) or {})
                merged.update(value)
                state[key] = merged
            else:
                state[key] = value
        state["updated_at"] = now()
        return save_state(run_id, state)


def _new_run_id():
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for _ in range(20):
        candidate = f"{stamp}-{uuid.uuid4().hex[:4]}"
        if not (_runs_dir() / candidate).exists():
            return candidate
    raise RuntimeError("Could not allocate a unique run id.")


def create_run(name=None, state=None):
    """Create an empty run folder with state.json and return its id."""
    if not _enabled():
        return None
    with _LOCK:
        run_id = _new_run_id()
        folder = _runs_dir() / run_id
        folder.mkdir(parents=True, exist_ok=False)
        try:
            for subdir in _RUN_SUBDIRS:
                (folder / subdir).mkdir(parents=True, exist_ok=True)
            raw = state if isinstance(state, dict) else {}
            cleaned = _sanitize_state(run_id, raw, name=name)
            _write_json_atomic(folder / "state.json", cleaned)
        except Exception:
            # A half-created run must not linger in the run list.
            shutil.rmtree(folder, ignore_errors=True)
            raise
        return run_id


def list_runs():
    """Return safe metadata for every valid run, newest updated first."""
    if not _enabled():
        return []
    base = _runs_dir()
    try:
        entries = list(base.iterdir())
    except (FileNotFoundError, NotADirectoryError, OSError):
        return []
    runs = []
    for entry in entries:
        try:
            if not entry.is_dir() or not _RUN_ID_RE.fullmatch(entry.name):
                continue
        except OSError:
            continue
        state = load_state(entry.name)
        if state is None:
            continue
        runs.append(_metadata(state))
    runs.sort(
        key=lambda item: (item.get("updated_at") or "", item.get("id") or ""),
        reverse=True,
    )
    return runs


# --------------------------------------------------------------------------
# Active marker
# --------------------------------------------------------------------------

def _read_marker():
    try:
        raw = _marker_path().read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_marker(run_id):
    _write_json_atomic(
        _marker_path(), {"run_id": run_id, "updated_at": now()}
    )


def _clear_marker():
    try:
        _marker_path().unlink()
    except FileNotFoundError:
        return
    except OSError:
        return


def current_run_id():
    """Return the marker's run id only when it names a valid existing run."""
    if not _enabled():
        return None
    run_id = _read_marker().get("run_id")
    try:
        folder = _run_folder(run_id)
    except ValueError:
        return None
    if not (folder / "state.json").is_file():
        return None
    if load_state(folder.name) is None:
        return None
    return folder.name


def set_active(run_id):
    """Point the active-run marker at an existing run."""
    if not _enabled():
        return None
    folder = _run_folder(run_id)
    if not (folder / "state.json").is_file() or load_state(folder.name) is None:
        raise ValueError("Run does not exist.")
    with _LOCK:
        _write_marker(folder.name)
    return folder.name


# --------------------------------------------------------------------------
# Path switching
# --------------------------------------------------------------------------

def _legacy_paths():
    """The original global output paths derived from config.OUTPUT_DIR."""
    out = Path(getattr(config, "OUTPUT_DIR", config.BASE_DIR / "output"))
    return {
        "SEARCH_RESULTS_XLSX": out / "search_results.xlsx",
        "CLOSED_SOURCE_XLSX": out / "closed_source_papers.xlsx",
        "EXTRACTED_XLSX": out / "extracted_data.xlsx",
        "CHECKPOINT_JSON": out / "checkpoint.json",
        "LOCAL_PDF_INDEX_JSON": out / "local_pdfs.json",
        "PDF_INDEX_JSON": out / "pdf_index.json",
        "ANNOTATED_PDF_DIR": out / "annotated_pdfs",
        "PDF_DIR": out / "pdfs",
        "FIGURES_DIR": out / "figures",
        "FIGURES_INDEX_JSON": out / "figures" / "index.json",
        "FIGURES_XLSX": out / "figures.xlsx",
    }


def apply_run_paths(run_id):
    """Point every output attribute at this run and create its subfolders."""
    if not _enabled():
        return None
    folder = _run_folder(run_id)
    folder.mkdir(parents=True, exist_ok=True)
    for subdir in _RUN_SUBDIRS:
        (folder / subdir).mkdir(parents=True, exist_ok=True)
    for attribute, relative in _PATH_RELS.items():
        setattr(config, attribute, folder / relative)
    config.ACTIVE_RUN_DIR = folder
    return folder


def apply_legacy_paths():
    """Restore the global output/ paths and clear ACTIVE_RUN_DIR."""
    if not _enabled():
        return None
    for attribute, value in _legacy_paths().items():
        setattr(config, attribute, value)
    config.ACTIVE_RUN_DIR = None
    return Path(config.OUTPUT_DIR)


def capture_paths():
    """Snapshot all switched path attributes for a later rollback."""
    captured = {
        attribute: getattr(config, attribute, None)
        for attribute in _PATH_RELS
    }
    captured["ACTIVE_RUN_DIR"] = getattr(config, "ACTIVE_RUN_DIR", None)
    return captured


def restore_paths(mapping):
    """Restore a mapping produced by ``capture_paths()``."""
    if not isinstance(mapping, dict):
        return None
    for attribute in _PATH_RELS:
        if attribute in mapping:
            setattr(config, attribute, mapping[attribute])
    if "ACTIVE_RUN_DIR" in mapping:
        config.ACTIVE_RUN_DIR = mapping["ACTIVE_RUN_DIR"]
    return None


# --------------------------------------------------------------------------
# Activation, deletion, legacy import
# --------------------------------------------------------------------------

def activate_run(run_id):
    """Load a run, switch the paths and make it active. No job is started."""
    if not _enabled():
        return None
    state = load_state(run_id)
    if state is None:
        raise ValueError("Run does not exist.")
    apply_run_paths(state["id"])
    set_active(state["id"])
    return state


def delete_run(run_id):
    """Delete one validated run folder.

    Deleting the active run clears the marker and restores the legacy paths;
    the API refuses this by itself, but direct callers are allowed to do it.
    """
    if not _enabled():
        return False
    folder = _run_folder(run_id)
    if not folder.is_dir():
        return False
    if folder.is_symlink():
        raise ValueError("Run folder must not be a symbolic link.")
    base = _runs_dir().resolve()
    target = folder.resolve()
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise ValueError("Run folder escapes RUNS_DIR.") from exc
    if target == base:
        raise ValueError("Invalid run folder.")
    with _LOCK:
        shutil.rmtree(target)
        if _read_marker().get("run_id") == folder.name:
            _clear_marker()
            apply_legacy_paths()
    return True


def _legacy_artifacts():
    out = Path(getattr(config, "OUTPUT_DIR", config.BASE_DIR / "output"))
    files = [out / name for name in _LEGACY_FILES]
    dirs = [out / name for name in _LEGACY_DIRS]
    return files, dirs


def _has_legacy_artifacts():
    files, dirs = _legacy_artifacts()
    return any(path.is_file() for path in files) or any(
        path.is_dir() for path in dirs
    )


def _import_legacy(run_id):
    """Copy/hardlink the legacy global artifacts into a newly created run."""
    folder = _run_folder(run_id)
    files, dirs = _legacy_artifacts()
    for source in files:
        if source.is_file():
            _copy_or_link(source, folder / source.name)
    for source in dirs:
        if source.is_dir():
            shutil.copytree(
                source,
                folder / source.name,
                copy_function=_copy_or_link,
                dirs_exist_ok=True,
            )


def ensure_active_run():
    """Return a usable active run id, creating/importing one if needed.

    Order of preference:

    1. the run named by output/active_run.json, when it still exists;
    2. the newest valid run (a corrupt/missing marker does not lose data);
    3. a one-time import of legacy output/ artifacts into "Imported run".

    When runs are disabled, or there is nothing to activate, returns None and
    leaves the global legacy files alone.
    """
    if not _enabled():
        return None
    with _LOCK:
        run_id = current_run_id()
        if run_id:
            apply_run_paths(run_id)
            return run_id

        runs = list_runs()
        if runs:
            newest = runs[0]["id"]
            apply_run_paths(newest)
            _write_marker(newest)
            return newest

        if _has_legacy_artifacts():
            run_id = create_run(name="Imported run")
            if run_id is None:
                return None
            try:
                _import_legacy(run_id)
                apply_run_paths(run_id)
                _write_marker(run_id)
            except Exception:
                # Do not leave a half-imported run behind; the legacy files
                # are still untouched.
                try:
                    shutil.rmtree(_run_folder(run_id), ignore_errors=True)
                finally:
                    apply_legacy_paths()
                raise
            return run_id

        # No run and nothing to import: keep the next search on legacy paths.
        apply_legacy_paths()
        return None
