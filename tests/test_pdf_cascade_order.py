"""Regression tests for the reordered PDF cascade in _try_attach_oa_pdf.

The cascade order is: ADS → Sci-Hub → arXiv → Unpaywall → Semantic Scholar → PMC.

These tests pin that ordering and verify the ADS/Sci-Hub opt-in gates so a
future refactor can't silently reorder the sources. They patch ADS
availability and Sci-Hub config directly, so they do not depend on the
ambient environment (whether ADS_API_TOKEN is set or config.json has a
scihub block).
"""

from __future__ import annotations

import requests
from conftest import FakeZotero

from zotero_mcp import ads_client
from zotero_mcp.server import _try_attach_oa_pdf
from zotero_mcp.tools import _helpers

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeHTTPResponse:
    def __init__(self, status_code=200, json_data=None, content=b"", headers=None, text=""):
        self.status_code = status_code
        self._json = json_data
        self.content = content
        self.text = text
        self.headers = headers or {}

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)

    def iter_content(self, chunk_size=8192):
        yield self.content


class _AttachZotero(FakeZotero):
    def __init__(self):
        super().__init__()
        self.attachments = []

    def attachment_both(self, files, parentid=None, **kwargs):
        self.attachments.append({"files": files, "parentid": parentid})


def _bypass_ssrf(monkeypatch):
    """Let the cascade download from any mocked URL (no real DNS)."""
    monkeypatch.setattr(_helpers, "_url_resolves_to_public_host", lambda url: True)


def _disable_ads(monkeypatch):
    """Make ADS unavailable so it's skipped from the cascade."""
    monkeypatch.setattr(ads_client, "is_available", lambda: False)


def _enable_ads(monkeypatch):
    monkeypatch.setattr(ads_client, "is_available", lambda: True)


# Sci-Hub has been globally disabled and removed from the cascade.
# The scihub_client module is kept for reference but no longer imported by
# _helpers._try_attach_oa_pdf. These _disable/_enable helpers are retained
# as no-ops so existing tests that call them don't break during the transition.


def _disable_scihub(monkeypatch):
    """No-op — Sci-Hub is globally disabled and no longer in the cascade."""
    pass


def _enable_scihub(monkeypatch):
    """No-op — Sci-Hub is globally disabled and cannot be re-enabled."""
    pass


# ---------------------------------------------------------------------------
# Source ordering
# ---------------------------------------------------------------------------


