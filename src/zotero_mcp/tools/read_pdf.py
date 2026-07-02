"""Tool for reading specific page ranges from PDF attachments."""

import json
import os
import tempfile
from pathlib import Path

from fastmcp import Context

from zotero_mcp import client as _client
from zotero_mcp import mineru_client
from zotero_mcp import utils as _utils
from zotero_mcp._app import mcp
from zotero_mcp.tools import _helpers


def _cleanup_path(file_path: str) -> None:
    """Remove a downloaded PDF and its parent temp directory."""
    try:
        parent = os.path.dirname(file_path)
        if os.path.exists(parent) and parent.startswith(tempfile.gettempdir()):
            import shutil

            shutil.rmtree(parent, ignore_errors=True)
    except Exception:
        pass


def _continue_reading_footer(item_key: str, actual_end: int, total_pages: int) -> str:
    """A tail prompt nudging the agent to read remaining pages.

    Returns an empty string when ``actual_end`` already covers the whole PDF,
    so callers can unconditionally append it.
    """
    if actual_end >= total_pages:
        return ""
    nxt = actual_end + 1
    end = min(actual_end + 50, total_pages)
    return (
        f"\n---\n**Read pages 1-{actual_end} of {total_pages}. "
        f"Pages {nxt}-{total_pages} not yet read.** "
        f"To continue: `zotero_read_pdf_pages(item_key='{item_key}', "
        f"start_page={nxt}, end_page={end})`."
    )


def _get_pdf_path(item_key: str, ctx: Context) -> tuple[str, str, str | None] | None:
    """Download a PDF attachment and return (file_path, title, attachment_key).

    Tries local storage first (via LocalZoteroReader), then downloads via API.
    Returns None if no PDF attachment is found. The ``attachment_key`` is the
    Zotero attachment item key used as the MinerU cache key (None when only the
    parent item is known). The caller is responsible for cleaning up file_path.
    """
    zot = _client.get_zotero_client()
    item = zot.item(item_key)

    # Try local storage first (persists on disk — no cleanup needed)
    try:
        from zotero_mcp.local_db import LocalZoteroReader

        if _utils.is_local_mode():
            config_path = Path.home() / ".config" / "zotero-mcp" / "config.json"
            zotero_db_path = None
            if config_path.exists():
                try:
                    with open(config_path, encoding="utf-8") as _f:
                        _cfg = json.load(_f)
                        zotero_db_path = _cfg.get("semantic_search", {}).get("zotero_db_path")
                except Exception:
                    pass
            with LocalZoteroReader(db_path=zotero_db_path) as reader:
                local_item = reader.get_item_by_key(item_key)
                if local_item:
                    for att_key, path, ctype in reader._iter_parent_attachments(local_item.item_id):
                        if ctype == "application/pdf":
                            resolved = reader._resolve_attachment_path(att_key, path or "")
                            if resolved and resolved.exists():
                                return str(resolved), local_item.title or item_key, att_key
    except Exception:
        pass

    # Fallback: resolve via the multi-source downloader (local -> WebDAV ->
    # Zotero cloud) so WebDAV-backed attachments work, not just cloud storage.
    attachment = _client.get_attachment_details(zot, item)
    if not attachment:
        return None

    pdf_extensions = {".pdf", ".PDF"}
    filename = attachment.filename or f"{attachment.key}.pdf"
    if not any(filename.endswith(ext) for ext in pdf_extensions):
        content_type = attachment.content_type or ""
        if "pdf" not in content_type.lower():
            return None

    tmpdir = tempfile.mkdtemp(prefix="zotero_pdf_")
    probe = os.path.join(tmpdir, os.path.basename(filename))
    try:
        download = _client.download_attachment_file(
            attachment.key,
            tmpdir,
            os.path.basename(filename),
            local_client=_client.get_local_zotero_client(),
            web_client=None if _utils.is_local_mode() else zot,
        )
    except Exception:
        _cleanup_path(probe)
        raise

    if download.path and download.path.exists() and download.path.stat().st_size > 0:
        return str(download.path), attachment.title, attachment.key

    _cleanup_path(probe)
    return None


