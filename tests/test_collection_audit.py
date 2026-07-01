"""Tests for zotero_audit_collection_membership — library-wide collection
membership audit (unfiled items, cross-filed items, overview)."""

from conftest import DummyContext, FakeZotero

from zotero_mcp import server


class FakeZoteroAudit(FakeZotero):
    """FakeZotero with pagination-aware items() and a collection() lookup.

    The base FakeZotero.items() ignores start/limit and returns everything,
    which breaks _paginate's loop (it never sees an empty/short page to
    stop). We override to slice like the real Zotero API. collection() is
    needed because the audit tool resolves collection names; the base lacks
    it.
    """

    def items(self, start=None, limit=None, **kwargs):
        page = limit or 100
        if start is not None:
            return self._items[start : start + page]
        return self._items[:page]

    def collection(self, key):
        for c in self._collections:
            if c.get("key") == key:
                return c
        raise Exception(f"Code: 404 — Not found ({key})")


def _make_audit_zot(monkeypatch, items=None, collections=None):
    zot = FakeZoteroAudit()
    zot._items = items or []
    zot._collections = collections or []
    monkeypatch.setattr("zotero_mcp.client.get_zotero_client", lambda: zot)
    return zot


def _make_item(key, title, date="2023", collections=None, item_type="journalArticle"):
    return {
        "key": key,
        "version": 1,
        "data": {
            "itemType": item_type,
            "title": title,
            "date": date,
            "creators": [],
            "tags": [],
            "collections": collections or [],
        },
    }


def _make_pdf_item(key, filename, date="2023", collections=None):
    return {
        "key": key,
        "version": 1,
        "data": {
            "itemType": "attachment",
            "filename": filename,
            "contentType": "application/pdf",
            "date": date,
            "parentItem": "",
            "collections": collections or [],
        },
    }


def _make_coll(key, name):
    return {"key": key, "version": 1, "data": {"name": name, "parentCollection": False}}


# ---------------------------------------------------------------------------
# Mixed library — the headline test exercising all three sections at once.
# ---------------------------------------------------------------------------


def test_audit_mixed_library(monkeypatch):
    """One unfiled, one single-collection, one cross-filed item."""
    _make_audit_zot(
        monkeypatch,
        items=[
            _make_item("UNF00001", "Unfiled Paper", "2023", collections=[]),
            _make_item("SIN00001", "Single Paper", "2022", collections=["COLAAAAA"]),
            _make_item("MUL00001", "Multi Paper", "2021", collections=["COLAAAAA", "COLBBBBB"]),
        ],
        collections=[_make_coll("COLAAAAA", "Machine Learning"), _make_coll("COLBBBBB", "NLP")],
    )

    result = server.audit_collection_membership(ctx=DummyContext())

    # Overview
    assert "Total items: 3" in result
    assert "Filed (in ≥1 collection): 2" in result
    assert "Unfiled (in 0 collections): 1" in result
    assert "In multiple collections: 1" in result

    # Unfiled section contains the right item
    assert "`UNF00001`" in result
    assert "Unfiled Paper" in result

    # Multi section contains the right item with resolved names
    assert "`MUL00001`" in result
    assert "Multi Paper" in result
    assert "Machine Learning" in result
    assert "NLP" in result


# ---------------------------------------------------------------------------
# Unfiled section formatting.
# ---------------------------------------------------------------------------


def test_audit_unfiled_count_and_list_format(monkeypatch):
    _make_audit_zot(
        monkeypatch,
        items=[
            _make_item("UNF00001", "Alpha", "2023", collections=[]),
            _make_item("UNF00002", "Beta", "2022", collections=[]),
            _make_item("FIL00001", "Gamma", "2021", collections=["COLAAAAA"]),
        ],
        collections=[_make_coll("COLAAAAA", "ML")],
    )

    result = server.audit_collection_membership(ctx=DummyContext())

    assert "Unfiled (in 0 collections): 2" in result
    assert "`UNF00001` | Alpha (2023)" in result
    assert "`UNF00002` | Beta (2022)" in result
    # The filed item must NOT appear in the unfiled section.
    assert "Gamma" not in result


def test_audit_unfiled_standalone_pdf_flag(monkeypatch):
    """A standalone PDF attachment with no collections shows [PDF]."""
    _make_audit_zot(
        monkeypatch,
        items=[
            _make_pdf_item("PDF00001", "loose.pdf", "2020", collections=[]),
        ],
    )

    result = server.audit_collection_membership(ctx=DummyContext())

    assert "Unfiled (in 0 collections): 1" in result
    assert "`PDF00001`" in result
    assert "[PDF]" in result


# ---------------------------------------------------------------------------
# Multiple-collections section sorting and name resolution.
# ---------------------------------------------------------------------------


