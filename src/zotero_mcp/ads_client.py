"""NASA ADS (Astrophysics Data System) API client.

Thin ``requests``-based wrapper around the ADS REST API
(https://api.adsabs.harvard.edu/v1). Used by the ``zotero_add_by_bibcode``,
``zotero_search_ads`` and ``zotero_ads_citation_network`` tools.

Authentication: a free token (https://ui.adsabs.harvard.edu/#user/settings/token)
passed via the ``ADS_API_TOKEN`` environment variable, sent as
``Authorization: Bearer <token>``. No official ``ads`` Python library is used —
the API is plain REST and adding the library would pull in extra dependencies.

All public functions return ``None`` / ``[]`` on failure rather than raising,
so tool callers can degrade gracefully (e.g. fall back to other metadata
sources or report a clear error string).
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any

logger = logging.getLogger(__name__)

ADS_API_BASE = "https://api.adsabs.harvard.edu/v1"
ADS_LINK_BASE = "https://ui.adsabs.harvard.edu/link_gateway"

# Last error category from the most recent request, for upstream tool callers
# to surface a clearer message than a generic "not found". Reset on each call.
# One of: None, "auth" (invalid/expired token), "unavailable" (network/5xx).
last_error: str | None = None

# Fields requested for a full record (used by fetch_record and citation graph).
_FULL_FIELDS = (
    "bibcode,title,author,first_author,year,doi,abstract,pub,bibstem,"
    "pubdate,volume,issue,page,doctype,property,esources,citation_count"
)
# Lighter field set for search/citation summaries.
_SEARCH_FIELDS = "bibcode,title,author,first_author,year,doi,pub,citation_count,esources"

# Rate-limit / retry tuning.
_MAX_RETRIES = 3
_RETRY_STATUSES = {429, 503}

# bibcode: 19-char fixed-width identifier YYYYJJJJJVVVVMPPPPA (dots pad gaps).
# Accept the canonical form; we don't fully validate the internal structure
# (ADS is the source of truth), but require the year prefix and rough length.
_BIBCODE_RE = re.compile(r"^\d{4}[A-Za-z0-9&.]{15}$")


# --------------------------------------------------------------------------- #
# Token & availability
# --------------------------------------------------------------------------- #
def get_ads_token() -> str | None:
    """Return the configured ADS API token, or None if unset."""
    return os.environ.get("ADS_API_TOKEN") or None


def is_available() -> bool:
    """True iff an ADS token is configured (liveness is probed lazily)."""
    return bool(get_ads_token())


# --------------------------------------------------------------------------- #
# bibcode normalization
# --------------------------------------------------------------------------- #
def normalize_bibcode(raw: str) -> str | None:
    """Normalize and lightly validate a bibcode.

    bibcodes are 19-char identifiers like ``2003ApJ...589L..21B``. We accept
    the canonical form (4-digit year + 15 chars) after stripping whitespace and
    common wrapping characters. Returns None if the input is not bibcode-shaped.
    """
    if not raw:
        return None
    cleaned = raw.strip().strip("[]\"'").strip()
    # Some callers paste a URL; extract the trailing path segment.
    if "/" in cleaned:
        cleaned = cleaned.rsplit("/", 1)[-1]
    if _BIBCODE_RE.match(cleaned):
        return cleaned
    return None


# --------------------------------------------------------------------------- #
# Core request with rate-limit retry
# --------------------------------------------------------------------------- #
def _ads_request(
    path: str,
    params: dict[str, Any] | None = None,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> dict | None:
    """Perform an authenticated ADS API request with retry on rate-limiting.

    Returns the parsed JSON dict, or None on any failure.
    """
    import requests

    global last_error
    last_error = None
    token = get_ads_token()
    if not token:
        last_error = "auth"
        logger.warning("ADS_API_TOKEN not set; cannot query ADS.")
        return None

    headers = {"Authorization": f"Bearer {token}"}
    url = f"{ADS_API_BASE}{path}"
    if method == "POST":
        headers["Content-Type"] = "application/json"

    last_err: str | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            resp = requests.request(method, url, headers=headers, params=params, json=body, timeout=timeout)
        except Exception as e:
            last_err = f"request error: {e}"
            time.sleep(2**attempt)
            continue

        if resp.status_code == 200:
            try:
                return resp.json()
            except Exception as e:
                logger.warning(f"ADS returned non-JSON: {e}")
                return None

        # Authentication failure: invalid or expired token. Distinguish from a
        # genuine "not found" so upstream tools can tell the user to fix the
        # token instead of reporting a misleading empty result.
        if resp.status_code in (401, 403):
            last_error = "auth"
            logger.warning(
                f"ADS {method} {path} rejected token (HTTP {resp.status_code}). "
                f"The ADS_API_TOKEN is invalid or expired — get a new one at "
                f"https://ui.adsabs.harvard.edu/#user/settings/token"
            )
            return None

        if resp.status_code in _RETRY_STATUSES:
            # Respect rate-limit reset if advertised, else exponential backoff.
            reset = resp.headers.get("X-RateLimit-Reset")
            wait = 2**attempt
            if reset:
                try:
                    wait = max(wait, min(float(reset) - time.time(), 60.0))
                except (TypeError, ValueError):
                    pass
            last_err = f"HTTP {resp.status_code} (rate-limited), retrying"
            time.sleep(max(wait, 1.0))
            continue

        # Non-retryable error.
        last_error = "unavailable"
        logger.warning(f"ADS {method} {path} failed: HTTP {resp.status_code}: {resp.text[:200]}")
        return None

    last_error = "unavailable"
    logger.warning(f"ADS {method} {path} exhausted retries: {last_err}")
    return None


# --------------------------------------------------------------------------- #
# Search / record / citation graph
# --------------------------------------------------------------------------- #
def search(
    query: str,
    *,
    fl: str = _SEARCH_FIELDS,
    fq: str | None = None,
    rows: int = 10,
    sort: str | None = None,
    start: int = 0,
) -> list[dict]:
    """Run an ADS search query; return response.docs[] (empty list on failure)."""
    params: dict[str, Any] = {"q": query, "fl": fl, "rows": rows, "start": start}
    if fq:
        params["fq"] = fq
    if sort:
        params["sort"] = sort
    data = _ads_request("/search/query", params=params)
    if not data:
        return []
    docs = (data.get("response") or {}).get("docs") or []
    return docs if isinstance(docs, list) else []


def fetch_record(bibcode: str) -> dict | None:
    """Fetch a single record by bibcode (full field set)."""
    docs = search(f"bibcode:{bibcode}", fl=_FULL_FIELDS, rows=1)
    return docs[0] if docs else None


def get_references(bibcode: str, rows: int = 50) -> list[dict]:
    """Papers referenced by ``bibcode`` (outgoing citations)."""
    return search(f"references({bibcode})", fl=_SEARCH_FIELDS, rows=rows, sort="date desc")


def get_citations(bibcode: str, rows: int = 50) -> list[dict]:
    """Papers citing ``bibcode`` (incoming citations)."""
    return search(f"citations({bibcode})", fl=_SEARCH_FIELDS, rows=rows, sort="date desc")


# --------------------------------------------------------------------------- #
# PDF link resolution
# --------------------------------------------------------------------------- #
def get_pdf_url(bibcode: str, prefer: str = "eprint") -> str | None:
    """Return a link_gateway URL for the best available PDF, or None.

    Checks the record's ``esources`` to decide between EPRINT_PDF (arXiv
    preprint, usually OA) and PUB_PDF (publisher version, often paywalled).
    Does NOT download — the caller passes the URL to
    ``_helpers._download_and_attach_pdf`` which applies SSRF guards.

    Args:
        bibcode: normalized bibcode.
        prefer: "eprint" (default, prefer arXiv OA) or "pub" (prefer publisher).
    """
    record = fetch_record(bibcode)
    if not record:
        return None
    esources = record.get("esources") or []
    if not isinstance(esources, list):
        esources = []

    eprint_available = "EPRINT_PDF" in esources
    pub_available = "PUB_PDF" in esources

    if prefer == "pub":
        order = [("PUB_PDF", pub_available), ("EPRINT_PDF", eprint_available)]
    else:
        order = [("EPRINT_PDF", eprint_available), ("PUB_PDF", pub_available)]

    for endpoint, available in order:
        if available:
            return f"{ADS_LINK_BASE}/{bibcode}/{endpoint}"

    # No direct PDF in esources; fall back to eprint gateway anyway (ADS may
    # still resolve an arXiv PDF even when esources doesn't list it).
    if eprint_available or prefer == "eprint":
        return f"{ADS_LINK_BASE}/{bibcode}/EPRINT_PDF"
    return None


# --------------------------------------------------------------------------- #
# Export (citation formats: BibTeX, AASTeX, etc.)
# --------------------------------------------------------------------------- #
# Formats accepted by the ADS /export/<format> endpoint.
SUPPORTED_EXPORT_FORMATS = (
    "bibtex",
    "bibtexabs",
    "aastex",
    "mnras",
    "icarus",
    "soph",
    "ris",
    "endnote",
    "ads",
    "procite",
    "refworks",
    "votable",
)


def export(
    bibcodes: list[str],
    fmt: str = "bibtex",
    sort: str | None = None,
) -> str | None:
    """Export one or more bibcodes to a citation format via the ADS export API.

    Calls ``POST /v1/export/<fmt>`` with a ``{"bibcode": [...]}`` body and
    returns the formatted citation text (the ``export`` field of the
    response). Returns ``None`` on any failure, following the module's
    graceful-degradation convention.

    Args:
        bibcodes: List of 19-char ADS bibcodes to export.
        fmt: Export format — one of :data:`SUPPORTED_EXPORT_FORMATS`.
        sort: Optional sort spec, e.g. ``"date desc"`` or
            ``"first_author asc"``.

    Returns:
        The formatted citation text, or ``None``.
    """
    if not bibcodes:
        return None
    body: dict[str, Any] = {"bibcode": list(bibcodes)}
    if sort:
        body["sort"] = sort
    data = _ads_request(f"/export/{fmt}", method="POST", body=body)
    if not data:
        return None
    return data.get("export")


# --------------------------------------------------------------------------- #
# ADS record → CSL-JSON conversion
# --------------------------------------------------------------------------- #
# ADS doctype → CSL type. Unknown types default to article-journal.
_DOCTYPE_TO_CSL = {
    "article": "article-journal",
    "eprint": "article-journal",
    "inproceedings": "paper-conference",
    "inbook": "chapter",
    "book": "book",
    "phdthesis": "thesis",
    "masterthesis": "thesis",
    "techreport": "report",
    "software": "article",  # no great CSL mapping; treat as article
    "abstract": "article",
    "catalog": "dataset",
    "nonarticle": "article",
}


def _split_author_name(name: str) -> dict:
    """Split an ADS author string ('Last, First M.') into a CSL name object."""
    name = name.strip()
    if not name:
        return {"literal": "Unknown"}
    if "," in name:
        family, given = name.split(",", 1)
        return {"family": family.strip(), "given": given.strip()}
    # No comma — treat the whole string as a literal (collaborations, etc.)
    return {"literal": name}


def doc_to_csl_json(doc: dict) -> dict:
    """Convert an ADS search doc into a CSL-JSON dict.

    The output is consumed by ``citation_import.csl_json_to_zotero``. The
    bibcode is injected into the ``note`` field so that it lands in the Zotero
    item's ``extra`` (via citation_import's note→extra mapping), enabling
    later ``_bibcode_in_library`` dedup.
    """
    csl: dict[str, Any] = {"type": _DOCTYPE_TO_CSL.get(doc.get("doctype", "article"), "article-journal")}

    title = doc.get("title")
    if isinstance(title, list) and title:
        csl["title"] = title[0]
    elif isinstance(title, str):
        csl["title"] = title

    authors = doc.get("author")
    if isinstance(authors, list) and authors:
        csl["author"] = [_split_author_name(a) for a in authors if a]

    year = doc.get("year")
    pubdate = doc.get("pubdate")  # "YYYY-MM-DD" or "YYYY-MM-00"
    # Prefer pubdate (full precision) over year; fall back to year.
    date_str = ""
    if pubdate:
        # ADS uses "YYYY-MM-00" when day is unknown; normalize to "YYYY-MM".
        date_str = str(pubdate).strip()
        # Strip a trailing "-00" day component → "YYYY-MM".
        if date_str.endswith("-00"):
            date_str = date_str[:-3]
    elif year:
        date_str = str(year).strip()
    if date_str:
        try:
            # Parse into date-parts for CSL. Accept "YYYY" or "YYYY-MM".
            parts = [int(p) for p in date_str.split("-") if p]
            csl["issued"] = {"date-parts": [parts]}
        except (TypeError, ValueError):
            pass

    pub = doc.get("pub")
    if pub:
        csl["container-title"] = pub

    # ADS journal abbreviation (e.g. "ApJ", "MNRAS", "A&A").  CSL's
    # container-title-short is the standard place for this; the Zotero
    # import path maps it to the journalAbbreviation field.
    bibstem = doc.get("bibstem")
    if bibstem:
        # ADS sometimes returns bibstem as a list (e.g. ["ApJL", "ApJL.1002"]);
        # the first element is the canonical short form.
        if isinstance(bibstem, list):
            bibstem = bibstem[0] if bibstem else None
        if bibstem:
            csl["container-title-short"] = str(bibstem).strip()

    for src, dst in (("volume", "volume"), ("issue", "issue"), ("page", "page")):
        val = doc.get(src)
        if val:
            csl[dst] = str(val)

    doi = doc.get("doi")
    if isinstance(doi, list) and doi:
        csl["DOI"] = doi[0]
    elif isinstance(doi, str):
        csl["DOI"] = doi

    abstract = doc.get("abstract")
    if abstract:
        csl["abstract"] = abstract

    # Inject bibcode into note → lands in Zotero extra field for dedup.
    bibcode = doc.get("bibcode")
    if bibcode:
        csl["note"] = f"bibcode: {bibcode}"

    return csl
