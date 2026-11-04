# AGENTS.md

ScrapeLite: a local search → open-access full-text fetch → LLM extraction pipeline.
Flask UI + CLI share one Python engine; results go to Excel files in `output/`
(the web app keeps one folder per search under `output/runs/`).
No build step, package metadata, linter, or CI; unit tests live in `tests/`.

## Commands (Linux/macOS)

- Use the project venv: `.venv/bin/python` (currently Python 3.13; scripts enforce >= 3.9).
- Web app: `bash start.sh` or `.venv/bin/python app.py` → http://127.0.0.1:5000
  (`start.sh` runs `setup.sh` automatically if the venv is missing). `app.py`'s
  `__main__` block enables `config.RUNS_ENABLED` and calls
  `runs.ensure_active_run()`; importing `app` (tests) leaves legacy paths active.
- CLI: `.venv/bin/python main.py` (search + extract)
  - `--search-only`, `--extract-only` (needs existing `output/search_results.xlsx`),
    `--limit N`, `--reset` (clears `output/checkpoint.json`),
    `--year-from YYYY` / `--year-to YYYY` (optional publication-year range),
    `--local-folder PATH`, `--recursive`, `--local-only` (folder-only; requires
    `--local-folder`). `--extract-only` relies on `output/local_pdfs.json` and
    never rescans the source folder.
- Setup: `bash setup.sh`; all setup/start scripts support `--dry-run`.
- Windows equivalents: `setup.bat` / `start.bat` (wrappers around `setup.ps1`).
  When changing setup/start behavior or dry-run output, keep `setup.sh`,
  `setup.ps1`, `start.sh`, and `start.bat` in sync.
- Tests: `.venv/bin/python -m unittest discover -s tests -v` (mocked, no network).
  Smoke check: `.venv/bin/python -c "import app, main, runs, figures, pdf_annotations"`.
  JS syntax: `node --check static/app.js`.
  `main.py --search-only` and any extraction/suggestions hit live services
  (PubMed/Europe PMC/Crossref, Ollama or the OpenAI-compatible endpoint).
- Extraction and ✨ suggestions require Ollama running with the configured model
  (`ollama list`), or a configured `OPENAI_BASE_URL` / `OPENAI_API_KEY` / `OPENAI_MODEL`.
- Excel I/O is deliberately openpyxl-only (no pandas). `make_ppt.py` needs
  `python-pptx`, which is **not** in `requirements.txt`.

## Architecture

- `config.py` is the single settings source: queries, sources, `YEAR_FROM` /
  `YEAR_TO`, local-PDF defaults (`LOCAL_PDF_DIR`, `LOCAL_PDF_RECURSIVE`,
  `MAX_LOCAL_PDFS`), LLM backend, `FIELDS` (drives the generated
  JSON schema/prompt), PDF archiving, evidence/annotation settings
  (`EVIDENCE_ENABLED`, `EVIDENCE_MAX_CHARS`, `EVIDENCE_FUZZY_THRESHOLD`,
  `PDF_INDEX_JSON`, `ANNOTATED_PDF_DIR`), figure settings
  (`FIGURES_ENABLED`, `FIGURES_GOAL_FILTER`, `FIGURE_DPI`,
  `FIGURE_MAX_PER_PAPER`, `FIGURE_MAX_CANDIDATES`, `FIGURES_DIR`,
  `FIGURES_INDEX_JSON`, `FIGURES_XLSX`), prompt-history path/caps, output
  paths, Flask host/port. The web Settings panel persists
  email/model/max-results/year range/local folder to
  `output/web_settings.json`; those values **override `config.py` at app
  startup**. `RUNS_ENABLED` (default `False`), `RUNS_DIR`
  (`OUTPUT_DIR/"runs"`), `ACTIVE_RUN_JSON` and the runtime
  `ACTIVE_RUN_DIR = None` drive per-run folders.
