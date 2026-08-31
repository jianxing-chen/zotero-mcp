"""Regression tests for bugs found during integration testing (2026-03-21).

Each test prevents a specific bug from reappearing.
"""

import json
import re
import time

from conftest import DummyContext, FakeZotero, _FakeResponse

from zotero_mcp import server
from zotero_mcp.batch_runner import read_status

# ---------------------------------------------------------------------------
# Bug 1: manage_collections passed [item_dict] (list) instead of item_dict
# to addto_collection, causing "list indices must be integers or slices, not str"
# ---------------------------------------------------------------------------


class TestManageCollectionsPayloadShape:
    """addto_collection receives a dict (not a list)."""

    def test_addto_receives_dict_not_list(self, monkeypatch):
        received = []

        class FakeZotManage(FakeZotero):
            def item(self, key):
                return {"key": key, "version": 1, "data": {"collections": []}}

            def collection(self, key):
                return {"key": key, "data": {"name": "Test Collection", "deleted": False}}

            def addto_collection(self, coll_key, payload, **kw):
                received.append(payload)
                return _FakeResponse(204)

        fake = FakeZotManage()
        fake._collections = [
            {"key": "COL00001", "data": {"name": "Test Collection", "parentCollection": False}},
        ]
        monkeypatch.setattr("zotero_mcp.tools._helpers._get_write_client", lambda ctx: (fake, fake))

        ctx = DummyContext()
        server.manage_collections(item_keys=["ITEM01"], add_to=["COL00001"], ctx=ctx)

        assert len(received) == 1
        # Must be a dict, NOT a list wrapping a dict
        assert isinstance(received[0], dict), f"addto_collection received {type(received[0])}, expected dict"
        assert received[0]["key"] == "ITEM01"


# ---------------------------------------------------------------------------
# Bug 2: merge_duplicates used update_item({"deleted": True}) which pyzotero
# rejects. Now uses direct PATCH with {"deleted": 1} for safe trashing.
# ---------------------------------------------------------------------------


class _FakeHttpClient:
    def __init__(self):
        self.patch_calls = []

    def patch(self, url="", headers=None, content=""):
        self.patch_calls.append({"url": url, "headers": headers, "content": content})
        return _FakeResponse(204)


class TestMergeTrashMethod:
    """Merge uses direct PATCH (not update_item) for trashing."""

    def _setup(self, monkeypatch, tmp_path=None):
        class FakeZotMerge(FakeZotero):
            def __init__(self):
                super().__init__()
                self.update_calls = []
                self.client = _FakeHttpClient()
                self.endpoint = "https://api.zotero.org"
                self.library_type = "users"
                self.library_id = "12345"

            def item(self, key):
                items = {
                    "KEEP": {
                        "key": "KEEP",
                        "version": 1,
                        "data": {
                            "title": "Keeper",
                            "itemType": "journalArticle",
                            "tags": [{"tag": "t1"}],
                            "collections": [],
                        },
                    },
                    "DUP1": {
                        "key": "DUP1",
                        "version": 2,
                        "data": {
                            "title": "Dup",
                            "itemType": "journalArticle",
                            "tags": [{"tag": "t2"}],
                            "collections": [],
                        },
                    },
                }
                return items[key]

            def children(self, key, **kw):
                return []

            def update_item(self, item, **kw):
                self.update_calls.append(item)
                item["version"] = item.get("version", 0) + 100
                return _FakeResponse(204)

            def addto_collection(self, key, payload, **kw):
                return _FakeResponse(204)

        fake = FakeZotMerge()
        monkeypatch.setattr("zotero_mcp.client.get_zotero_client", lambda: fake)
        monkeypatch.setattr("zotero_mcp.tools._helpers._get_write_client", lambda ctx: (fake, fake))
        if tmp_path is not None:
            monkeypatch.setattr("zotero_mcp.batch_runner._TASKS_DIR", tmp_path / "batch_tasks")
        return fake

    def test_trash_uses_direct_patch_not_update_item(self, monkeypatch, tmp_path):
        """Trashing must use client.patch with deleted:1, NOT update_item."""
        fake = self._setup(monkeypatch, tmp_path)
        ctx = DummyContext()

        result = server.merge_duplicates(keeper_key="KEEP", duplicate_keys=["DUP1"], confirm=True, ctx=ctx)

        # confirm=True spawns a background task
        assert "started" in result.lower() or "⏳" in result
        assert "get_batch_task_status" in result
        # Wait for the background merge to finish before checking side effects
        m = re.search(r"\*\*([^*]+)\*\*", result)
        task_id = m.group(1)
        deadline = time.time() + 5
        while time.time() < deadline:
            s = read_status(task_id)
            if s and s.status in ("completed", "failed"):
                break
            time.sleep(0.05)

        # update_item should NOT have been called with any "deleted" field
        for call in fake.update_calls:
            data = call.get("data", {})
            assert "deleted" not in data, (
                "update_item was called with 'deleted' field — this will fail "
                "with 'Invalid keys present in item'. Must use direct PATCH."
            )

        # Direct PATCH should have been called for DUP1
        assert len(fake.client.patch_calls) >= 1
        patch_contents = [json.loads(c["content"]) for c in fake.client.patch_calls]
        assert any(c.get("deleted") == 1 for c in patch_contents)


