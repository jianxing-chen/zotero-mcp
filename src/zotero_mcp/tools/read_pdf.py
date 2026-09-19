"""Tool for reading specific page ranges from PDF attachments."""

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from contextlib import contextmanager
from typing import Literal

from fastmcp import Context
from fastmcp.utilities.types import Image
from fastmcp.exceptions import ToolError

from zotero_mcp import client as _client
from zotero_mcp import mineru_client
from zotero_mcp import library as _library
from zotero_mcp import utils as _utils
from zotero_mcp._app import mcp
from zotero_mcp.config import load_config
from zotero_mcp.extract import extract_pdf, pdf_page_count
from zotero_mcp.tools import _helpers

if TYPE_CHECKING:
    from zotero_mcp.batch_runner import TaskStatus

logger = logging.getLogger(__name__)


_TMPDIR_PREFIX = "zotero_pdf_"


class PdfReadError(ToolError):
    """A page read that did not produce pages.

    This tool used to *return* its failures as prose -- "No PDF attachment
    found for item: ...", "Could not read PDF for item ...". A return value is
    indistinguishable from content, so every caller treated a failed read as a
    successful one: ``zotero-cli --json read`` wrapped the message in an
    ``ok: true`` envelope and exited 0, and the MCP tool answered with
    ``isError: false``. A pipeline consuming either could not tell "here are
    the pages" from "there are no pages" without parsing English.

    Raising fixes both surfaces at once, because both already know how to
    report an exception: FastMCP marks the tool result as an error, and
    ``cli_standalone.main`` turns it into an ``ok: false`` envelope with a
    nonzero exit code. Neither needed a change.

    Subclasses ``ToolError`` so FastMCP treats it as a tool error rather than
    an internal crash, and carries ``code`` so the envelope's ``error.code``
    is a stable value a caller can branch on instead of the class name.
    """

    def __init__(self, message: str, code: str = "pdf_read_failed"):
        super().__init__(message)
        self.code = code


def _cleanup_path(file_path: str) -> None:
    """Remove a PDF this module downloaded, along with the directory it made.

    Deletes the file's *parent directory*, so it must only ever be handed a
    path inside a directory this module created with ``mkdtemp``. Two things
    are checked before removing anything, both of which have bitten:

    - The directory's name must carry our ``zotero_pdf_`` prefix. A bare
      "is it under the temp dir" test is not enough: on Linux
      ``gettempdir()`` is ``/tmp``, so a path like ``/tmp/paper.pdf`` has
      ``/tmp`` as its parent and passes that test, and the call then wipes
      the entire system temp directory. macOS hides the bug, because there
      ``gettempdir()`` is under ``/var/folders`` and the prefix never
      matches ``/tmp``.
    - The directory must still be a strict subdirectory of the temp root, so
      the root itself can never be the target.

    A file resolved out of the user's Zotero storage must never be passed
    here: deleting its parent takes the user's own copy of the PDF with it.
    """
    try:
        parent = os.path.dirname(os.path.abspath(file_path))
        temp_root = os.path.abspath(tempfile.gettempdir())
        if not os.path.isdir(parent):
            return
        if os.path.samefile(parent, temp_root):
            return
        if os.path.commonpath([parent, temp_root]) != temp_root:
            return
        if not os.path.basename(parent).startswith(_TMPDIR_PREFIX):
            return
        import shutil

        shutil.rmtree(parent, ignore_errors=True)
    except Exception:
        pass


def _continue_reading_footer(item_key: str, actual_end: int, total_pages: int) -> str:
    """A tail prompt nudging the agent to read remaining pages.

    Returns an empty string when ``actual_end`` already covers the whole PDF,
    so callers can unconditionally append it. The 50-page step has been
    removed (the per-call page cap is gone); the prompt now suggests the
    full remaining range in one shot.
    """
    if actual_end >= total_pages:
        return ""
    nxt = actual_end + 1
    return (
        f"\n---\n**Read pages 1-{actual_end} of {total_pages}. "
        f"Pages {nxt}-{total_pages} not yet read.** "
        f"To continue: `zotero_read_pdf_pages(item_key='{item_key}', "
        f"start_page={nxt}, end_page={total_pages})`."
    )


