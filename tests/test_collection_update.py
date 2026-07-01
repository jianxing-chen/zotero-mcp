"""Tests for zotero_update_collection — rename and move (change parent)."""

import pytest
from conftest import DummyContext, FakeZotero, _FakeResponse

from zotero_mcp import server


class FakeZoteroUpdate(FakeZotero):
    """Tracks update_collection calls and provides collection() lookup."""

    def __init__(self):
        super().__init__()
        self.updated_collections = []

    def collection(self, key, **kwargs):
        for c in self._collections:
            if c.get("key") == key:
                return c
        raise Exception(f"Code: 404 — Not found: collection {key}")

    def update_collection(self, payload, **kwargs):
        self.updated_collections.append(payload)
        return _FakeResponse(204)


def _make_coll(key, name, parent=False, version=1):
    return {
        "key": key,
        "version": version,
        "data": {"name": name, "parentCollection": parent},
    }


@pytest.fixture
def fake_zot():
    zot = FakeZoteroUpdate()
    zot._collections = [
        _make_coll("COLAAAAA", "Machine Learning"),
        _make_coll("COLBBBBB", "Deep Learning", parent="COLAAAAA"),
        _make_coll("COLCCCCC", "NLP Papers"),
    ]
    return zot


@pytest.fixture
def ctx():
    return DummyContext()


def _patch_web_only(monkeypatch, fake_zot):
    monkeypatch.setattr("zotero_mcp.client.get_zotero_client", lambda: fake_zot)
    monkeypatch.setattr(
        "zotero_mcp.tools._helpers._get_write_client", lambda ctx: (fake_zot, fake_zot)
    )


def _patch_local_only(monkeypatch, fake_zot):
    monkeypatch.setattr("zotero_mcp.client.get_zotero_client", lambda: fake_zot)

    def _raise_local_only(ctx):
        raise ValueError("Cannot perform write operations in local-only mode.")

    monkeypatch.setattr("zotero_mcp.tools._helpers._get_write_client", _raise_local_only)


# ===========================================================================
# Rename
# ===========================================================================


class TestRename:
    def test_rename_collection(self, monkeypatch, fake_zot, ctx):
        """new_name changes the collection name."""
        _patch_web_only(monkeypatch, fake_zot)

        result = server.update_collection(
            collection_key="COLAAAAA", new_name="ML Papers", ctx=ctx
        )

        assert "Updated collection" in result
        assert "ML Papers" in result
        assert "`COLAAAAA`" in result
        # The update payload should carry the new name.
        assert len(fake_zot.updated_collections) == 1
        payload = fake_zot.updated_collections[0]
        assert payload["data"]["name"] == "ML Papers"

    def test_rename_preserves_parent(self, monkeypatch, fake_zot, ctx):
        """Renaming a subcollection must not change its parent."""
        _patch_web_only(monkeypatch, fake_zot)

        server.update_collection(
            collection_key="COLBBBBB", new_name="Deep Nets", ctx=ctx
        )

        payload = fake_zot.updated_collections[0]
        assert payload["data"]["name"] == "Deep Nets"
        # Parent should be unchanged.
        assert payload["data"]["parentCollection"] == "COLAAAAA"


# ===========================================================================
# Move (change parent)
# ===========================================================================


class TestMove:
    def test_move_to_top_level_via_root_keyword(self, monkeypatch, fake_zot, ctx):
        """new_parent='root' moves the collection to the top level."""
        _patch_web_only(monkeypatch, fake_zot)

        result = server.update_collection(
            collection_key="COLBBBBB", new_parent="root", ctx=ctx
        )

        assert "Updated collection" in result
        assert "top level" in result.lower()
        payload = fake_zot.updated_collections[0]
        assert payload["data"]["parentCollection"] is False

    def test_move_to_top_level_via_empty_string(self, monkeypatch, fake_zot, ctx):
        """new_parent='' also moves to top level."""
        _patch_web_only(monkeypatch, fake_zot)

        result = server.update_collection(
            collection_key="COLBBBBB", new_parent="", ctx=ctx
        )

        assert "Updated collection" in result
        payload = fake_zot.updated_collections[0]
        assert payload["data"]["parentCollection"] is False

    def test_move_to_new_parent_by_key(self, monkeypatch, fake_zot, ctx):
        """new_parent as a collection key moves under that parent."""
        _patch_web_only(monkeypatch, fake_zot)

        result = server.update_collection(
            collection_key="COLCCCCC", new_parent="COLAAAAA", ctx=ctx
        )

        assert "Updated collection" in result
        payload = fake_zot.updated_collections[0]
        assert payload["data"]["parentCollection"] == "COLAAAAA"

    def test_move_to_new_parent_by_name(self, monkeypatch, fake_zot, ctx):
        """new_parent as a name is resolved to a key."""
        _patch_web_only(monkeypatch, fake_zot)

        result = server.update_collection(
            collection_key="COLCCCCC", new_parent="Machine Learning", ctx=ctx
        )

        assert "Updated collection" in result
        payload = fake_zot.updated_collections[0]
        assert payload["data"]["parentCollection"] == "COLAAAAA"

    def test_move_preserves_name(self, monkeypatch, fake_zot, ctx):
        """Moving must not change the collection name."""
        _patch_web_only(monkeypatch, fake_zot)

        server.update_collection(
            collection_key="COLCCCCC", new_parent="COLAAAAA", ctx=ctx
        )

        payload = fake_zot.updated_collections[0]
        assert payload["data"]["name"] == "NLP Papers"


