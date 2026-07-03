"""Tests for the background batch task runner (batch_runner.py) and the
batch_cleanup_notes integration.

Tests cover:
- TaskStatus serialization / round-trip
- create_task / read_status / update_status / list_tasks
- spawn_task lifecycle: pending → running → completed (and failed)
- batch_cleanup_notes dry_run path unchanged
- batch_cleanup_notes execute path returns task_id + spawns background thread
- get_batch_task_status polling tool
- Worker acquires/releases RLock per-item (not whole-loop)
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock

import pytest
from conftest import DummyContext

from zotero_mcp import server
from zotero_mcp.batch_runner import (
    TaskStatus,
    create_task,
    format_status_markdown,
    list_tasks,
    read_status,
    spawn_task,
    update_status,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_tasks_dir(tmp_path, monkeypatch):
    """Redirect batch_runner to a temp directory so tests are isolated."""
    import zotero_mcp.batch_runner as br

    monkeypatch.setattr(br, "_TASKS_DIR", tmp_path / "batch_tasks")
    return br._TASKS_DIR


# ---------------------------------------------------------------------------
# TaskStatus serialization
# ---------------------------------------------------------------------------


class TestTaskStatusSerialization:
    def test_round_trip(self):
        s = TaskStatus(
            task_id="test123",
            task_type="batch_cleanup_notes",
            status="pending",
            created_at="2026-01-01T00:00:00Z",
            total=5,
            work_items=[{"key": "AAA11111"}, {"key": "BBB22222"}],
        )
        d = s.to_dict()
        assert d["task_id"] == "test123"
        assert d["work_items"] == [{"key": "AAA11111"}, {"key": "BBB22222"}]

        s2 = TaskStatus.from_dict(d)
        assert s2.task_id == "test123"
        assert s2.work_items == s.work_items

    def test_from_dict_ignores_unknown_keys(self):
        """Forward-compat: unknown keys from future versions are ignored."""
        d = {
            "task_id": "test456",
            "task_type": "foo",
            "status": "pending",
            "created_at": "2026-01-01",
            "future_field": "should not break",
        }
        s = TaskStatus.from_dict(d)
        assert s.task_id == "test456"


# ---------------------------------------------------------------------------
# File-backed status store
# ---------------------------------------------------------------------------


class TestStatusStore:
    def test_create_task_writes_file(self, tmp_tasks_dir):
        items = [{"key": "A"}, {"key": "B"}, {"key": "C"}]
        status = create_task("batch_cleanup_notes", work_items=items)
        assert status.status == "pending"
        assert status.total == 3
        assert len(status.work_items) == 3

        # File exists on disk
        path = tmp_tasks_dir / f"{status.task_id}.json"
        assert path.exists()

        # Content is valid JSON
        data = json.loads(path.read_text())
        assert data["task_id"] == status.task_id

    def test_read_status_round_trip(self, tmp_tasks_dir):
        items = [{"key": "A"}]
        status = create_task("test_type", work_items=items)
        loaded = read_status(status.task_id)
        assert loaded is not None
        assert loaded.task_id == status.task_id
        assert loaded.task_type == "test_type"
        assert loaded.work_items == items

    def test_read_status_nonexistent(self, tmp_tasks_dir):
        assert read_status("nonexistent") is None

    def test_update_status_partial(self, tmp_tasks_dir):
        status = create_task("test_type", work_items=[{"key": "A"}])
        update_status(status.task_id, processed=1, succeeded=1, status="running")
        loaded = read_status(status.task_id)
        assert loaded is not None
        assert loaded.processed == 1
        assert loaded.succeeded == 1
        assert loaded.status == "running"

    def test_list_tasks_filtered(self, tmp_tasks_dir):
        create_task("type_a", work_items=[])
        create_task("type_b", work_items=[])
        create_task("type_a", work_items=[])

        all_tasks = list_tasks()
        assert len(all_tasks) == 3

        type_a = list_tasks("type_a")
        assert len(type_a) == 2
        assert all(t.task_type == "type_a" for t in type_a)

    def test_list_tasks_empty_dir(self, tmp_tasks_dir):
        assert list_tasks() == []


# ---------------------------------------------------------------------------
# spawn_task lifecycle
# ---------------------------------------------------------------------------


class TestSpawnTask:
    def test_completed_lifecycle(self, tmp_tasks_dir):
        """spawn_task transitions pending → running → completed."""
        status = create_task("test", work_items=[{"key": "A"}, {"key": "B"}])

        def worker(s: TaskStatus) -> None:
            for i, item in enumerate(s.work_items):
                update_status(s.task_id, processed=i + 1, succeeded=i + 1)

        spawn_task(status, worker)

        # Wait for completion (daemon thread, but we need to join-like)

        deadline = time.time() + 5
        while time.time() < deadline:
            loaded = read_status(status.task_id)
            if loaded and loaded.status == "completed":
                break
            time.sleep(0.05)

        loaded = read_status(status.task_id)
        assert loaded is not None
        assert loaded.status == "completed"
        assert loaded.processed == 2
        assert loaded.succeeded == 2
        assert loaded.started_at is not None
        assert loaded.completed_at is not None

    def test_failed_lifecycle(self, tmp_tasks_dir):
        """If worker raises, status becomes 'failed' with error message."""
        status = create_task("test", work_items=[{"key": "A"}])

        def worker(s: TaskStatus) -> None:
            raise ValueError("something went wrong")

        spawn_task(status, worker)

        deadline = time.time() + 5
        while time.time() < deadline:
            loaded = read_status(status.task_id)
            if loaded and loaded.status in ("failed", "completed"):
                break
            time.sleep(0.05)

        loaded = read_status(status.task_id)
        assert loaded is not None
        assert loaded.status == "failed"
        assert "something went wrong" in (loaded.error or "")


# ---------------------------------------------------------------------------
# format_status_markdown
# ---------------------------------------------------------------------------


class TestFormatStatus:
    def test_includes_key_fields(self):
        s = TaskStatus(
            task_id="abc123",
            task_type="batch_cleanup_notes",
            status="completed",
            created_at="2026-01-01",
            total=10,
            processed=10,
            succeeded=9,
            failed=1,
            result_summary="Trashed 9 notes, 1 failed.",
        )
        md = format_status_markdown(s)
        assert "abc123" in md
        assert "completed" in md
        assert "10/10" in md
        assert "9" in md
        assert "Trashed 9 notes" in md

    def test_shows_succeeded_and_failed_items(self):
        s = TaskStatus(
            task_id="test456",
            task_type="batch_cleanup_notes",
            status="completed",
            created_at="2026-01-01",
            total=3,
            processed=3,
            succeeded=2,
            failed=1,
            succeeded_items=[
                {"key": "AAA11111"},
                {"key": "BBB22222", "detail": "trashed"},
            ],
            failed_items=[
                {"key": "CCC33333", "detail": "HTTP 500"},
            ],
        )
        md = format_status_markdown(s)
        assert "Succeeded (2)" in md
        assert "`AAA11111`" in md
        assert "`BBB22222`" in md
        assert "trashed" in md
        assert "Failed (1)" in md
        assert "`CCC33333`" in md
        assert "HTTP 500" in md

    def test_truncates_long_item_lists(self):
        s = TaskStatus(
            task_id="test789",
            task_type="batch_cleanup_notes",
            status="completed",
            created_at="2026-01-01",
            total=60,
            processed=60,
            succeeded=55,
            failed=5,
            succeeded_items=[{"key": f"K{i:04d}"} for i in range(55)],
            failed_items=[{"key": f"F{i:04d}", "detail": "err"} for i in range(5)],
        )
        md = format_status_markdown(s)
        assert "and 5 more" in md  # 55 - 50 = 5 truncated


# ---------------------------------------------------------------------------
# batch_cleanup_notes integration
# ---------------------------------------------------------------------------


class _FakePatchResponse:
    def __init__(self, status_code=204, text=""):
        self.status_code = status_code
        self.text = text


class _FakeHttpxClient:
    def __init__(self, status_code=204, fail_keys=None):
        self._status_code = status_code
        self._fail_keys = fail_keys or set()
        self.calls = []

    def patch(self, url, headers, content):
        self.calls.append({"url": url, "headers": headers, "content": content})
        key = url.rstrip("/").split("/")[-1]
        if key in self._fail_keys:
            return _FakePatchResponse(500, "server error")
        return _FakePatchResponse(self._status_code)


class _FakeZotero:
    def __init__(self, notes, patch_status=204, fail_keys=None):
        self._notes = notes
        self.endpoint = "https://api.zotero.org"
        self.library_type = "users"
        self.library_id = "12345"
        self.client = _FakeHttpxClient(patch_status, fail_keys)

    def items(self, start=0, limit=100, **kwargs):
        pool = self._notes
        item_type = kwargs.get("itemType")
        if item_type:
            pool = [n for n in pool if n.get("data", {}).get("itemType") == item_type]
        return pool[start : start + limit]

    def item(self, key):
        for n in self._notes:
            if n.get("key") == key:
                return n
        raise KeyError(key)


def _make_note(key, content="", parent=None, version=1):
    data = {"itemType": "note", "note": content, "version": version}
    if parent:
        data["parentItem"] = parent
    return {"key": key, "version": version, "data": data}


def _patch_zotero(monkeypatch, fake):
    monkeypatch.setattr("zotero_mcp.client.get_zotero_client", lambda: fake)
    monkeypatch.setattr("zotero_mcp.utils.is_local_mode", lambda: False)
    monkeypatch.setattr("zotero_mcp.client.get_web_zotero_client", lambda: fake)


class TestBatchCleanupNotesDryRun:
    """dry_run path should be unchanged — returns preview, no task created."""

    def test_dry_run_returns_preview(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "zotero_mcp.batch_runner._TASKS_DIR", tmp_path / "batch_tasks"
        )
        notes = [_make_note("N0000001"), _make_note("N0000002")]
        fake = _FakeZotero(notes)
        _patch_zotero(monkeypatch, fake)

        result = server.batch_cleanup_notes(dry_run=True, ctx=DummyContext())

        assert "Preview" in result
        assert "Matched: 2 notes" in result
        assert fake.client.calls == []  # no deletions


class TestBatchCleanupNotesExecute:
    """Execute path should return task_id and spawn background thread."""

    def test_execute_returns_task_id(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "zotero_mcp.batch_runner._TASKS_DIR", tmp_path / "batch_tasks"
        )
        notes = [_make_note("N0000001"), _make_note("N0000002")]
        fake = _FakeZotero(notes)
        _patch_zotero(monkeypatch, fake)

        result = server.batch_cleanup_notes(dry_run=False, ctx=DummyContext())

        assert "started" in result.lower() or "⏳" in result
        assert "get_batch_task_status" in result
        # Should NOT have trashed synchronously
        assert "Trashed:" not in result

    def test_execute_creates_status_file(self, monkeypatch, tmp_path):
        tasks_dir = tmp_path / "batch_tasks"
        monkeypatch.setattr("zotero_mcp.batch_runner._TASKS_DIR", tasks_dir)
        notes = [_make_note("N0000001"), _make_note("N0000002")]
        fake = _FakeZotero(notes)
        _patch_zotero(monkeypatch, fake)
        # Patch _get_note_write_client so the background worker gets the
        # fake client (the worker calls it independently of the foreground).
        monkeypatch.setattr(
            "zotero_mcp.tools.annotations._get_note_write_client",
            lambda desc: (fake, None),
        )
        # Patch lock to be no-op for instant acquire.
        monkeypatch.setattr("zotero_mcp.client._lock_timeout", lambda: 0.0)
        fake_lock = MagicMock()
        fake_lock.acquire.return_value = True
        monkeypatch.setattr("zotero_mcp.client._zotero_api_lock", fake_lock)

        server.batch_cleanup_notes(dry_run=False, ctx=DummyContext())

        # Wait for the background thread to finish. The task spawns a daemon
        # thread that writes the status file; poll until it reaches a terminal
        # state (completed/failed) so the assertions below don't race against
        # an in-flight write.
        deadline = time.time() + 5
        status_file = None
        while time.time() < deadline:
            files = list(tasks_dir.glob("*.json"))
            if files:
                status_file = files[0]
                data = json.loads(status_file.read_text())
                if data["status"] in ("completed", "failed"):
                    break
            time.sleep(0.05)

        assert status_file is not None, "No status file was created by the background task"
        data = json.loads(status_file.read_text())
        assert data["task_type"] == "batch_cleanup_notes"
        assert data["total"] == 2
        assert data["status"] in ("pending", "running", "completed")

    def test_worker_trashes_all_notes(self, monkeypatch, tmp_path):
        """The background worker should eventually trash all matched notes."""
        tasks_dir = tmp_path / "batch_tasks"
        monkeypatch.setattr("zotero_mcp.batch_runner._TASKS_DIR", tasks_dir)

        notes = [_make_note("N0000001"), _make_note("N0000002")]
        fake = _FakeZotero(notes)
        _patch_zotero(monkeypatch, fake)
        # Patch _get_note_write_client so the worker gets the fake client.
        monkeypatch.setattr(
            "zotero_mcp.tools.annotations._get_note_write_client",
            lambda desc: (fake, None),
        )
        # Patch lock to be no-op for instant acquire.
        monkeypatch.setattr("zotero_mcp.client._lock_timeout", lambda: 0.0)
        fake_lock = MagicMock()
        fake_lock.acquire.return_value = True
        monkeypatch.setattr("zotero_mcp.client._zotero_api_lock", fake_lock)

        server.batch_cleanup_notes(dry_run=False, ctx=DummyContext())

        # Wait for the background thread to finish
        deadline = time.time() + 5
        while time.time() < deadline:
            files = list(tasks_dir.glob("*.json"))
            if files:
                data = json.loads(files[0].read_text())
                if data["status"] in ("completed", "failed"):
                    break
            time.sleep(0.05)

        # All notes should have been trashed
        assert len(fake.client.calls) == 2
        data = json.loads(files[0].read_text())
        assert data["status"] == "completed"
        assert data["succeeded"] == 2
        assert data["failed"] == 0


# ---------------------------------------------------------------------------
# get_batch_task_status tool
# ---------------------------------------------------------------------------


class TestGetBatchTaskStatus:
    def test_by_task_id(self, tmp_path):
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(
            "zotero_mcp.batch_runner._TASKS_DIR", tmp_path / "batch_tasks"
        )
        try:
            status = create_task(
                "batch_cleanup_notes", work_items=[{"key": "A"}]
            )
            update_status(status.task_id, status="completed", processed=1, succeeded=1)
            result = server.get_batch_task_status(
                task_id=status.task_id, ctx=DummyContext()
            )
            assert status.task_id in result
            assert "completed" in result
            assert "1/1" in result
        finally:
            monkeypatch.undo()

    def test_task_not_found(self, tmp_path):
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(
            "zotero_mcp.batch_runner._TASKS_DIR", tmp_path / "batch_tasks"
        )
        try:
            result = server.get_batch_task_status(
                task_id="nonexistent", ctx=DummyContext()
            )
            assert "not found" in result.lower()
        finally:
            monkeypatch.undo()

    def test_list_recent_tasks(self, tmp_path):
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(
            "zotero_mcp.batch_runner._TASKS_DIR", tmp_path / "batch_tasks"
        )
        try:
            create_task("type_a", work_items=[])
            create_task("type_b", work_items=[])
            result = server.get_batch_task_status(ctx=DummyContext())
            assert "Recent Batch Tasks" in result
            assert "type_a" in result
            assert "type_b" in result
        finally:
            monkeypatch.undo()

    def test_list_filtered_by_type(self, tmp_path):
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(
            "zotero_mcp.batch_runner._TASKS_DIR", tmp_path / "batch_tasks"
        )
        try:
            create_task("type_a", work_items=[])
            create_task("type_b", work_items=[])
            create_task("type_a", work_items=[])
            result = server.get_batch_task_status(
                task_type="type_a", ctx=DummyContext()
            )
            assert "type_a" in result
            assert "Filtered by type: type_a" in result
        finally:
            monkeypatch.undo()


# ---------------------------------------------------------------------------
# DummyCtx — ensures background workers can call ctx.info() without crashing
# ---------------------------------------------------------------------------


class TestDummyCtx:
    """Verify DummyCtx is a safe drop-in for real ctx in background workers.

    This is the regression test for the bug where background workers passed
    ctx=None to helpers like _try_attach_oa_pdf, which call ctx.info()
    without None-guards, causing AttributeError on every PDF cascade hit.
    """

    def test_info_warning_error_are_noops(self):
        from zotero_mcp.batch_runner import DummyCtx

        ctx = DummyCtx()
        # These must not raise
        ctx.info("test message")
        ctx.warning("test warning")
        ctx.error("test error")
        ctx.report_progress(1, 10, "processing")

    def test_handle_write_response_with_dummy_ctx(self):
        """_handle_write_response must not crash with DummyCtx."""
        from zotero_mcp.batch_runner import DummyCtx
        from zotero_mcp.tools._helpers import _handle_write_response

        # A failed response triggers ctx.error() internally
        class FakeResp:
            status_code = 409
            text = "Conflict"

        # Must not raise — DummyCtx.error() is a no-op
        result = _handle_write_response(FakeResp(), DummyCtx())
        assert result is not None

    def test_dummy_ctx_not_none(self):
        """DummyCtx is truthy and not None — passing it avoids NoneType errors."""
        from zotero_mcp.batch_runner import DummyCtx

        ctx = DummyCtx()
        assert ctx is not None
        assert hasattr(ctx, "info")
        assert hasattr(ctx, "warning")
        assert hasattr(ctx, "error")