def _get_pdf_path(item_key: str, ctx: Context) -> tuple[str, str, str | None, bool] | None:
    """Resolve a PDF attachment to a readable file.

    Tries local storage first (via LocalZoteroReader, reading files in place),
    then downloads via API. Returns ``(file_path, title, attachment_key,
    is_temp)`` or None if no PDF attachment is found. ``attachment_key`` is the
    Zotero attachment item key used as the MinerU cache key (None when only
    the parent item is known); ``is_temp`` says the caller owns the file and
    must remove it afterwards.
    """
    item = _library.get_library_backend().get_item(item_key)

    # Try local storage first (persists on disk — no cleanup needed)
    try:
        from zotero_mcp.local_db import LocalZoteroReader

        if _utils.is_local_mode():
            with LocalZoteroReader(db_path=load_config().resolve_zotero_db_path()) as reader:
                # The key may name the PDF attachment itself. Attachments have
                # no children, so the parent scan below comes up empty and we
                # would wrongly report "No PDF attachment found" (#372).
                attachment = reader.get_attachment_by_key(item_key)
                if attachment and "pdf" in (attachment["content_type"] or "").lower():
                    resolved = reader.resolve_attachment_file(item_key)
                    if resolved:
                        return str(resolved), attachment["title"] or item_key, item_key, False

                local_item = reader.get_item_by_key(item_key)
                if local_item:
                    for att_key, _path, ctype in reader._iter_parent_attachments(local_item.item_id):
                        if ctype == "application/pdf":
                            resolved = reader.resolve_attachment_file(att_key)
                            if resolved:
                                return str(resolved), local_item.title or item_key, att_key, False
                # Fast path: item_key itself is a PDF attachment (e.g. user
                # passed an attachment key directly) that get_attachment_by_key
                # missed but the API item dict still describes. Without this
                # we fall through to the tmpdir download path, and the tmp
                # file would be gone by the time the background MinerU worker
                # tried to read it — manifesting as a stuck "running" task.
                item_data = item.get("data", {}) if isinstance(item, dict) else {}
                if item_data.get("itemType") == "attachment" and item_data.get("contentType") == "application/pdf":
                    att_key = item.get("key", item_key)
                    resolved = reader.resolve_attachment_file(att_key)
                    if resolved and resolved.exists():
                        title = item_data.get("filename") or item_key
                        return str(resolved), title, att_key, False
    except Exception:
        pass

    # Fallback: resolve via the multi-source downloader (local -> WebDAV ->
    # Zotero cloud) so WebDAV-backed attachments work, not just cloud storage.
    # PDF only: this tool renders page ranges, so a markdown-first
    # attachment_priority must not hand it a file it cannot paginate.
    # Everything below is the download fallback, reached only when the file
    # is not in local storage. The API client is built here so a PDF already
    # on disk never needs one.
    zot = _client.get_zotero_client()
    attachment = _client.get_attachment_details(zot, item, priority=("pdf",))
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
        return str(download.path), attachment.title, attachment.key, True

    _cleanup_path(probe)
    return None


#: Most pages one text read returns.
_TEXT_MAX_PAGES = 50
#: Most pages one image read returns. A page image costs a vision model a few
#: thousand tokens, so an image read is for the pages that need one.
_IMAGE_MAX_PAGES = 10
#: Long edge of a rendered image in pixels. Vision models downscale anything
#: larger, so rendering past it only makes the response heavier.
_IMAGE_MAX_EDGE = 1568
#: Cap on magnification, so a crop of a few words is not blown up to mush.
_IMAGE_MAX_ZOOM = 4.0
#: Inline math characters on a page before its text is flagged as garbled.
#: A few variable names survive extraction; dense notation does not.
_INLINE_MATH_FLAG = 25

_IMAGE_HINTS = {
    "mcp": (
        "*Flagged pages have math, figures or tables that text extraction garbles. "
        "Read them with format='image'; add rect=[x, y, width, height] from "
        "zotero_get_page_layout to zoom into one of them.*"
    ),
    "cli": (
        "*Flagged pages have math, figures or tables that text extraction garbles. "
        "View them with `zotero-cli read {item_key} --start-page N --format image`; add "
        "--rect x,y,width,height from `zotero-cli layout ATTACHMENT_KEY` to zoom into one of them.*"
    ),
}