@mcp.tool(
    name="zotero_read_pdf_pages",
    description="Read specific page range(s) from a PDF attachment of a Zotero item. "
    "Use this when you know which pages to read — for example after getting the PDF "
    "outline via zotero_get_pdf_outline. Pages are 1-indexed. Max 50 pages per call. "
    "IMPORTANT for full reading: a single call returns at most 50 pages, but many "
    "papers have 20-40+ pages. To read a paper IN FULL, call this tool repeatedly "
    "with consecutive ranges (e.g. 1-50, 51-100) until the returned total page "
    "count is covered — do NOT stop after the first call unless you have read the "
    "last page. The output header shows 'Total pages in PDF: N'; if your current "
    "end_page < N, pages remain unread. MinerU caches the whole PDF after the "
    "first call, so later ranges return instantly. "
    "When MinerU is configured (see ``mineru`` block in config.json), the tool returns "
    "structured Markdown with formulas as LaTeX and tables as HTML — far more accurate "
    "than plain text extraction for papers. Otherwise falls back to PyMuPDF text layer. "
    "Requires PyMuPDF: pip install zotero-mcp-server[pdf]",
)
def read_pdf_pages(
    item_key: str,
    start_page: int,
    end_page: int | None = None,
    *,
    ctx: Context,
) -> str:
    """Extract and return text from a specific page range of a PDF.

    Args:
        item_key: Zotero item key/ID of the paper or its PDF attachment.
        start_page: First page to read (1-indexed).
        end_page: Last page to read (1-indexed). If omitted, reads only start_page.
        ctx: MCP context.

    Returns:
        Markdown-formatted page content with metadata header.
    """
    try:
        if not item_key or not item_key.strip():
            return "Error: item_key cannot be empty."

        if end_page is not None and end_page < start_page:
            return "Error: end_page must be greater than or equal to start_page."

        ctx.info(f"Reading PDF pages {start_page}-{end_page or start_page} for item {item_key}")

        result = _get_pdf_path(item_key, ctx)
        if result is None:
            return f"No PDF attachment found for item: {item_key}"

        pdf_path, title, attachment_key = result

        # Determine total page count via PyMuPDF (lightweight, needed for range
        # validation regardless of which extractor runs). If PyMuPDF is missing,
        # we cannot safely validate ranges — but MinerU may still work, so only
        # block when MinerU is also unavailable.
        total_pages = _probe_total_pages(pdf_path)
        if total_pages is None:
            # PyMuPDF unavailable. MinerU may still be usable, but we lose
            # range validation. Surface a clear error to avoid unsafe slicing.
            return (
                "PyMuPDF is required for PDF page reading (to validate page ranges). "
                "Install it with: pip install zotero-mcp-server[pdf]"
            )

        actual_end = end_page if end_page is not None else start_page
        if start_page < 1 or start_page > total_pages:
            _cleanup_path(pdf_path)
            return f"Start page {start_page} is out of range. PDF has {total_pages} pages (1-{total_pages})."
        if end_page is not None and end_page > total_pages:
            _cleanup_path(pdf_path)
            return f"End page {end_page} is out of range. PDF has {total_pages} pages (1-{total_pages})."

        page_count = actual_end - start_page + 1
        if page_count > 50:
            _cleanup_path(pdf_path)
            return f"Requested {page_count} pages (max 50). Please narrow your page range."

        # --- MinerU preferred path (structured: formulas as LaTeX, tables as HTML) ---
        mineru_output = _try_mineru(attachment_key, pdf_path, start_page, actual_end, total_pages, title, item_key, ctx)
        if mineru_output is not None:
            _cleanup_path(pdf_path)
            return mineru_output

        # --- Fallback: PyMuPDF text-layer extraction ---
        return _extract_with_pymupdf(pdf_path, title, item_key, start_page, actual_end, total_pages)

    except Exception as e:
        ctx.error(f"Error reading PDF pages: {str(e)}")
        return f"Error reading PDF pages: {str(e)}"


def _probe_total_pages(pdf_path: str) -> int | None:
    """Return PDF page count, or None if PyMuPDF is unavailable."""
    try:
        import fitz
    except ImportError:
        return None
    doc = fitz.open(pdf_path)
    try:
        return len(doc)
    finally:
        doc.close()