# ---------------------------------------------------------------------------
# Bug 3: find_duplicates used zot.everything() which caused
# "cannot pickle '_thread.RLock' object" in MCP contexts.
# Now uses manual pagination.
# ---------------------------------------------------------------------------


class TestFindDuplicatesNoPicle:
    """find_duplicates does NOT call everything()."""

    def test_everything_not_called(self, monkeypatch):
        class FakeZotNoPickle(FakeZotero):
            def everything(self, *args, **kwargs):
                raise RuntimeError("everything() should not be called — causes RLock pickle error")

            def items(self, **kwargs):
                return [
                    {"key": "A", "data": {"title": "Paper A", "itemType": "journalArticle", "DOI": ""}},
                    {"key": "B", "data": {"title": "Paper B", "itemType": "journalArticle", "DOI": ""}},
                ]

        fake = FakeZotNoPickle()
        monkeypatch.setattr("zotero_mcp.client.get_zotero_client", lambda: fake)

        ctx = DummyContext()
        # Should complete without hitting everything()
        result = server.find_duplicates(method="both", ctx=ctx)
        assert "Error" not in result or "No duplicates" in result


# ---------------------------------------------------------------------------
# Bug 4: PDF outline tried to import _get_storage_dir as standalone function
# and used a broken local mode path. It now downloads through
# client.download_attachment_file (local -> WebDAV -> web), which dumps via
# the client rather than reading a storage path directly (#372).
# ---------------------------------------------------------------------------


class TestPdfOutlineDownloadMethod:
    """PDF outline uses the multi-source downloader (local API → WebDAV → Web API),
    not a single `zot.dump` call that fails hard on transient `file://` URIs."""

    def test_outline_uses_multi_source_downloader(self, monkeypatch):
        import sys
        import types

        dump_called = []

        class FakeZotDump(FakeZotero):
            def children(self, key, **kw):
                return [
                    {
                        "key": "ATT01",
                        "data": {
                            "itemType": "attachment",
                            "contentType": "application/pdf",
                            "filename": "paper.pdf",
                            "parentItem": key,
                        },
                    }
                ]

            def dump(self, key, filename=None, path=None):
                dump_called.append({"key": key, "filename": filename, "path": path})
                # Create a dummy file
                import os

                if path and filename:
                    with open(os.path.join(path, filename), "wb") as f:
                        f.write(b"%PDF-1.4 fake")

        fake = FakeZotDump()
        monkeypatch.setattr("zotero_mcp.client.get_zotero_client", lambda: fake)
        monkeypatch.setattr("zotero_mcp.client.get_local_zotero_client", lambda: fake)
        monkeypatch.setattr("zotero_mcp.utils.is_local_mode", lambda: True)

        # Mock fitz
        class FakeDoc:
            def get_toc(self):
                return [[1, "Intro", 1]]

            def close(self):
                pass

        fake_fitz = types.ModuleType("fitz")
        fake_fitz.open = lambda *a, **kw: FakeDoc()
        monkeypatch.setitem(sys.modules, "fitz", fake_fitz)

        ctx = DummyContext()
        result = server.get_pdf_outline(item_key="PARENT01", ctx=ctx)

        # The multi-source downloader ultimately calls dump() on the local
        # client when one is available — verify the PDF was actually fetched.
        assert len(dump_called) == 1, (
            "download_attachment_file should reach the local dump() path"
        )
        assert "Intro" in result


# ---------------------------------------------------------------------------
# Bug 5: batch_update_tags had no tag filter parameter,
# only text search via 'q' parameter.
# ---------------------------------------------------------------------------


class TestBatchUpdateTagsFilter:
    """batch_update_tags accepts a 'tag' parameter for filtering."""

    def test_tag_parameter_exists_in_signature(self):
        import inspect

        sig = inspect.signature(server.batch_update_tags)
        assert "tag" in sig.parameters, "batch_update_tags must have a 'tag' parameter for tag-based filtering"


# ---------------------------------------------------------------------------
# Bug 6: get_pdf_outline should prefer MinerU cache over re-downloading PDF.
# Reading from the cache avoids the transient `file://` protocol bug and
# reflects the document's actual structure (publisher bookmarks can be stale).
# ---------------------------------------------------------------------------


