"""Tests for the API-based reranker (oMLX /v1/rerank protocol).

The CrossEncoderReranker (local sentence-transformers) is already covered by
existing semantic tests; these focus on ApiReranker's HTTP request construction,
response parsing, error handling, and the _get_reranker dispatch logic.
No real HTTP calls — requests.post is mocked.
"""

import sys
from unittest.mock import MagicMock, patch

import pytest

if sys.version_info >= (3, 14):
    pytest.skip("chromadb incompatible with Python 3.14+", allow_module_level=True)

from zotero_mcp.semantic_search import ApiReranker, CrossEncoderReranker


# --------------------------------------------------------------------------- #
# ApiReranker request construction & response parsing
# --------------------------------------------------------------------------- #
class TestApiRerankerRequests:
    def _mock_response(self, status=200, results=None):
        resp = MagicMock()
        resp.status_code = status
        resp.json.return_value = {
            "id": "rerank-abc123",
            "results": results if results is not None else [
                {"index": 2, "relevance_score": 0.95},
                {"index": 0, "relevance_score": 0.72},
                {"index": 1, "relevance_score": 0.31},
            ],
            "model": "Qwen3-Reranker-8B-mxfp8",
            "usage": {"total_tokens": 42},
        }
        resp.text = ""
        return resp

    def test_basic_rerank(self):
        r = ApiReranker(model="Qwen3-Reranker-8B", base_url="http://localhost:8000/v1")
        with patch("requests.post", return_value=self._mock_response()) as mock_post:
            scored = r.rerank_with_scores("query", ["doc0", "doc1", "doc2"], top_k=3)

        # Results sorted by descending score.
        assert scored == [(2, 0.95), (0, 0.72), (1, 0.31)]

        # Verify the request was built correctly.
        call = mock_post.call_args
        url = call.args[0]
        assert url == "http://localhost:8000/v1/rerank"
        body = call.kwargs["json"]
        assert body["model"] == "Qwen3-Reranker-8B"
        assert body["query"] == "query"
        assert body["documents"] == ["doc0", "doc1", "doc2"]
        assert body["top_n"] == 3
        assert body["return_documents"] is False

    def test_top_k_truncation(self):
        r = ApiReranker(model="m", base_url="http://localhost:8000/v1")
        with patch("requests.post", return_value=self._mock_response()):
            scored = r.rerank_with_scores("q", ["a", "b", "c"], top_k=2)
        assert len(scored) == 2
        assert scored[0] == (2, 0.95)  # highest first

    def test_rerank_returns_indices_only(self):
        r = ApiReranker(model="m", base_url="http://localhost:8000/v1")
        with patch("requests.post", return_value=self._mock_response()):
            indices = r.rerank("q", ["a", "b", "c"], top_k=3)
        assert indices == [2, 0, 1]

    def test_auth_header_sent_when_key_provided(self):
        r = ApiReranker(model="m", base_url="http://localhost:8000/v1", api_key="secret")
        with patch("requests.post", return_value=self._mock_response()) as mock_post:
            r.rerank_with_scores("q", ["d"], top_k=1)
        headers = mock_post.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer secret"

    def test_no_auth_header_without_key(self):
        r = ApiReranker(model="m", base_url="http://localhost:8000/v1")
        with patch("requests.post", return_value=self._mock_response()) as mock_post:
            r.rerank_with_scores("q", ["d"], top_k=1)
        headers = mock_post.call_args.kwargs["headers"]
        assert "Authorization" not in headers

    def test_base_url_trailing_slash_stripped(self):
        r = ApiReranker(model="m", base_url="http://localhost:8000/v1/")
        with patch("requests.post", return_value=self._mock_response()) as mock_post:
            r.rerank_with_scores("q", ["d"], top_k=1)
        assert mock_post.call_args.args[0] == "http://localhost:8000/v1/rerank"

    def test_unsorted_results_get_sorted(self):
        # A non-conformant server returns unsorted results — we sort defensively.
        r = ApiReranker(model="m", base_url="http://localhost:8000/v1")
        unsorted = [
            {"index": 0, "relevance_score": 0.1},
            {"index": 1, "relevance_score": 0.9},
            {"index": 2, "relevance_score": 0.5},
        ]
        with patch("requests.post", return_value=self._mock_response(results=unsorted)):
            scored = r.rerank_with_scores("q", ["a", "b", "c"], top_k=3)
        assert scored == [(1, 0.9), (2, 0.5), (0, 0.1)]

    def test_nested_format_zenmux(self):
        # zenmux-style: query+documents under "input", options under "parameters".
        r = ApiReranker(
            model="qwen/qwen3-rerank",
            base_url="https://zenmux.ai/api/v1",
            request_format="nested",
        )
        with patch("requests.post", return_value=self._mock_response()) as mock_post:
            scored = r.rerank_with_scores("query", ["d0", "d1", "d2"], top_k=3)

        assert scored == [(2, 0.95), (0, 0.72), (1, 0.31)]
        body = mock_post.call_args.kwargs["json"]
        # Nested structure: input.{query,documents}, parameters.top_n
        assert "input" in body
        assert body["input"]["query"] == "query"
        assert body["input"]["documents"] == ["d0", "d1", "d2"]
        assert body["parameters"]["top_n"] == 3
        assert body["parameters"]["return_documents"] is False
        # No top-level query/documents (that's the flat format).
        assert "query" not in body
        assert "documents" not in body

    def test_flat_format_default(self):
        # Default format is flat (oMLX/Jina-style): top-level query/documents.
        r = ApiReranker(model="m", base_url="http://localhost:8000/v1")
        with patch("requests.post", return_value=self._mock_response()) as mock_post:
            r.rerank_with_scores("q", ["d"], top_k=1)
        body = mock_post.call_args.kwargs["json"]
        assert body["query"] == "q"
        assert body["documents"] == ["d"]
        assert body["top_n"] == 1
        assert "input" not in body


# --------------------------------------------------------------------------- #
# ApiReranker error handling
# --------------------------------------------------------------------------- #
class TestApiRerankerErrors:
    def test_http_error_raises(self):
        r = ApiReranker(model="m", base_url="http://localhost:8000/v1")
        err_resp = MagicMock()
        err_resp.status_code = 500
        err_resp.text = "internal error"
        with patch("requests.post", return_value=err_resp):
            with pytest.raises(RuntimeError, match="HTTP 500"):
                r.rerank_with_scores("q", ["d"], top_k=1)

    def test_network_error_raises(self):
        import requests as req
        r = ApiReranker(model="m", base_url="http://localhost:8000/v1")
        with patch("requests.post", side_effect=req.ConnectionError("refused")):
            with pytest.raises(req.ConnectionError):
                r.rerank_with_scores("q", ["d"], top_k=1)

    def test_missing_base_url_raises(self):
        r = ApiReranker(model="m", base_url=None)
        with pytest.raises(ValueError, match="base_url"):
            r.rerank_with_scores("q", ["d"], top_k=1)

    def test_empty_results_returns_empty_list(self):
        r = ApiReranker(model="m", base_url="http://localhost:8000/v1")
        empty_resp = MagicMock()
        empty_resp.status_code = 200
        empty_resp.json.return_value = {"results": []}
        with patch("requests.post", return_value=empty_resp):
            assert r.rerank_with_scores("q", ["d"], top_k=1) == []
