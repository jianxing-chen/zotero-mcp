"""Tests for the local-mode guard in ``update_search_database``.

In local mode (``ZOTERO_LOCAL=true``) the MCP tool must NOT execute the
update — it holds a process-wide ``threading.RLock`` for the entire
duration (potentially tens of minutes), blocking every other Zotero API
tool.  Worse, multiple MCP server processes each hold an independent
RLock and can race on the same ChromaDB, corrupting it.  The guard
returns a CLI command string instead so the user can run the update
safely from a terminal.
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
# Local-mode guard — tool refuses and returns a CLI command
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "env_value",
    ["true", "yes", "1", "True", "TRUE"],
)
class TestLocalModeGuard:
    """In local mode the tool returns a CLI command, never executes."""

    def test_default_returns_fulltext_command(self, dummy_ctx, monkeypatch, env_value):
        monkeypatch.setenv("ZOTERO_LOCAL", env_value)
        result = _call_update(dummy_ctx)
        assert "zotero-mcp update-db --fulltext" in result
        assert "terminal" in result.lower()
        # Must NOT have started an actual update
        assert "Starting semantic search database update" not in result

    def test_force_rebuild_returns_rebuild_command(self, dummy_ctx, monkeypatch, env_value):
        monkeypatch.setenv("ZOTERO_LOCAL", env_value)
        result = _call_update(dummy_ctx, force_rebuild=True)
        assert "--force-rebuild" in result
        assert "--fulltext" in result

    def test_reindex_keys_returns_reindex_command(self, dummy_ctx, monkeypatch, env_value):
        monkeypatch.setenv("ZOTERO_LOCAL", env_value)
        result = _call_update(dummy_ctx, reindex_keys=["ABC12345", "DEF67890"])
        assert "--reindex-keys ABC12345,DEF67890" in result

    def test_reindex_keys_with_force(self, dummy_ctx, monkeypatch, env_value):
        monkeypatch.setenv("ZOTERO_LOCAL", env_value)
        result = _call_update(
            dummy_ctx, reindex_keys=["ABC12345"], force_reindex=True
        )
        assert "--reindex-keys ABC12345" in result
        assert "--force" in result

    def test_reindex_cached_mineru_returns_command(self, dummy_ctx, monkeypatch, env_value):
        monkeypatch.setenv("ZOTERO_LOCAL", env_value)
        result = _call_update(dummy_ctx, reindex_cached_mineru=True)
        assert "--reindex-cached-mineru" in result

    def test_reindex_cached_mineru_with_force(self, dummy_ctx, monkeypatch, env_value):
        monkeypatch.setenv("ZOTERO_LOCAL", env_value)
        result = _call_update(
            dummy_ctx, reindex_cached_mineru=True, force_reindex=True
        )
        assert "--reindex-cached-mineru" in result
        assert "--force" in result

    def test_message_mentions_db_status(self, dummy_ctx, monkeypatch, env_value):
        monkeypatch.setenv("ZOTERO_LOCAL", env_value)
        result = _call_update(dummy_ctx)
        assert "db-status" in result


class TestLocalModeGuardDoesNotExecute:
    """Verify the guard fires *before* any semantic-search import or call."""

    def test_create_semantic_search_not_called(self, dummy_ctx, monkeypatch):
        monkeypatch.setenv("ZOTERO_LOCAL", "true")
        with patch(
            "zotero_mcp.semantic_search.create_semantic_search"
        ) as mock_create:
            result = _call_update(dummy_ctx)
            mock_create.assert_not_called()
        assert "zotero-mcp update-db" in result


# ---------------------------------------------------------------------------
# Cloud mode — guard does NOT fire, tool proceeds normally
# ---------------------------------------------------------------------------


class TestCloudModeNotGuarded:
    """In cloud mode the tool should execute as before (no guard)."""

    def test_no_local_env_proceeds_to_execution(self, dummy_ctx, monkeypatch):
        monkeypatch.delenv("ZOTERO_LOCAL", raising=False)
        # Mock create_semantic_search so we get past the import guard
        mock_search = MagicMock()
        mock_search.update_database.return_value = {
            "total_items": 10,
            "processed_items": 5,
            "added_items": 3,
            "updated_items": 2,
            "skipped_items": 0,
            "errors": 0,
            "duration": "0:00:01",
            "start_time": "2026-01-01T00:00:00",
            "end_time": "2026-01-01T00:00:01",
        }
        with patch(
            "zotero_mcp.semantic_search.create_semantic_search", return_value=mock_search
        ):
            result = _call_update(dummy_ctx)
        # Should have actually called update_database
        mock_search.update_database.assert_called_once()
        assert "Database Update Results" in result
        # Should NOT contain the guard message
        assert "Run this command in your terminal" not in result
