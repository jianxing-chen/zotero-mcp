"""Tests for the MinerU-first / PyMuPDF-fallback routing in read_pdf_pages.

Patches mineru_client and _get_pdf_path so no real Zotero/PDF/MinerU I/O
happens. Verifies: (1) MinerU path wins when enabled+available; (2) every
MinerU failure mode falls back to PyMuPDF; (3) disabled config uses PyMuPDF
with zero behavior change.
"""

import sys

import pytest

from zotero_mcp import mineru_client
from zotero_mcp.tools import read_pdf


class _FakeFitzDoc:
    """Minimal fitz document stub for the PyMuPDF fallback path."""

    def __init__(self, pages_text):
        self._pages = pages_text

    def __len__(self):
        return len(self._pages)

    def __getitem__(self, i):
        return _FakeFitzPage(self._pages[i])

    def close(self):
        pass


class _FakeFitzPage:
    def __init__(self, text):
        self._text = text

    def get_text(self):
        return self._text


class _FakeCtx:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


@pytest.fixture
def ctx():
    return _FakeCtx()


def _patch_path(monkeypatch, pdf_path, title, att_key):
    """Patch _get_pdf_path to return a fixed (path, title, key) tuple."""
    monkeypatch.setattr(
        read_pdf,
        "_get_pdf_path",
        lambda item_key, _ctx: (str(pdf_path), title, att_key),
    )


def _patch_pymupdf(monkeypatch, pages_text):
    """Patch _probe_total_pages and the fitz open used by the fallback."""

    class _FakeFitzModule:
        @staticmethod
        def open(_path):
            return _FakeFitzDoc(pages_text)

    monkeypatch.setattr(read_pdf, "_probe_total_pages", lambda _p: len(pages_text))
    # _extract_with_pymupdf imports fitz locally; inject our fake module.
    monkeypatch.setitem(sys.modules, "fitz", _FakeFitzModule)


