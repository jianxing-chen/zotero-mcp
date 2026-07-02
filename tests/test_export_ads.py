"""Tests for ADS export functionality."""

from unittest.mock import MagicMock, patch

from zotero_mcp.ads_client import SUPPORTED_EXPORT_FORMATS, export
from zotero_mcp.tools.ads import _bibcode_from_arxiv, _parse_arxiv_from_extra


class TestParseArxivFromExtra:
    """Tests for _parse_arxiv_from_extra — arXiv ID extraction from extra."""

    def test_new_style_id(self):
        assert _parse_arxiv_from_extra("arXiv:2401.12345") == "2401.12345"

    def test_new_style_with_classification(self):
        assert _parse_arxiv_from_extra("arXiv:2401.12345 [astro-ph.CO]") == "2401.12345"

    def test_old_style_id(self):
        assert _parse_arxiv_from_extra("arXiv:astro-ph/0501001") == "astro-ph/0501001"

    def test_multiline_extra_picks_first_arxiv(self):
        extra = "Some note\narXiv:2401.12345 [astro-ph.CO]\nDOI: 10.1234/abc"
        assert _parse_arxiv_from_extra(extra) == "2401.12345"

    def test_none_or_empty(self):
        assert _parse_arxiv_from_extra(None) is None
        assert _parse_arxiv_from_extra("") is None

    def test_no_arxiv_line(self):
        assert _parse_arxiv_from_extra("bibcode: 2024ApJ...968L..12A\nDOI: 10.1/x") is None


class TestBibcodeFromArxiv:
    """Tests for _bibcode_from_arxiv — ADS arXiv→bibcode lookup."""

    def test_returns_bibcode_on_hit(self):
        with patch("zotero_mcp.tools.ads.ads_client") as mock_ads:
            mock_ads.is_available.return_value = True
            mock_ads.search.return_value = [{"bibcode": "2024ApJ...968L..12A"}]
            assert _bibcode_from_arxiv("2401.12345") == "2024ApJ...968L..12A"
            mock_ads.search.assert_called_once_with("arxiv:2401.12345", rows=1)

    def test_returns_none_on_no_hits(self):
        with patch("zotero_mcp.tools.ads.ads_client") as mock_ads:
            mock_ads.is_available.return_value = True
            mock_ads.search.return_value = []
            assert _bibcode_from_arxiv("9999.99999") is None

    def test_returns_none_when_ads_unavailable(self):
        with patch("zotero_mcp.tools.ads.ads_client") as mock_ads:
            mock_ads.is_available.return_value = False
            assert _bibcode_from_arxiv("2401.12345") is None
            mock_ads.search.assert_not_called()

    def test_returns_none_on_empty_id(self):
        assert _bibcode_from_arxiv("") is None
        assert _bibcode_from_arxiv(None) is None

    def test_handles_search_exception(self):
        with patch("zotero_mcp.tools.ads.ads_client") as mock_ads:
            mock_ads.is_available.return_value = True
            mock_ads.search.side_effect = RuntimeError("network")
            assert _bibcode_from_arxiv("2401.12345") is None


class TestExportFunction:
    """Tests for ads_client.export()."""

    def test_export_bibtex_returns_export_field(self):
        with patch("zotero_mcp.ads_client._ads_request") as mock_req:
            mock_req.return_value = {
                "export": "@ARTICLE{2024ApJ...968L..12A,\n author = {...},\n}",
                "msg": "OK",
                "status": "success",
            }
            result = export(["2024ApJ...968L..12A"], fmt="bibtex")
        assert result is not None
        assert "@ARTICLE" in result
        # Verify the request was made correctly.
        mock_req.assert_called_once()
        call_args = mock_req.call_args
        assert call_args[0][0] == "/export/bibtex"
        assert call_args[1]["method"] == "POST"
        assert call_args[1]["body"] == {"bibcode": ["2024ApJ...968L..12A"]}

    def test_export_aastex_format(self):
        with patch("zotero_mcp.ads_client._ads_request") as mock_req:
            mock_req.return_value = {"export": "\\bibitem{2024ApJ...968L..12A} ..."}
            result = export(["2024ApJ...968L..12A"], fmt="aastex")
        assert result is not None
        assert "\\bibitem" in result
        assert mock_req.call_args[0][0] == "/export/aastex"

    def test_export_multiple_bibcodes(self):
        with patch("zotero_mcp.ads_client._ads_request") as mock_req:
            mock_req.return_value = {"export": "@ARTICLE{...}\n@ARTICLE{...}"}
            bcs = ["2024ApJ...968L..12A", "2023MNRAS.521..123B"]
            result = export(bcs, fmt="bibtex")
        assert result is not None
        assert mock_req.call_args[1]["body"] == {"bibcode": bcs}

    def test_export_with_sort(self):
        with patch("zotero_mcp.ads_client._ads_request") as mock_req:
            mock_req.return_value = {"export": "@ARTICLE{...}"}
            export(["2024ApJ...968L..12A"], fmt="bibtex", sort="date desc")
        body = mock_req.call_args[1]["body"]
        assert body["sort"] == "date desc"

    def test_export_empty_bibcodes_returns_none(self):
        assert export([], fmt="bibtex") is None

    def test_export_failure_returns_none(self):
        with patch("zotero_mcp.ads_client._ads_request", return_value=None):
            result = export(["2024ApJ...968L..12A"], fmt="bibtex")
        assert result is None

    def test_export_missing_export_field_returns_none(self):
        with patch("zotero_mcp.ads_client._ads_request", return_value={"msg": "OK"}):
            result = export(["2024ApJ...968L..12A"], fmt="bibtex")
        assert result is None

    def test_supported_formats_includes_key_formats(self):
        for fmt in ("bibtex", "aastex", "mnras", "ris", "endnote"):
            assert fmt in SUPPORTED_EXPORT_FORMATS


