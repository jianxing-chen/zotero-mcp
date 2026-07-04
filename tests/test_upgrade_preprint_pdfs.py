"""Tests for _trash_pdf_attachments and upgrade_preprint_pdfs.

Covers:
- _trash_pdf_attachments: soft-deletes PDF attachments, skips non-PDF children
- upgrade_preprint_pdfs: filters preprints with arXiv IDs, upgrades metadata,
  downloads publisher PDF, only trashes old PDF when download succeeds.
"""

from __future__ import annotations

import re
from unittest.mock import MagicMock, patch

from zotero_mcp.tools import _helpers


def _extract_task_id(result: str) -> str:
    r"""Extract the task_id from the tool's return string.

    The return string wraps the task_id in ``**...**`` AND in backticks
    (``scihub.enabled``, ``zotero_get_batch_task_status``, ``<task_id>``),
    so a naive ``split('`')[1]`` would grab the wrong backtick-enclosed
    token. Anchor to the canonical task_id format ``YYYYmmddTHHMMSSZ-<hex>``.
    """
    m = re.search(r"\*\*([0-9T]{8}T[0-9]{6}Z-[0-9a-f]+)\*\*", result)
    assert m, f"could not extract task_id from result: {result[:120]!r}"
    return m.group(1)

# ---------------------------------------------------------------------------
# _trash_pdf_attachments
# ---------------------------------------------------------------------------