# ===========================================================================
# Combined rename + move
# ===========================================================================


class TestRenameAndMove:
    def test_rename_and_move_in_one_call(self, monkeypatch, fake_zot, ctx):
        """Both new_name and new_parent applied together."""
        _patch_web_only(monkeypatch, fake_zot)

        result = server.update_collection(
            collection_key="COLCCCCC",
            new_name="Natural Language Processing",
            new_parent="COLAAAAA",
            ctx=ctx,
        )

        assert "Updated collection" in result
        assert "Natural Language Processing" in result
        # Change summary shows the new_parent spec as passed by the caller.
        assert "COLAAAAA" in result

        payload = fake_zot.updated_collections[0]
        assert payload["data"]["name"] == "Natural Language Processing"
        assert payload["data"]["parentCollection"] == "COLAAAAA"

    def test_already_at_top_level_no_op_message(self, monkeypatch, fake_zot, ctx):
        """Moving a top-level collection to root is a no-op, not an error."""
        _patch_web_only(monkeypatch, fake_zot)

        result = server.update_collection(
            collection_key="COLAAAAA", new_parent="root", ctx=ctx
        )

        assert "No changes needed" in result


# ===========================================================================
# Validation / error paths
# ===========================================================================


class TestValidation:
    def test_no_parameters_returns_error(self, monkeypatch, fake_zot, ctx):
        """Neither new_name nor new_parent → clear error."""
        _patch_web_only(monkeypatch, fake_zot)

        result = server.update_collection(
            collection_key="COLAAAAA", ctx=ctx
        )

        assert "Nothing to update" in result
        assert len(fake_zot.updated_collections) == 0

    def test_collection_not_found(self, monkeypatch, fake_zot, ctx):
        """Unknown collection key → error, no update attempted."""
        _patch_web_only(monkeypatch, fake_zot)

        result = server.update_collection(
            collection_key="NOPE0000", new_name="Whatever", ctx=ctx
        )

        assert "not found" in result.lower()
        assert len(fake_zot.updated_collections) == 0

    def test_cannot_be_own_parent(self, monkeypatch, fake_zot, ctx):
        """A collection cannot be moved under itself."""
        _patch_web_only(monkeypatch, fake_zot)

        result = server.update_collection(
            collection_key="COLAAAAA", new_parent="COLAAAAA", ctx=ctx
        )

        assert "its own parent" in result
        assert len(fake_zot.updated_collections) == 0

    def test_parent_name_not_found(self, monkeypatch, fake_zot, ctx):
        """Unresolvable parent name → error, no update."""
        _patch_web_only(monkeypatch, fake_zot)

        result = server.update_collection(
            collection_key="COLCCCCC", new_parent="Nonexistent Folder", ctx=ctx
        )

        assert "Error" in result
        assert "Nonexistent Folder" in result
        assert len(fake_zot.updated_collections) == 0

    def test_local_only_mode_returns_error(self, monkeypatch, fake_zot, ctx):
        """In local-only mode (no API key), write tools should refuse."""
        _patch_local_only(monkeypatch, fake_zot)

        result = server.update_collection(
            collection_key="COLAAAAA", new_name="New Name", ctx=ctx
        )

        assert "local-only" in result
        assert len(fake_zot.updated_collections) == 0