- `runs.py`: per-search-dataset folders + savable state. A run id must match
  `^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$` (no separators/absolute paths/`..`).
  `create_run()` makes `RUNS_DIR/<timestamp>-<4hex>/` with
  `state.json`, `pdfs/`, `annotated_pdfs/`, `figures/`; the run also owns
  `search_results.xlsx`, `extracted_data.xlsx`, `closed_source_papers.xlsx`,
  `checkpoint.json`, `local_pdfs.json`, `pdf_index.json`, `figures/index.json`
  and `figures.xlsx`. `apply_run_paths()` repoints
  `SEARCH_RESULTS_XLSX`, `CLOSED_SOURCE_XLSX`, `EXTRACTED_XLSX`,
  `CHECKPOINT_JSON`, `LOCAL_PDF_INDEX_JSON`, `PDF_INDEX_JSON`,
  `ANNOTATED_PDF_DIR`, `PDF_DIR`, `FIGURES_DIR`, `FIGURES_INDEX_JSON` and
  `FIGURES_XLSX`, and sets `config.ACTIVE_RUN_DIR`; it never changes
  `OUTPUT_DIR`, `PROMPT_HISTORY_JSON` or `app.SETTINGS_PATH`.
  `apply_legacy_paths()` restores `OUTPUT_DIR/*` and clears `ACTIVE_RUN_DIR`.
  `capture_paths()`/`restore_paths()` support search rollback.
  State schema v1: `{schema_version, id, name, created_at, updated_at, status,
  query:{tree,rendered,selected_paths,children_selected_paths}, sources,
  max_results, year_from, year_to, excluded_journals, local_folder,
  local_recursive, local_only, extraction:{goal,columns,mode},
  settings:{email,model}, counts:{papers,extracted,closed,figures},
  last_search_at, last_extract_at}`. Unknown keys (API keys, output paths) are
  dropped. `list_runs()` returns metadata only, newest updated first; corrupt
  states are skipped. The `output/active_run.json` marker holds
  `{run_id, updated_at}`. `current_run_id()` verifies the state is readable.
  `ensure_active_run()` prefers the marker, then the newest valid run, then a
  one-time import of global artifacts into **Imported run** (copy/hardlink;
  source files are never moved/deleted; `web_settings.json`,
  `prompt_history.json` and `runs/` are not imported). Deleting the active run
  clears the marker and restores legacy paths; the API refuses that deletion.
- `local_pdfs.py`: local folder source. `scan_folder()` validates the folder,
  reads pypdf metadata plus a DOI from the first three pages, dedupes by DOI then
  normalized title, and copies/hardlinks each valid `%PDF-` into `config.PDF_DIR`
  as `local_<path-hash>_<stem>.pdf`. `merge_local_papers()` matches online rows
  by normalized DOI then title. `output/local_pdfs.json` maps `paper_key` to the
  archived filename; `attach_to_papers()` re-attaches it when loading
  `search_results.xlsx` (missing files are ignored).
- `history.py`: global prompt history store in `output/prompt_history.json`
  (`{"version": 1, "entries": [...]}`). Kinds are `search`, `goal` and
  `columns`; entries carry id/data/created_at/last_used/uses. Dedup uses a
  canonical fingerprint of kind + JSON data (stable dict key order), duplicate
  records increment `uses` and move to the front, and writes are atomic under a
  `threading.Lock`. Caps are `PROMPT_HISTORY_MAX_ENTRIES` (100 total) and
  `PROMPT_HISTORY_MAX_PER_KIND` (50). Missing/corrupt files read as empty.
  `app.py` records after search/extract validation and before scanning or
  processing; a history failure is logged as a warning and never fails the job.
  Never record email/API keys/model/backend secrets here.
