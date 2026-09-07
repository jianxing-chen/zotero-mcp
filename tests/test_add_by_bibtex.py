"""Integration tests for the zotero_add_by_bibtex MCP tool."""

import re
import time

from conftest import FakeZotero

from zotero_mcp import server
from zotero_mcp.batch_runner import read_status


def _wait_for_task(result):
    """Extract task_id from a background-task return string and wait for completion.

    Returns the final TaskStatus (or None if the result isn't a task-start string).
    """
    m = re.search(r"\*\*([^*]+)\*\*", result)
    if not m:
        return None
    task_id = m.group(1)
    deadline = time.time() + 10
    while time.time() < deadline:
        s = read_status(task_id)
        if s and s.status in ("completed", "failed"):
            return s
        time.sleep(0.05)
    return read_status(task_id)


class FakeZoteroWithAttach(FakeZotero):
    """FakeZotero + attachment_both stub (for OA PDF attempts)."""

    def __init__(self):
        super().__init__()
        self.attachments = []

    def attachment_both(self, files, parentid=None, **kwargs):
        self.attachments.append({"files": files, "parentid": parentid})
        return {"success": {"0": "ATCH0001"}, "successful": {}, "failed": {}}


def _patch_hybrid(monkeypatch):
    """Install a write-capable FakeZotero; return it."""
    fake = FakeZoteroWithAttach()
    monkeypatch.setattr(
        "zotero_mcp.tools._helpers._get_write_client",
        lambda ctx: (fake, fake),
    )
    return fake


def _disable_oa_pdf(monkeypatch):
    """Disable network lookups in _try_attach_oa_pdf for tests that create DOIs."""
    monkeypatch.setattr(
        "zotero_mcp.tools._helpers._try_attach_oa_pdf",
        lambda *args, **kwargs: "no open-access PDF found (stubbed)",
    )


# ---------------------------------------------------------------------------
# Happy path: single entry
# ---------------------------------------------------------------------------


class TestSingleEntry:
    def test_creates_journal_article(self, monkeypatch, dummy_ctx, tmp_path):
        monkeypatch.setattr(
            "zotero_mcp.batch_runner._TASKS_DIR", tmp_path / "batch_tasks"
        )
        fake = _patch_hybrid(monkeypatch)
        _disable_oa_pdf(monkeypatch)

        bib = """
        @article{smith2020,
          title={Hello},
          author={Smith, John},
          journal={Nature},
          year={2020},
          doi={10.1234/x},
        }
        """
        result = server.add_by_bibtex(bibtex=bib, ctx=dummy_ctx)

        assert "started" in result.lower() or "⏳" in result
        assert "get_batch_task_status" in result
        _wait_for_task(result)

        assert len(fake.created) == 1
        created = fake.created[0]
        assert created["itemType"] == "journalArticle"
        assert created["title"] == "Hello"
        assert created["publicationTitle"] == "Nature"
        assert created["DOI"] == "10.1234/x"
        assert "Citation Key: smith2020" in created["extra"]

    def test_citekey_preserved_in_extra(self, monkeypatch, dummy_ctx, tmp_path):
        monkeypatch.setattr(
            "zotero_mcp.batch_runner._TASKS_DIR", tmp_path / "batch_tasks"
        )
        fake = _patch_hybrid(monkeypatch)
        _disable_oa_pdf(monkeypatch)
        bib = "@book{MyCite2021, title={B}, author={A, B}, publisher={P}, year=2021}"

        result = server.add_by_bibtex(bibtex=bib, ctx=dummy_ctx)
        _wait_for_task(result)

        assert "Citation Key: MyCite2021" in fake.created[0]["extra"]


# ---------------------------------------------------------------------------
# Multiple entries
# ---------------------------------------------------------------------------


class TestMultipleEntries:
    def test_creates_multiple_items(self, monkeypatch, dummy_ctx, tmp_path):
        monkeypatch.setattr(
            "zotero_mcp.batch_runner._TASKS_DIR", tmp_path / "batch_tasks"
        )
        fake = _patch_hybrid(monkeypatch)
        _disable_oa_pdf(monkeypatch)

        bib = """
        @article{a, title={A}, author={X, Y}, year={2020}, doi={10.1234/a}}
        @book{b, title={B}, author={P, Q}, publisher={Pub}, year={2021}}
        """
        result = server.add_by_bibtex(bibtex=bib, ctx=dummy_ctx)

        assert "started" in result.lower() or "⏳" in result
        assert "get_batch_task_status" in result
        _wait_for_task(result)

        assert len(fake.created) == 2
        assert fake.created[0]["itemType"] == "journalArticle"
        assert fake.created[1]["itemType"] == "book"


# ---------------------------------------------------------------------------
# Caller tags and collections are merged with source tags
# ---------------------------------------------------------------------------


