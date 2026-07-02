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


# Loose global bounds. The floor (30 tokens) catches smelly one-liners;
# the ceiling (450 tokens) catches runaway growth. Between them, anything
# goes — small wording changes never trip the test. A rubric-complete
# description (purpose + guidelines + limitations + params + example)
# almost always lands well inside this range, so raising the floor or
# lowering the ceiling is rarely warranted.
DESC_FLOOR = 30
DESC_CEILING = 450


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
        content = f.read_text()
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