- `searchers.py`: API clients + `SOURCE_REGISTRY` (`pubmed`, `europepmc`,
  `crossref`, `chemrxiv`). To add a source, implement
  `search_<name>(query, limit=None, year_from=None, year_to=None)` returning
  paper dicts and add one registry entry; the UI and `/api/sources` list it
  automatically. `normalize_year_range()` is the shared validator: it returns
  4-digit strings or `None` for open ends and rejects bools, non-4-digit values
  and `from > to` with `ValueError`. Dedup key is
  PMID > lowercased DOI > lowercased title (`paper_key`). `search()` and
  `search_all()` accept an optional `cancel_check` callable and raise
  `SearchCancelled` between sources/queries; in-flight HTTP requests always
  finish. Omitting `cancel_check` keeps the old behaviour. Paper dicts carry
  `publication_type`: `journal article` (PubMed/Europe PMC/Crossref),
  `preprint` (ChemRxiv) or `local PDF` (local-only records); `_merge_into()`
  backfills it on dedup like the other metadata fields.
- ChemRxiv is searched **through Crossref**, never chemrxiv.org: the official
  public API is behind a Cloudflare browser challenge (HTTP 403 "Just a
  moment..."). `search_chemrxiv()` sends `filter=type:posted-content,
  prefix:10.26434`, optionally `from-posted-date`/`until-posted-date`, then
  keeps only DOIs whose lowercased value contains `chemrxiv` (the prefix alone
  is not exclusive). It reuses `_crossref_to_paper()` with
  `date_keys=("posted", "published")`, `publication_type="preprint"`,
  `journal_fallback="ChemRxiv"`, `is_oa=True` and PDF-first link selection;
  journal Crossref behavior is unchanged. Crossref returns the ChemRxiv PDF
  as `https://chemrxiv.org/doi/pdf/<doi>` in `link`.
- `fetcher.py`: full text via PMC XML → source-provided OA URL → Unpaywall;
  rejects bodies under 500 chars.
- `pdf_fetcher.py`: archiving runs only during Extract, not Search. Article PDF
  first, then preprint (Europe PMC PPR → Crossref relations → bioRxiv/medRxiv).
  Only payloads starting with `%PDF-` are saved; filenames are deterministic
  (PMID > DOI > title).
- `pdf_annotations.py`: locates the model's verbatim `evidence` quote in the
  archived PDF (PyMuPDF `search_for`, normalized block matching, then
  `difflib.SequenceMatcher` fuzzy matching at `EVIDENCE_FUZZY_THRESHOLD`),
  fills `evidence_page`, writes one cached annotated PDF per paper to
  `output/annotated_pdfs/`, and updates `output/pdf_index.json` atomically under
  a lock. Every resolved quote gets the same explicit yellow highlight
  (`HIGHLIGHT_COLOR = (1.0, 1.0, 0.0)`), exact and fuzzy alike. Each highlight's
  comment popup contains `Data: field=value; field=value` built from the
  extracted row (`ANNOTATION_META_COLUMNS` — `paper_key`, `row_index`, `doi`,
  `source`, `publication_type`, `pdf_file`, `pdf_source`, `evidence`,
  `evidence_page` — are excluded, values are stringified and capped) followed
  by `Evidence: "…"`; rows with no user data fall back to `Row N` + the quote.
  Index evidence items store `{row_index, text, page, status, data}`. The
  annotation filename contains a hash of paper key + source bytes + evidence
  list + per-row data mapping, so changing the quote or any extracted value
  regenerates the file; a matching file is reused. The index stores basenames
  only. `annotate_paper_rows()` fills rows and returns `(rows, summary)` and
  never raises. `load_index()`, `api_summary()`, `resolve_paper_paths()` and
  `regenerate_annotation()` are the safe read/serve helpers (they reject
  absolute paths, `..` and escapes outside `PDF_DIR` / `ANNOTATED_PDF_DIR`).
- `figures.py`: PDF-only, goal-driven figure/table search during Extract.
  `_page_candidates()` finds `page.get_text("dict")` caption blocks
  (`^(figure|fig.?|table)\s*S?\d+[A-Za-z]?\b`), prefers nearby image/drawing
  rects, then a vertical band above/below the caption, then a full-page
  fallback (`kind` `caption`/`image`/`page`). `_detect()` collects candidate
  descriptors while the PyMuPDF document is open, caps them at
  `FIGURE_MAX_CANDIDATES`, and (unless `goal is None` or
  `FIGURES_GOAL_FILTER=False`) calls `select_figures_for_goal()`; only selected
  candidates are rendered. The selector uses `suggester._require_backend()` /
  `_call_llm()` / `_load_json_object()` with a `{"selected":[{"id","reason"}]}`
  JSON schema and ignores unknown/duplicate ids. Empty goals skip the LLM and
  save nothing; backend/parse failures are warnings and save nothing. PNGs are
  written at `FIGURE_DPI` under `FIGURES_DIR/<safe-paper-stem>/`, deduped by
  SHA-256 of paper_key + page + kind + PNG bytes, capped at
  `FIGURE_MAX_PER_PAPER`, and registered in `output/figures/index.json`
  (`{"version":1,"figures":[...]}`) plus `output/figures.xlsx`; entries carry
  the model's `reason` (empty for legacy/all mode). All operations are
  best-effort and never raise into extraction. `process_paper(pdf_path,
  paper_key, title="", pdf_file="", pdf_source="", goal=None,
  goal_filter=None)` replaces only that paper's index entries; `clear()` (web
  search only) removes the index, xlsx and cached PNG dirs. The index stores
  POSIX relative paths only; `resolve_figure_path()`/`api_list()` validate them
  under `FIGURES_DIR`.
