"""Tests for the ADS API client: requests mocked, no network."""

import sys
import types
from unittest.mock import MagicMock

from zotero_mcp import ads_client as A


# --------------------------------------------------------------------------- #
# bibcode normalization
# --------------------------------------------------------------------------- #
class TestNormalizeBibcode:
    def test_valid_canonical(self):
        assert A.normalize_bibcode("2003ApJ...589L..21B") == "2003ApJ...589L..21B"

    def test_strips_whitespace(self):
        assert A.normalize_bibcode("  2003ApJ...589L..21B  ") == "2003ApJ...589L..21B"

    def test_strips_brackets_quotes(self):
        assert A.normalize_bibcode('["2003ApJ...589L..21B"]') == "2003ApJ...589L..21B"

    def test_extracts_from_url(self):
        assert A.normalize_bibcode("https://ui.adsabs.harvard.edu/abs/2003ApJ...589L..21B") == "2003ApJ...589L..21B"

    def test_rejects_garbage(self):
        assert A.normalize_bibcode("not-a-bibcode") is None
        assert A.normalize_bibcode("") is None
        assert A.normalize_bibcode(None) is None

    def test_ampersand_bibstem(self):
        # A&AS contains & — valid bibcode
        assert A.normalize_bibcode("2000A&AS..143...85A") == "2000A&AS..143...85A"


# --------------------------------------------------------------------------- #
# doc_to_csl_json
# --------------------------------------------------------------------------- #
class TestDocToCslJson:
    def test_full_record(self):
        doc = {
            "bibcode": "2003ApJ...589L..21B",
            "title": ["A Test Paper"],
            "author": ["Smith, John", "Lee, Kate", "Brown, Bob", "Extra, Author"],
            "year": 2003,
            "pub": "The Astrophysical Journal",
            "volume": "589",
            "issue": "1",
            "page": "L21",
            "doi": ["10.1086/374884"],
            "abstract": "An abstract here.",
            "doctype": "article",
        }
        csl = A.doc_to_csl_json(doc)
        assert csl["type"] == "article-journal"
        assert csl["title"] == "A Test Paper"
        assert csl["DOI"] == "10.1086/374884"
        assert csl["container-title"] == "The Astrophysical Journal"
        assert csl["volume"] == "589"
        assert csl["issued"] == {"date-parts": [[2003]]}
        assert len(csl["author"]) == 4
        assert csl["author"][0] == {"family": "Smith", "given": "John"}
        assert csl["note"] == "bibcode: 2003ApJ...589L..21B"

    def test_minimal_record(self):
        csl = A.doc_to_csl_json({"bibcode": "2024arXiv24010001X", "doctype": "eprint"})
        assert csl["type"] == "article-journal"
        assert csl["note"] == "bibcode: 2024arXiv24010001X"
        assert "title" not in csl

    def test_inproceedings_maps_to_conference(self):
        csl = A.doc_to_csl_json({"doctype": "inproceedings"})
        assert csl["type"] == "paper-conference"

    def test_collaboration_author_literal(self):
        csl = A.doc_to_csl_json({"author": ["Planck Collaboration"]})
        assert csl["author"] == [{"literal": "Planck Collaboration"}]

    def test_string_doi(self):
        csl = A.doc_to_csl_json({"doi": "10.1234/test"})
        assert csl["DOI"] == "10.1234/test"


