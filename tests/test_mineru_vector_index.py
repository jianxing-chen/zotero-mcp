"""Tests for MinerU cache → vector DB integration (reindex_keys path).

Covers the "精读 → 完整向量库 → 语义搜索定位 → 精读验证" workflow:
  1. read_cached_pages_joined — reads pages.json, joins with \f
  2. list_cached_attachment_keys — scans cache root for usable parses
  3. _extract_fulltext_for_item with prefer_mineru — step-0 MinerU preference
  4. _process_item_batch — MinerU-sourced items get unlimited chunks + page metadata
"""

import json

from zotero_mcp.mineru_client import list_cached_attachment_keys, read_cached_pages_joined
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


# ---------------------------------------------------------------------------
# list_cached_attachment_keys
# ---------------------------------------------------------------------------


class TestListCachedAttachmentKeys:
    """Scan the MinerU cache root for attachment keys with a usable parse."""

    def _make_cache(self, tmp_path, key, pages):
        cache_dir = tmp_path / "mineru" / key
        cache_dir.mkdir(parents=True)
        (cache_dir / "pages.json").write_text(json.dumps(pages), encoding="utf-8")
        return cache_dir

    def test_returns_keys_with_non_empty_pages(self, tmp_path):
        self._make_cache(tmp_path, "AAAA1111", ["page one", "page two"])
        self._make_cache(tmp_path, "BBBB2222", ["only page"])
        config = {"cache_dir": str(tmp_path / "mineru")}
        keys = list_cached_attachment_keys(config)
        assert keys == {"AAAA1111", "BBBB2222"}

    def test_excludes_dirs_without_pages_json(self, tmp_path):
        """A directory with only fulltext.md (no pages.json) is not ready."""
        cache_dir = tmp_path / "mineru" / "ONLYMD1"
        cache_dir.mkdir(parents=True)
        (cache_dir / "fulltext.md").write_text("content", encoding="utf-8")
        config = {"cache_dir": str(tmp_path / "mineru")}
        assert list_cached_attachment_keys(config) == set()

    def test_excludes_empty_pages_list(self, tmp_path):
        """pages.json with [] is a failed/empty parse — skip it."""
        self._make_cache(tmp_path, "EMPTYKEY", [])
        config = {"cache_dir": str(tmp_path / "mineru")}
        assert list_cached_attachment_keys(config) == set()

    def test_excludes_all_blank_pages(self, tmp_path):
        """All-whitespace pages carry no usable text — skip."""
        self._make_cache(tmp_path, "BLANKKEY", ["  ", "\n\t"])
        config = {"cache_dir": str(tmp_path / "mineru")}
        assert list_cached_attachment_keys(config) == set()

    def test_includes_dir_with_some_real_pages(self, tmp_path):
        """One blank page among real ones still counts (page boundary kept)."""
        self._make_cache(tmp_path, "MIDKEY01", ["real text", "", "more text"])
        config = {"cache_dir": str(tmp_path / "mineru")}
        assert list_cached_attachment_keys(config) == {"MIDKEY01"}

    def test_skips_corrupt_pages_json(self, tmp_path):
        cache_dir = tmp_path / "mineru" / "BADKEY01"
        cache_dir.mkdir(parents=True)
        (cache_dir / "pages.json").write_text("{not json", encoding="utf-8")
        config = {"cache_dir": str(tmp_path / "mineru")}
        assert list_cached_attachment_keys(config) == set()

    def test_missing_cache_dir_returns_empty(self, tmp_path):
        config = {"cache_dir": str(tmp_path / "does-not-exist")}
        assert list_cached_attachment_keys(config) == set()


# ---------------------------------------------------------------------------
# _mineru_index_current — reindex_keys idempotency guard
# ---------------------------------------------------------------------------