- `extractor.py`: Ollama path constrains output with the JSON schema and expects a
  bare array; OpenAI JSON mode requires the `{"items": [...]}` object wrapper.
  Both shapes plus fenced/prose fallbacks are handled in `_parse_response`.
  The schema/prompt ask for the reserved `evidence` string when
  `EVIDENCE_ENABLED` is true; `main.make_instruction()` adds the same rule and
  spells out one quote per table row in table mode. Ollama extraction timeout
  is 900 s.
- `app.py`: thin Flask layer that imports `_prepare_paper`, `_load_search_results`,
  etc. from `main.py`, so engine changes affect both interfaces. Jobs are
  in-memory and one-at-a-time; do not enable Flask's reloader or multiple workers.
  Job statuses are `running`, `done`, `error` and `cancelled`; `_active_job()`
  treats only `running` as active. `POST /api/job/<id>/cancel` sets the job's
  `cancel_requested` flag under `JOB_LOCK` (404 unknown, 409 non-running) and the
  job body stops at its next cooperative check.
  When `config.RUNS_ENABLED` is true, a normal `/api/search` creates a run and
  applies its paths before scanning/network access; success writes the sheet,
  clears extraction/closed/checkpoint/figures/local index inside that run, writes
  counts/last_search_at and sets the active marker. Any cancel/error before the
  dataset is complete restores the captured previous paths and deletes the
  abandoned new folder. `merge:true` (Refresh search) requires an active run,
  does not create a folder, merges by `paper_key`, applies the blocklist to the
  merged set, writes the local index with `replace=False`, and preserves
  extraction/closed/checkpoint/figure outputs. The job result adds `run_id`,
  `merged` and `added`. `_run_extract_job` calls `ensure_active_run()` (never
  creates one), saves extraction goal/columns/mode, then counts/last_extract_at
  for done/cancelled/error. Run routes: `GET /api/runs`,
  `GET /api/run`, `POST /api/runs/<id>/load`,
  `POST /api/runs/<id>/delete`; load/delete return 409 while a job is running and
  delete refuses the active run. `open_output` uses
  `config.ACTIVE_RUN_DIR or config.OUTPUT_DIR`; `/download/<name>` and
  `/api/results` keep reading dynamic config paths.
  `GET /api/annotated-pdf/<path:paper_key>` serves only keys in
  `output/pdf_index.json`, regenerating a missing annotation or falling back to
  the original PDF; unknown/invalid keys are 404. `GET /api/results` keeps
  `{search, extracted, closed}` and adds the API-safe `annotations` mapping plus
  the `figures` list. `GET /api/figures[?paper_key=]` lists indexed figures and
  `GET /api/figure/<figure_id>` serves only ids in the figure index, resolving
  their relative paths under `FIGURES_DIR`; unknown/unsafe/missing ids are 404.
  `/download` exposes only the four Excel files (including `figures.xlsx`).