def _try_mineru(
    attachment_key: str | None,
    pdf_path: str,
    start_page: int,
    actual_end: int,
    total_pages: int,
    title: str,
    item_key: str,
    ctx: Context,
) -> str | None:
    """Attempt MinerU structured extraction. Returns Markdown str, or None to fall back.

    None is returned when MinerU is disabled, unavailable, or fails — the caller
    then falls back to PyMuPDF. This guarantees reading always works.
    """
    config = mineru_client.load_mineru_config()
    if not mineru_client.is_mineru_enabled(config):
        return None
    if not mineru_client.is_mineru_available(config):
        ctx.warning("MinerU is enabled but unavailable (no CLI/API configured); using PyMuPDF fallback.")
        return None
    if not attachment_key:
        # Without an attachment key we cannot cache; skip MinerU rather than
        # parse uncached on every call (MinerU is too slow for that).
        ctx.warning("MinerU enabled but attachment key unknown; using PyMuPDF fallback.")
        return None

    ctx.info("Extracting with MinerU (structured: formulas + tables)...")
    try:
        parsed = mineru_client.read_cached_or_parse(attachment_key, Path(pdf_path), config)
    except Exception as e:
        ctx.warning(f"MinerU parse raised an error; using PyMuPDF fallback: {e}")
        return None
    if parsed is None:
        ctx.warning("MinerU parse returned no result; using PyMuPDF fallback.")
        return None

    # Slice the requested page range from the per-page split.
    pages = parsed.pages
    zstart = start_page - 1
    zend = actual_end - 1
    # If MinerU returned a single whole-document page (page split failed),
    # deliver the whole document but flag the caveat.
    single_page_caveat = len(pages) == 1 and total_pages > 1

    output = [
        f"# PDF Pages {start_page}-{actual_end} of {title}",
        f"**Item Key:** {item_key}",
        f"**Total pages in PDF:** {total_pages}",
        f"**Extraction:** MinerU ({parsed.source.replace('mineru:', '')})",
    ]
    if single_page_caveat:
        output.append(
            f"**Note:** page-level split unavailable; showing full document for pages {start_page}-{actual_end}."
        )
    # When MinerU just parsed the full document (not a cache hit), the
    # per-page cache is now available for the semantic-search build path.
    # Nudge the agent to build a complete, page-aware vector index.
    if parsed.source != "mineru:cached":
        output.append(
            f"**MinerU cache created.** To make this document fully "
            f"searchable in the semantic index (with page numbers), "
            f"call: `zotero_update_search_database(reindex_keys=['{item_key}'])`"
        )
    output.append("")

    if single_page_caveat:
        output.append(pages[0].strip() or "*[No extractable text]*")
    else:
        for page_num in range(zstart, zend + 1):
            page_text = pages[page_num] if page_num < len(pages) else ""
            output.append(f"## Page {page_num + 1}")
            output.append("")
            output.append(page_text.strip() if page_text.strip() else "*[No extractable text on this page]*")
            output.append("")

    return _helpers._prepend_size_warning(
        "\n".join(output) + _continue_reading_footer(item_key, actual_end, total_pages),
        "MinerU output preserves formula LaTeX and table HTML — richer than plain text.",
    )


def _extract_with_pymupdf(
    pdf_path: str, title: str, item_key: str, start_page: int, actual_end: int, total_pages: int
) -> str:
    """Fallback path: PyMuPDF text-layer extraction (current behavior)."""
    import fitz

    doc = fitz.open(pdf_path)
    try:
        zstart = start_page - 1
        zend = actual_end - 1
        output = [
            f"# PDF Pages {start_page}-{actual_end} of {title}",
            f"**Item Key:** {item_key}",
            f"**Total pages in PDF:** {total_pages}",
            "**Extraction:** PyMuPDF (fallback)",
            "",
        ]
        for page_num in range(zstart, zend + 1):
            page = doc[page_num]
            text = page.get_text()
            output.append(f"## Page {page_num + 1}")
            output.append("")
            if text.strip():
                output.append(text.strip())
            else:
                output.append("*[No extractable text on this page]*")
            output.append("")

        return _helpers._prepend_size_warning(
            "\n".join(output) + _continue_reading_footer(item_key, actual_end, total_pages),
            "Consider using zotero_semantic_search to find specific content instead of reading full pages.",
        )
    finally:
        doc.close()
        _cleanup_path(pdf_path)
