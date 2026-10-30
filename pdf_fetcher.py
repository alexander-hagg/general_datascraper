"""
pdf_fetcher.py — download and archive a paper's PDF, with a preprint fallback.

Called during the Extract step (never during Search). For each paper it tries,
in order:

  1. the article PDF
       - PMC Open Access PDF link (from the OA XML, plus canonical PMC/Europe PMC
         render URLs)
       - an open-access URL the search source already gave us, if it is a PDF
       - Unpaywall `url_for_pdf` (best location first, then all OA locations)
  2. a preprint PDF
       - Europe PMC preprint records (PPR ids) with a PDF full-text link
       - Crossref `relation` (`has-preprint` / `is-preprint-of`)
       - bioRxiv / medRxiv public API (`api.biorxiv.org/details/...`)

If neither works, returns a clear failure reason. Every network call is
timeout-guarded and failures never raise out of `acquire_pdf()`.

The saved file is validated to start with the `%PDF-` magic bytes before it is
written to `config.PDF_DIR` (output/pdfs/ by default).
"""

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

import config

EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
EUROPEPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
CROSSREF = "https://api.crossref.org/works"
UNPAYWALL = "https://api.unpaywall.org/v2"
BIORXIV = "https://api.biorxiv.org/details/biorxiv"
MEDRXIV = "https://api.biorxiv.org/details/medrxiv"

PDF_MAGIC = b"%PDF-"
XLINK_HREF = "{http://www.w3.org/1999/xlink}href"


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def acquire_pdf(paper):
    """Download and archive a PDF for `paper`.

    Returns a dict:
        path      Path to the saved PDF, or None
        filename  deterministic filename ("" when nothing was saved)
        source    "article" | "preprint" | None
        server    which server supplied it (e.g. "pmc", "unpaywall", "biorxiv")
        label     "article:pmc" / "preprint:biorxiv" / "none"
        reason    failure reason ("" on success)

    Never raises.
    """
    try:
        if not getattr(config, "PDF_ARCHIVE_ENABLED", True):
            # Archiving is off, so no full text is obtainable here. Report the
            # documented closed-source reason rather than an internal toggle note.
            return _result(None, paper, reason="no PDF and no preprint found")

        filename = pdf_filename(paper)
        dest = Path(config.PDF_DIR) / filename

        # 1. Article PDF first.
        for url, server in _article_pdf_candidates(paper):
            if _download_pdf(url, dest):
                return _result(dest, paper, "article", server)

        # 2. Preprint PDF next.
        for url, server in _preprint_pdf_candidates(paper):
            if _download_pdf(url, dest):
                return _result(dest, paper, "preprint", server)

        return _result(None, paper, reason="no PDF and no preprint found")
    except Exception as e:  # defensive: archiving must never break a run
        return _result(None, paper, reason=f"pdf archiving error: {e}")