class TestTagsAndCollections:
    def test_caller_tags_merged_with_source_keywords(self, monkeypatch, dummy_ctx, tmp_path):
        monkeypatch.setattr(
            "zotero_mcp.batch_runner._TASKS_DIR", tmp_path / "batch_tasks"
        )
        fake = _patch_hybrid(monkeypatch)
        _disable_oa_pdf(monkeypatch)

        bib = """
        @article{x, title={T}, author={A, B}, year={2020},
          keywords={source1, source2}}
        """
        result = server.add_by_bibtex(bibtex=bib, tags=["caller1", "source1"], ctx=dummy_ctx)
        _wait_for_task(result)

        tags = [t["tag"] for t in fake.created[0]["tags"]]
        # source1 should only appear once (case-insensitive dedup)
        assert tags == ["source1", "source2", "caller1"]

    def test_collections_applied(self, monkeypatch, dummy_ctx, tmp_path):
        monkeypatch.setattr(
            "zotero_mcp.batch_runner._TASKS_DIR", tmp_path / "batch_tasks"
        )
        fake = _patch_hybrid(monkeypatch)
        fake._collections = [
            {"key": "COL00001", "data": {"name": "One", "parentCollection": False}},
            {"key": "COL00002", "data": {"name": "Two", "parentCollection": False}},
        ]
        _disable_oa_pdf(monkeypatch)

        bib = "@article{x, title={T}, author={A, B}, year={2020}}"
        result = server.add_by_bibtex(
            bibtex=bib,
            collections=["COL00001", "COL00002"],
            ctx=dummy_ctx,
        )
        _wait_for_task(result)

        assert fake.created[0]["collections"] == ["COL00001", "COL00002"]

    def test_collection_names_resolved(self, monkeypatch, dummy_ctx, tmp_path):
        """Collection names resolve to keys once, before the entry loop."""
        monkeypatch.setattr(
            "zotero_mcp.batch_runner._TASKS_DIR", tmp_path / "batch_tasks"
        )
        fake = _patch_hybrid(monkeypatch)
        fake._collections = [
            {"key": "COL00001", "data": {"name": "Reading List", "parentCollection": False}},
        ]
        _disable_oa_pdf(monkeypatch)

        bib = "@article{x, title={T}, author={A, B}, year={2020}}"
        result = server.add_by_bibtex(
            bibtex=bib,
            collections=["reading list"],
            ctx=dummy_ctx,
        )
        _wait_for_task(result)

        assert fake.created[0]["collections"] == ["COL00001"]


# ---------------------------------------------------------------------------
# DOI triggers OA PDF attempt; no DOI skips it
# ---------------------------------------------------------------------------


class TestOaPdfAttempt:
    def test_doi_triggers_oa_attempt(self, monkeypatch, dummy_ctx, tmp_path):
        monkeypatch.setattr(
            "zotero_mcp.batch_runner._TASKS_DIR", tmp_path / "batch_tasks"
        )
        _patch_hybrid(monkeypatch)
        called = {"count": 0, "doi": None}

        def stub(write_zot, item_key, doi, ctx, **kwargs):
            called["count"] += 1
            called["doi"] = doi
            return "stubbed attempt"

        monkeypatch.setattr("zotero_mcp.tools._helpers._try_attach_oa_pdf", stub)

        bib = "@article{a, title={T}, author={A, B}, year=2020, doi={10.1234/x}}"
        result = server.add_by_bibtex(bibtex=bib, ctx=dummy_ctx)
        _wait_for_task(result)

        assert called["count"] == 1
        assert called["doi"] == "10.1234/x"

    def test_no_doi_no_oa_attempt(self, monkeypatch, dummy_ctx, tmp_path):
        monkeypatch.setattr(
            "zotero_mcp.batch_runner._TASKS_DIR", tmp_path / "batch_tasks"
        )
        fake = _patch_hybrid(monkeypatch)
        called = {"count": 0}

        def stub(*args, **kwargs):
            called["count"] += 1
            return "stubbed"

        monkeypatch.setattr("zotero_mcp.tools._helpers._try_attach_oa_pdf", stub)

        bib = "@book{b, title={T}, author={A, B}, publisher={P}, year=2020}"
        result = server.add_by_bibtex(bibtex=bib, ctx=dummy_ctx)
        # Wait for the worker: leaving it running lets a slow thread resolve
        # the NEXT test's monkeypatched client and create into its fake.
        _wait_for_task(result)

        assert called["count"] == 0
        assert len(fake.created) == 1


# ---------------------------------------------------------------------------
# file_path ingestion
# ---------------------------------------------------------------------------


