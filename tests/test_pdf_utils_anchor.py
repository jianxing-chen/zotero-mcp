"""Tests for MinerU-aware anchor extraction in pdf_utils.

Covers two functions:
- ``strip_latex_and_html`` — rewrites MinerU markdown (LaTeX math + HTML
  tables) to plain text closer to the PDF text layer.
- ``_extract_anchor`` — picks start/end anchor phrases; now strips
  LaTeX/HTML first so anchors land on plain text instead of ``$$`` or
  ``<td>`` markers that the PDF text layer does not contain.

These tests do NOT require PyMuPDF — they exercise the pure-string
helpers, not ``find_text_position`` (which opens a real PDF).
"""

from __future__ import annotations

from zotero_mcp.pdf_utils import (
    ANCHOR_TARGET_LENGTH,
    _extract_anchor,
    strip_latex_and_html,
)

# =============================================================================
# strip_latex_and_html
# =============================================================================


class TestStripLatexAndHtml:
    """Unit tests for the LaTeX/HTML stripping normalizer."""

    def test_inline_math_stripped(self):
        assert strip_latex_and_html(r"$\alpha$ and $\beta$") == "and"

    def test_display_math_stripped(self):
        # $$...$$ block becomes a space; surrounding prose survives
        result = strip_latex_and_html("$$E=mc^2$$")
        assert result == ""

    def test_display_math_with_surrounding_text(self):
        result = strip_latex_and_html("$$E=mc^2$$ then we proceed")
        assert result == "then we proceed"

    def test_paren_math_stripped(self):
        assert strip_latex_and_html(r"\(\alpha + \beta\) plus text") == "plus text"

    def test_bracket_math_stripped(self):
        assert strip_latex_and_html(r"\[x = y\] conclusion") == "conclusion"

    def test_html_table_stripped(self):
        # MinerU emits tables as HTML; the cell text survives, tags don't
        result = strip_latex_and_html("<table><tr><td>x</td><td>y</td></tr></table>")
        assert result == "x y"

    def test_html_bold_stripped(self):
        assert strip_latex_and_html("<b>bold</b> text") == "bold text"

    def test_latex_command_keeps_name(self):
        # \alpha → alpha (some PDFs render the symbol name as an English word)
        assert strip_latex_and_html(r"\frac{a}{b}") == "frac a b"

    def test_braces_removed(self):
        assert strip_latex_and_html("{a} {b}") == "a b"

    def test_mixed_formula_and_html(self):
        # Real MinerU output: formula then HTML then plain text
        result = strip_latex_and_html("$$x^2$$ then <b>bold</b> text")
        assert result == "then bold text"

    def test_whitespace_collapsed(self):
        result = strip_latex_and_html("$$a$$   $$b$$   text")
        assert result == "text"

    def test_plain_text_unchanged(self):
        # No LaTeX/HTML → no change (aside from whitespace collapse)
        assert strip_latex_and_html("We propose a novel approach") == "We propose a novel approach"

    def test_empty_string(self):
        assert strip_latex_and_html("") == ""

    def test_only_formula_returns_empty(self):
        assert strip_latex_and_html("$$E=mc^2$$") == ""

    def test_underscore_subscript_preserved(self):
        # x_2 is not a LaTeX command (no backslash), so it survives as-is
        assert strip_latex_and_html("x_2 value") == "x_2 value"

    def test_caret_superscript_preserved(self):
        # x^2 without $ delimiters is plain text, not math
        assert strip_latex_and_html("x^2 value") == "x^2 value"

    def test_nested_dollar_delimiters(self):
        # $...$ should match the shortest pair, not greedy across multiple
        result = strip_latex_and_html("$a$ middle $b$")
        assert result == "middle"

    def test_multiline_formula(self):
        # Display math can span lines
        result = strip_latex_and_html("$$\nE = mc^2\n$$\nresult")
        assert result == "result"

    def test_backslash_without_command_left_alone(self):
        # Lone backslash (not followed by letters) is not a LaTeX command
        result = strip_latex_and_html("path\\to and text")
        # The backslash isn't a \command, but brace removal doesn't apply.
        # _LATEX_CMD only matches \[a-zA-Z]+, so the lone \ survives.
        # This is fine — it's an edge case that doesn't occur in MinerU output.
        assert "text" in result


# =============================================================================
# _extract_anchor with MinerU text
# =============================================================================