class TestCascadeOrder:
    """Verify the source list is built in the documented priority order.

    Order: ADS → arXiv → Unpaywall → Semantic Scholar → PubMed Central.
    Sci-Hub was removed from the cascade (globally disabled). ADS is first;
    in practice its PUB_PDF is WAF-blocked so it effectively returns the
    arXiv preprint (EPRINT_PDF).
    """

    def test_ads_first_when_available(self, monkeypatch, dummy_ctx):
        """With ADS available, the first source tried is ADS."""
        _enable_ads(monkeypatch)
        zot = _AttachZotero()
        tried: list[str] = []

        def fake_ads_pdf_url_by_doi(doi, prefer_pub, ctx, *, pub_only=False):
            tried.append("ADS")
            return "https://ads.example.com/paper.pdf"

        monkeypatch.setattr(_helpers, "_try_ads_pdf_url_by_doi", fake_ads_pdf_url_by_doi)

        def fake_get(url, **kwargs):
            tried.append(f"GET {url}")
            return _FakeHTTPResponse(
                200,
                content=b"%PDF-1.4 " + b"x" * 2000,
                headers={"Content-Type": "application/pdf"},
            )

        monkeypatch.setattr(requests, "get", fake_get)
        _bypass_ssrf(monkeypatch)

        result = _try_attach_oa_pdf(zot, "ITEM1", "10.1234/test", dummy_ctx, prefer_pub_pdf=True)
        assert "PDF attached" in result
        assert "ADS" in result
        assert tried[0] == "ADS"

    def test_arxiv_before_unpaywall(self, monkeypatch, dummy_ctx):
        """With ADS off, arXiv is tried before Unpaywall.

        We give arXiv a CrossRef metadata relation pointing to an arXiv PDF.
        The cascade should pick it up without ever hitting Unpaywall.
        """
        _disable_ads(monkeypatch)
        zot = _AttachZotero()
        hit_unpaywall = []

        crossref_meta = {"relation": {"has-preprint": [{"id-type": "arxiv", "id": "2401.12345"}]}}

        def fake_get(url, **kwargs):
            if "unpaywall.org" in url:
                hit_unpaywall.append(1)
            if "arxiv.org/pdf" in url:
                return _FakeHTTPResponse(
                    200,
                    content=b"%PDF-1.4 " + b"x" * 2000,
                    headers={"Content-Type": "application/pdf"},
                )
            return _FakeHTTPResponse(404)

        monkeypatch.setattr(requests, "get", fake_get)
        _bypass_ssrf(monkeypatch)

        result = _try_attach_oa_pdf(
            zot,
            "ITEM1",
            "10.1234/test",
            dummy_ctx,
            crossref_metadata=crossref_meta,
        )
        assert "PDF attached" in result
        assert "arXiv (via CrossRef)" in result
        assert hit_unpaywall == [], "Unpaywall should not be reached when arXiv succeeds"

    def test_unpaywall_before_s2_before_pmc(self, monkeypatch, dummy_ctx):
        """Full fallback chain: arXiv misses, then Unpaywall → S2 → PMC."""
        _disable_ads(monkeypatch)
        _disable_scihub(monkeypatch)
        zot = _AttachZotero()
        order: list[str] = []

        def fake_get(url, **kwargs):
            if "unpaywall.org" in url:
                order.append("Unpaywall")
                return _FakeHTTPResponse(200, json_data={"best_oa_location": None, "oa_locations": []})
            if "semanticscholar.org" in url:
                order.append("S2")
                return _FakeHTTPResponse(200, json_data={"openAccessPdf": None})
            if "ncbi.nlm.nih.gov/tools/idconv" in url:
                order.append("PMC")
                return _FakeHTTPResponse(200, json_data={"records": [{"pmcid": "PMC999"}]})
            if "pmc.ncbi.nlm.nih.gov/articles" in url:
                return _FakeHTTPResponse(
                    200,
                    content=b"%PDF-1.4 " + b"x" * 2000,
                    headers={"Content-Type": "application/pdf"},
                )
            return _FakeHTTPResponse(404)

        monkeypatch.setattr(requests, "get", fake_get)
        _bypass_ssrf(monkeypatch)

        result = _try_attach_oa_pdf(zot, "ITEM1", "10.1234/test", dummy_ctx, crossref_metadata=None)
        assert "PubMed Central" in result
        assert "PDF attached" in result
        # PMC is tried last, after Unpaywall and S2 both miss.
        assert order == ["Unpaywall", "S2", "PMC"]


# ---------------------------------------------------------------------------
# Opt-in gates
# ---------------------------------------------------------------------------


