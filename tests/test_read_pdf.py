"""Tests for zotero_read_pdf_pages tool."""

import sys
import types

import pytest
from conftest import DummyContext, FakeZotero

from zotero_mcp import server

# ---------------------------------------------------------------------------
# Helpers: fake fitz module and document
# ---------------------------------------------------------------------------


class FakePage:
    def __init__(self, text):
        self._text = text

    def get_text(self):
        return self._text


class FakeDocument:
    def __init__(self, pages, total=None):
        self._pages = pages
        self._total = total if total is not None else len(pages)

    def __len__(self):
        return self._total

    def __getitem__(self, index):
        return self._pages[index]

    def close(self):
        pass


def _make_fake_fitz(pages, total=None):
    fake_fitz = types.ModuleType("fitz")
    fake_fitz.open = lambda *args, **kwargs: FakeDocument(pages, total)  # noqa: ARG005
    return fake_fitz


def _patch_fitz(monkeypatch, pages, total=None):
    fake_fitz = _make_fake_fitz(pages, total)
    monkeypatch.setitem(sys.modules, "fitz", fake_fitz)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def dummy_ctx():
    return DummyContext()


@pytest.fixture
def fake_zot():
    return FakeZotero()


@pytest.fixture(autouse=True)
def _disable_mineru(monkeypatch):
    """These tests exercise the PyMuPDF fallback path; disable MinerU so the
    async background-parse path (introduced to avoid MCP timeouts on cold
    cache) is not triggered. Without this, a user with a real cloud_token
    configured would see "parse started" messages instead of page content.
    """
    from zotero_mcp import mineru_client

    monkeypatch.setattr(mineru_client, "load_mineru_config", lambda: {})
    monkeypatch.setattr(mineru_client, "is_mineru_enabled", lambda _c: False)



# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHappyPath:
    """Single page and page range reads."""

    def test_single_page(self, monkeypatch, dummy_ctx, fake_zot):
        _patch_fitz(monkeypatch, [FakePage("Page 1 content.")] * 10, total=10)
        monkeypatch.setattr(
            "zotero_mcp.tools.read_pdf._get_pdf_path",
            lambda _k, _c: ("/tmp/test.pdf", "Test Paper", "ATTKEY"),
        )

        result = server.read_pdf_pages(item_key="ITEM01", start_page=3, ctx=dummy_ctx)

        assert "## Page 3" in result
        assert "Page 1 content." in result

    def test_page_range(self, monkeypatch, dummy_ctx, fake_zot):
        pages = [
            FakePage("Content of page 1."),
            FakePage("Content of page 2."),
            FakePage("Content of page 3."),
            FakePage("Content of page 4."),
            FakePage("Content of page 5."),
        ]
        _patch_fitz(monkeypatch, pages)
        monkeypatch.setattr(
            "zotero_mcp.tools.read_pdf._get_pdf_path",
            lambda _k, _c: ("/tmp/test.pdf", "Test Paper", "ATTKEY"),
        )

        result = server.read_pdf_pages(item_key="ITEM01", start_page=2, end_page=4, ctx=dummy_ctx)

        assert "## Page 2" in result
        assert "Content of page 2." in result
        assert "## Page 3" in result
        assert "Content of page 3." in result
        assert "## Page 4" in result
        assert "Content of page 4." in result
        assert "## Page 1" not in result
        assert "## Page 5" not in result

    def test_header_contains_metadata(self, monkeypatch, dummy_ctx, fake_zot):
        _patch_fitz(monkeypatch, [FakePage("hello")])
        monkeypatch.setattr(
            "zotero_mcp.tools.read_pdf._get_pdf_path",
            lambda _k, _c: ("/tmp/test.pdf", "My Paper Title", "ATTKEY"),
        )

        result = server.read_pdf_pages(item_key="KEY123", start_page=1, ctx=dummy_ctx)

        assert "# PDF Pages 1-1 of My Paper Title" in result
        assert "**Item Key:** KEY123" in result
        assert "**Total pages in PDF:** 1" in result
        assert "**Coverage:** p.1-1/1" in result
        assert "**Cache:** 不适用" in result