- `suggester.py`: PubMed autocomplete plus the LLM helpers behind the web UI.
  `autocomplete()` never raises. `suggest_related()`,
  `suggest_related_many()` and `suggest_goal(goal, columns=None,
  mode="records")` call `_require_backend()` first (`extractor.backend_status()`
  checks Ollama `/api/tags` or the OpenAI config), then `_call_llm(prompt,
  schema)` with `LLM_TIMEOUT = 120` seconds, JSON mode and schema-constrained
  output; `_load_json_object()` tolerates fenced/prose JSON. `BackendUnavailable`
  propagates so the route can answer 503; bad input raises `ValueError`.
  `suggest_goal()` whitespace-normalizes the goal, caps it at 4000 characters,
  accepts a column list or comma-separated string (cleaned/deduped/capped at 50)
  and normalizes `mode` to `records`/`table`. It returns at most three cleaned,
  deduped goal strings (1200 characters each), never starts a job or records
  prompt history, and never writes the goal itself.
- `static/app.js` + `templates/index.html`: vanilla JS query builder, no bundler.
  `/api/autocomplete` proxies PubMed suggestions to avoid browser CORS;
  `/api/suggest` uses the LLM through `suggester.py`; `POST /api/suggest-goal`
  takes `{goal, columns, mode}`, returns `{source, model, suggestions}` and maps
  empty/invalid input to 400, `BackendUnavailable` to 503 and unexpected
  LLM/parse failures to 502 ("Could not improve the goal: …"). The route must
  never call `_start_job` or `history.record`.
  `#improve-goal` sits directly below `#goal` (the Previous goals row stays
  above the textarea); `improveGoal()` posts the current goal/columns/mode and
  renders 1-3 candidates in `#goal-suggest-panel` with the existing
  `.suggest-panel` styles and an `LLM · model` badge. Each candidate has
  **Use this goal**, which writes the exact text into `#goal`, focuses it and
  shows a banner; Cancel/Dismiss closes the panel. There is no auto-run and no
  history entry — only starting Extract records the goal. The Data & PDFs tab keeps
  the grouped extracted table on the left and a sticky ~46% quote/PDF pane on
  the right (`iframe#pdf-viewer`, annotations metadata by `row_index`, page
  anchor, "Open in new tab"); mobile stacks the pane below the table. The quote
  panel is the fallback because browser PDF viewers vary in highlight support.
  The data group header shows an amber `preprint` badge when the search record
  or any extracted row has `publication_type == "preprint"` (`isPreprintGroup`);
  the Search table renders the generic `publication_type` column as `type` and
  highlights `preprint` cells. A full-width `#figures-section` below
  `.data-layout` renders `data.figures` as paper-grouped lazy thumbnails
  (`/api/figure/<id>`) with a no-dependency
  lightbox (backdrop/Escape/close, label, page, caption, selection reason when
  present, new-tab link); the
  section and `figures.xlsx` buttons are hidden when the list is empty.
  `#progress-card` also hosts the `#cancel-job` button: it is visible only while
  a job is `running`, posts to the cancel endpoint, disables itself and shows
  `Cancelling…` when `cancel_requested` is true, and is reset/hidden for
  `done`, `error` and `cancelled`. `#runs-card` (hidden unless `/api/runs`
  reports `enabled`) lists runs, loads/refreshes/deletes them and drives
  `/api/runs/<id>/load|delete`; page load calls `loadRuns({applyActive:true})`
  after sources/settings and applies the active state without auto-running.
  `doSearch({merge:true})` is Refresh search. `applyRunState()` restores query +
  checkbox paths, sources, max results, years, journals, local folder,
  extraction goal/columns/mode and email/model fields (not saved automatically).
  `queryModel.pathForNode()`/`nodeAtPath()` use child-index paths;
  `restore(tree, {preserveEmpty:true})` keeps empty nodes for run restores while
  history keeps dropping them. `snapshotQuerySelection()` /
  `restoreQuerySelection()` persist `selected_paths` and
  `children_selected_paths`; state-2 groups re-add their descendants and call
  `pruneSelection()`/`syncSelectionUI()`. `syncSelectionUI` checks a group box
  when `selectedIds.has(id) || childrenSelectedGroups.has(id)`; preserve that
  group-checkbox fix when editing selection code.