@mcp.tool(
    name="zotero_read_pdf_pages",
    description="Read specific page range(s) from a PDF attachment of a Zotero item. "
    "Use this when you know which pages to read — for example after getting the PDF "
    "outline via zotero_get_pdf_outline. Pages are 1-indexed. "
    "format='text' (default) returns Markdown with the heading structure preserved and "
    "flags pages whose equations, figures or tables the text garbles. "
    "format='image' returns the pages as PNG images (up to 10) so those can be read "
    "exactly; rect=[x, y, width, height] (normalized 0-1, e.g. from "
    "zotero_get_page_layout) returns just that region of start_page, zoomed in.",
    # Text or a list of text and images, so no single structured schema fits.
    output_schema=None,
)
def read_pdf_pages(
    item_key: str,
    start_page: int,
    end_page: int | None = None,
    format: Literal["text", "image"] = "text",
    rect: list[float] | str | None = None,
    *,
    backend: str | None = None,
    ctx: Context,
) -> str | list:
    """Read a page range of an item's PDF as Markdown, or as page images.

    Args:
        item_key: Zotero item key/ID of the paper or its PDF attachment.
        start_page: First page to read (1-indexed).
        end_page: Last page to read (1-indexed). If omitted, reads only start_page.
        format: "text" for Markdown, "image" for PNG page images.
        rect: With format="image", crop start_page to [x, y, width, height].
        ctx: MCP context.
    """
    if format == "image":
        header, pages = render_pdf_pages(item_key, start_page, end_page, rect=rect, ctx=ctx)
        return [header, *(Image(data=page["png"], format="png") for page in pages)]
    if format != "text":
        raise PdfReadError(f"format must be 'text' or 'image', got {format!r}", code="bad_format")
    return read_pdf_text(item_key, start_page, end_page, ctx=ctx, backend=backend)


@contextmanager
def _page_range(
    item_key: str,
    start_page: int,
    end_page: int | None,
    *,
    max_pages: int | None,
    ctx: Context,
):
    """Validate a page range against an item's PDF.

    Yields ``(pdf_path, title, total_pages, end_page, clamped_note)`` and
    removes a downloaded working copy afterwards, never a file in the user's
    library. The range is checked before anything is resolved (#528).
    """
    if not item_key or not item_key.strip():
        raise PdfReadError("Error: item_key cannot be empty.", code="empty_item_key")
    if end_page is not None and end_page < start_page:
        raise PdfReadError(
            "Error: end_page must be greater than or equal to start_page.",
            code="invalid_page_range",
        )

    ctx.info(f"Reading PDF pages {start_page}-{end_page or start_page} for item {item_key}")

    result = _get_pdf_path(item_key, ctx)
    if result is None:
        raise PdfReadError(
            f"No PDF attachment found for item: {item_key}",
            code="no_pdf_attachment",
        )
    pdf_path, title, attachment_key, is_temp = result
    # ``cleanup[0]`` stays True while this context manager owns the file.
    # A caller that hands the file to someone else (the MinerU background
    # worker) clears it so the finally below does not delete a file that is
    # still being read.
    cleanup = [bool(is_temp)]

    try:
        try:
            total_pages = pdf_page_count(pdf_path)
        except Exception as exc:
            raise PdfReadError(
                f"Could not read PDF for item {item_key}: {exc}",
                code="pdf_unreadable",
            ) from exc

        if start_page < 1 or start_page > total_pages:
            raise PdfReadError(
                f"Start page {start_page} is out of range. PDF has {total_pages} pages (1-{total_pages}).",
                code="page_out_of_range",
            )
        # A caller rarely knows the page count before the first read, and
        # "read to the end" is the usual intent behind an end page that is too
        # large. Clamp and say so instead of failing the whole read.
        actual_end = end_page if end_page is not None else start_page
        clamped_note = None
        if actual_end > total_pages:
            clamped_note = (
                f"*End page {actual_end} is past the last page; "
                f"read through page {total_pages}.*"
            )
            actual_end = total_pages

        requested = actual_end - start_page + 1
        if max_pages is not None and requested > max_pages:
            raise PdfReadError(
                f"Requested {requested} pages (max {max_pages}). Please narrow your page range.",
                code="page_limit_exceeded",
            )

        yield pdf_path, title, attachment_key, total_pages, actual_end, clamped_note, cleanup
    finally:
        if cleanup[0]:
            _cleanup_path(pdf_path)