class TestMineruPreferred:
    def test_mineru_cache_hit_returns_structured_content(self, tmp_path, monkeypatch, ctx):
        """Cache hit: MinerU enabled + available + valid cache → instant structured output."""
        pdf = tmp_path / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        _patch_path(monkeypatch, pdf, "Test Paper", "ATTKEY")
        # PyMuPDF fallback must NOT run; report 3 pages so requesting page 2
        # passes range validation, then MinerU serves it from its own pages list.
        monkeypatch.setattr(read_pdf, "_probe_total_pages", lambda _p: 3)
        monkeypatch.setitem(
            sys.modules, "fitz", type("F", (), {"open": staticmethod(lambda _p: _FakeFitzDoc(["MUST NOT RUN"]))})
        )

        monkeypatch.setattr(mineru_client, "load_mineru_config", lambda: {"enabled": True, "backend": "cloud", "cloud_token": "tok"})
        monkeypatch.setattr(mineru_client, "is_mineru_enabled", lambda _c: True)
        monkeypatch.setattr(mineru_client, "is_mineru_available", lambda _c: True)

        parsed = mineru_client.ParseResult(
            markdown="# full doc\n\n$$Attention(Q,K,V)=softmax(...)$$",
            pages=["page 0 content", "$$formula$$", "page 2"],
            source="mineru:cached",
        )
        # Cache hit path: _cache_is_valid → True, _read_cache → ParseResult.
        # read_cached_or_parse must NOT be called in the foreground.
        monkeypatch.setattr(mineru_client, "_cache_is_valid", lambda _key, _path, _cfg: True)
        monkeypatch.setattr(mineru_client, "_read_cache", lambda _key, _cfg: parsed)
        parse_called = {"n": 0}
        monkeypatch.setattr(
            mineru_client, "read_cached_or_parse",
            lambda *a, **k: parse_called.__setitem__("n", 1) or None,
        )

        out = read_pdf.read_pdf_pages("ITEM1", 2, 2, ctx=ctx)
        assert "MinerU (cached)" in out
        assert "$$formula$$" in out  # structured output preserved
        assert "PyMuPDF" not in out  # fallback did NOT run
        assert "## Page 2" in out
        assert parse_called["n"] == 0  # cache hit: no parse in foreground
        # Coverage header (provenance for citation)
        assert "**Coverage:** p.2-2/3" in out
        assert "**Cache:** 命中" in out

    def test_mineru_cache_hit_shows_new_cache_status_for_fresh_parse(self, tmp_path, monkeypatch, ctx):
        """Fresh parse (not cached) → Coverage header shows '新建'."""
        pdf = tmp_path / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        _patch_path(monkeypatch, pdf, "Test Paper", "ATTKEY")
        monkeypatch.setattr(read_pdf, "_probe_total_pages", lambda _p: 5)
        monkeypatch.setitem(
            sys.modules, "fitz", type("F", (), {"open": staticmethod(lambda _p: _FakeFitzDoc(["X"]))})
        )
        monkeypatch.setattr(mineru_client, "load_mineru_config", lambda: {"enabled": True, "backend": "cloud", "cloud_token": "tok"})
        monkeypatch.setattr(mineru_client, "is_mineru_enabled", lambda _c: True)
        monkeypatch.setattr(mineru_client, "is_mineru_available", lambda _c: True)

        parsed = mineru_client.ParseResult(
            markdown="# full doc", pages=["p1", "p2", "p3", "p4", "p5"],
            source="mineru:cloud-vlm",  # fresh parse, not cached
        )
        monkeypatch.setattr(mineru_client, "_cache_is_valid", lambda _key, _path, _cfg: True)
        monkeypatch.setattr(mineru_client, "_read_cache", lambda _key, _cfg: parsed)

        out = read_pdf.read_pdf_pages("ITEM1", 1, 3, ctx=ctx)
        assert "**Coverage:** p.1-3/5" in out
        assert "**Cache:** 新建" in out

    def test_mineru_disabled_falls_back_silently(self, tmp_path, monkeypatch, ctx):
        pdf = tmp_path / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        _patch_path(monkeypatch, pdf, "Paper", "ATTKEY")
        _patch_pymupdf(monkeypatch, ["page1", "page2 text"])

        monkeypatch.setattr(mineru_client, "load_mineru_config", lambda: {})
        monkeypatch.setattr(mineru_client, "is_mineru_enabled", lambda _c: False)

        out = read_pdf.read_pdf_pages("ITEM1", 2, 2, ctx=ctx)
        assert "PyMuPDF (fallback)" in out
        assert "page2 text" in out
        assert "MinerU" not in out

    def test_mineru_unavailable_falls_back(self, tmp_path, monkeypatch, ctx):
        pdf = tmp_path / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        _patch_path(monkeypatch, pdf, "Paper", "ATTKEY")
        _patch_pymupdf(monkeypatch, ["fallback page"])

        monkeypatch.setattr(mineru_client, "load_mineru_config", lambda: {"enabled": True})
        monkeypatch.setattr(mineru_client, "is_mineru_enabled", lambda _c: True)
        monkeypatch.setattr(mineru_client, "is_mineru_available", lambda _c: False)

        out = read_pdf.read_pdf_pages("ITEM1", 1, 1, ctx=ctx)
        assert "PyMuPDF (fallback)" in out
        assert "fallback page" in out

    def test_mineru_cache_miss_returns_task_id(self, tmp_path, monkeypatch, ctx):
        """Cache miss: MinerU enabled + available + cold cache → return task_id,
        do NOT block on read_cached_or_parse in the foreground, do NOT fall
        back to PyMuPDF (the worker handles parsing in the background).
        """
        pdf = tmp_path / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        _patch_path(monkeypatch, pdf, "Paper", "ATTKEY")
        _patch_pymupdf(monkeypatch, ["MUST NOT RUN fallback"])

        monkeypatch.setattr(mineru_client, "load_mineru_config", lambda: {"enabled": True, "backend": "cloud", "cloud_token": "tok"})
        monkeypatch.setattr(mineru_client, "is_mineru_enabled", lambda _c: True)
        monkeypatch.setattr(mineru_client, "is_mineru_available", lambda _c: True)
        # Cold cache: _cache_is_valid → False (so the foreground takes the
        # background-task branch instead of returning cached content).
        monkeypatch.setattr(mineru_client, "_cache_is_valid", lambda _k, _p, _c: False)

        # Spy on create_task to capture the work_items without actually
        # spawning a real thread (which would call read_cached_or_parse).
        captured = {}

        def _fake_create_task(task_type, work_items=None, **extra):
            captured["task_type"] = task_type
            captured["work_items"] = work_items
            # Return a minimal TaskStatus-like object with a fake task_id.
            from zotero_mcp.batch_runner import TaskStatus
            return TaskStatus(
                task_id="TESTTASKID",
                task_type=task_type,
                status="pending",
                created_at="now",
                total=len(work_items or []),
                work_items=work_items or [],
            )

        spawn_called = {"n": 0}
        # Import the names _try_mineru uses (it imports inside the function).
        import zotero_mcp.batch_runner as _br
        monkeypatch.setattr(_br, "create_task", _fake_create_task)
        monkeypatch.setattr(_br, "spawn_task", lambda _status, _worker: spawn_called.__setitem__("n", 1))

        # read_cached_or_parse must NOT be called in the foreground.
        parse_called = {"n": 0}
        monkeypatch.setattr(
            mineru_client, "read_cached_or_parse",
            lambda *a, **k: parse_called.__setitem__("n", 1) or None,
        )

        out = read_pdf.read_pdf_pages("ITEM1", 1, 1, ctx=ctx)
        assert "TESTTASKID" in out  # task_id returned to caller
        assert "zotero_get_batch_task_status" in out  # polling hint
        assert "MUST NOT RUN" not in out  # PyMuPDF extraction content not shown
        assert "PyMuPDF (fallback)" not in out  # did NOT run the PyMuPDF fallback path
        assert parse_called["n"] == 0  # no synchronous parse in foreground
        assert captured["task_type"] == "mineru_parse"
        assert spawn_called["n"] == 1  # spawn_task was invoked
        assert captured["work_items"][0]["attachment_key"] == "ATTKEY"
        assert captured["work_items"][0]["item_key"] == "ITEM1"

    def test_mineru_cache_corrupt_falls_through_to_background(self, tmp_path, monkeypatch, ctx):
        """Cache valid but _read_cache returns None (corrupt) → invalidate + background task."""
        pdf = tmp_path / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        _patch_path(monkeypatch, pdf, "Paper", "ATTKEY")
        monkeypatch.setattr(read_pdf, "_probe_total_pages", lambda _p: 3)
        monkeypatch.setitem(
            sys.modules, "fitz", type("F", (), {"open": staticmethod(lambda _p: _FakeFitzDoc(["X"]))})
        )
        monkeypatch.setattr(mineru_client, "load_mineru_config", lambda: {"enabled": True, "backend": "cloud", "cloud_token": "tok"})
        monkeypatch.setattr(mineru_client, "is_mineru_enabled", lambda _c: True)
        monkeypatch.setattr(mineru_client, "is_mineru_available", lambda _c: True)
        monkeypatch.setattr(mineru_client, "_cache_is_valid", lambda _k, _p, _c: True)
        monkeypatch.setattr(mineru_client, "_read_cache", lambda _k, _c: None)
        invalidate_called = {"n": 0}
        monkeypatch.setattr(
            mineru_client, "_invalidate_cache",
            lambda _k, _c: invalidate_called.__setitem__("n", 1),
        )
        import zotero_mcp.batch_runner as _br
        monkeypatch.setattr(_br, "create_task", lambda task_type, work_items=None, **extra: type(
            "S", (), {"task_id": "CORRUPT1", "task_type": task_type, "work_items": work_items}
        ))
        monkeypatch.setattr(_br, "spawn_task", lambda _s, _w: None)

        out = read_pdf.read_pdf_pages("ITEM1", 1, 1, ctx=ctx)
        assert "CORRUPT1" in out  # background task spawned after cache invalidate
        assert invalidate_called["n"] == 1

    def test_no_attachment_key_skips_mineru(self, tmp_path, monkeypatch, ctx):
        """Without an attachment key we can't cache; MinerU must be skipped."""
        pdf = tmp_path / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        # att_key is None
        monkeypatch.setattr(read_pdf, "_get_pdf_path", lambda item_key, _ctx: (str(pdf), "Paper", None))
        _patch_pymupdf(monkeypatch, ["fallback without key"])

        monkeypatch.setattr(mineru_client, "load_mineru_config", lambda: {"enabled": True})
        monkeypatch.setattr(mineru_client, "is_mineru_enabled", lambda _c: True)
        monkeypatch.setattr(mineru_client, "is_mineru_available", lambda _c: True)

        parse_called = {"n": 0}
        monkeypatch.setattr(
            mineru_client, "read_cached_or_parse", lambda *a, **k: parse_called.__setitem__("n", 1) or None
        )

        out = read_pdf.read_pdf_pages("ITEM1", 1, 1, ctx=ctx)
        assert "PyMuPDF (fallback)" in out
        assert parse_called["n"] == 0  # MinerU not invoked without a cache key

    def test_backend_param_recorded_in_work_items(self, tmp_path, monkeypatch, ctx):
        """backend='cloud' is recorded in work_items for the background worker."""
        pdf = tmp_path / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        _patch_path(monkeypatch, pdf, "Paper", "ATTKEY")
        monkeypatch.setattr(read_pdf, "_probe_total_pages", lambda _p: 3)
        monkeypatch.setitem(sys.modules, "fitz", type("F", (), {"open": staticmethod(lambda _p: _FakeFitzDoc(["X"]))}))

        monkeypatch.setattr(mineru_client, "load_mineru_config", lambda: {"enabled": True, "backend": "cloud", "cloud_token": "tok"})
        monkeypatch.setattr(mineru_client, "is_mineru_enabled", lambda _c: True)
        monkeypatch.setattr(mineru_client, "is_mineru_available", lambda _c: True)
        monkeypatch.setattr(mineru_client, "_cache_is_valid", lambda _k, _p, _c: False)

        captured = {}

        def _fake_create_task(task_type, work_items=None, **extra):
            captured["task_type"] = task_type
            captured["work_items"] = work_items
            from zotero_mcp.batch_runner import TaskStatus
            return TaskStatus(
                task_id="BACKENDTEST", task_type=task_type, status="pending",
                created_at="now", total=len(work_items or []), work_items=work_items or [],
            )

        import zotero_mcp.batch_runner as _br
        monkeypatch.setattr(_br, "create_task", _fake_create_task)
        monkeypatch.setattr(_br, "spawn_task", lambda _s, _w: None)

        read_pdf.read_pdf_pages("ITEM1", 1, 1, backend="cloud", ctx=ctx)
        assert captured["task_type"] == "mineru_parse"
        assert captured["work_items"][0]["backend"] == "cloud"

    def test_no_backend_records_none_in_work_items(self, tmp_path, monkeypatch, ctx):
        """Omitting backend records None in work_items (worker uses configured backend)."""
        pdf = tmp_path / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        _patch_path(monkeypatch, pdf, "Paper", "ATTKEY")
        monkeypatch.setattr(read_pdf, "_probe_total_pages", lambda _p: 3)
        monkeypatch.setitem(sys.modules, "fitz", type("F", (), {"open": staticmethod(lambda _p: _FakeFitzDoc(["X"]))}))

        monkeypatch.setattr(mineru_client, "load_mineru_config", lambda: {"enabled": True, "backend": "cloud", "cloud_token": "tok"})
        monkeypatch.setattr(mineru_client, "is_mineru_enabled", lambda _c: True)
        monkeypatch.setattr(mineru_client, "is_mineru_available", lambda _c: True)
        monkeypatch.setattr(mineru_client, "_cache_is_valid", lambda _k, _p, _c: False)

        captured = {}

        def _fake_create_task(task_type, work_items=None, **extra):
            captured["work_items"] = work_items
            from zotero_mcp.batch_runner import TaskStatus
            return TaskStatus(
                task_id="NOBACKEND", task_type=task_type, status="pending",
                created_at="now", total=len(work_items or []), work_items=work_items or [],
            )

        import zotero_mcp.batch_runner as _br
        monkeypatch.setattr(_br, "create_task", _fake_create_task)
        monkeypatch.setattr(_br, "spawn_task", lambda _s, _w: None)

        read_pdf.read_pdf_pages("ITEM1", 1, 1, ctx=ctx)
        assert captured["work_items"][0]["backend"] is None