def test_audit_multi_sorted_by_count_desc(monkeypatch):
    """Item in 3 collections ranks above item in 2."""
    _make_audit_zot(
        monkeypatch,
        items=[
            _make_item("MUL00002", "Two Cols", "2022", collections=["C0000001", "C0000002"]),
            _make_item("MUL00003", "Three Cols", "2021", collections=["C0000001", "C0000002", "C0000003"]),
        ],
        collections=[
            _make_coll("C0000001", "One"),
            _make_coll("C0000002", "Two"),
            _make_coll("C0000003", "Three"),
        ],
    )

    result = server.audit_collection_membership(ctx=DummyContext())

    # The three-collection item must appear before the two-collection item.
    idx_three = result.index("Three Cols")
    idx_two = result.index("Two Cols")
    assert idx_three < idx_two

    # Membership counts and resolved names in the line.
    assert "in 3: One, Two, Three" in result
    assert "in 2: One, Two" in result


def test_audit_multi_shows_names(monkeypatch):
    """Collection keys are resolved to human-readable names."""
    _make_audit_zot(
        monkeypatch,
        items=[
            _make_item("MUL00001", "Paper", "2023", collections=["COLAAAAA", "COLBBBBB"]),
        ],
        collections=[_make_coll("COLAAAAA", "Astrophysics"), _make_coll("COLBBBBB", "Cosmology")],
    )

    result = server.audit_collection_membership(ctx=DummyContext())

    assert "Astrophysics" in result
    assert "Cosmology" in result


def test_audit_multi_dangling_key_shown_when_name_unknown(monkeypatch):
    """If a collection key has no matching name (deleted/dangling), the raw
    key is shown rather than dropping the entry."""
    _make_audit_zot(
        monkeypatch,
        items=[
            _make_item("MUL00001", "Paper", "2023", collections=["GONE0001", "COLAAAAA"]),
        ],
        collections=[_make_coll("COLAAAAA", "Known")],  # GONE0001 not in list
    )

    result = server.audit_collection_membership(ctx=DummyContext())

    assert "GONE0001" in result
    assert "Known" in result


def test_audit_collection_name_lookup_failure_degrades_to_keys(monkeypatch):
    """If zot.collections() raises, the audit still works — multi section
    shows raw keys instead of names."""

    class _BrokenCollections(FakeZoteroAudit):
        def collections(self, **kwargs):
            raise Exception("connection refused")

    zot = _BrokenCollections()
    zot._items = [_make_item("MUL00001", "Paper", "2023", collections=["COLAAAAA", "COLBBBBB"])]
    monkeypatch.setattr("zotero_mcp.client.get_zotero_client", lambda: zot)

    result = server.audit_collection_membership(ctx=DummyContext())

    # Should not error; should still show the item with raw keys.
    assert "Error" not in result
    assert "`MUL00001`" in result
    assert "COLAAAAA" in result
    assert "COLBBBBB" in result


# ---------------------------------------------------------------------------
# Edge cases: empty library, all filed, no multi.
# ---------------------------------------------------------------------------


def test_audit_empty_library(monkeypatch):
    _make_audit_zot(monkeypatch, items=[])

    result = server.audit_collection_membership(ctx=DummyContext())

    assert result == "No items found in the library."


def test_audit_all_filed(monkeypatch):
    """When unfiled=0, the section says so explicitly."""
    _make_audit_zot(
        monkeypatch,
        items=[
            _make_item("FIL00001", "A", "2023", collections=["COLAAAAA"]),
            _make_item("FIL00002", "B", "2022", collections=["COLAAAAA"]),
        ],
        collections=[_make_coll("COLAAAAA", "ML")],
    )

    result = server.audit_collection_membership(ctx=DummyContext())

    assert "Unfiled (in 0 collections): 0" in result
    assert "All items are filed in at least one collection." in result


def test_audit_no_multi(monkeypatch):
    """When multi=0, the section says so explicitly."""
    _make_audit_zot(
        monkeypatch,
        items=[
            _make_item("FIL00001", "A", "2023", collections=["COLAAAAA"]),
            _make_item("UNF00001", "B", "2022", collections=[]),
        ],
        collections=[_make_coll("COLAAAAA", "ML")],
    )

    result = server.audit_collection_membership(ctx=DummyContext())

    assert "In multiple collections: 0" in result
    assert "No items appear in multiple collections." in result


# ---------------------------------------------------------------------------
# Limit / truncation.
# ---------------------------------------------------------------------------


def test_audit_limit_truncates_both_sections(monkeypatch):
    """limit applies independently to unfiled and multi sections."""
    items = []
    # 3 unfiled
    for i in range(3):
        items.append(_make_item(f"U{i:05d}", f"Unfiled {i}", "2023", collections=[]))
    # 3 multi (each in 2 collections)
    for i in range(3):
        items.append(_make_item(f"M{i:05d}", f"Multi {i}", "2023", collections=["C0000001", "C0000002"]))
    _make_audit_zot(
        monkeypatch,
        items=items,
        collections=[_make_coll("C0000001", "One"), _make_coll("C0000002", "Two")],
    )

    result = server.audit_collection_membership(limit=2, ctx=DummyContext())

    # Both sections show truncation footer.
    assert "Showing 2 of 3" in result
    # The footer line appears twice (once per section).
    assert result.count("Showing 2 of 3") == 2
