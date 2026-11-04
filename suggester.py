"""
suggester.py — search-term and extraction-goal suggestions for the web app.

Three independent helpers, all called from app.py:

  - autocomplete(query): instant completions from PubMed's public suggestion
    endpoint (https://pubmed.ncbi.nlm.nih.gov/suggestions/). Errors are treated
    as "no suggestions" so the UI stays usable offline.

  - suggest_related(term) / suggest_related_many(terms): conceptual expansion
    through the same Ollama / OpenAI-compatible backend that extractor.py uses
    (JSON mode / schema-constrained output). These raise BackendUnavailable when
    the configured model cannot be reached, so the UI can show an actionable
    message.

  - suggest_goal(goal, columns, mode): rewrites the user's extraction goal as
    one to three clearer, more extraction-friendly goals. It only returns text
    candidates; applying one is an explicit user action in the UI.

None of the helpers touch the search or extraction pipeline and none has any
state-changing side effects (in particular, they never record prompt history).
"""

import json
import re

import requests

import config
import extractor

PUBMED_SUGGEST_URL = "https://pubmed.ncbi.nlm.nih.gov/suggestions/"
AUTOCOMPLETE_TIMEOUT = 6      # seconds — keep the dropdown snappy
LLM_TIMEOUT = 120             # seconds — interactive, much shorter than extraction
MAX_AUTOCOMPLETE = 8
MAX_RELATED = 8
MAX_GOALS = 3
MAX_GOAL_CHARS = 4000
MAX_GOAL_SUGGESTION_CHARS = 1200
MAX_GOAL_COLUMNS = 50
MAX_GOAL_COLUMN_CHARS = 80
GOAL_MODES = ("records", "table")
GOAL_MODE_LABELS = {
    "records": "individual records (one row per distinct record)",
    "table": "table rows (one row per table row, never merged)",
}


def llm_source():
    """Provenance metadata attached to LLM suggestion payloads.

    The UI renders this as a badge, e.g. "LLM · qwen2.5:7b-instruct". Keeping it
    here means app.py and any future caller report the same, current model name.
    """
    if config.LLM_BACKEND == "openai":
        model = config.OPENAI_MODEL or ""
    else:
        model = config.OLLAMA_MODEL or ""
    return {"source": "llm", "model": model}

_RELATED_RULES = (
    "Return closely related literature-search terms for the same concept. Include "
    "synonyms, acronyms, full names, alternative gene/protein names, British/US "
    "spellings, and legacy names. Only include well-established terms. Do not repeat "
    "the original term. Keep each suggestion short (1-6 words)."
)

_SINGLE_SCHEMA = {
    "type": "object",
    "properties": {
        "suggestions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["suggestions"],
}

_MULTI_SCHEMA = {
    "type": "object",
    "properties": {
        "groups": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "term": {"type": "string"},
                    "suggestions": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["term", "suggestions"],
            },
        },
    },
    "required": ["groups"],
}

