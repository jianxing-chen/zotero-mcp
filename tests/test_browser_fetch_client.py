"""Unit tests for the browser_fetch_client module.

All DevTools HTTP/WebSocket calls are mocked — no real browser needed.
"""

from __future__ import annotations

import base64
import json
from unittest.mock import MagicMock, patch

from zotero_mcp import browser_fetch_client as bfc

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class TestConfig:
    def test_load_returns_defaults_when_no_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bfc, "_DEFAULT_CONFIG_PATH", tmp_path / "nope.json")
        cfg = bfc.load_browser_fetch_config()
        assert cfg["enabled"] is False
        assert cfg["debug_port"] == 9222
        assert cfg["page_wait_seconds"] == 8
        assert cfg["inter_item_sleep_seconds"] == 6

    def test_load_reads_block(self, tmp_path, monkeypatch):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"browser_fetch": {"enabled": True, "debug_port": 9333}}))
        monkeypatch.setattr(bfc, "_DEFAULT_CONFIG_PATH", path)
        cfg = bfc.load_browser_fetch_config()
        assert cfg["enabled"] is True
        assert cfg["debug_port"] == 9333
        # Defaults still present for unspecified keys.
        assert cfg["page_wait_seconds"] == 8

    def test_is_enabled(self):
        assert bfc.is_browser_fetch_enabled({"enabled": True}) is True
        assert bfc.is_browser_fetch_enabled({"enabled": False}) is False
        assert bfc.is_browser_fetch_enabled(None) is False
        assert bfc.is_browser_fetch_enabled({}) is False


# ---------------------------------------------------------------------------
# Challenge detection
# ---------------------------------------------------------------------------


class TestIsChallengePage:
    def test_cloudflare_challenge(self):
        assert bfc.is_challenge_page("Just a moment...", "challenges.cloudflare.com", "") is True

    def test_please_wait(self):
        assert bfc.is_challenge_page("Please wait", "some html", "https://example.com") is True

    def test_tdm_reservation(self):
        assert bfc.is_challenge_page("", "tdm-reservation page", "https://example.com") is True

    def test_elsevier_signin_redirect(self):
        assert bfc.is_challenge_page("", "", "https://id.elsevier.com/login") is True

    def test_normal_article_page(self):
        assert bfc.is_challenge_page("My Paper Title", "<html>article</html>", "https://www.sciencedirect.com/...") is False


# ---------------------------------------------------------------------------
# DevToolsClient
# ---------------------------------------------------------------------------


class TestDevToolsClient:
    def test_is_reachable_true(self):
        client = bfc.DevToolsClient(9222)
        with patch.object(client, "http_get", return_value='{"Browser": "Chrome"}'):
            assert client.is_reachable() is True

    def test_is_reachable_false_on_error(self):
        client = bfc.DevToolsClient(9222)
        with patch.object(client, "http_get", side_effect=Exception("refused")):
            assert client.is_reachable() is False

    def test_open_page(self):
        client = bfc.DevToolsClient(9222)
        page = {"id": "ABC", "webSocketDebuggerUrl": "ws://localhost/devtools"}
        with patch.object(client, "http_get", return_value=json.dumps(page)):
            result = client.open_page("https://example.com")
            assert result["id"] == "ABC"

    def test_close_page_swallows_errors(self):
        client = bfc.DevToolsClient(9222)
        with patch.object(client, "http_get", side_effect=Exception("closed")):
            client.close_page("ABC")  # must not raise

    def test_evaluate_returns_value(self):
        client = bfc.DevToolsClient(9222)
        ws_msg = {"result": {"result": {"value": "hello"}}}
        with patch.object(client, "call", return_value=ws_msg):
            assert client.evaluate("ws://x", "document.title") == "hello"

    def test_evaluate_returns_none_on_error(self):
        client = bfc.DevToolsClient(9222)
        with patch.object(client, "call", return_value={"error": "boom"}):
            assert client.evaluate("ws://x", "expr") is None


