"""Tests for _trash_pdf_attachments and upgrade_preprint_pdfs.

Covers:
- _trash_pdf_attachments: soft-deletes PDF attachments, skips non-PDF children
- upgrade_preprint_pdfs: filters preprints with arXiv IDs, upgrades metadata,
  downloads publisher PDF, only trashes old PDF when download succeeds.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from zotero_mcp.tools import _helpers

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
    def test_skips_not_published(self, mock_get_client, mock_upgrade, dummy_ctx, monkeypatch):
        """When _upgrade_single_preprint returns not_published, no PDF download/trash."""
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
        assert "Not yet published" in result
        helpers_mod._try_attach_oa_pdf.assert_not_called()
        helpers_mod._trash_pdf_attachments.assert_not_called()

    @patch("zotero_mcp.tools.write._upgrade_single_preprint")
    @patch("zotero_mcp.tools.write._helpers._get_write_client")
    def test_downloads_then_trashes_on_success(self, mock_get_client, mock_upgrade, dummy_ctx, monkeypatch):
        """Upgrade succeeds + PDF download succeeds -> old PDF trashed."""
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
        monkeypatch.setattr(
            helpers_mod,
            "_try_attach_oa_pdf",
            lambda *a, **k: "PDF attached (source: Sci-Hub)",
        )
        trash_mock = MagicMock(return_value=1)
        monkeypatch.setattr(helpers_mod, "_trash_pdf_attachments", trash_mock)

        result = upgrade_preprint_pdfs(ctx=dummy_ctx)
        assert "PDFs replaced with publisher version:** 1" in result
        trash_mock.assert_called_once()

    @patch("zotero_mcp.tools.write._upgrade_single_preprint")
    @patch("zotero_mcp.tools.write._helpers._get_write_client")
    def test_keeps_pdf_on_download_fail(self, mock_get_client, mock_upgrade, dummy_ctx, monkeypatch):
        """Upgrade succeeds but PDF download fails -> old PDF NOT trashed."""
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
        assert "PDF not found (kept arXiv PDF):** 1" in result
        trash_mock.assert_not_called()

    @patch("zotero_mcp.tools.write._upgrade_single_preprint")
    @patch("zotero_mcp.tools.write._helpers._get_write_client")
    def test_require_bibcode_filters_to_bibcode_only(self, mock_get_client, mock_upgrade, dummy_ctx, monkeypatch):
        """require_bibcode=True: only preprints with a bibcode: line are processed."""
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
        assert "Total preprints checked:** 1" in result
        mock_upgrade.assert_called_once()
        assert mock_upgrade.call_args[0][1] == "PRE1"

    @patch("zotero_mcp.tools.write._upgrade_single_preprint")
    @patch("zotero_mcp.tools.write._helpers._get_write_client")
    def test_default_scans_all_arxiv_preprints(self, mock_get_client, mock_upgrade, dummy_ctx, monkeypatch):
        """require_bibcode=False (default): all preprints with arXiv ID are processed."""
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
        assert "Total preprints checked:** 2" in result
        assert mock_upgrade.call_count == 2

    @patch("zotero_mcp.tools.write._upgrade_single_preprint")
    @patch("zotero_mcp.tools.write._helpers._get_write_client")
    def test_item_keys_processes_specified_items_only(self, mock_get_client, mock_upgrade, dummy_ctx, monkeypatch):
        """When item_keys is given, only those items are fetched (no library scan)."""
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
        assert "Total preprints checked:** 2" in result
        assert mock_upgrade.call_count == 2

    @patch("zotero_mcp.tools.write._upgrade_single_preprint")
    @patch("zotero_mcp.tools.write._helpers._get_write_client")
    def test_item_keys_skips_nonexistent(self, mock_get_client, mock_upgrade, dummy_ctx, monkeypatch):
        """When item_keys references a nonexistent item, it's skipped gracefully."""
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
        assert "Total preprints checked:** 1" in result
        mock_upgrade.assert_called_once()
        assert mock_upgrade.call_args[0][1] == "GOOD111"
