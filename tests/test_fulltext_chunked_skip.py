"""Regression tests for issue: `update-db --fulltext` with chunking ON
re-embeds every item on every run instead of skipping already-indexed items.

Root cause
-----------
``_get_items_from_local_db`` checks whether an item is already indexed by
calling ``chroma_client.get_document_metadata(it.key)`` — using the *bare*
item key. When chunking is enabled, however, the IDs actually written to
ChromaDB are ``<key>#0``, ``<key>#1``, …; the bare ``<key>`` is never stored
(see ``_process_item_batch`` lines 2199-2211). So the existing-item lookup
always returns ``None``, ``should_extract`` stays ``True``, and every item
is re-extracted and re-embedded on every run — a full rebuild in disguise,
burning embedding-API tokens that the user thought were incremental.

The non-chunking path stores IDs as bare ``<key>``, so the lookup works
there and the bug only manifests when ``semantic_search.chunking.enabled``
is true (which is the user's default).
"""

import sqlite3
import sys

import pytest

if sys.version_info >= (3, 14):
    pytest.skip(
        "chromadb currently relies on pydantic v1 paths that are incompatible with Python 3.14+",
        allow_module_level=True,
    )

from zotero_mcp import semantic_search
from zotero_mcp.semantic_search import ZoteroSemanticSearch


def make_zotero_db(path, keys):
    """Create a minimal zotero.sqlite with the given item keys.

    Includes the empty side tables referenced by get_items_with_text and
    _iter_parent_attachments so fulltext-extraction runs cleanly (no PDFs
    on disk means extract_fulltext_for_item returns empty, and the
    skip-check proceeds without crashing on missing tables).
    """
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE itemTypes (itemTypeID INTEGER PRIMARY KEY, typeName TEXT)")
    conn.execute("INSERT INTO itemTypes VALUES (1, 'journalArticle')")
    conn.execute(
        """CREATE TABLE items (
            itemID INTEGER PRIMARY KEY, itemTypeID INT, dateAdded TEXT,
            dateModified TEXT, clientDateModified TEXT, libraryID INT,
            key TEXT UNIQUE, version INT, synced INT
        )"""
    )
    for i, key in enumerate(keys, start=1):
        conn.execute(
            "INSERT INTO items VALUES (?, 1, '2026-01-01 00:00:00', "
            "'2026-01-01 00:00:00', '2026-01-01 00:00:00', 1, ?, 1, 0)",
            (i, key),
        )
    # Empty side tables referenced by get_items_with_text / _iter_parent_attachments.
    conn.execute("CREATE TABLE deletedItems (itemID INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE itemData (itemID INT, fieldID INT, valueID INT)")
    conn.execute("CREATE TABLE itemDataValues (valueID INTEGER PRIMARY KEY, value TEXT)")
    conn.execute("CREATE TABLE fields (fieldID INTEGER PRIMARY KEY, fieldName TEXT)")
    conn.execute("CREATE TABLE itemNotes (itemID INT, parentItemID INT, note TEXT)")
    conn.execute("CREATE TABLE itemCreators (itemID INT, creatorID INT)")
    conn.execute("CREATE TABLE creators (creatorID INTEGER PRIMARY KEY, firstName TEXT, lastName TEXT)")
    # itemAttachments is referenced by _iter_parent_attachments; no rows
    # here so get_fulltext_meta_for_item returns [] (no local fulltext).
    conn.execute(
        """CREATE TABLE itemAttachments (
            itemID INTEGER PRIMARY KEY, parentItemID INT, path TEXT,
            contentType TEXT, charsetID INT
        )"""
    )
    conn.commit()
    conn.close()
    # _get_storage_dir() infers the storage dir as db_path.parent/"storage";
    # create it so _resolve_attachment_path doesn't trip on a missing dir.
    (path.parent / "storage").mkdir(exist_ok=True)


class RecordingChroma:
    """ChromaClient stand-in that records every get_document_metadata
    lookup so the test can assert which id was probed.

    Preloads a metadata dict for a given id (chunk-0 form or bare key).
    Returns None for any id not in ``self._meta``.
    """

    def __init__(self, meta_by_id=None, chunking=False):
        self.embedding_max_tokens = 8000
        self._meta = dict(meta_by_id or {})
        self._chunking = chunking
        self.metadata_lookups: list[str] = []  # ids probed
        self.upserts: list[list[str]] = []  # ids upserted per batch

    def truncate_text(self, text, max_tokens=None):
        return text

    def get_document_metadata(self, doc_id):
        self.metadata_lookups.append(doc_id)
        return self._meta.get(doc_id)

    def get_existing_ids(self, ids):
        return {i for i in ids if i in self._meta}

    def upsert_documents(self, documents, metadatas, ids):
        self.upserts.append(list(ids))

    def reset_collection(self):
        self._meta.clear()