class TestCascadeGates:
    def test_ads_skipped_when_not_configured(self, monkeypatch, dummy_ctx):
        """With no ADS token, ADS is absent from the cascade entirely."""
        _disable_ads(monkeypatch)
        _disable_scihub(monkeypatch)
        zot = _AttachZotero()
        ads_url_called = []

        def fake_get(url, **kwargs):
            if "adsabs.harvard.edu" in url or "link_gateway" in url:
                ads_url_called.append(url)
            if "unpaywall.org" in url:
                # Unpaywall has the PDF — cascade stops here.
                return _FakeHTTPResponse(
                    200,
                    json_data={"best_oa_location": {"url_for_pdf": "https://upw.example.com/p.pdf"}},
                )
            if "upw.example.com" in url:
                return _FakeHTTPResponse(
                    200,
                    content=b"%PDF-1.4 " + b"x" * 2000,
                    headers={"Content-Type": "application/pdf"},
                )
            return _FakeHTTPResponse(404)

        monkeypatch.setattr(requests, "get", fake_get)
        _bypass_ssrf(monkeypatch)

        result = _try_attach_oa_pdf(zot, "ITEM1", "10.1234/test", dummy_ctx)
        assert "Unpaywall" in result
        assert ads_url_called == []

    def test_bibcode_short_circuits_ads_doi_lookup(self, monkeypatch, dummy_ctx):
        """When bibcode is given, ADS uses _try_ads_pdf_url (not _try_ads_pdf_url_by_doi)."""
        _enable_ads(monkeypatch)
        _disable_scihub(monkeypatch)
        zot = _AttachZotero()
        by_bibcode_called = []
        by_doi_called = []

        def fake_by_bibcode(bibcode, prefer_pub, ctx, *, pub_only=False):
            by_bibcode_called.append(bibcode)
            return "https://ads.example.com/paper.pdf"

        def fake_by_doi(doi, prefer_pub, ctx, *, pub_only=False):
            by_doi_called.append(doi)
            return "https://ads.example.com/paper.pdf"

        monkeypatch.setattr(_helpers, "_try_ads_pdf_url", fake_by_bibcode)
        monkeypatch.setattr(_helpers, "_try_ads_pdf_url_by_doi", fake_by_doi)

        def fake_get(url, **kwargs):
            return _FakeHTTPResponse(
                200,
                content=b"%PDF-1.4 " + b"x" * 2000,
                headers={"Content-Type": "application/pdf"},
            )

        monkeypatch.setattr(requests, "get", fake_get)
        _bypass_ssrf(monkeypatch)

        result = _try_attach_oa_pdf(
            zot,
            "ITEM1",
            "10.1234/test",
            dummy_ctx,
            bibcode="2003ApJ...589L..21B",
            prefer_pub_pdf=True,
        )
        assert "ADS" in result
        assert by_bibcode_called == ["2003ApJ...589L..21B"]
        assert by_doi_called == [], "DOI lookup must not run when bibcode is given"

    def test_prefer_pub_pdf_routes_pub_to_ads(self, monkeypatch, dummy_ctx):
        """prefer_pub_pdf=True makes ADS request the publisher PDF (prefer='pub')."""
        _enable_ads(monkeypatch)
        _disable_scihub(monkeypatch)
        zot = _AttachZotero()
        seen_pref = []

        def fake_ads_pdf_url(bibcode, prefer_pub, ctx, *, pub_only=False):
            seen_pref.append(prefer_pub)
            return "https://ads.example.com/paper.pdf"

        monkeypatch.setattr(_helpers, "_try_ads_pdf_url", fake_ads_pdf_url)

        def fake_get(url, **kwargs):
            return _FakeHTTPResponse(
                200,
                content=b"%PDF-1.4 " + b"x" * 2000,
                headers={"Content-Type": "application/pdf"},
            )

        monkeypatch.setattr(requests, "get", fake_get)
        _bypass_ssrf(monkeypatch)

        _try_attach_oa_pdf(
            zot,
            "ITEM1",
            "10.1234/test",
            dummy_ctx,
            bibcode="2003ApJ...589L..21B",
            prefer_pub_pdf=True,
        )
        assert seen_pref == [True]

    def test_prefer_pub_false_routes_eprint_to_ads(self, monkeypatch, dummy_ctx):
        """prefer_pub_pdf=False makes ADS prefer the arXiv eprint (prefer='eprint')."""
        _enable_ads(monkeypatch)
        _disable_scihub(monkeypatch)
        zot = _AttachZotero()
        seen_pref = []

        def fake_ads_pdf_url(bibcode, prefer_pub, ctx, *, pub_only=False):
            seen_pref.append(prefer_pub)
            return "https://ads.example.com/paper.pdf"

        monkeypatch.setattr(_helpers, "_try_ads_pdf_url", fake_ads_pdf_url)

        def fake_get(url, **kwargs):
            return _FakeHTTPResponse(
                200,
                content=b"%PDF-1.4 " + b"x" * 2000,
                headers={"Content-Type": "application/pdf"},
            )

        monkeypatch.setattr(requests, "get", fake_get)
        _bypass_ssrf(monkeypatch)

        _try_attach_oa_pdf(
            zot,
            "ITEM1",
            "10.1234/test",
            dummy_ctx,
            bibcode="2003ApJ...589L..21B",
            prefer_pub_pdf=False,
        )
        assert seen_pref == [False]