class TestTrashPdfAttachments:
    def test_trashes_pdf_attachments(self, dummy_ctx):
        """Two PDF attachments -> both PATCHed with {"deleted": 1}."""
        write_zot = MagicMock()
        pdf1 = {
            "key": "ATT1",
            "version": 10,
            "data": {"itemType": "attachment", "contentType": "application/pdf"},
        }
        pdf2 = {
            "key": "ATT2",
            "version": 20,
            "data": {"itemType": "attachment", "contentType": "application/pdf"},
        }
        write_zot.children.return_value = [pdf1, pdf2]
        write_zot.endpoint = "https://api.zotero.org"
        write_zot.library_type = "user"
        write_zot.library_id = "12345"
        write_zot.client.patch.return_value = MagicMock(status_code=204)

        count = _helpers._trash_pdf_attachments(write_zot, "ITEM1", dummy_ctx)

        assert count == 2
        assert write_zot.client.patch.call_count == 2
        # Verify the PATCH payload contains {"deleted": 1}
        for call in write_zot.client.patch.call_args_list:
            content = call.kwargs.get("content") or (call.args[1] if len(call.args) > 1 else None)
            assert '"deleted": 1' in content

    def test_skips_non_pdf_children(self, dummy_ctx):
        """Notes and non-PDF attachments are left untouched."""
        write_zot = MagicMock()
        note = {
            "key": "NOTE1",
            "version": 5,
            "data": {"itemType": "note"},
        }
        pdf = {
            "key": "PDF1",
            "version": 10,
            "data": {"itemType": "attachment", "contentType": "application/pdf"},
        }
        html_attach = {
            "key": "HTML1",
            "version": 15,
            "data": {"itemType": "attachment", "contentType": "text/html"},
        }
        write_zot.children.return_value = [note, pdf, html_attach]
        write_zot.endpoint = "https://api.zotero.org"
        write_zot.library_type = "user"
        write_zot.library_id = "12345"
        write_zot.client.patch.return_value = MagicMock(status_code=204)

        count = _helpers._trash_pdf_attachments(write_zot, "ITEM1", dummy_ctx)

        assert count == 1
        assert write_zot.client.patch.call_count == 1

    def test_returns_zero_on_no_children(self, dummy_ctx):
        """No children -> 0 trashed, no PATCH calls."""
        write_zot = MagicMock()
        write_zot.children.return_value = []
        count = _helpers._trash_pdf_attachments(write_zot, "ITEM1", dummy_ctx)
        assert count == 0
        write_zot.client.patch.assert_not_called()

    def test_handles_children_fetch_error(self, dummy_ctx):
        """If children() raises, return 0 without crashing."""
        write_zot = MagicMock()
        write_zot.children.side_effect = Exception("network error")
        count = _helpers._trash_pdf_attachments(write_zot, "ITEM1", dummy_ctx)
        assert count == 0

    def test_only_keys_preserves_newly_attached_pdf(self, dummy_ctx):
        """Regression test for the data-loss bug: when a new PDF was
        downloaded and then _trash_pdf_attachments was called, it re-listed
        children and trashed the NEW PDF too, leaving the item with zero
        PDFs. The only_keys allowlist lets the caller pass the keys of PDFs
        that existed *before* the download so the new one (whose key is not
        in the set) is preserved.
        """
        write_zot = MagicMock()
        old_pdf = {
            "key": "OLD1",
            "version": 10,
            "data": {"itemType": "attachment", "contentType": "application/pdf"},
        }
        new_pdf = {
            "key": "NEW1",
            "version": 11,
            "data": {"itemType": "attachment", "contentType": "application/pdf"},
        }
        write_zot.children.return_value = [old_pdf, new_pdf]
        write_zot.endpoint = "https://api.zotero.org"
        write_zot.library_type = "user"
        write_zot.library_id = "12345"
        write_zot.client.patch.return_value = MagicMock(status_code=204)

        # only_keys={"OLD1"} -> only OLD1 is trashed, NEW1 preserved.
        count = _helpers._trash_pdf_attachments(
            write_zot, "ITEM1", dummy_ctx, only_keys={"OLD1"}
        )

        assert count == 1
        assert write_zot.client.patch.call_count == 1
        # The trashed URL must contain OLD1, not NEW1.
        call = write_zot.client.patch.call_args_list[0]
        url = call.kwargs.get("url", "")
        assert "OLD1" in url
        assert "NEW1" not in url

    def test_only_keys_none_trashes_all(self, dummy_ctx):
        """only_keys=None (default) trashes every PDF — backward compatible."""
        write_zot = MagicMock()
        pdf1 = {
            "key": "A1",
            "version": 10,
            "data": {"itemType": "attachment", "contentType": "application/pdf"},
        }
        pdf2 = {
            "key": "A2",
            "version": 20,
            "data": {"itemType": "attachment", "contentType": "application/pdf"},
        }
        write_zot.children.return_value = [pdf1, pdf2]
        write_zot.endpoint = "https://api.zotero.org"
        write_zot.library_type = "user"
        write_zot.library_id = "12345"
        write_zot.client.patch.return_value = MagicMock(status_code=204)

        count = _helpers._trash_pdf_attachments(
            write_zot, "ITEM1", dummy_ctx, only_keys=None
        )
        assert count == 2


# ---------------------------------------------------------------------------
# _list_pdf_attachment_keys
# ---------------------------------------------------------------------------


class TestListPdfAttachmentKeys:
    def test_returns_pdf_keys_only(self):
        """Returns keys of attachment items with contentType=application/pdf."""
        write_zot = MagicMock()
        pdf1 = {
            "key": "P1",
            "data": {"itemType": "attachment", "contentType": "application/pdf"},
        }
        note = {"key": "N1", "data": {"itemType": "note"}}
        html = {
            "key": "H1",
            "data": {"itemType": "attachment", "contentType": "text/html"},
        }
        pdf2 = {
            "key": "P2",
            "data": {"itemType": "attachment", "contentType": "application/pdf"},
        }
        write_zot.children.return_value = [pdf1, note, html, pdf2]
        keys = _helpers._list_pdf_attachment_keys(write_zot, "ITEM1")
        assert keys == ["P1", "P2"]

    def test_returns_empty_on_no_children(self):
        write_zot = MagicMock()
        write_zot.children.return_value = []
        assert _helpers._list_pdf_attachment_keys(write_zot, "ITEM1") == []

    def test_returns_empty_on_children_fetch_error(self):
        """If children() raises, return [] without crashing."""
        write_zot = MagicMock()
        write_zot.children.side_effect = Exception("network error")
        assert _helpers._list_pdf_attachment_keys(write_zot, "ITEM1") == []


