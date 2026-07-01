"""ADS (NASA Astrophysics Data System) tools: search and citation network.

Mirrors the structure of ``discovery.py`` (OpenAlex) but targets the ADS API.
Reuses ``_doi_in_library`` and ``_render_related`` from discovery for
consistency, adding a ``_bibcode_in_library`` check for papers without DOIs.
"""

from __future__ import annotations

import re
from typing import Literal

from zotero_mcp import ads_client
from zotero_mcp import client as _client
from zotero_mcp._app import mcp
from zotero_mcp._context import Context
from zotero_mcp.client import with_zotero_api_lock
from zotero_mcp.tools import _helpers
from zotero_mcp.tools.discovery import _doi_in_library, _render_related

_ITEM_KEY_RE = re.compile(r"^[A-Z0-9]{8}$")


def _bibcode_in_library(zot, bibcode: str) -> bool:
    """Best-effort check: is a paper with this bibcode already in Zotero?

    bibcode is not a native Zotero field; ``add_by_bibcode`` writes it into the
    item's ``extra`` as ``bibcode: <value>``. We search the library for the
    bibcode substring and confirm via the extra field. Returns False on any
    error (treat as "not in library").
    """
    if not bibcode:
        return False
    try:
        zot.add_parameters(q=bibcode, qmode="everything", itemType="-attachment", limit=5)
        results = zot.items()
    except Exception:
        try:
            results = zot.items(q=bibcode, qmode="everything", itemType="-attachment", limit=5)
        except Exception:
            return False
    needle = f"bibcode: {bibcode}".lower()
    for item in results or []:
        extra = str(item.get("data", {}).get("extra", "")).lower()
        if needle in extra:
            return True
    return False


def _in_library(zot, doc: dict) -> bool:
    """Combined membership check: DOI OR bibcode match."""
    doi = ""
    raw_doi = doc.get("doi")
    if isinstance(raw_doi, list) and raw_doi:
        doi = raw_doi[0]
    elif isinstance(raw_doi, str):
        doi = raw_doi
    if doi:
        norm = _helpers._normalize_doi(doi)
        if norm and _doi_in_library(zot, norm):
            return True
    bibcode = doc.get("bibcode")
    if bibcode and _bibcode_in_library(zot, bibcode):
        return True
    return False


def _ads_doc_summary(doc: dict) -> dict:
    """Compact summary of an ADS doc for rendering (mirrors _work_summary)."""
    title = doc.get("title")
    if isinstance(title, list):
        title = title[0] if title else "Untitled"
    elif not isinstance(title, str):
        title = "Untitled"

    year = doc.get("year") or "n.d."

    authors = []
    raw_authors = doc.get("author") or []
    if isinstance(raw_authors, list):
        for a in raw_authors[:3]:
            if a:
                authors.append(a)
        if len(raw_authors) > 3:
            authors.append("et al.")

    doi = ""
    raw_doi = doc.get("doi")
    if isinstance(raw_doi, list) and raw_doi:
        doi = raw_doi[0]
    elif isinstance(raw_doi, str):
        doi = raw_doi

    cited_by = doc.get("citation_count", 0) or 0
    bibcode = doc.get("bibcode", "")

    return {
        "title": title,
        "year": year,
        "doi": doi,
        "cited_by": cited_by,
        "authors": authors,
        "bibcode": bibcode,
    }


def _resolve_bibcode(identifier: str, zot) -> str | None:
    """Resolve an identifier (Zotero item key or bibcode) to a normalized bibcode."""
    ident = (identifier or "").strip()
    if not ident:
        return None
    if _ITEM_KEY_RE.match(ident):
        # Zotero item key → look up bibcode from the item's extra field.
        try:
            item = zot.item(ident)
        except Exception:
            return None
        extra = (item or {}).get("data", {}).get("extra", "") or ""
        for line in extra.splitlines():
            line = line.strip()
            if line.lower().startswith("bibcode:"):
                bc = line.split(":", 1)[1].strip()
                if bc:
                    return bc
        return None
    return ads_client.normalize_bibcode(ident)


