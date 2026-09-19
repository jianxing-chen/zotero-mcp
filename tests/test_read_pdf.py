"""Tests for zotero_read_pdf_pages tool."""

import sys
import types

import pytest
from conftest import DummyContext, FakeZotero

from zotero_mcp import server
from zotero_mcp.extract import PAGE_SEPARATOR, ExtractedDoc
from zotero_mcp.tools import read_pdf as read_pdf_tools
from zotero_mcp.tools.read_pdf import PdfReadError

# ---------------------------------------------------------------------------
# Helpers: fake fitz module and document
# ---------------------------------------------------------------------------


def _patch_extract(monkeypatch, page_texts, total=None, needs_ocr=()):
    """Stand in for ``extract_pdf``/``pdf_page_count`` with known page text.

    Mirrors the real contract the tool depends on: out-of-range indices are
    dropped, and ``page_numbers`` reports the absolute source page for each
    returned page.
    """
    total_pages = total if total is not None else len(page_texts)

    def _fake_page_count(_path):
        return total_pages

    def _fake_extract_pdf(_path, *, pages=None, max_pages=None):
        wanted = [
            p for p in (range(total_pages) if pages is None else pages)
            if 0 <= p < total_pages
        ]
        texts = [page_texts[p % len(page_texts)] for p in wanted]
        return ExtractedDoc(
            text=PAGE_SEPARATOR.join(texts),
            pages=tuple(texts),
            page_numbers=tuple(wanted),
            page_count=total_pages,
            source="pdf",
            needs_ocr=tuple(needs_ocr),
        )

    monkeypatch.setattr("zotero_mcp.tools.read_pdf.pdf_page_count", _fake_page_count)
    monkeypatch.setattr("zotero_mcp.tools.read_pdf.extract_pdf", _fake_extract_pdf)


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
    """Compat shim over the extract layer (the tool no longer opens fitz here).

    Accepts the historical FakePage list and feeds each page's text through
    the patched ``extract_pdf``/``pdf_page_count`` the tool actually calls.
    """
    texts = [pg.get_text() if hasattr(pg, "get_text") else str(pg) for pg in pages]
    _patch_extract(monkeypatch, texts, total=total)


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
            lambda _k, _c: ("/tmp/test.pdf", "Test Paper", "ATTKEY", False),
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
            lambda _k, _c: ("/tmp/test.pdf", "Test Paper", "ATTKEY", False),
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
            lambda _k, _c: ("/tmp/test.pdf", "My Paper Title", "ATTKEY", False),
        )

        result = server.read_pdf_pages(item_key="KEY123", start_page=1, ctx=dummy_ctx)

        assert "# PDF Pages 1-1 of My Paper Title" in result
        assert "**Item Key:** KEY123" in result
        assert "**Total pages in PDF:** 1" in result


