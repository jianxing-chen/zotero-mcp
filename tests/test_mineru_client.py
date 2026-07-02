"""Tests for the MinerU client: degradation chain, cache, page splitting.

All subprocess/requests calls are mocked — these are pure unit tests that
verify routing logic, not real MinerU behavior. Mirrors the monkeypatch style
used across the test suite (patch module-level functions in zotero_mcp.*).
"""

import json
import subprocess
import sys
from pathlib import Path

from zotero_mcp import mineru_client as M


# --------------------------------------------------------------------------- #
# Config loading & availability
# --------------------------------------------------------------------------- #
class TestConfig:
    def test_load_missing_config_returns_empty(self, tmp_path):
        assert M.load_mineru_config(tmp_path / "nope.json") == {}

    def test_load_present_config_returns_block(self, tmp_path):
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"mineru": {"enabled": True, "backend": "hybrid"}}))
        block = M.load_mineru_config(cfg)
        assert block == {"enabled": True, "backend": "hybrid"}

    def test_load_invalid_json_returns_empty(self, tmp_path):
        cfg = tmp_path / "config.json"
        cfg.write_text("{not json")
        assert M.load_mineru_config(cfg) == {}

    def test_is_enabled_explicit_false(self):
        assert M.is_mineru_enabled({"enabled": False}) is False

    def test_is_enabled_missing(self):
        assert M.is_mineru_enabled({}) is False

    def test_is_enabled_true(self):
        assert M.is_mineru_enabled({"enabled": True}) is True

    def test_available_api_requires_url(self):
        assert M.is_mineru_available({"backend": "api"}) is False
        assert M.is_mineru_available({"backend": "api", "api_url": "http://h:8000"}) is True

    def test_available_local_requires_executable(self, monkeypatch):
        # No executable configured and not on PATH → unavailable.
        monkeypatch.setattr(M.shutil, "which", lambda _name: None)
        assert M.is_mineru_available({"backend": "hybrid"}) is False

        monkeypatch.setattr(M.shutil, "which", lambda _name: "/usr/local/bin/mineru")
        assert M.is_mineru_available({"backend": "hybrid"}) is True

    def test_available_explicit_executable_missing(self, tmp_path):
        cfg = {"backend": "hybrid", "executable": str(tmp_path / "nonexistent")}
        assert M.is_mineru_available(cfg) is False