# --------------------------------------------------------------------------- #
# API request / search (mocked)
# --------------------------------------------------------------------------- #
class TestApiRequests:
    def _mock_resp(self, status=200, json_data=None, headers=None):
        resp = MagicMock()
        resp.status_code = status
        resp.json.return_value = json_data or {}
        resp.text = ""
        resp.headers = headers or {}
        return resp

    def test_no_token_returns_none(self, monkeypatch):
        monkeypatch.delenv("ADS_API_TOKEN", raising=False)
        assert A._ads_request("/search/query") is None

    def test_search_returns_docs(self, monkeypatch):
        monkeypatch.setenv("ADS_API_TOKEN", "fake-token")
        payload = {"response": {"docs": [{"bibcode": "2003ApJ...589L..21B", "title": ["X"]}]}}
        fake_requests = types.ModuleType("requests")
        fake_requests.request = MagicMock(return_value=self._mock_resp(200, payload))
        monkeypatch.setitem(sys.modules, "requests", fake_requests)

        docs = A.search("black holes", rows=5)
        assert len(docs) == 1
        assert docs[0]["bibcode"] == "2003ApJ...589L..21B"
        # Verify the request was made with Bearer auth.
        call_args = fake_requests.request.call_args
        assert call_args.kwargs["headers"]["Authorization"] == "Bearer fake-token"

    def test_search_failure_returns_empty(self, monkeypatch):
        monkeypatch.setenv("ADS_API_TOKEN", "fake-token")
        fake_requests = types.ModuleType("requests")
        fake_requests.request = MagicMock(return_value=self._mock_resp(500))
        monkeypatch.setitem(sys.modules, "requests", fake_requests)
        assert A.search("anything") == []

    def test_401_sets_auth_error_flag(self, monkeypatch):
        """A 401 (invalid/expired token) sets last_error='auth' so upstream
        tools can show a clear 'fix your token' message instead of 'not found'."""
        monkeypatch.setenv("ADS_API_TOKEN", "bad-token")
        fake_requests = types.ModuleType("requests")
        fake_requests.request = MagicMock(return_value=self._mock_resp(401))
        monkeypatch.setitem(sys.modules, "requests", fake_requests)
        assert A.search("anything") == []
        assert A.last_error == "auth"

    def test_403_sets_auth_error_flag(self, monkeypatch):
        monkeypatch.setenv("ADS_API_TOKEN", "bad-token")
        fake_requests = types.ModuleType("requests")
        fake_requests.request = MagicMock(return_value=self._mock_resp(403))
        monkeypatch.setitem(sys.modules, "requests", fake_requests)
        assert A.search("anything") == []
        assert A.last_error == "auth"

    def test_500_sets_unavailable_error_flag(self, monkeypatch):
        """A 5xx sets last_error='unavailable' (distinct from auth)."""
        monkeypatch.setenv("ADS_API_TOKEN", "fake-token")
        fake_requests = types.ModuleType("requests")
        fake_requests.request = MagicMock(return_value=self._mock_resp(500))
        monkeypatch.setitem(sys.modules, "requests", fake_requests)
        A.search("x")
        assert A.last_error == "unavailable"

    def test_success_resets_error_flag(self, monkeypatch):
        """A successful request clears any prior last_error."""
        monkeypatch.setenv("ADS_API_TOKEN", "fake-token")
        # First a 500, then a 200.
        responses = [self._mock_resp(500), self._mock_resp(200, {"response": {"docs": []}})]
        fake_requests = types.ModuleType("requests")
        fake_requests.request = MagicMock(side_effect=responses)
        monkeypatch.setitem(sys.modules, "requests", fake_requests)
        A._ads_request("/x")  # 500
        assert A.last_error == "unavailable"
        A._ads_request("/x")  # 200
        assert A.last_error is None

    def test_fetch_record_returns_single(self, monkeypatch):
        monkeypatch.setenv("ADS_API_TOKEN", "fake-token")
        doc = {"bibcode": "2003ApJ...589L..21B", "title": ["Found"]}
        payload = {"response": {"docs": [doc]}}
        fake_requests = types.ModuleType("requests")
        fake_requests.request = MagicMock(return_value=self._mock_resp(200, payload))
        monkeypatch.setitem(sys.modules, "requests", fake_requests)
        assert A.fetch_record("2003ApJ...589L..21B") == doc

    def test_fetch_record_not_found(self, monkeypatch):
        monkeypatch.setenv("ADS_API_TOKEN", "fake-token")
        payload = {"response": {"docs": []}}
        fake_requests = types.ModuleType("requests")
        fake_requests.request = MagicMock(return_value=self._mock_resp(200, payload))
        monkeypatch.setitem(sys.modules, "requests", fake_requests)
        assert A.fetch_record("BADBIBCODE") is None

    def test_references_uses_operator(self, monkeypatch):
        monkeypatch.setenv("ADS_API_TOKEN", "fake-token")
        payload = {"response": {"docs": [{"bibcode": "1998AJ....116.1009R"}]}}
        fake_requests = types.ModuleType("requests")
        fake_requests.request = MagicMock(return_value=self._mock_resp(200, payload))
        monkeypatch.setitem(sys.modules, "requests", fake_requests)
        refs = A.get_references("2003ApJ...589L..21B")
        assert len(refs) == 1
        # Verify the query used references() operator.
        params = fake_requests.request.call_args.kwargs["params"]
        assert "references(2003ApJ...589L..21B)" in params["q"]

    def test_citations_uses_operator(self, monkeypatch):
        monkeypatch.setenv("ADS_API_TOKEN", "fake-token")
        payload = {"response": {"docs": []}}
        fake_requests = types.ModuleType("requests")
        fake_requests.request = MagicMock(return_value=self._mock_resp(200, payload))
        monkeypatch.setitem(sys.modules, "requests", fake_requests)
        A.get_citations("2003ApJ...589L..21B", rows=10)
        params = fake_requests.request.call_args.kwargs["params"]
        assert "citations(2003ApJ...589L..21B)" in params["q"]


