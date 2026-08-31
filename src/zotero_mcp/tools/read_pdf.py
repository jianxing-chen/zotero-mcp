"""Tool for reading specific page ranges from PDF attachments."""

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from fastmcp import Context

from zotero_mcp import client as _client
from zotero_mcp import mineru_client
from zotero_mcp import utils as _utils
from zotero_mcp._app import mcp
from zotero_mcp.config import load_config
from zotero_mcp.extract import extract_pdf, pdf_page_count
from zotero_mcp.tools import _helpers

if TYPE_CHECKING:
    from zotero_mcp.batch_runner import TaskStatus

logger = logging.getLogger(__name__)


_TMPDIR_PREFIX = "zotero_pdf_"


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
            with LocalZoteroReader(db_path=load_config().resolve_zotero_db_path()) as reader:
                # The key may name the PDF attachment itself. Attachments have
                # no children, so the parent scan below comes up empty and we
                # would wrongly report "No PDF attachment found" (#372).
                attachment = reader.get_attachment_by_key(item_key)
                if attachment and "pdf" in (attachment["content_type"] or "").lower():
                    resolved = reader._resolve_attachment_path(
                        item_key, attachment["zotero_path"] or ""
                    )
                    if not (resolved and resolved.exists()):
                        # Recorded filename drifted on disk — scan the folder (#291)
                        resolved = reader._scan_storage_for_attachment(
                            item_key, attachment["content_type"]
                        )
                    if resolved and resolved.exists():
                        return str(resolved), attachment["title"] or item_key, item_key

                local_item = reader.get_item_by_key(item_key)
                if local_item:
                    for att_key, path, ctype in reader._iter_parent_attachments(local_item.item_id):
                        if ctype == "application/pdf":
                            resolved = reader._resolve_attachment_path(att_key, path or "")
                            if resolved and resolved.exists():
                                return str(resolved), local_item.title or item_key, att_key
                # Fast path: item_key itself is a PDF attachment (e.g. user
                # passed an attachment key directly). Resolve it without
                # going through the parent-item lookup, which fails for
                # attachment keys (they're not in the items table). Without
                # this, we'd fall through to the tmpdir download path, and
                # the tmp file would be gone by the time the background
                # MinerU worker tried to read it (race with main-process
                # cleanup) — manifesting as a stuck "running" task that
                # never completes.
                item_data = item.get("data", {}) if isinstance(item, dict) else {}
                if item_data.get("itemType") == "attachment" and item_data.get("contentType") == "application/pdf":
                    att_key = item.get("key", item_key)
                    zotero_path = item_data.get("path") or ""
                    resolved = reader._resolve_attachment_path(att_key, zotero_path)
                    # Zotero local API often omits `path` for storage-managed
                    # attachments (only returns `filename`). Fall back to the
                    # storage dir directly: <storage>/<att_key>/<filename>.
                    if resolved is None and not zotero_path and item_data.get("filename"):
                        storage_dir = reader._get_storage_dir()
                        if storage_dir:
                            candidate = storage_dir / att_key / item_data["filename"]
                            if candidate.exists():
                                resolved = candidate
                    if resolved and resolved.exists():
                        title = item_data.get("filename") or item_key
                        return str(resolved), title, att_key
    except Exception:
        pass

    # Fallback: resolve via the multi-source downloader (local -> WebDAV ->
    # Zotero cloud) so WebDAV-backed attachments work, not just cloud storage.
    # PDF only: this tool renders page ranges, so a markdown-first
    # attachment_priority must not hand it a file it cannot paginate.
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
        return str(download.path), attachment.title, attachment.key

    _cleanup_path(probe)
    return None


@mcp.tool(
    name="zotero_read_pdf_pages",
    description=(
        "Read specific page range(s) from a PDF attachment of a Zotero item — "
        "also known as 精读 (close/structured reading). "
        "THE tool for extracting PDF content with accurate formulas (LaTeX) "
        "and tables (HTML) via MinerU (cloud only). "
        "MinerU parses the ENTIRE PDF on first call and caches it, so later "
        "reads of any page are instant. On a cold cache it returns a task_id "
        "instead of blocking — poll `zotero_get_batch_task_status(task_id=...)`; "
        "once 'completed', call again for instant content. For immediate "
        "content, set mineru.enabled=false to use PyMuPDF fallback. "
        "Use when the user says 精读/读论文/读这本书/read this paper/extract "
        "formulas or tables; or after zotero_semantic_search returns a page "
        "number. "
        "Pages are 1-indexed; NO per-call page cap — request the full document "
        "(start_page=1, end_page=N) in one call. "
        "After a fresh MinerU parse, a hint suggests "
        "`zotero_update_search_database(reindex_keys=[...])` to build a "
        "page-aware vector index. "
        "backend: only 'cloud' supported; other values fall back to PyMuPDF. "
        "Requires PyMuPDF: pip install zotero-mcp-server[pdf]."
    ),
)
def read_pdf_pages(
    item_key: str,
    start_page: int,
    end_page: int | None = None,
    *,
    backend: str | None = None,
    ctx: Context,
) -> str:
    """Extract and return text from a specific page range of a PDF.

    Args:
        item_key: Zotero item key/ID of the paper or its PDF attachment.
        start_page: First page to read (1-indexed).
        end_page: Last page to read (1-indexed). If omitted, reads only start_page.
        backend: Pin a single MinerU backend. Only ``"cloud"`` is currently
            supported; other values (``"pipeline"``, ``"hybrid"``, ``"api"``)
            are disabled at the config layer and cause MinerU to be
            unavailable (PyMuPDF fallback). None (default) uses the configured
            backend. Ignored when MinerU is disabled or a cache hit serves the
            request.
        ctx: MCP context.

    Returns:
        Markdown-formatted page content with metadata header. When MinerU's
        cache is cold, returns a "parse started" message with a task_id
        instead of blocking on the multi-minute parse.
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

        # NOTE: the previous 50-page-per-call cap has been removed. MinerU
        # parses the entire PDF on the first call anyway (its cache is
        # whole-document), and the page range only slices the returned
        # content. Large ranges are fine; very large outputs are flagged by
        # _helpers._prepend_size_warning rather than hard-truncated.

        # --- MinerU preferred path (structured: formulas as LaTeX, tables as HTML) ---
        mineru_output = _try_mineru(
            attachment_key, pdf_path, start_page, actual_end, total_pages, title, item_key, ctx, backend=backend
        )
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
    *,
    backend: str | None = None,
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
    # it. The worker is responsible for cleanup when it finishes. For local
    # storage paths (the common case) cleanup is a no-op anyway.
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
            f"**Coverage:** p.{start_page}-{actual_end}/{total_pages} | **Cache:** 不适用",
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
