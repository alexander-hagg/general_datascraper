# ScrapeLite

A small local web app that searches scientific databases for papers and pulls
structured data out of them using a local LLM (Ollama) — no code or terminal needed.

**The whole flow:** type search terms → pick websites → **Search** → open-access
papers are fetched → an LLM reads the full text and extracts whatever you asked
for → everything lands in an Excel file in the `output/` folder.

---

## Quick start (for someone who doesn't write code)

1. **One time only:** double-click `setup.bat`.
   It installs Python (if missing), creates an isolated Python environment,
   installs the packages, installs Ollama (if missing) and downloads the model.
2. **Every time:** double-click `start.bat`.
   A browser opens at http://127.0.0.1:5000 — that's the whole app.
3. Search for papers, then click **Extract data** and describe what you want
   (e.g. *"every mutation tested and whether it increased or decreased function"*).
4. Results are saved automatically in the `output/` folder. Use the
   **📂 Open output folder** button in the app to jump straight there.

That's it. The Excel files are the output; the browser is just the control panel.



## Dry run (preview without changing anything)

Want to see exactly what setup would do before committing? Run the setup script
with `--dry-run` (also accepted: `-n`, `--dryrun`). It walks through the same
decisions as a real run — Python detection and version check, virtual
environment, pip installs, Ollama install, and model download — but performs
**no state-changing operations at all**: no venv creation, no `pip install`, no
`brew`/`curl | sh`, no `ollama pull`, and no file writes.

- **Windows:** `setup.bat --dry-run` (or `start.bat --dry-run`)
- **Linux / macOS:** `bash setup.sh --dry-run` (or `bash start.sh --dry-run`)

Each step is printed as `[DRY-RUN] would ...` or `[DRY-RUN] skip ...`, including
the condition that led to the decision. The run ends with
`Dry run complete — nothing was changed.` Re-run without the flag to apply.



## Nested boolean groups

The search box supports nested groups, similar to PubMed Advanced Search
or Web of Science history combining. Each row after the first has its own
AND/OR/NOT connector, and rows can be wrapped into a group with its own
AND/OR connector. Groups can be nested arbitrarily.

- Use the checkboxes to select one or more rows/groups, then click
  **Wrap selected in group**.
- Click a group's header/block to cycle its selection:
  **1st click** selects the group itself, **2nd click** selects all members
  (descendants) inside it, **3rd click** returns to the group itself. The cycle
  repeats 1 → 2 → 1 → 2. Clicking buttons, inputs, dropdowns or a nested group
  inside the block does not trigger the parent group's cycle. In state 1,
  **Wrap selected in group** wraps the group node itself; in state 2 it wraps
  the group's member nodes.
- Selection is preserved when you add terms/groups or accept suggestions, so
  you never have to re-check rows after the tree is restructured.
- Inside a group use **+ term** / **+ group** to add children, and **dissolve**
  to remove the group while keeping its contents.
- The **Query preview** line always shows the exact string that will be sent to
  the search APIs.

Multi-word terms are quoted automatically (`epithelial sodium channel` becomes
`"epithelial sodium channel"`). Terms that already contain double quotes or a
PubMed field tag such as `[Title/Abstract]` are left exactly as typed.

Example queries you can build:

```
"Epithelial Sodium Channel" AND (missense OR mutation)
(ENaC OR "epithelial sodium channel" OR SCNN1) AND (missense OR mutation)
A AND (B OR (C AND D))
```

Multi-word terms are quoted automatically, so `Epithelial Sodium Channel` is
emitted as `"Epithelial Sodium Channel"` in the first example.

## Search suggestions

Two kinds of help are available while you build the query:

- **Instant autocomplete** — as you type in any term box, a dropdown appears with
  completions from PubMed's public suggestion API
  (`https://pubmed.ncbi.nlm.nih.gov/suggestions/`). Use the arrow keys and Enter
  to pick one, or Escape to dismiss; clicking an item inserts it into the term.
  Requests go through the local Flask server, so the browser never talks to NCBI
  directly. If the request fails, the dropdown simply stays empty and plain query
  building keeps working.