def _make_search(db_path, chroma, chunking_enabled):
    """Build a ZoteroSemanticSearch without touching Chroma or pyzotero.

    Force chunking config through a subclass override so no config file is
    needed. The caller is responsible for monkeypatching
    ``semantic_search.is_local_mode`` to True when running the fulltext path
    (the import-time binding means the patch must target the
    ``zotero_mcp.semantic_search`` module namespace).
    """
    s = object.__new__(_ChunkingSearch)
    s.zotero_client = None
    s.db_path = str(db_path)
    s.config_path = None
    s.chroma_client = chroma
    s.update_config = {"auto_update": False, "update_frequency": "manual"}
    s._force_chunking = chunking_enabled
    return s


class _ChunkingSearch(ZoteroSemanticSearch):
    """Subclass that lets tests force the chunking config without a file."""

    @property
    def _chunking_config(self):  # type: ignore[override]
        return {
            "enabled": getattr(self, "_force_chunking", False),
            "chunk_size": 1500,
            "overlap": 200,
            "max_chunks_per_item": 20,
            "max_chunks_override": {},
            "max_pages_override": {},
        }


# ---------------------------------------------------------------------------
# The bug: chunked lookup must probe {key}#0, not the bare key
# ---------------------------------------------------------------------------


class TestChunkedFulltextSkipLookup:
    @pytest.fixture(autouse=True)
    def _force_local_mode(self, monkeypatch):
        """_get_items_from_source guards on is_local_mode(); tests here
        exercise the local-DB path so it must appear enabled."""
        monkeypatch.setattr(semantic_search, "is_local_mode", lambda: True)

    def test_chunked_run_probes_chunk0_id_not_bare_key(self, tmp_path):
        """With chunking ON and the item already indexed (chunk-0 metadata
        present), ``_get_items_from_local_db`` must probe ``KEY#0`` and skip
        re-extraction. Pre-fix it probed the bare ``KEY`` (always None) and
        re-extracted every time."""
        db = tmp_path / "zotero.sqlite"
        make_zotero_db(db, ["ABCD1234"])
        # Pre-populate ChromaDB as if the item was already indexed with fulltext.
        chunk0_meta = {
            "item_key": "ABCD1234",
            "group_id": 0,
            "has_fulltext": True,
            "date_modified": "2026-01-01 00:00:00",
        }
        chroma = RecordingChroma(
            meta_by_id={"ABCD1234#0": chunk0_meta},
            chunking=True,
        )
        s = _make_search(db, chroma, chunking_enabled=True)

        items = s._get_items_from_local_db(
            extract_fulltext=True,
            chroma_client=chroma,
            force_rebuild=False,
        )

        # The lookup must have probed the chunk-0 id, not the bare key.
        assert "ABCD1234#0" in chroma.metadata_lookups
        # And because chunk-0 metadata is present + has_fulltext, the item
        # must be skipped (no upsert, no re-extraction).
        assert items == []

    def test_chunked_run_re_extracts_when_chunk0_missing(self, tmp_path):
        """When chunk-0 metadata is absent (item not yet indexed), the item
        must fall through to extraction + upsert — even under chunking."""
        db = tmp_path / "zotero.sqlite"
        make_zotero_db(db, ["NEWHASH01"])
        chroma = RecordingChroma(meta_by_id={}, chunking=True)
        s = _make_search(db, chroma, chunking_enabled=True)

        # No fulltext on disk and no PDF → extract_fulltext_for_item returns
        # empty, but the item still goes through (metadata-only). The key
        # assertion: the chunk-0 id is the one probed.
        items = s._get_items_from_local_db(
            extract_fulltext=True,
            chroma_client=chroma,
            force_rebuild=False,
        )
        assert "NEWHASH01#0" in chroma.metadata_lookups
        # Item is returned for indexing (metadata-only since no PDF text).
        assert len(items) == 1
        assert items[0]["key"] == "NEWHASH01"

    def test_non_chunked_run_probes_bare_key(self, tmp_path):
        """With chunking OFF, IDs are bare keys — the lookup must probe the
        bare key (regression guard so the fix doesn't break the non-chunking
        path)."""
        db = tmp_path / "zotero.sqlite"
        make_zotero_db(db, ["BBBB1111"])
        meta = {"item_key": "BBBB1111", "group_id": 0, "has_fulltext": True,
                "date_modified": "2026-01-01 00:00:00"}
        chroma = RecordingChroma(meta_by_id={"BBBB1111": meta}, chunking=False)
        s = _make_search(db, chroma, chunking_enabled=False)

        items = s._get_items_from_local_db(
            extract_fulltext=True,
            chroma_client=chroma,
            force_rebuild=False,
        )
        # Bare key probed (non-chunking stores IDs as bare keys).
        assert "BBBB1111" in chroma.metadata_lookups
        assert "BBBB1111#0" not in chroma.metadata_lookups
        assert items == []  # already indexed → skipped

    def test_chunked_run_skips_failed_extraction_unmodified(self, tmp_path):
        """A previously-failed extraction (has_fulltext='failed') whose item
        has not been modified must be skipped — the same optimization as the
        non-chunking path. Pre-fix this branch was unreachable under chunking
        because the bare-key lookup returned None."""
        db = tmp_path / "zotero.sqlite"
        make_zotero_db(db, ["FAILKEY01"])
        chunk0_meta = {
            "item_key": "FAILKEY01",
            "group_id": 0,
            "has_fulltext": "failed",
            "date_modified": "2026-01-01 00:00:00",  # matches the sqlite row
            "attachment_keys": "",  # matches the fixture's (empty) attachment set
        }
        chroma = RecordingChroma(meta_by_id={"FAILKEY01#0": chunk0_meta})
        s = _make_search(db, chroma, chunking_enabled=True)

        items = s._get_items_from_local_db(
            extract_fulltext=True,
            chroma_client=chroma,
            force_rebuild=False,
        )
        assert "FAILKEY01#0" in chroma.metadata_lookups
        assert items == []  # unmodified failure → skipped

    def test_chunked_run_retries_failed_when_item_modified(self, tmp_path):
        """A previously-failed extraction whose item WAS modified since the
        failure must be retried (updated_existing path). Pre-fix unreachable."""
        db = tmp_path / "zotero.sqlite"
        make_zotero_db(db, ["MODKEY001"])
        chunk0_meta = {
            "item_key": "MODKEY001",
            "group_id": 0,
            "has_fulltext": "failed",
            "date_modified": "2025-12-31 00:00:00",  # OLDER than sqlite row
        }
        chroma = RecordingChroma(meta_by_id={"MODKEY001#0": chunk0_meta})
        s = _make_search(db, chroma, chunking_enabled=True)

        items = s._get_items_from_local_db(
            extract_fulltext=True,
            chroma_client=chroma,
            force_rebuild=False,
        )
        assert "MODKEY001#0" in chroma.metadata_lookups
        # Modified since failure → falls through to extraction (returned).
        assert len(items) == 1