class TestPdfOutlineMineruCachePreferred:
    """get_pdf_outline reads from the MinerU cache when available, and only
    falls back to PyMuPDF (via PDF download) when no cache exists."""

    def test_returns_mineru_cache_outline_without_download(self, monkeypatch, tmp_path):
        """When a MinerU cache exists for the attachment, the outline is
        derived from the cache and the PDF is NOT downloaded."""
        import sys
        import types

        # Set up a fake MinerU cache dir.
        cache_root = tmp_path / "mineru"
        cache_root.mkdir()
        attachment_key = "ATT01"
        cache_dir = cache_root / attachment_key
        cache_dir.mkdir()
        # fulltext.md with markdown headings.
        (cache_dir / "fulltext.md").write_text(
            "# Title\n\n"
            "## ABSTRACT\n\n"
            "body...\n\n"
            "## 1. Introduction\n\n"
            "intro body\n\n"
            "## 2. Methods\n\n"
            "methods body\n",
            encoding="utf-8",
        )
        # pages.json: page 1 contains ABSTRACT + Introduction, page 2 has Methods.
        (cache_dir / "pages.json").write_text(
            json.dumps([
                "# Title\n\n## ABSTRACT\n\nbody...\n\n## 1. Introduction\n\nintro body",
                "## 2. Methods\n\nmethods body",
            ]),
            encoding="utf-8",
        )

        # Patch the cache resolution to point at our temp dir.
        import zotero_mcp.mineru_client as mc
        monkeypatch.setattr(mc, "_resolve_cache_dir", lambda config: cache_root)
        # MinerU enabled/available so the cache path runs.
        monkeypatch.setattr(mc, "is_mineru_enabled", lambda cfg: True)

        # The Zotero client returns a single PDF child attachment.
        class FakeZot(FakeZotero):
            def children(self, key, **kw):
                return [{
                    "key": attachment_key,
                    "data": {
                        "itemType": "attachment",
                        "contentType": "application/pdf",
                        "filename": "paper.pdf",
                        "parentItem": key,
                    },
                }]

        monkeypatch.setattr("zotero_mcp.client.get_zotero_client", lambda: FakeZot())
        monkeypatch.setattr("zotero_mcp.client.get_local_zotero_client", lambda: None)
        monkeypatch.setattr("zotero_mcp.utils.is_local_mode", lambda: True)

        # Patch download_attachment_file — it should NOT be called.
        download_calls = []
        def _no_download(*a, **kw):
            download_calls.append(a)
            raise AssertionError("download_attachment_file should not be called when MinerU cache exists")
        import zotero_mcp.client as _c
        monkeypatch.setattr(_c, "download_attachment_file", _no_download)

        ctx = DummyContext()
        result = server.get_pdf_outline(item_key="PARENT01", ctx=ctx)

        assert "via MinerU cache" in result, "should note the MinerU cache source"
        assert "Introduction" in result
        assert "Methods" in result
        assert download_calls == [], "PDF should not be downloaded when cache hits"

    def test_falls_back_to_pymupdf_when_no_cache(self, monkeypatch, tmp_path):
        """When no MinerU cache exists, falls back to PyMuPDF via download."""
        import sys
        import types

        # Empty cache dir — no MinerU cache for the attachment.
        cache_root = tmp_path / "mineru"
        cache_root.mkdir()

        import zotero_mcp.mineru_client as mc
        monkeypatch.setattr(mc, "_resolve_cache_dir", lambda config: cache_root)
        monkeypatch.setattr(mc, "is_mineru_enabled", lambda cfg: True)

        attachment_key = "ATT01"
        class FakeZot(FakeZotero):
            def children(self, key, **kw):
                return [{
                    "key": attachment_key,
                    "data": {
                        "itemType": "attachment",
                        "contentType": "application/pdf",
                        "filename": "paper.pdf",
                        "parentItem": key,
                    },
                }]

            def dump(self, key, filename=None, path=None):
                import os
                if path and filename:
                    with open(os.path.join(path, filename), "wb") as f:
                        f.write(b"%PDF-1.4 fake")

        fake = FakeZot()
        monkeypatch.setattr("zotero_mcp.client.get_zotero_client", lambda: fake)
        monkeypatch.setattr("zotero_mcp.client.get_local_zotero_client", lambda: fake)
        monkeypatch.setattr("zotero_mcp.utils.is_local_mode", lambda: True)

        # The TOC is read out-of-process since #372; stub the outcome.
        from zotero_mcp.tools import write as write_tools

        monkeypatch.setattr(
            write_tools,
            "_extract_pdf_toc",
            lambda *a, **kw: write_tools.TocOutcome("ok", [[1, "Intro", 1]]),
        )

        ctx = DummyContext()
        result = server.get_pdf_outline(item_key="PARENT01", ctx=ctx)

        assert "Intro" in result
        # Should NOT have the "via MinerU cache" header — that's the fallback path.
        assert "via MinerU cache" not in result