# --------------------------------------------------------------------------- #
# PDF URL resolution
# --------------------------------------------------------------------------- #
class TestGetPdfUrl:
    def test_prefers_eprint(self, monkeypatch):
        monkeypatch.setenv("ADS_API_TOKEN", "fake-token")
        doc = {"bibcode": "2003ApJ...589L..21B", "esources": ["EPRINT_PDF", "PUB_PDF"]}
        payload = {"response": {"docs": [doc]}}
        fake_requests = types.ModuleType("requests")
        fake_requests.request = MagicMock(
            return_value=MagicMock(status_code=200, json=MagicMock(return_value=payload), headers={}, text="")
        )
        monkeypatch.setitem(sys.modules, "requests", fake_requests)
        url = A.get_pdf_url("2003ApJ...589L..21B")
        assert url is not None
        assert "EPRINT_PDF" in url

    def test_prefers_pub_when_requested(self, monkeypatch):
        monkeypatch.setenv("ADS_API_TOKEN", "fake-token")
        doc = {"bibcode": "2003ApJ...589L..21B", "esources": ["EPRINT_PDF", "PUB_PDF"]}
        payload = {"response": {"docs": [doc]}}
        fake_requests = types.ModuleType("requests")
        fake_requests.request = MagicMock(
            return_value=MagicMock(status_code=200, json=MagicMock(return_value=payload), headers={}, text="")
        )
        monkeypatch.setitem(sys.modules, "requests", fake_requests)
        url = A.get_pdf_url("2003ApJ...589L..21B", prefer="pub")
        assert url is not None
        assert "PUB_PDF" in url

    def test_no_esources_falls_back_to_eprint_gateway(self, monkeypatch):
        monkeypatch.setenv("ADS_API_TOKEN", "fake-token")
        doc = {"bibcode": "2003ApJ...589L..21B", "esources": []}
        payload = {"response": {"docs": [doc]}}
        fake_requests = types.ModuleType("requests")
        fake_requests.request = MagicMock(
            return_value=MagicMock(status_code=200, json=MagicMock(return_value=payload), headers={}, text="")
        )
        monkeypatch.setitem(sys.modules, "requests", fake_requests)
        url = A.get_pdf_url("2003ApJ...589L..21B")
        assert url is not None
        assert "EPRINT_PDF" in url

    def test_record_not_found_returns_none(self, monkeypatch):
        monkeypatch.setenv("ADS_API_TOKEN", "fake-token")
        payload = {"response": {"docs": []}}
        fake_requests = types.ModuleType("requests")
        fake_requests.request = MagicMock(
            return_value=MagicMock(status_code=200, json=MagicMock(return_value=payload), headers={}, text="")
        )
        monkeypatch.setitem(sys.modules, "requests", fake_requests)
        assert A.get_pdf_url("BADBIBCODE") is None


# --------------------------------------------------------------------------- #
# Availability
# --------------------------------------------------------------------------- #
class TestAvailability:
    def test_available_with_token(self, monkeypatch):
        monkeypatch.setenv("ADS_API_TOKEN", "tok")
        assert A.is_available() is True

    def test_unavailable_without_token(self, monkeypatch):
        monkeypatch.delenv("ADS_API_TOKEN", raising=False)
        assert A.is_available() is False
        assert A.get_ads_token() is None
