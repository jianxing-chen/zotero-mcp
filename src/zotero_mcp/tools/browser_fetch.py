"""MCP tool: upgrade arXiv PDFs to publisher versions via a browser session.

A browser-assisted variant of ``upgrade_preprint_pdfs``. For each arXiv-ID
item it tries the existing HTTP cascade first (ADS PUB_PDF via
``pub_only=True``); only when the HTTP download is blocked by a publisher
WAF/captcha/403 does it fall back to fetching the PDF inside a live,
already-authorized Chrome/Edge DevTools session.

The browser carries the user's real session cookies and institutional
authorization, so the in-page ``fetch()`` looks like a normal navigation
and bypasses the bot detection that blocks plain ``requests.get``.

Legal boundary: the user must manually sign in and pass any verification
in the browser before calling this tool. It does not bypass paywalls,
CAPTCHA, or institutional gates — it only fetches PDFs the user is
already authorized to access.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time as _time
from typing import TYPE_CHECKING

from zotero_mcp import ads_client as _ads_client
from zotero_mcp import browser_fetch_client as _browser_fetch
from zotero_mcp._app import mcp
from zotero_mcp._context import Context
from zotero_mcp.tools import _helpers

if TYPE_CHECKING:
    from zotero_mcp.batch_runner import TaskStatus

logger = logging.getLogger(__name__)


def _with_api_lock(fn):
    """Acquire the Zotero API RLock around ``fn`` (re-exported from write)."""
    from zotero_mcp.tools import write as _write

    return _write._with_api_lock(fn)


@mcp.tool(
    name="zotero_upgrade_preprint_pdfs_via_browser",
    description=(
        "Replace arXiv PDFs with publisher versions, falling back to a live "
        "browser session when the HTTP cascade is blocked. For each item: "
        "(1) try the existing publisher-only HTTP cascade (ADS PUB_PDF via "
        "pub_only=True); (2) if that fails (WAF/403/challenge), "
        "fetch the PDF inside an already-authorized Chrome/Edge DevTools "
        "session via in-page fetch() or PDF.js extraction. "
        "Scans the same two item types as zotero_upgrade_preprint_pdfs: "
        "preprints with an arXiv ID (metadata upgraded first) and "
        "journalArticles with an arXiv ID in Extra (PDF only). After "
        "successful replacement the arXiv line is removed from Extra "
        "(idempotent on re-runs). Old PDFs are trashed only after a "
        "successful download (recoverable from Zotero's Trash). "
        "Requires a running Chrome/Edge with --remote-debugging-port and "
        "the browser_fetch.enabled config block. The user must manually "
        "sign in and pass any bot verification in the browser window "
        "before calling this tool — it does not bypass paywalls or CAPTCHA. "
        "Trigger modes: (a) default scan; (b) require_bibcode=True; "
        "(c) item_keys=[...]; (d) collection='name or key'; "
        "(e) collection='_unfiled'. "
        "Runs as a background task — returns task_id immediately, poll "
        "with zotero_get_batch_task_status."
    ),
)
def upgrade_preprint_pdfs_via_browser(
    limit: int | None = None,
    item_keys: list[str] | str | None = None,
    require_bibcode: bool = False,
    collection: str | None = None,
    *,
    ctx: Context,
) -> str:
    """Browser-assisted publisher PDF replacement.

    Not decorated with @with_zotero_api_lock: spawns a background task that
    acquires the lock per-item. HTTP-first: the browser is only used when
    the HTTP cascade (ADS PUB_PDF) is blocked by the publisher.
    """
    try:
        read_zot, _write_zot = _helpers._get_write_client(ctx)
    except ValueError as e:
        return str(e)

    if not _ads_client.is_available():
        return (
            "Error: ADS_API_TOKEN is not set. This tool needs ADS to resolve the "
            "published version of each preprint. Get a free token at "
            "https://ui.adsabs.harvard.edu/#user/settings/token"
        )

    config = _browser_fetch.load_browser_fetch_config()
    if not _browser_fetch.is_browser_fetch_enabled(config):
        return (
            "Error: browser_fetch is not enabled. Add a `browser_fetch` block to "
            "~/.config/zotero-mcp/config.json with `enabled: true` and "
            "`debug_port: <port>`, launch Chrome/Edge with "
            "`--remote-debugging-port=<port>`, sign in to your institutional "
            "proxy / publisher, then retry."
        )

    debug_port = int(config.get("debug_port", 9222))
    devtools = _browser_fetch.DevToolsClient(debug_port)
    if not devtools.is_reachable():
        return (
            f"Error: no browser found at the DevTools endpoint on port {debug_port}. "
            f"Launch Chrome/Edge with `--remote-debugging-port={debug_port}` and "
            "keep the window open."
        )

    # Reuse the shared scan logic from upgrade_preprint_pdfs.
    from zotero_mcp.tools.write import _scan_arxiv_items_for_pdf_upgrade

    preprints = _scan_arxiv_items_for_pdf_upgrade(
        read_zot,
        item_keys=item_keys,
        collection=collection,
        require_bibcode=require_bibcode,
        limit=limit,
        ctx=ctx,
    )
    if isinstance(preprints, str):
        return preprints

    total = len(preprints)
    if total == 0:
        return "No preprint items found matching the criteria."

    from zotero_mcp.batch_runner import create_task, spawn_task

    work_items = [
        {"key": it.get("key", ""), "title": (it.get("data", {}).get("title") or "")[:60]}
        for it in preprints[:total]
    ]
    status = create_task("upgrade_preprint_pdfs_via_browser", work_items=work_items)
    spawn_task(status, lambda s: _upgrade_preprint_pdfs_via_browser_worker(s, preprints[:total], config))

    return (
        f"⏳ Browser-assisted PDF upgrade started: **{status.task_id}**\n\n"
        f"Will process {total} item(s) in the background.\n"
        f"HTTP cascade is tried first; the browser is used only as a fallback "
        f"when the publisher blocks direct download.\n\n"
        f"Check progress: call `zotero_get_batch_task_status` "
        f"with task_id `{status.task_id}`."
    )


def _resolve_pub_doi_and_bibcode(write_zot, key: str, item_type: str, item_data: dict) -> tuple[str | None, str | None]:
    """Resolve the publisher DOI + bibcode for an item.

    For preprints: re-reads the item after metadata upgrade.
    For journalArticles: reads directly from the cached item_data.
    Returns (pub_doi, pub_bibcode) — pub_doi is None for arXiv DOIs
    (``10.48550/...``).
    """
    from zotero_mcp.tools.write import _parse_bibcode_from_extra

    if item_type == "preprint":
        try:
            upgraded_item = _with_api_lock(lambda k=key: write_zot.item(k))
            pub_doi = (upgraded_item.get("data", {}).get("DOI") or "").strip()
            if not pub_doi or pub_doi.startswith("10.48550/"):
                pub_doi = None
            pub_bibcode = _parse_bibcode_from_extra(upgraded_item.get("data", {}).get("extra"))
            return pub_doi, pub_bibcode
        except Exception as e:
            logger.warning(f"Re-read of upgraded item {key} failed: {e}")
            return None, None

    # journalArticle: read directly from the cached data.
    pub_doi = (item_data.get("DOI") or "").strip()
    if not pub_doi or pub_doi.startswith("10.48550/"):
        pub_doi = None
    pub_bibcode = _parse_bibcode_from_extra(item_data.get("extra"))
    return pub_doi, pub_bibcode


def _upgrade_preprint_pdfs_via_browser_worker(status: TaskStatus, preprints: list[dict], config: dict) -> None:
    """Background worker: HTTP-first, browser fallback for publisher PDFs."""
    from zotero_mcp.batch_runner import DummyCtx, update_status
    from zotero_mcp.tools.write import (
        _remove_arxiv_from_extra,
        _scan_arxiv_items_for_pdf_upgrade,  # noqa: F401 — kept for symmetry
        _upgrade_single_preprint,
    )

    try:
        _, write_zot = _helpers._get_write_client(DummyCtx())
    except ValueError as e:
        raise RuntimeError(str(e))

    debug_port = int(config.get("debug_port", 9222))
    page_wait = int(config.get("page_wait_seconds", 8))
    devtools = _browser_fetch.DevToolsClient(debug_port)

    total = len(preprints)
    succeeded = 0
    failed = 0
    succeeded_items: list[dict] = []
    failed_items: list[dict] = []

    for idx, it in enumerate(preprints, 1):
        key = it.get("key", "")
        if not key:
            continue
        item_type = it.get("data", {}).get("itemType", "")
        try:
            # Step 1: metadata upgrade for preprints; journalArticle skips.
            if item_type == "preprint":
                upg = _with_api_lock(lambda k=key: _upgrade_single_preprint(write_zot, k))
                if upg["status"] != "upgraded":
                    failed += 1
                    succeeded_items.append({"key": key, "detail": "not published"})
                    _time.sleep(0.3)
                    continue

            # Step 2: resolve publisher DOI + bibcode.
            pub_doi, pub_bibcode = _resolve_pub_doi_and_bibcode(
                write_zot, key, item_type, it.get("data", {})
            )
            if not pub_doi:
                failed += 1
                succeeded_items.append({"key": key, "detail": "no publisher DOI"})
                _time.sleep(0.3)
                continue

            # Step 3: HTTP-first — try the existing pub_only cascade.
            old_pdf_keys = set(
                _with_api_lock(lambda k=key: _helpers._list_pdf_attachment_keys(write_zot, k))
            )
            pdf_status = _with_api_lock(
                lambda: _helpers._try_attach_oa_pdf(
                    write_zot, key, pub_doi, DummyCtx(),
                    bibcode=pub_bibcode, prefer_pub_pdf=True, pub_only=True,
                )
            )
            if "attached" in (pdf_status or "").lower():
                # HTTP succeeded — trash old PDFs (preserve the new one) + idempotency marker.
                _with_api_lock(
                    lambda: _helpers._trash_pdf_attachments(
                        write_zot, key, DummyCtx(), only_keys=old_pdf_keys,
                    )
                )
                _remove_arxiv_from_extra(write_zot, key)
                succeeded += 1
                succeeded_items.append({"key": key, "detail": "PDF replaced via HTTP"})
                _time.sleep(0.3)
                if idx % 5 == 0 or idx == total:
                    update_status(
                        status.task_id,
                        processed=idx, succeeded=succeeded, failed=failed,
                        succeeded_items=succeeded_items, failed_items=failed_items,
                    )
                continue

            # Step 4: browser fallback — fetch inside the live authorized session.
            article_url = f"https://doi.org/{pub_doi}"
            pdf_bytes, source_url, label = _browser_fetch.fetch_publisher_pdf_via_browser(
                devtools, article_url, page_wait_seconds=page_wait,
            )
            if not pdf_bytes:
                failed += 1
                failed_items.append({"key": key, "detail": f"browser: {label}"})
                _time.sleep(0.3)
                if idx % 5 == 0 or idx == total:
                    update_status(
                        status.task_id,
                        processed=idx, succeeded=succeeded, failed=failed,
                        succeeded_items=succeeded_items, failed_items=failed_items,
                    )
                continue

            # Browser succeeded — attach the bytes, trash old PDFs, remove arXiv marker.
            # Re-snapshot old keys in case the HTTP attempt partially attached something.
            old_pdf_keys = set(
                _with_api_lock(lambda k=key: _helpers._list_pdf_attachment_keys(write_zot, k))
            )
            safe_doi = pub_doi.replace("/", "_")
            filename = f"{safe_doi}.pdf"
            try:
                with tempfile.TemporaryDirectory() as tmpdir:
                    filepath = os.path.join(tmpdir, filename)
                    with open(filepath, "wb") as f:
                        f.write(pdf_bytes)

                    # Try attaching via Zotero Web API (which uploads to Zotero Storage).
                    # If the storage quota is exceeded, fall back to WebDAV upload
                    # (when configured). attachment_both may create the attachment
                    # item (metadata) but fail on the file upload — the orphaned
                    # "empty shell" must be cleaned up so it doesn't show a broken
                    # "file not found" link in the Zotero desktop client.
                    try:
                        _with_api_lock(
                            lambda: write_zot.attachment_both(
                                [(filename, filepath)], parentid=key,
                            )
                        )
                    except Exception as attach_err:
                        # Clean up orphaned empty-shell attachments created by the
                        # failed attachment_both (item created, file upload failed).
                        from pyzotero.zotero import build_url as _build_url

                        _current_children = _with_api_lock(
                            lambda k=key: write_zot.children(k)
                        )
                        for _c in _current_children:
                            _cd = _c.get("data", {})
                            if _cd.get("itemType") != "attachment":
                                continue
                            if _cd.get("contentType") != "application/pdf":
                                continue
                            # An empty shell has no md5/mtime (file never uploaded).
                            if _cd.get("md5") is None and _cd.get("mtime") is None:
                                if _c["key"] not in old_pdf_keys:  # don't trash pre-existing
                                    _ck = _c["key"]
                                    _cv = _c.get("version", 0)
                                    _url = _build_url(
                                        write_zot.endpoint,
                                        f"/{write_zot.library_type}/{write_zot.library_id}/items/{_ck}",
                                    )
                                    _r = write_zot.client.patch(
                                        url=_url,
                                        headers={"If-Unmodified-Since-Version": str(_cv)},
                                        content=json.dumps({"deleted": 1}),
                                    )
                                    logger.info(f"Cleaned up orphaned empty-shell attachment {_ck}: HTTP {_r.status_code}")

                        from zotero_mcp import webdav as _webdav
                        if _webdav.is_webdav_configured():
                            logger.info(f"Zotero Storage attach failed ({attach_err}), trying WebDAV...")
                            # Create a bare attachment item (no file bytes), then
                            # upload the file to WebDAV using its key.
                            # item_template requires linkmode param (lowercase) for
                            # itemType=attachment, otherwise the API returns 400.
                            template = _with_api_lock(
                                lambda: write_zot.item_template("attachment", "imported_file")
                            )
                            template["parentItem"] = key
                            template["title"] = filename
                            template["contentType"] = "application/pdf"
                            template["filename"] = filename
                            created = _with_api_lock(
                                lambda: write_zot.create_items([template])
                            )
                            # Extract the new attachment key from the response.
                            # pyzotero's create_items returns:
                            #   {'success': {'0': 'RA3EKKJG'}, 'successful': {'0': {full item}}, ...}
                            # The key can be in 'success' (as a string value) or
                            # in 'successful' (inside the full item dict).
                            new_key = None
                            if isinstance(created, dict):
                                # Try 'success' first — values are key strings
                                for v in created.get("success", {}).values():
                                    if isinstance(v, str) and v:
                                        new_key = v
                                        break
                                # Try 'successful' — values are full item dicts
                                if not new_key:
                                    for entry in created.get("successful", {}).values():
                                        if isinstance(entry, dict) and entry.get("key"):
                                            new_key = entry["key"]
                                            break
                                # Also try 'success' as a list (older pyzotero)
                                if not new_key:
                                    for entry in created.get("success", []):
                                        if isinstance(entry, dict) and entry.get("key"):
                                            new_key = entry["key"]
                                            break
                            if new_key:
                                _webdav.upload_attachment_to_webdav(
                                    attachment_key=new_key, file_path=filepath,
                                )
                                logger.info(f"WebDAV upload succeeded for {new_key}.zip")
                            else:
                                raise RuntimeError(f"WebDAV fallback: could not extract attachment key from {created}")
                        else:
                            raise
            except Exception as e:
                logger.warning(f"Failed to attach browser-fetched PDF for {key}: {e}")
                failed += 1
                failed_items.append({"key": key, "detail": f"attach failed: {e}"})
                _time.sleep(0.3)
                if idx % 5 == 0 or idx == total:
                    update_status(
                        status.task_id,
                        processed=idx, succeeded=succeeded, failed=failed,
                        succeeded_items=succeeded_items, failed_items=failed_items,
                    )
                continue

            _with_api_lock(
                lambda: _helpers._trash_pdf_attachments(
                    write_zot, key, DummyCtx(), only_keys=old_pdf_keys,
                )
            )
            _remove_arxiv_from_extra(write_zot, key)
            succeeded += 1
            succeeded_items.append({"key": key, "detail": f"PDF replaced via browser ({label})"})
        except Exception as e:
            logger.warning(f"Failed to process item {key}: {e}")
            failed += 1
            failed_items.append({"key": key, "detail": str(e)})
        _time.sleep(0.3)

        if idx % 5 == 0 or idx == total:
            update_status(
                status.task_id,
                processed=idx, succeeded=succeeded, failed=failed,
                succeeded_items=succeeded_items, failed_items=failed_items,
            )

    update_status(
        status.task_id,
        processed=total, succeeded=succeeded, failed=failed,
        succeeded_items=succeeded_items, failed_items=failed_items,
        result_summary=(
            f"HTTP succeeded: {sum(1 for i in succeeded_items if 'HTTP' in i.get('detail', ''))}, "
            f"browser succeeded: {sum(1 for i in succeeded_items if 'browser' in i.get('detail', ''))}, "
            f"failed: {failed}."
        ),
    )