@mcp.tool(
    name="zotero_search_ads",
    description="Search the NASA ADS (Astrophysics Data System) for papers by "
    "query, returning results with bibcodes and noting which are already in "
    "your Zotero library. Requires ADS_API_TOKEN (free at "
    "https://ui.adsabs.harvard.edu/#user/settings/token). Use fielded queries "
    "like 'title:exoplanets' or 'author:\"Hubble, E\"'. To import a result, "
    "use zotero_add_by_bibcode with its bibcode.",
)
@with_zotero_api_lock
def search_ads(
    query: str,
    fq: str | None = None,
    rows: int = 10,
    sort: str | None = None,
    *,
    ctx: Context,
) -> str:
    """Search ADS and tag results already in the Zotero library.

    Args:
        query: ADS query string (supports fielded syntax like title:exoplanets).
        fq: optional filter query (e.g. property: refereed, database:astronomy).
        rows: max results (default 10, capped at 50).
        sort: sort field, e.g. "citation_count desc" or "date desc".
        ctx: MCP context.
    """
    try:
        if not query or not query.strip():
            return "Error: query cannot be empty."

        if not ads_client.is_available():
            return (
                "Error: ADS_API_TOKEN is not set. Get a free token at "
                "https://ui.adsabs.harvard.edu/#user/settings/token and set "
                "the ADS_API_TOKEN environment variable."
            )

        limit = _helpers._normalize_limit(rows, default=10, max_val=50)
        ctx.info(f"Searching ADS for: {query}")

        docs = ads_client.search(query, fq=fq, rows=limit, sort=sort)
        if not docs:
            if ads_client.last_error == "auth":
                return (
                    "Error: your ADS_API_TOKEN was rejected (invalid or expired). "
                    "Get a new free token at "
                    "https://ui.adsabs.harvard.edu/#user/settings/token and "
                    "update the ADS_API_TOKEN environment variable."
                )
            return f"No ADS results found for: {query}"

        zot = _client.get_zotero_client()
        # Single pass: build summaries (with in-library status) once and reuse
        # for both the count and the render, avoiding a 2x _in_library call.
        summaries = []
        in_library_count = 0
        for doc in docs:
            summary = _ads_doc_summary(doc)
            summary["in_library"] = _in_library(zot, doc)
            if summary["in_library"]:
                in_library_count += 1
            summaries.append(summary)

        lines = [
            f"# ADS Search: {query}",
            f"**Results:** {len(docs)} | **Already in library:** {in_library_count}",
            "",
        ]
        for i, summary in enumerate(summaries, 1):
            marker = "in library ✓" if summary["in_library"] else "not in library"
            authors = ", ".join(summary["authors"]) if summary["authors"] else "Unknown"
            lines.append(f"{i}. **{summary['title']}** ({summary['year']})")
            lines.append(f"   - Bibcode: `{summary['bibcode']}`")
            lines.append(f"   - Authors: {authors}")
            if summary["doi"]:
                lines.append(f"   - DOI: {summary['doi']}")
            lines.append(f"   - Cited by: {summary['cited_by']}")
            lines.append(f"   - {marker}")
            lines.append("")

        if in_library_count < len(docs):
            lines.append(
                "_To import a paper, use zotero_add_by_bibcode with its bibcode._"
            )
        return "\n".join(lines)

    except Exception as e:
        ctx.error(f"Error searching ADS: {e}")
        return f"Error searching ADS: {e}"