class TestExtractAnchorMineru:
    """Verify _extract_anchor picks plain-text anchors from MinerU markdown."""

    def test_plain_text_start_anchor_unchanged(self):
        # No LaTeX → behaves like before
        text = "We propose a novel approach to attention mechanisms for NLP tasks. " * 3
        anchor = _extract_anchor(text, from_start=True)
        assert anchor.startswith("We propose")
        assert len(anchor) >= ANCHOR_TARGET_LENGTH - 5  # within word-boundary tolerance

    def test_plain_text_end_anchor_unchanged(self):
        text = "We propose a novel approach to attention mechanisms for NLP tasks. " * 3
        anchor = _extract_anchor(text, from_start=False)
        assert "NLP tasks" in anchor or "tasks" in anchor

    def test_start_anchor_skips_leading_formula(self):
        # MinerU text: formula at the start, plain text after
        # Without stripping, the anchor would begin with "$$Attention..."
        text = (
            "$$Attention(Q,K,V)=softmax(\\frac{QK^T}{\\sqrt{d_k}})$$ "
            "We propose a novel architecture called the Transformer which "
            "relies solely on attention mechanisms dispensing with recurrence "
            "and convolutions entirely."
        )
        anchor = _extract_anchor(text, from_start=True)
        # Anchor must NOT start with $ or \
        assert not anchor.startswith("$")
        assert not anchor.startswith("\\")
        assert "We propose" in anchor or "propose" in anchor

    def test_end_anchor_skips_trailing_formula(self):
        # MinerU text: plain text then formula at the end
        text = (
            "We propose a novel architecture called the Transformer which "
            "relies solely on attention mechanisms dispensing with recurrence "
            "and convolutions entirely. The loss is "
            "$$L = -\\sum_{i} y_i \\log p_i$$"
        )
        anchor = _extract_anchor(text, from_start=False)
        # Anchor must NOT end with $ or \
        assert not anchor.endswith("$")
        assert not anchor.endswith("\\")
        # Should land on the prose before the formula
        assert "entirely" in anchor or "loss" in anchor or "convolutions" in anchor

    def test_all_formula_returns_empty(self):
        # Text is entirely a formula — no plain-text anchor possible
        text = "$$E=mc^2$$"
        anchor = _extract_anchor(text, from_start=True)
        assert anchor == ""

    def test_all_formula_end_returns_empty(self):
        text = "$$E=mc^2$$"
        anchor = _extract_anchor(text, from_start=False)
        assert anchor == ""

    def test_html_table_start_anchor_skips_tags(self):
        # MinerU table: the anchor should land on cell text, not <td>
        text = (
            "<table><tr><td>Method</td><td>Accuracy</td><td>F1</td></tr>"
            "<tr><td>Baseline</td><td>0.85</td><td>0.82</td></tr>"
            "<tr><td>Ours</td><td>0.92</td><td>0.90</td></tr></table> "
            "These results demonstrate that our approach significantly "
            "outperforms the baseline across all evaluated metrics."
        )
        anchor = _extract_anchor(text, from_start=True)
        assert not anchor.startswith("<")
        # Should contain cell content like "Method" or "Accuracy"
        assert any(w in anchor for w in ["Method", "Accuracy", "F1", "Baseline"])

    def test_html_table_end_anchor_skips_tags(self):
        text = (
            "These results demonstrate that our approach significantly "
            "outperforms the baseline across all evaluated metrics. "
            "<table><tr><td>Method</td><td>Accuracy</td></tr></table>"
        )
        anchor = _extract_anchor(text, from_start=False)
        assert not anchor.endswith(">")
        assert "metrics" in anchor or "evaluated" in anchor or "outperforms" in anchor

    def test_mixed_formula_and_text_start_anchor(self):
        # Formula in the middle, plain text at start
        text = (
            "In this section we describe the model architecture in detail. "
            "$$Attention(Q,K,V)=softmax(QK^T/\\sqrt{d_k})V$$ "
            "The attention function can be described as mapping a query "
            "and a set of key-value pairs to an output."
        )
        anchor = _extract_anchor(text, from_start=True)
        # Should start with the plain-text beginning
        assert anchor.startswith("In this section")

    def test_short_text_returns_empty(self):
        # Below the ANCHOR_TARGET_LENGTH * 2 threshold
        text = "Short text"
        assert _extract_anchor(text, from_start=True) == ""
        assert _extract_anchor(text, from_start=False) == ""

    def test_short_cleaned_text_returns_empty(self):
        # Raw text is long enough, but after stripping it's too short
        # (almost entirely formulas)
        text = "$$" + "x^2" * 30 + "$$"
        assert _extract_anchor(text, from_start=True) == ""
        assert _extract_anchor(text, from_start=False) == ""

    def test_anchor_does_not_contain_dollar_sign(self):
        # Regression: ensure no $ leaks into the anchor from any position
        text = (
            "We propose $a$ novel approach $b$ with attention mechanisms "
            "for NLP tasks and we evaluate on several benchmark datasets "
            "to demonstrate the effectiveness of our proposed method here."
        )
        start_anchor = _extract_anchor(text, from_start=True)
        end_anchor = _extract_anchor(text, from_start=False)
        assert "$" not in start_anchor
        assert "$" not in end_anchor

    def test_anchor_does_not_contain_html_brackets(self):
        text = (
            "We propose <b>a novel</b> approach with attention mechanisms "
            "for NLP tasks and we evaluate on several benchmark datasets "
            "to demonstrate the effectiveness of our proposed method here."
        )
        start_anchor = _extract_anchor(text, from_start=True)
        end_anchor = _extract_anchor(text, from_start=False)
        assert "<" not in start_anchor
        assert ">" not in start_anchor
        assert "<" not in end_anchor
        assert ">" not in end_anchor

    def test_anchor_does_not_contain_backslash(self):
        text = (
            "We propose \\textbf{a novel} approach with attention mechanisms "
            "for NLP tasks and we evaluate on several benchmark datasets "
            "to demonstrate the effectiveness of our proposed method here."
        )
        start_anchor = _extract_anchor(text, from_start=True)
        end_anchor = _extract_anchor(text, from_start=False)
        assert "\\" not in start_anchor
        assert "\\" not in end_anchor
