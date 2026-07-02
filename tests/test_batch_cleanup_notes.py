"""Tests for zotero_batch_cleanup_notes — batch-delete (trash) notes."""

from conftest import DummyContext  # noqa: I001
from zotero_mcp import server


# ---------------------------------------------------------------------------
# Fake HTTP + Zotero stubs
# ---------------------------------------------------------------------------


class FakePatchResponse:
    def __init__(self, status_code=204, text=""):
        self.status_code = status_code
        self.text = text


class FakeHttpxClient:
    """Records patch() calls; can fail on specific keys for error tests."""

    def __init__(self, status_code=204, text="", fail_keys=None):
        self._status_code = status_code
        self._text = text
        self.calls = []
        self._fail_keys = fail_keys or set()

    def patch(self, url, headers, content):
        self.calls.append({"url": url, "headers": headers, "content": content})
        # Extract key from URL for per-key failure simulation.
        key = url.rstrip("/").split("/")[-1]
        if key in self._fail_keys:
            return FakePatchResponse(500, "server error")
        return FakePatchResponse(self._status_code, self._text)


class FakeZoteroBatchCleanup:
    """Fake Zotero client for batch_cleanup_notes tests.

    Supports items() with itemType filtering + pagination, item() lookup,
    and a client.patch() that records calls.
    """

    def __init__(self, notes, patch_status=204, fail_keys=None):
        self._notes = notes
        self.endpoint = "https://api.zotero.org"
        self.library_type = "users"
        self.library_id = "12345"
        self.client = FakeHttpxClient(status_code=patch_status, fail_keys=fail_keys)

    def items(self, start=0, limit=100, **kwargs):
        # Honor itemType filtering.
        item_type = kwargs.get("itemType")
        pool = self._notes
        if item_type:
            pool = [n for n in pool if n.get("data", {}).get("itemType") == item_type]
        return pool[start : start + limit]

    def item(self, key):
        for n in self._notes:
            if n.get("key") == key:
                return n
        raise KeyError(key)


def _make_note(key, content="", parent=None, version=1):
    """Create a note item dict.

    - content="" → empty note (will match empty_only filter)
    - parent=None → standalone note (no parentItem)
    - parent="KEY" → child note attached to that item
    """
    data = {"itemType": "note", "note": content, "version": version}
    if parent:
        data["parentItem"] = parent
    return {"key": key, "version": version, "data": data}


def _patch(monkeypatch, fake):
    """Patch for non-local (web) mode."""
    monkeypatch.setattr("zotero_mcp.client.get_zotero_client", lambda: fake)
    monkeypatch.setattr("zotero_mcp.utils.is_local_mode", lambda: False)
    monkeypatch.setattr("zotero_mcp.client.get_web_zotero_client", lambda: fake)


# ---------------------------------------------------------------------------
# dry_run=True tests
# ---------------------------------------------------------------------------


def test_dry_run_returns_preview_no_deletion(monkeypatch):
    """dry_run=True should NOT call patch — only return a preview."""
    notes = [
        _make_note("N0000001"),
        _make_note("N0000002"),
    ]
    fake = FakeZoteroBatchCleanup(notes)
    _patch(monkeypatch, fake)

    result = server.batch_cleanup_notes(dry_run=True, ctx=DummyContext())

    assert "Preview" in result
    assert "Matched: 2 notes" in result
    assert fake.client.calls == []  # no deletions


def test_dry_run_shows_matched_notes(monkeypatch):
    """Preview should list matched note keys."""
    notes = [_make_note("N0000001"), _make_note("N0000002")]
    fake = FakeZoteroBatchCleanup(notes)
    _patch(monkeypatch, fake)

    result = server.batch_cleanup_notes(dry_run=True, ctx=DummyContext())

    assert "`N0000001`" in result
    assert "`N0000002`" in result
    assert "standalone" in result
    assert "empty" in result


