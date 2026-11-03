"""
searchers.py — query PubMed, Europe PMC and Crossref, and merge the results.

Every source speaks the same boolean AND/OR/NOT syntax, so a single query string
from config.QUERIES works across all of them. All three are public REST APIs.

Each result is a plain dict with these keys:
    sources      (list of source names, e.g. ["pubmed", "europepmc"])
    title, authors, abstract, doi, pmid, pmcid, year, journal  (strings)
    is_oa        (True/False/None — whether the source flagged it open access)
    fulltext_url (an open-access full-text URL, if the source provided one)
"""

import re
import time
import xml.etree.ElementTree as ET

import requests

import config

ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
EUROPEPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
CROSSREF = "https://api.crossref.org/works"

# Open ends for Europe PMC's PUB_YEAR range. Its query syntax needs both
# endpoints even when the user only supplied one year.
EUROPEPMC_MIN_YEAR = "1000"
EUROPEPMC_MAX_YEAR = "3000"


def _normalize_year(value):
    """Return one optional publication year as a 4-digit string or None."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("Year must be a four-digit number.")
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
    else:
        raise ValueError("Year must be a four-digit number.")

    if not re.fullmatch(r"[0-9]{4}", text):
        raise ValueError("Year must be a four-digit number.")
    return text


def normalize_year_range(year_from=None, year_to=None):
    """Validate a publication-year range.

    Accepts None, blank strings, integers and 4-digit strings. Returns
    `(year_from, year_to)` as 4-digit strings (or None for an open end).
    Bools and non-4-digit values are rejected, as is an inverted range.
    """
    start = _normalize_year(year_from)
    end = _normalize_year(year_to)
    if start is not None and end is not None and int(start) > int(end):
        raise ValueError("Start year must not be after end year.")
    return start, end


def year_range_label(year_from=None, year_to=None):
    """Human-readable label for a normalized range ('' means all years)."""
    start, end = normalize_year_range(year_from, year_to)
    if start and end:
        return f"{start}-{end}"
    if start:
        return f"{start} onwards"
    if end:
        return f"up to {end}"
    return ""


def paper_key(paper):
    """Dedup key: PMID if present, else DOI (lowercased), else title."""
    if paper.get("pmid"):
        return f"pmid:{paper['pmid']}"
    if paper.get("doi"):
        return f"doi:{paper['doi'].lower()}"
    return f"title:{paper.get('title', '').strip().lower()}"


def _merge_into(papers, incoming):
    key = paper_key(incoming)
    if key not in papers:
        papers[key] = incoming
        return

    existing = papers[key]
    for f in ("title", "authors", "abstract", "doi", "pmid", "pmcid", "year", "journal"):
        if not existing.get(f) and incoming.get(f):
            existing[f] = incoming[f]

    existing["sources"] = sorted(set(existing.get("sources", [])) | set(incoming.get("sources", [])))
    if not existing.get("is_oa") and incoming.get("is_oa"):
        existing["is_oa"] = incoming["is_oa"]
    if not existing.get("fulltext_url") and incoming.get("fulltext_url"):
        existing["fulltext_url"] = incoming["fulltext_url"]


def merge_papers(existing, incoming):
    """Merge a list of already-known papers with a list of newly-found ones.

    Dedupes by PMID > DOI > title and backfills empty fields from the new
    records, so re-running a search keeps old rows and only ADDS new papers.
    """
    papers = {}
    for p in existing:
        _merge_into(papers, p)
    for p in incoming:
        _merge_into(papers, p)
    return list(papers.values())


def search(query, sources, limit=None, year_from=None, year_to=None):
    """Search ONE query across the given sources. Returns a merged, deduped list.

    Used by the web app (the UI sends a query string + a list of source names).
    `year_from`/`year_to` are validated once here and forwarded to each source.
    """
    year_from, year_to = normalize_year_range(year_from, year_to)
    range_note = year_range_label(year_from, year_to)
    papers = {}
    for source in sources:
        entry = SOURCE_REGISTRY.get(source)
        if entry is None:
            print(f"  !! unknown source '{source}' (valid: {', '.join(SOURCE_REGISTRY)}) - skipping")
            continue
        suffix = f" [years {range_note}]" if range_note else ""
        print(f"  searching {source}: {query[:60]}{'...' if len(query) > 60 else ''}{suffix}")
        try:
            results = entry["fn"](
                query, limit=limit, year_from=year_from, year_to=year_to
            )
        except Exception as e:
            print(f"  !! {source} query failed: {e}")
            continue
        for p in results:
            _merge_into(papers, p)
    return list(papers.values())


def search_all(limit=None, year_from=None, year_to=None):
    """CLI helper: run every config.SOURCES x config.QUERIES, merge and dedup."""
    year_from, year_to = normalize_year_range(year_from, year_to)
    papers = {}
    for q in config.QUERIES:
        for p in search(
            q, config.SOURCES, limit=limit, year_from=year_from, year_to=year_to
        ):
            _merge_into(papers, p)
    return list(papers.values())


# --------------------------------------------------------------------------
# PubMed (NCBI E-utilities)
# --------------------------------------------------------------------------

def _pubmed_params(**extra):
    p = {"tool": "general-datascraper", "email": config.NCBI_EMAIL, **extra}
    if config.NCBI_API_KEY:
        p["api_key"] = config.NCBI_API_KEY
    return p


def search_pubmed(query, limit=None, year_from=None, year_to=None):
    year_from, year_to = normalize_year_range(year_from, year_to)
    limit = limit or config.MAX_RESULTS_PER_QUERY

    extra = {
        "db": "pubmed", "term": query, "retmax": str(limit),
        "retmode": "json", "sort": "relevance",
    }
    # PubMed accepts four-digit YYYY values for its own publication-date range.
    # Send only the bounds that exist so an open end stays open.
    if year_from or year_to:
        extra["datetype"] = "pdat"
        if year_from:
            extra["mindate"] = year_from
        if year_to:
            extra["maxdate"] = year_to
    params = _pubmed_params(**extra)
    r = requests.get(ESEARCH, params=params, timeout=config.HTTP_TIMEOUT)
    r.raise_for_status()
    idlist = r.json().get("esearchresult", {}).get("idlist", [])
    time.sleep(config.SLEEP_SECONDS)
    if not idlist:
        return []

    params = _pubmed_params(
        db="pubmed", id=",".join(idlist), rettype="abstract", retmode="xml"
    )
    r = requests.get(EFETCH, params=params, timeout=config.HTTP_TIMEOUT)
    r.raise_for_status()
    time.sleep(config.SLEEP_SECONDS)
    return _parse_pubmed_xml(r.text)


def _parse_pubmed_xml(xml_text):
    root = ET.fromstring(xml_text)
    papers = []
    for article in root.findall(".//PubmedArticle"):
        pmid = (article.findtext(".//MedlineCitation/PMID") or "").strip()
        title = _text_of(article.find(".//Article/ArticleTitle"))
        if not title and not pmid:
            continue
        papers.append({
            "sources": ["pubmed"],
            "title": title,
            "authors": _pubmed_authors(article),
            "abstract": _pubmed_abstract(article),
            "doi": _pubmed_articleid(article, "doi"),
            "pmid": pmid,
            "pmcid": _pubmed_articleid(article, "pmc"),
            "year": _pubmed_year(article),
            "journal": _text_of(article.find(".//Article/Journal/Title")),
            "is_oa": None,
            "fulltext_url": "",
        })
    return papers


def _text_of(el):
    """Join all text inside an element, keeping text inside <i>/<sub> etc.
    (Element.findtext() would stop at the first nested tag and truncate
    titles like 'Human <i>α6β4</i> Nicotinic ...' to just 'Human'.)"""
    if el is None:
        return ""
    return "".join(el.itertext()).strip()


def _pubmed_authors(article):
    names = []
    for a in article.findall(".//Article/AuthorList/Author"):
        last = a.findtext("LastName") or ""
        initials = a.findtext("Initials") or a.findtext("ForeName") or ""
        if last:
            names.append(f"{last} {initials}".strip())
    return "; ".join(names)


def _pubmed_abstract(article):
    parts = []
    for at in article.findall(".//Article/Abstract/AbstractText"):
        parts.append("".join(at.itertext()).strip())
    return " ".join(parts)


def _pubmed_articleid(article, idtype):
    for aid in article.findall(".//PubmedData/ArticleIdList/ArticleId"):
        if aid.get("IdType") == idtype:
            return (aid.text or "").strip()
    if idtype == "doi":
        for el in article.findall(".//Article/ELocationID"):
            if el.get("EIdType") == "doi":
                return (el.text or "").strip()
    return ""


def _pubmed_year(article):
    y = article.findtext(".//Article/Journal/JournalIssue/PubDate/Year")
    if y:
        return y.strip()
    md = article.findtext(".//Article/Journal/JournalIssue/PubDate/MedlineDate")
    if md:
        m = re.search(r"\d{4}", md)
        if m:
            return m.group(0)
    return ""


# --------------------------------------------------------------------------
# Europe PMC (REST)
# --------------------------------------------------------------------------

def search_europepmc(query, limit=None, year_from=None, year_to=None):
    year_from, year_to = normalize_year_range(year_from, year_to)
    limit = limit or config.MAX_RESULTS_PER_QUERY

    query = (query or "").strip()
    if year_from or year_to:
        clause = (
            f"PUB_YEAR:[{year_from or EUROPEPMC_MIN_YEAR} "
            f"TO {year_to or EUROPEPMC_MAX_YEAR}]"
        )
        query = f"{query} AND {clause}" if query else clause

    papers = []
    cursor = "*"
    while len(papers) < limit:
        params = {
            "query": query,
            "format": "json",
            "resultType": "core",
            "pageSize": str(min(1000, limit)),
            "cursorMark": cursor,
        }
        r = requests.get(EUROPEPMC, params=params, headers=config.HTTP_HEADERS, timeout=config.HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        results = data.get("resultList", {}).get("result", [])
        if not results:
            break
        for rec in results:
            p = _europepmc_to_paper(rec)
            if p:
                papers.append(p)
        nxt = data.get("nextCursorMark")
        if not nxt or nxt == cursor:
            break
        cursor = nxt
        time.sleep(config.SLEEP_SECONDS)
    return papers[:limit]


def _europepmc_to_paper(rec):
    pmid = rec.get("pmid", "") or ""
    doi = rec.get("doi", "") or ""
    title = rec.get("title", "") or ""
    if not (pmid or doi or title):
        return None

    journal = rec.get("journalTitle", "") or ""
    if not journal:
        ji = rec.get("journalInfo", {})
        if isinstance(ji, dict):
            journal = (ji.get("journal") or {}).get("title", "") or ""

    fulltext_url = ""
    ftl = rec.get("fullTextUrlList", {})
    if isinstance(ftl, dict):
        urls = ftl.get("fullTextUrl", [])
        if urls and isinstance(urls[0], dict):
            fulltext_url = urls[0].get("url", "") or ""

    is_oa = rec.get("isOpenAccess") in ("Y", "y", True)

    return {
        "sources": ["europepmc"],
        "title": title,
        "authors": rec.get("authorString", "") or "",
        "abstract": rec.get("abstractText", "") or "",
        "doi": doi,
        "pmid": pmid,
        "pmcid": rec.get("pmcid", "") or "",
        "year": str(rec.get("pubYear", "") or ""),
        "journal": journal,
        "is_oa": is_oa if is_oa else None,
        "fulltext_url": fulltext_url,
    }


# --------------------------------------------------------------------------
# Crossref (REST)
# --------------------------------------------------------------------------

def search_crossref(query, limit=None, year_from=None, year_to=None):
    year_from, year_to = normalize_year_range(year_from, year_to)
    limit = limit or config.MAX_RESULTS_PER_QUERY

    # Restrict to real journal articles: a bare `query` also returns figure
    # captions, supplementary files, book chapters and dissertations, which
    # have no authors/year and pollute the results.
    filters = ["type:journal-article"]
    if year_from:
        filters.append(f"from-pub-date:{year_from}-01-01")
    if year_to:
        filters.append(f"until-pub-date:{year_to}-12-31")

    params = {
        "query": query,
        "rows": str(min(limit, 1000)),
        "mailto": config.NCBI_EMAIL,
        "filter": ",".join(filters),
        "select": "DOI,title,author,abstract,published,container-title,link",
    }
    r = requests.get(CROSSREF, params=params, timeout=config.HTTP_TIMEOUT)
    r.raise_for_status()
    items = r.json().get("message", {}).get("items", [])
    papers = []
    for it in items:
        p = _crossref_to_paper(it)
        if p:
            papers.append(p)
    return papers[:limit]


def _crossref_to_paper(it):
    doi = it.get("DOI", "") or ""
    title_list = it.get("title") or []
    title = title_list[0] if title_list else ""
    if not (doi or title):
        return None

    authors = "; ".join(
        f"{a.get('family', '')} {a.get('given', '')}".strip()
        for a in (it.get("author") or [])
        if a.get("family")
    )

    year = ""
    for k in ("published", "published-print", "published-online"):
        pub = it.get(k)
        if isinstance(pub, dict) and pub.get("date-parts"):
            dp = pub["date-parts"][0]
            if dp and dp[0]:
                year = str(dp[0])
                break

    journal = ""
    ct = it.get("container-title") or []
    if ct:
        journal = ct[0]

    abstract = re.sub(r"<[^>]+>", " ", it.get("abstract", "") or "")
    abstract = re.sub(r"\s+", " ", abstract).strip()

    fulltext_url = ""
    for link in (it.get("link") or []):
        if link.get("URL"):
            fulltext_url = link["URL"]
            break

    return {
        "sources": ["crossref"],
        "title": title,
        "authors": authors,
        "abstract": abstract,
        "doi": doi,
        "pmid": "",
        "pmcid": "",
        "year": year,
        "journal": journal,
        "is_oa": None,
        "fulltext_url": fulltext_url,
    }


# Available sources. Adding a new website later = write a search_* function and add
# ONE entry here; it then appears automatically in the web UI and in config.SOURCES.
SOURCE_REGISTRY = {
    "pubmed":    {"label": "PubMed",     "fn": search_pubmed},
    "europepmc": {"label": "Europe PMC", "fn": search_europepmc},
    "crossref":  {"label": "Crossref",   "fn": search_crossref},
}
