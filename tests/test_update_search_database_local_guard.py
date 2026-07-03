"""Tests for the always-on guard in ``update_search_database``.

The MCP tool must NOT execute the update — it holds a process-wide
``threading.RLock`` for the entire duration (potentially tens of
minutes), blocking every other Zotero API tool.  Worse, multiple MCP
server processes each hold an independent RLock and can race on the
same ChromaDB, corrupting it.  The guard returns a CLI command string
instead so the user can run the update safely from a terminal.

This guard fires in ALL modes (local and web) — the risk of ChromaDB
corruption from concurrent processes is not mode-specific.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from zotero_mcp import server

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _call_update(dummy_ctx, **kwargs):
    """Invoke the registered update_search_database tool with kwargs."""
    return server.update_search_database(*(), ctx=dummy_ctx, **kwargs)


# ---------------------------------------------------------------------------
# Guard fires in all modes — tool refuses and returns a CLI command
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "env_value,mode",
    [
        ("true", "local"),
        ("yes", "local"),
        ("1", "local"),
        ("", "web"),
    ],
)
class TestGuardAllModes:
    """The guard fires in both local and web mode."""

    def test_default_returns_fulltext_command(self, dummy_ctx, monkeypatch, env_value, mode):
        if env_value:
            monkeypatch.setenv("ZOTERO_LOCAL", env_value)
        else:
            monkeypatch.delenv("ZOTERO_LOCAL", raising=False)
        result = _call_update(dummy_ctx)
        assert "zotero-mcp update-db --fulltext" in result
        assert "terminal" in result.lower()
        # Must NOT have started an actual update
        assert "Starting semantic search database update" not in result

    def test_force_rebuild_returns_rebuild_command(self, dummy_ctx, monkeypatch, env_value, mode):
        if env_value:
            monkeypatch.setenv("ZOTERO_LOCAL", env_value)
        else:
            monkeypatch.delenv("ZOTERO_LOCAL", raising=False)
        result = _call_update(dummy_ctx, force_rebuild=True)
        assert "--force-rebuild" in result
        assert "--fulltext" in result

    def test_reindex_keys_returns_reindex_command(self, dummy_ctx, monkeypatch, env_value, mode):
        if env_value:
            monkeypatch.setenv("ZOTERO_LOCAL", env_value)
        else:
            monkeypatch.delenv("ZOTERO_LOCAL", raising=False)
        result = _call_update(dummy_ctx, reindex_keys=["ABC12345", "DEF67890"])
        assert "--reindex-keys ABC12345,DEF67890" in result

    def test_reindex_keys_with_force(self, dummy_ctx, monkeypatch, env_value, mode):
        if env_value:
            monkeypatch.setenv("ZOTERO_LOCAL", env_value)
        else:
            monkeypatch.delenv("ZOTERO_LOCAL", raising=False)
        result = _call_update(
            dummy_ctx, reindex_keys=["ABC12345"], force_reindex=True
        )
        assert "--reindex-keys ABC12345" in result
        assert "--force" in result

    def test_reindex_cached_mineru_returns_command(self, dummy_ctx, monkeypatch, env_value, mode):
        if env_value:
            monkeypatch.setenv("ZOTERO_LOCAL", env_value)
        else:
            monkeypatch.delenv("ZOTERO_LOCAL", raising=False)
        result = _call_update(dummy_ctx, reindex_cached_mineru=True)
        assert "--reindex-cached-mineru" in result

    def test_reindex_cached_mineru_with_force(self, dummy_ctx, monkeypatch, env_value, mode):
        if env_value:
            monkeypatch.setenv("ZOTERO_LOCAL", env_value)
        else:
            monkeypatch.delenv("ZOTERO_LOCAL", raising=False)
        result = _call_update(
            dummy_ctx, reindex_cached_mineru=True, force_reindex=True
        )
        assert "--reindex-cached-mineru" in result
        assert "--force" in result

    def test_message_mentions_db_status(self, dummy_ctx, monkeypatch, env_value, mode):
        if env_value:
            monkeypatch.setenv("ZOTERO_LOCAL", env_value)
        else:
            monkeypatch.delenv("ZOTERO_LOCAL", raising=False)
        result = _call_update(dummy_ctx)
        assert "db-status" in result


class TestGuardDoesNotExecute:
    """Verify the guard fires *before* any semantic-search import or call."""

    def test_create_semantic_search_not_called(self, dummy_ctx, monkeypatch):
        monkeypatch.setenv("ZOTERO_LOCAL", "true")
        with patch(
            "zotero_mcp.semantic_search.create_semantic_search"
        ) as mock_create:
            result = _call_update(dummy_ctx)
            mock_create.assert_not_called()
        assert "zotero-mcp update-db" in result

    def test_create_semantic_search_not_called_web_mode(self, dummy_ctx, monkeypatch):
        monkeypatch.delenv("ZOTERO_LOCAL", raising=False)
        with patch(
            "zotero_mcp.semantic_search.create_semantic_search"
        ) as mock_create:
            result = _call_update(dummy_ctx)
            mock_create.assert_not_called()
        assert "zotero-mcp update-db" in result