class TestRangeValidation:
    def test_start_page_out_of_range(self, tmp_path, monkeypatch, ctx):
        pdf = tmp_path / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        _patch_path(monkeypatch, pdf, "Paper", "ATTKEY")
        monkeypatch.setattr(read_pdf, "_probe_total_pages", lambda _p: 3)
        monkeypatch.setattr(mineru_client, "load_mineru_config", lambda: {})

        out = read_pdf.read_pdf_pages("ITEM1", 10, 12, ctx=ctx)
        assert "out of range" in out
        assert "3 pages" in out

    def test_no_50_page_limit(self, tmp_path, monkeypatch, ctx):
        """The 50-page-per-call cap has been removed; requesting 60 pages of a
        100-page PDF should NOT error with 'max 50'."""
        pdf = tmp_path / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        _patch_path(monkeypatch, pdf, "Paper", "ATTKEY")
        monkeypatch.setattr(read_pdf, "_probe_total_pages", lambda _p: 100)
        monkeypatch.setattr(mineru_client, "load_mineru_config", lambda: {})
        monkeypatch.setattr(mineru_client, "is_mineru_enabled", lambda _c: False)

        out = read_pdf.read_pdf_pages("ITEM1", 1, 60, ctx=ctx)
        assert "max 50" not in out

    def test_pymupdf_unavailable_blocks_with_clear_message(self, tmp_path, monkeypatch, ctx):
        pdf = tmp_path / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        _patch_path(monkeypatch, pdf, "Paper", "ATTKEY")
        # PyMuPDF unavailable → cannot validate ranges.
        monkeypatch.setattr(read_pdf, "_probe_total_pages", lambda _p: None)
        monkeypatch.setattr(mineru_client, "load_mineru_config", lambda: {})

        out = read_pdf.read_pdf_pages("ITEM1", 1, 1, ctx=ctx)
        assert "PyMuPDF is required" in out