class TestErrors:
    """Input validation and error cases.

    Each of these used to be *returned* as a string, which made a failed read
    indistinguishable from a successful one: `zotero-cli --json read` wrapped
    it in an ``ok: true`` envelope and exited 0. They are raised now, and each
    carries the code the envelope reports, so the assertions check both.
    """

    def test_empty_item_key(self, dummy_ctx):
        with pytest.raises(PdfReadError) as exc:
            server.read_pdf_pages(item_key="", start_page=1, ctx=dummy_ctx)
        assert "item_key cannot be empty" in str(exc.value)
        assert exc.value.code == "empty_item_key"

    def test_whitespace_item_key(self, dummy_ctx):
        with pytest.raises(PdfReadError) as exc:
            server.read_pdf_pages(item_key="   ", start_page=1, ctx=dummy_ctx)
        assert "item_key cannot be empty" in str(exc.value)
        assert exc.value.code == "empty_item_key"

    def test_end_page_less_than_start_page(self, dummy_ctx):
        with pytest.raises(PdfReadError) as exc:
            server.read_pdf_pages(item_key="ITEM01", start_page=5, end_page=3, ctx=dummy_ctx)
        assert "end_page must be greater than or equal to start_page" in str(exc.value)
        assert exc.value.code == "invalid_page_range"

    def test_no_pdf_attachment(self, monkeypatch, dummy_ctx, fake_zot):
        monkeypatch.setattr(
            "zotero_mcp.tools.read_pdf._get_pdf_path",
            lambda _k, _c: None,
        )

        with pytest.raises(PdfReadError) as exc:
            server.read_pdf_pages(item_key="ITEM01", start_page=1, ctx=dummy_ctx)

        assert "No PDF attachment found" in str(exc.value)
        assert exc.value.code == "no_pdf_attachment"

    def test_start_page_out_of_range(self, monkeypatch, dummy_ctx, fake_zot):
        _patch_fitz(monkeypatch, [FakePage("p1")], total=1)
        monkeypatch.setattr(
            "zotero_mcp.tools.read_pdf._get_pdf_path",
            lambda _k, _c: ("/tmp/test.pdf", "Paper", "ATTKEY", False),
        )

        with pytest.raises(PdfReadError) as exc:
            server.read_pdf_pages(item_key="ITEM01", start_page=5, ctx=dummy_ctx)

        assert "out of range" in str(exc.value)
        assert "1-1" in str(exc.value)
        assert exc.value.code == "page_out_of_range"

    def test_end_page_past_the_last_page_is_clamped(self, monkeypatch, dummy_ctx, fake_zot):
        """A caller rarely knows the page count before its first read, and an
        end page that is too large means "to the end". Failing the whole read
        over it cost a retry for nothing."""
        _patch_extract(monkeypatch, ["p1", "p2", "p3"], total=3)
        monkeypatch.setattr(
            "zotero_mcp.tools.read_pdf._get_pdf_path",
            lambda _k, _c: ("/tmp/test.pdf", "Paper", "ATTKEY", False),
        )

        result = server.read_pdf_pages(item_key="ITEM01", start_page=2, end_page=10, ctx=dummy_ctx)

        assert "# PDF Pages 2-3" in result
        assert "End page 10 is past the last page; read through page 3." in result
        assert "## Page 3" in result
        assert "## Page 4" not in result

    def test_large_page_range_no_cap(self, monkeypatch, dummy_ctx, fake_zot):
        """Fork behaviour: text reads carry no page cap. Requesting 55 pages
        of a 100-page PDF succeeds — MinerU serves whole-document slices and
        oversized output is warned about, not refused."""
        _patch_extract(monkeypatch, [f"p{i}" for i in range(1, 101)], total=100)
        monkeypatch.setattr(
            "zotero_mcp.tools.read_pdf._get_pdf_path",
            lambda _k, _c: ("/tmp/test.pdf", "Paper", "ATTKEY", False),
        )

        result = server.read_pdf_pages(item_key="ITEM01", start_page=1, end_page=55, ctx=dummy_ctx)

        assert "## Page 1" in result
        assert "## Page 55" in result
        assert "max" not in result

    def test_missing_fitz_module(self, monkeypatch, dummy_ctx, fake_zot, tmp_path):
        bad = tmp_path / "not-a-pdf.bin"
        bad.write_bytes(b"plain text, no PDF header")
        monkeypatch.setattr(
            "zotero_mcp.tools.read_pdf._get_pdf_path",
            lambda _k, _c: (str(bad), "Paper", "ATTKEY", False),
        )

        with pytest.raises(PdfReadError) as exc:
            server.read_pdf_pages(item_key="ITEM01", start_page=1, ctx=dummy_ctx)

        assert "Could not read PDF" in str(exc.value)
        assert "Not a PDF" in str(exc.value)
        assert exc.value.code == "pdf_unreadable"

    def test_a_failed_read_is_never_a_successful_return(self, monkeypatch, dummy_ctx, fake_zot):
        """The regression guard for the whole class: every failure path above
        has to raise, so a caller can never receive prose where it expected
        pages. Enumerated rather than sampled, because the bug was that one
        path at a time drifted back to returning a string."""
        monkeypatch.setattr(
            "zotero_mcp.tools.read_pdf._get_pdf_path",
            lambda _k, _c: ("/tmp/test.pdf", "Paper", True),
        )
        _patch_extract(monkeypatch, ["p1"] * 3, total=3)

        failures = [
            dict(item_key="", start_page=1),
            dict(item_key="ITEM01", start_page=5, end_page=3),
            dict(item_key="ITEM01", start_page=9),
        ]
        for kwargs in failures:
            with pytest.raises(PdfReadError):
                server.read_pdf_pages(ctx=dummy_ctx, **kwargs)

    def test_validation_precedes_any_lookup(self, monkeypatch, dummy_ctx):
        """The repro in #528 needs no library, key or running Zotero: an
        invalid range is rejected before anything is resolved."""
        def _explode(*_a, **_k):
            raise AssertionError("a rejected range must not reach the client")

        monkeypatch.setattr("zotero_mcp.tools.read_pdf._get_pdf_path", _explode)

        with pytest.raises(PdfReadError):
            server.read_pdf_pages(item_key="TESTKEY1", start_page=2, end_page=1, ctx=dummy_ctx)


