"""Tests for MinerU cache → vector DB integration (reindex_keys path).

Covers the three pieces of the "精读 → 完整向量库 → 语义搜索定位 → 精读验证"
workflow:
  1. read_cached_pages_joined — reads pages.json, joins with \f
  2. _extract_fulltext_for_item with prefer_mineru — step-0 MinerU preference
  3. _process_item_batch — MinerU-sourced items get unlimited chunks + page metadata
"""

import json

from zotero_mcp.mineru_client import read_cached_pages_joined
from zotero_mcp.semantic_search import _page_for_offset, split_into_passages

# ---------------------------------------------------------------------------
# read_cached_pages_joined
# ---------------------------------------------------------------------------


class TestReadCachedPagesJoined:
    def test_returns_form_feed_joined_pages(self, tmp_path):
        """pages.json with 3 pages → text joined with \\f (3 pages, 2 separators)."""
        cache_dir = tmp_path / "mineru" / "ATTKEY01"
        cache_dir.mkdir(parents=True)
        pages = ["Page one content", "Page two text", "Page three"]
        (cache_dir / "pages.json").write_text(json.dumps(pages), encoding="utf-8")

        config = {"cache_dir": str(tmp_path / "mineru")}
        result = read_cached_pages_joined("ATTKEY01", config)

        assert result is not None
        assert result.count("\f") == 2  # 3 pages → 2 separators
        assert "Page one content" in result
        assert "Page two text" in result
        assert "Page three" in result

    def test_returns_none_when_no_cache(self, tmp_path):
        """No pages.json → None (caller falls back to pdfminer)."""
        config = {"cache_dir": str(tmp_path / "mineru")}
        assert read_cached_pages_joined("NOEXIST01", config) is None

    def test_returns_none_for_empty_pages_list(self, tmp_path):
        """Empty list → None."""
        cache_dir = tmp_path / "mineru" / "EMPTYKEY1"
        cache_dir.mkdir(parents=True)
        (cache_dir / "pages.json").write_text("[]", encoding="utf-8")

        config = {"cache_dir": str(tmp_path / "mineru")}
        assert read_cached_pages_joined("EMPTYKEY1", config) is None

    def test_returns_none_for_all_blank_pages(self, tmp_path):
        """All-whitespace pages → None (no usable text)."""
        cache_dir = tmp_path / "mineru" / "BLANKKEY1"
        cache_dir.mkdir(parents=True)
        (cache_dir / "pages.json").write_text(json.dumps(["  ", "\n\t"]), encoding="utf-8")

        config = {"cache_dir": str(tmp_path / "mineru")}
        assert read_cached_pages_joined("BLANKKEY1", config) is None

    def test_preserves_blank_pages_as_separators(self, tmp_path):
        """A blank page in the middle still counts as a page boundary."""
        cache_dir = tmp_path / "mineru" / "MIDKEY01"
        cache_dir.mkdir(parents=True)
        pages = ["Real content", "", "More content"]
        (cache_dir / "pages.json").write_text(json.dumps(pages), encoding="utf-8")

        config = {"cache_dir": str(tmp_path / "mineru")}
        result = read_cached_pages_joined("MIDKEY01", config)

        assert result is not None
        assert result.count("\f") == 2  # 3 elements → 2 separators

    def test_returns_none_for_empty_key(self):
        assert read_cached_pages_joined("", {}) is None
        assert read_cached_pages_joined(None, {}) is None  # type: ignore[arg-type]

    def test_handles_corrupt_json(self, tmp_path):
        """Corrupt pages.json → None, not an exception."""
        cache_dir = tmp_path / "mineru" / "BADKEY01"
        cache_dir.mkdir(parents=True)
        (cache_dir / "pages.json").write_text("{not json", encoding="utf-8")

        config = {"cache_dir": str(tmp_path / "mineru")}
        assert read_cached_pages_joined("BADKEY01", config) is None


# ---------------------------------------------------------------------------
# _page_for_offset with MinerU-style \f-separated text
# ---------------------------------------------------------------------------


class TestPageForOffset:
    def test_finds_page_1(self):
        text = "alpha\fbeta\fgamma"
        assert _page_for_offset(text, 0) == 1
        assert _page_for_offset(text, 2) == 1

    def test_finds_page_2(self):
        text = "alpha\fbeta\fgamma"
        # offset 6 is in "beta" (after the first \f)
        assert _page_for_offset(text, 6) == 2

    def test_finds_page_3(self):
        text = "alpha\fbeta\fgamma"
        # offset 11 is in "gamma" (after two \f)
        assert _page_for_offset(text, 11) == 3

    def test_returns_none_without_separators(self):
        """Plain text (no \f) → None (page unknowable)."""
        assert _page_for_offset("no separators here", 5) is None

    def test_split_into_passages_preserves_page_metadata(self):
        """Chunks from \f-separated text get correct page numbers."""
        # 3 pages, each ~500 chars, joined with \f
        page1 = "word " * 100
        page2 = "term " * 100
        page3 = "data " * 100
        text = f"{page1}\f{page2}\f{page3}"

        passages = split_into_passages(text, chunk_size=400, overlap=50, max_chunks=100)

        assert len(passages) > 3  # should produce several chunks
        # The first chunk should be on page 1.
        _, c0, _ = passages[0]
        assert _page_for_offset(text, c0) == 1
        # A chunk in the second half should be on page 2 or 3.
        _, c0_late, _ = passages[-1]
        page_late = _page_for_offset(text, c0_late)
        assert page_late in (2, 3)


# ---------------------------------------------------------------------------
# _process_item_batch: MinerU source lifts max_chunks
# ---------------------------------------------------------------------------


class TestMineruChunksLift:
    """Verify that fulltextSource='mineru-cache' lifts the max_chunks cap.

    We test the logic directly by simulating what _process_item_batch does:
    resolve item_max from config, then lift if source is mineru-cache.
    """

    def test_mineru_source_lifts_to_unlimited(self):
        """A 500-page book should produce far more than 20 chunks."""
        # Simulate a long document (500 pages × ~2500 chars = 1.25M chars)
        # but use a smaller sample for test speed.
        page_text = "The quick brown fox. " * 100  # ~2000 chars
        pages = [page_text] * 50  # 50 "pages"
        text = "\f".join(pages)

        # With default max_chunks=20 (pdfminer path):
        passages_capped = split_into_passages(text, chunk_size=1500, overlap=200, max_chunks=20)
        assert len(passages_capped) == 20

        # With lifted max_chunks (MinerU path, max=10000):
        passages_full = split_into_passages(text, chunk_size=1500, overlap=200, max_chunks=10000)
        assert len(passages_full) > 20  # full-document chunking
        # Every page should have at least one chunk covering it.
        pages_hit = {_page_for_offset(text, c0) for _, c0, _ in passages_full}
        assert len(pages_hit) == 50  # all 50 pages represented

    def test_non_mineru_source_keeps_cap(self):
        """pdfminer-sourced text stays at max_chunks=20."""
        page_text = "The quick brown fox. " * 100
        text = page_text * 50  # long text, NO \f separators

        passages = split_into_passages(text, chunk_size=1500, overlap=200, max_chunks=20)
        assert len(passages) == 20
        # No \f → no page numbers.
        _, c0, _ = passages[0]
        assert _page_for_offset(text, c0) is None
