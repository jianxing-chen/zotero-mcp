"""Tests for the `collection` field support in zotero_advanced_search.

The advanced_search tool description lists `collection` as a searchable
field, but _extract_values previously had no branch for it — the actual
Zotero field is `collections` (plural, a list of keys), so the generic
fallback read the non-existent `data["collection"]` and silently matched
nothing. These tests verify the fix.
"""

from conftest import DummyContext

from zotero_mcp import server


class _FakeZotero:
    """Minimal stub: paginated items() returning a fixed list."""

    def __init__(self, items):
        self._items = items

    def items(self, start=0, limit=100, **_kwargs):
        return self._items[start : start + limit]


def _make_item(key, title, collections):
    return {
        "key": key,
        "data": {
            "itemType": "journalArticle",
            "title": title,
            "date": "2023",
            "creators": [],
            "tags": [],
            "collections": collections,
        },
    }


_ITEMS = [
    _make_item("AAA11111", "In Collection A", ["COLAAAAA"]),
    _make_item("BBB22222", "In Collections A and B", ["COLAAAAA", "COLBBBBB"]),
    _make_item("CCC33333", "Unfiled Paper", []),
    _make_item("DDD44444", "In Collection C", ["COLCCCCC"]),
]


def _patch(monkeypatch, items=None):
    monkeypatch.setattr(
        "zotero_mcp.client.get_zotero_client",
        lambda: _FakeZotero(items if items is not None else _ITEMS),
    )


# ---------------------------------------------------------------------------
# is — match items belonging to a given collection.
# ---------------------------------------------------------------------------


def test_collection_field_is_matches_items_in_that_collection(monkeypatch):
    _patch(monkeypatch)

    result = server.advanced_search(
        conditions=[{"field": "collection", "operation": "is", "value": "COLAAAAA"}],
        ctx=DummyContext(),
    )

    assert "In Collection A" in result
    assert "In Collections A and B" in result
    assert "Unfiled Paper" not in result
    assert "In Collection C" not in result


def test_collections_plural_is_alias(monkeypatch):
    """The plural form `collections` should work identically."""
    _patch(monkeypatch)

    result = server.advanced_search(
        conditions=[{"field": "collections", "operation": "is", "value": "COLBBBBB"}],
        ctx=DummyContext(),
    )

    assert "In Collections A and B" in result
    assert "In Collection A" not in result


# ---------------------------------------------------------------------------
# isNot — match items NOT belonging to a given collection.
#
# Note: _matches_condition returns all(comparisons) for isNot. For an item
# whose collections list is non-empty but doesn't contain the target, each
# value comparison is True → the item matches. For an unfiled item (empty
# collections), _extract_values returns [] → _matches_condition returns
# False (existing behavior, unchanged by this fix).
# ---------------------------------------------------------------------------


def test_collection_field_isNot_excludes_items_in_that_collection(monkeypatch):
    _patch(monkeypatch)

    result = server.advanced_search(
        conditions=[{"field": "collection", "operation": "isNot", "value": "COLAAAAA"}],
        ctx=DummyContext(),
    )

    # Items NOT in COLAAAAA should appear.
    assert "In Collection C" in result
    # Items IN COLAAAAA should be excluded.
    assert "In Collection A" not in result
    assert "In Collections A and B" not in result


# ---------------------------------------------------------------------------
# doesNotContain — same semantics as isNot for list-valued fields.
# ---------------------------------------------------------------------------


def test_collection_field_doesNotContain(monkeypatch):
    _patch(monkeypatch)

    result = server.advanced_search(
        conditions=[
            {"field": "collection", "operation": "doesNotContain", "value": "COLCCCCC"}
        ],
        ctx=DummyContext(),
    )

    # Everything except the item in COLCCCCC.
    assert "In Collection A" in result
    assert "In Collections A and B" in result
    assert "In Collection C" not in result


# ---------------------------------------------------------------------------
# Regression: before the fix, `collection` matched nothing at all.
# ---------------------------------------------------------------------------


def test_collection_field_regression_previously_silent_no_match(monkeypatch):
    """A search for a collection that DOES contain items must find them —
    before the fix this returned 'No items found'."""
    _patch(monkeypatch)

    result = server.advanced_search(
        conditions=[{"field": "collection", "operation": "is", "value": "COLCCCCC"}],
        ctx=DummyContext(),
    )

    assert "In Collection C" in result
    assert "No items found" not in result