@mcp.tool(
    name="zotero_ads_citation_network",
    description="Explore the ADS citation graph of a paper: its references "
    "(papers it cites) or citations (papers citing it). Identifies which "
    "related papers are already in your Zotero library. Provide a bibcode or "
    "a Zotero item key (must have a bibcode in its extra field). Requires "
    "ADS_API_TOKEN.",
)
@with_zotero_api_lock
def ads_citation_network(
    identifier: str,
    direction: Literal["references", "citations", "both"] = "both",
    limit: int = 20,
    *,
    ctx: Context,
) -> str:
    """Fetch the ADS citation network and tag papers already in Zotero.

    Args:
        identifier: a bibcode (e.g. 2003ApJ...589L..21B) or Zotero item key.
        direction: references (outgoing), citations (incoming), or both.
        limit: max papers per direction (default 20, capped at 50).
        ctx: MCP context.
    """
    try:
        if not identifier or not identifier.strip():
            return "Error: identifier cannot be empty."

        if not ads_client.is_available():
            return (
                "Error: ADS_API_TOKEN is not set. Get a free token at "
                "https://ui.adsabs.harvard.edu/#user/settings/token and set "
                "the ADS_API_TOKEN environment variable."
            )

        zot = _client.get_zotero_client()
        bibcode = _resolve_bibcode(identifier, zot)
        if not bibcode:
            return (
                f"Error: could not resolve a valid bibcode from '{identifier}'. "
                "Provide a bibcode (e.g. 2003ApJ...589L..21B) or a Zotero item "
                "key whose extra field contains 'bibcode: ...'."
            )

        cap = _helpers._normalize_limit(limit, default=20, max_val=50)
        ctx.info(f"Fetching ADS citation network for {bibcode} ({direction})")

        refs: list[dict] = []
        cits: list[dict] = []
        if direction in ("references", "both"):
            refs = ads_client.get_references(bibcode, rows=cap)
        if direction in ("citations", "both"):
            cits = ads_client.get_citations(bibcode, rows=cap)

        if not refs and not cits and ads_client.last_error == "auth":
            return (
                "Error: your ADS_API_TOKEN was rejected (invalid or expired). "
                "Get a new free token at "
                "https://ui.adsabs.harvard.edu/#user/settings/token and "
                "update the ADS_API_TOKEN environment variable."
            )

        # Tag in-library status.
        ref_summaries = []
        cit_summaries = []
        in_lib_refs = 0
        in_lib_cits = 0
        for doc in refs:
            s = _ads_doc_summary(doc)
            s["in_library"] = _in_library(zot, doc)
            if s["in_library"]:
                in_lib_refs += 1
            ref_summaries.append(s)
        for doc in cits:
            s = _ads_doc_summary(doc)
            s["in_library"] = _in_library(zot, doc)
            if s["in_library"]:
                in_lib_cits += 1
            cit_summaries.append(s)

        total_in_lib = in_lib_refs + in_lib_cits
        lines = [
            f"# ADS Citation Network: {bibcode}",
            f"**References:** {len(refs)} ({in_lib_refs} in library) | "
            f"**Citations:** {len(cits)} ({in_lib_cits} in library) | "
            f"**Total in library:** {total_in_lib}",
            "",
        ]
        if direction in ("references", "both"):
            lines += _render_related(ref_summaries, "References")
        if direction in ("citations", "both"):
            lines += _render_related(cit_summaries, "Citations")

        if total_in_lib < (len(refs) + len(cits)):
            lines.append(
                "_To import a missing paper, use zotero_add_by_bibcode with its bibcode._"
            )
        return "\n".join(lines)

    except Exception as e:
        ctx.error(f"Error fetching ADS citation network: {e}")
        return f"Error fetching ADS citation network: {e}"


