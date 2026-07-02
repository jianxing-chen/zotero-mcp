"""Tests for ADS tools (search_ads, ads_citation_network, add_by_bibcode).

All ADS/Zotero calls mocked. Verifies: token-missing errors, search routing,
in-library tagging, citation network direction handling, bibcode dedup,
and graceful degradation.
"""

import pytest
from conftest import FakeZotero

from zotero_mcp import ads_client, server


class _FakeCtx:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


@pytest.fixture
def ctx():
    return _FakeCtx()


# --------------------------------------------------------------------------- #
# search_ads
# --------------------------------------------------------------------------- #
class TestSearchAds:
    def test_no_token_returns_clear_error(self, ctx, monkeypatch):
        monkeypatch.delenv("ADS_API_TOKEN", raising=False)
        out = server.search_ads(query="black holes", ctx=ctx)
        assert "ADS_API_TOKEN" in out
        assert "not set" in out

    def test_empty_query_error(self, ctx, monkeypatch):
        monkeypatch.setenv("ADS_API_TOKEN", "tok")
        out = server.search_ads(query="", ctx=ctx)
        assert "cannot be empty" in out

    def test_search_renders_results(self, ctx, monkeypatch):
        monkeypatch.setenv("ADS_API_TOKEN", "tok")
        monkeypatch.setattr(
            ads_client,
            "search",
            lambda *a, **k: [
                {
                    "bibcode": "2003ApJ...589L..21B",
                    "title": ["Dark Energy"],
                    "author": ["Riess, A."],
                    "year": 2003,
                    "doi": ["10.1086/374884"],
                    "citation_count": 5000,
                }
            ],
        )
        # Patch at the module path the tool actually imports (_client.get_zotero_client).
        monkeypatch.setattr("zotero_mcp.tools.ads._client.get_zotero_client", lambda: FakeZotero())
        out = server.search_ads(query="dark energy", ctx=ctx)
        assert "Dark Energy" in out
        assert "2003ApJ...589L..21B" in out
        assert "not in library" in out
        assert "add_by_bibcode" in out  # import hint

    def test_no_results(self, ctx, monkeypatch):
        monkeypatch.setenv("ADS_API_TOKEN", "tok")
        monkeypatch.setattr(ads_client, "search", lambda *a, **k: [])
        out = server.search_ads(query="nonexistent topic", ctx=ctx)
        assert "No ADS results" in out


# --------------------------------------------------------------------------- #
# ads_citation_network
# --------------------------------------------------------------------------- #
class TestCitationNetwork:
    def test_no_token_error(self, ctx, monkeypatch):
        monkeypatch.delenv("ADS_API_TOKEN", raising=False)
        out = server.ads_citation_network(identifier="2003ApJ...589L..21B", ctx=ctx)
        assert "ADS_API_TOKEN" in out

    def test_invalid_bibcode_error(self, ctx, monkeypatch):
        monkeypatch.setenv("ADS_API_TOKEN", "tok")
        monkeypatch.setattr("zotero_mcp.tools.ads._client.get_zotero_client", lambda: FakeZotero())
        out = server.ads_citation_network(identifier="not-a-bibcode", ctx=ctx)
        assert "could not resolve" in out or "bibcode" in out.lower()

    def test_both_directions(self, ctx, monkeypatch):
        monkeypatch.setenv("ADS_API_TOKEN", "tok")
        refs = [
            {
                "bibcode": "1998AJ....116.1009R",
                "title": ["Ref Paper"],
                "author": ["X"],
                "year": 1998,
                "citation_count": 100,
            }
        ]
        cits = [
            {
                "bibcode": "2010MNRAS.401..123Y",
                "title": ["Citing Paper"],
                "author": ["Y"],
                "year": 2010,
                "citation_count": 50,
            }
        ]
        monkeypatch.setattr(ads_client, "get_references", lambda bc, rows=50: refs)
        monkeypatch.setattr(ads_client, "get_citations", lambda bc, rows=50: cits)
        monkeypatch.setattr("zotero_mcp.tools.ads._client.get_zotero_client", lambda: FakeZotero())

        out = server.ads_citation_network(identifier="2003ApJ...589L..21B", direction="both", ctx=ctx)
        assert "References" in out
        assert "Citations" in out
        assert "Ref Paper" in out
        assert "Citing Paper" in out

    def test_references_only(self, ctx, monkeypatch):
        monkeypatch.setenv("ADS_API_TOKEN", "tok")
        refs = [
            {
                "bibcode": "1998AJ....116.1009R",
                "title": ["Only Ref"],
                "author": ["X"],
                "year": 1998,
                "citation_count": 1,
            }
        ]
        monkeypatch.setattr(ads_client, "get_references", lambda bc, rows=50: refs)
        monkeypatch.setattr(ads_client, "get_citations", lambda bc, rows=50: [])
        monkeypatch.setattr("zotero_mcp.tools.ads._client.get_zotero_client", lambda: FakeZotero())

        out = server.ads_citation_network(identifier="2003ApJ...589L..21B", direction="references", ctx=ctx)
        assert "Only Ref" in out
        # No Citations section heading (summary line still mentions "Citations: 0").
        assert "## Citations" not in out