def pdf_text(path):
    """Extract text from a saved PDF (used when full-text fetching failed)."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception:
        return ""


def pdf_filename(paper):
    """Deterministic, filesystem-safe name: PMID > DOI > sanitized title."""
    pmid = (paper.get("pmid") or "").strip()
    doi = (paper.get("doi") or "").strip()
    if pmid:
        stem = f"PMID_{_safe(pmid)}"
    elif doi:
        stem = _safe(doi)
    else:
        stem = _safe(paper.get("title") or "paper")
    return f"{stem}.pdf"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _result(path, paper, source=None, server="", reason=""):
    filename = pdf_filename(paper) if path else ""
    if path:
        label = f"{source}:{server}" if server else (source or "unknown")
    else:
        label = "none"
    return {
        "path": path,
        "filename": filename,
        "source": source,
        "server": server,
        "label": label,
        "reason": reason,
    }


def _safe(value):
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value)).strip("._-")
    return value or "paper"


def _looks_like_pdf(url):
    return url.lower().split("?")[0].endswith(".pdf")


def _download_pdf(url, dest):
    """Fetch `url`, verify the %PDF- magic, and write it to `dest`."""
    if not url:
        return False
    try:
        r = requests.get(
            url,
            headers=config.HTTP_HEADERS,
            timeout=config.HTTP_TIMEOUT,
            allow_redirects=True,
        )
        r.raise_for_status()
        content = r.content
    except Exception:
        return False
    if not content or content.lstrip()[:5] != PDF_MAGIC:
        return False
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
    except OSError:
        return False
    return True


# --------------------------------------------------------------------------
# 1. Article PDF candidates
# --------------------------------------------------------------------------

def _article_pdf_candidates(paper):
    pmcid = (paper.get("pmcid") or "").strip()
    if pmcid:
        for url in _pmc_pdf_urls(pmcid):
            yield url, "pmc"

    url = (paper.get("fulltext_url") or "").strip()
    if url and _looks_like_pdf(url):
        yield url, "source"

    doi = (paper.get("doi") or "").strip()
    if doi:
        for url in _unpaywall_pdf_urls(doi):
            yield url, "unpaywall"


def _pmc_pdf_urls(pmcid):
    urls = []
    params = {
        "db": "pmc",
        "id": pmcid,
        "rettype": "xml",
        "retmode": "xml",
        "tool": "general-datascraper",
        "email": config.NCBI_EMAIL,
    }
    try:
        r = requests.get(
            EFETCH, params=params, headers=config.HTTP_HEADERS,
            timeout=config.HTTP_TIMEOUT,
        )
        r.raise_for_status()
        root = ET.fromstring(r.text)
        for el in root.iter():
            if not el.tag.endswith("self-uri"):
                continue
            ctype = (el.get("content-type") or "").lower()
            href = el.get(XLINK_HREF) or el.get("href") or ""
            if href and ("pdf" in ctype or href.lower().endswith(".pdf")):
                urls.append(href)
    except Exception:
        pass

    # Canonical PMC PDF path and Europe PMC's PDF render endpoint.
    urls.append(f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/pdf/")
    urls.append(f"https://europepmc.org/articles/{pmcid}?pdf=render")
    return _dedupe(urls)


def _unpaywall_pdf_urls(doi):
    params = {"email": config.NCBI_EMAIL}
    try:
        r = requests.get(
            f"{UNPAYWALL}/{doi}", params=params, headers=config.HTTP_HEADERS,
            timeout=config.HTTP_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
    except Exception:
        return []
    if not data.get("is_oa"):
        return []

    locations = []
    best = data.get("best_oa_location") or {}
    if isinstance(best, dict) and best:
        locations.append(best)
    locations.extend(data.get("oa_locations") or [])

    urls = []
    for loc in locations:
        if not isinstance(loc, dict):
            continue
        url = loc.get("url_for_pdf") or ""
        if url:
            urls.append(url)
    return _dedupe(urls)


# --------------------------------------------------------------------------
# 2. Preprint PDF candidates
# --------------------------------------------------------------------------

def _preprint_pdf_candidates(paper):
    doi = (paper.get("doi") or "").strip()
    title = (paper.get("title") or "").strip()

    for url in _europepmc_preprint_pdf_urls(doi, title):
        yield url, "europepmc"

    for url in _crossref_preprint_pdf_urls(doi):
        yield url, "crossref"

    for url, server in _biorxiv_preprint_pdf_urls(doi):
        yield url, server


def _europepmc_preprint_pdf_urls(doi, title):
    queries = []
    if doi:
        queries.append(f'DOI:"{doi}"')
    if title:
        queries.append(f'TITLE:"{title.replace(chr(34), "")}"')

    urls = []
    for query in queries:
        params = {
            "query": query,
            "format": "json",
            "resultType": "core",
            "pageSize": "25",
        }
        try:
            r = requests.get(
                EUROPEPMC, params=params, headers=config.HTTP_HEADERS,
                timeout=config.HTTP_TIMEOUT,
            )
            r.raise_for_status()
            results = r.json().get("resultList", {}).get("result", [])
        except Exception:
            continue
        for rec in results:
            if not _is_preprint_record(rec):
                continue
            urls.extend(_fulltext_pdf_urls(rec))
    return _dedupe(urls)


def _is_preprint_record(rec):
    if not isinstance(rec, dict):
        return False
    if (rec.get("source") or "").upper() == "PPR":
        return True
    if str(rec.get("id") or "").upper().startswith("PPR"):
        return True
    pub_type_list = rec.get("pubTypeList")
    if isinstance(pub_type_list, dict):
        pub_types = pub_type_list.get("pubType", [])
        if isinstance(pub_types, list):
            return any("preprint" in str(t).lower() for t in pub_types)
    return False


def _fulltext_pdf_urls(rec):
    urls = []
    ftl = rec.get("fullTextUrlList") or {}
    if not isinstance(ftl, dict):
        return urls
    for item in ftl.get("fullTextUrl", []) or []:
        if not isinstance(item, dict):
            continue
        url = item.get("url") or ""
        style = (item.get("documentStyle") or "").lower()
        if url and ("pdf" in style or _looks_like_pdf(url)):
            urls.append(url)
    return urls


def _crossref_preprint_pdf_urls(doi):
    if not doi:
        return []
    try:
        r = requests.get(
            f"{CROSSREF}/{doi}", params={"mailto": config.NCBI_EMAIL},
            headers=config.HTTP_HEADERS, timeout=config.HTTP_TIMEOUT,
        )
        r.raise_for_status()
        message = r.json().get("message", {})
    except Exception:
        return []

    urls = []
    relation = message.get("relation") or {}
    for key in ("has-preprint", "is-preprint-of"):
        for item in relation.get(key) or []:
            if not isinstance(item, dict):
                continue
            related_doi = item.get("id") or ""
            if related_doi:
                urls.extend(_crossref_work_pdf_urls(related_doi))
    return _dedupe(urls)


def _crossref_work_pdf_urls(doi):
    try:
        r = requests.get(
            f"{CROSSREF}/{doi}", params={"mailto": config.NCBI_EMAIL},
            headers=config.HTTP_HEADERS, timeout=config.HTTP_TIMEOUT,
        )
        r.raise_for_status()
        message = r.json().get("message", {})
    except Exception:
        return []

    urls = []
    for link in message.get("link") or []:
        if not isinstance(link, dict):
            continue
        url = link.get("URL") or ""
        ctype = (link.get("content-type") or "").lower()
        if url and ("pdf" in ctype or _looks_like_pdf(url)):
            urls.append(url)
    return urls


def _biorxiv_preprint_pdf_urls(doi):
    if not doi:
        return []
    out = []
    for server, base in (("biorxiv", BIORXIV), ("medrxiv", MEDRXIV)):
        try:
            r = requests.get(
                f"{base}/{doi}", headers=config.HTTP_HEADERS,
                timeout=config.HTTP_TIMEOUT,
            )
            r.raise_for_status()
            data = r.json()
        except Exception:
            continue
        for rec in data.get("collection") or []:
            if not isinstance(rec, dict):
                continue
            rec_doi = rec.get("doi") or ""
            if not rec_doi:
                continue
            version = rec.get("version") or "1"
            rec_server = (rec.get("server") or server).lower()
            host = "www.medrxiv.org" if rec_server == "medrxiv" else "www.biorxiv.org"
            out.append((f"https://{host}/content/{rec_doi}v{version}.full.pdf", rec_server))
    return out


def _dedupe(items):
    seen, out = set(), []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out
