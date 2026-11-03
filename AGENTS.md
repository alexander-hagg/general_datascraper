# AGENTS.md

ScrapeLite: a local search → open-access full-text fetch → LLM extraction pipeline.
Flask UI + CLI share one Python engine; results go to Excel files in `output/`.
No build step, package metadata, test suite, linter, or CI.

## Commands (Linux/macOS)

- Use the project venv: `.venv/bin/python` (currently Python 3.13; scripts enforce >= 3.9).
- Web app: `bash start.sh` or `.venv/bin/python app.py` → http://127.0.0.1:5000
  (`start.sh` runs `setup.sh` automatically if the venv is missing).
- CLI: `.venv/bin/python main.py` (search + extract)
  - `--search-only`, `--extract-only` (needs existing `output/search_results.xlsx`),
    `--limit N`, `--reset` (clears `output/checkpoint.json`).
- Setup: `bash setup.sh`; all setup/start scripts support `--dry-run`.
- Windows equivalents: `setup.bat` / `start.bat` (wrappers around `setup.ps1`).
  When changing setup/start behavior or dry-run output, keep `setup.sh`,
  `setup.ps1`, `start.sh`, and `start.bat` in sync.
- No tests. Smoke check: `.venv/bin/python -c "import app, main"`.
  `main.py --search-only` and any extraction/suggestions hit live services
  (PubMed/Europe PMC/Crossref, Ollama or the OpenAI-compatible endpoint).
- Extraction and ✨ suggestions require Ollama running with the configured model
  (`ollama list`), or a configured `OPENAI_BASE_URL` / `OPENAI_API_KEY` / `OPENAI_MODEL`.
- Excel I/O is deliberately openpyxl-only (no pandas). `make_ppt.py` needs
  `python-pptx`, which is **not** in `requirements.txt`.

## Architecture

- `config.py` is the single settings source: queries, sources, LLM backend,
  `FIELDS` (drives the generated JSON schema/prompt), PDF archiving, output paths,
  Flask host/port. The web Settings panel persists email/model/max-results to
  `output/web_settings.json`; those values **override `config.py` at app startup**.
- `searchers.py`: API clients + `SOURCE_REGISTRY`. To add a source, implement
  `search_<name>(query, limit=None)` returning paper dicts and add one registry
  entry; the UI and `/api/sources` list it automatically. Dedup key is
  PMID > lowercased DOI > lowercased title (`paper_key`).
- `fetcher.py`: full text via PMC XML → source-provided OA URL → Unpaywall;
  rejects bodies under 500 chars.
- `pdf_fetcher.py`: archiving runs only during Extract, not Search. Article PDF
  first, then preprint (Europe PMC PPR → Crossref relations → bioRxiv/medRxiv).
  Only payloads starting with `%PDF-` are saved; filenames are deterministic
  (PMID > DOI > title).
- `extractor.py`: Ollama path constrains output with the JSON schema and expects a
  bare array; OpenAI JSON mode requires the `{"items": [...]}` object wrapper.
  Both shapes plus fenced/prose fallbacks are handled in `_parse_response`.
  Ollama extraction timeout is 900 s.
- `app.py`: thin Flask layer that imports `_prepare_paper`, `_load_search_results`,
  etc. from `main.py`, so engine changes affect both interfaces. Jobs are
  in-memory and one-at-a-time; do not enable Flask's reloader or multiple workers.
- `static/app.js` + `templates/index.html`: vanilla JS query builder, no bundler.
  `/api/autocomplete` proxies PubMed suggestions to avoid browser CORS;
  `/api/suggest` uses the LLM through `suggester.py`.

## Behavior that is easy to break

- **CLI vs web search differ on purpose:** CLI search merges new results into the
  existing `search_results.xlsx`; web search replaces the sheet and also deletes
  `extracted_data.xlsx` + `closed_source_papers.xlsx` and resets the checkpoint
  (a fresh dataset). Don't align them without deciding which behavior is wanted.
- Checkpoint skips `extracted` and `closed` papers; `error` entries retry.
  Web search resets it, CLI only with `--reset`.
- Web Extract lets the user supply columns, which override `config.FIELDS` for
  that run (all treated as text). If the column header changes, the app starts a
  fresh `extracted_data.xlsx`; the CLI always uses `config.FIELDS`/`INSTRUCTION`.
- If HTML/XML full text fails but a PDF was archived, `_prepare_paper` extracts
  from the PDF. With `PDF_ARCHIVE_ENABLED = False` there is no PDF fallback.
- Papers with extractable text but no archivable PDF are still extracted **and**
  appended to `closed_source_papers.xlsx` with
  `PDF unavailable — download manually`; keep these reason strings stable.
- Output files: `search_results.xlsx`, `extracted_data.xlsx` (appended),
  `closed_source_papers.xlsx` (appended), `checkpoint.json`, `pdfs/`.

## Repository gotchas

- `.venv/` is **tracked** in git (~2.5k Windows-venv files) even though
  `.gitignore` lists it; the active environment is Linux (Python 3.13). Never use
  `git add -A` / `git add .` — stage explicit paths only. The same applies to
  tracked `output/checkpoint.json` and `output/search_results.xlsx` despite
  ignore rules.
- `README.md` is the user-facing guide (Windows-first, non-technical audience);
  update it when user-visible behavior or setup flow changes.