## Behavior that is easy to break

- **CLI vs web search differ on purpose:** CLI search merges new results into the
  existing global `output/search_results.xlsx`; a normal web search creates a new
  run folder, replaces that run's search sheet and deletes that run's
  `extracted_data.xlsx` + `closed_source_papers.xlsx`, resets its checkpoint and
  clears its figures (a fresh dataset). Web **Refresh search** (`merge:true`) is
  the additive path and must keep the run's extraction/closed/checkpoint/figure
  files. Don't align CLI and web without deciding which behavior is wanted.
  The local index follows the same rule: a new web run writes it with
  `replace=True` (stale mappings cannot survive a replaced dataset), Refresh
  writes with `replace=False`, while a CLI run that scans `--local-folder` writes
  the final merged set so retained papers keep their mappings. The figure index
  follows it too: a new web search clears it (`figures.clear()`), CLI search and
  web Refresh preserve it. When runs are disabled, the old web replace behavior
  is unchanged on the global paths.
- **Cancellation is cooperative and must preserve partial work:** the web
  Cancel button sets `cancel_requested`; it never kills a thread. `search()`
  checks between sources (and `search_all()` between queries) and raises
  `SearchCancelled`, which `_run_search_job()` maps to status `cancelled`, not
  `error`. Before writing/replacing anything, the search job checks again, so a
  cancelled search leaves the previous `search_results.xlsx`, extracted/closed
  files, checkpoint, local index and figures untouched. With runs enabled, a
  cancelled/failed **new** search also restores the previously captured paths and
  deletes its abandoned new run folder; a cancelled **Refresh** keeps the active
  run and its previous sheet. `_run_extract_job()`
  checks before each paper and again after the current one; the in-flight paper
  finishes and its rows/checkpoint/closed entry are saved, then the loop breaks.
  Unprocessed papers are never marked done, so a later Extract resumes them.
  Progress/result are set from the completed count and the log says
  `Extraction cancelled after N paper(s); completed rows were kept.`. A cancel
  during a long HTTP/LLM call takes effect only when that call returns. Do not
  add background workers, per-paper threads, or code that treats `cancelled` as
  `error`/active.
- **Local PDFs are a separate source and skip the online PDF path:**
  `_prepare_paper()` uses the archived local file directly, reports
  `source="local"` / `label="local:folder"`, and never calls
  `fetcher.fetch_full_text()` or `pdf_fetcher.acquire_pdf()`. If the file has no
  extractable text it returns no text (no fallback to another PDF), so the normal
  closed flow records `PDF saved but no extractable text`. A missing indexed file
  falls back to the normal online path. Local PDFs are not filtered by
  `--year-from`/`--year-to`; the range only affects the online search.
- **Evidence/annotation is best-effort and must never fail extraction:**
  both `main.run_extract()` and `app._run_extract_job()` call
  `pdf_annotations.annotate_paper_rows()` after `build_extract_rows()` and
  before writing Excel, only when a PDF path exists and `EVIDENCE_ENABLED` is
  true. Any exception is logged as a warning; the extracted rows (with evidence
  text and blank `evidence_page`) are still written. No PDF means the quote is
  kept and the page stays blank. The index registers a PDF even when no quote
  resolves, so the viewer can show the original. Highlights are always yellow
  and each popup must keep both the row's non-meta extracted data
  (`Data: field=value; …`) and the verbatim quote; the annotation hash includes
  that data, so editing an extracted value must invalidate the cached PDF. The
  index/annotations store basenames only; never add an API/serving path that
  accepts a caller-supplied filename. There is **no OCR**: image-only PDFs leave
  quotes unresolved.
