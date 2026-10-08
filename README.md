<!-- mcp-name: io.github.54yyyu/zotero-mcp -->

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/54yyyu/zotero-mcp/main/docs/assets/logo-dark.svg">
    <img src="https://raw.githubusercontent.com/54yyyu/zotero-mcp/main/docs/assets/logo-light.svg" width="96" height="96" alt="Zotero MCP logo">
  </picture>
</p>

<h1 align="center">Zotero MCP: Chat with your Research Library in Claude, ChatGPT, and more</h1>

<p align="center">
  <b>Your Zotero library, in every AI agent.</b><br>
  Search, read, cite and annotate your papers from Claude, ChatGPT, Codex, Cursor, or a chat panel inside Zotero.
</p>

<p align="center">
  <a href="#quick-start">Install</a> ·
  <a href="#documentation">Docs</a> ·
  <a href="#zotero-agent-chat-inside-zotero">Zotero Agent</a> ·
  <a href="https://discord.gg/BvgjbcBUqg">Discord</a>
</p>

<p align="center">
  <a href="https://pypi.org/project/zotero-mcp-server/"><img src="https://img.shields.io/pypi/v/zotero-mcp-server?color=cc2936&label=PyPI" alt="PyPI version"></a>
  <a href="https://pepy.tech/projects/zotero-mcp-server"><img src="https://img.shields.io/pepy/dt/zotero-mcp-server?color=cc2936&label=downloads" alt="Downloads"></a>
  <a href="https://discord.gg/BvgjbcBUqg"><img src="https://img.shields.io/badge/Discord-join-5865F2?logo=discord&logoColor=white" alt="Discord"></a>
  <a href="https://github.com/54yyyu/zotero-mcp/blob/main/LICENSE"><img src="https://img.shields.io/github/license/54yyyu/zotero-mcp?color=555" alt="MIT license"></a>
</p>

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/54yyyu/zotero-mcp/main/docs/assets/readme/hero-dark.webp">
    <source media="(prefers-color-scheme: light)" srcset="https://raw.githubusercontent.com/54yyyu/zotero-mcp/main/docs/assets/readme/hero-light.webp">
    <img src="https://raw.githubusercontent.com/54yyyu/zotero-mcp/main/docs/assets/readme/hero-light.webp" width="900" alt="Zotero with &quot;Attention Is All You Need&quot; open on page 4. The Zotero Agent panel on the right explains why the dot products are scaled by the square root of d_k, with page citations; clicking one has highlighted the cited sentence in the PDF.">
  </picture>
  <br>
  <sub>Zotero Agent explaining a paper with page citations. A click on a citation opens the page and flashes the cited sentence.</sub>
</p>