# ---------------------------------------------------------------------------
# PDF URL discovery
# ---------------------------------------------------------------------------


class TestFindGenericPdfUrl:
    def test_finds_citation_pdf_url_meta(self):
        devtools = MagicMock()
        # The JS returns a list of {url, text, source} dicts.
        devtools.evaluate.return_value = [
            {"url": "https://example.com/article.pdf", "text": "Download PDF", "source": "meta"},
        ]
        url = bfc.find_generic_pdf_url(devtools, "ws://x", "https://example.com/article")
        assert url == "https://example.com/article.pdf"

    def test_finds_iframe_pdf(self):
        devtools = MagicMock()
        devtools.evaluate.return_value = [
            {"url": "/pdf/viewer", "text": "", "source": "iframe"},
            {"url": "https://cdn.example.com/paper.pdf", "text": "", "source": "embed"},
        ]
        url = bfc.find_generic_pdf_url(devtools, "ws://x", "https://example.com/article")
        assert url == "https://cdn.example.com/paper.pdf"

    def test_returns_empty_when_no_candidates(self):
        devtools = MagicMock()
        devtools.evaluate.return_value = []
        assert bfc.find_generic_pdf_url(devtools, "ws://x", "https://example.com") == ""

    def test_skips_low_score_noise(self):
        devtools = MagicMock()
        devtools.evaluate.return_value = [
            {"url": "https://example.com/privacy.pdf", "text": "", "source": "a"},
        ]
        # privacy.pdf scores negative — should be skipped.
        assert bfc.find_generic_pdf_url(devtools, "ws://x", "https://example.com") == ""


# ---------------------------------------------------------------------------
# PDF byte extraction
# ---------------------------------------------------------------------------


class TestFetchPdfInPageContext:
    def test_returns_bytes_on_success(self):
        devtools = MagicMock()
        pdf_bytes = b"%PDF-1.4 " + b"x" * 2000
        encoded = base64.b64encode(pdf_bytes).decode("ascii")
        devtools.evaluate.return_value = encoded
        result = bfc.fetch_pdf_in_page_context(devtools, "ws://x", "https://example.com/p.pdf")
        assert result == pdf_bytes

    def test_returns_none_on_error_string(self):
        devtools = MagicMock()
        devtools.evaluate.return_value = "ERR:network"
        assert bfc.fetch_pdf_in_page_context(devtools, "ws://x", "https://example.com/p.pdf") is None

    def test_returns_none_on_non_pdf_magic(self):
        devtools = MagicMock()
        html_bytes = b"<html>not a pdf</html>"
        encoded = base64.b64encode(html_bytes).decode("ascii")
        devtools.evaluate.return_value = encoded
        assert bfc.fetch_pdf_in_page_context(devtools, "ws://x", "https://example.com/p.pdf") is None


class TestExtractPdfFromViewer:
    def test_returns_bytes_on_success(self):
        devtools = MagicMock()
        pdf_bytes = b"%PDF-1.4 " + b"y" * 1500
        encoded = base64.b64encode(pdf_bytes).decode("ascii")
        devtools.evaluate.return_value = encoded
        result = bfc.extract_pdf_from_pdfjs_viewer(devtools, "ws://x")
        assert result == pdf_bytes

    def test_returns_none_on_timeout(self):
        devtools = MagicMock()
        devtools.evaluate.return_value = "ERR:timeout_waiting_for_pdf_viewer"
        assert bfc.extract_pdf_from_pdfjs_viewer(devtools, "ws://x") is None


# ---------------------------------------------------------------------------
# fetch_publisher_pdf_via_browser (integration with mocked DevTools)
# ---------------------------------------------------------------------------


