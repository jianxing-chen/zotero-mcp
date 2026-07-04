"""Tests for the publisher-only cascade (pub_only=True).

Covers the upgrade_preprint_pdfs requirement: when the item already has
the arXiv preprint, downloading another arXiv copy is pointless, so
pub_only=True restricts the cascade to publisher-version sources only
(Sci-Hub + ADS PUB_PDF) and skips sources that return arXiv preprints
(ADS EPRINT_PDF, arXiv via CrossRef, Unpaywall, Semantic Scholar, PMC).
"""

from __future__ import annotations

from unittest.mock import MagicMock

from zotero_mcp import ads_client
from zotero_mcp.tools import _helpers

# ---------------------------------------------------------------------------
# ads_client.get_pdf_urls(pub_only=True)
# ---------------------------------------------------------------------------


class TestGetPdfUrlsPubOnly:
    def test_pub_only_returns_pub_pdf_when_available(self, monkeypatch):
        """Both PUB_PDF and EPRINT_PDF in esources, pub_only=True -> only PUB_PDF."""
        record = {"bibcode": "2013ApJ...769..127L", "esources": ["PUB_PDF", "EPRINT_PDF"]}
        monkeypatch.setattr(ads_client, "fetch_record", lambda bc: record)
        urls = ads_client.get_pdf_urls("2013ApJ...769..127L", prefer="pub", pub_only=True)
        assert urls == ["https://ui.adsabs.harvard.edu/link_gateway/2013ApJ...769..127L/PUB_PDF"]

    def test_pub_only_returns_empty_when_only_eprint_available(self, monkeypatch):
        """Only EPRINT_PDF in esources, pub_only=True -> [] (no fallback)."""
        record = {"bibcode": "B", "esources": ["EPRINT_PDF"]}
        monkeypatch.setattr(ads_client, "fetch_record", lambda bc: record)
        urls = ads_client.get_pdf_urls("B", prefer="pub", pub_only=True)
        assert urls == []

    def test_pub_only_returns_empty_when_no_esources(self, monkeypatch):
        record = {"bibcode": "B", "esources": []}
        monkeypatch.setattr(ads_client, "fetch_record", lambda bc: record)
        urls = ads_client.get_pdf_urls("B", prefer="pub", pub_only=True)
        assert urls == []

    def test_pub_only_default_false_falls_back_to_eprint(self, monkeypatch):
        """pub_only=False (default): PUB_PDF unavailable -> EPRINT_PDF used."""
        record = {"bibcode": "B", "esources": ["EPRINT_PDF"]}
        monkeypatch.setattr(ads_client, "fetch_record", lambda bc: record)
        urls = ads_client.get_pdf_urls("B", prefer="pub", pub_only=False)
        # Default behavior: PUB_PDF unavailable -> EPRINT_PDF in the list
        assert urls == ["https://ui.adsabs.harvard.edu/link_gateway/B/EPRINT_PDF"]

    def test_pub_only_no_record_returns_empty(self, monkeypatch):
        monkeypatch.setattr(ads_client, "fetch_record", lambda bc: None)
        assert ads_client.get_pdf_urls("B", prefer="pub", pub_only=True) == []


class TestGetPdfUrlsByDoiPubOnly:
    def test_pub_only_forwarded(self, monkeypatch):
        """pub_only=True is forwarded to get_pdf_urls (no EPRINT_PDF)."""
        record = {"bibcode": "B", "esources": ["PUB_PDF", "EPRINT_PDF"]}
        monkeypatch.setattr(ads_client, "search", lambda *a, **k: [{"bibcode": "B"}])
        monkeypatch.setattr(ads_client, "fetch_record", lambda bc: record)
        urls, bibcode = ads_client.get_pdf_urls_by_doi("10.1/x", prefer="pub", pub_only=True)
        assert bibcode == "B"
        assert urls == ["https://ui.adsabs.harvard.edu/link_gateway/B/PUB_PDF"]

    def test_pub_only_skips_eprint_only_record(self, monkeypatch):
        """If only EPRINT_PDF exists, pub_only=True returns empty list."""
        record = {"bibcode": "B", "esources": ["EPRINT_PDF"]}
        monkeypatch.setattr(ads_client, "search", lambda *a, **k: [{"bibcode": "B"}])
        monkeypatch.setattr(ads_client, "fetch_record", lambda bc: record)
        urls, _ = ads_client.get_pdf_urls_by_doi("10.1/x", prefer="pub", pub_only=True)
        assert urls == []


# ---------------------------------------------------------------------------
# _try_attach_oa_pdf(pub_only=True) source filtering
# ---------------------------------------------------------------------------


