"""Integration tests for the zotero_upgrade_preprint_pdfs_via_browser tool.

Covers:
- Error returns when browser_fetch is disabled or DevTools is unreachable.
- HTTP-first behavior: when the HTTP cascade succeeds, the browser is never called.
- Browser fallback: when HTTP fails, the browser fetcher is invoked.
- Browser failure: both HTTP and browser fail -> item recorded as failed, old PDF kept.
- pub_only=True is passed to the HTTP cascade.
"""

from __future__ import annotations

import re
import time as _time
from unittest.mock import MagicMock, patch

from zotero_mcp import ads_client
from zotero_mcp import browser_fetch_client as bfc
from zotero_mcp.tools import _helpers as helpers_mod

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_task_id(result: str) -> str:
    r"""Extract task_id from the tool return string (anchored to the canonical format)."""
    m = re.search(r"\*\*([0-9T]{8}T[0-9]{6}Z-[0-9a-f]+)\*\*", result)
    assert m, f"could not extract task_id from result: {result[:120]!r}"
    return m.group(1)


def _make_preprint_item(key="PRE1", title="Test Paper", arxiv_id="2401.12345", bibcode="2013ApJ...769..127L"):
    return {
        "key": key,
        "version": 1,
        "data": {
            "itemType": "preprint",
            "title": title,
            "extra": f"arXiv:{arxiv_id}\nbibcode: {bibcode}",
        },
    }


def _wait_for_task(task_id: str, timeout: float = 5.0) -> object | None:
    from zotero_mcp.batch_runner import read_status

    deadline = _time.time() + timeout
    while _time.time() < deadline:
        loaded = read_status(task_id)
        if loaded and loaded.status in ("completed", "failed"):
            return loaded
        _time.sleep(0.05)
    return read_status(task_id)


# ---------------------------------------------------------------------------
# Tool-level error checks
# ---------------------------------------------------------------------------