def test_dry_run_footer_has_instruction(monkeypatch):
    """Preview should instruct user to pass dry_run=False."""
    notes = [_make_note("N0000001")]
    fake = FakeZoteroBatchCleanup(notes)
    _patch(monkeypatch, fake)

    result = server.batch_cleanup_notes(dry_run=True, ctx=DummyContext())

    assert "dry_run=False" in result
    assert "Trash" in result


# ---------------------------------------------------------------------------
# dry_run=False (execute) tests
# ---------------------------------------------------------------------------


def test_execute_trashes_matched_notes(monkeypatch):
    """dry_run=False should call patch for each matched note."""
    notes = [
        _make_note("N0000001"),
        _make_note("N0000002"),
        _make_note("N0000003"),
    ]
    fake = FakeZoteroBatchCleanup(notes)
    _patch(monkeypatch, fake)

    result = server.batch_cleanup_notes(dry_run=False, ctx=DummyContext())

    assert "Trashed: 3" in result
    assert "Failed: 0" in result
    assert len(fake.client.calls) == 3


def test_execute_trashed_notes_listed(monkeypatch):
    """Each trashed note key should appear in the output."""
    notes = [_make_note("AAA11111"), _make_note("BBB22222")]
    fake = FakeZoteroBatchCleanup(notes)
    _patch(monkeypatch, fake)

    result = server.batch_cleanup_notes(dry_run=False, ctx=DummyContext())

    assert "`AAA11111`" in result
    assert "`BBB22222`" in result


# ---------------------------------------------------------------------------
# Filter tests
# ---------------------------------------------------------------------------


def test_standalone_only_excludes_child_notes(monkeypatch):
    """standalone_only=True should exclude notes with parentItem."""
    notes = [
        _make_note("STA00001"),  # standalone
        _make_note("CHI00001", parent="PARENT01"),  # child
    ]
    fake = FakeZoteroBatchCleanup(notes)
    _patch(monkeypatch, fake)

    result = server.batch_cleanup_notes(dry_run=True, ctx=DummyContext())

    assert "Matched: 1 notes" in result
    assert "`STA00001`" in result
    assert "CHI00001" not in result


def test_standalone_only_false_includes_child_notes(monkeypatch):
    """standalone_only=False should include child notes too."""
    notes = [
        _make_note("STA00001"),
        _make_note("CHI00001", parent="PARENT01"),
    ]
    fake = FakeZoteroBatchCleanup(notes)
    _patch(monkeypatch, fake)

    result = server.batch_cleanup_notes(standalone_only=False, dry_run=True, ctx=DummyContext())

    assert "Matched: 2 notes" in result


def test_empty_only_excludes_notes_with_content(monkeypatch):
    """empty_only=True should exclude notes with non-blank content."""
    notes = [
        _make_note("EMP00001"),  # empty
        _make_note("CON00001", content="<p>This has content</p>"),
    ]
    fake = FakeZoteroBatchCleanup(notes)
    _patch(monkeypatch, fake)

    result = server.batch_cleanup_notes(dry_run=True, ctx=DummyContext())

    assert "Matched: 1 notes" in result
    assert "`EMP00001`" in result
    assert "CON00001" not in result


def test_empty_only_false_includes_content_notes(monkeypatch):
    """empty_only=False should include notes with content."""
    notes = [
        _make_note("EMP00001"),
        _make_note("CON00001", content="<p>This has content</p>"),
    ]
    fake = FakeZoteroBatchCleanup(notes)
    _patch(monkeypatch, fake)

    result = server.batch_cleanup_notes(empty_only=False, dry_run=True, ctx=DummyContext())

    assert "Matched: 2 notes" in result


