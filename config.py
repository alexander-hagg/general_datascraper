"""
config.py — every setting you can change lives in this file.

Open this file, edit the values between the "=====" markers, save, and run:

    python main.py

The pipeline is:

    search (keywords -> websites)  ->  output/search_results.xlsx
    -> for each paper: try to get its full text (open access only)
         open   -> feed to a local/hosted LLM -> output/extracted_data.xlsx
         closed -> output/closed_source_papers.xlsx

See README.md for a full walkthrough of where everything is and what to change.
"""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ==================== 1. SEARCH ====================

# Your search queries. Use plain boolean logic: AND, OR, NOT, parentheses.
# These work across PubMed, Europe PMC and Crossref.
QUERIES = [
    "nicotinic acetylcholine receptor AND mutation AND (electrophysiology OR patch-clamp)",
    "nicotinic acetylcholine receptor AND mutation AND (gain-of-function OR loss-of-function)",
]

# Which websites to search. Delete the ones you don't want.
#   "pubmed"    -> NCBI PubMed (E-utilities API)  — needs NCBI_EMAIL below
#   "europepmc" -> Europe PMC (REST API)          — free, no key
#   "crossref"  -> Crossref (REST API)            — free, no key; great for DOI metadata
#   "chemrxiv"  -> ChemRxiv preprints (via the Crossref API; marked "preprint")
SOURCES = ["pubmed", "europepmc", "crossref", "chemrxiv"]

# Safety cap: max results per query, per source. Raise/lower as you like.
MAX_RESULTS_PER_QUERY = 50

# Optional publication-year range, applied by every search source.
# Use four-digit years (integers or strings): YEAR_FROM = 2015, YEAR_TO = 2024.
# Leave None (or "") for no limit on that side; both None means all years.
# The range is sent to the APIs separately and is NOT added to the query text.
YEAR_FROM = None
YEAR_TO = None

# Journals to drop from the results. Matching is case-insensitive and uses
# substrings, so a full journal name also catches capitalization/spacing
# differences and extra punctuation. It is applied AFTER all sources are
# merged, so papers already in search_results.xlsx are removed too.
# Add one journal name per line, for example:
# EXCLUDED_JOURNALS = [
#     "Predatory Journal of Examples",
#     "Journal of Questionable Results",
# ]
EXCLUDED_JOURNALS = []

# NCBI policy REQUIRES an email for PubMed access. Use your real one.
# (It is only used for contact / rate-limiting, not authentication.)
NCBI_EMAIL = "you@uni.de"

# Optional: an NCBI API key raises your PubMed rate limit (free at
# https://www.ncbi.nlm.nih.gov/account/). Leave "" if you don't have one.
NCBI_API_KEY = ""

# Politeness: seconds to sleep between API requests. Don't set this to 0.
SLEEP_SECONDS = 0.4
HTTP_TIMEOUT = 30
HTTP_HEADERS = {"User-Agent": f"general-datascraper/1.0 (mailto:{NCBI_EMAIL})"}

# ==================== 2. LLM (local or hosted) ====================

# Which model backend to use:
#   "ollama" -> a model running locally on your computer (free, private, offline)
#   "openai" -> any OpenAI-compatible HTTP API (e.g. KI connect NRW, Gemini, etc.)
LLM_BACKEND = "ollama"

# --- Ollama (local) ---
OLLAMA_URL   = "http://localhost:11434"     # default Ollama port
OLLAMA_MODEL = "qwen2.5:7b-instruct"        # your model (see: ollama list)

# --- OpenAI-compatible (e.g. KI connect NRW) ---
# Set LLM_BACKEND = "openai" and fill these in. Your institution gives you a
# base URL + key (see README.md -> "Using KI connect NRW").
OPENAI_BASE_URL = ""     # e.g. "https://<your-endpoint>/v1"
OPENAI_API_KEY  = ""
OPENAI_MODEL    = ""     # model name your provider expects

# Generation settings (used by both backends)
NUM_CTX     = 32768      # context window: how much text the model can read at once
NUM_PREDICT = 8192       # max tokens in the answer
TEMPERATURE = 0.1        # 0.1 = consistent, predictable (good for extraction)
MAX_CHARS   = 90000      # cap each paper at this many characters before asking (~fits the context window)

# ==================== 3. EXTRACTION: what to pull out ====================

# How the CLI treats the model's answer by default:
#   "records" -> one row per distinct record (default)
#   "table"   -> one row per row of a results table; rows are never merged
# The web app lets you pick this per Extract run.
EXTRACTION_MODE = "records"

# Plain-English instructions to the model: what a "record" is, what to skip.
INSTRUCTION = """\
You are a careful research assistant. Read the TEXT below and extract every
record that matches the requested task.

Rules:
- Include every record that is clearly stated or measured in the text.
- NEVER invent or guess. Only report what is explicitly written in the TEXT.
- If a specific value is missing, leave that field empty instead of guessing.
- If no matching records are described, return an empty list [].
"""

# The columns you want in the output spreadsheet. Each line is:
#   ("column name", "kind" [, [allowed values]])
#   kinds:  "text"   -> any text
#           "number" -> a whole number
#           "choice" -> the model must pick one of the allowed values you list
FIELDS = [
    ("subunit",     "text"),
    ("mutation",    "text"),
    ("position",    "number"),
    ("effect",      "choice", ["GOF", "LOF", "No effect"]),
    ("technique",   "text"),
]