class TestFetchPublisherPdfViaBrowser:
    def test_science_direct_metadata_path(self):
        """ScienceDirect pdfDownload metadata -> in-page fetch succeeds."""
        devtools = MagicMock()
        sd_html = (
            '"pdfDownload":{"isPdfFullText":false,"urlMetadata":{"queryParams":'
            '{"md5":"abc","pid":"def"},"pii":"S1234","pdfExtension":".pdf",'
            '"path":"/science/article/pii"}}'
        )
        pdf_bytes = b"%PDF-1.4 " + b"z" * 3000
        encoded = base64.b64encode(pdf_bytes).decode("ascii")

        # Sequence of evaluate calls:
        # 1. article outerHTML (msg_id=10) -> sd_html
        # 2. document.title (msg_id=11) -> "Article Title"
        # 3. location.href (msg_id=12) -> article url
        # 4. open_page for pdf -> returns page dict
        # 5. pdf viewer location.href (msg_id=20) -> ""
        # 6. pdf viewer outerHTML (msg_id=22) -> ""
        # 7. fetch_pdf_in_page_context (msg_id=30, from article context) -> encoded
        devtools.open_page.return_value = {"id": "P1", "webSocketDebuggerUrl": "ws://pdf"}
        devtools.evaluate.side_effect = [sd_html, "Article Title", "https://www.sciencedirect.com/...", "", "", encoded]

        # Patch the module-level fetch_pdf_in_page_context to return the bytes
        # (since the real function calls devtools.evaluate which has side_effect).
        with patch.object(bfc, "fetch_pdf_in_page_context", return_value=pdf_bytes):
            result_bytes, url, label = bfc.fetch_publisher_pdf_via_browser(
                devtools, "https://doi.org/10.1016/x", page_wait_seconds=0,
            )
        assert result_bytes == pdf_bytes
        assert "sciencedirect" in url.lower() or "pdf" in label.lower()
        assert "sd_" in label

    def test_generic_publisher_path(self):
        """No ScienceDirect metadata -> generic citation_pdf_url -> in-page fetch."""
        devtools = MagicMock()
        pdf_bytes = b"%PDF-1.4 " + b"w" * 2500
        devtools.open_page.return_value = {"id": "P1", "webSocketDebuggerUrl": "ws://pdf"}
        devtools.evaluate.side_effect = [
            "<html>no sd metadata</html>",  # article html
            "Generic Paper",  # title
            "https://publisher.example.com/article",  # current url
        ]
        with patch.object(bfc, "find_generic_pdf_url", return_value="https://publisher.example.com/paper.pdf"), \
             patch.object(bfc, "fetch_pdf_in_page_context", return_value=pdf_bytes):
            result_bytes, url, label = bfc.fetch_publisher_pdf_via_browser(
                devtools, "https://doi.org/10.1/x", page_wait_seconds=0,
            )
        assert result_bytes == pdf_bytes
        assert "generic" in label

    def test_challenge_page_returns_none(self):
        """Bot verification page -> (None, url, 'challenge_page')."""
        devtools = MagicMock()
        devtools.open_page.return_value = {"id": "P1", "webSocketDebuggerUrl": "ws://x"}
        devtools.evaluate.side_effect = [
            "challenges.cloudflare.com challenge",
            "Please wait",
            "https://challenges.cloudflare.com/...",
        ]
        result, url, label = bfc.fetch_publisher_pdf_via_browser(
            devtools, "https://doi.org/10.1/x", page_wait_seconds=0,
        )
        assert result is None
        assert label == "challenge_page"

    def test_no_pdf_metadata_returns_none(self):
        devtools = MagicMock()
        devtools.open_page.return_value = {"id": "P1", "webSocketDebuggerUrl": "ws://x"}
        devtools.evaluate.side_effect = [
            "<html>article</html>",
            "Title",
            "https://example.com/article",
        ]
        with patch.object(bfc, "find_generic_pdf_url", return_value=""):
            result, url, label = bfc.fetch_publisher_pdf_via_browser(
                devtools, "https://doi.org/10.1/x", page_wait_seconds=0,
            )
        assert result is None
        assert label == "no_pdf_metadata"
