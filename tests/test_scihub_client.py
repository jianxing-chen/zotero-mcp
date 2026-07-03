"""Unit tests for the Sci-Hub client (scihub_client.py).

Covers config loading, the enabled gate, and the HTML iframe/embed/redirect
parsing in ``find_pdf_url``. Network access is mocked throughout — these tests
never hit a real Sci-Hub domain.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import requests

from zotero_mcp import scihub_client
from zotero_mcp.scihub_client import (
    DEFAULT_DOMAIN,
    find_pdf_url,
    get_scihub_domain,
    is_scihub_enabled,
    load_scihub_config,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeHTTPResponse:
    """Minimal requests.Response stand-in."""

    def __init__(self, status_code=200, text="", content=b"", headers=None, url=""):
        self.status_code = status_code
        self.text = text
        self.content = content
        self.headers = headers or {}
        self.url = url

    def json(self):
        return None


def _write_config(tmp_path: Path, scihub_block: dict) -> Path:
    """Write a config.json with the given scihub block and return its path."""
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"scihub": scihub_block}))
    return cfg_path


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


class TestLoadScihubConfig:
    def test_returns_empty_when_no_file(self, tmp_path):
        cfg = load_scihub_config(config_path=tmp_path / "nonexistent.json")
        assert cfg == {}

    def test_returns_scihub_block(self, tmp_path):
        cfg_path = _write_config(tmp_path, {"enabled": True, "domain": "sci-hub.se"})
        cfg = load_scihub_config(config_path=cfg_path)
        assert cfg == {"enabled": True, "domain": "sci-hub.se"}

    def test_returns_empty_when_block_missing(self, tmp_path):
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({"mineru": {"enabled": True}}))
        cfg = load_scihub_config(config_path=cfg_path)
        assert cfg == {}

    def test_corrupt_json_returns_empty(self, tmp_path):
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text("not valid json {{{")
        cfg = load_scihub_config(config_path=cfg_path)
        assert cfg == {}


class TestIsScihubEnabled:
    def test_enabled_true(self):
        assert is_scihub_enabled({"enabled": True}) is True

    def test_enabled_false(self):
        assert is_scihub_enabled({"enabled": False}) is False

    def test_missing_key_defaults_false(self):
        assert is_scihub_enabled({}) is False

    def test_none_config(self):
        assert is_scihub_enabled(None) is False


class TestGetScihubDomain:
    def test_uses_configured_domain(self):
        assert get_scihub_domain({"domain": "sci-hub.se"}) == "sci-hub.se"

    def test_strips_trailing_slash(self):
        assert get_scihub_domain({"domain": "sci-hub.se/"}) == "sci-hub.se"

    def test_falls_back_to_default(self):
        assert get_scihub_domain({}) == DEFAULT_DOMAIN

    def test_non_string_ignored(self):
        assert get_scihub_domain({"domain": 123}) == DEFAULT_DOMAIN


# ---------------------------------------------------------------------------
# find_pdf_url
# ---------------------------------------------------------------------------


class TestFindPdfUrl:
    """Tests for find_pdf_url with the two-step POST path.

    _try_post now: (1) GET landing page -> parse <form action=...>, (2) POST to
    that action URL. _try_get (fallback): GET /{identifier}. The tests route
    requests.get by URL: landing page vs. the per-identifier GET fallback.
    """

    LANDING_FORM_HTML = '<form method = "POST" action ="https://www.tesble.com/">'

    def _make_router(
        self,
        landing_html: str,
        landing_url_contains: str = "sci-hub.ee/",
        post_html: str = "",
        get_html: str = "",
        get_status: int = 200,
        post_status: int = 200,
        landing_status: int = 200,
    ):
        """Return (fake_get, fake_post) callables that route by URL.

        - GET to the landing page (URL contains landing_url_contains and has no
          identifier) -> returns landing_html (the <form> page).
        - GET to /{identifier} (the _try_get fallback) -> returns get_html.
        - POST (any URL) -> returns post_html.
        """

        def fake_get(url, **k):
            # Landing page: URL ends with the domain root, no identifier.
            if landing_url_contains in url and url.rstrip("/").endswith(("sci-hub.ee", "sci-hub.ru", "sci-hub.se")):
                return _FakeHTTPResponse(landing_status, text=landing_html, url=url)
            # Per-identifier GET fallback.
            return _FakeHTTPResponse(get_status, text=get_html, url=url)

        def fake_post(url, **k):
            return _FakeHTTPResponse(post_status, text=post_html, url=url)

        return fake_get, fake_post

    def test_disabled_returns_none(self, monkeypatch, tmp_path):
        """When enabled=False, find_pdf_url returns None without any request."""
        cfg_path = _write_config(tmp_path, {"enabled": False})
        monkeypatch.setattr(scihub_client, "_DEFAULT_CONFIG_PATH", cfg_path)
        ctx = MagicMock()
        called = []
        monkeypatch.setattr(requests, "post", lambda *a, **k: called.append("post") or _FakeHTTPResponse())
        monkeypatch.setattr(requests, "get", lambda *a, **k: called.append("get") or _FakeHTTPResponse())
        assert find_pdf_url("10.1038/nature12373", ctx) is None
        assert called == []

    def test_post_parses_pdf_element(self, monkeypatch, tmp_path):
        """POST response with id='pdf' src=... -> returns that URL (primary path)."""
        post_html = '<embed id="pdf" src="https://sci.bban.top/pdf/paper.pdf" type="application/pdf">'
        cfg_path = _write_config(tmp_path, {"enabled": True, "domain": "sci-hub.ee"})
        monkeypatch.setattr(scihub_client, "_DEFAULT_CONFIG_PATH", cfg_path)
        ctx = MagicMock()
        fake_get, fake_post = self._make_router(landing_html=self.LANDING_FORM_HTML, post_html=post_html)
        monkeypatch.setattr(requests, "get", fake_get)
        monkeypatch.setattr(requests, "post", fake_post)
        result = find_pdf_url("10.1038/nature12373", ctx)
        assert result == "https://sci.bban.top/pdf/paper.pdf"

    def test_post_resolves_form_action(self, monkeypatch, tmp_path):
        """The POST is sent to the form action URL (tesble.com), not the domain."""
        post_html = '<embed id="pdf" src="https://sci.bban.top/pdf/x.pdf">'
        cfg_path = _write_config(tmp_path, {"enabled": True, "domain": "sci-hub.ee"})
        monkeypatch.setattr(scihub_client, "_DEFAULT_CONFIG_PATH", cfg_path)
        ctx = MagicMock()
        post_urls = []
        fake_get, _ = self._make_router(landing_html=self.LANDING_FORM_HTML, post_html=post_html)

        def tracking_post(url, **k):
            post_urls.append(url)
            return _FakeHTTPResponse(200, text=post_html, url=url)

        monkeypatch.setattr(requests, "get", fake_get)
        monkeypatch.setattr(requests, "post", tracking_post)
        find_pdf_url("10.1234/test", ctx)
        assert post_urls
        assert "tesble.com" in post_urls[0]

    def test_post_falls_back_to_get(self, monkeypatch, tmp_path):
        """When POST yields nothing, GET fallback (/{identifier}) is tried."""
        post_html = "<html><body>No PDF in POST response.</body></html>"
        get_html = '<iframe src="https://cdn.example.com/fallback.pdf"></iframe>'
        cfg_path = _write_config(tmp_path, {"enabled": True, "domain": "sci-hub.ee"})
        monkeypatch.setattr(scihub_client, "_DEFAULT_CONFIG_PATH", cfg_path)
        ctx = MagicMock()
        fake_get, fake_post = self._make_router(
            landing_html=self.LANDING_FORM_HTML, post_html=post_html, get_html=get_html
        )
        monkeypatch.setattr(requests, "get", fake_get)
        monkeypatch.setattr(requests, "post", fake_post)
        result = find_pdf_url("10.1234/test", ctx)
        assert result == "https://cdn.example.com/fallback.pdf"

    def test_get_parses_iframe(self, monkeypatch, tmp_path):
        """GET fallback parses <iframe> when POST returns nothing."""
        post_html = "<html>empty</html>"
        get_html = '<iframe src="//cdn.sci-hub.ru/downloads/2023.pdf"></iframe>'
        cfg_path = _write_config(tmp_path, {"enabled": True, "domain": "sci-hub.ru"})
        monkeypatch.setattr(scihub_client, "_DEFAULT_CONFIG_PATH", cfg_path)
        ctx = MagicMock()
        fake_get, fake_post = self._make_router(
            landing_html=self.LANDING_FORM_HTML, post_html=post_html, get_html=get_html
        )
        monkeypatch.setattr(requests, "get", fake_get)
        monkeypatch.setattr(requests, "post", fake_post)
        result = find_pdf_url("10.1038/nature12373", ctx)
        assert result == "https://cdn.sci-hub.ru/downloads/2023.pdf"

    def test_get_parses_embed(self, monkeypatch, tmp_path):
        """GET fallback parses <embed> when POST returns nothing."""
        post_html = "<html>empty</html>"
        get_html = '<embed src="https://sci-hub.ru/pdf/abc.pdf" type="application/pdf">'
        cfg_path = _write_config(tmp_path, {"enabled": True})
        monkeypatch.setattr(scihub_client, "_DEFAULT_CONFIG_PATH", cfg_path)
        ctx = MagicMock()
        fake_get, fake_post = self._make_router(
            landing_html=self.LANDING_FORM_HTML, post_html=post_html, get_html=get_html
        )
        monkeypatch.setattr(requests, "get", fake_get)
        monkeypatch.setattr(requests, "post", fake_post)
        result = find_pdf_url("10.1234/test", ctx)
        assert result == "https://sci-hub.ru/pdf/abc.pdf"

    def test_get_handles_relative_url(self, monkeypatch, tmp_path):
        """GET fallback resolves relative iframe src against the final URL."""
        post_html = "<html>empty</html>"
        get_html = '<iframe src="/downloads/relative.pdf"></iframe>'
        cfg_path = _write_config(tmp_path, {"enabled": True, "domain": "sci-hub.ru"})
        monkeypatch.setattr(scihub_client, "_DEFAULT_CONFIG_PATH", cfg_path)
        ctx = MagicMock()

        def fake_get(url, **k):
            if url.rstrip("/").endswith("sci-hub.ru"):
                return _FakeHTTPResponse(200, text=self.LANDING_FORM_HTML, url=url)
            return _FakeHTTPResponse(200, text=get_html, url="https://sci-hub.ru/10.1234")

        monkeypatch.setattr(requests, "get", fake_get)
        monkeypatch.setattr(requests, "post", lambda url, **k: _FakeHTTPResponse(200, text=post_html, url=url))
        result = find_pdf_url("10.1234/test", ctx)
        assert result == "https://sci-hub.ru/downloads/relative.pdf"

    def test_parses_js_redirect(self, monkeypatch, tmp_path):
        """JS location.href redirect is parsed from the GET fallback."""
        post_html = "<html>empty</html>"
        get_html = "<script>location.href='https://other.site/paper.pdf'</script>"
        cfg_path = _write_config(tmp_path, {"enabled": True})
        monkeypatch.setattr(scihub_client, "_DEFAULT_CONFIG_PATH", cfg_path)
        ctx = MagicMock()
        fake_get, fake_post = self._make_router(
            landing_html=self.LANDING_FORM_HTML, post_html=post_html, get_html=get_html
        )
        monkeypatch.setattr(requests, "get", fake_get)
        monkeypatch.setattr(requests, "post", fake_post)
        result = find_pdf_url("10.1234/test", ctx)
        assert result == "https://other.site/paper.pdf"

    def test_returns_none_on_no_match(self, monkeypatch, tmp_path):
        """Both POST and GET return nothing -> None."""
        html = "<html><body>No PDF here.</body></html>"
        cfg_path = _write_config(tmp_path, {"enabled": True})
        monkeypatch.setattr(scihub_client, "_DEFAULT_CONFIG_PATH", cfg_path)
        ctx = MagicMock()
        fake_get, fake_post = self._make_router(landing_html=self.LANDING_FORM_HTML, post_html=html, get_html=html)
        monkeypatch.setattr(requests, "get", fake_get)
        monkeypatch.setattr(requests, "post", fake_post)
        assert find_pdf_url("10.1234/test", ctx) is None

    def test_returns_none_on_landing_403(self, monkeypatch, tmp_path):
        """Landing GET returns 403 -> POST skipped -> GET fallback -> 403 -> None."""
        cfg_path = _write_config(tmp_path, {"enabled": True})
        monkeypatch.setattr(scihub_client, "_DEFAULT_CONFIG_PATH", cfg_path)
        ctx = MagicMock()
        fake_get, fake_post = self._make_router(
            landing_html="", landing_status=403, post_html="", get_html="", get_status=403
        )
        monkeypatch.setattr(requests, "get", fake_get)
        monkeypatch.setattr(requests, "post", fake_post)
        assert find_pdf_url("10.1234/test", ctx) is None

    def test_returns_none_on_network_error(self, monkeypatch, tmp_path):
        """All requests raise -> None."""
        cfg_path = _write_config(tmp_path, {"enabled": True})
        monkeypatch.setattr(scihub_client, "_DEFAULT_CONFIG_PATH", cfg_path)
        ctx = MagicMock()

        def boom(*a, **k):
            raise requests.ConnectionError("timeout")

        monkeypatch.setattr(requests, "post", boom)
        monkeypatch.setattr(requests, "get", boom)
        assert find_pdf_url("10.1234/test", ctx) is None

    def test_empty_identifier_returns_none(self, monkeypatch, tmp_path):
        cfg_path = _write_config(tmp_path, {"enabled": True})
        monkeypatch.setattr(scihub_client, "_DEFAULT_CONFIG_PATH", cfg_path)
        ctx = MagicMock()
        assert find_pdf_url("", ctx) is None

    def test_post_uses_configured_domain_for_landing(self, monkeypatch, tmp_path):
        """The landing GET targets the configured domain."""
        cfg_path = _write_config(tmp_path, {"enabled": True, "domain": "sci-hub.se"})
        monkeypatch.setattr(scihub_client, "_DEFAULT_CONFIG_PATH", cfg_path)
        ctx = MagicMock()
        get_urls = []
        post_html = '<embed id="pdf" src="https://x/p.pdf">'

        def fake_get(url, **k):
            get_urls.append(url)
            return _FakeHTTPResponse(200, text=self.LANDING_FORM_HTML, url=url)

        monkeypatch.setattr(requests, "get", fake_get)
        monkeypatch.setattr(requests, "post", lambda url, **k: _FakeHTTPResponse(200, text=post_html, url=url))
        find_pdf_url("10.1234/test", ctx)
        assert get_urls
        assert "sci-hub.se" in get_urls[0]

    def test_upgrades_http_to_https(self, monkeypatch, tmp_path):
        """A PDF src starting with http:// is upgraded to https://."""
        post_html = '<embed id="pdf" src="http://cdn.example.com/paper.pdf">'
        cfg_path = _write_config(tmp_path, {"enabled": True})
        monkeypatch.setattr(scihub_client, "_DEFAULT_CONFIG_PATH", cfg_path)
        ctx = MagicMock()
        fake_get, fake_post = self._make_router(landing_html=self.LANDING_FORM_HTML, post_html=post_html)
        monkeypatch.setattr(requests, "get", fake_get)
        monkeypatch.setattr(requests, "post", fake_post)
        result = find_pdf_url("10.1234/test", ctx)
        assert result.startswith("https://")
        result = find_pdf_url("10.1234/test", ctx)
        assert result.startswith("https://")