- **Figure search is goal-driven, best-effort and PDF-only:** both
  `main.run_extract()` and `app._run_extract_job()` call
  `figures.process_paper()` whenever `FIGURES_ENABLED` and `pdf.get("path")`
  exist — after the annotation block, before/around the Excel write, even when
  `EVIDENCE_ENABLED = False` and even when the model returned zero records.
  The web job passes the user's `goal`; the CLI passes `config.INSTRUCTION`.
  With the default `FIGURES_GOAL_FILTER=True`, the goal is sent to the
  configured LLM and only matching candidates are saved. A goal that does not
  ask for figures/images/tables/visual data (and an empty goal, which skips the
  LLM) saves none. `goal=None` preserves legacy all-figures behaviour for
  direct callers/tests; `FIGURES_GOAL_FILTER=False` restores it for the
  pipeline. A backend/parse failure only logs `Figure warning: …`; extraction
  continues. Figures apply to local PDFs too (they have archived paths). Web
  search calls `figures.clear()` when it replaces the dataset (index +
  `figures.xlsx` + cached PNG dirs); CLI search merges and must never clear
  them. `process_paper()` replaces only that paper's index entries, so
  re-extraction cannot leave stale figures. Detection uses captions plus nearby
  image/drawing rects, a caption band or a full-page fallback (`kind`
  `caption`/`image`/`page`), then large embedded images on uncaptioned pages.
  It is **PDF-only**, with **no OCR**, no HTML/PMC figure download, and relies
  on caption text for labelled figures/tables. Index paths are POSIX relative
  names validated under `FIGURES_DIR`; never accept a caller-supplied path.
  Orphan PNGs from older versions may remain, but only index entries are
  served. The web job result adds `figures_candidates`, `figures_matched`,
  `figures_skipped` and `figure_candidate_capped` next to the saved/duplicate/
  capped/warning counters.
- **Improve goal is advisory and side-effect free:** the button only rewrites
  goal text. It must never start/queue an extraction, write `web_settings.json`,
  save a run state, or record prompt history; those happen when the user clicks
  **Extract data**. Applying a candidate is explicit (**Use this goal**) and only
  updates `#goal`; Cancel/Dismiss leaves it unchanged. Keep the button/panel
  directly after `#goal` and before the column-name field, and keep the Previous
  goals history row above the textarea.
- **Web folder-only search:** `/api/search` allows an empty query and no sources
  only when a valid `local_folder` is supplied. Supplying a query without sources
  (or sources without a query) is still a 400. Omitted `local_folder` /
  `local_recursive` keys fall back to saved settings; an explicitly blank folder
  disables local PDFs. Settings are validated only at search time.
- **Prompt history is global across workspaces/browser sessions and
  best-effort:** `app.py` records the normalized
  search payload (including the browser's `query_tree` plus sources, max results,
  years, journal terms and local-folder fields) after validation and before any
  scan/search; Extract records the goal and, when non-empty, the joined columns
  before processing. `/api/suggest-goal` never records history or saves the goal;
  only starting Extract does. Any `history.record` failure is logged as a warning and
  must not fail the job. `GET /api/history[?kind=search|goal|columns]` lists
  entries newest-used first; `POST /api/history/clear` with an optional `kind`
  clears one kind or all. Never expose the history file through a serving route.
  The browser snapshot format is
  `{type:"group", op:"AND", children:[{type:"term", text, op} |
  {type:"group", op, children}]}` with **no node ids**; `queryModel.restore()`
  regenerates ids and ignores invalid/empty nodes. Selecting a previous search
  restores fields but never auto-runs; legacy entries without `query_tree` fall
  back to one term containing the rendered query. History stays global across
  runs and uses the default `restore()` (drops empty nodes); run selection paths
  are stored in the run state, not in prompt history.
- Checkpoint skips `extracted` and `closed` papers; `error` entries retry.
  Web search resets it, CLI only with `--reset`.
