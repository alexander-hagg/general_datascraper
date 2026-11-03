# AGENTS.md

ScrapeLite: a local search → open-access full-text fetch → LLM extraction pipeline.
Flask UI + CLI share one Python engine; results go to Excel files in `output/`.
No build step, package metadata, linter, or CI; unit tests live in `tests/`.

## Commands (Linux/macOS)

- Use the project venv: `.venv/bin/python` (currently Python 3.13; scripts enforce >= 3.9).
- Web app: `bash start.sh` or `.venv/bin/python app.py` → http://127.0.0.1:5000
  (`start.sh` runs `setup.sh` automatically if the venv is missing).
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
  Smoke check: `.venv/bin/python -c "import app, main, searchers, pdf_annotations"`.
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
  `PDF_INDEX_JSON`, `ANNOTATED_PDF_DIR`), prompt-history path/caps, output
  paths, Flask host/port. The web Settings panel persists
  email/model/max-results/year range/local folder to
  `output/web_settings.json`; those values **override `config.py` at app
  startup**.
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
- `searchers.py`: API clients + `SOURCE_REGISTRY`. To add a source, implement
  `search_<name>(query, limit=None, year_from=None, year_to=None)` returning
  paper dicts and add one registry entry; the UI and `/api/sources` list it
  automatically. `normalize_year_range()` is the shared validator: it returns
  4-digit strings or `None` for open ends and rejects bools, non-4-digit values
  and `from > to` with `ValueError`. Dedup key is
  PMID > lowercased DOI > lowercased title (`paper_key`).
- `fetcher.py`: full text via PMC XML → source-provided OA URL → Unpaywall;
  rejects bodies under 500 chars.
- `pdf_fetcher.py`: archiving runs only during Extract, not Search. Article PDF
  first, then preprint (Europe PMC PPR → Crossref relations → bioRxiv/medRxiv).
  Only payloads starting with `%PDF-` are saved; filenames are deterministic
  (PMID > DOI > title).
- `pdf_annotations.py`: locates the model's verbatim `evidence` quote in the
  archived PDF (PyMuPDF `search_for`, normalized block matching, then
  `difflib.SequenceMatcher` fuzzy matching at `EVIDENCE_FUZZY_THRESHOLD`),
  fills `evidence_page`, writes one cached highlighted PDF per paper to
  `output/annotated_pdfs/`, and updates `output/pdf_index.json` atomically under
  a lock. The annotation filename contains a hash of paper key + source bytes +
  evidence list; a matching file is reused. The index stores basenames only.
  `annotate_paper_rows()` fills rows and returns `(rows, summary)` and never
  raises. `load_index()`, `api_summary()`, `resolve_paper_paths()` and
  `regenerate_annotation()` are the safe read/serve helpers (they reject
  absolute paths, `..` and escapes outside `PDF_DIR` / `ANNOTATED_PDF_DIR`).
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
  `GET /api/annotated-pdf/<path:paper_key>` serves only keys in
  `output/pdf_index.json`, regenerating a missing annotation or falling back to
  the original PDF; unknown/invalid keys are 404. `GET /api/results` keeps
  `{search, extracted, closed}` and adds the API-safe `annotations` mapping.
  `/download` exposes only the three Excel files.
- `static/app.js` + `templates/index.html`: vanilla JS query builder, no bundler.
  `/api/autocomplete` proxies PubMed suggestions to avoid browser CORS;
  `/api/suggest` uses the LLM through `suggester.py`. The Data & PDFs tab keeps
  the grouped extracted table on the left and a sticky ~46% quote/PDF pane on
  the right (`iframe#pdf-viewer`, annotations metadata by `row_index`, page
  anchor, "Open in new tab"); mobile stacks the pane below the table. The quote
  panel is the fallback because browser PDF viewers vary in highlight support.

## Behavior that is easy to break

- **CLI vs web search differ on purpose:** CLI search merges new results into the
  existing `search_results.xlsx`; web search replaces the sheet and also deletes
  `extracted_data.xlsx` + `closed_source_papers.xlsx` and resets the checkpoint
  (a fresh dataset). Don't align them without deciding which behavior is wanted.
  The local index follows the same rule: the web job always writes it with
  `replace=True` (stale mappings cannot survive a replaced dataset), while a CLI
  run that scans `--local-folder` writes the final merged set so retained papers
  keep their mappings.
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
  resolves, so the viewer can show the original. The index/annotations store
  basenames only; never add an API/serving path that accepts a caller-supplied
  filename. There is **no OCR**: image-only PDFs leave quotes unresolved.
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
  before processing. Any `history.record` failure is logged as a warning and
  must not fail the job. `GET /api/history[?kind=search|goal|columns]` lists
  entries newest-used first; `POST /api/history/clear` with an optional `kind`
  clears one kind or all. Never expose the history file through a serving route.
  The browser snapshot format is
  `{type:"group", op:"AND", children:[{type:"term", text, op} |
  {type:"group", op, children}]}` with **no node ids**; `queryModel.restore()`
  regenerates ids and ignores invalid/empty nodes. Selecting a previous search
  restores fields but never auto-runs; legacy entries without `query_tree` fall
  back to one term containing the rendered query.
- Checkpoint skips `extracted` and `closed` papers; `error` entries retry.
  Web search resets it, CLI only with `--reset`.
- **The year range is separate from the boolean query:** it is never
  concatenated into the Query preview or the string passed as `query`. In
  `/api/search`, an omitted `year_from`/`year_to` falls back to saved settings
  then `config.py`; an explicitly blank side means an open end. Source mapping:
  PubMed `datetype=pdat` + `mindate`/`maxdate`; Europe PMC
  `PUB_YEAR:[from TO to]` (open ends `1000`/`3000`); Crossref
  `from-pub-date:YYYY-01-01` / `until-pub-date:YYYY-12-31` appended to the
  `type:journal-article` filter. Source clients validate direct calls;
  `search()`/`search_all()` validate once up front.
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
  highlighted copies, git-ignored).

## Repository gotchas

- `.venv/` is **tracked** in git (~2.5k Windows-venv files) even though
  `.gitignore` lists it; the active environment is Linux (Python 3.13). Never use
  `git add -A` / `git add .` — stage explicit paths only. The same applies to
  tracked `output/checkpoint.json` and `output/search_results.xlsx` despite
  ignore rules.
- `README.md` is the user-facing guide (Windows-first, non-technical audience);
  update it when user-visible behavior or setup flow changes.
