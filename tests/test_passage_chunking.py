"""Tests for passage-level chunked retrieval (Tier-1 grounded search).

Covers the pure splitter, page mapping, the best-snippet extractor, the
chunk-aware indexing path in `_process_item_batch`, and chunk grouping in
`_enrich_search_results`. Chunking is opt-in; these tests force it on via a
config file or by setting `_chunking_config` directly.
"""

import json
import sys

import pytest

if sys.version_info >= (3, 14):
    pytest.skip(
        "chromadb relies on pydantic v1 paths incompatible with Python 3.14+",
        allow_module_level=True,
    )

from zotero_mcp import semantic_search
from zotero_mcp.semantic_search import (
    _chunks_for_pages,
    _page_for_offset,
    best_snippet,
    split_into_passages,
)

# ---------------------------------------------------------------------------
# Pure splitter
# ---------------------------------------------------------------------------


def test_split_empty_returns_nothing():
    assert split_into_passages("", 100, 20, 10) == []
    assert split_into_passages("   ", 100, 20, 10) == []


def test_split_short_text_single_passage():
    out = split_into_passages("a short body", chunk_size=100, overlap=20, max_chunks=10)
    assert len(out) == 1
    text, start, end = out[0]
    assert text == "a short body"
    assert start == 0


def test_split_offsets_are_monotonic_and_cover_text():
    body = ("Sentence one. " * 200).strip()
    out = split_into_passages(body, chunk_size=200, overlap=40, max_chunks=50)
    assert len(out) > 1
    # Offsets strictly advance and each passage is non-empty.
    prev_start = -1
    for text, start, end in out:
        assert text.strip()
        assert start > prev_start
        assert end > start
        prev_start = start


def test_split_respects_max_chunks():
    body = "word " * 5000
    out = split_into_passages(body, chunk_size=100, overlap=10, max_chunks=7)
    assert len(out) == 7


def test_split_overlap_larger_than_chunk_is_tolerated():
    body = "x" * 1000
    out = split_into_passages(body, chunk_size=100, overlap=500, max_chunks=20)
    # Must still terminate and make progress.
    assert out
    assert all(end > start for _, start, end in out)


# ---------------------------------------------------------------------------
# Page mapping
# ---------------------------------------------------------------------------


def test_page_for_offset_without_separators_is_none():
    assert _page_for_offset("no form feeds here", 5) is None


def test_page_for_offset_counts_form_feeds():
    text = "page one\fpage two\fpage three"
    assert _page_for_offset(text, 0) == 1
    assert _page_for_offset(text, text.index("page two")) == 2
    assert _page_for_offset(text, text.index("page three")) == 3


# ---------------------------------------------------------------------------
# _chunks_for_pages — page-to-chunk estimate
# ---------------------------------------------------------------------------


def test_chunks_for_pages_zero_or_negative_returns_one():
    assert _chunks_for_pages(0) == 1
    assert _chunks_for_pages(-5) == 1


def test_chunks_for_pages_proportional():
    # stride = 1500 - 200 = 1300; chars = pages * 2500
    # 100 pages → 100 * 2500 / 1300 ≈ 192.3 → ceil = 193
    assert _chunks_for_pages(100) == 193
    # 10 pages → 10 * 2500 / 1300 ≈ 19.23 → ceil = 20
    assert _chunks_for_pages(10) == 20


def test_chunks_for_pages_custom_stride():
    # chunk_size=1000, overlap=100 → stride=900
    # 5 pages → 5 * 2500 / 900 ≈ 13.9 → ceil = 14
    assert _chunks_for_pages(5, chunk_size=1000, overlap=100) == 14


def test_chunks_for_pages_large_overlap_clamped():
    # overlap >= chunk_size would give stride <= 0; clamped to 1
    # 2 pages → 2 * 2500 / 1 = 5000
    assert _chunks_for_pages(2, chunk_size=100, overlap=200) == 5000


# ---------------------------------------------------------------------------
# Best-snippet extractor
# ---------------------------------------------------------------------------


def test_best_snippet_short_text_returned_whole():
    snippet, off = best_snippet("anything", "tiny doc")
    assert snippet == "tiny doc"
    assert off == 0


