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


## Search & Extract vs Data & PDFs

The results area has two tabs:

- **Search & Extract** (the default) shows the papers that were found.
- **Data & PDFs** appears as soon as there are extracted rows. It groups those
  rows by paper, so you can see at a glance which paper each row came from.
  Each group shows the paper title, journal, year and how many rows belong to
  it; the rows themselves are listed underneath. Rows whose paper cannot be
  identified are collected in a clearly labelled **Unlinked rows** group.

Click any data row to highlight it. A panel on the right shows the selected
paper and a placeholder for evidence. PDF viewing and annotation are not in
this version yet, but the pane is already reserved for them. The technical
`paper_key` column is used behind the scenes and is not shown in the data
tables; your own extracted columns are always shown.


## Extraction modes

The Extract card has an **Extraction mode** selector:

- **Individual records** (default) — one output row per distinct record the
  model finds. This is the right choice for prose, mixed papers, and most
  everyday extraction.
- **Table rows** — one output row per row of a results table. The model is told
  never to merge, combine or summarise separate table rows. Use this when a
  paper reports a table and every table row must stay separate.

The same choice is available from the command line:

```
python main.py --extract-only --mode table
```

Omitting `--mode` uses `EXTRACTION_MODE = "records"` from `config.py`.


## Paper identity and the spreadsheet columns

Every search-result and extracted row now carries a stable `paper_key` (PMID
first, then lowercased DOI, then title) so the app can connect extracted rows
back to their paper.
Extracted rows also have a 1-based `row_index` that restarts for each paper.
Older `search_results.xlsx` files without a `paper_key` still work: the key is
worked out in memory, and existing extracted files are upgraded in place the
next time extraction runs, without losing any rows or values. Where an older
extracted row has a DOI, the key is filled in from that DOI; rows with no
identifying information are left blank and appear under **Unlinked rows**.

`extracted_data.xlsx` also reserves `evidence` and `evidence_page` columns now
(they are empty until the evidence features arrive). The names `paper_key`,
`row_index`, `evidence`, `evidence_page`, `pdf_file`, `pdf_source`, plus the
existing `doi` and `source` metadata columns, cannot be used as your own
column names; the app asks you to rename them if you try. Duplicate column
names (ignoring case) are rejected as well, so the Excel header stays
unambiguous.



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
AND/OR/NOT connector. Groups can be nested arbitrarily.

A group whose connector is **NOT** is excluded from the part of the query
before it. For example:

```
A AND B NOT (predatory OR questionable)
```

finds papers matching A and B, then removes anything that matches either
`predatory` or `questionable`. PubMed and Europe PMC apply this boolean logic
directly; Crossref's `query` is relevance-based, so it treats the words as
search terms rather than strict boolean filtering. In the builder, put `A` and
`B` at the top level, add a group with `predatory` and `questionable`, then set
the group's connector to **NOT**.

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
A AND B NOT (predatory OR questionable)
```

Multi-word terms are quoted automatically, so `Epithelial Sodium Channel` is
emitted as `"Epithelial Sodium Channel"` in the first example.


## Excluding journals

The **Exclude journals** box in the Search card lets you drop papers from
specific journals after the search has run. Put **one journal name per line**,
for example:

```
Predatory Journal of Examples
Journal of Questionable Results
```

Matching is **case-insensitive** and uses **substrings**, so a full journal name
also catches records with different capitalisation, spacing, or extra punctuation
around the name (for example `journal of questionable results.`). Papers with no
journal name are never excluded. Because the match is a substring, a very short
entry can catch unrelated journals — use the full journal name where possible.

The same list can be set in `config.py`:

```python
EXCLUDED_JOURNALS = [
    "Predatory Journal of Examples",
    "Journal of Questionable Results",
]
```

Things to know:

- The filter runs **after** all websites have answered and their results have
  been merged, never inside an individual source query. It therefore also removes
  rows that were already in `search_results.xlsx` when the CLI merges a new
  search into the old sheet. The web app replaces the sheet on every search, so
  it filters the fresh result set.
- The app shows how many papers were removed
  (`Search finished: 42 papers (3 excluded by journal).`), and the CLI prints
  `Excluded N paper(s) by journal blocklist.`
- The list is saved with the other web settings, so it comes back the next time
  you open the app. A search also saves it automatically, without clicking
  **Save settings**.
- Journal exclusions are **not** pushed into PubMed, Europe PMC or Crossref
  queries. In particular, Crossref's `query` parameter is relevance-based rather
  than strict boolean matching, so the blocklist is the only reliable way to
  remove those records.


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
