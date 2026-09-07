"""Background batch task runner with file-backed status store.

Pattern: tool validates args → registers task → spawns daemon thread →
returns task_id immediately. Background thread does the work, writing
progress to a JSON file. A separate poll-status tool reads the file.

Status files live in ``~/.config/zotero-mcp/batch_tasks/<task_id>.json``
(survives server restart; pollable by a separate tool).

Why not FastMCP's native ``task=True``? That requires Docket + Redis
infrastructure — too heavy for a local MCP server. This module uses a
plain ``threading.Thread`` (daemon) and atomic JSON writes, requiring
zero external dependencies.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


class DummyCtx:
    """No-op MCP context for background worker threads.

    Background workers can't use the real MCP ``Context`` — once the
    tool function returns, the MCP request is complete and ``ctx.info()``
    calls are no-ops or raise. This class provides the same method
    signatures (``info``, ``warning``, ``error``) as no-ops, so it can
    be passed anywhere a ``ctx`` is expected (e.g.
    ``_try_attach_oa_pdf(..., ctx, ...)``, ``_handle_write_response(resp, ctx)``)
    without crashing on ``ctx.info(...)``.

    Workers should use this instead of passing ``None`` — ``None`` would
    crash any helper that calls ``ctx.info()`` without a ``None`` guard
    (and there are ~39 such call sites in ``_helpers.py`` alone).
    """

    def info(self, *_args, **_kwargs) -> None:
        pass

    def warning(self, *_args, **_kwargs) -> None:
        pass

    def error(self, *_args, **_kwargs) -> None:
        pass

    def report_progress(self, *_args, **_kwargs) -> None:
        pass

_TASKS_DIR = Path.home() / ".config" / "zotero-mcp" / "batch_tasks"
_active_lock = threading.Lock()  # guards the in-memory registry
_active_tasks: dict[str, threading.Thread] = {}


@dataclass
class TaskStatus:
    """Persisted status of a background batch task."""

    task_id: str
    task_type: str  # e.g. "batch_cleanup_notes"
    status: str  # "pending" | "running" | "completed" | "failed"
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    total: int = 0
    processed: int = 0
    succeeded: int = 0
    failed: int = 0
    error: str | None = None
    result_summary: str | None = None
    # Per-item results: list of {"key": ..., "status": ..., "detail": ...}
    # so the poll tool can show which items succeeded/failed and why.
    succeeded_items: list[dict] = field(default_factory=list)
    failed_items: list[dict] = field(default_factory=list)
    # The work items (e.g. [{"key": "ABCD1234"}, ...]). Stored in the
    # status file so the background thread can read them without
    # re-scanning the Zotero library.
    work_items: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> TaskStatus:
        """Reconstruct from a dict, ignoring unknown keys (forward-compat)."""
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _ensure_tasks_dir() -> Path:
    _TASKS_DIR.mkdir(parents=True, exist_ok=True)
    return _TASKS_DIR


def _status_path(task_id: str) -> Path:
    return _TASKS_DIR / f"{task_id}.json"


def _save_status(status: TaskStatus) -> None:
    """Atomically write status to a JSON file (tmp + rename).

    Two Windows-specific hazards need handling beyond POSIX's plain rename:

    - ``os.replace`` raises ``PermissionError`` (WinError 5) while another
      thread — typically a status poller's ``read_status`` — holds the
      destination open, so retry briefly instead of failing the batch task.
    - A fixed tmp name lets two concurrent saves clobber each other's tmp
      file, so each save writes its own.
    """
    _ensure_tasks_dir()
    path = _status_path(status.task_id)
    tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    with open(tmp, "w") as f:
        json.dump(status.to_dict(), f, indent=2, default=str)
    for attempt in range(6):
        try:
            tmp.replace(path)  # atomic on POSIX
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.02 * (attempt + 1))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def create_task(task_type: str, work_items: list[dict], **extra) -> TaskStatus:
    """Register a new task, write its initial status, return it.

    Args:
        task_type: human-readable task type (e.g. "batch_cleanup_notes").
        work_items: the items to process (stored in the status file so
            the background thread can read them without re-scanning).
        **extra: additional fields to set on the TaskStatus.

    Returns:
        The initial TaskStatus with status="pending".
    """
    task_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    status = TaskStatus(
        task_id=task_id,
        task_type=task_type,
        status="pending",
        created_at=datetime.now(timezone.utc).isoformat(),
        total=len(work_items),
        work_items=work_items,
        **{k: v for k, v in extra.items() if k in TaskStatus.__dataclass_fields__},
    )
    _save_status(status)
    return status


def update_status(task_id: str, **fields) -> None:
    """Update specific fields of a task's status (reads, mutates, writes)."""
    status = read_status(task_id)
    if status is None:
        return
    for k, v in fields.items():
        setattr(status, k, v)
    _save_status(status)