def test_best_snippet_centers_on_query_terms():
    head = "irrelevant filler. " * 40
    target = "mindfulness based cognitive therapy reduces relapse"
    text = head + target + " more filler here." * 40
    snippet, off = best_snippet("mindfulness cognitive therapy", text, width=120)
    assert "mindfulness" in snippet.lower()
    assert off > 0  # not the head of the document


def test_best_snippet_no_terms_falls_back_to_head():
    text = "alpha beta gamma delta " * 50
    snippet, off = best_snippet("zzz", text, width=80)
    assert off == 0
    assert snippet.startswith("alpha")


# ---------------------------------------------------------------------------
# Chunk-aware indexing
# ---------------------------------------------------------------------------


class ChunkingFakeChroma:
    """Chroma stub that records upserts and supports chunk bookkeeping."""

    def __init__(self, existing=None):
        self.upserted_ids = []
        self.upserted_docs = []
        self.upserted_metas = []
        self.deleted_parents = []
        self.embedding_max_tokens = 8000
        self._existing = set(existing or [])

    def get_existing_ids(self, ids):
        return self._existing & set(ids)

    def delete_item_chunks(self, item_key):
        self.deleted_parents.append(item_key)

    def upsert_documents(self, documents, metadatas, ids):
        self.upserted_docs.extend(documents)
        self.upserted_metas.extend(metadatas)
        self.upserted_ids.extend(ids)

    def truncate_text(self, text, max_tokens=None):
        return text


def _chunking_search(monkeypatch, config=None, existing=None):
    monkeypatch.setattr(semantic_search, "get_zotero_client", lambda: object())
    s = semantic_search.ZoteroSemanticSearch(chroma_client=ChunkingFakeChroma(existing))
    s._chunking_config = {
        "enabled": True,
        "chunk_size": 120,
        "overlap": 20,
        "max_chunks_per_item": 10,
    }
    if config:
        s._chunking_config.update(config)
    return s


def _long_item(key="ITEM0001"):
    body = "Mindfulness reduces relapse. " * 60
    return {
        "key": key,
        "data": {
            "title": "Mindfulness Paper",
            "itemType": "journalArticle",
            "abstractNote": "An abstract.",
            "creators": [],
            "fulltext": body,
        },
    }


def test_chunking_emits_multiple_passage_ids(monkeypatch):
    s = _chunking_search(monkeypatch)
    stats = s._process_item_batch([_long_item("ITEM0001")], force_rebuild=True)

    # One *item* processed, but many chunk ids upserted.
    assert stats["processed"] == 1
    assert len(s.chroma_client.upserted_ids) > 1
    assert all(cid.startswith("ITEM0001#") for cid in s.chroma_client.upserted_ids)
    # Each chunk carries passage provenance in its metadata.
    meta0 = s.chroma_client.upserted_metas[0]
    assert meta0["parent_item_key"] == "ITEM0001"
    assert meta0["chunk_index"] == 0
    assert meta0["n_chunks"] == len(s.chroma_client.upserted_ids)
    assert "char_start" in meta0 and "char_end" in meta0


def test_chunking_added_vs_updated_is_item_granular(monkeypatch):
    # Pretend the item already exists (its chunk #0 is present).
    s = _chunking_search(monkeypatch, existing={"ITEM0001#0"})
    stats = s._process_item_batch([_long_item("ITEM0001")], force_rebuild=False)
    assert stats["updated"] == 1
    assert stats["added"] == 0
    # Stale chunks for the re-indexed item were cleared first.
    assert "ITEM0001" in s.chroma_client.deleted_parents


def test_default_path_still_item_level(monkeypatch):
    # No chunking config -> ids are bare item keys (regression guard).
    monkeypatch.setattr(semantic_search, "get_zotero_client", lambda: object())
    s = semantic_search.ZoteroSemanticSearch(chroma_client=ChunkingFakeChroma())
    stats = s._process_item_batch([_long_item("ITEM0001")], force_rebuild=True)
    assert stats["processed"] == 1
    assert s.chroma_client.upserted_ids == ["ITEM0001"]