# --------------------------------------------------------------------------- #
# CLI subprocess invocation & hybrid→pipeline fallback
# --------------------------------------------------------------------------- #
class TestCliFallback:
    def _make_completed(self, returncode=0, stdout="", stderr="", md_content=""):
        """Build a CompletedProcess plus a side_effect that also writes the .md."""
        result = subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)

        def _run_side_effect(cmd, **kwargs):
            # Simulate MinerU writing its .md into the -o output dir.
            out_dir = None
            for i, a in enumerate(cmd):
                if a == "-o" and i + 1 < len(cmd):
                    out_dir = Path(cmd[i + 1])
            if out_dir is not None and md_content:
                # MinerU names output after the input stem; the client searches
                # by stem match then size. Write a single .md.
                (out_dir / "paper.md").write_text(md_content, encoding="utf-8")
            return result

        return _run_side_effect

    def test_hybrid_success(self, tmp_path, monkeypatch):
        pdf = tmp_path / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        config = {"enabled": True, "backend": "hybrid", "timeout": 30}
        monkeypatch.setattr(M.shutil, "which", lambda _n: "/usr/local/bin/mineru")
        monkeypatch.setattr(
            M.subprocess,
            "run",
            self._make_completed(md_content="# Page\n\n$$x^2$$ content"),
        )

        parsed = M.read_cached_or_parse("ATTKEY", pdf, config, force_rebuild=True)
        assert parsed is not None
        # "hybrid" is normalized to "hybrid-auto-engine" (MinerU 3.x canonical name)
        assert parsed.source == "mineru:hybrid-auto-engine"
        assert "x^2" in parsed.markdown

    def test_hybrid_failure_falls_back_to_pipeline(self, tmp_path, monkeypatch):
        pdf = tmp_path / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        config = {"enabled": True, "backend": "hybrid", "timeout": 30}
        monkeypatch.setattr(M.shutil, "which", lambda _n: "/usr/local/bin/mineru")

        call_count = {"n": 0}

        def _run(cmd, **kwargs):
            call_count["n"] += 1
            out_dir = None
            for i, a in enumerate(cmd):
                if a == "-o" and i + 1 < len(cmd):
                    out_dir = Path(cmd[i + 1])
            # First call (hybrid) fails; second call (pipeline) succeeds.
            if "-b" in cmd:
                backend = cmd[cmd.index("-b") + 1]
                if backend == "hybrid":
                    return subprocess.CompletedProcess(cmd, returncode=1, stderr="OOM")
                if backend == "pipeline" and out_dir is not None:
                    (out_dir / "paper.md").write_text("pipeline output", encoding="utf-8")
                    return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
            return subprocess.CompletedProcess(cmd, returncode=1, stderr="unknown")

        monkeypatch.setattr(M.subprocess, "run", _run)

        parsed = M.read_cached_or_parse("ATTKEY", pdf, config, force_rebuild=True)
        assert parsed is not None
        assert parsed.source == "mineru:pipeline"
        assert "pipeline output" in parsed.markdown
        assert call_count["n"] == 2  # hybrid tried, then pipeline

    def test_both_backends_fail_returns_none(self, tmp_path, monkeypatch):
        pdf = tmp_path / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        config = {"enabled": True, "backend": "hybrid", "timeout": 30}
        monkeypatch.setattr(M.shutil, "which", lambda _n: "/usr/local/bin/mineru")
        monkeypatch.setattr(
            M.subprocess,
            "run",
            lambda cmd, **k: subprocess.CompletedProcess(cmd, returncode=1, stderr="dead"),
        )

        assert M.read_cached_or_parse("ATTKEY", pdf, config, force_rebuild=True) is None

    def test_timeout_returns_none(self, tmp_path, monkeypatch):
        pdf = tmp_path / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        config = {"enabled": True, "backend": "pipeline", "timeout": 30}
        monkeypatch.setattr(M.shutil, "which", lambda _n: "/usr/local/bin/mineru")

        def _raise_timeout(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, 30)

        monkeypatch.setattr(M.subprocess, "run", _raise_timeout)
        assert M.read_cached_or_parse("ATTKEY", pdf, config, force_rebuild=True) is None

    def test_no_executable_returns_none(self, tmp_path, monkeypatch):
        pdf = tmp_path / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        config = {"enabled": True, "backend": "hybrid"}
        monkeypatch.setattr(M.shutil, "which", lambda _n: None)
        assert M.read_cached_or_parse("ATTKEY", pdf, config, force_rebuild=True) is None