- **✨ per-row button** — asks the configured LLM for related terms for that one
  term: synonyms, acronyms, alternative gene/protein names, British/US spellings,
  and legacy names (e.g. `ENaC` → `epithelial sodium channel`, `SCNN1`).
- **✨ Suggest related terms (global)** — does the same for every term in the
  query tree and shows the suggestions grouped per source term.

Every suggestion is labelled with its provenance: autocomplete items carry a
**PubMed** badge, while ✨ items carry an **LLM** badge that includes the
configured model name (e.g. `LLM · qwen2.5:7b-instruct`). The `source` (and
`model`) fields are also present in the `/api/autocomplete` and `/api/suggest`
JSON payloads.

Accepted suggestions are wrapped together with the original term into a
parenthesized OR group, e.g. `ENaC` + `epithelial sodium channel` + `SCNN1`
becomes `(ENaC OR "epithelial sodium channel" OR SCNN1)`; multi-word terms are
quoted by the same rule as the rest of the builder. The autocomplete dropdown
only completes the term you are typing — it does not create OR groups.

**Privacy:** autocomplete sends the typed text to NCBI/PubMed. LLM suggestions
send the affected search term(s) to the LLM backend configured in `config.py`
(or the Settings panel) — a local Ollama model keeps this on your machine, while
an OpenAI-compatible endpoint sends them to that provider. Nothing else is sent,
and neither feature changes the search or extraction pipeline.

## PDF archiving & preprint fallback

During the **Extract** step (never during Search), ScrapeLite also downloads and
archives each paper's PDF into `output/pdfs/`. Filenames are deterministic and
derived from the paper key: `PMID_123456.pdf` when a PMID exists, otherwise the
DOI (e.g. `10.1000_example.pdf`), otherwise a sanitized title. Only payloads that
start with the `%PDF-` magic bytes are written, and every network call is
timeout-guarded, so a failed download never stops the run.

The fallback order is:

1. **Article PDF first** — the PMC Open Access PDF link, an open-access URL the
   search source already provided (if it is a PDF), or Unpaywall's
   `url_for_pdf` (best location first, then all OA locations).
2. **Preprint PDF next** — if no article PDF is obtainable, ScrapeLite looks for
   a preprint version via Europe PMC preprint records (`PPR` ids with a PDF
   full-text link), Crossref `relation` (`has-preprint` / `is-preprint-of`), and
   the bioRxiv / medRxiv public API (`api.biorxiv.org/details/...`).
3. **Neither** — the paper is treated as not open access and added to
   `output/closed_source_papers.xlsx` with the reason
   `no PDF and no preprint found`.

When a preprint PDF is used, extraction still proceeds (a preprint counts as
open access) and the output marks it: `extracted_data.xlsx` gains `pdf_file` and
`pdf_source` columns, where `pdf_source` is e.g. `article:pmc`,
`preprint:biorxiv`, or `none`. This makes it clear that the data may differ from
the final published article.

If extractable full text exists (HTML/XML ≥ 500 characters) but no PDF and no
preprint can be saved, the data is still extracted **and** the paper is also
appended to `output/closed_source_papers.xlsx` with the reason
`PDF unavailable — download manually`, so the PDF gap stays on your radar. If
neither text nor a PDF/preprint exists, the paper is closed-source exactly as
before.

PDF archiving is on by default; set `PDF_ARCHIVE_ENABLED = False` in
`config.py` to skip it. The `output/pdfs/` folder is git-ignored.

## Requirements

- Windows 10/11 (macOS/Linux also work if you run `python app.py` directly)
- Python 3.10+ (installed automatically by `setup.bat` via winget if missing)
- [Ollama](https://ollama.com) (installed automatically by `setup.bat` if missing)
- ~8 GB free disk for the model, plus a few GB of RAM to run it
