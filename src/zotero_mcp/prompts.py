"""MCP prompts: reusable research workflows over the Zotero tools.

Importing this module registers each ``@mcp.prompt`` with the FastMCP app (a
side effect, mirroring the tool modules). Prompts are surfaced by MCP hosts as
slash-commands/templates the user can invoke; each returns a plain instruction
string that steers the assistant to chain the Zotero tools in a sensible order.

These are intentionally dependency-free and never touch the Zotero API at
import time, so they load in any environment.
"""

from zotero_mcp._app import mcp


@mcp.prompt(
    name="zotero_literature_review",
    description="Run a structured literature review on a topic using the Zotero library.",
)
def literature_review(topic: str, depth: str = "standard") -> str:
    """Guide a grounded literature review on *topic*.

    Args:
        topic: The research question or subject to review.
        depth: 'quick' (library only), 'standard' (library + citation graph),
            or 'deep' (also flag coverage gaps to fetch).
    """
    steps = [
        f"Conduct a literature review on: **{topic}**.",
        "",
        "Work through these steps, citing item keys and quoting matched passages:",
        f"1. Run `zotero_semantic_search(query='{topic}', limit=12)` to find the most "
        "relevant papers already in the library. Note each paper's key and the "
        "matched passage.",
        "2. Cluster the results into themes. For each theme, name the key papers and "
        "summarize their contribution in 1-2 sentences with the supporting quote.",
    ]
    if depth in ("standard", "deep"):
        steps.append(
            "3. For the 2-3 most central papers, call "
            "`zotero_find_related_papers(identifier=<item key or DOI>, direction='both')` "
            "to surface seminal references and newer citing work. Flag any strong "
            "related papers that are NOT yet in the library."
        )
    if depth == "deep":
        steps.append(
            "4. Run `zotero_library_coverage()` (or scoped to the relevant collection) "
            "to list on-topic items missing a PDF, and offer to fetch them via "
            "`zotero_add_by_doi`."
        )
    steps += [
        "",
        "Finish with: (a) a synthesized narrative of the state of the field, "
        "(b) open questions / gaps, and (c) a short reading list of the highest-value "
        "papers (with keys). Ground every claim in a specific item.",
    ]
    return "\n".join(steps)


@mcp.prompt(
    name="zotero_synthesize_my_notes",
    description="Synthesize your own highlights and notes across a topic or collection.",
)
def synthesize_my_notes(scope: str) -> str:
    """Turn the user's annotations into a themed synthesis.

    Args:
        scope: A collection name/key, a tag, or a topic describing which notes
            to pull together.
    """
    return "\n".join(
        [
            f"Synthesize my own reading notes and highlights for: **{scope}**.",
            "",
            "1. Call `zotero_synthesize_annotations` (pass `collection_key` or `tag` if "
            f"'{scope}' names one; otherwise gather library-wide and filter to the topic). "
            "This returns a per-paper digest of my highlights and notes.",
            "2. Read the digest and identify cross-cutting THEMES — points multiple "
            "papers agree on — and TENSIONS — where my highlighted sources disagree.",
            "3. Produce a synthesis organized by theme, quoting my highlights and "
            "attributing each to its paper. Surface contradictions explicitly.",
            "4. End with the 3-5 most important takeaways and any gaps where I have no notes yet.",
            "",
            "Use only my actual annotations as evidence; do not invent claims.",
        ]
    )


@mcp.prompt(
    name="zotero_find_contradicting_evidence",
    description="Stress-test a claim by finding supporting and contradicting papers.",
)
def find_contradicting_evidence(claim: str) -> str:
    """Search the library for evidence for and against *claim*."""
    return "\n".join(
        [
            f"Stress-test this claim against my Zotero library: **{claim}**",
            "",
            "1. `zotero_semantic_search(query=<the claim>, limit=10)` — find papers directly on this topic.",
            "2. `zotero_semantic_search` again with an INVERTED / skeptical phrasing of "
            "the claim (e.g. limitations, null results, criticisms) to surface "
            "disconfirming work.",
            "3. Sort the results into SUPPORTS / CONTRADICTS / MIXED, quoting the matched "
            "passage and citing the item key for each.",
            "4. Weigh the evidence: note study quality signals where visible (sample, "
            "method, recency) and state how well-supported the claim is overall.",
            "",
            "Be even-handed — actively look for the strongest contradicting evidence, not just confirmation.",
        ]
    )


@mcp.prompt(
    name="zotero_expand_from_paper",
    description="Snowball a reading list outward from one seed paper via its citation graph.",
)
def expand_from_paper(identifier: str) -> str:
    """Grow a reading list outward from a seed paper.

    Args:
        identifier: A Zotero item key or a DOI for the seed paper.
    """
    return "\n".join(
        [
            f"Expand my reading list outward from this seed paper: **{identifier}**",
            "",
            f"1. `zotero_find_related_papers(identifier='{identifier}', direction='both', "
            "limit=25)` to get its references (foundational work) and citations "
            "(follow-ups).",
            "2. Rank the related papers by relevance to my interests and by citation "
            "count. Highlight the ones already flagged as NOT in my library.",
            "3. For the top not-in-library papers, offer to add them with "
            "`zotero_add_by_doi` (which also tries to attach an open-access PDF).",
            "4. Summarize how the seed paper sits in its citation neighborhood: what it "
            "builds on, and how later work extended or challenged it.",
        ]
    )