_GOAL_SCHEMA = {
    "type": "object",
    "properties": {
        "suggestions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["suggestions"],
}


class BackendUnavailable(RuntimeError):
    """The configured LLM backend is not reachable/configured."""


# --------------------------------------------------------------------------
# Instant autocomplete (PubMed)
# --------------------------------------------------------------------------

def autocomplete(query, limit=MAX_AUTOCOMPLETE):
    """Return up to `limit` PubMed search completions for `query`.

    Never raises: any network/parse problem yields an empty list so the caller
    can silently fall back to plain query building.
    """
    q = (query or "").strip()[:200]
    if len(q) < 2:
        return []
    try:
        limit = max(1, min(int(limit), 20))
    except (TypeError, ValueError):
        limit = MAX_AUTOCOMPLETE

    headers = {
        "User-Agent": config.HTTP_HEADERS.get("User-Agent", "general-datascraper/1.0"),
        "Accept": "application/json",
    }
    try:
        r = requests.get(
            PUBMED_SUGGEST_URL,
            params={"term": q},
            headers=headers,
            timeout=AUTOCOMPLETE_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
    except Exception:
        return []

    if not isinstance(data, dict):
        return []
    raw = data.get("suggestions")
    if not isinstance(raw, list):
        return []

    out, seen = [], set()
    ql = q.lower()
    for value in raw:
        s = re.sub(r"\s+", " ", str(value or "")).strip()
        if not s:
            continue
        # PubMed lowercases suggestions; keep the user's capitalisation on the
        # part they already typed (e.g. "ENaC" + "enac kidney" -> "ENaC kidney").
        if s.lower().startswith(ql):
            s = q + s[len(q):]
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------
# LLM "Suggest related terms"
# --------------------------------------------------------------------------

def _require_backend():
    status = extractor.backend_status()
    if not status.get("ok"):
        raise BackendUnavailable(
            status.get("message")
            or "The LLM backend is not available. Start Ollama (or configure the "
               "OpenAI-compatible backend in config.py) and try again."
        )


def _call_llm(prompt, schema):
    """Call the configured backend in JSON mode, mirroring extractor.py's style."""
    if config.LLM_BACKEND == "openai":
        payload = {
            "model": config.OPENAI_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {config.OPENAI_API_KEY}"}
        url = config.OPENAI_BASE_URL.rstrip("/") + "/chat/completions"
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=LLM_TIMEOUT)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        except requests.RequestException as e:
            raise BackendUnavailable(
                f"Could not reach the OpenAI-compatible backend ({e}). "
                "Check OPENAI_BASE_URL / OPENAI_API_KEY in config.py."
            )

    payload = {
        "model": config.OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "format": schema,
        "options": {
            "temperature": 0.2,
            "num_ctx": config.NUM_CTX,
            "num_predict": 1024,
        },
    }
    try:
        r = requests.post(
            f"{config.OLLAMA_URL}/api/generate", json=payload, timeout=LLM_TIMEOUT
        )
        r.raise_for_status()
        return r.json().get("response", "")
    except requests.RequestException as e:
        raise BackendUnavailable(
            f"Could not reach Ollama at {config.OLLAMA_URL} ({e}). "
            "Start Ollama and try again."
        )


def _load_json_object(raw):
    """Best-effort parse of a JSON object, tolerating ```json fences and prose."""
    def _try(s):
        try:
            d = json.loads(s)
        except json.JSONDecodeError:
            return None
        return d if isinstance(d, dict) else None

    got = _try(raw)
    if got is not None:
        return got
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if m:
        got = _try(m.group(1))
        if got is not None:
            return got
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        return _try(m.group(0))
    return None


def _clean_suggestions(original, values):
    """Normalise, dedupe and cap the model's suggestions for one term."""
    orig = (original or "").strip().lower()
    out, seen = [], set()
    if not isinstance(values, list):
        return out
    for value in values:
        if not isinstance(value, str):
            continue
        s = re.sub(r"\s+", " ", value.strip().strip('"').strip())
        if not s or len(s) > 120:
            continue
        if s.lower() == orig or s.lower() in seen:
            continue
        seen.add(s.lower())
        out.append(s)
        if len(out) >= MAX_RELATED:
            break
    return out


def _single_prompt(term):
    return (
        "You are a biomedical literature search assistant.\n"
        f'Term: "{term}"\n'
        + _RELATED_RULES
        + "\nReturn a JSON object with one key, \"suggestions\", whose value is an "
        "array of strings. Return ONLY the JSON object (no prose, no markdown)."
    )


def _multi_prompt(terms):
    listed = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(terms))
    return (
        "You are a biomedical literature search assistant.\n"
        "For EACH term below, provide related search terms.\n"
        + _RELATED_RULES
        + "\n\nTerms:\n"
        + listed
        + "\n\nReturn a JSON object with one key, \"groups\", whose value is an array "
        "of objects. Each object must have \"term\" (the exact input term) and "
        "\"suggestions\" (an array of strings). Every input term must appear exactly "
        "once, in the same order. Return ONLY the JSON object (no prose, no markdown)."
    )


def suggest_related(term):
    """Return related terms for a single search term."""
    term = re.sub(r"\s+", " ", (term or "").strip())[:200]
    if not term:
        raise ValueError("Type a term first.")
    _require_backend()
    raw = _call_llm(_single_prompt(term), _SINGLE_SCHEMA)
    data = _load_json_object(raw) or {}
    return _clean_suggestions(term, data.get("suggestions"))


def suggest_related_many(terms):
    """Return [{"term", "suggestions"}] for every input term (order preserved)."""
    clean_terms, seen = [], set()
    for raw_term in terms or []:
        t = re.sub(r"\s+", " ", str(raw_term or "").strip())[:200]
        key = t.lower()
        if not t or key in seen:
            continue
        seen.add(key)
        clean_terms.append(t)
    if not clean_terms:
        raise ValueError("Add at least one search term first.")
    _require_backend()
    raw = _call_llm(_multi_prompt(clean_terms), _MULTI_SCHEMA)
    data = _load_json_object(raw) or {}

    by_term = {}
    groups = data.get("groups")
    if isinstance(groups, list):
        for group in groups:
            if not isinstance(group, dict):
                continue
            label = str(group.get("term") or "").strip().lower()
            if not label:
                continue
            for original in clean_terms:
                if label == original.lower():
                    by_term[original.lower()] = _clean_suggestions(
                        original, group.get("suggestions")
                    )
                    break

    return [
        {"term": t, "suggestions": by_term.get(t.lower(), [])}
        for t in clean_terms
    ]


# --------------------------------------------------------------------------
# LLM "Improve goal" (extraction instruction)
# --------------------------------------------------------------------------

_GOAL_RULES = (
    "Rewrite the extraction goal as a precise instruction for a research-paper "
    "extraction assistant. Preserve the user's scientific intent; do not change "
    "what they want to learn. Make the goal self-contained, specific and "
    "extraction-friendly. Clarify what counts as one record (or one table row), "
    "which conditions, units and quantified values matter, and whether negative "
    "or null findings should be reported. State the output granularity explicitly. "
    "Do not invent domain facts, values, or requirements the user did not ask for. "
    "Return 1-3 concise candidate goals, not commentary."
)


def _clean_goal(goal):
    """Validate and normalise the user's extraction goal."""
    if goal is None:
        goal = ""
    if not isinstance(goal, str):
        raise ValueError("The extraction goal must be text.")
    goal = re.sub(r"\s+", " ", goal).strip()
    if not goal:
        raise ValueError("Describe what data you want to extract first.")
    if len(goal) > MAX_GOAL_CHARS:
        raise ValueError(
            f"The extraction goal is too long (max {MAX_GOAL_CHARS} characters)."
        )
    return goal


def _clean_goal_columns(columns):
    """Return cleaned, deduped column names from a list or comma-separated string."""
    if columns is None:
        return []
    if isinstance(columns, str):
        raw_columns = columns.split(",")
    elif isinstance(columns, (list, tuple)):
        raw_columns = columns
    else:
        raise ValueError(
            "Columns must be a list of names or a comma-separated string."
        )

    out, seen = [], set()
    for raw in raw_columns:
        if raw is None:
            continue
        name = re.sub(r"\s+", " ", str(raw)).strip()
        if not name:
            continue
        if len(name) > MAX_GOAL_COLUMN_CHARS:
            name = name[:MAX_GOAL_COLUMN_CHARS].rstrip()
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
        if len(out) >= MAX_GOAL_COLUMNS:
            break
    return out


def _goal_mode(mode):
    """Validate and normalise the extraction mode for the goal prompt."""
    value = str(mode or "records").strip().lower()
    if value not in GOAL_MODES:
        raise ValueError(
            f"Unknown extraction mode '{mode}'. Choose one of: "
            f"{', '.join(GOAL_MODES)}."
        )
    return value


def _goal_prompt(goal, columns, mode):
    listed = ", ".join(columns) if columns else "(none specified)"
    return (
        "You are an expert extraction-instruction editor.\n"
        f'Current goal: "{goal}"\n'
        f"Requested columns: {listed}\n"
        f"Extraction mode: {GOAL_MODE_LABELS[mode]}\n\n"
        + _GOAL_RULES
        + "\nReturn a JSON object with one key, \"suggestions\", whose value is an "
        "array of 1-3 goal strings. Return ONLY the JSON object (no prose, no "
        "markdown)."
    )


def _clean_goal_suggestions(values):
    """Normalise, dedupe and cap the model's rewritten goals."""
    out, seen = [], set()
    if not isinstance(values, list):
        return out
    for value in values:
        if not isinstance(value, str):
            continue
        s = value.strip()
        # Tolerate models that wrap an item in a fence or decorate it with
        # markdown despite being asked for plain strings.
        s = re.sub(r"^```[A-Za-z0-9_-]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
        s = re.sub(r"^#{1,6}\s+", "", s)
        s = re.sub(r"^(?:[-*\u2022]|\d+[.)])\s+", "", s)
        s = re.sub(r"(\*\*|__)(.+?)\1", r"\2", s)
        s = re.sub(r"^(?:goal|option)\s*\d*\s*[:\-]\s*", "", s, flags=re.IGNORECASE)
        s = s.strip().strip('"').strip("'").strip()
        s = re.sub(r"\s+", " ", s)
        if len(s) > MAX_GOAL_SUGGESTION_CHARS:
            s = s[:MAX_GOAL_SUGGESTION_CHARS].rstrip()
        key = s.lower()
        if not s or key in seen:
            continue
        seen.add(key)
        out.append(s)
        if len(out) >= MAX_GOALS:
            break
    return out


def suggest_goal(goal, columns=None, mode="records"):
    """Return 1-3 clearer extraction goals for the current goal/columns/mode.

    Raises ValueError for invalid input and BackendUnavailable when the
    configured LLM cannot be reached. This function has no side effects: it
    does not save the goal, start an extraction job, or record prompt history.
    """
    clean_goal = _clean_goal(goal)
    clean_columns = _clean_goal_columns(columns)
    clean_mode = _goal_mode(mode)
    _require_backend()
    raw = _call_llm(
        _goal_prompt(clean_goal, clean_columns, clean_mode),
        _GOAL_SCHEMA,
    )
    data = _load_json_object(raw or "") or {}
    return _clean_goal_suggestions(data.get("suggestions"))