def read_pdf_text(
    item_key: str,
    start_page: int,
    end_page: int | None = None,
    *,
    ctx: Context,
    surface: Literal["mcp", "cli"] = "mcp",
    backend: str | None = None,
) -> str:
    """Markdown for a page range, with pages the text garbles flagged.

    ``surface`` picks whether the closing advice names the MCP tool's image
    format or the zotero-cli flag.
    """
    try:
        # No page cap on text reads (fork): MinerU serves whole-document
        # slices and oversized PyMuPDF output is flagged by
        # _prepend_size_warning rather than refused.
        with _page_range(item_key, start_page, end_page,
                         max_pages=None, ctx=ctx) as (pdf_path, title, attachment_key,
                                                                 total_pages, actual_end,
                                                                 clamped_note, cleanup):
            # --- MinerU preferred path (fork): structured extraction with
            # formulas as LaTeX and tables as HTML, served from cache. On a
            # cold cache a background parse task is spawned (clearing
            # ``cleanup`` so the temp file survives for the worker) and this
            # call falls through to the text layer below. ---
            mineru_output = _try_mineru(
                attachment_key, pdf_path, start_page, actual_end, total_pages,
                title, item_key, ctx, backend=backend, file_owner=cleanup,
            )
            if mineru_output is not None:
                return mineru_output
            try:
                # extract_pdf takes 0-indexed pages; the tool's API is 1-indexed.
                doc = extract_pdf(pdf_path, pages=list(range(start_page - 1, actual_end)))
            except Exception as exc:
                raise PdfReadError(
                    f"Could not read PDF for item {item_key}: {exc}",
                    code="pdf_unreadable",
                ) from exc
            flags = _garbled_content_flags(pdf_path, doc)

        output = [
            f"# PDF Pages {start_page}-{actual_end} of {title}",
            f"**Item Key:** {item_key}",
            f"**Total pages in PDF:** {total_pages}",
            "",
        ]
        if clamped_note:
            output.extend([clamped_note, ""])

        for page_index, markdown in zip(doc.page_numbers, doc.pages):
            output.append(f"## Page {page_index + 1}")
            output.append("")
            if markdown.strip():
                output.append(markdown.strip())
            elif page_index in doc.needs_ocr:
                output.append("*[No text layer on this page — it is a scanned image]*")
            else:
                output.append("*[No extractable text on this page]*")
            output.append("")
            if page_index in flags:
                output.extend([flags[page_index], ""])
        if flags:
            output.append(_IMAGE_HINTS[surface].format(item_key=item_key))
        return _helpers._prepend_size_warning(
            "\n".join(output) + _continue_reading_footer(item_key, actual_end, total_pages),
            "Consider using zotero_semantic_search to find specific content instead of reading full pages.",
        )

    except PdfReadError:
        # Already carries the specific code; re-wrapping it here would bury
        # that under the generic one and repeat the message.
        raise
    except Exception as e:
        ctx.error(f"Error reading PDF pages: {str(e)}")
        raise PdfReadError(f"Error reading PDF pages: {str(e)}") from e