def read_status(task_id: str) -> TaskStatus | None:
    """Read a task's status from disk. Returns None if not found."""
    path = _status_path(task_id)
    if not path.exists():
        return None
    try:
        with open(path) as f:
            return TaskStatus.from_dict(json.load(f))
    except Exception:
        return None


def list_tasks(task_type: str | None = None) -> list[TaskStatus]:
    """List all tasks, optionally filtered by type. Newest first."""
    if not _TASKS_DIR.exists():
        return []
    tasks: list[TaskStatus] = []
    for p in sorted(_TASKS_DIR.glob("*.json"), reverse=True):
        try:
            s = TaskStatus.from_dict(json.loads(p.read_text()))
            if task_type is None or s.task_type == task_type:
                tasks.append(s)
        except Exception:
            continue
    return tasks


def spawn_task(status: TaskStatus, worker_fn: Callable[[TaskStatus], None]) -> None:
    """Spawn a daemon thread to run ``worker_fn(status)``.

    The wrapper transitions status: pending → running → completed/failed.
    The worker_fn receives the TaskStatus and should call update_status()
    periodically to report progress.
    """
    now = datetime.now(timezone.utc).isoformat()

    def _run() -> None:
        try:
            update_status(status.task_id, status="running", started_at=now)
            worker_fn(status)
            # The worker is responsible for setting its own terminal status
            # (completed/failed) via update_status() — it has the context to
            # distinguish "parse succeeded" from "parse returned None". Only
            # set completed here if the worker forgot to (defensive fallback).
            current = read_status(status.task_id)
            if current is None or current.status == "running":
                update_status(
                    status.task_id,
                    status="completed",
                    completed_at=datetime.now(timezone.utc).isoformat(),
                )
        except Exception as e:
            logger.exception(f"Batch task {status.task_id} failed: {e}")
            update_status(
                status.task_id,
                status="failed",
                error=str(e),
                completed_at=datetime.now(timezone.utc).isoformat(),
            )

    t = threading.Thread(target=_run, daemon=True, name=f"zmcp-batch-{status.task_id}")
    with _active_lock:
        _active_tasks[status.task_id] = t
    t.start()


def format_status_markdown(status: TaskStatus) -> str:
    """Format a single task status as markdown for the poll tool."""
    lines = [
        f"# Batch Task: {status.task_id}",
        "",
        f"- **Type:** {status.task_type}",
        f"- **Status:** {status.status}",
        f"- **Progress:** {status.processed}/{status.total}",
        f"- **Succeeded:** {status.succeeded}",
        f"- **Failed:** {status.failed}",
        f"- **Created:** {status.created_at}",
    ]
    if status.started_at:
        lines.append(f"- **Started:** {status.started_at}")
    if status.completed_at:
        lines.append(f"- **Completed:** {status.completed_at}")
    if status.error:
        lines.append(f"- **Error:** {status.error}")

    # Per-item results (shown when completed, or partially while running).
    if status.succeeded_items:
        lines.append("")
        lines.append(f"## Succeeded ({len(status.succeeded_items)})")
        for item in status.succeeded_items[:50]:
            key = item.get("key", "?")
            detail = item.get("detail", "")
            if detail:
                lines.append(f"- `{key}` — {detail}")
            else:
                lines.append(f"- `{key}`")
        if len(status.succeeded_items) > 50:
            lines.append(f"... and {len(status.succeeded_items) - 50} more")

    if status.failed_items:
        lines.append("")
        lines.append(f"## Failed ({len(status.failed_items)})")
        for item in status.failed_items[:50]:
            key = item.get("key", "?")
            detail = item.get("detail", item.get("error", ""))
            if detail:
                lines.append(f"- `{key}` — {detail}")
            else:
                lines.append(f"- `{key}`")
        if len(status.failed_items) > 50:
            lines.append(f"... and {len(status.failed_items) - 50} more")

    if status.result_summary:
        lines.append("")
        lines.append(status.result_summary)
    return "\n".join(lines)