class TestUpgradePreprintPdfsViaBrowser:
    def _setup_common(self, monkeypatch, tmp_path, *, clients=None):
        monkeypatch.setattr("zotero_mcp.batch_runner._TASKS_DIR", tmp_path / "batch_tasks")
        monkeypatch.setattr(ads_client, "is_available", lambda: True)
        # Mock _get_write_client so the tool passes the client-availability check
        # without needing real ZOTERO_API_KEY/ZOTERO_LOCAL env vars. Pass
        # clients=(read_zot, write_zot) to inject specific mocks per test.
        if clients is None:
            clients = (MagicMock(), MagicMock())
        monkeypatch.setattr(helpers_mod, "_get_write_client", lambda ctx: clients)

    def test_returns_error_when_browser_fetch_disabled(self, monkeypatch, tmp_path, dummy_ctx):
        self._setup_common(monkeypatch, tmp_path)
        monkeypatch.setattr(bfc, "load_browser_fetch_config", lambda: {"enabled": False, "debug_port": 9222})
        monkeypatch.setattr(bfc, "is_browser_fetch_enabled", lambda cfg: cfg.get("enabled", False))

        from zotero_mcp.tools.browser_fetch import upgrade_preprint_pdfs_via_browser

        result = upgrade_preprint_pdfs_via_browser(ctx=dummy_ctx)
        assert "browser_fetch is not enabled" in result

    def test_returns_error_when_devtools_unreachable(self, monkeypatch, tmp_path, dummy_ctx):
        self._setup_common(monkeypatch, tmp_path)
        monkeypatch.setattr(bfc, "load_browser_fetch_config", lambda: {"enabled": True, "debug_port": 9222})
        monkeypatch.setattr(bfc, "is_browser_fetch_enabled", lambda cfg: True)
        monkeypatch.setattr(bfc.DevToolsClient, "is_reachable", lambda self: False)

        from zotero_mcp.tools.browser_fetch import upgrade_preprint_pdfs_via_browser

        result = upgrade_preprint_pdfs_via_browser(ctx=dummy_ctx)
        assert "no browser found" in result
        assert "9222" in result

    @patch("zotero_mcp.tools.write._upgrade_single_preprint")
    def test_http_first_skips_browser_on_success(
        self, mock_upgrade, monkeypatch, tmp_path, dummy_ctx,
    ):
        """When HTTP cascade succeeds, the browser fetcher is never called."""
        preprint = _make_preprint_item()
        read_zot = MagicMock()

        def items_side_effect(*, itemType=None, **_kw):
            return [preprint] if itemType == "preprint" else []

        read_zot.items.side_effect = items_side_effect
        write_zot = MagicMock()
        write_zot.item.return_value = {
            "key": "PRE1", "version": 2,
            "data": {
                "itemType": "journalArticle",
                "DOI": "10.1088/0004-637X/769/2/127",
                "extra": "arXiv:2401.12345\nbibcode: 2013ApJ...769..127L",
            },
        }
        self._setup_common(monkeypatch, tmp_path, clients=(read_zot, write_zot))
        monkeypatch.setattr(bfc, "load_browser_fetch_config", lambda: {
            "enabled": True, "debug_port": 9222, "page_wait_seconds": 0,
        })
        monkeypatch.setattr(bfc, "is_browser_fetch_enabled", lambda cfg: True)
        monkeypatch.setattr(bfc.DevToolsClient, "is_reachable", lambda self: True)
        mock_upgrade.return_value = {"key": "PRE1", "status": "upgraded", "details": "", "error": ""}

        from zotero_mcp.tools.browser_fetch import upgrade_preprint_pdfs_via_browser

        # HTTP cascade succeeds
        cascade_mock = MagicMock(return_value="PDF attached (source: Sci-Hub)")
        monkeypatch.setattr(helpers_mod, "_try_attach_oa_pdf", cascade_mock)
        monkeypatch.setattr(helpers_mod, "_list_pdf_attachment_keys", lambda wz, k: ["OLD_PDF"])
        trash_mock = MagicMock(return_value=1)
        monkeypatch.setattr(helpers_mod, "_trash_pdf_attachments", trash_mock)

        # Browser fetcher must NOT be called
        browser_mock = MagicMock(return_value=(b"%PDF-1.4 ", "url", "should_not_happen"))
        monkeypatch.setattr(bfc, "fetch_publisher_pdf_via_browser", browser_mock)

        result = upgrade_preprint_pdfs_via_browser(ctx=dummy_ctx)
        assert "started" in result.lower() or "⏳" in result
        task_id = _extract_task_id(result)
        loaded = _wait_for_task(task_id)
        assert loaded is not None
        assert loaded.status == "completed"
        assert loaded.succeeded == 1
        assert loaded.failed == 0
        # Verify pub_only=True was passed to the HTTP cascade
        assert cascade_mock.call_args.kwargs.get("pub_only") is True
        # Browser fetcher was never called
        browser_mock.assert_not_called()

    @patch("zotero_mcp.tools.write._upgrade_single_preprint")
    def test_browser_fallback_on_http_failure(
        self, mock_upgrade, monkeypatch, tmp_path, dummy_ctx,
    ):
        """HTTP fails -> browser fetcher is called and succeeds."""
        preprint = _make_preprint_item()
        read_zot = MagicMock()

        def items_side_effect(*, itemType=None, **_kw):
            return [preprint] if itemType == "preprint" else []

        read_zot.items.side_effect = items_side_effect
        write_zot = MagicMock()
        write_zot.item.return_value = {
            "key": "PRE1", "version": 2,
            "data": {
                "itemType": "journalArticle",
                "DOI": "10.1088/0004-637X/769/2/127",
                "extra": "arXiv:2401.12345\nbibcode: 2013ApJ...769..127L",
            },
        }
        self._setup_common(monkeypatch, tmp_path, clients=(read_zot, write_zot))
        monkeypatch.setattr(bfc, "load_browser_fetch_config", lambda: {
            "enabled": True, "debug_port": 9222, "page_wait_seconds": 0,
        })
        monkeypatch.setattr(bfc, "is_browser_fetch_enabled", lambda cfg: True)
        monkeypatch.setattr(bfc.DevToolsClient, "is_reachable", lambda self: True)
        mock_upgrade.return_value = {"key": "PRE1", "status": "upgraded", "details": "", "error": ""}

        from zotero_mcp.tools.browser_fetch import upgrade_preprint_pdfs_via_browser

        # HTTP cascade fails (no PDF found)
        cascade_mock = MagicMock(return_value="no open-access PDF found (checked ...)")
        monkeypatch.setattr(helpers_mod, "_try_attach_oa_pdf", cascade_mock)
        monkeypatch.setattr(helpers_mod, "_list_pdf_attachment_keys", lambda wz, k: ["OLD_PDF"])
        trash_mock = MagicMock(return_value=1)
        monkeypatch.setattr(helpers_mod, "_trash_pdf_attachments", trash_mock)

        # Browser fetcher succeeds
        pdf_bytes = b"%PDF-1.4 " + b"x" * 3000
        browser_mock = MagicMock(return_value=(pdf_bytes, "https://publisher.com/p.pdf", "generic_in_page_fetch"))
        monkeypatch.setattr(bfc, "fetch_publisher_pdf_via_browser", browser_mock)

        # attachment_both mock
        write_zot.attachment_both.return_value = {"success": [{"key": "NEW_PDF"}]}

        result = upgrade_preprint_pdfs_via_browser(ctx=dummy_ctx)
        task_id = _extract_task_id(result)
        loaded = _wait_for_task(task_id)
        assert loaded is not None
        assert loaded.status == "completed"
        assert loaded.succeeded == 1
        assert loaded.failed == 0
        # Browser was called
        browser_mock.assert_called_once()
        # Old PDF was trashed with only_keys (preserving the new browser-fetched one)
        assert trash_mock.called
        assert trash_mock.call_args.kwargs.get("only_keys") == {"OLD_PDF"}
        # attachment_both was called with the browser bytes
        assert write_zot.attachment_both.called

    @patch("zotero_mcp.tools.write._upgrade_single_preprint")
    def test_browser_failure_recorded(
        self, mock_upgrade, monkeypatch, tmp_path, dummy_ctx,
    ):
        """Both HTTP and browser fail -> item failed, old PDF NOT trashed."""
        preprint = _make_preprint_item()
        read_zot = MagicMock()

        def items_side_effect(*, itemType=None, **_kw):
            return [preprint] if itemType == "preprint" else []

        read_zot.items.side_effect = items_side_effect
        write_zot = MagicMock()
        write_zot.item.return_value = {
            "key": "PRE1", "version": 2,
            "data": {
                "itemType": "journalArticle",
                "DOI": "10.1088/0004-637X/769/2/127",
                "extra": "arXiv:2401.12345\nbibcode: 2013ApJ...769..127L",
            },
        }
        self._setup_common(monkeypatch, tmp_path, clients=(read_zot, write_zot))
        monkeypatch.setattr(bfc, "load_browser_fetch_config", lambda: {
            "enabled": True, "debug_port": 9222, "page_wait_seconds": 0,
        })
        monkeypatch.setattr(bfc, "is_browser_fetch_enabled", lambda cfg: True)
        monkeypatch.setattr(bfc.DevToolsClient, "is_reachable", lambda self: True)
        mock_upgrade.return_value = {"key": "PRE1", "status": "upgraded", "details": "", "error": ""}

        from zotero_mcp.tools.browser_fetch import upgrade_preprint_pdfs_via_browser

        # HTTP fails
        monkeypatch.setattr(helpers_mod, "_try_attach_oa_pdf",
                            MagicMock(return_value="no open-access PDF found"))
        monkeypatch.setattr(helpers_mod, "_list_pdf_attachment_keys", lambda wz, k: ["OLD_PDF"])
        trash_mock = MagicMock()
        monkeypatch.setattr(helpers_mod, "_trash_pdf_attachments", trash_mock)

        # Browser also fails
        browser_mock = MagicMock(return_value=(None, "https://publisher.com", "challenge_page"))
        monkeypatch.setattr(bfc, "fetch_publisher_pdf_via_browser", browser_mock)

        result = upgrade_preprint_pdfs_via_browser(ctx=dummy_ctx)
        task_id = _extract_task_id(result)
        loaded = _wait_for_task(task_id)
        assert loaded is not None
        assert loaded.status == "completed"
        assert loaded.succeeded == 0
        assert loaded.failed == 1
        # Old PDF was NOT trashed
        trash_mock.assert_not_called()
        # attachment_both was NOT called (no bytes to attach)
        write_zot.attachment_both.assert_not_called()