def test_per_item_max_chunks_override_exceeds_global_cap(monkeypatch):
    """A per-item-key override lets one item produce more chunks than the
    global max_chunks_per_item, without affecting other items."""
    # Default cap is 10 (from _chunking_search). Override BOOK0001 to 50.
    s = _chunking_search(
        monkeypatch,
        config={"max_chunks_override": {"BOOK0001": 50}},
    )
    # Long enough body to exceed 10 chunks at chunk_size=120 (≈30+ passages).
    body = "Chapter content paragraph. " * 400
    book_item = {
        "key": "BOOK0001",
        "data": {
            "title": "Thick Book",
            "itemType": "book",
            "abstractNote": "An abstract.",
            "creators": [],
            "fulltext": body,
        },
    }
    stats = s._process_item_batch([book_item], force_rebuild=True)
    assert stats["processed"] == 1
    # Override 50 > global 10, so we should get more than 10 chunks.
    n = len(s.chroma_client.upserted_ids)
    assert n > 10, f"override should allow >10 chunks, got {n}"
    assert n <= 50, f"override should cap at 50, got {n}"
    assert all(cid.startswith("BOOK0001#") for cid in s.chroma_client.upserted_ids)


def test_per_item_override_is_case_insensitive(monkeypatch):
    """Config keys can be any case; lookup upper-cases the item key."""
    s = _chunking_search(
        monkeypatch,
        config={"max_chunks_override": {"book0001": 50}},
    )
    body = "Chapter content paragraph. " * 400
    book_item = {
        "key": "BOOK0001",  # upper case in the item
        "data": {"itemType": "book", "title": "T", "fulltext": body, "creators": []},
    }
    s._process_item_batch([book_item], force_rebuild=True)
    # Still gets >10 (override matched despite case difference).
    assert len(s.chroma_client.upserted_ids) > 10


def test_no_override_falls_back_to_global_cap(monkeypatch):
    """Without an override for this key, the global max_chunks_per_item applies."""
    s = _chunking_search(monkeypatch)  # no max_chunks_override set
    body = "Chapter content paragraph. " * 400  # long enough to hit cap
    book_item = {
        "key": "BOOK9999",
        "data": {
            "title": "Another Book",
            "itemType": "book",
            "abstractNote": "An abstract.",
            "creators": [],
            "fulltext": body,
        },
    }
    stats = s._process_item_batch([book_item], force_rebuild=True)
    assert stats["processed"] == 1
    # Global cap is 10 — should be capped even though the body is longer.
    assert len(s.chroma_client.upserted_ids) == 10


def test_override_does_not_affect_other_items(monkeypatch):
    """Override for one key must not bleed into another item in the same batch."""
    s = _chunking_search(
        monkeypatch,
        config={"max_chunks_override": {"BOOK0001": 50}},
    )
    body = "Chapter content paragraph. " * 400
    items = [
        {
            "key": "BOOK0001",
            "data": {"itemType": "book", "title": "A", "fulltext": body, "creators": []},
        },
        {
            "key": "BOOK0002",  # no override — should hit global cap of 10
            "data": {"itemType": "book", "title": "B", "fulltext": body, "creators": []},
        },
    ]
    s._process_item_batch(items, force_rebuild=True)
    book1 = [i for i in s.chroma_client.upserted_ids if i.startswith("BOOK0001#")]
    book2 = [i for i in s.chroma_client.upserted_ids if i.startswith("BOOK0002#")]
    assert len(book1) > 10  # overridden
    assert len(book2) == 10  # global cap


def test_max_pages_override_converts_to_chunks(monkeypatch):
    """max_pages_override lets the user specify pages instead of chunks.
    The code converts via _chunks_for_pages; with chunk_size=120/overlap=20
    (stride=100), 50 pages → 50 * 2500 / 100 = 1250 chunks."""
    s = _chunking_search(
        monkeypatch,
        config={"max_pages_override": {"BOOK0001": 50}},
    )
    # Provide enough text that the cap (not the text) is the limiting factor.
    # 1250 chunks × 120 chars ≈ 150k chars needed; we give 200k to be safe.
    body = "Word token here. " * 12000  # ~216k chars
    book_item = {
        "key": "BOOK0001",
        "data": {
            "title": "Thick Book",
            "itemType": "book",
            "abstractNote": "An abstract.",
            "creators": [],
            "fulltext": body,
        },
    }
    s._process_item_batch([book_item], force_rebuild=True)
    n = len(s.chroma_client.upserted_ids)
    # Should exceed the global cap of 10 — pages override worked.
    assert n > 10, f"pages override should allow >10 chunks, got {n}"