class TestFilePath:
    def test_reads_bib_file(self, monkeypatch, dummy_ctx, tmp_path):
        monkeypatch.setattr(
            "zotero_mcp.batch_runner._TASKS_DIR", tmp_path / "batch_tasks"
        )
        fake = _patch_hybrid(monkeypatch)
        _disable_oa_pdf(monkeypatch)

        bib_file = tmp_path / "refs.bib"
        bib_file.write_text(
            "@article{a, title={T}, author={A, B}, year=2020, doi={10.1234/x}}",
            encoding="utf-8",
        )

        result = server.add_by_bibtex(file_path=str(bib_file), ctx=dummy_ctx)

        assert "started" in result.lower() or "⏳" in result
        assert "get_batch_task_status" in result
        _wait_for_task(result)

        assert len(fake.created) == 1
        assert fake.created[0]["title"] == "T"

    def test_reads_bibtex_extension(self, monkeypatch, dummy_ctx, tmp_path):
        monkeypatch.setattr(
            "zotero_mcp.batch_runner._TASKS_DIR", tmp_path / "batch_tasks"
        )
        fake = _patch_hybrid(monkeypatch)
        _disable_oa_pdf(monkeypatch)

        f = tmp_path / "refs.bibtex"
        f.write_text("@book{b, title={B}, author={P, Q}, publisher={Pub}, year=2020}", encoding="utf-8")

        result = server.add_by_bibtex(file_path=str(f), ctx=dummy_ctx)
        _wait_for_task(result)
        assert len(fake.created) == 1

    def test_rejects_wrong_extension(self, monkeypatch, dummy_ctx, tmp_path):
        _patch_hybrid(monkeypatch)
        f = tmp_path / "refs.txt"
        f.write_text("@article{a, title={T}, year=2020}", encoding="utf-8")

        result = server.add_by_bibtex(file_path=str(f), ctx=dummy_ctx)
        assert "Unsupported file extension" in result

    def test_rejects_missing_file(self, monkeypatch, dummy_ctx):
        _patch_hybrid(monkeypatch)
        result = server.add_by_bibtex(
            file_path="/absolutely/no/such/file.bib",
            ctx=dummy_ctx,
        )
        assert "not found" in result.lower()

    def test_rejects_relative_path(self, monkeypatch, dummy_ctx):
        _patch_hybrid(monkeypatch)
        result = server.add_by_bibtex(file_path="refs.bib", ctx=dummy_ctx)
        assert "absolute" in result.lower()

    def test_rejects_symlink(self, monkeypatch, dummy_ctx, tmp_path):
        _patch_hybrid(monkeypatch)
        target = tmp_path / "real.bib"
        target.write_text("@article{a, title={T}, year=2020}", encoding="utf-8")
        link = tmp_path / "linked.bib"
        link.symlink_to(target)

        result = server.add_by_bibtex(file_path=str(link), ctx=dummy_ctx)
        assert "symlink" in result.lower()


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


class TestErrorPaths:
    def test_empty_bibtex(self, monkeypatch, dummy_ctx):
        _patch_hybrid(monkeypatch)
        result = server.add_by_bibtex(bibtex="", ctx=dummy_ctx)
        assert "Must provide" in result

    def test_neither_bibtex_nor_file_path(self, monkeypatch, dummy_ctx):
        _patch_hybrid(monkeypatch)
        result = server.add_by_bibtex(ctx=dummy_ctx)
        assert "Must provide" in result

    def test_both_bibtex_and_file_path_rejected(self, monkeypatch, dummy_ctx):
        _patch_hybrid(monkeypatch)
        result = server.add_by_bibtex(
            bibtex="@a{x}",
            file_path="/tmp/x.bib",
            ctx=dummy_ctx,
        )
        assert "not both" in result

    def test_no_valid_entries(self, monkeypatch, dummy_ctx):
        _patch_hybrid(monkeypatch)
        result = server.add_by_bibtex(bibtex="this is not bibtex", ctx=dummy_ctx)
        assert "No valid @entries" in result

    def test_local_only_mode_rejected(self, monkeypatch, dummy_ctx):
        def raise_local(ctx):
            raise ValueError("Cannot perform write operations in local-only mode.")

        monkeypatch.setattr("zotero_mcp.tools._helpers._get_write_client", raise_local)
        result = server.add_by_bibtex(bibtex="@a{x, title=T}", ctx=dummy_ctx)
        assert "local-only" in result.lower()

    def test_partial_failure_continues(self, monkeypatch, dummy_ctx, tmp_path):
        """If one entry fails conversion, others should still be created."""
        monkeypatch.setattr(
            "zotero_mcp.batch_runner._TASKS_DIR", tmp_path / "batch_tasks"
        )
        fake = _patch_hybrid(monkeypatch)
        _disable_oa_pdf(monkeypatch)

        # Monkeypatch create_items to fail on the second call
        call_count = {"n": 0}
        original_create = fake.create_items

        def flaky_create(items, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise RuntimeError("simulated write failure")
            return original_create(items, **kwargs)

        fake.create_items = flaky_create

        bib = """
        @article{a, title={A}, author={X, Y}, year={2020}}
        @article{b, title={B}, author={P, Q}, year={2020}}
        @article{c, title={C}, author={R, S}, year={2020}}
        """
        result = server.add_by_bibtex(bibtex=bib, ctx=dummy_ctx)

        assert "started" in result.lower() or "⏳" in result
        assert "get_batch_task_status" in result
        s = _wait_for_task(result)
        assert s is not None

        # 2 succeeded, 1 failed (simulated write failure on the second create).
        assert s.succeeded == 2
        assert s.failed == 1
        assert s.result_summary is not None
        assert "simulated write failure" in str(s.failed_items) or any(
            "simulated write failure" in (it.get("detail") or "") for it in s.failed_items
        )