class TestEdgeCases:
    """Edge case behaviors."""

    def test_end_page_equals_start_page(self, monkeypatch, dummy_ctx, fake_zot):
        """When end_page == start_page, should behave like single page."""
        _patch_fitz(monkeypatch, [FakePage("p1"), FakePage("p2"), FakePage("p3")])
        monkeypatch.setattr(
            "zotero_mcp.tools.read_pdf._get_pdf_path",
            lambda _k, _c: ("/tmp/test.pdf", "Test Paper", "ATTKEY", False),
        )

        result = server.read_pdf_pages(item_key="ITEM01", start_page=2, end_page=2, ctx=dummy_ctx)

        assert "## Page 2" in result
        assert "## Page 1" not in result
        assert "## Page 3" not in result

    def test_reads_last_page(self, monkeypatch, dummy_ctx, fake_zot):
        _patch_fitz(monkeypatch, [FakePage("first"), FakePage("last")])
        monkeypatch.setattr(
            "zotero_mcp.tools.read_pdf._get_pdf_path",
            lambda _k, _c: ("/tmp/test.pdf", "Paper", "ATTKEY", False),
        )

        result = server.read_pdf_pages(item_key="ITEM01", start_page=2, ctx=dummy_ctx)

        assert "## Page 2" in result
        assert "last" in result

    def test_continue_footer_when_pages_remain(self, monkeypatch, dummy_ctx, fake_zot):
        """Reading pages 1-3 of a 10-page PDF should append a 'continue' footer."""
        _patch_fitz(monkeypatch, [FakePage(f"p{i}") for i in range(10)], total=10)
        monkeypatch.setattr(
            "zotero_mcp.tools.read_pdf._get_pdf_path",
            lambda _k, _c: ("/tmp/test.pdf", "Paper", "ATTKEY", False),
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
            lambda _k, _c: ("/tmp/test.pdf", "Paper", "ATTKEY", False),
        )

        result = server.read_pdf_pages(item_key="ITEM01", start_page=1, end_page=5, ctx=dummy_ctx)

        assert "not yet read" not in result
        assert "continue" not in result.lower()

    def test_empty_page_text(self, monkeypatch, dummy_ctx, fake_zot):
        _patch_fitz(monkeypatch, [FakePage(""), FakePage("has text"), FakePage("")])
        monkeypatch.setattr(
            "zotero_mcp.tools.read_pdf._get_pdf_path",
            lambda _k, _c: ("/tmp/test.pdf", "Paper", "ATTKEY", False),
        )

        result = server.read_pdf_pages(item_key="ITEM01", start_page=1, end_page=3, ctx=dummy_ctx)

        assert "[No extractable text on this page]" in result
        assert "has text" in result
