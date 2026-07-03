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

    def __init__(self, status_code=200, text="", content=b"", headers=None):
        self.status_code = status_code
        self.text = text
        self.content = content
        self.headers = headers or {}

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
    def test_disabled_returns_none(self, monkeypatch, tmp_path):
        """When enabled=False, find_pdf_url returns None without any request."""
        cfg_path = _write_config(tmp_path, {"enabled": False})
        monkeypatch.setattr(scihub_client, "_DEFAULT_CONFIG_PATH", cfg_path)
        ctx = MagicMock()
        # Even if requests.get would be called, it must not be here.
        called = []
        monkeypatch.setattr(requests, "get", lambda *a, **k: called.append(1) or _FakeHTTPResponse())
        assert find_pdf_url("10.1038/nature12373", ctx) is None
        assert called == []

    def test_parses_iframe(self, monkeypatch, tmp_path):
        html = '<html><body><iframe src="//cdn.sci-hub.ru/downloads/2023.pdf"></iframe></body></html>'
        cfg_path = _write_config(tmp_path, {"enabled": True, "domain": "sci-hub.ru"})
        monkeypatch.setattr(scihub_client, "_DEFAULT_CONFIG_PATH", cfg_path)
        ctx = MagicMock()
        monkeypatch.setattr(requests, "get", lambda url, **k: _FakeHTTPResponse(200, text=html))
        result = find_pdf_url("10.1038/nature12373", ctx)
        assert result == "https://cdn.sci-hub.ru/downloads/2023.pdf"

    def test_parses_embed(self, monkeypatch, tmp_path):
        html = '<html><embed src="https://sci-hub.ru/pdf/abc.pdf" type="application/pdf"></html>'
        cfg_path = _write_config(tmp_path, {"enabled": True})
        monkeypatch.setattr(scihub_client, "_DEFAULT_CONFIG_PATH", cfg_path)
        ctx = MagicMock()
        monkeypatch.setattr(requests, "get", lambda url, **k: _FakeHTTPResponse(200, text=html))
        result = find_pdf_url("10.1234/test", ctx)
        assert result == "https://sci-hub.ru/pdf/abc.pdf"

    def test_handles_relative_url(self, monkeypatch, tmp_path):
        html = '<iframe src="/downloads/relative.pdf"></iframe>'
        cfg_path = _write_config(tmp_path, {"enabled": True, "domain": "sci-hub.ru"})
        monkeypatch.setattr(scihub_client, "_DEFAULT_CONFIG_PATH", cfg_path)
        ctx = MagicMock()
        monkeypatch.setattr(requests, "get", lambda url, **k: _FakeHTTPResponse(200, text=html))
        result = find_pdf_url("10.1234/test", ctx)
        assert result == "https://sci-hub.ru/downloads/relative.pdf"

    def test_parses_js_redirect(self, monkeypatch, tmp_path):
        html = "<script>location.href='https://other.site/paper.pdf'</script>"
        cfg_path = _write_config(tmp_path, {"enabled": True})
        monkeypatch.setattr(scihub_client, "_DEFAULT_CONFIG_PATH", cfg_path)
        ctx = MagicMock()
        monkeypatch.setattr(requests, "get", lambda url, **k: _FakeHTTPResponse(200, text=html))
        result = find_pdf_url("10.1234/test", ctx)
        assert result == "https://other.site/paper.pdf"

    def test_returns_none_on_no_match(self, monkeypatch, tmp_path):
        html = "<html><body>No PDF here.</body></html>"
        cfg_path = _write_config(tmp_path, {"enabled": True})
        monkeypatch.setattr(scihub_client, "_DEFAULT_CONFIG_PATH", cfg_path)
        ctx = MagicMock()
        monkeypatch.setattr(requests, "get", lambda url, **k: _FakeHTTPResponse(200, text=html))
        assert find_pdf_url("10.1234/test", ctx) is None

    def test_returns_none_on_http_error(self, monkeypatch, tmp_path):
        cfg_path = _write_config(tmp_path, {"enabled": True})
        monkeypatch.setattr(scihub_client, "_DEFAULT_CONFIG_PATH", cfg_path)
        ctx = MagicMock()
        monkeypatch.setattr(requests, "get", lambda url, **k: _FakeHTTPResponse(404))
        assert find_pdf_url("10.1234/test", ctx) is None

    def test_returns_none_on_network_error(self, monkeypatch, tmp_path):
        cfg_path = _write_config(tmp_path, {"enabled": True})
        monkeypatch.setattr(scihub_client, "_DEFAULT_CONFIG_PATH", cfg_path)
        ctx = MagicMock()

        def boom(*a, **k):
            raise requests.ConnectionError("timeout")

        monkeypatch.setattr(requests, "get", boom)
        assert find_pdf_url("10.1234/test", ctx) is None

    def test_empty_identifier_returns_none(self, monkeypatch, tmp_path):
        cfg_path = _write_config(tmp_path, {"enabled": True})
        monkeypatch.setattr(scihub_client, "_DEFAULT_CONFIG_PATH", cfg_path)
        ctx = MagicMock()
        assert find_pdf_url("", ctx) is None

    def test_uses_configured_domain_in_query(self, monkeypatch, tmp_path):
        """The lookup URL uses the configured domain, not the default."""
        cfg_path = _write_config(tmp_path, {"enabled": True, "domain": "sci-hub.se"})
        monkeypatch.setattr(scihub_client, "_DEFAULT_CONFIG_PATH", cfg_path)
        ctx = MagicMock()
        seen_urls = []
        monkeypatch.setattr(
            requests,
            "get",
            lambda url, **k: (
                seen_urls.append(url) or _FakeHTTPResponse(200, text="<iframe src='https://x/p.pdf'></iframe>")
            ),
        )
        find_pdf_url("10.1234/test", ctx)
        assert seen_urls
        assert "sci-hub.se" in seen_urls[0]

    def test_upgrades_http_to_https(self, monkeypatch, tmp_path):
        """An iframe src starting with http:// is upgraded to https://."""
        html = '<iframe src="http://cdn.example.com/paper.pdf"></iframe>'
        cfg_path = _write_config(tmp_path, {"enabled": True})
        monkeypatch.setattr(scihub_client, "_DEFAULT_CONFIG_PATH", cfg_path)
        ctx = MagicMock()
        monkeypatch.setattr(requests, "get", lambda url, **k: _FakeHTTPResponse(200, text=html))
        result = find_pdf_url("10.1234/test", ctx)
        assert result.startswith("https://")