class TestMineruIndexCurrent:
    """Idempotency guard: skip reindex when already indexed from valid MinerU cache."""

    def _make_search(self):
        from zotero_mcp.semantic_search import ZoteroSemanticSearch

        # Avoid real config / chroma init — we only call _mineru_index_current.
        return ZoteroSemanticSearch.__new__(ZoteroSemanticSearch)

    def _fake_chroma(self, chunk0_meta):
        from unittest.mock import MagicMock

        c = MagicMock()
        c.get_document_metadata.return_value = chunk0_meta
        return c

    def _fake_reader(self, attachments, mineru_hits):
        from unittest.mock import MagicMock

        r = MagicMock()
        r._iter_parent_attachments.return_value = iter(attachments)
        # _read_mineru_cache returns the cached text (truthy) for hit keys.
        r._read_mineru_cache.side_effect = lambda k: "cached" if k in mineru_hits else None
        return r

    def test_returns_true_when_indexed_from_mineru_and_cache_valid(self):
        """Chunk-0 is mineru-cache + cache readable on disk → skip (True)."""
        s = self._make_search()
        chroma = self._fake_chroma({"fulltext_source": "mineru-cache"})
        reader = self._fake_reader([("ATT1", "p", "application/pdf")], {"ATT1"})
        assert s._mineru_index_current(chroma, reader, "PARENT", 1) is True

    def test_returns_false_when_indexed_from_zotero_cache(self):
        """Not yet upgraded to MinerU index → reindex needed (False)."""
        s = self._make_search()
        chroma = self._fake_chroma({"fulltext_source": "zotero-cache"})
        reader = self._fake_reader([("ATT1", "p", "application/pdf")], {"ATT1"})
        assert s._mineru_index_current(chroma, reader, "PARENT", 1) is False

    def test_returns_false_when_no_metadata_stored(self):
        """Item not yet in the vector DB → reindex needed (False)."""
        s = self._make_search()
        chroma = self._fake_chroma(None)
        reader = self._fake_reader([("ATT1", "p", "application/pdf")], {"ATT1"})
        assert s._mineru_index_current(chroma, reader, "PARENT", 1) is False

    def test_returns_false_when_no_fulltext_source_field(self):
        """Old index without fulltext_source metadata → reindex needed."""
        s = self._make_search()
        chroma = self._fake_chroma({"has_fulltext": True})
        reader = self._fake_reader([("ATT1", "p", "application/pdf")], {"ATT1"})
        assert s._mineru_index_current(chroma, reader, "PARENT", 1) is False

    def test_returns_false_when_mineru_indexed_but_cache_gone(self):
        """Indexed from MinerU but cache deleted/invalidated (PDF replaced)
        → reindex to refresh (False)."""
        s = self._make_search()
        chroma = self._fake_chroma({"fulltext_source": "mineru-cache"})
        reader = self._fake_reader([("ATT1", "p", "application/pdf")], set())
        assert s._mineru_index_current(chroma, reader, "PARENT", 1) is False

    def test_returns_true_if_any_attachment_has_valid_cache(self):
        """Multiple attachments — one with a live cache is enough."""
        s = self._make_search()
        chroma = self._fake_chroma({"fulltext_source": "mineru-cache"})
        reader = self._fake_reader([("ATT1", "p", "application/pdf"), ("ATT2", "p", "application/pdf")], {"ATT2"})
        assert s._mineru_index_current(chroma, reader, "PARENT", 1) is True


# ---------------------------------------------------------------------------
# _resolve_cached_mineru_keys — batch expansion of MinerU caches → parent keys
# ---------------------------------------------------------------------------