class TestErrors:
    """Input validation and error cases."""

    def test_empty_item_key(self, dummy_ctx):
        result = server.read_pdf_pages(item_key="", start_page=1, ctx=dummy_ctx)
        assert "item_key cannot be empty" in result

    def test_whitespace_item_key(self, dummy_ctx):
        result = server.read_pdf_pages(item_key="   ", start_page=1, ctx=dummy_ctx)
        assert "item_key cannot be empty" in result

    def test_end_page_less_than_start_page(self, dummy_ctx):
        result = server.read_pdf_pages(item_key="ITEM01", start_page=5, end_page=3, ctx=dummy_ctx)
        assert "end_page must be greater than or equal to start_page" in result

    def test_no_pdf_attachment(self, monkeypatch, dummy_ctx, fake_zot):
        monkeypatch.setattr(
            "zotero_mcp.tools.read_pdf._get_pdf_path",
            lambda _k, _c: None,
        )

        result = server.read_pdf_pages(item_key="ITEM01", start_page=1, ctx=dummy_ctx)

        assert "No PDF attachment found" in result

    def test_start_page_out_of_range(self, monkeypatch, dummy_ctx, fake_zot):
        _patch_fitz(monkeypatch, [FakePage("p1")], total=1)
        monkeypatch.setattr(
            "zotero_mcp.tools.read_pdf._get_pdf_path",
            lambda _k, _c: ("/tmp/test.pdf", "Paper", "ATTKEY"),
        )

        result = server.read_pdf_pages(item_key="ITEM01", start_page=5, ctx=dummy_ctx)

        assert "out of range" in result
        assert "1-1" in result

    def test_end_page_out_of_range(self, monkeypatch, dummy_ctx, fake_zot):
        _patch_fitz(monkeypatch, [FakePage("p1")] * 3, total=3)
        monkeypatch.setattr(
            "zotero_mcp.tools.read_pdf._get_pdf_path",
            lambda _k, _c: ("/tmp/test.pdf", "Paper", "ATTKEY"),
        )

        result = server.read_pdf_pages(item_key="ITEM01", start_page=1, end_page=10, ctx=dummy_ctx)

        assert "out of range" in result
        assert "1-3" in result

    def test_large_page_range_no_cap(self, monkeypatch, dummy_ctx, fake_zot):
        """The 50-page-per-call cap has been removed; requesting 55 pages of a
        100-page PDF should succeed (PyMuPDF fallback), not error."""
        _patch_fitz(monkeypatch, [FakePage("p")] * 100, total=100)
        monkeypatch.setattr(
            "zotero_mcp.tools.read_pdf._get_pdf_path",
            lambda _k, _c: ("/tmp/test.pdf", "Paper", "ATTKEY"),
        )

        result = server.read_pdf_pages(item_key="ITEM01", start_page=1, end_page=55, ctx=dummy_ctx)

        # No error; PyMuPDF fallback returned content for all 55 pages.
        assert "max 50" not in result
        assert "PyMuPDF (fallback)" in result
        assert "## Page 1" in result
        assert "## Page 55" in result

    def test_missing_fitz_module(self, monkeypatch, dummy_ctx, fake_zot):
        monkeypatch.setattr(
            "zotero_mcp.tools.read_pdf._get_pdf_path",
            lambda _k, _c: ("/tmp/test.pdf", "Paper", "ATTKEY"),
        )
        monkeypatch.setitem(sys.modules, "fitz", None)

        result = server.read_pdf_pages(item_key="ITEM01", start_page=1, ctx=dummy_ctx)

        assert "PyMuPDF" in result


class TestEdgeCases:
    """Edge case behaviors."""

    def test_end_page_equals_start_page(self, monkeypatch, dummy_ctx, fake_zot):
        """When end_page == start_page, should behave like single page."""
        _patch_fitz(monkeypatch, [FakePage("p1"), FakePage("p2"), FakePage("p3")])
        monkeypatch.setattr(
            "zotero_mcp.tools.read_pdf._get_pdf_path",
            lambda _k, _c: ("/tmp/test.pdf", "Test Paper", "ATTKEY"),
        )

        result = server.read_pdf_pages(item_key="ITEM01", start_page=2, end_page=2, ctx=dummy_ctx)

        assert "## Page 2" in result
        assert "## Page 1" not in result
        assert "## Page 3" not in result

    def test_reads_last_page(self, monkeypatch, dummy_ctx, fake_zot):
        _patch_fitz(monkeypatch, [FakePage("first"), FakePage("last")])
        monkeypatch.setattr(
            "zotero_mcp.tools.read_pdf._get_pdf_path",
            lambda _k, _c: ("/tmp/test.pdf", "Paper", "ATTKEY"),
        )

        result = server.read_pdf_pages(item_key="ITEM01", start_page=2, ctx=dummy_ctx)

        assert "## Page 2" in result
        assert "last" in result

    def test_continue_footer_when_pages_remain(self, monkeypatch, dummy_ctx, fake_zot):
        """Reading pages 1-3 of a 10-page PDF should append a 'continue' footer."""
        _patch_fitz(monkeypatch, [FakePage(f"p{i}") for i in range(10)], total=10)
        monkeypatch.setattr(
            "zotero_mcp.tools.read_pdf._get_pdf_path",
            lambda _k, _c: ("/tmp/test.pdf", "Paper", "ATTKEY"),
        )

        result = server.read_pdf_pages(item_key="ITEM01", start_page=1, end_page=3, ctx=dummy_ctx)

        assert "Read pages 1-3 of 10" in result
        assert "Pages 4-10 not yet read" in result
        assert "start_page=4" in result

    def test_no_continue_footer_when_last_page_read(self, monkeypatch, dummy_ctx, fake_zot):
        """Reading through the last page should NOT append a 'continue' footer."""
        _patch_fitz(monkeypatch, [FakePage(f"p{i}") for i in range(5)], total=5)
        monkeypatch.setattr(
            "zotero_mcp.tools.read_pdf._get_pdf_path",
            lambda _k, _c: ("/tmp/test.pdf", "Paper", "ATTKEY"),
        )

        result = server.read_pdf_pages(item_key="ITEM01", start_page=1, end_page=5, ctx=dummy_ctx)

        assert "not yet read" not in result
        assert "continue" not in result.lower()

    def test_empty_page_text(self, monkeypatch, dummy_ctx, fake_zot):
        _patch_fitz(monkeypatch, [FakePage(""), FakePage("has text"), FakePage("")])
        monkeypatch.setattr(
            "zotero_mcp.tools.read_pdf._get_pdf_path",
            lambda _k, _c: ("/tmp/test.pdf", "Paper", "ATTKEY"),
        )

        result = server.read_pdf_pages(item_key="ITEM01", start_page=1, end_page=3, ctx=dummy_ctx)

        assert "[No extractable text on this page]" in result
        assert "has text" in result
