"""Tests for the MinerU-first / PyMuPDF-fallback routing in read_pdf_pages.

Patches mineru_client and _get_pdf_path so no real Zotero/PDF/MinerU I/O
happens. Verifies: (1) MinerU path wins when enabled+available; (2) every
MinerU failure mode falls back to PyMuPDF; (3) disabled config uses PyMuPDF
with zero behavior change.
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from zotero_mcp.tools import read_pdf
from zotero_mcp import mineru_client


class _FakeFitzDoc:
    """Minimal fitz document stub for the PyMuPDF fallback path."""

    def __init__(self, pages_text):
        self._pages = pages_text

    def __len__(self):
        return len(self._pages)

    def __getitem__(self, i):
        return _FakeFitzPage(self._pages[i])

    def close(self):
        pass


class _FakeFitzPage:
    def __init__(self, text):
        self._text = text

    def get_text(self):
        return self._text


class _FakeCtx:
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass


@pytest.fixture
def ctx():
    return _FakeCtx()


def _patch_path(monkeypatch, pdf_path, title, att_key):
    """Patch _get_pdf_path to return a fixed (path, title, key) tuple."""
    monkeypatch.setattr(
        read_pdf, "_get_pdf_path",
        lambda item_key, _ctx: (str(pdf_path), title, att_key),
    )


def _patch_pymupdf(monkeypatch, pages_text):
    """Patch _probe_total_pages and the fitz open used by the fallback."""

    class _FakeFitzModule:
        @staticmethod
        def open(_path):
            return _FakeFitzDoc(pages_text)

    monkeypatch.setattr(read_pdf, "_probe_total_pages", lambda _p: len(pages_text))
    # _extract_with_pymupdf imports fitz locally; inject our fake module.
    monkeypatch.setitem(sys.modules, "fitz", _FakeFitzModule)


class TestMineruPreferred:
    def test_mineru_enabled_and_available_wins(self, tmp_path, monkeypatch, ctx):
        pdf = tmp_path / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        _patch_path(monkeypatch, pdf, "Test Paper", "ATTKEY")
        # PyMuPDF fallback must NOT run; report 3 pages so requesting page 2
        # passes range validation, then MinerU serves it from its own pages list.
        monkeypatch.setattr(read_pdf, "_probe_total_pages", lambda _p: 3)
        monkeypatch.setitem(sys.modules, "fitz", type("F", (), {"open": staticmethod(lambda _p: _FakeFitzDoc(["MUST NOT RUN"]))}))

        monkeypatch.setattr(mineru_client, "load_mineru_config",
                            lambda: {"enabled": True, "backend": "hybrid"})
        monkeypatch.setattr(mineru_client, "is_mineru_enabled", lambda _c: True)
        monkeypatch.setattr(mineru_client, "is_mineru_available", lambda _c: True)

        parsed = mineru_client.ParseResult(
            markdown="# full doc\n\n$$Attention(Q,K,V)=softmax(...)$$",
            pages=["page 0 content", "$$formula$$", "page 2"],
            source="mineru:hybrid",
        )
        monkeypatch.setattr(mineru_client, "read_cached_or_parse",
                            lambda _key, _path, _cfg: parsed)

        out = read_pdf.read_pdf_pages("ITEM1", 2, 2, ctx=ctx)
        assert "MinerU (hybrid)" in out
        assert "$$formula$$" in out          # structured output preserved
        assert "PyMuPDF" not in out            # fallback did NOT run
        assert "## Page 2" in out

    def test_mineru_disabled_falls_back_silently(self, tmp_path, monkeypatch, ctx):
        pdf = tmp_path / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        _patch_path(monkeypatch, pdf, "Paper", "ATTKEY")
        _patch_pymupdf(monkeypatch, ["page1", "page2 text"])

        monkeypatch.setattr(mineru_client, "load_mineru_config", lambda: {})
        monkeypatch.setattr(mineru_client, "is_mineru_enabled", lambda _c: False)

        out = read_pdf.read_pdf_pages("ITEM1", 2, 2, ctx=ctx)
        assert "PyMuPDF (fallback)" in out
        assert "page2 text" in out
        assert "MinerU" not in out

    def test_mineru_unavailable_falls_back(self, tmp_path, monkeypatch, ctx):
        pdf = tmp_path / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        _patch_path(monkeypatch, pdf, "Paper", "ATTKEY")
        _patch_pymupdf(monkeypatch, ["fallback page"])

        monkeypatch.setattr(mineru_client, "load_mineru_config",
                            lambda: {"enabled": True})
        monkeypatch.setattr(mineru_client, "is_mineru_enabled", lambda _c: True)
        monkeypatch.setattr(mineru_client, "is_mineru_available", lambda _c: False)

        out = read_pdf.read_pdf_pages("ITEM1", 1, 1, ctx=ctx)
        assert "PyMuPDF (fallback)" in out
        assert "fallback page" in out

    def test_mineru_returns_none_falls_back(self, tmp_path, monkeypatch, ctx):
        pdf = tmp_path / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        _patch_path(monkeypatch, pdf, "Paper", "ATTKEY")
        _patch_pymupdf(monkeypatch, ["fallback content"])

        monkeypatch.setattr(mineru_client, "load_mineru_config",
                            lambda: {"enabled": True})
        monkeypatch.setattr(mineru_client, "is_mineru_enabled", lambda _c: True)
        monkeypatch.setattr(mineru_client, "is_mineru_available", lambda _c: True)
        monkeypatch.setattr(mineru_client, "read_cached_or_parse",
                            lambda _k, _p, _c: None)

        out = read_pdf.read_pdf_pages("ITEM1", 1, 1, ctx=ctx)
        assert "PyMuPDF (fallback)" in out

    def test_mineru_raises_falls_back(self, tmp_path, monkeypatch, ctx):
        pdf = tmp_path / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        _patch_path(monkeypatch, pdf, "Paper", "ATTKEY")
        _patch_pymupdf(monkeypatch, ["safe fallback"])

        monkeypatch.setattr(mineru_client, "load_mineru_config",
                            lambda: {"enabled": True})
        monkeypatch.setattr(mineru_client, "is_mineru_enabled", lambda _c: True)
        monkeypatch.setattr(mineru_client, "is_mineru_available", lambda _c: True)

        def _boom(*a, **k):
            raise RuntimeError("mineru crashed")
        monkeypatch.setattr(mineru_client, "read_cached_or_parse", _boom)

        out = read_pdf.read_pdf_pages("ITEM1", 1, 1, ctx=ctx)
        assert "PyMuPDF (fallback)" in out
        assert "safe fallback" in out

    def test_no_attachment_key_skips_mineru(self, tmp_path, monkeypatch, ctx):
        """Without an attachment key we can't cache; MinerU must be skipped."""
        pdf = tmp_path / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        # att_key is None
        monkeypatch.setattr(read_pdf, "_get_pdf_path",
                            lambda item_key, _ctx: (str(pdf), "Paper", None))
        _patch_pymupdf(monkeypatch, ["fallback without key"])

        monkeypatch.setattr(mineru_client, "load_mineru_config",
                            lambda: {"enabled": True})
        monkeypatch.setattr(mineru_client, "is_mineru_enabled", lambda _c: True)
        monkeypatch.setattr(mineru_client, "is_mineru_available", lambda _c: True)

        parse_called = {"n": 0}
        monkeypatch.setattr(mineru_client, "read_cached_or_parse",
                            lambda *a, **k: parse_called.__setitem__("n", 1) or None)

        out = read_pdf.read_pdf_pages("ITEM1", 1, 1, ctx=ctx)
        assert "PyMuPDF (fallback)" in out
        assert parse_called["n"] == 0  # MinerU not invoked without a cache key