# ---------------------------------------------------------------------------
# End-to-end: update_database must not upsert already-indexed chunked items
# ---------------------------------------------------------------------------


class _StubZotero:
    def last_modified_version(self):
        return 0


class TestUpdateDatabaseChunkedIncrementalSkip:
    def test_second_fulltext_run_skips_already_indexed_items(self, tmp_path, monkeypatch):
        """Two consecutive ``update-db --fulltext`` runs with chunking ON must
        NOT re-embed items the second time. Pre-fix the second run re-embedded
        everything (a hidden full rebuild burning embedding tokens)."""
        monkeypatch.setattr(semantic_search, "is_local_mode", lambda: True)
        db = tmp_path / "zotero.sqlite"
        make_zotero_db(db, ["AAAA1111", "BBBB2222"])

        # After the first run, both items have chunk-0 metadata with has_fulltext.
        meta = {
            "AAAA1111#0": {"item_key": "AAAA1111", "group_id": 0, "has_fulltext": True,
                            "date_modified": "2026-01-01 00:00:00"},
            "BBBB2222#0": {"item_key": "BBBB2222", "group_id": 0, "has_fulltext": True,
                            "date_modified": "2026-01-01 00:00:00"},
        }
        chroma = RecordingChroma(meta_by_id=meta)
        s = _make_search(db, chroma, chunking_enabled=True)
        s.zotero_client = _StubZotero()

        # Patch the watermark so update_database goes through the full-scan
        # path (extract_fulltext forces the non-incremental branch by design).
        monkeypatch.setattr(s, "_load_last_sync_version", lambda: 0)

        stats = s.update_database(extract_fulltext=True, include_fulltext=False)

        # Both items are already indexed → no upserts at all.
        assert stats["total_items"] == 0
        assert chroma.upserts == []
        # And the chunk-0 ids were probed for both.
        assert "AAAA1111#0" in chroma.metadata_lookups
        assert "BBBB2222#0" in chroma.metadata_lookups