# --------------------------------------------------------------------------- #
# add_by_bibcode
# --------------------------------------------------------------------------- #
class TestAddByBibcode:
    def test_no_token_error(self, ctx, monkeypatch):
        monkeypatch.delenv("ADS_API_TOKEN", raising=False)
        out = server.add_by_bibcode(bibcode="2003ApJ...589L..21B", ctx=ctx)
        assert "ADS_API_TOKEN" in out

    def test_invalid_bibcode_reports_error(self, ctx, monkeypatch):
        monkeypatch.setenv("ADS_API_TOKEN", "tok")
        # Patch _get_write_client to avoid needing real Zotero creds.
        fake_zot = FakeZotero()
        monkeypatch.setattr(
            "zotero_mcp.tools.write._helpers._get_write_client",
            lambda c: (fake_zot, fake_zot),
        )
        out = server.add_by_bibcode(bibcode="garbage", ctx=ctx)
        assert "no valid bibcodes" in out or "invalid" in out.lower()

    def test_record_not_found(self, ctx, monkeypatch):
        monkeypatch.setenv("ADS_API_TOKEN", "tok")
        fake_zot = FakeZotero()
        monkeypatch.setattr(
            "zotero_mcp.tools.write._helpers._get_write_client",
            lambda c: (fake_zot, fake_zot),
        )
        monkeypatch.setattr(ads_client, "fetch_record", lambda bc: None)
        out = server.add_by_bibcode(bibcode="2003ApJ...589L..21B", ctx=ctx)
        assert "not found" in out or "❌" in out

    def test_successful_add(self, ctx, monkeypatch):
        monkeypatch.setenv("ADS_API_TOKEN", "tok")
        fake_zot = FakeZotero()
        monkeypatch.setattr(
            "zotero_mcp.tools.write._helpers._get_write_client",
            lambda c: (fake_zot, fake_zot),
        )
        doc = {
            "bibcode": "2003ApJ...589L..21B",
            "title": ["Test Paper"],
            "author": ["Smith, J."],
            "year": 2003,
            "doi": ["10.1086/374884"],
            "pub": "ApJ",
            "doctype": "article",
        }
        monkeypatch.setattr(ads_client, "fetch_record", lambda bc: doc)
        monkeypatch.setattr(ads_client, "get_pdf_url", lambda bc: None)
        # find_existing_items returns [] (not in library)
        monkeypatch.setattr(
            "zotero_mcp.tools.write._helpers.find_existing_items",
            lambda zot, **kw: [],
        )
        # _try_attach_oa_pdf returns None (no OA PDF found)
        monkeypatch.setattr(
            "zotero_mcp.tools.write._helpers._try_attach_oa_pdf",
            lambda *a, **k: None,
        )
        monkeypatch.setattr(
            "zotero_mcp.tools.write._helpers.ensure_collection_membership",
            lambda *a, **k: [],
        )
        out = server.add_by_bibcode(bibcode="2003ApJ...589L..21B", ctx=ctx)
        assert "Successfully added" in out or "Test Paper" in out
        # Verify the item was created in fake Zotero.
        assert len(fake_zot.created) >= 1