class TestRangeValidation:
    def test_start_page_out_of_range(self, tmp_path, monkeypatch, ctx):
        pdf = tmp_path / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        _patch_path(monkeypatch, pdf, "Paper", "ATTKEY")
        monkeypatch.setattr(read_pdf, "_probe_total_pages", lambda _p: 3)
        monkeypatch.setattr(mineru_client, "load_mineru_config", lambda: {})

        out = read_pdf.read_pdf_pages("ITEM1", 10, 12, ctx=ctx)
        assert "out of range" in out
        assert "3 pages" in out

    def test_exceeds_50_pages(self, tmp_path, monkeypatch, ctx):
        pdf = tmp_path / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        _patch_path(monkeypatch, pdf, "Paper", "ATTKEY")
        monkeypatch.setattr(read_pdf, "_probe_total_pages", lambda _p: 100)
        monkeypatch.setattr(mineru_client, "load_mineru_config", lambda: {})

        out = read_pdf.read_pdf_pages("ITEM1", 1, 60, ctx=ctx)
        assert "max 50" in out

    def test_pymupdf_unavailable_blocks_with_clear_message(self, tmp_path, monkeypatch, ctx):
        pdf = tmp_path / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        _patch_path(monkeypatch, pdf, "Paper", "ATTKEY")
        # PyMuPDF unavailable → cannot validate ranges.
        monkeypatch.setattr(read_pdf, "_probe_total_pages", lambda _p: None)
        monkeypatch.setattr(mineru_client, "load_mineru_config", lambda: {})

        out = read_pdf.read_pdf_pages("ITEM1", 1, 1, ctx=ctx)
        assert "PyMuPDF is required" in out