def test_max_chunks_override_takes_precedence_over_pages(monkeypatch):
    """If both max_chunks_override and max_pages_override are set for the same
    key, the explicit chunk count wins (more precise)."""
    s = _chunking_search(
        monkeypatch,
        config={
            "max_chunks_override": {"BOOK0001": 30},
            "max_pages_override": {"BOOK0001": 500},  # would give a huge number
        },
    )
    body = "Word token here. " * 12000
    book_item = {
        "key": "BOOK0001",
        "data": {"itemType": "book", "title": "T", "fulltext": body, "creators": []},
    }
    s._process_item_batch([book_item], force_rebuild=True)
    # Should be capped at 30 (explicit chunks), not the pages-derived number.
    assert len(s.chroma_client.upserted_ids) == 30


def test_chunking_config_loaded_from_file(monkeypatch, tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"semantic_search": {"chunking": {"enabled": True, "chunk_size": 256}}}))
    monkeypatch.setattr(semantic_search, "get_zotero_client", lambda: object())
    monkeypatch.setattr(semantic_search, "create_chroma_client", lambda _p: ChunkingFakeChroma())
    s = semantic_search.ZoteroSemanticSearch(config_path=str(cfg))
    assert s._chunking_enabled is True
    assert s._chunking_config["chunk_size"] == 256


# ---------------------------------------------------------------------------
# Chunk grouping in enrichment
# ---------------------------------------------------------------------------


class _ZotItemStub:
    def item(self, key):
        return {"key": key, "data": {"title": f"Title {key}"}}


def test_enrich_groups_chunks_back_to_items(monkeypatch):
    s = _chunking_search(monkeypatch)
    s.zotero_client = _ZotItemStub()

    chroma_results = {
        "ids": [["PAP1#3", "PAP1#0", "PAP2#1"]],
        "distances": [[0.10, 0.20, 0.30]],
        "documents": [
            [
                "mindfulness relapse passage from paper one chunk three",
                "intro passage from paper one chunk zero",
                "unrelated passage from paper two",
            ]
        ],
        "metadatas": [
            [
                {
                    "parent_item_key": "PAP1",
                    "chunk_index": 3,
                    "n_chunks": 5,
                    "char_start": 900,
                    "char_end": 1020,
                    "page": 4,
                },
                {"parent_item_key": "PAP1", "chunk_index": 0, "n_chunks": 5, "char_start": 0, "char_end": 120},
                {"parent_item_key": "PAP2", "chunk_index": 1, "n_chunks": 2, "char_start": 100, "char_end": 220},
            ]
        ],
    }

    enriched = s._enrich_search_results(chroma_results, "mindfulness relapse", limit=10)

    # Two distinct items, PAP1 represented by its best (first) passage.
    assert [r["item_key"] for r in enriched] == ["PAP1", "PAP2"]
    best = enriched[0]
    assert best["chunk_index"] == 3
    assert best["page"] == 4
    assert best["char_start"] == 900
    assert best["matched_passage"]
    assert best["zotero_item"]["data"]["title"] == "Title PAP1"


def test_enrich_caps_at_limit(monkeypatch):
    s = _chunking_search(monkeypatch)
    s.zotero_client = _ZotItemStub()
    chroma_results = {
        "ids": [["A#0", "B#0", "C#0", "D#0"]],
        "distances": [[0.1, 0.2, 0.3, 0.4]],
        "documents": [["a", "b", "c", "d"]],
        "metadatas": [[{"parent_item_key": x} for x in ("A", "B", "C", "D")]],
    }
    enriched = s._enrich_search_results(chroma_results, "q", limit=2)
    assert [r["item_key"] for r in enriched] == ["A", "B"]