class TestExportAdsTool:
    """Tests for the zotero_export_ads MCP tool."""

    def test_export_ads_with_bibcode(self):
        from zotero_mcp.tools.ads import export_ads

        ctx = MagicMock()
        ctx.info = MagicMock()

        with patch("zotero_mcp.tools.ads.ads_client") as mock_ads:
            mock_ads.is_available.return_value = True
            mock_ads.normalize_bibcode.side_effect = lambda x: x if len(x) == 19 else None
            mock_ads.export.return_value = "@ARTICLE{2024ApJ...968L..12A,\n author = {...},\n}"

            result = export_ads(
                bibcodes="2024ApJ...968L..12A",
                format="bibtex",
                ctx=ctx,
            )
        assert "ADS Export" in result
        assert "@ARTICLE" in result
        assert "```bibtex" in result

    def test_export_ads_with_zotero_item_key(self):
        from zotero_mcp.tools.ads import export_ads

        ctx = MagicMock()
        ctx.info = MagicMock()

        mock_zot = MagicMock()
        mock_zot.item.return_value = {"data": {"extra": "arXiv:2401.12345\nbibcode: 2024ApJ...968L..12A"}}

        with patch("zotero_mcp.tools.ads._client") as mock_client, patch("zotero_mcp.tools.ads.ads_client") as mock_ads:
            mock_client.get_zotero_client.return_value = mock_zot
            mock_ads.is_available.return_value = True
            mock_ads.normalize_bibcode.return_value = None  # not a raw bibcode
            mock_ads.export.return_value = "@ARTICLE{2024ApJ...968L..12A, ...}"

            result = export_ads(
                bibcodes="ABCD1234",
                format="bibtex",
                ctx=ctx,
            )
        assert "@ARTICLE" in result
        assert "2024ApJ...968L..12A" in result

    def test_export_ads_item_key_arxiv_only_fallback(self):
        """Item key with only an arXiv: line (no bibcode:) should resolve via ADS."""
        from zotero_mcp.tools.ads import export_ads

        ctx = MagicMock()
        ctx.info = MagicMock()

        mock_zot = MagicMock()
        mock_zot.item.return_value = {"data": {"extra": "arXiv:2401.12345 [astro-ph.CO]"}}

        with patch("zotero_mcp.tools.ads._client") as mock_client, patch("zotero_mcp.tools.ads.ads_client") as mock_ads:
            mock_client.get_zotero_client.return_value = mock_zot
            mock_ads.is_available.return_value = True
            mock_ads.normalize_bibcode.return_value = None  # not a raw bibcode
            # The arXiv→bibcode ADS lookup returns one hit with a bibcode.
            mock_ads.search.return_value = [{"bibcode": "2024ApJ...968L..12A"}]
            mock_ads.export.return_value = "@ARTICLE{2024ApJ...968L..12A, ...}"

            result = export_ads(
                bibcodes="ABCD1234",
                format="bibtex",
                ctx=ctx,
            )
        assert "@ARTICLE" in result
        assert "2024ApJ...968L..12A" in result
        # Confirm the arXiv lookup was issued.
        mock_ads.search.assert_called_once_with("arxiv:2401.12345", rows=1)

    def test_export_ads_item_key_bibcode_preferred_over_arxiv(self):
        """When extra has both bibcode: and arXiv:, bibcode wins (no ADS call)."""
        from zotero_mcp.tools.ads import export_ads

        ctx = MagicMock()
        ctx.info = MagicMock()

        mock_zot = MagicMock()
        mock_zot.item.return_value = {"data": {"extra": "arXiv:2401.12345\nbibcode: 2024ApJ...968L..12A"}}

        with patch("zotero_mcp.tools.ads._client") as mock_client, patch("zotero_mcp.tools.ads.ads_client") as mock_ads:
            mock_client.get_zotero_client.return_value = mock_zot
            mock_ads.is_available.return_value = True
            mock_ads.normalize_bibcode.return_value = None
            mock_ads.export.return_value = "@ARTICLE{...}"

            result = export_ads(bibcodes="ABCD1234", ctx=ctx)
        assert "2024ApJ...968L..12A" in result
        # No ADS search should be issued — bibcode line suffices.
        mock_ads.search.assert_not_called()

    def test_export_ads_item_key_arxiv_lookup_returns_no_bibcode(self):
        """If the arXiv→bibcode ADS search finds nothing, the item is unresolved."""
        from zotero_mcp.tools.ads import export_ads

        ctx = MagicMock()
        mock_zot = MagicMock()
        mock_zot.item.return_value = {"data": {"extra": "arXiv:9999.99999 [astro-ph.CO]"}}

        with patch("zotero_mcp.tools.ads._client") as mock_client, patch("zotero_mcp.tools.ads.ads_client") as mock_ads:
            mock_client.get_zotero_client.return_value = mock_zot
            mock_ads.is_available.return_value = True
            mock_ads.normalize_bibcode.return_value = None
            mock_ads.search.return_value = []  # no hits

            result = export_ads(bibcodes="ABCD1234", ctx=ctx)
        assert "could not resolve" in result

    def test_export_ads_item_key_old_style_arxiv_id(self):
        """Old-style arXiv ID (astro-ph/0501001) should also resolve."""
        from zotero_mcp.tools.ads import export_ads

        ctx = MagicMock()
        ctx.info = MagicMock()

        mock_zot = MagicMock()
        mock_zot.item.return_value = {"data": {"extra": "arXiv:astro-ph/0501001"}}

        with patch("zotero_mcp.tools.ads._client") as mock_client, patch("zotero_mcp.tools.ads.ads_client") as mock_ads:
            mock_client.get_zotero_client.return_value = mock_zot
            mock_ads.is_available.return_value = True
            mock_ads.normalize_bibcode.return_value = None
            mock_ads.search.return_value = [{"bibcode": "2005ApJ...621..745B"}]
            mock_ads.export.return_value = "@ARTICLE{2005ApJ...621..745B, ...}"

            result = export_ads(
                bibcodes="WXYZ5678",
                format="bibtex",
                ctx=ctx,
            )
        assert "2005ApJ...621..745B" in result
        mock_ads.search.assert_called_once_with("arxiv:astro-ph/0501001", rows=1)

    def test_export_ads_multiple_bibcodes(self):
        from zotero_mcp.tools.ads import export_ads

        ctx = MagicMock()
        ctx.info = MagicMock()

        with patch("zotero_mcp.tools.ads.ads_client") as mock_ads:
            mock_ads.is_available.return_value = True
            mock_ads.normalize_bibcode.side_effect = lambda x: x if len(x) == 19 else None
            mock_ads.export.return_value = "@ARTICLE{bc1}\n@ARTICLE{bc2}"

            result = export_ads(
                bibcodes=["2024ApJ...968L..12A", "2023MNRAS.521..123B"],
                format="aastex",
                ctx=ctx,
            )
        assert "2 paper(s)" in result
        assert "```latex" in result  # aastex uses latex highlighting

    def test_export_ads_no_token(self):
        from zotero_mcp.tools.ads import export_ads

        ctx = MagicMock()
        with patch("zotero_mcp.tools.ads.ads_client") as mock_ads:
            mock_ads.is_available.return_value = False
            result = export_ads(bibcodes="2024ApJ...968L..12A", ctx=ctx)
        assert "ADS API token" in result

    def test_export_ads_empty_input(self):
        from zotero_mcp.tools.ads import export_ads

        ctx = MagicMock()
        with patch("zotero_mcp.tools.ads.ads_client") as mock_ads:
            mock_ads.is_available.return_value = True
            result = export_ads(bibcodes="", ctx=ctx)
        assert "at least one" in result

    def test_export_ads_unresolved_bibcode(self):
        from zotero_mcp.tools.ads import export_ads

        ctx = MagicMock()
        mock_zot = MagicMock()
        mock_zot.item.return_value = {"data": {"extra": ""}}

        with patch("zotero_mcp.tools.ads._client") as mock_client, patch("zotero_mcp.tools.ads.ads_client") as mock_ads:
            mock_client.get_zotero_client.return_value = mock_zot
            mock_ads.is_available.return_value = True
            mock_ads.normalize_bibcode.return_value = None

            result = export_ads(bibcodes="GARBAGE1", ctx=ctx)
        assert "could not resolve" in result

    def test_export_ads_aastex_format(self):
        from zotero_mcp.tools.ads import export_ads

        ctx = MagicMock()
        ctx.info = MagicMock()

        with patch("zotero_mcp.tools.ads.ads_client") as mock_ads:
            mock_ads.is_available.return_value = True
            mock_ads.normalize_bibcode.side_effect = lambda x: x if len(x) == 19 else None
            mock_ads.export.return_value = "\\bibitem{2024ApJ...968L..12A} Author 2024, ApJ, 968, L12."

            result = export_ads(
                bibcodes="2024ApJ...968L..12A",
                format="aastex",
                ctx=ctx,
            )
        assert "\\bibitem" in result
        assert "```latex" in result