class TestResolveCachedMineruKeys:
    """Expand MinerU cache directory → parent item keys for batch reindex."""

    def _make_search(self, db_path=None):
        from zotero_mcp.semantic_search import ZoteroSemanticSearch

        s = ZoteroSemanticSearch.__new__(ZoteroSemanticSearch)
        s.db_path = db_path
        return s

    def test_returns_empty_when_no_caches(self, monkeypatch):
        s = self._make_search()
        monkeypatch.setattr("zotero_mcp.mineru_client.list_cached_attachment_keys", lambda: set())
        assert s._resolve_cached_mineru_keys() == []

    def test_maps_attachment_keys_to_parent_keys(self, monkeypatch, tmp_path):
        """Cache has ATT1/ATT2 → parents P1/P2 (deduplicated)."""
        s = self._make_search(db_path=str(tmp_path / "zotero.sqlite"))
        monkeypatch.setattr(
            "zotero_mcp.mineru_client.list_cached_attachment_keys",
            lambda: {"ATT1", "ATT2"},
        )

        # Fake the LocalZoteroReader context manager + reverse lookup.
        from contextlib import contextmanager

        class FakeReader:
            def __init__(self, mapping):
                self._mapping = mapping

            def get_parent_keys_for_attachments(self, att_keys):
                return {k: v for k, v in self._mapping.items() if k in att_keys}

        mapping = {"ATT1": "P1", "ATT2": "P2"}
        monkeypatch.setattr(
            "zotero_mcp.semantic_search.LocalZoteroReader",
            lambda db_path=None: contextmanager(lambda: (yield FakeReader(mapping)))(),
        )
        result = s._resolve_cached_mineru_keys()
        assert sorted(result) == ["P1", "P2"]

    def test_deduplicates_parent_keys(self, monkeypatch, tmp_path):
        """Two cached attachments under the same parent → one parent key."""
        s = self._make_search(db_path=str(tmp_path / "zotero.sqlite"))
        monkeypatch.setattr(
            "zotero_mcp.mineru_client.list_cached_attachment_keys",
            lambda: {"ATT1", "ATT2"},
        )
        from contextlib import contextmanager

        class FakeReader:
            def __init__(self, mapping):
                self._mapping = mapping

            def get_parent_keys_for_attachments(self, att_keys):
                return {k: v for k, v in self._mapping.items() if k in att_keys}

        mapping = {"ATT1": "PARENT", "ATT2": "PARENT"}
        monkeypatch.setattr(
            "zotero_mcp.semantic_search.LocalZoteroReader",
            lambda db_path=None: contextmanager(lambda: (yield FakeReader(mapping)))(),
        )
        assert s._resolve_cached_mineru_keys() == ["PARENT"]

    def test_drops_standalone_attachments(self, monkeypatch, tmp_path):
        """An attachment with no parent is absent from the mapping → dropped."""
        s = self._make_search(db_path=str(tmp_path / "zotero.sqlite"))
        monkeypatch.setattr(
            "zotero_mcp.mineru_client.list_cached_attachment_keys",
            lambda: {"ATT1", "ORPHAN"},
        )
        from contextlib import contextmanager

        class FakeReader:
            def __init__(self, mapping):
                self._mapping = mapping

            def get_parent_keys_for_attachments(self, att_keys):
                return {k: v for k, v in self._mapping.items() if k in att_keys}

        # ORPHAN has no parent → not in mapping
        mapping = {"ATT1": "P1"}
        monkeypatch.setattr(
            "zotero_mcp.semantic_search.LocalZoteroReader",
            lambda db_path=None: contextmanager(lambda: (yield FakeReader(mapping)))(),
        )
        assert s._resolve_cached_mineru_keys() == ["P1"]


# ---------------------------------------------------------------------------
# get_database_status — mineru_cache observability
# ---------------------------------------------------------------------------