@mcp.prompt(
    name="zotero_read_paper",
    description="Read a paper in full and produce a structured summary with "
    "page references. Automatically detects whether a MinerU-powered "
    "vector index exists for the item — if so, uses semantic search to "
    "locate relevant chapters/sections and reads only those pages "
    "(efficient for long documents). If not, falls back to sequential "
    "page-by-page reading so nothing is missed.",
)
def read_paper(item_key: str, focus: str = "") -> str:
    """Read an entire paper and summarize it.

    Args:
        item_key: Zotero item key/ID of the paper.
        focus: optional aspect to focus on (e.g. 'methods', 'results',
            'the derivation in section 3'). Empty = full balanced summary.
    """
    focus_clause = f" Pay special attention to: {focus}." if focus else ""
    focus_query = focus or "main topic key findings methodology results"
    return "\n".join(
        [
            f"Read and summarize the paper with Zotero item key `{item_key}` — "
            f"the FULL paper, not just the first few pages.{focus_clause}",
            "",
            "Follow these steps:",
            "",
            f"1. `zotero_get_item_metadata(item_key='{item_key}')` — get the title, authors, year, DOI for context.",
            "",
            f"2. `zotero_get_pdf_outline(item_key='{item_key}')` — get the table of "
            "contents. If it returns 'no outline', proceed anyway.",
            "",
            "3. **Detect whether a MinerU-powered vector index exists** by running "
            f"`zotero_semantic_search(query='{focus_query}', limit=8)` — "
            f"if results for `{item_key}` appear with a **page number** in the "
            "Location field (e.g. 'p. 145'), the document has a full MinerU "
            "vector index. Choose your reading strategy accordingly:",
            "",
            "   **Strategy A — Vector-indexed (page numbers present in search hits):**",
            "   The document has been parsed by MinerU and fully indexed. Use "
            "semantic search to locate every major section instead of reading "
            "sequentially:",
            "   a. Run several `zotero_semantic_search` calls with different "
            "   queries to map the document's structure: e.g. "
            "'introduction and motivation', 'methodology and data', "
            "'key results and main findings', 'discussion and conclusions', "
            "'limitations and future work'.",
            f"   b. Collect the page numbers from each hit. Sort them and read "
            f"   the relevant page ranges with `zotero_read_pdf_pages"
            f"(item_key='{item_key}', start_page=N, end_page=M)` — "
            f"MinerU cache means later reads are instant.",
            "   c. If a section spans many pages, read it in ≤50-page batches. "
            "   Cross-check: have you covered every section from the outline? "
            "   If any section was missed by the semantic queries, read it "
            "   directly by page range.",
            "   This strategy is far more token-efficient than sequential "
            "reading for long documents (books, theses, long reviews).",
            "",
            "   **Strategy B — No vector index (search returns no hits for this item, or no page numbers):**",
            f"   Read sequentially in batches of up to 50 pages. Start with "
            f"`zotero_read_pdf_pages(item_key='{item_key}', start_page=1, "
            "end_page=50)`. The output header tells you the total page count. "
            "Continue in consecutive 50-page batches (51-100, 101-150) until "
            "every page is covered. Do NOT stop after the first batch unless "
            "the paper is ≤50 pages. MinerU caches the parsed PDF after the "
            "first call, so later batches are fast.",
            f"   After reading the full paper, run "
            f"`zotero_update_search_database(reindex_keys=['{item_key}'])` to "
            f"build a MinerU-powered vector index from this high-precision "
            f"Markdown — the whole PDF has been parsed, so every page becomes "
            f"searchable with accurate page numbers (增量构建, idempotent). "
            f"This lets future read_paper calls use Strategy A.",
            "",
            "4. After reading the relevant pages (via either strategy), produce a structured summary:",
            "   - **Research question & motivation**",
            "   - **Methodology** (model, data, experimental setup)",
            "   - **Key results** (cite specific numbers, formulas, figures)",
            "   - **Conclusions & limitations**",
            "   Reference page numbers for important findings (e.g. 'p. 12').",
            "",
            "If the user later asks to highlight (zotero_create_annotation) text "
            "from a MinerU-parsed page, prefer plain-text sentences — formulas "
            "($...$) and HTML tables in MinerU Markdown don't match the PDF text "
            "layer exactly, so the highlight may fail to locate them.",
            "",
            "If the paper is very long (>200 pages, e.g. a thesis or book) and "
            "Strategy A is unavailable, first use the outline to identify the "
            "most relevant chapters, and read those in full rather than every page.",
        ]
    )