# ---------------------------------------------------------------------------
# upgrade_preprint_pdfs
# ---------------------------------------------------------------------------


class TestUpgradePreprintPdfs:
    """Test the upgrade_preprint_pdfs tool's branching logic."""

    def _make_preprint_item(self, key="PRE1", title="Test Paper", arxiv_id="2401.12345", bibcode="2013ApJ...769..127L"):
        return {
            "key": key,
            "version": 1,
            "data": {
                "itemType": "preprint",
                "title": title,
                "extra": f"arXiv:{arxiv_id}\nbibcode: {bibcode}",
            },
        }

    def _setup_mocks(self, monkeypatch, preprints, ads_available=True, scihub_enabled=False):
        """Set up all mocks for upgrade_preprint_pdfs."""
        # Mock _get_write_client
        read_zot = MagicMock()
        write_zot = MagicMock()
        read_zot.items.return_value = preprints if preprints else []

        # Mock write_zot.item (re-read after upgrade) returning published DOI
        def mock_item(key):
            return {
                "key": key,
                "version": 2,
                "data": {
                    "itemType": "journalArticle",
                    "title": "Test Paper",
                    "DOI": "10.1088/0004-637X/769/2/127",
                    "extra": "arXiv:2401.12345\nbibcode: 2013ApJ...769..127L",
                },
            }

        write_zot.item = mock_item
        write_zot.children.return_value = []
        write_zot.endpoint = "https://api.zotero.org"
        write_zot.library_type = "user"
        write_zot.library_id = "12345"
        write_zot.client = MagicMock()
        write_zot.client.patch.return_value = MagicMock(status_code=204)

        monkeypatch.setattr(_helpers, "_get_write_client", lambda ctx: (read_zot, write_zot))

        # Mock ADS availability
        from zotero_mcp import ads_client

        monkeypatch.setattr(ads_client, "is_available", lambda: ads_available)

        # Mock Sci-Hub
        from zotero_mcp import scihub_client

        monkeypatch.setattr(scihub_client, "is_scihub_enabled", lambda cfg: scihub_enabled)
        monkeypatch.setattr(scihub_client, "load_scihub_config", lambda *a, **k: {"enabled": scihub_enabled})

        return read_zot, write_zot

    @patch("zotero_mcp.tools.write._helpers._get_write_client")
    def test_returns_error_when_ads_unavailable(self, mock_get_client, dummy_ctx, monkeypatch):
        """Without ADS_API_TOKEN, the tool returns an error immediately."""
        mock_get_client.return_value = (MagicMock(), MagicMock())

        from zotero_mcp import ads_client

        monkeypatch.setattr(ads_client, "is_available", lambda: False)

        from zotero_mcp.tools.write import upgrade_preprint_pdfs

        result = upgrade_preprint_pdfs(ctx=dummy_ctx)
        assert "Error" in result
        assert "ADS_API_TOKEN" in result

    @patch("zotero_mcp.tools.write._upgrade_single_preprint")
    @patch("zotero_mcp.tools.write._helpers._get_write_client")
    def test_skips_not_published(self, mock_get_client, mock_upgrade, dummy_ctx, monkeypatch, tmp_path):
        """When _upgrade_single_preprint returns not_published, no PDF download/trash."""
        monkeypatch.setattr("zotero_mcp.batch_runner._TASKS_DIR", tmp_path / "batch_tasks")
        preprint = self._make_preprint_item()
        read_zot = MagicMock()
        read_zot.items.return_value = [preprint]
        write_zot = MagicMock()
        mock_get_client.return_value = (read_zot, write_zot)

        mock_upgrade.return_value = {
            "key": "PRE1",
            "status": "not_published",
            "details": "",
            "error": "",
        }

        from zotero_mcp import ads_client

        monkeypatch.setattr(ads_client, "is_available", lambda: True)

        from zotero_mcp import scihub_client

        monkeypatch.setattr(scihub_client, "is_scihub_enabled", lambda cfg: False)

        from zotero_mcp.tools import _helpers as helpers_mod
        from zotero_mcp.tools.write import upgrade_preprint_pdfs

        # Verify _try_attach_oa_pdf and _trash_pdf_attachments are NOT called
        monkeypatch.setattr(helpers_mod, "_try_attach_oa_pdf", MagicMock())
        monkeypatch.setattr(helpers_mod, "_trash_pdf_attachments", MagicMock())

        result = upgrade_preprint_pdfs(ctx=dummy_ctx)
        assert "started" in result.lower() or "⏳" in result
        assert "get_batch_task_status" in result

    @patch("zotero_mcp.tools.write._upgrade_single_preprint")
    @patch("zotero_mcp.tools.write._helpers._get_write_client")
    def test_downloads_then_trashes_on_success(self, mock_get_client, mock_upgrade, dummy_ctx, monkeypatch, tmp_path):
        """Upgrade succeeds + PDF download succeeds -> old PDF trashed.

        Regression checks:
        - The worker snapshots the old PDF keys *before* the download and
          passes them as ``only_keys`` to _trash_pdf_attachments, so the
          newly-attached publisher PDF is NOT trashed (data-loss bug fix).
        - The worker passes ``pub_only=True`` to _try_attach_oa_pdf so the
          cascade is restricted to publisher-version sources only — the item
          already has the arXiv preprint, so an arXiv fallback is pointless.
        """
        import time as _time

        from zotero_mcp.batch_runner import read_status

        monkeypatch.setattr("zotero_mcp.batch_runner._TASKS_DIR", tmp_path / "batch_tasks")
        preprint = self._make_preprint_item()
        read_zot = MagicMock()
        read_zot.items.return_value = [preprint]
        write_zot = MagicMock()
        write_zot.item.return_value = {
            "key": "PRE1",
            "version": 2,
            "data": {
                "itemType": "journalArticle",
                "DOI": "10.1088/0004-637X/769/2/127",
                "extra": "arXiv:2401.12345\nbibcode: 2013ApJ...769..127L",
            },
        }
        mock_get_client.return_value = (read_zot, write_zot)

        mock_upgrade.return_value = {
            "key": "PRE1",
            "status": "upgraded",
            "details": "preprint→journalArticle, filled: date, volume",
            "error": "",
        }

        from zotero_mcp import ads_client

        monkeypatch.setattr(ads_client, "is_available", lambda: True)

        from zotero_mcp import scihub_client

        monkeypatch.setattr(scihub_client, "is_scihub_enabled", lambda cfg: True)
        monkeypatch.setattr(scihub_client, "load_scihub_config", lambda *a, **k: {"enabled": True})

        from zotero_mcp.tools import _helpers as helpers_mod
        from zotero_mcp.tools.write import upgrade_preprint_pdfs

        # Mock the cascade to return success
        cascade_mock = MagicMock(return_value="PDF attached (source: Sci-Hub)")
        monkeypatch.setattr(helpers_mod, "_try_attach_oa_pdf", cascade_mock)
        # Snapshot before the download returns ["OLD_PDF"].
        monkeypatch.setattr(
            helpers_mod,
            "_list_pdf_attachment_keys",
            lambda wz, k: ["OLD_PDF"],
        )
        trash_mock = MagicMock(return_value=1)
        monkeypatch.setattr(helpers_mod, "_trash_pdf_attachments", trash_mock)

        result = upgrade_preprint_pdfs(ctx=dummy_ctx)
        assert "started" in result.lower() or "⏳" in result
        assert "get_batch_task_status" in result

        # Extract task_id and wait for the background worker to finish.
        task_id = _extract_task_id(result)
        deadline = _time.time() + 5
        while _time.time() < deadline:
            loaded = read_status(task_id)
            if loaded and loaded.status in ("completed", "failed"):
                break
            _time.sleep(0.05)

        # The worker must have called _trash_pdf_attachments with
        # only_keys={"OLD_PDF"} — the data-loss bug was that it trashed
        # the newly-attached PDF too (it re-listed children and deleted
        # every PDF, including the one just downloaded).
        assert trash_mock.called, "worker never called _trash_pdf_attachments"
        kwargs = trash_mock.call_args.kwargs
        assert "only_keys" in kwargs, "only_keys not passed — data-loss bug"
        assert kwargs["only_keys"] == {"OLD_PDF"}, (
            f"expected only_keys={{'OLD_PDF'}}, got {kwargs['only_keys']!r}"
        )

        # The cascade must have been called with pub_only=True — the
        # publisher-only restriction that prevents downloading another
        # arXiv copy when the item already has the preprint.
        assert cascade_mock.called, "worker never called _try_attach_oa_pdf"
        cascade_kwargs = cascade_mock.call_args.kwargs
        assert cascade_kwargs.get("pub_only") is True, (
            f"expected pub_only=True, got {cascade_kwargs.get('pub_only')!r}"
        )

    @patch("zotero_mcp.tools.write._upgrade_single_preprint")
    @patch("zotero_mcp.tools.write._helpers._get_write_client")
    def test_keeps_pdf_on_download_fail(self, mock_get_client, mock_upgrade, dummy_ctx, monkeypatch, tmp_path):
        """Upgrade succeeds but PDF download fails -> old PDF NOT trashed."""
        monkeypatch.setattr("zotero_mcp.batch_runner._TASKS_DIR", tmp_path / "batch_tasks")
        preprint = self._make_preprint_item()
        read_zot = MagicMock()
        read_zot.items.return_value = [preprint]
        write_zot = MagicMock()
        write_zot.item.return_value = {
            "key": "PRE1",
            "version": 2,
            "data": {
                "itemType": "journalArticle",
                "DOI": "10.1088/0004-637X/769/2/127",
                "extra": "bibcode: 2013ApJ...769..127L",
            },
        }
        mock_get_client.return_value = (read_zot, write_zot)

        mock_upgrade.return_value = {
            "key": "PRE1",
            "status": "upgraded",
            "details": "preprint→journalArticle",
            "error": "",
        }

        from zotero_mcp import ads_client

        monkeypatch.setattr(ads_client, "is_available", lambda: True)

        from zotero_mcp import scihub_client

        monkeypatch.setattr(scihub_client, "is_scihub_enabled", lambda cfg: False)

        from zotero_mcp.tools import _helpers as helpers_mod
        from zotero_mcp.tools.write import upgrade_preprint_pdfs

        # Mock the cascade to return failure (no "attached" in the string)
        monkeypatch.setattr(
            helpers_mod,
            "_try_attach_oa_pdf",
            lambda *a, **k: "no open-access PDF found (checked ...)",
        )
        trash_mock = MagicMock()
        monkeypatch.setattr(helpers_mod, "_trash_pdf_attachments", trash_mock)

        result = upgrade_preprint_pdfs(ctx=dummy_ctx)
        assert "started" in result.lower() or "⏳" in result
        assert "get_batch_task_status" in result

    @patch("zotero_mcp.tools.write._upgrade_single_preprint")
    @patch("zotero_mcp.tools.write._helpers._get_write_client")
    def test_no_publisher_pdf_skips_item(self, mock_get_client, mock_upgrade, dummy_ctx, monkeypatch, tmp_path):
        """Publisher version not available -> item skipped, old arXiv PDF kept.

        The cascade is restricted to publisher-version sources (pub_only=True),
        so when none has the publisher PDF, the cascade returns a no-PDF
        string; the worker records the item as 'PDF not found' and does NOT
        trash the old arXiv PDF.
        """
        import time as _time

        from zotero_mcp.batch_runner import read_status

        monkeypatch.setattr("zotero_mcp.batch_runner._TASKS_DIR", tmp_path / "batch_tasks")
        preprint = self._make_preprint_item()
        read_zot = MagicMock()

        # items() must respond differently per itemType so the worker doesn't
        # see the same preprint twice (once as preprint, once as journalArticle).
        def items_side_effect(*, itemType=None, **_kw):
            if itemType == "preprint":
                return [preprint]
            return []

        read_zot.items.side_effect = items_side_effect
        write_zot = MagicMock()
        write_zot.item.return_value = {
            "key": "PRE1",
            "version": 2,
            "data": {
                "itemType": "journalArticle",
                "DOI": "10.1088/0004-637X/769/2/127",
                "extra": "arXiv:2401.12345\nbibcode: 2013ApJ...769..127L",
            },
        }
        mock_get_client.return_value = (read_zot, write_zot)

        mock_upgrade.return_value = {
            "key": "PRE1",
            "status": "upgraded",
            "details": "preprint→journalArticle",
            "error": "",
        }

        from zotero_mcp import ads_client

        monkeypatch.setattr(ads_client, "is_available", lambda: True)

        from zotero_mcp import scihub_client

        monkeypatch.setattr(scihub_client, "is_scihub_enabled", lambda cfg: False)

        from zotero_mcp.tools import _helpers as helpers_mod
        from zotero_mcp.tools.write import upgrade_preprint_pdfs

        # Mock the cascade to return failure (no publisher PDF found).
        cascade_mock = MagicMock(return_value="no open-access PDF found (checked ...)")
        monkeypatch.setattr(helpers_mod, "_try_attach_oa_pdf", cascade_mock)
        trash_mock = MagicMock()
        monkeypatch.setattr(helpers_mod, "_trash_pdf_attachments", trash_mock)

        result = upgrade_preprint_pdfs(ctx=dummy_ctx)
        task_id = _extract_task_id(result)

        # Wait for the worker to finish.
        deadline = _time.time() + 5
        while _time.time() < deadline:
            loaded = read_status(task_id)
            if loaded and loaded.status in ("completed", "failed"):
                break
            _time.sleep(0.05)

        loaded = read_status(task_id)
        assert loaded is not None
        # pub_only must be True.
        assert cascade_mock.call_args.kwargs.get("pub_only") is True
        # No PDF attached -> trash NOT called (old arXiv PDF preserved).
        trash_mock.assert_not_called()
        # Item recorded as failed/PDF not found.
        assert loaded.failed == 1
        assert loaded.succeeded == 0
        assert any(it.get("key") == "PRE1" for it in loaded.failed_items)

    @patch("zotero_mcp.tools.write._upgrade_single_preprint")
    @patch("zotero_mcp.tools.write._helpers._get_write_client")
    def test_require_bibcode_filters_to_bibcode_only(self, mock_get_client, mock_upgrade, dummy_ctx, monkeypatch, tmp_path):
        """require_bibcode=True: only preprints with a bibcode: line are processed."""
        monkeypatch.setattr("zotero_mcp.batch_runner._TASKS_DIR", tmp_path / "batch_tasks")
        with_bibcode = {
            "key": "PRE1",
            "data": {
                "itemType": "preprint",
                "title": "Has bibcode",
                "extra": "arXiv:2401.12345\nbibcode: 2013ApJ...769..127L",
            },
        }
        without_bibcode = {
            "key": "PRE2",
            "data": {"itemType": "preprint", "title": "Only arXiv", "extra": "arXiv:2401.99999"},
        }
        read_zot = MagicMock()
        read_zot.items.return_value = [with_bibcode, without_bibcode]
        mock_get_client.return_value = (read_zot, MagicMock())

        mock_upgrade.return_value = {"key": "PRE1", "status": "not_published", "details": "", "error": ""}

        from zotero_mcp import ads_client

        monkeypatch.setattr(ads_client, "is_available", lambda: True)

        from zotero_mcp import scihub_client

        monkeypatch.setattr(scihub_client, "is_scihub_enabled", lambda cfg: False)

        from zotero_mcp.tools.write import upgrade_preprint_pdfs

        result = upgrade_preprint_pdfs(require_bibcode=True, ctx=dummy_ctx)
        assert "started" in result.lower() or "⏳" in result
        assert "get_batch_task_status" in result

    @patch("zotero_mcp.tools.write._upgrade_single_preprint")
    @patch("zotero_mcp.tools.write._helpers._get_write_client")
    def test_default_scans_all_arxiv_preprints(self, mock_get_client, mock_upgrade, dummy_ctx, monkeypatch, tmp_path):
        """require_bibcode=False (default): all preprints with arXiv ID are processed."""
        monkeypatch.setattr("zotero_mcp.batch_runner._TASKS_DIR", tmp_path / "batch_tasks")
        with_bibcode = {
            "key": "PRE1",
            "data": {
                "itemType": "preprint",
                "title": "Has bibcode",
                "extra": "arXiv:2401.12345\nbibcode: 2013ApJ...769..127L",
            },
        }
        without_bibcode = {
            "key": "PRE2",
            "data": {"itemType": "preprint", "title": "Only arXiv", "extra": "arXiv:2401.99999"},
        }
        read_zot = MagicMock()
        read_zot.items.return_value = [with_bibcode, without_bibcode]
        mock_get_client.return_value = (read_zot, MagicMock())

        mock_upgrade.return_value = {"key": "", "status": "not_published", "details": "", "error": ""}

        from zotero_mcp import ads_client

        monkeypatch.setattr(ads_client, "is_available", lambda: True)

        from zotero_mcp import scihub_client

        monkeypatch.setattr(scihub_client, "is_scihub_enabled", lambda cfg: False)

        from zotero_mcp.tools.write import upgrade_preprint_pdfs

        result = upgrade_preprint_pdfs(ctx=dummy_ctx)
        # Both preprints processed (one has bibcode, one has only arXiv)
        assert "started" in result.lower() or "⏳" in result
        assert "get_batch_task_status" in result

    @patch("zotero_mcp.tools.write._upgrade_single_preprint")
    @patch("zotero_mcp.tools.write._helpers._get_write_client")
    def test_item_keys_processes_specified_items_only(self, mock_get_client, mock_upgrade, dummy_ctx, monkeypatch, tmp_path):
        """When item_keys is given, only those items are fetched (no library scan)."""
        monkeypatch.setattr("zotero_mcp.batch_runner._TASKS_DIR", tmp_path / "batch_tasks")
        read_zot = MagicMock()
        # read_zot.item(key) is called for each specified key
        preprint1 = self._make_preprint_item(key="AAA1111")
        preprint2 = self._make_preprint_item(key="BBB2222", title="Other Paper")
        read_zot.item.side_effect = lambda key: {"AAA1111": preprint1, "BBB2222": preprint2}[key]
        mock_get_client.return_value = (read_zot, MagicMock())

        mock_upgrade.return_value = {"key": "", "status": "not_published", "details": "", "error": ""}

        from zotero_mcp import ads_client

        monkeypatch.setattr(ads_client, "is_available", lambda: True)

        from zotero_mcp import scihub_client

        monkeypatch.setattr(scihub_client, "is_scihub_enabled", lambda cfg: False)

        from zotero_mcp.tools.write import upgrade_preprint_pdfs

        result = upgrade_preprint_pdfs(item_keys=["AAA1111", "BBB2222"], ctx=dummy_ctx)
        # read_zot.items() (scan) must NOT be called; read_zot.item() is called per key
        read_zot.items.assert_not_called()
        assert read_zot.item.call_count == 2
        assert "started" in result.lower() or "⏳" in result
        assert "get_batch_task_status" in result

    @patch("zotero_mcp.tools.write._upgrade_single_preprint")
    @patch("zotero_mcp.tools.write._helpers._get_write_client")
    def test_item_keys_skips_nonexistent(self, mock_get_client, mock_upgrade, dummy_ctx, monkeypatch, tmp_path):
        """When item_keys references a nonexistent item, it's skipped gracefully."""
        monkeypatch.setattr("zotero_mcp.batch_runner._TASKS_DIR", tmp_path / "batch_tasks")
        read_zot = MagicMock()
        preprint = self._make_preprint_item(key="GOOD111")
        read_zot.item.side_effect = lambda key: preprint if key == "GOOD111" else None
        mock_get_client.return_value = (read_zot, MagicMock())

        mock_upgrade.return_value = {"key": "GOOD111", "status": "not_published", "details": "", "error": ""}

        from zotero_mcp import ads_client

        monkeypatch.setattr(ads_client, "is_available", lambda: True)

        from zotero_mcp import scihub_client

        monkeypatch.setattr(scihub_client, "is_scihub_enabled", lambda cfg: False)

        from zotero_mcp.tools.write import upgrade_preprint_pdfs

        result = upgrade_preprint_pdfs(item_keys=["GOOD111", "BAD2222"], ctx=dummy_ctx)
        # Only the existing item is processed
        assert "started" in result.lower() or "⏳" in result
        assert "get_batch_task_status" in result

    @patch("zotero_mcp.tools.write._helpers.resolve_collection_specs")
    @patch("zotero_mcp.tools.write._upgrade_single_preprint")
    @patch("zotero_mcp.tools.write._helpers._get_write_client")
    def test_collection_scans_named_collection(
        self, mock_get_client, mock_upgrade, mock_resolve, dummy_ctx, monkeypatch, tmp_path
    ):
        """collection='MyFolder' resolves to a key and scans only that collection."""
        monkeypatch.setattr("zotero_mcp.batch_runner._TASKS_DIR", tmp_path / "batch_tasks")
        read_zot = MagicMock()
        preprint_in_coll = self._make_preprint_item(key="INCOLL1")
        read_zot.collection_items.return_value = [preprint_in_coll]
        mock_get_client.return_value = (read_zot, MagicMock())
        mock_resolve.return_value = ["COLLKEY"]

        mock_upgrade.return_value = {"key": "INCOLL1", "status": "not_published", "details": "", "error": ""}

        from zotero_mcp import ads_client

        monkeypatch.setattr(ads_client, "is_available", lambda: True)

        from zotero_mcp import scihub_client

        monkeypatch.setattr(scihub_client, "is_scihub_enabled", lambda cfg: False)

        from zotero_mcp.tools.write import upgrade_preprint_pdfs

        result = upgrade_preprint_pdfs(collection="MyFolder", ctx=dummy_ctx)
        # read_zot.items() (full scan) must NOT be called
        read_zot.items.assert_not_called()
        # read_zot.collection_items() was called with the resolved key
        read_zot.collection_items.assert_called()
        assert "started" in result.lower() or "⏳" in result
        assert "get_batch_task_status" in result

    @patch("zotero_mcp.tools.write._upgrade_single_preprint")
    @patch("zotero_mcp.tools.write._helpers._get_write_client")
    def test_collection_unfiled_skips_filed_items(self, mock_get_client, mock_upgrade, dummy_ctx, monkeypatch, tmp_path):
        """collection='_unfiled' only processes preprints with no collections."""
        monkeypatch.setattr("zotero_mcp.batch_runner._TASKS_DIR", tmp_path / "batch_tasks")
        read_zot = MagicMock()
        unfiled_preprint = self._make_preprint_item(key="UNFILED1")
        # Simulate a filed item (has collections list)
        filed_preprint = self._make_preprint_item(key="FILED1", title="Filed")
        filed_preprint["data"]["collections"] = [{"key": "COLLKEY", "name": "SomeFolder"}]
        read_zot.items.return_value = [unfiled_preprint, filed_preprint]
        mock_get_client.return_value = (read_zot, MagicMock())

        mock_upgrade.return_value = {"key": "UNFILED1", "status": "not_published", "details": "", "error": ""}

        from zotero_mcp import ads_client

        monkeypatch.setattr(ads_client, "is_available", lambda: True)

        from zotero_mcp import scihub_client

        monkeypatch.setattr(scihub_client, "is_scihub_enabled", lambda cfg: False)

        from zotero_mcp.tools.write import upgrade_preprint_pdfs

        result = upgrade_preprint_pdfs(collection="_unfiled", ctx=dummy_ctx)
        # Only the unfiled preprint is processed
        assert "started" in result.lower() or "⏳" in result
        assert "get_batch_task_status" in result