def _try_mineru(
    attachment_key: str | None,
    pdf_path: str,
    start_page: int,
    actual_end: int,
    total_pages: int,
    title: str,
    item_key: str,
    ctx: Context,
    *,
    backend: str | None = None,
    file_owner: list[bool] | None = None,
) -> str | None:
    """Attempt MinerU structured extraction. Returns Markdown str, or None to fall back.

    None is returned when MinerU is disabled, unavailable, or has no
    attachment key — the caller then falls back to PyMuPDF.

    When MinerU is enabled and the cache is valid, the cached per-page split
    is sliced and returned immediately (cache hit — instant).

    When MinerU is enabled but the cache is cold (first read of this PDF), a
    **background task** is spawned (``batch_runner.create_task`` +
    ``spawn_task``) to call ``read_cached_or_parse`` in a daemon thread, and
    this function returns a short "parse started" message with a ``task_id``.
    This avoids blocking the MCP tool call for the minutes MinerU takes to
    parse a full document (which exceeds the MCP client timeout). The worker
    writes the MinerU cache on completion; the next ``read_pdf_pages`` call
    hits the cache and returns instantly.

    ``backend`` pins a single MinerU backend (no cross-backend fallback) when
    set; None uses the configured backend with the full degradation chain.
    """
    config = mineru_client.load_mineru_config()
    if not mineru_client.is_mineru_enabled(config):
        return None
    if not mineru_client.is_mineru_available(config):
        ctx.warning(
            "MinerU is enabled but unavailable (only 'cloud' backend is supported; "
            "check mineru.cloud_token); using PyMuPDF fallback."
        )
        return None
    if not attachment_key:
        # Without an attachment key we cannot cache; skip MinerU rather than
        # parse uncached on every call (MinerU is too slow for that).
        ctx.warning("MinerU enabled but attachment key unknown; using PyMuPDF fallback.")
        return None

    # 1. Cache hit? Slice and return immediately (no parse, no background task).
    if mineru_client._cache_is_valid(attachment_key, Path(pdf_path), config):
        cached = mineru_client._read_cache(attachment_key, config)
        if cached is not None:
            _cleanup_path(pdf_path)
            return _format_mineru_output(
                cached, start_page, actual_end, total_pages, title, item_key
            )
        # Cache corrupt → drop and rebuild (via background task below).
        mineru_client._invalidate_cache(attachment_key, config)

    # 2. Cache miss → spawn a background parse task and return immediately.
    #    The MCP tool call would time out long before MinerU finishes a full
    #    document parse (minutes), so we never block on it here. The worker
    #    writes the MinerU cache on success; the next read_pdf_pages call hits
    #    the cache. Pattern: batch_runner.create_task + spawn_task (same as
    #    batch_cleanup_notes / upgrade_preprint_pdfs).
    from zotero_mcp.batch_runner import create_task, spawn_task

    work_items = [
        {
            "attachment_key": attachment_key,
            "pdf_path": pdf_path,
            "backend": backend,
            "item_key": item_key,
        }
    ]
    status = create_task("mineru_parse", work_items=work_items)
    spawn_task(status, _mineru_parse_worker)
    # NOTE: do NOT _cleanup_path(pdf_path) here — the background worker reads
    # it (and takes over cleanup when it finishes). ``file_owner`` is the
    # caller's cleanup flag from _page_range: clearing it keeps the context
    # manager's finally from deleting a temp download out from under the
    # worker. For local storage paths (the common case) cleanup is a no-op
    # anyway.
    if file_owner is not None:
        file_owner[0] = False
    backend_label = f" ({backend})" if backend else ""
    ctx.info(f"MinerU parse started in background{backend_label}: task_id={status.task_id}")
    return (
        f"⏳ MinerU 解析已启动: **{status.task_id}**\n\n"
        f"首次解析全本 PDF 需要几分钟（MinerU cloud 服务）。\n"
        f"解析完成后会缓存到 `~/.cache/zotero-mcp/mineru/{attachment_key}/`，"
        f"二次调用 `zotero_read_pdf_pages` 即可瞬时返回结构化内容。\n\n"
        f"**查询进度**: 调用 `zotero_get_batch_task_status(task_id='{status.task_id}')`。\n"
        f"**完成后重试**: `zotero_read_pdf_pages(item_key='{item_key}', "
        f"start_page={start_page}, end_page={actual_end})`。\n\n"
        f"**或者**现在就用 PyMuPDF 快速预览（无结构化公式/表格）: "
        f"MinerU 不可用时本工具会自动降级到 PyMuPDF；如需立即降级，"
        f"可临时禁用 MinerU（设置 `mineru.enabled=false`）后重试。"
    )