def test_whitespace_only_note_matches_empty(monkeypatch):
    """Notes with only whitespace HTML should be treated as empty."""
    notes = [
        _make_note("WS000001", content="<p>   </p>"),
        _make_note("BR000001", content="<p><br></p>"),
    ]
    fake = FakeZoteroBatchCleanup(notes)
    _patch(monkeypatch, fake)

    result = server.batch_cleanup_notes(dry_run=True, ctx=DummyContext())

    assert "Matched: 2 notes" in result


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_continue_on_error(monkeypatch):
    """If one note fails to trash, others should still proceed."""
    notes = [
        _make_note("N0000001"),
        _make_note("N0000002"),
        _make_note("N0000003"),
    ]
    fake = FakeZoteroBatchCleanup(notes, fail_keys={"N0000002"})
    _patch(monkeypatch, fake)

    result = server.batch_cleanup_notes(dry_run=False, ctx=DummyContext())

    assert "Trashed: 2" in result
    assert "Failed: 1" in result
    assert "`N0000002`" in result  # appears in Failed section


def test_no_matches_returns_message(monkeypatch):
    """When nothing matches, return a clear message."""
    notes = [_make_note("CON00001", content="<p>has content</p>")]
    fake = FakeZoteroBatchCleanup(notes)
    _patch(monkeypatch, fake)

    result = server.batch_cleanup_notes(dry_run=True, ctx=DummyContext())

    assert "No notes found matching the criteria" in result
    assert fake.client.calls == []


# ---------------------------------------------------------------------------
# Limit
# ---------------------------------------------------------------------------


def test_limit_caps_processed_notes(monkeypatch):
    """limit should cap the number of notes fetched and processed."""
    notes = [_make_note(f"N{i:07d}") for i in range(5)]
    fake = FakeZoteroBatchCleanup(notes)
    _patch(monkeypatch, fake)

    result = server.batch_cleanup_notes(dry_run=False, limit=2, ctx=DummyContext())

    assert "Trashed: 2" in result
    assert len(fake.client.calls) == 2


# ---------------------------------------------------------------------------
# Local-only mode
# ---------------------------------------------------------------------------


def test_local_only_mode_returns_error(monkeypatch):
    """In local-only mode (no API key), should return an error."""
    notes = [_make_note("N0000001")]
    fake = FakeZoteroBatchCleanup(notes)

    monkeypatch.setattr("zotero_mcp.client.get_zotero_client", lambda: fake)
    monkeypatch.setattr("zotero_mcp.utils.is_local_mode", lambda: True)
    monkeypatch.setattr("zotero_mcp.client.get_web_zotero_client", lambda: None)

    result = server.batch_cleanup_notes(dry_run=False, ctx=DummyContext())

    assert "Error" in result
    assert "API" in result or "web" in result.lower()


# ---------------------------------------------------------------------------
# content_filter tests
# ---------------------------------------------------------------------------


def test_content_filter_auto_comments_matches_comment_notes(monkeypatch):
    """content_filter='auto_comments' matches notes starting with 'Comment:'."""
    notes = [
        _make_note("COM00001", content="<p>Comment: 12 pages, 6 figures. Accepted for publication in ApJ</p>"),
        _make_note("COM00002", content="<p>Comment: Accepted for publication in MNRAS</p>"),
        _make_note("HAN00001", content="<p>Pulsating WD in MSP Binary</p>"),
        _make_note("EMP00001"),  # empty
    ]
    fake = FakeZoteroBatchCleanup(notes)
    _patch(monkeypatch, fake)

    result = server.batch_cleanup_notes(
        content_filter="auto_comments", standalone_only=False, dry_run=True, ctx=DummyContext()
    )

    assert "Matched: 2 notes" in result
    assert "`COM00001`" in result
    assert "`COM00002`" in result
    # Handwritten and empty notes should NOT match.
    assert "HAN00001" not in result
    assert "EMP00001" not in result


def test_content_filter_auto_comments_skips_handwritten(monkeypatch):
    """Handwritten notes (not starting with 'Comment:') must be preserved."""
    notes = [
        _make_note("HAN00001", content="<p>About the cooling sequence of 47 Tuc</p>"),
        _make_note("HAN00002", content="<p>Need to check Eq. 15 in section 3</p>"),
        _make_note("COM00001", content="<p>Comment: 20 pages, accepted</p>"),
    ]
    fake = FakeZoteroBatchCleanup(notes)
    _patch(monkeypatch, fake)

    result = server.batch_cleanup_notes(
        content_filter="auto_comments", standalone_only=False, dry_run=True, ctx=DummyContext()
    )

    assert "Matched: 1 notes" in result
    assert "`COM00001`" in result
    assert "HAN00001" not in result
    assert "HAN00002" not in result