# ==================== 4. PDF ARCHIVING (Extract step) ====================

# Save each processed paper's PDF to output/pdfs/ while extracting. The article
# PDF is tried first; if none is available, a preprint PDF is used instead.
# Set to False to skip PDF downloads entirely (extraction still works).
PDF_ARCHIVE_ENABLED = True
PDF_DIR = OUTPUT_DIR / "pdfs"

# Evidence quotes and annotated PDFs (Extract step). When enabled, the model is
# asked for one short verbatim quote per extracted record. ScrapeLite locates
# the quote in the archived PDF, records its 1-based page in `evidence_page`,
# and writes a highlighted copy to output/annotated_pdfs/.
# Scanned/image-only PDFs are not OCR'd, so their quotes stay unresolved.
EVIDENCE_ENABLED = True
EVIDENCE_MAX_CHARS = 300
EVIDENCE_FUZZY_THRESHOLD = 0.75

# Figure/table screenshots (Extract step, PDF only). ScrapeLite scans each
# archived PDF with PyMuPDF for caption regions (or large embedded images on
# pages without captions), then asks the configured LLM which candidates help
# answer the extraction goal. Only selected candidates are rendered and saved
# as PNGs under output/figures/. If the goal does not ask for figures, images,
# tables or visual data, no screenshots are saved.
#   FIGURES_GOAL_FILTER  True = goal-driven selection (default);
#                        False = legacy "save every detected figure" behaviour
#   FIGURE_DPI           rendering resolution of each screenshot
#   FIGURE_MAX_PER_PAPER safety cap on saved figures per paper
#   FIGURE_MAX_CANDIDATES cap on candidates sent to the selection LLM
# There is no OCR and no HTML/PMC figure download: image-only PDFs without a
# text caption yield no figure. Detection/selection is best-effort and never
# fails extraction. The generated index and output/figures.xlsx are git-ignored.
FIGURES_ENABLED = True
FIGURES_GOAL_FILTER = True
FIGURE_DPI = 150
FIGURE_MAX_PER_PAPER = 50
FIGURE_MAX_CANDIDATES = 100
FIGURES_DIR = OUTPUT_DIR / "figures"
FIGURES_INDEX_JSON = FIGURES_DIR / "index.json"
FIGURES_XLSX = OUTPUT_DIR / "figures.xlsx"

# Optional: a folder of PDFs you already have on this computer. Use it from the
# web Search card, or from the CLI with --local-folder PATH. PDFs are copied
# (or hardlinked) into PDF_DIR so extraction stays self-contained.
#   LOCAL_PDF_DIR       default folder shown in the web app ("" = none)
#   LOCAL_PDF_RECURSIVE default for the "Include subfolders" checkbox
#   MAX_LOCAL_PDFS      safety cap; a larger folder is rejected with a clear error
LOCAL_PDF_DIR = ""
LOCAL_PDF_RECURSIVE = False
MAX_LOCAL_PDFS = 500

# ==================== 5. OUTPUT FILES (you usually won't change these) ====================

SEARCH_RESULTS_XLSX = OUTPUT_DIR / "search_results.xlsx"
CLOSED_SOURCE_XLSX  = OUTPUT_DIR / "closed_source_papers.xlsx"
EXTRACTED_XLSX      = OUTPUT_DIR / "extracted_data.xlsx"
CHECKPOINT_JSON     = OUTPUT_DIR / "checkpoint.json"
LOCAL_PDF_INDEX_JSON = OUTPUT_DIR / "local_pdfs.json"
PDF_INDEX_JSON      = OUTPUT_DIR / "pdf_index.json"
ANNOTATED_PDF_DIR   = OUTPUT_DIR / "annotated_pdfs"

# ==================== 4b. RUN FOLDERS (web app) ====================

# A run is one search dataset: its search sheet, extraction outputs, checkpoint,
# PDFs, annotations, figures and a state.json live together in
# output/runs/<run-id>/. The web app enables this at startup
# (`python app.py` / start scripts); the CLI and unit tests keep using the
# legacy global files above unless RUNS_ENABLED is turned on explicitly.
#
# output/active_run.json remembers the loaded run. It is a small marker, not a
# dataset; deleting it only means "pick the newest run or import the legacy
# output/ files". Web settings and prompt history stay global on purpose.
RUNS_ENABLED = False
RUNS_DIR = OUTPUT_DIR / "runs"
ACTIVE_RUN_JSON = OUTPUT_DIR / "active_run.json"
ACTIVE_RUN_DIR = None  # runtime only: set by runs.apply_run_paths()

# Prompt history for the web app (searches, extraction goals and columns).
# It is global across workspaces/browser sessions and git-ignored. Old entries
# are dropped automatically: at most PROMPT_HISTORY_MAX_ENTRIES in total and
# PROMPT_HISTORY_MAX_PER_KIND per kind, keeping the most recently used ones.
PROMPT_HISTORY_JSON = OUTPUT_DIR / "prompt_history.json"
PROMPT_HISTORY_MAX_ENTRIES = 100
PROMPT_HISTORY_MAX_PER_KIND = 50

# ==================== 5. WEB APP ====================

# Run the local web app with:  python app.py   (or double-click start.bat)
APP_HOST      = "127.0.0.1"   # only reachable from this computer (safest)
APP_PORT      = 5000
APP_AUTO_OPEN = True          # open the browser automatically on start
DEFAULT_MAX_RESULTS = 50      # default "max results" shown in the search form