# --------------------------------------------------------------------------- #
# Cache hit / invalidation
# --------------------------------------------------------------------------- #
class TestCache:
    def _config_with_cache(self, cache_dir):
        return {"enabled": True, "backend": "pipeline", "cache_dir": str(cache_dir)}

    def test_cache_hit_skips_parse(self, tmp_path, monkeypatch):
        pdf = tmp_path / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        cache = tmp_path / "cache"
        config = self._config_with_cache(cache)

        # Pre-populate cache with valid meta (matching mtime+size).
        cache_dir = cache / "ATTKEY"
        cache_dir.mkdir(parents=True)
        (cache_dir / "fulltext.md").write_text("cached markdown", encoding="utf-8")
        (cache_dir / "pages.json").write_text(json.dumps(["p1", "p2"]), encoding="utf-8")
        stat = pdf.stat()
        (cache_dir / "meta.json").write_text(
            json.dumps(
                {
                    "pdf_mtime": stat.st_mtime,
                    "pdf_size": stat.st_size,
                }
            ),
            encoding="utf-8",
        )

        parse_called = {"n": 0}
        monkeypatch.setattr(
            M,
            "_dispatch_parse",
            lambda *a, **k: parse_called.__setitem__("n", parse_called["n"] + 1) or ("md", None, "mineru:pipeline"),
        )

        parsed = M.read_cached_or_parse("ATTKEY", pdf, config)
        assert parsed is not None
        assert parsed.source == "mineru:cached"
        assert parsed.pages == ["p1", "p2"]
        assert parse_called["n"] == 0  # cache hit, no parse

    def test_cache_invalidated_on_mtime_change(self, tmp_path, monkeypatch):
        pdf = tmp_path / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        cache = tmp_path / "cache"
        config = self._config_with_cache(cache)

        cache_dir = cache / "ATTKEY"
        cache_dir.mkdir(parents=True)
        (cache_dir / "fulltext.md").write_text("stale", encoding="utf-8")
        (cache_dir / "pages.json").write_text(json.dumps(["stale"]), encoding="utf-8")
        # Stale meta: wrong size triggers rebuild.
        (cache_dir / "meta.json").write_text(
            json.dumps(
                {
                    "pdf_mtime": 1.0,
                    "pdf_size": 99999,
                }
            ),
            encoding="utf-8",
        )

        monkeypatch.setattr(M, "_dispatch_parse", lambda *a, **k: ("fresh markdown", None, "mineru:hybrid"))

        parsed = M.read_cached_or_parse("ATTKEY", pdf, config)
        assert parsed is not None
        assert "fresh" in parsed.markdown
        # Cache should now be refreshed with new content.
        assert "fresh markdown" in (cache_dir / "fulltext.md").read_text()

    def test_force_rebuild_ignores_valid_cache(self, tmp_path, monkeypatch):
        pdf = tmp_path / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        cache = tmp_path / "cache"
        config = self._config_with_cache(cache)

        cache_dir = cache / "ATTKEY"
        cache_dir.mkdir(parents=True)
        (cache_dir / "fulltext.md").write_text("cached", encoding="utf-8")
        (cache_dir / "pages.json").write_text(json.dumps(["cached"]), encoding="utf-8")
        stat = pdf.stat()
        (cache_dir / "meta.json").write_text(
            json.dumps(
                {
                    "pdf_mtime": stat.st_mtime,
                    "pdf_size": stat.st_size,
                }
            ),
            encoding="utf-8",
        )

        monkeypatch.setattr(M, "_dispatch_parse", lambda *a, **k: ("rebuilt", None, "mineru:pipeline"))

        parsed = M.read_cached_or_parse("ATTKEY", pdf, config, force_rebuild=True)
        assert parsed is not None
        assert "rebuilt" in parsed.markdown


# --------------------------------------------------------------------------- #
# Page splitting
# --------------------------------------------------------------------------- #
class TestSplitPages:
    def test_no_content_list_returns_whole_doc_as_single_page(self):
        pages = M._split_pages("whole document text", content_list=None)
        assert pages == ["whole document text"]

    def test_empty_markdown_returns_empty_list(self):
        assert M._split_pages("   ", content_list=None) == []

    def test_content_list_split_by_page_idx(self):
        content_list = [
            {"page_idx": 0, "text": "page zero"},
            {"page_idx": 0, "text": "more zero"},
            {"page_idx": 1, "text": "page one"},
            {"page_idx": 3, "text": "page three"},
        ]
        pages = M._split_pages("md", content_list=content_list)
        # Pages 0,1 present; page 2 absent → empty string; page 3 present.
        assert len(pages) == 4
        assert "zero" in pages[0] and "more zero" in pages[0]
        assert pages[1] == "page one"
        assert pages[2] == ""  # no content on page 3 (index 2)
        assert pages[3] == "page three"

    def test_content_list_not_a_list_falls_back(self):
        # Garbage content_list (not a list) → fallback to whole doc.
        pages = M._split_pages("fallback whole", content_list="not a list")  # type: ignore[arg-type]
        assert pages == ["fallback whole"]