class TestGetDatabaseStatusMineruCache:
    """The status dict reports MinerU cache / index / pending counts."""

    def _make_search(self):
        from unittest.mock import MagicMock

        from zotero_mcp.semantic_search import ZoteroSemanticSearch

        s = ZoteroSemanticSearch.__new__(ZoteroSemanticSearch)
        s.chroma_client = MagicMock()
        s.chroma_client.get_collection_info.return_value = {"count": 0}
        s.update_config = {"last_update": None}
        s.config_path = None
        s.db_path = None
        s.zotero_client = MagicMock()
        # _load_* helpers are called by get_database_status; stub them.
        s._load_openai_batch_enabled = lambda: False
        s._resolve_openai_batch_enabled = lambda v: False
        s.should_update_database = lambda: False
        return s

    def test_reports_cached_count(self, monkeypatch):
        s = self._make_search()
        monkeypatch.setattr("zotero_mcp.mineru_client.list_cached_attachment_keys", lambda: {"A", "B"})
        monkeypatch.setattr("zotero_mcp.semantic_search.is_local_mode", lambda: False)
        s._count_mineru_indexed_items = lambda: 0
        status = s.get_database_status()
        mc = status["mineru_cache"]
        assert mc["cached_attachment_keys"] == 2
        assert mc["indexed_from_mineru"] == 0
        # pending is None without local mode (can't map att→parent)
        assert mc["pending"] is None

    def test_reports_indexed_from_mineru_count(self, monkeypatch):
        s = self._make_search()
        monkeypatch.setattr("zotero_mcp.mineru_client.list_cached_attachment_keys", lambda: set())
        s._count_mineru_indexed_items = lambda: 5
        status = s.get_database_status()
        assert status["mineru_cache"]["indexed_from_mineru"] == 5

    def test_pending_count_in_local_mode(self, monkeypatch, tmp_path):
        """Local mode: 3 cached parents, 1 already indexed → pending=2."""
        s = self._make_search()
        s.db_path = str(tmp_path / "zotero.sqlite")
        monkeypatch.setattr("zotero_mcp.mineru_client.list_cached_attachment_keys", lambda: {"ATT1", "ATT2", "ATT3"})
        monkeypatch.setattr("zotero_mcp.semantic_search.is_local_mode", lambda: True)
        s._count_mineru_indexed_items = lambda: 1

        from contextlib import contextmanager

        class FakeReader:
            def get_parent_keys_for_attachments(self, att_keys):
                return {"ATT1": "P1", "ATT2": "P2", "ATT3": "P3"}

        monkeypatch.setattr(
            "zotero_mcp.semantic_search.LocalZoteroReader",
            lambda db_path=None: contextmanager(lambda: (yield FakeReader()))(),
        )
        status = s.get_database_status()
        assert status["mineru_cache"]["pending"] == 2

    def test_no_cache_returns_zeros(self, monkeypatch):
        s = self._make_search()
        monkeypatch.setattr("zotero_mcp.mineru_client.list_cached_attachment_keys", lambda: set())
        s._count_mineru_indexed_items = lambda: 0
        status = s.get_database_status()
        mc = status["mineru_cache"]
        assert mc["cached_attachment_keys"] == 0
        assert mc["indexed_from_mineru"] == 0


class TestCountMineruIndexedItems:
    """Scan chunk-0 metadatas for fulltext_source='mineru-cache'."""

    def _make_search(self):
        from unittest.mock import MagicMock

        from zotero_mcp.semantic_search import ZoteroSemanticSearch

        s = ZoteroSemanticSearch.__new__(ZoteroSemanticSearch)
        s.chroma_client = MagicMock()
        return s

    def test_counts_chunk0_with_mineru_source(self):
        s = self._make_search()
        s.chroma_client.collection.get.return_value = {
            "ids": ["K1#0", "K1#1", "K2#0", "K3#0", "K3#1"],
            "metadatas": [
                {"fulltext_source": "mineru-cache"},
                {"fulltext_source": "mineru-cache"},
                {"fulltext_source": "zotero-cache"},
                {"fulltext_source": "mineru-cache"},
                {"fulltext_source": "mineru-cache"},
            ],
        }
        # K1#0, K3#0 are mineru → 2 items
        assert s._count_mineru_indexed_items() == 2

    def test_ignores_non_chunk0(self):
        """Only chunk-0 ids count (one per item)."""
        s = self._make_search()
        s.chroma_client.collection.get.return_value = {
            "ids": ["K1#1", "K1#2"],  # no #0
            "metadatas": [{"fulltext_source": "mineru-cache"}, {"fulltext_source": "mineru-cache"}],
        }
        assert s._count_mineru_indexed_items() == 0

    def test_returns_zero_on_error(self):
        s = self._make_search()
        s.chroma_client.collection.get.side_effect = Exception("db locked")
        assert s._count_mineru_indexed_items() == 0