def test_content_filter_empty_or_auto_matches_both(monkeypatch):
    """content_filter='empty_or_auto' matches empty + Comment: notes, keeps handwritten."""
    notes = [
        _make_note("EMP00001"),  # empty
        _make_note("EMP00002", content="<p>   </p>"),  # whitespace-only
        _make_note("COM00001", content="<p>Comment: 12 pages, 6 figures</p>"),
        _make_note("COM00002", content="<p>Comment: Accepted in ApJ</p>"),
        _make_note("HAN00001", content="<p>Pulsating WD in MSP Binary</p>"),
        _make_note("HAN00002", content="<p>关于视差为负数的情况</p>"),
    ]
    fake = FakeZoteroBatchCleanup(notes)
    _patch(monkeypatch, fake)

    result = server.batch_cleanup_notes(
        content_filter="empty_or_auto", standalone_only=False, dry_run=True, ctx=DummyContext()
    )

    # 2 empty + 2 Comment: = 4 matched; 2 handwritten preserved.
    assert "Matched: 4 notes" in result
    assert "`EMP00001`" in result
    assert "`EMP00002`" in result
    assert "`COM00001`" in result
    assert "`COM00002`" in result
    assert "HAN00001" not in result
    assert "HAN00002" not in result


def test_content_filter_regex(monkeypatch):
    """A custom regex pattern should match note content."""
    notes = [
        _make_note("MAT00001", content="<p>TODO: check this reference</p>"),
        _make_note("MAT00002", content="<p>Need to verify TODO item</p>"),
        _make_note("NOM00001", content="<p>Regular note without keyword</p>"),
    ]
    fake = FakeZoteroBatchCleanup(notes)
    _patch(monkeypatch, fake)

    result = server.batch_cleanup_notes(content_filter="TODO", standalone_only=False, dry_run=True, ctx=DummyContext())

    assert "Matched: 2 notes" in result
    assert "`MAT00001`" in result
    assert "`MAT00002`" in result
    assert "NOM00001" not in result


def test_content_filter_none_uses_empty_only(monkeypatch):
    """When content_filter=None, behavior falls back to empty_only."""
    notes = [
        _make_note("EMP00001"),  # empty
        _make_note("COM00001", content="<p>Comment: accepted</p>"),
        _make_note("HAN00001", content="<p>Handwritten note</p>"),
    ]
    fake = FakeZoteroBatchCleanup(notes)
    _patch(monkeypatch, fake)

    # content_filter=None + empty_only=True → only empty notes.
    result = server.batch_cleanup_notes(content_filter=None, empty_only=True, dry_run=True, ctx=DummyContext())
    assert "Matched: 1 notes" in result
    assert "`EMP00001`" in result

    # content_filter=None + empty_only=False → all notes.
    result2 = server.batch_cleanup_notes(
        content_filter=None, empty_only=False, standalone_only=False, dry_run=True, ctx=DummyContext()
    )
    assert "Matched: 3 notes" in result2


def test_content_filter_preview_shows_note_content(monkeypatch):
    """Dry-run preview should show note content snippet for verification."""
    notes = [
        _make_note("COM00001", content="<p>Comment: 12 pages, accepted in ApJ</p>"),
        _make_note("EMP00001"),  # empty
    ]
    fake = FakeZoteroBatchCleanup(notes)
    _patch(monkeypatch, fake)

    result = server.batch_cleanup_notes(
        content_filter="empty_or_auto", standalone_only=False, dry_run=True, ctx=DummyContext()
    )

    # Preview should show content snippet and (empty) marker.
    assert "Comment: 12 pages" in result  # content preview for COM00001
    assert "(empty)" in result  # empty marker for EMP00001
    assert "Content filter: empty_or_auto" in result