# --------------------------------------------------------------------------- #
# API backend
# --------------------------------------------------------------------------- #
class TestApiBackend:
    # These tests use the fake host ``h`` (non-resolvable); bypass the SSRF
    # host check so the api-backend routing logic can be exercised. The guard
    # itself is covered by TestMineruSSRFGuard below.
    def _bypass_ssrf(self, monkeypatch):
        monkeypatch.setattr(M, "_url_is_public", lambda url: True)

    def test_api_success(self, tmp_path, monkeypatch):
        import io
        import zipfile

        self._bypass_ssrf(monkeypatch)
        pdf = tmp_path / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        config = {"enabled": True, "backend": "api", "api_url": "http://h:8000", "timeout": 30}

        # Build a fake zip response containing a .md.
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("paper.md", "# API output\n\n$$E=mc^2$$")
        zip_bytes = buf.getvalue()

        class _FakeResp:
            status_code = 200
            content = zip_bytes
            text = ""

        fake_requests = type("R", (), {"post": staticmethod(lambda *a, **k: _FakeResp())})()
        monkeypatch.setitem(sys.modules, "requests", fake_requests)
        # The function imports requests lazily inside; patch the module attr too.
        monkeypatch.setattr(M, "_call_mineru_api", M._call_mineru_api)

        parsed = M.read_cached_or_parse("ATTKEY", pdf, config, force_rebuild=True)
        assert parsed is not None
        assert parsed.source == "mineru:api"
        assert "mc^2" in parsed.markdown

    def test_api_failure_returns_none(self, tmp_path, monkeypatch):
        self._bypass_ssrf(monkeypatch)
        pdf = tmp_path / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        config = {"enabled": True, "backend": "api", "api_url": "http://h:8000", "timeout": 30}

        class _FakeResp:
            status_code = 500
            content = b""
            text = "server error"

        fake_requests = type("R", (), {"post": staticmethod(lambda *a, **k: _FakeResp())})()
        monkeypatch.setitem(sys.modules, "requests", fake_requests)

        assert M.read_cached_or_parse("ATTKEY", pdf, config, force_rebuild=True) is None


# --------------------------------------------------------------------------- #
# SSRF guards on cloud/api backend fetches
# --------------------------------------------------------------------------- #
class TestMineruSSRFGuard:
    """The cloud download URL and api upload URL are server-controlled, so
    they must be validated through the SSRF guard before any HTTP request —
    a malicious/compromised mineru.net response pointing at a private host or
    the 169.254.169.254 cloud-metadata endpoint must be rejected."""

    def test_url_is_public_rejects_loopback(self):
        assert M._url_is_public("http://127.0.0.1:23119/x") is False

    def test_url_is_public_rejects_cloud_metadata(self):
        assert M._url_is_public("http://169.254.169.254/latest/meta-data/") is False

    def test_url_is_public_rejects_private_range(self):
        assert M._url_is_public("http://10.0.0.5/secret.pdf") is False
        assert M._url_is_public("http://192.168.1.1/x") is False

    def test_url_is_public_rejects_non_http_scheme(self):
        assert M._url_is_public("file:///etc/passwd") is False
        assert M._url_is_public("gopher://127.0.0.1/x") is False

    def test_url_is_public_allows_public_host(self):
        # mineru.net is a real public host; the resolver should accept it.
        assert M._url_is_public("https://mineru.net/api/v4/x") is True

    def test_cloud_download_zip_rejects_loopback(self, monkeypatch):
        """A loopback download URL is rejected before requests.get is called."""
        monkeypatch.setattr(M, "_url_is_public", lambda url: False)
        result = M._cloud_download_zip("http://127.0.0.1/evil.zip")
        assert result is None

    def test_cloud_download_zip_redirect_to_private_rejected(self, monkeypatch):
        """A public URL that 302-redirects to a private host is rejected."""
        calls = {"n": 0}

        def fake_public(url):
            calls["n"] += 1
            # First check (public start URL) passes; the redirect target fails.
            return calls["n"] == 1

        monkeypatch.setattr(M, "_url_is_public", fake_public)

        class _FakeResp:
            status_code = 302
            headers = {"Location": "http://127.0.0.1/secret"}

            def close(self):
                pass

        import requests

        monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResp())
        result = M._cloud_download_zip("https://mineru.net/result.zip")
        assert result is None