class TestTryAttachOaPdfPubOnly:
    """Verify pub_only=True skips arXiv/Unpaywall/S2/PMC sources."""

    def _setup_ads(self, monkeypatch, bibcode=None, urls=None):
        """Configure ADS to be available and return the given URLs."""
        monkeypatch.setattr(ads_client, "is_available", lambda: True)
        if bibcode:
            monkeypatch.setattr(
                _helpers._ads_client,
                "get_pdf_urls",
                lambda bc, prefer="eprint", pub_only=False: urls or [],
            )

    def test_pub_only_skips_arxiv_unpaywall_s2_pmc_sources(self, monkeypatch, dummy_ctx):
        """When pub_only=True, only Sci-Hub + ADS sources are tried.

        Mock every source to detect calls. With pub_only=True, the arXiv/
        Unpaywall/Semantic-Scholar/PMC source functions must NOT be invoked.
        """
        # Disable Sci-Hub so it doesn't appear in the source list.
        from zotero_mcp import scihub_client

        monkeypatch.setattr(scihub_client, "is_scihub_enabled", lambda cfg: False)
        monkeypatch.setattr(scihub_client, "load_scihub_config", lambda *a, **k: {})

        # ADS available, returns [] (no PUB_PDF for this bibcode).
        self._setup_ads(monkeypatch, bibcode="B", urls=[])

        # Track calls to each non-publisher source — they should NOT be called.
        arxiv_mock = MagicMock(return_value=None)
        unpaywall_mock = MagicMock(return_value=None)
        s2_mock = MagicMock(return_value=None)
        pmc_mock = MagicMock(return_value=None)
        monkeypatch.setattr(_helpers, "_try_arxiv_from_crossref", arxiv_mock)
        monkeypatch.setattr(_helpers, "_try_unpaywall", unpaywall_mock)
        monkeypatch.setattr(_helpers, "_try_semantic_scholar", s2_mock)
        monkeypatch.setattr(_helpers, "_try_pmc", pmc_mock)

        write_zot = MagicMock()
        result = _helpers._try_attach_oa_pdf(
            write_zot, "ITEM1", "10.1/x", dummy_ctx,
            bibcode="B", prefer_pub_pdf=True, pub_only=True,
        )
        assert "no open-access PDF" in result or "no PDF" in result.lower()
        arxiv_mock.assert_not_called()
        unpaywall_mock.assert_not_called()
        s2_mock.assert_not_called()
        pmc_mock.assert_not_called()

    def test_pub_only_false_calls_all_sources(self, monkeypatch, dummy_ctx):
        """pub_only=False (default): all sources are tried as before."""
        from zotero_mcp import scihub_client

        monkeypatch.setattr(scihub_client, "is_scihub_enabled", lambda cfg: False)
        monkeypatch.setattr(scihub_client, "load_scihub_config", lambda *a, **k: {})

        self._setup_ads(monkeypatch, bibcode="B", urls=[])

        arxiv_mock = MagicMock(return_value=None)
        unpaywall_mock = MagicMock(return_value=None)
        s2_mock = MagicMock(return_value=None)
        pmc_mock = MagicMock(return_value=None)
        monkeypatch.setattr(_helpers, "_try_arxiv_from_crossref", arxiv_mock)
        monkeypatch.setattr(_helpers, "_try_unpaywall", unpaywall_mock)
        monkeypatch.setattr(_helpers, "_try_semantic_scholar", s2_mock)
        monkeypatch.setattr(_helpers, "_try_pmc", pmc_mock)

        write_zot = MagicMock()
        _helpers._try_attach_oa_pdf(
            write_zot, "ITEM1", "10.1/x", dummy_ctx,
            bibcode="B", prefer_pub_pdf=True, pub_only=False,
        )
        arxiv_mock.assert_called()
        unpaywall_mock.assert_called()
        s2_mock.assert_called()
        pmc_mock.assert_called()

    def test_pub_only_attaches_publisher_pdf_when_available(self, monkeypatch, dummy_ctx):
        """With pub_only=True, if ADS PUB_PDF is available, it's downloaded."""
        from zotero_mcp import scihub_client

        monkeypatch.setattr(scihub_client, "is_scihub_enabled", lambda cfg: False)
        monkeypatch.setattr(scihub_client, "load_scihub_config", lambda *a, **k: {})

        pub_url = "https://ui.adsabs.harvard.edu/link_gateway/B/PUB_PDF"
        self._setup_ads(monkeypatch, bibcode="B", urls=[pub_url])

        # _download_and_attach_pdf returns a non-None suffix on success.
        monkeypatch.setattr(_helpers, "_download_and_attach_pdf", lambda *a, **k: "")
        # Non-publisher sources must not be called.
        monkeypatch.setattr(_helpers, "_try_arxiv_from_crossref", MagicMock(return_value=None))
        monkeypatch.setattr(_helpers, "_try_unpaywall", MagicMock(return_value=None))
        monkeypatch.setattr(_helpers, "_try_semantic_scholar", MagicMock(return_value=None))
        monkeypatch.setattr(_helpers, "_try_pmc", MagicMock(return_value=None))

        write_zot = MagicMock()
        result = _helpers._try_attach_oa_pdf(
            write_zot, "ITEM1", "10.1/x", dummy_ctx,
            bibcode="B", prefer_pub_pdf=True, pub_only=True,
        )
        assert "PDF attached" in result
        assert "ADS" in result
