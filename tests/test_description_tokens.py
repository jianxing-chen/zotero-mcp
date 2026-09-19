"""Token-usage tests for @mcp.tool descriptions.

Guards against the two extremes of tool-description length found by
Hasan et al., *"Model Context Protocol (MCP) Tool Descriptions Are
Smelly!"* (arXiv:2602.14878): trivial one-liners (AI can't tell when or
how to call the tool) and runaway bloat (wastes context window, drowns
the signal). A single loose floor/ceiling pair is applied to EVERY tool
so the check never needs per-tool tuning — only genuine regressions to
an extreme are caught; normal wording churn passes freely.

Skipped when `tiktoken` isn't installed.
"""

import pytest

tiktoken = pytest.importorskip("tiktoken")


# Per-tool token budgets (min, max). Measured on the post-rubric-rewrite
# descriptions; min ≈ 0.67×, max ≈ 1.5× of the current value.
# If you legitimately need to exceed a max, update the budget and mention
# why in the PR — that's an active choice, not an accident.
# Fork's global bounds, kept for the coarse floor/ceiling tests below;
# TOOL_BUDGETS above carries the per-tool budgets.
DESC_FLOOR = 30
DESC_CEILING = 450

TOOL_BUDGETS = {
    # tools/annotations.py
    "zotero_get_annotations":          (110, 245),
    "zotero_get_notes":                (119, 267),
    "zotero_manage_note":              (139, 312),
    "zotero_create_annotation":        (196, 439),
    # tools/local_auth.py
    "zotero_authorize_local_writes":   (115, 260),
    "zotero_write_capabilities":       ( 70, 165),
    # tools/retrieval.py
    "zotero_get_tags":                 ( 85, 195),
    "zotero_get_item_children":        (138, 310),
    # tools/write.py
    "zotero_add_item":                 (277, 450),  # max clamped to the hard cap
    "zotero_update_item":              (190, 426),
    "zotero_batch_update":             (131, 294),
    "zotero_set_item_collections":     ( 98, 220),
    "zotero_update_collection":        ( 84, 188),
    # Both rewritten for the paging/auto-merge work (#394, #395): the auto
    # mode's two-call confirmation and keeper heuristic are things a model has
    # to know before it calls, so they belong in the description. merge's max
    # is clamped to the hard cap.
    "zotero_find_duplicates":          (234, 450),
    "zotero_merge_duplicates":         (295, 450),
    "zotero_attach_file":              (190, 430),
    # tools/search.py
    "zotero_search_items":             (175, 400),
    "zotero_search_by_tag":            (115, 265),
    "zotero_search_by_citation_key":   (125, 280),
    "zotero_advanced_search":          (175, 400),
    # filters guidance widened (single-key example, $and, no year key);
    # new baseline ~319 tokens, max clamped to the hard cap.
    "zotero_semantic_search":          (214, 450),
    "zotero_update_search_database":   (130, 295),
    "zotero_get_search_database_status": ( 75, 170),
}

# Global ceiling: even rubric-rich descriptions shouldn't exceed this.
# The paper's RQ-2 data shows diminishing returns (and AS inflation) past
# this range for compact variants.
PER_TOOL_HARD_MAX = 450


def _collect_tool_descriptions():
    """Return {tool_name: description_string} by parsing @mcp.tool blocks
    from the source files directly.

    We don't introspect the FastMCP runtime because its internal layout
    has churned across versions (no stable `tools` or `_tool_manager.tools`
    attribute). Parsing the source gives a stable, FastMCP-version-
    independent check.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src" / "zotero_mcp" / "tools"
    files = sorted(root.glob("*.py"))

    block_re = re.compile(
        r"@mcp\.tool\(\s*(.*?)\n\s*\)(?:\s*\n@[\w.]+(?:\([^)]*\))?)*\s*\n(?:async\s+)?def ", re.DOTALL
    )
    name_re = re.compile(r'name="([^"]+)"')

    descriptions: dict[str, str] = {}
    for f in files:
        # encoding is explicit: the tool descriptions contain non-ASCII, and
        # the default on Windows is cp1252, which cannot decode them.
        content = f.read_text(encoding="utf-8")
        for m in block_re.finditer(content):
            block = m.group(1)
            name_m = name_re.search(block)
            if not name_m:
                continue
            name = name_m.group(1)
            desc_idx = block.find("description=")
            if desc_idx == -1:
                continue
            desc_text = block[desc_idx + len("description=") :].strip()
            if desc_text.endswith(","):
                desc_text = desc_text[:-1].strip()
            try:
                desc_val = eval(desc_text, {"__builtins__": {}}, {})
            except Exception:
                desc_val = desc_text
            descriptions[name] = desc_val
    return descriptions


@pytest.fixture(scope="module")
def enc():
    # cl100k_base covers both GPT-4 and Claude-family tokenizers closely
    # enough for a budget check.
    return tiktoken.get_encoding("cl100k_base")


@pytest.fixture(scope="module")
def descriptions():
    descs = _collect_tool_descriptions()
    if not descs:
        pytest.skip("could not enumerate MCP tools — FastMCP internals changed?")
    return descs


class TestDescriptionBounds:
    """Every tool's description must stay inside the loose floor/ceiling.

    The floor catches smelly one-liners (AI can't tell when/how to call the
    tool); the ceiling catches runaway bloat (wastes context). Between them,
    wording churn is free — no per-tool tuning is ever needed. Only a genuine
    regression to an extreme trips the test.
    """

    def test_no_trivial_one_liners(self, enc, descriptions):
        """No tool description below the floor — purpose-only one-liners are
        the most costly smell in practice (Hasan et al. RQ-1)."""
        trivial = [
            (name, len(enc.encode(desc))) for name, desc in descriptions.items() if len(enc.encode(desc)) < DESC_FLOOR
        ]
        assert not trivial, (
            f"{len(trivial)} tool(s) below {DESC_FLOOR}-token floor (likely "
            f"smelly one-liners): {trivial}. Add params/guidelines/example."
        )

    def test_no_runaway_bloat(self, enc, descriptions):
        """No tool description above the ceiling — compact rubric-complete
        descriptions rarely need more (Hasan et al. RQ-2)."""
        over = [
            (name, len(enc.encode(desc))) for name, desc in descriptions.items() if len(enc.encode(desc)) > DESC_CEILING
        ]
        assert not over, (
            f"{len(over)} tool(s) above {DESC_CEILING}-token ceiling: {over}. "
            f"Compact the description or split guidance into the docstring."
        )