- **The year range is separate from the boolean query:** it is never
  concatenated into the Query preview or the string passed as `query`. In
  `/api/search`, an omitted `year_from`/`year_to` falls back to saved settings
  then `config.py`; an explicitly blank side means an open end. Source mapping:
  PubMed `datetype=pdat` + `mindate`/`maxdate`; Europe PMC
  `PUB_YEAR:[from TO to]` (open ends `1000`/`3000`); Crossref
  `from-pub-date:YYYY-01-01` / `until-pub-date:YYYY-12-31` appended to the
  `type:journal-article` filter; ChemRxiv
  `from-posted-date:YYYY-01-01` / `until-posted-date:YYYY-12-31` appended to
  the `type:posted-content,prefix:10.26434` filters. Source clients validate
  direct calls; `search()`/`search_all()` validate once up front.
- **`publication_type` is pipeline metadata, never a user field:** it is in
  `SEARCH_COLS`, `CLOSED_COLS`, `EXTRACT_META_COLUMNS`, `RESERVED_COLUMNS` and
  (via RESERVED) `PROTECTED_COLUMNS`, so web column validation rejects it.
  `paper_to_row()` and `build_extract_rows()` copy it onto every row;
  `_load_search_results()` reads it and calls `writer.ensure_columns()` so an
  old search sheet gains a blank column without losing data. `api_results()`
  adds the same blank column in memory for legacy sheets, and extract migration
  already uses `writer.ensure_columns()`. Values are `journal article`,
  `preprint` and `local PDF`; keep them lowercase/stable because the UI checks
  `publication_type == "preprint"` for the badge.
- Web Extract lets the user supply columns, which override `config.FIELDS` for
  that run (all treated as text). If the column header changes, the app starts a
  fresh `extracted_data.xlsx`; the CLI always uses `config.FIELDS`/`INSTRUCTION`.
- If HTML/XML full text fails but a PDF was archived, `_prepare_paper` extracts
  from the PDF. With `PDF_ARCHIVE_ENABLED = False` there is no PDF fallback.
- Papers with extractable text but no archivable PDF are still extracted **and**
  appended to `closed_source_papers.xlsx` with
  `PDF unavailable — download manually`; keep these reason strings stable.
- Output files: `search_results.xlsx`, `extracted_data.xlsx` (appended),
  `closed_source_papers.xlsx` (appended), `checkpoint.json`,
  `local_pdfs.json` (local mapping, git-ignored), `prompt_history.json`
  (global web prompt history, git-ignored), `pdfs/`, `pdf_index.json`
  (evidence/annotation index, git-ignored), `annotated_pdfs/` (cached
  highlighted copies, git-ignored), `figures/` (figure index + cached PNGs,
  git-ignored), `figures.xlsx` (figure metadata including the selection
  `reason`, covered by `output/*.xlsx`).
  With runs enabled these same files live under `output/runs/<run-id>/`; the
  global copies remain for CLI/legacy use. `output/active_run.json` is the
  loaded-run marker and `output/runs/` is a dataset container; both are
  git-ignored. `output/web_settings.json` stays global.
- Run tests must patch `config.RUNS_ENABLED=True` plus temporary `RUNS_DIR`,
  `ACTIVE_RUN_JSON` and `OUTPUT_DIR`, and restore `runs.capture_paths()` in
  cleanup because `apply_run_paths()` mutates module-level config paths. The
  existing suite relies on `RUNS_ENABLED=False` so all 262 original tests keep
  using the global paths unchanged.

## Repository gotchas

- `.venv/` is **tracked** in git (~2.5k Windows-venv files) even though
  `.gitignore` lists it; the active environment is Linux (Python 3.13). Never use
  `git add -A` / `git add .` — stage explicit paths only. The same applies to
  tracked `output/checkpoint.json` and `output/search_results.xlsx` despite
  ignore rules.
- `README.md` is the user-facing guide (Windows-first, non-technical audience);
  update it when user-visible behavior or setup flow changes.
