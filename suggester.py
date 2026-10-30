"""
suggester.py — search-term suggestions for the query builder.

Two independent helpers, both called from app.py:

  - autocomplete(query): instant completions from PubMed's public suggestion
    endpoint (https://pubmed.ncbi.nlm.nih.gov/suggestions/). Errors are treated
    as "no suggestions" so the UI stays usable offline.

  - suggest_related(term) / suggest_related_many(terms): conceptual expansion
    through the same Ollama / OpenAI-compatible backend that extractor.py uses
    (JSON mode / schema-constrained output). These raise BackendUnavailable when
    the configured model cannot be reached, so the UI can show an actionable
    message.

Neither helper touches the search or extraction pipeline and neither has any
state-changing side effects.
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
