"""
filters.py — post-search result filters.

The only filter right now is the journal blocklist: after all sources have
answered and their results have been merged, papers whose `journal` field
contains any blocked journal name are dropped from the result set.

Matching is deliberately a case-insensitive SUBSTRING test, not an exact
equality test. A full journal name such as

    "Journal of Questionable Results"

therefore also catches records with extra punctuation or different spacing,
e.g. "journal of questionable results." or "Journal  of   Questionable Results".
The trade-off is that a very short term can match unrelated journals, so use
the full journal name where possible.

This module is pure (no I/O, no globals) so it is easy to test and can be used
by both the CLI and the web app.
"""


def normalize_journal(text):
    """Normalize journal text for comparison: casefold, collapse spaces, strip."""
    if text is None:
        return ""
    return " ".join(str(text).casefold().split())


def parse_journal_terms(value):
    """Turn a settings value into a list of journal names.

    Accepts:
      - None                      -> []
      - a list/tuple of names     -> cleaned names
      - a newline-separated string -> one name per line

    Leading/trailing whitespace and blank lines are ignored. Duplicates are
    removed case-insensitively (and whitespace-insensitively), keeping the
    first spelling the user supplied so the UI can show it back unchanged.
    """
    if value is None:
        return []
    if isinstance(value, str):
        raw = value.splitlines()
    elif isinstance(value, (list, tuple)):
        raw = value
    else:
        raw = [value]

    terms, seen = [], set()
    for item in raw:
        if item is None:
            continue
        text = str(item).strip()
        if not text:
            continue
        key = normalize_journal(text)
        if not key or key in seen:
            continue
        seen.add(key)
        terms.append(text)
    return terms


def _iter_terms(terms):
    """Yield normalized terms from a parsed list or a raw string/list value."""
    if terms is None:
        return []
    if isinstance(terms, str):
        terms = parse_journal_terms(terms)
    elif not isinstance(terms, (list, tuple)):
        terms = parse_journal_terms(terms)
    return [normalize_journal(t) for t in terms if normalize_journal(t)]


def journal_matches(journal, terms):
    """True when `journal` contains any blocked term (case-insensitive).

    Empty journals and empty terms never match. A term also matches when it
    appears in the middle of a longer journal name (substring matching), which
    is what makes punctuation/spacing variants of a journal easy to block.
    """
    haystack = normalize_journal(journal)
    if not haystack:
        return False
    for needle in _iter_terms(terms):
        if needle in haystack:
            return True
    return False


def is_excluded(paper, terms):
    """True when a paper's `journal` is blocked. Papers without a journal stay."""
    if not paper:
        return False
    return journal_matches(paper.get("journal"), terms)


def filter_papers(papers, terms):
    """Split papers into `(kept, excluded)` without mutating the input.

    Returns two new lists in the original order. The count of excluded papers
    is simply `len(excluded)`.
    """
    kept, excluded = [], []
    for paper in papers or []:
        if is_excluded(paper, terms):
            excluded.append(paper)
        else:
            kept.append(paper)
    return kept, excluded