**Zotero MCP** connects your [Zotero](https://www.zotero.org/) research library with [ChatGPT](https://openai.com), [Claude](https://www.anthropic.com/claude), and other AI assistants (e.g., [Cherry Studio](https://cherry-ai.com/), [Chorus](https://chorus.sh), [Cursor](https://www.cursor.com/)) via the [Model Context Protocol](https://modelcontextprotocol.io/introduction). Search your library, read and annotate papers, add and organize items, and find research by meaning.

> **AI agents:** read [docs/for-agents.md](https://github.com/54yyyu/zotero-mcp/blob/main/docs/for-agents.md) first. It covers which route to use, setup, and the commands in one place.

## Three ways in

One package, `zotero-mcp-server`, ships all three. Pick the one that fits where you work.

| | **MCP server** | **`zotero-cli` + agent skill** | **Zotero Agent** |
|---|---|---|---|
| **For** | Chat apps that speak MCP but have no shell: Claude Desktop, ChatGPT, Cherry Studio, Chorus | Agents with a shell: Claude Code, Codex, Cursor, Windsurf, Gemini CLI, Amp, OpenCode | Chatting inside Zotero, next to the PDF you are reading |
| **How** | `zotero-mcp setup` | `zotero-mcp install-skill` | `zotero-cli plugin`, then install the `.xpi` in Zotero |
| **Why** | Works in any MCP client | 98 tokens in context until it is needed, instead of ~13k | Knows the item, page and selection you have open; answers link to pages |
| **Guide** | [Getting started](https://github.com/54yyyu/zotero-mcp/blob/main/docs/getting-started.md) | [CLI and agent skill](https://github.com/54yyyu/zotero-mcp/blob/main/docs/cli.md) | [Zotero Agent plugin](https://github.com/54yyyu/zotero-mcp/blob/main/docs/chat-plugin.md) |

## What it does

- **Search** by title, author, tag, collection, full text, or meaning ([semantic search](https://github.com/54yyyu/zotero-mcp/blob/main/docs/semantic-search.md) with local, OpenAI, Gemini, or Ollama embeddings)
- **Read** metadata, BibTeX, full text, and page ranges of PDFs, with page images where text extraction garbles math, figures, and tables
- **Annotate**: highlights and area boxes placed on the exact words, figure, table, or equation; notes; PDF annotation extraction
- **Write**: add papers by DOI, URL, ISBN, BibTeX, or file (with open-access PDFs), manage collections and tags, merge duplicates
- **Local or web**: in local mode reads come straight from `zotero.sqlite`; writes go to the running Zotero 10+ or through the web API
- **Scite** citation tallies and retraction alerts (optional)

## Quick start

**1. Install** (Python 3.10+):

```bash
uv tool install zotero-mcp-server     # or: pip install zotero-mcp-server
```

> **New to the command line?** Try the community-built [Zotero MCP Setup](https://github.com/ehawkin/zotero-mcp-setup): a macOS GUI installer, one-click scripts for Mac and Windows, and a step-by-step guide.

**2. Enable Zotero's local API**: in Zotero 7+, open **Settings → Advanced** and tick *Allow other applications on this computer to communicate with Zotero*.

**3. Connect your assistant**:

<details>
<summary><b>Claude Desktop</b></summary>

```bash
zotero-mcp setup      # auto-configures Claude Desktop
```

or add the server by hand to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "zotero": {
      "command": "zotero-mcp",
      "env": { "ZOTERO_LOCAL": "true" }
    }
  }
}
```

</details>

<details>
<summary><b>Claude Code, Codex, Cursor and other agents with a shell</b></summary>

Teach the agent to drive `zotero-cli` (the cheaper route, see [below](#mcp-server-or-agent-skill)):

```bash
zotero-mcp install-skill
```

Or use the MCP server: for Claude Code, add the same `mcpServers` entry as above to `~/.claude.json`.

</details>

<details>
<summary><b>ChatGPT, Cherry Studio, Chorus, Autohand and other clients</b></summary>

See [Getting started](https://github.com/54yyyu/zotero-mcp/blob/main/docs/getting-started.md).

</details>

<details>
<summary><b>Zotero itself (the Zotero Agent chat panel)</b></summary>

```bash
zotero-cli plugin     # where the .xpi is, and how to install it
```

Then in Zotero: **Tools > Plugins**, the gear, **Install Plugin From File**. Details: [Zotero Agent plugin](https://github.com/54yyyu/zotero-mcp/blob/main/docs/chat-plugin.md).

</details>

**4. Writes (optional)**: on Zotero 10+, run `zotero-mcp authorize-local` once and choose **Always Allow**. On older Zotero, add `ZOTERO_API_KEY` and `ZOTERO_LIBRARY_ID` to write through the web API.

Then ask things like *"Find papers in my library on attention mechanisms"*, *"Summarize the key findings of this paper"*, or *"Highlight the main claims of this PDF"*.

### Optional extras

The base install covers search, reading, annotations, and writes. Heavier features are extras:

| Extra | What it adds | Install command |
|-------|-------------|-----------------|
| `semantic` | Semantic search via ChromaDB, sentence-transformers, OpenAI/Gemini embeddings | `pip install "zotero-mcp-server[semantic]"` |
| `pdf` | PDF outlines, page layout and page images (PyMuPDF), EPUB annotations | `pip install "zotero-mcp-server[pdf]"` |
| `scite` | [Scite](https://scite.ai) citation tallies and retraction alerts (no account needed) | `pip install "zotero-mcp-server[scite]"` |
| `all` | Everything above | `pip install "zotero-mcp-server[all]"` |

Update any time with `zotero-mcp update`.

## MCP server or agent skill?

If your agent has a shell (Claude Code, Cursor, Codex, Windsurf, Gemini CLI, Amp, OpenCode …), one command teaches it to drive `zotero-cli`:

```bash
zotero-mcp install-skill
```

An MCP server sends every tool's schema on every request, before you type anything. The skill costs 98 tokens until the agent decides it is relevant:

| Route | In context | Paid |
|---|---:|---|
| MCP server, default profile (38 tools) | **13,448** | every request |
| Agent skill, frontmatter only | **98** | always |
| Agent skill, body loaded | 1,368 | when it fires |

Use the MCP server when your client speaks MCP but has no shell (Claude Desktop, ChatGPT); use the skill when it has a shell. Both share one config. Details: [CLI and agent skill](https://github.com/54yyyu/zotero-mcp/blob/main/docs/cli.md).

## Zotero Agent: chat inside Zotero

A plugin that adds a chat panel to Zotero itself, driven by your own Claude Code, Codex or pi. It knows which item and page you have open and works your library through `zotero-cli`. Answers cite papers with real `zotero://` links, so a click opens the PDF at that page. It ships in the wheel: run `zotero-cli plugin` for the file and the install steps, or paste the one-paragraph prompt in the docs to your agent and let it do the setup. Details: [Zotero Agent plugin](https://github.com/54yyyu/zotero-mcp/blob/main/docs/chat-plugin.md).

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/54yyyu/zotero-mcp/main/docs/assets/readme/library-dark.webp">
    <source media="(prefers-color-scheme: light)" srcset="https://raw.githubusercontent.com/54yyyu/zotero-mcp/main/docs/assets/readme/library-light.webp">
    <img src="https://raw.githubusercontent.com/54yyyu/zotero-mcp/main/docs/assets/readme/library-light.webp" width="900" alt="Zotero's library view with a Deep Learning collection. The Zotero Agent panel answers which papers in the library trace how attention replaced recurrence, linking Bahdanau et al. 2015, Vaswani et al. 2017 and Devlin et al. 2019.">
  </picture>
</p>

## Documentation

| Guide | What's in it |
|---|---|
| [Getting started](https://github.com/54yyyu/zotero-mcp/blob/main/docs/getting-started.md) | Connecting Claude Desktop and Claude Code, ChatGPT, Cherry Studio, Chorus, Autohand, and other MCP clients |
| [Configuration](https://github.com/54yyyu/zotero-mcp/blob/main/docs/configuration.md) | Environment variables, local writes, web and hybrid modes, the SQLite read backend, global search, text extraction, command-line options |
| [Semantic search](https://github.com/54yyyu/zotero-mcp/blob/main/docs/semantic-search.md) | Embedding models, building and updating the index |
| [Tools](https://github.com/54yyyu/zotero-mcp/blob/main/docs/tools.md) | Every MCP tool, tool groups (`ZOTERO_MCP_TOOLSETS`), related items, PDF annotation extraction |
| [CLI and agent skill](https://github.com/54yyyu/zotero-mcp/blob/main/docs/cli.md) | `zotero-cli` command reference, `--json` output, `install-skill` |
| [Zotero Agent plugin](https://github.com/54yyyu/zotero-mcp/blob/main/docs/chat-plugin.md) | The chat panel inside Zotero: install, first run, agents and sign-in, privacy |
| [Docker](https://github.com/54yyyu/zotero-mcp/blob/main/docs/docker-images.md) | Container images and runtime modes |
| [Troubleshooting](https://github.com/54yyyu/zotero-mcp/blob/main/docs/troubleshooting.md) | Common problems and fixes |
| [For AI agents](https://github.com/54yyyu/zotero-mcp/blob/main/docs/for-agents.md) | One guide for an agent setting up or using Zotero MCP |

Website: [stevenyuyy.com/zotero-mcp](https://stevenyuyy.com/zotero-mcp/) · [Changelog](https://github.com/54yyyu/zotero-mcp/blob/main/CHANGELOG.md)

## Contributing

Issues and pull requests are welcome. Run the tests with `uv run pytest tests/`. A live integration test plan, meant to be run by Claude against a real library, is in [docs/integration-test-plan.md](https://github.com/54yyyu/zotero-mcp/blob/main/docs/integration-test-plan.md). Changes to the plugin in `plugin/` (AGPL-3.0) are accepted on the condition that the maintainer may also release them under other terms, including commercial ones.

Thanks to everyone who has contributed code, fixes, and ideas to Zotero MCP.

<p align="center">
  <a href="https://github.com/54yyyu/zotero-mcp/graphs/contributors">
    <img src="https://contrib.rocks/image?repo=54yyyu/zotero-mcp&max=120&columns=18" width="720" alt="Contributors to Zotero MCP">
  </a>
</p>

<p align="center">
  <a href="https://star-history.com/#54yyyu/zotero-mcp&Date">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=54yyyu/zotero-mcp&type=Date&theme=dark">
      <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=54yyyu/zotero-mcp&type=Date">
      <img src="https://api.star-history.com/svg?repos=54yyyu/zotero-mcp&type=Date" width="600" alt="Star history of 54yyyu/zotero-mcp">
    </picture>
  </a>
</p>

## Support

Zotero MCP is free and open source.

If it saves you or your lab time, sponsoring helps cover the unglamorous parts: Windows and WSL2 edge
cases, Zotero schema changes, group-library support, and the embedding/search infrastructure.

<a href="https://github.com/sponsors/54yyyu"><img src="https://img.shields.io/badge/Sponsor-GitHub%20Sponsors-ea4aaa?logo=githubsponsors&logoColor=white" alt="Sponsor on GitHub"></a> <a href="https://buymeacoffee.com/stevenyuyy"><img src="https://img.shields.io/badge/Buy%20Me%20a%20Coffee-ffdd00?logo=buy-me-a-coffee&logoColor=black" alt="Buy Me a Coffee"></a>

**Labs and institutions:** the $50 and $200 tiers are meant to be expensable, and include priority
triage on the issues affecting your workflow.

## License

The MCP server and `zotero-cli` are MIT ([LICENSE](https://github.com/54yyyu/zotero-mcp/blob/main/LICENSE)). The Zotero Agent plugin in `plugin/` is AGPL-3.0-or-later ([plugin/LICENSE](https://github.com/54yyyu/zotero-mcp/blob/main/plugin/LICENSE)), the license Zotero itself uses.

## 🔧 Fork additions

This fork (jianxing-chen/zotero-mcp) extends upstream with everything below; the rest of this README and the docs/ guides describe upstream behaviour.

## ⌨️ Local Mode CLI Cheat Sheet

> **Why a separate section?** In local/hybrid mode the MCP `zotero_update_search_database` tool can time out at the client layer while the server-side job keeps running and holds an in-process lock — every subsequent tool call then fails with *"Another Zotero API operation is still in progress"*. Running these commands directly in your terminal avoids that entirely: no client timeout, no lock, and you get a live progress bar.

All commands below read `client_env` from `~/.config/zotero-mcp/config.json` automatically, so you do **not** need to prefix them with `ZOTERO_LOCAL=true ZOTERO_API_KEY=...` etc.

### Check status (no lock, safe to run anytime)

```bash
# Show vector DB stats: document count, last update time, embedding model,
# MinerU cache count, and whether the DB needs updating.
zotero-mcp db-status

# Inspect indexed documents — search by title/author, show aggregate stats,
# or peek at the first chars of stored document text. Useful for verifying
# that a specific paper is actually in the vector DB.
zotero-mcp db-inspect --stats
zotero-mcp db-inspect --filter "Spergel" --show-documents
```

### Update the vector database

```bash
# Metadata-only index (title, abstract, authors, tags). Fast — no PDF text.
# Use this when you don't need full-text semantic search.
zotero-mcp update-db

# Full-text index: extracts PDF text from your local Zotero storage and
# embeds it. This is the recommended command for local/hybrid mode.
# It is slow on the first run (minutes to hours for a large library) but
# subsequent runs skip items already up to date.
zotero-mcp update-db --fulltext

# Force a complete rebuild — deletes the existing collection and re-embeds
# EVERYTHING from scratch. Use only when you changed the embedding model
# (different vector dimensions), the chunk_size/overlap, or the DB is
# corrupted. This costs the most embedding API calls.
zotero-mcp update-db --fulltext --force-rebuild

# Re-embed specific items only (e.g. after editing metadata or replacing a
# PDF). Reuses MinerU '精读' cache when available. Much faster than a full
# scan because it skips the library walk.
zotero-mcp update-db --reindex-keys ABC12345,DEF67890

# Re-embed every paper you've ever '精读'd (read via zotero_read_pdf_pages).
# Idempotent — skips items already indexed from a valid MinerU cache.
# Use --force to bypass the idempotency guard (e.g. after changing
# chunk_size/overlap or the embedding model).
zotero-mcp update-db --reindex-cached-mineru
zotero-mcp update-db --reindex-cached-mineru --force
```

### When to run which command

| Situation | Command |
|-----------|---------|
| First time enabling semantic search | `update-db --fulltext` |
| Added/removed papers in Zotero | `update-db --fulltext` (skips up-to-date items) |
| Edited a paper's metadata or replaced its PDF | `update-db --reindex-keys <KEY>` |
| Want page-level search after 精读-ing papers | `update-db --reindex-cached-mineru` |
| Changed embedding model / chunk_size | `update-db --fulltext --force-rebuild` |
| Just want to check if DB needs updating | `db-status` (no write, no lock) |
| Vector DB seems inconsistent / corrupted | `update-db --fulltext --force-rebuild` |
| Want cheap async embeddings (OpenAI only) | `update-db --openai-batch`, then `openai-batch-status` / `openai-batch-import` |

### OpenAI Batch API (async, cheaper)

If you have a large library and want to save on embedding costs, submit the
batch asynchronously and import later:

```bash
zotero-mcp update-db --fulltext --openai-batch   # submit, returns immediately
zotero-mcp openai-batch-status                    # check progress
zotero-mcp openai-batch-import                    # import completed embeddings
```

> **⚠️ Never trigger `zotero_update_search_database` from an AI assistant in local mode.** The MCP client times out (~60s) while the server-side job keeps running and holds a process-wide lock, blocking all other tools. Run `zotero-mcp update-db --fulltext` in your terminal instead — you'll see a live progress bar and the lock won't wedge.

---


## 📄 MinerU Structured PDF Reading

When enabled, the `zotero_read_pdf_pages` tool returns structured Markdown — **formulas as LaTeX, tables as HTML** — far more accurate than PyMuPDF text-layer extraction.

### Setup

```bash
zotero-mcp setup   # the wizard asks whether to configure MinerU
```

The wizard configures the `cloud` backend (the only supported one):
- **`cloud`** (recommended, only supported): MinerU cloud API (mineru.net) — highest accuracy (vlm 95+), ~15s/paper, needs `cloud_token` from <https://mineru.net/apiManage/docs>

Two `semantic_search.embedding_config` keys tune the Ollama path for slower
hardware or very large libraries:

```jsonc
"embedding_config": {
  "model_name": "bge-m3",
  "timeout": 600,            // HTTP timeout per /api/embed call (default 120s)
  "request_batch_size": 64   // documents per request (default 64)
}
```

Raise `timeout` if indexing reports `Read timed out`; lower
`request_batch_size` to make each request cover less GPU work, which usually
fixes timeouts more reliably than raising the timeout alone.

When you choose OpenAI, setup also asks whether database updates should use
OpenAI Batch API. Batch updates are cheaper for large libraries, but they are
asynchronous: submit the batch, wait for completion, then import the embeddings.

The `api` (remote FastAPI) and local CLI (`hybrid`/`pipeline`) backends are **disabled at the config layer** — their code is retained in the module for future re-enablement but `is_mineru_available` returns False for them. Any failure falls back to PyMuPDF.

**Async first-parse.** On a cold cache, `zotero_read_pdf_pages` does NOT block on the multi-minute MinerU parse — it spawns a background task and returns a `task_id`. Poll `zotero_get_batch_task_status(task_id=...)` for progress; once `completed`, call `zotero_read_pdf_pages` again to get the structured content instantly (cache hit). For immediate content without waiting, set `mineru.enabled=false` to use the PyMuPDF fallback.

**No per-call page cap.** You may request the full document (`start_page=1, end_page=N`) in one call. The 50-page-per-call limit has been removed.

```json
// mineru block in ~/.config/zotero-mcp/config.json
{
  "mineru": {
    "enabled": true,
    "backend": "cloud",
    "cloud_token": "your-mineru-net-token",
    "cloud_model": "vlm",
    "timeout": 600,
    "cache_dir": "~/.cache/zotero-mcp/mineru"
  }
}
```

### Two extraction engines, one cache

| Scenario | Engine | Reason |
|----------|--------|--------|
| Semantic search (bulk, most items) | PyMuPDF / .zotero-ft-cache | Milliseconds; embeddings tolerate garbled formulas |
| Precise reading of one paper (LLM reads formulas/tables) | MinerU | Seconds–minutes, but formulas/tables are accurate |
| **Full-document vector index for a thick book** | **MinerU cache → reindex_keys** | MinerU's per-page text (with `\f` separators) gives accurate page numbers in search results + full chunking (no 20-chunk cap) |

MinerU results are cached per attachment key (`~/.cache/zotero-mcp/mineru/<key>/`) — only `.md` and split data are stored; MinerU's other byproducts (model JSON, layout PDFs, images) are discarded. Any failure silently falls back to PyMuPDF.

### MinerU-powered vector index (for long documents)

When you've read a paper or book via `zotero_read_pdf_pages` (triggering a MinerU parse), the per-page cache (`pages.json`) is available to the semantic-search build path. Running `zotero_update_search_database(reindex_keys=["ITEM_KEY"])` builds a **full-document vector index** from the MinerU text:

- **Page numbers in search results** — MinerU's per-page text is joined with form-feed separators, so `zotero_semantic_search` results include `p. N` in the Location field. The agent can then call `zotero_read_pdf_pages(item_key, start_page=N)` to精读 that exact page.
- **No 20-chunk cap** — a 500-page book produces ~800+ chunks, making every page searchable. Items without a MinerU cache keep the default 20-chunk limit.
- **Transparent fallback** — if no MinerU cache exists for an item, `reindex_keys` falls back to pdfminer as before.
- **Idempotent** — re-running `reindex_keys` skips items already indexed from a still-valid MinerU cache (no redundant embedding cost). Use `--force` to bypass and re-embed anyway (e.g. after changing `chunk_size`/`overlap` or the embedding model).

This enables a **"embedding定位 → MinerU精读验证"** workflow: semantic search finds the relevant page, then `read_pdf_pages` returns the structured Markdown (formulas as LaTeX) for that page — all from cache, instant.

```bash
# After reading a paper once (MinerU cache created):
zotero-mcp update-db --reindex-keys ITEM_KEY        # build MinerU-powered vector index

# Index ALL papers you've 精读'd in one call (idempotent — skips ones already done):
zotero-mcp update-db --reindex-cached-mineru

# Force re-embed even items already indexed from MinerU cache:
zotero-mcp update-db --reindex-cached-mineru --force

# See how many papers are cached / indexed / pending:
zotero-mcp db-status
```

`--reindex-cached-mineru` scans the MinerU cache directory for every paper you've 精读'd, reverse-maps attachment keys to parent item keys, and re-indexes only those not yet built from MinerU — so it's safe to run repeatedly.

## 🔭 NASA ADS Astrophysics Literature

Integrate NASA ADS (Astrophysics Data System) for literature search, import, and citation-graph analysis.

### Setup

1. Get a free API token at [ui.adsabs.harvard.edu](https://ui.adsabs.harvard.edu/#user/settings/token)
2. Run `zotero-mcp setup` — the wizard prompts for the token (hidden input via getpass, prevents shell-history leaks)
3. The token is written to the `ADS_API_TOKEN` env var, propagated to the MCP server by your client

### The three tools

**`zotero_add_by_bibcode`** — import by bibcode:
```
bibcode → ADS search API → structured fields → CSL-JSON → reuse existing batch pipeline
→ bibcode stored in Extra field → PDF cascade: ADS (PUB_PDF then EPRINT_PDF) → arXiv → Unpaywall → S2 → PMC
```

**`zotero_search_ads`** — fielded search:
```
query="title:exoplanets author:\"Riess, A\"" fq="property:refereed" sort="citation_count desc"
→ results tagged ✓in library / not in library
```

**`zotero_ads_citation_network`** — citation graph:
```
identifier="2003ApJ...589L..21B" direction="both"
→ references (outgoing) + citations (incoming)
→ each tagged in-library/not, summary: "Found N references, M citations, K already in library"
```

### Fault tolerance
- Token not configured → clear error message with signup link
- ADS unreachable / rate-limited → exponential backoff retry (2s/4s/8s), reads `X-RateLimit-Reset`
- PDF paywalled → silently skipped, import still succeeds (metadata without PDF)
- All third-party PDF URLs pass through SSRF guards (rejects private/loopback/cloud-metadata hosts, re-validates each redirect hop)

### PDF download cascade

When importing a paper with a DOI (via `add_by_doi`, `add_by_bibcode`, `add_by_bibtex`, `add_by_csl_json`), the server tries these PDF sources in order and stops at the first that yields a downloadable file:

1. **ADS link_gateway** (requires `ADS_API_TOKEN`) — prefers the publisher PDF (`PUB_PDF`) when `prefer_pub_pdf=True`, falling back to the arXiv preprint (`EPRINT_PDF`). In practice the publisher PDF is usually blocked by a WAF/captcha, so ADS effectively returns the arXiv preprint.
2. **arXiv** (via CrossRef relations — always open access)
3. **Unpaywall**
4. **Semantic Scholar**
5. **PubMed Central**

All sources return only a URL; the bytes are fetched through `_download_and_attach_pdf`, which applies the same SSRF guards (private-host rejection, per-redirect re-validation) to every source.

> **Note**: Sci-Hub was globally disabled and removed from the cascade. The `scihub_client` module is kept for reference but no longer wired in. For publisher PDFs blocked by WAFs, use `zotero_upgrade_preprint_pdfs_via_browser` with a live browser session.

### Browser-Session Publisher PDF Fetcher (optional)

When `zotero_upgrade_preprint_pdfs` (or the `pub_only` HTTP cascade) is blocked by publisher WAFs/captchas/403s, the browser-session fetcher can download the publisher PDF through a **live, already-authorized Chrome/Edge DevTools session**. The browser carries the user's real session cookies and institutional authorization, so the in-page `fetch()` looks like a normal navigation and bypasses the bot detection that blocks plain `requests.get`.

**Legal boundary**: this does NOT bypass paywalls, CAPTCHA, or institutional gates. You must manually sign in and pass any verification in the browser window before calling the tool. It only fetches PDFs you are already authorized to access.

**Install** (adds `websocket-client` for the DevTools protocol):

```bash
pip install "zotero-mcp-server[browser]"
```

**Configure** — add to `~/.config/zotero-mcp/config.json`:

```json
"browser_fetch": {
  "enabled": true,
  "debug_port": 9222,
  "page_wait_seconds": 8,
  "inter_item_sleep_seconds": 6
}
```

**Launch a dedicated Chrome session** (macOS):

```bash
bash scripts/launch_chrome_remote_debug_macos.sh \
  --direct-connection \
  --disable-extensions \
  --one-shot-profile \
  --remote-debugging-port 9222 \
  --url "https://www.sciencedirect.com/"
```

In the opened Chrome window:
1. Sign in to your institutional proxy / publisher (ScienceDirect, IEEE, Wiley, etc.)
2. Pass any bot-verification page manually
3. Open one article and click "View PDF" once
4. Keep the window open

**Call the tool**:

- *"Replace arXiv PDFs with publisher versions, using my browser session when HTTP is blocked"* → `zotero_upgrade_preprint_pdfs_via_browser`

The tool tries the HTTP cascade first (ADS PUB_PDF); only when that is blocked does it fall back to the browser session. Old PDFs are trashed only after a successful download (recoverable from Zotero's Trash). Poll with `zotero_get_batch_task_status`.

Adapted from [sciencedirect-live-session-fetcher](https://github.com/Given-Dream/sciencedirect-live-session-fetcher).

### Text Extraction Settings

PDFs are parsed with [pdf-inspector](https://github.com/firecrawl/pdf-inspector), which produces Markdown with the document's heading structure intact. These keys live under `semantic_search.extraction` in `~/.config/zotero-mcp/config.json`:

```json
{
  "semantic_search": {
    "extraction": {
      "pdf_max_pages": 50,
      "fulltext_display_max_pages": 10,
      "attachment_priority": ["markdown", "pdf", "html", "other"]
    }
  }
}
```

| Key | Default | What it does |
|---|---|---|
| `pdf_max_pages` | `50` | Pages extracted per PDF when indexing. Raising it does not widen what search sees on its own — that is bounded by the embedding model's token limit or `chunking.max_chunks_per_item`. |
| `fulltext_display_max_pages` | `10` | Pages returned by `zotero_get_item_fulltext`. Separate from the above because reading a paper is bounded by your assistant's context, not by recall. |
| `attachment_priority` | `["pdf", "html", "other"]` | Order in which attachment kinds are tried when an item has several readable files. |

**`attachment_priority`** exists for the case where you have converted a paper to clean Markdown yourself and attached it next to the original PDF. By default the PDF still wins; listing `"markdown"` first makes your converted copy the one that gets read and indexed. Valid entries are `pdf`, `html`, `markdown`, `text` and `other`. `other` is a catch-all matching every kind not named elsewhere in the list, so the default sweeps Markdown and plain text into one bucket where the larger file wins. Omitting `other` means anything unlisted is never chosen.

Changing this setting marks affected items for re-extraction, so a following `zotero-mcp update-db` refreshes text that came from a now-deprioritized attachment rather than leaving stale embeddings behind.

To read one specific attachment regardless of priority, pass that attachment's own key to `zotero_get_item_fulltext` (find it with `zotero_get_item_children`) — an attachment key bypasses the priority order and reads exactly that file.


## 🧰 Tool Groups

Every tool this server registers is sent to the model on **every** request, so
the tool list is a fixed tax on your context window before you type anything.
To keep that cost proportionate, optional capabilities are grouped into
*toolsets* that you turn on when you need them.

Set `ZOTERO_MCP_TOOLSETS` to control which groups are exposed:

| Value | Effect |
|---|---|
| *(unset)* | Default profile — core tools plus `libraries`, `search-admin`, `pdf-geometry` |
| `all` | Everything (the pre-0.9 behaviour) |
| `none` | Core tools only — the smallest surface |
| `scite,feeds` | Core plus the named groups |
| `all,-scite` | Everything except the named groups |

Values are case-insensitive and may be comma- or space-separated. An unknown
group name is an error at startup rather than a silent no-op.

| Group | Default | Contents |
|---|---|---|
| `scite` | off | Scite citation tallies and retraction checks (calls scite.ai; pairs with the `[scite]` extra) |
| `duplicates` | off | Find and merge duplicate items — library maintenance |
| `discovery` | off | `find_related_papers`, `library_coverage` — corpus-level exploration |
| `feeds` | off | Zotero RSS feed subscriptions |
| `relations` | off | Explicit item-to-item "related items" links |
| `libraries` | **on** | List and switch between personal/group libraries |
| `search-admin` | **on** | Build and inspect the semantic search index |
| `pdf-geometry` | **on** | Page layout and PDF outline — pairs with area annotations |
| `chatgpt-connector` | auto | The `search`/`fetch` pair required by ChatGPT deep research |

`chatgpt-connector` is scoped by transport: it turns on automatically when the
server is served over `streamable-http` or `sse` (how ChatGPT reaches it) and
stays off for `stdio`. Name it explicitly to override either way.

Anything not listed above is **core** and always available.

**Note:** a disabled tool is genuinely absent — not merely hidden — so the
model cannot call it. If you rely on a capability, enable its group.

Example (Claude Desktop / Claude Code):

```json
"env": {
  "ZOTERO_LOCAL": "true",
  "ZOTERO_MCP_TOOLSETS": "scite,duplicates"
}
```