def _format_mineru_output(
    parsed: mineru_client.ParseResult,
    start_page: int,
    actual_end: int,
    total_pages: int,
    title: str,
    item_key: str,
) -> str:
    """Format a cached/parsed MinerU result into the page-range Markdown output.

    Shared by the cache-hit path in ``_try_mineru`` (and available for future
    callers that already hold a ``ParseResult``). ``parsed.source`` controls
    the ``Extraction:`` header label and the reindex hint.
    """
    pages = parsed.pages
    zstart = start_page - 1
    zend = actual_end - 1
    # If MinerU returned a single whole-document page (page split failed),
    # deliver the whole document but flag the caveat.
    single_page_caveat = len(pages) == 1 and total_pages > 1
    extraction_label = parsed.source.replace("mineru:", "")
    cache_status = "命中" if parsed.source == "mineru:cached" else "新建"

    output = [
        f"# PDF Pages {start_page}-{actual_end} of {title}",
        f"**Item Key:** {item_key}",
        f"**Total pages in PDF:** {total_pages}",
        f"**Extraction:** MinerU ({extraction_label})",
        f"**Coverage:** p.{start_page}-{actual_end}/{total_pages} | **Cache:** {cache_status}",
    ]
    if single_page_caveat:
        output.append(
            f"**Note:** page-level split unavailable; showing full document for pages {start_page}-{actual_end}."
        )
    # When MinerU just parsed the full document (not a cache hit), the
    # per-page cache is now available for the semantic-search build path.
    # Nudge the agent to build a complete, page-aware vector index from
    # this high-precision Markdown — the whole-PDF parse above means every
    # page is now searchable (no 20-chunk cap) with accurate page numbers.
    if parsed.source != "mineru:cached":
        output.append(
            f"**MinerU cache created (全本 PDF 已解析为高精度 Markdown).** "
            f"To make this full document searchable with page numbers in "
            f"semantic search, call: "
            f"`zotero_update_search_database(reindex_keys=['{item_key}'])` "
            f"(增量构建 — 只重新 embed 这一篇，不影响其他条目；幂等，可重复调用)"
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


def _outline_from_mineru_cache(attachment_key: str) -> str | None:
    """Derive a hierarchical outline from a MinerU cached parse.

    Reads ``fulltext.md`` (MinerU's high-precision Markdown — formulas as
    LaTeX, tables as HTML, chapter headings preserved as ``# / ## / ###``)
    and ``pages.json`` (per-page text split) from the MinerU cache directory,
    then maps each heading to its physical page number in the PDF.

    This is the preferred source for ``zotero_get_pdf_outline`` when the
    cache exists: it avoids re-downloading the PDF (sidestepping the
    ``file://`` protocol bug seen when ``zot.dump`` hits a transient local
    Zotero API state), and the outline reflects the document's *actual*
    structure (as MinerU parsed it) rather than the publisher's possibly
    stale or missing embedded bookmarks.

    Args:
        attachment_key: The PDF attachment's Zotero key (cache dir name).

    Returns:
        Markdown-formatted hierarchical outline (same format as the
        PyMuPDF-based ``get_pdf_outline``), or None if no valid MinerU
        cache exists for this attachment.
    """
    import re

    config = mineru_client.load_mineru_config()
    cache_dir = mineru_client._cache_dir_for(attachment_key, config)
    fulltext_path = cache_dir / "fulltext.md"
    pages_path = cache_dir / "pages.json"
    if not (fulltext_path.exists() and pages_path.exists()):
        return None

    try:
        markdown = fulltext_path.read_text(encoding="utf-8")
        pages = json.loads(pages_path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"Failed to read MinerU cache for outline ({attachment_key}): {e}")
        return None

    if not isinstance(pages, list) or not markdown:
        return None

    # Find all markdown headings in fulltext.md (level, title, char position).
    heading_matches = list(re.finditer(r"^(#{1,6})\s+(.+)$", markdown, re.MULTILINE))
    if not heading_matches:
        return None

    # Map each page to its starting char offset in fulltext.md so we can
    # locate which page a heading belongs to. pages.json's per-page text
    # is a substring (possibly whitespace-normalized) of fulltext.md, so
    # we anchor by the first non-empty chars of each page.
    page_offsets: list[tuple[int, int]] = []  # (start_offset, page_number)
    search_from = 0
    for i, page_text in enumerate(pages, start=1):
        # Find a stable anchor: first ~30 non-empty chars of the page.
        anchor = page_text[:30].strip()
        if not anchor:
            # Empty page — approximate at current search position.
            page_offsets.append((search_from, i))
            continue
        pos = markdown.find(anchor, search_from)
        if pos == -1:
            # Try shorter anchor (whitespace differences may break long match).
            anchor = page_text[:15].strip()
            pos = markdown.find(anchor, search_from) if anchor else -1
        if pos == -1:
            # Page text not found verbatim (MinerU may have re-flowed).
            # Fall back to current search_from so this page is treated as
            # starting where the previous one ended.
            pos = search_from
        page_offsets.append((pos, i))
        search_from = pos + 1

    def _page_of(pos: int) -> int:
        """Return the page number (1-indexed) containing char offset ``pos``."""
        page = 1
        for start, pnum in page_offsets:
            if start <= pos:
                page = pnum
            else:
                break
        return page

    lines = [f"# PDF Outline (via MinerU cache) for attachment `{attachment_key}`", ""]
    for m in heading_matches:
        level = len(m.group(1))
        title = m.group(2).strip()
        page = _page_of(m.start())
        indent = "  " * (level - 1)
        lines.append(f"{indent}- {title} (p. {page})")

    return "\n".join(lines)


def _mineru_parse_worker(status: "TaskStatus") -> None:
    """Background worker: parse the full PDF with MinerU and write the cache.

    Follows the ``batch_runner`` worker contract (cf. ``_batch_cleanup_worker``
    in tools/annotations.py). Reads its work item from ``status.work_items``,
    calls ``mineru_client.read_cached_or_parse`` (which writes the MinerU
    cache on success), and reports progress via ``update_status``. The
    worker must NOT touch the foreground MCP ``Context`` — it is dead once
    the tool function returned. Logs via ``logging`` only.

    The status JSON is written under
    ``~/.config/zotero-mcp/batch_tasks/<task_id>.json`` and is pollable via
    ``zotero_get_batch_task_status``.
    """
    from zotero_mcp.batch_runner import update_status

    if not status.work_items:
        update_status(status.task_id, status="failed", error="no work items")
        return

    item = status.work_items[0]
    attachment_key = item.get("attachment_key")
    pdf_path = item.get("pdf_path")
    backend = item.get("backend")

    if not attachment_key or not pdf_path:
        update_status(
            status.task_id, status="failed", error="missing attachment_key or pdf_path"
        )
        return

    config = mineru_client.load_mineru_config()
    backend_label = item.get("backend") or config.get("backend") or "cloud"
    update_status(
        status.task_id,
        result_summary=f"MinerU 解析中 (backend={backend_label})，每 3 秒轮询一次 cloud API...",
    )
    logger.warning(
        f"MinerU parse worker {status.task_id}: starting cloud parse "
        f"(attachment={attachment_key}, backend={backend_label})"
    )

    # Capture WARNING-level logs from mineru_client during the parse, so we
    # can surface the failure reason in the task status (the MCP server's
    # stderr isn't always visible to the user).
    import logging as _logging
    captured_warnings: list[str] = []

    class _CaptureHandler(_logging.Handler):
        def emit(self, record: _logging.LogRecord) -> None:
            if record.levelno >= _logging.WARNING:
                captured_warnings.append(self.format(record))

    capture_handler = _CaptureHandler()
    capture_handler.setFormatter(_logging.Formatter("%(name)s: %(message)s"))
    mineru_logger = _logging.getLogger("zotero_mcp.mineru_client")
    mineru_logger.addHandler(capture_handler)
    try:
        parsed = mineru_client.read_cached_or_parse(
            attachment_key, Path(pdf_path), config, backend_override=backend
        )
    except Exception as e:
        logger.exception(f"MinerU parse worker {status.task_id} raised: {e}")
        update_status(
            status.task_id,
            status="failed",
            error=str(e),
            result_summary=f"解析异常: {e}",
        )
        return
    finally:
        mineru_logger.removeHandler(capture_handler)
        _cleanup_path(pdf_path)

    if parsed is None:
        # Surface the captured warnings so the user/agent can see WHY it
        # failed (which cloud API step returned an error) without needing
        # access to the server's stderr.
        detail = "; ".join(captured_warnings[-5:]) if captured_warnings else "no warnings captured"
        update_status(
            status.task_id,
            status="failed",
            error=f"MinerU parse returned no result ({detail})",
            result_summary=(
                "解析失败 — 详细原因见 error 字段。"
                "二次调用 read_pdf_pages 会自动降级到 PyMuPDF。"
            ),
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
        return

    update_status(
        status.task_id,
        status="completed",
        processed=1,
        succeeded=1,
        succeeded_items=[{"key": attachment_key, "detail": parsed.source}],
        result_summary=(
            f"MinerU 解析完成 ({parsed.source})。"
            f"二次调用 read_pdf_pages(item_key=...) 即可命中缓存，瞬时返回。"
        ),
        completed_at=datetime.now(timezone.utc).isoformat(),
    )


def _garbled_content_flags(pdf_path: str, doc) -> dict[int, str]:
    """A note per page (0-indexed) naming what its extracted text cannot carry.

    Display equations and dense inline math come from the page's fonts
    (``pdf_layout.scan_math``); figures and tables from their captions in the
    extracted Markdown, which is already in hand. A page with none of these
    gets no note. Best effort: without PyMuPDF, or on a file it cannot open,
    the read simply carries no flags.
    """
    try:
        import fitz

        from zotero_mcp.pdf_layout import _parse_caption_block, scan_math

        pdf = fitz.open(pdf_path)
    except Exception:
        return {}

    flags: dict[int, str] = {}
    try:
        for page_index, markdown in zip(doc.page_numbers, doc.pages):
            equations, inline_math = scan_math(pdf[page_index])
            numbered = [eq["label"] for eq in equations if eq["label"]]
            unnumbered = len(equations) - len(numbered)
            items = []
            if numbered:
                items.append(("Equation " if len(numbered) == 1 else "Equations ") + ", ".join(numbered))
            if unnumbered:
                items.append(f"{unnumbered} unnumbered equation{'s' if unnumbered > 1 else ''}")
            for line in markdown.splitlines():
                caption = _parse_caption_block(line.strip().lstrip("#*_ ").strip())
                if caption and caption["label"] not in items:
                    items.append(caption["label"])
            if inline_math >= _INLINE_MATH_FLAG:
                items.append("inline math")
            if items:
                flags[page_index] = f"> **Garbled in this text:** {', '.join(items)}"
    except Exception:
        return {}
    finally:
        pdf.close()
    return flags


def render_pdf_pages(
    item_key: str,
    start_page: int,
    end_page: int | None = None,
    *,
    rect: list[float] | str | None = None,
    ctx: Context,
) -> tuple[str, list[dict]]:
    """Render a page range, or one region of ``start_page``, to PNG.

    Returns:
        (header, pages): a Markdown header naming what was rendered, and one
        ``{"page", "png", "width", "height"}`` per image.
    """
    box = None
    if rect is not None:
        box = _helpers._normalize_float_list_input(rect, 4, "rect")
        if (
            box is None
            or not all(0 <= value <= 1 for value in box)
            or box[2] <= 0 or box[3] <= 0
            or box[0] + box[2] > 1.0001 or box[1] + box[3] > 1.0001
        ):
            raise PdfReadError(
                f"rect must be [x, y, width, height] within the page, normalized to 0-1; got {rect!r}",
                code="bad_rect",
            )
        if end_page not in (None, start_page):
            raise PdfReadError(
                "rect crops a single page; omit end_page or set it to start_page.",
                code="invalid_page_range",
            )
    try:
        import fitz
    except ImportError as exc:
        raise PdfReadError(
            f"Rendering pages requires PyMuPDF. {_utils.install_hint('pdf')}",
            code="missing_dependency",
        ) from exc

    with _page_range(item_key, start_page, end_page,
                     max_pages=_IMAGE_MAX_PAGES, ctx=ctx) as (pdf_path, title, _att_key,
                                                              total_pages, actual_end,
                                                              clamped_note, _cleanup):
        pdf = fitz.open(pdf_path)
        try:
            pages = []
            for number in range(start_page, actual_end + 1):
                page = pdf[number - 1]
                clip = page.rect
                if box is not None:
                    x, y, w, h = box
                    clip = fitz.Rect(
                        clip.x0 + x * clip.width, clip.y0 + y * clip.height,
                        clip.x0 + (x + w) * clip.width, clip.y0 + (y + h) * clip.height,
                    )
                zoom = min(_IMAGE_MAX_EDGE / max(clip.width, clip.height), _IMAGE_MAX_ZOOM)
                pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip, alpha=False)
                pages.append({"page": number, "png": pixmap.tobytes("png"),
                              "width": pixmap.width, "height": pixmap.height})
        finally:
            pdf.close()

    what = (
        f"Region [{', '.join(f'{v:.4f}' for v in box)}] of page {start_page}"
        if box is not None else f"Pages {start_page}-{actual_end}"
    )
    header = [f"# {what} of {title}", f"**Item Key:** {item_key}",
              f"**Total pages in PDF:** {total_pages}"]
    if clamped_note:
        header.append(clamped_note)
    return "\n".join(header), pages