@mcp.tool(
    name="zotero_export_ads",
    description=(
        "Export one or more papers from NASA ADS in a citation format ready "
        "to paste into LaTeX or a .bib file. Uses the ADS /export API, which "
        "returns the official, publisher-accurate citation text — more "
        "precise than local BibTeX generation. "
        "Accepts either ADS bibcodes (e.g. '2024ApJ...968L..12A') or Zotero "
        "item keys (8-char, bibcode resolved from the item's extra field). "
        "Pass a single value or a comma-separated/JSON list for batch export. "
        "format: 'bibtex' (default, for .bib files), 'aastex' (AASTeX "
        "\\bibitem), 'mnras' (MNRAS style), 'ris', 'endnote', or 'ads'. "
        "sort: optional, e.g. 'date desc' or 'first_author asc'. "
        "Requires an ADS API token. "
        "Example: zotero_export_ads(bibcodes='2024ApJ...968L..12A', "
        "format='bibtex') → returns @article{...} BibTeX entry."
    )
)
@with_zotero_api_lock
def export_ads(
    bibcodes: str | list[str],
    format: Literal[
        "bibtex", "bibtexabs", "aastex", "mnras", "icarus", "soph",
        "ris", "endnote", "ads", "procite", "refworks", "votable",
    ] = "bibtex",
    sort: str | None = None,
    *,
    ctx: Context
) -> str:
    """Export papers from ADS in a citation format (BibTeX, AASTeX, etc.)."""
    try:
        if not ads_client.is_available():
            return (
                "Error: ADS API token is not configured. "
                "Run 'zotero-mcp setup' to add it, or set ADS_API_TOKEN."
            )

        # Normalize input to a list of strings.
        raw_list = _helpers._normalize_str_list_input(bibcodes, "bibcodes")
        if not raw_list:
            return "Error: at least one bibcode or Zotero item key is required."

        # Resolve each: could be a bibcode or a Zotero item key.
        # Lazily create the Zotero client only if we encounter an item key
        # (8-char [A-Z0-9]); pure-bibcode inputs don't need Zotero access.
        read_zot = None
        resolved: list[str] = []
        unresolved: list[str] = []
        for ident in raw_list:
            ident = ident.strip()
            if not ident:
                continue
            # Try direct bibcode first — avoids touching Zotero for the
            # common case where the user passes raw bibcodes.
            direct_bc = ads_client.normalize_bibcode(ident)
            if direct_bc:
                resolved.append(direct_bc)
                continue
            # Looks like a Zotero item key — resolve bibcode from extra.
            if _ITEM_KEY_RE.match(ident):
                if read_zot is None:
                    try:
                        read_zot = _client.get_zotero_client()
                    except Exception:
                        read_zot = None
                if read_zot is not None:
                    bc = _resolve_bibcode(ident, read_zot)
                    if bc:
                        resolved.append(bc)
                        continue
            unresolved.append(ident)

        if not resolved:
            return (
                f"Error: could not resolve any valid bibcodes from {raw_list}. "
                "Pass ADS bibcodes (e.g. '2024ApJ...968L..12A') or Zotero item "
                "keys whose extra field contains 'bibcode: ...'."
            )

        ctx.info(f"Exporting {len(resolved)} bibcode(s) as {format} from ADS...")

        result = ads_client.export(resolved, fmt=format, sort=sort)
        if not result:
            return (
                f"Error: ADS export returned no data for {len(resolved)} "
                f"bibcode(s) in '{format}' format. Check that the bibcodes "
                "are valid and the format is supported."
            )

        # Build a clear, copy-pasteable response.
        lines = [
            f"# ADS Export — {format.upper()}",
            "",
            f"**{len(resolved)} paper(s)** exported from ADS:",
        ]
        for bc in resolved:
            lines.append(f"- `{bc}`")
        if unresolved:
            lines.append("")
            lines.append(f"**Unresolved ({len(unresolved)}):** {', '.join(unresolved)}")
        lines.append("")
        lines.append(f"```{_codeblock_lang(format)}")
        lines.append(result.rstrip())
        lines.append("```")
        return "\n".join(lines)

    except Exception as e:
        ctx.error(f"Error exporting from ADS: {e}")
        return f"Error exporting from ADS: {e}"


def _codeblock_lang(fmt: str) -> str:
    """Pick a syntax-highlight language for the code fence."""
    if fmt.startswith("bibtex"):
        return "bibtex"
    if fmt in ("aastex", "mnras", "icarus", "soph", "ads"):
        return "latex"
    return ""