class TestMineruParseWorker:
    """Tests for the background _mineru_parse_worker function.

    The worker is spawned by _try_mineru when MinerU's cache is cold. It
    calls read_cached_or_parse (which writes the MinerU cache on success) and
    reports progress via update_status. These tests exercise the worker
    directly by constructing a TaskStatus and invoking it synchronously.
    """

    def _make_status(self, work_items):
        """Build a TaskStatus with a fake task_id (no disk I/O needed)."""
        from zotero_mcp.batch_runner import TaskStatus
        return TaskStatus(
            task_id="WORKERTEST",
            task_type="mineru_parse",
            status="pending",
            created_at="now",
            total=len(work_items),
            work_items=work_items,
        )

    def test_worker_success_writes_completed_status(self, tmp_path, monkeypatch):
        """Worker: read_cached_or_parse returns ParseResult → status='completed'."""
        pdf = tmp_path / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        work_items = [{"attachment_key": "ATTKEY", "pdf_path": str(pdf), "backend": None, "item_key": "ITEM1"}]
        status = self._make_status(work_items)

        parsed = mineru_client.ParseResult(
            markdown="# full doc", pages=["p1", "p2"], source="mineru:cloud-vlm"
        )
        monkeypatch.setattr(mineru_client, "load_mineru_config", lambda: {"enabled": True, "backend": "cloud", "cloud_token": "tok"})
        monkeypatch.setattr(mineru_client, "read_cached_or_parse", lambda *a, **k: parsed)

        updates = []
        import zotero_mcp.batch_runner as _br
        monkeypatch.setattr(_br, "update_status", lambda task_id, **fields: updates.append((task_id, fields)))

        read_pdf._mineru_parse_worker(status)

        # Final update should mark the task completed with the parse source.
        completed_updates = [u for u in updates if u[1].get("status") == "completed"]
        assert len(completed_updates) == 1
        assert completed_updates[0][0] == "WORKERTEST"
        fields = completed_updates[0][1]
        assert fields["succeeded"] == 1
        assert fields["processed"] == 1
        assert "mineru:cloud-vlm" in fields["result_summary"]

    def test_worker_parse_none_writes_failed_status(self, tmp_path, monkeypatch):
        """Worker: read_cached_or_parse returns None → status='failed'."""
        pdf = tmp_path / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        work_items = [{"attachment_key": "ATTKEY", "pdf_path": str(pdf), "backend": None, "item_key": "ITEM1"}]
        status = self._make_status(work_items)

        monkeypatch.setattr(mineru_client, "load_mineru_config", lambda: {"enabled": True, "backend": "cloud", "cloud_token": "tok"})
        monkeypatch.setattr(mineru_client, "read_cached_or_parse", lambda *a, **k: None)

        updates = []
        import zotero_mcp.batch_runner as _br
        monkeypatch.setattr(_br, "update_status", lambda task_id, **fields: updates.append((task_id, fields)))

        read_pdf._mineru_parse_worker(status)

        failed_updates = [u for u in updates if u[1].get("status") == "failed"]
        assert len(failed_updates) == 1
        assert "no result" in failed_updates[0][1]["error"].lower()

    def test_worker_exception_writes_failed_status(self, tmp_path, monkeypatch):
        """Worker: read_cached_or_parse raises → status='failed' with error message."""
        pdf = tmp_path / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        work_items = [{"attachment_key": "ATTKEY", "pdf_path": str(pdf), "backend": None, "item_key": "ITEM1"}]
        status = self._make_status(work_items)

        monkeypatch.setattr(mineru_client, "load_mineru_config", lambda: {"enabled": True, "backend": "cloud", "cloud_token": "tok"})

        def _boom(*a, **k):
            raise RuntimeError("network down")

        monkeypatch.setattr(mineru_client, "read_cached_or_parse", _boom)

        updates = []
        import zotero_mcp.batch_runner as _br
        monkeypatch.setattr(_br, "update_status", lambda task_id, **fields: updates.append((task_id, fields)))

        read_pdf._mineru_parse_worker(status)

        failed_updates = [u for u in updates if u[1].get("status") == "failed"]
        assert len(failed_updates) == 1
        assert "network down" in failed_updates[0][1]["error"]

    def test_worker_empty_work_items_writes_failed(self, tmp_path, monkeypatch):
        """Worker: no work_items → status='failed' immediately."""
        status = self._make_status([])
        updates = []
        import zotero_mcp.batch_runner as _br
        monkeypatch.setattr(_br, "update_status", lambda task_id, **fields: updates.append((task_id, fields)))

        read_pdf._mineru_parse_worker(status)

        failed_updates = [u for u in updates if u[1].get("status") == "failed"]
        assert len(failed_updates) == 1
        assert "no work items" in failed_updates[0][1]["error"].lower()

    def test_worker_missing_keys_writes_failed(self, tmp_path, monkeypatch):
        """Worker: work_item missing attachment_key or pdf_path → status='failed'."""
        work_items = [{"attachment_key": None, "pdf_path": "/x.pdf", "backend": None, "item_key": "ITEM1"}]
        status = self._make_status(work_items)
        updates = []
        import zotero_mcp.batch_runner as _br
        monkeypatch.setattr(_br, "update_status", lambda task_id, **fields: updates.append((task_id, fields)))

        read_pdf._mineru_parse_worker(status)

        failed_updates = [u for u in updates if u[1].get("status") == "failed"]
        assert len(failed_updates) == 1
        assert "missing" in failed_updates[0][1]["error"].lower()
