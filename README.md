# Zotero MCP: Chat with your Research Library—Local or Web—in Claude, ChatGPT, and more.

<p align="center">
  <a href="https://www.zotero.org/">
    <img src="https://img.shields.io/badge/Zotero-CC2936?style=for-the-badge&logo=zotero&logoColor=white" alt="Zotero">
  </a>
  <a href="https://www.anthropic.com/claude">
    <img src="https://img.shields.io/badge/Claude-6849C3?style=for-the-badge&logo=anthropic&logoColor=white" alt="Claude">
  </a>
  <a href="https://chatgpt.com/">
    <img src="https://img.shields.io/badge/ChatGPT-74AA9C?style=for-the-badge&logo=openai&logoColor=white" alt="ChatGPT">
  </a>
  <a href="https://modelcontextprotocol.io/introduction">
    <img src="https://img.shields.io/badge/MCP-0175C2?style=for-the-badge&logoColor=white" alt="MCP">
  </a>
  <a href="https://pypi.org/project/zotero-mcp-server/">
    <img src="https://img.shields.io/pypi/v/zotero-mcp-server?style=for-the-badge&logo=pypi&logoColor=white" alt="PyPI">
  </a>
  <a href="https://discord.gg/BvgjbcBUqg">
    <img src="https://img.shields.io/badge/Discord-5865F2?style=for-the-badge&logo=discord&logoColor=white" alt="Discord">
  </a>
</p>

**Zotero MCP** seamlessly connects your [Zotero](https://www.zotero.org/) research library with [ChatGPT](https://openai.com), [Claude](https://www.anthropic.com/claude), and other AI assistants (e.g., [Cherry Studio](https://cherry-ai.com/), [Chorus](https://chorus.sh), [Cursor](https://www.cursor.com/)) via the [Model Context Protocol](https://modelcontextprotocol.io/introduction). Review papers, get summaries, analyze citations, extract PDF annotations, import astrophysics literature, and more!

> This fork adds **MinerU structured PDF reading** and **NASA ADS literature integration** on top of the original.

[中文文档 (Chinese README)](./README.zh-CN.md)

---

## ✨ Features

### 🧠 AI-Powered Semantic Search
- **Vector-based similarity search** over your entire research library (requires `[semantic]` extra)
- **Multiple embedding models**: Default (free, local), OpenAI, Gemini, and Ollama
- **Intelligent results** with similarity scores and contextual matching
- **Auto-updating database** with configurable sync schedules

### 🔍 Search Your Library
- Find papers, articles, and books by title, author, or content
- Perform complex searches with multiple criteria
- Browse collections, tags, and recent additions
- Semantic search for conceptual and topic-based discovery

### 📚 Access Your Content
- Retrieve detailed metadata for any item (markdown, BibTeX, or raw JSON export)
- Get full text content (when available)
- Look up items by BetterBibTeX citation key

### 📄 MinerU Structured PDF Reading (new `[mineru]` extra)
- **Extract formulas as LaTeX and tables as HTML** — impossible with plain PyMuPDF text-layer extraction
- When an LLM calls `zotero_read_pdf_pages` to read a paper, MinerU returns correct formulas like `$\text{Attention}(Q,K,V)=\text{softmax}(\frac{QK^T}{\sqrt{d_k}})V$`
- **Split by use case**: semantic search (bulk analysis) keeps PyMuPDF's millisecond speed; only precise single-paper reading uses MinerU
- Three backends: `api` (remote service, zero local deps), `hybrid` (local GPU), `pipeline` (local CPU fallback) — hybrid auto-degrades to pipeline on OOM
- Results cached per attachment key to avoid re-parsing
- Any failure silently falls back to PyMuPDF — no MinerU installed = 100% original behavior

### 🔭 NASA ADS Astrophysics Literature (new)
- **`zotero_add_by_bibcode`**: import papers by bibcode — fetches ADS metadata, converts to a Zotero item, stores bibcode in the Extra field for dedup, and attempts OA PDF download (Unpaywall cascade + ADS link_gateway fallback)
- **`zotero_search_ads`**: fielded ADS search (e.g. `title:exoplanets`, `author:"Riess, A"`) with results tagged "in library ✓" / "not in library" (DOI + bibcode dual match)
- **`zotero_ads_citation_network`**: citation graph analysis — references (papers it cites) and citations (papers citing it), flagging high-impact gaps in your library
- Free token ([get one here](https://ui.adsabs.harvard.edu/#user/settings/token)), configured via `ADS_API_TOKEN` env var

### 📝 Work with Annotations
- Extract and search PDF annotations with page numbers
- Access Zotero's native annotations
- Create and update notes and annotations
- Extract PDF table of contents / outlines (requires `[pdf]` extra)

### ✏️ Write Operations
- **Add papers by DOI** with auto-fetched metadata and open-access PDF cascade (Unpaywall, arXiv, Semantic Scholar, PMC)
- **Add papers by URL** (arXiv, DOI links, generic webpages) or from local files
- Create and manage collections, update item metadata, batch-update tags
- Find and merge duplicate items with dry-run preview
- **Hybrid mode**: local reads + web API writes for local-mode users

### 📊 Scite Citation Intelligence (optional `[scite]` extra)
- **Citation tallies**: See how many papers support, contrast, or mention each item — the MCP version of the [Scite Zotero Plugin](https://github.com/scitedotai/scite-zotero-plugin)
- **Retraction alerts**: Scan your library for retracted or corrected papers
- No Scite account required — uses public API endpoints

### 🌐 Flexible Access Methods
- Local mode for offline access (no API key needed)
- Web API for cloud library access
- Hybrid mode: read from local Zotero, write via web API
- Direct WebDAV attachment downloads (e.g. for Nutstore/坚果云 sync)

### ⌨️ Standalone CLI (`zotero-cli`)
- Search, browse, and edit your library directly from the terminal — no AI assistant required
- Ideal for scripting, automation, and quick lookups
- Short aliases (`s`, `g`, `ann`, `coll`) for interactive use

## 🚀 Quick Install

> **New to the command line?** Try the community-built [Zotero MCP Setup](https://github.com/ehawkin/zotero-mcp-setup) — includes a macOS GUI installer (DMG), one-click install scripts for Mac/Windows, and a step-by-step guide. No Terminal experience needed.

### Default Installation (core tools only)

The base install is lightweight — it includes search, metadata retrieval, annotations, and write operations. No ML/AI dependencies are pulled in.

```bash
# uv (recommended)
uv tool install zotero-mcp-server
zotero-mcp setup

# or pip
pip install zotero-mcp-server
zotero-mcp setup

# or pipx
pipx install zotero-mcp-server
zotero-mcp setup
```

### Optional Extras

Heavy ML/PDF dependencies are separated into optional extras so the base install stays fast and small:

| Extra | What it adds | Install command |
|-------|-------------|-----------------|
| `semantic` | Semantic search via ChromaDB, sentence-transformers, OpenAI/Gemini embeddings | `pip install "zotero-mcp-server[semantic]"` |
| `pdf` | PDF outline extraction (PyMuPDF) and EPUB annotation support | `pip install "zotero-mcp-server[pdf]"` |
| `mineru` | **MinerU structured reading** — formulas as LaTeX, tables as HTML | `pip install "zotero-mcp-server[mineru]"` |
| `scite` | [Scite](https://scite.ai) citation intelligence — tallies and retraction alerts (no account needed) | `pip install "zotero-mcp-server[scite]"` |
| `all` | Everything above | `pip install "zotero-mcp-server[all]"` |

> **ADS needs no extra**: NASA ADS integration only requires `requests` (already a core dep) — just set the `ADS_API_TOKEN` env var.
>
> **MinerU dependency isolation**: the `[mineru]` extra pulls `mineru[all]` (includes torch). To avoid dependency conflicts, install MinerU in a separate venv and point the `mineru.executable` config at its CLI — the main package stays torch-free. The `api` backend mode needs zero local deps.

```bash
# Full install with all features
uv tool install "zotero-mcp-server[all]"

# Just semantic search
uv tool install "zotero-mcp-server[semantic]"
```

If you only need basic library access (search, read, annotate, write), the default install with no extras is all you need.

#### Updating Your Installation

```bash
zotero-mcp update --check-only    # Check for updates
zotero-mcp update                 # Update to latest version (preserves all configurations)
```

## 🧠 Semantic Search

AI-powered semantic search lets you find research based on concepts and meaning, not just keywords.

### Setup

```bash
# Configure during initial setup (recommended)
zotero-mcp setup

# Or configure semantic search separately
zotero-mcp setup --semantic-config-only
```

**Available Embedding Models:**
- **Default (all-MiniLM-L6-v2)**: Free, runs locally, good for most use cases
- **OpenAI**: Better quality, requires API key (`text-embedding-3-small` or `text-embedding-3-large`)
- **Gemini**: Better quality, requires API key (`gemini-embedding-001`)
- **Ollama**: Runs locally via Ollama API (e.g., `qwen3-embedding`)

**Update Frequency Options:** Manual / Auto on startup / Daily / Every N days

### Using

```bash
zotero-mcp update-db                       # Build database (fast, metadata-only)
zotero-mcp update-db --fulltext            # With full-text extraction (slower, comprehensive)
zotero-mcp update-db --force-rebuild       # Force complete rebuild
zotero-mcp update-db --openai-batch        # Submit via OpenAI Batch API (cheaper, async)
zotero-mcp openai-batch-status             # Check batch status
zotero-mcp openai-batch-import             # Import completed batches
zotero-mcp db-status                       # Show database status
```

**Example queries in your AI assistant:**
- *"Find research similar to machine learning concepts in neuroscience"*
- *"Papers that discuss climate change impacts on agriculture"*
- *"Find papers conceptually similar to this abstract: [paste abstract]"*

## 📄 MinerU Structured PDF Reading

When enabled, the `zotero_read_pdf_pages` tool returns structured Markdown — **formulas as LaTeX, tables as HTML** — far more accurate than PyMuPDF text-layer extraction.

### Setup

```bash
zotero-mcp setup   # the wizard asks whether to configure MinerU
```

The wizard guides you through choosing a backend:
- **`api`**: call a remote MinerU FastAPI service (zero local torch/ray deps) — configure `api_url`
- **`hybrid`** (default): local `mineru` CLI with GPU — auto-falls back to `pipeline` on OOM
- **`pipeline`**: local CPU, always works but slower

```json
// mineru block in ~/.config/zotero-mcp/config.json
{
  "mineru": {
    "enabled": true,
    "backend": "hybrid",
    "api_url": null,
    "executable": null,
    "timeout": 600,
    "cache_dir": "~/.cache/zotero-mcp/mineru"
  }
}
```

### Why split by use case?

| Scenario | Engine | Reason |
|----------|--------|--------|
| Semantic search (bulk analysis of dozens/hundreds) | PyMuPDF | Milliseconds; embeddings tolerate garbled formulas |
| Precise reading of one paper (LLM reads formulas/tables) | MinerU | Seconds–minutes, but formulas/tables are accurate |

MinerU results are cached per attachment key (`~/.cache/zotero-mcp/mineru/<key>/`) — only `.md` and split data are stored; MinerU's other byproducts (model JSON, layout PDFs, images) are discarded. Any failure silently falls back to PyMuPDF.

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
→ bibcode stored in Extra field → OA PDF cascade (Unpaywall/arXiv) + ADS link_gateway fallback
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

## 🖥️ Setup & Usage

**Requirements**
- Python 3.10+
- Zotero 7+ (for local API with full-text access)
- An MCP-compatible client (e.g., Claude Desktop, ChatGPT Developer Mode, Cherry Studio, Chorus)

### For Claude Desktop (example MCP client)

#### Configuration

1. **Auto-configure** (recommended):
   ```bash
   zotero-mcp setup
   ```

2. **Manual configuration** — add to `claude_desktop_config.json`:
   ```json
   {
     "mcpServers": {
       "zotero": {
         "command": "zotero-mcp",
         "env": {
           "ZOTERO_LOCAL": "true",
           "ZOTERO_API_KEY": "YOUR_API_KEY",
           "ZOTERO_LIBRARY_ID": "YOUR_LIBRARY_ID",
           "ADS_API_TOKEN": "YOUR_ADS_TOKEN (optional)"
         }
       }
     }
   }
   ```

   For **local read-only use**, `ZOTERO_LOCAL: "true"` is all you need — drop the API_KEY and LIBRARY_ID lines entirely. Add them only to enable **write mode**: the local API is fast but read-only, so the server uses the Zotero web API for write operations.

   - Generate an API key from <https://www.zotero.org/settings/security#applications>.
   - `ZOTERO_LIBRARY_ID` is your numeric **userID**, shown on that same page (for a group library, use the group's ID and also set `ZOTERO_LIBRARY_TYPE: "group"`).

   > **Tip:** if Claude Desktop can't find the `zotero-mcp` command, use the absolute path (`zotero-mcp setup-info` or `which zotero-mcp`) — GUI apps don't always inherit your shell `PATH`.

#### Usage

1. Start Zotero desktop (make sure local API is enabled in preferences)
2. Launch Claude Desktop
3. Ask in natural language:
   - "Search my library for papers on machine learning"
   - "Read page 4 of this transformer paper and explain the attention formula" (MinerU returns correct LaTeX)
   - "Import 2003ApJ...589L..21B from ADS into my cosmology collection"
   - "Analyze this paper's citation graph — what high-impact papers am I missing?"
   - "Move all papers tagged 'survey' into the 'surveys' collection"
   - "Extract all PDF annotations from my paper on neural networks"
   - "Find papers conceptually similar to deep learning in computer vision" *(semantic search)*

### For Cherry Studio

Go to Settings → MCP Servers → Edit MCP Configuration:

```json
{
  "mcpServers": {
    "zotero": {
      "name": "zotero",
      "type": "stdio",
      "isActive": true,
      "command": "zotero-mcp",
      "args": [],
      "env": {
        "ZOTERO_LOCAL": "true"
      }
    }
  }
}
```

## 🔧 Advanced Configuration

### WebDAV Attachment Storage

If your Zotero syncs PDF attachments via WebDAV (e.g. Nutstore/坚果云), configure these env vars so the server can download attachments directly from WebDAV (a fallback when the local API is unavailable):

```bash
ZOTERO_WEBDAV_URL=https://dav.jianguoyun.com/dav/
ZOTERO_WEBDAV_USERNAME=your_account
ZOTERO_WEBDAV_PASSWORD=app_password   # Nutstore uses an "app password", not your login password
```

> **Note:** organizing/classifying (collection membership) only touches Zotero metadata and is **completely unaffected by WebDAV**. Only PDF-reading features (read_pdf_pages, annotation extraction, semantic-search fulltext indexing) may be affected — with WebDAV configured, they automatically pull files from the cloud.

### Environment Variables

**Zotero Connection:**
- `ZOTERO_LOCAL=true`: Use the local Zotero API (default: false)
- `ZOTERO_API_KEY`: Your Zotero API key (for web API)
- `ZOTERO_LIBRARY_ID`: Your Zotero library ID (for web API)
- `ZOTERO_LIBRARY_TYPE`: Library type (user or group, default: user)
- `ZOTERO_WEBDAV_URL` / `ZOTERO_WEBDAV_USERNAME` / `ZOTERO_WEBDAV_PASSWORD`: WebDAV attachment storage (optional)

**Semantic Search:**
- `ZOTERO_EMBEDDING_MODEL`: Embedding model (default, openai, gemini, ollama)
- `OPENAI_API_KEY` / `OPENAI_EMBEDDING_MODEL` / `OPENAI_BASE_URL`
- `GEMINI_API_KEY` / `GEMINI_EMBEDDING_MODEL` / `GEMINI_BASE_URL`
- `OLLAMA_EMBEDDING_MODEL` / `OLLAMA_BASE_URL`
- `ZOTERO_DB_PATH`: Custom `zotero.sqlite` path (optional)

**NASA ADS (astrophysics literature):**
- `ADS_API_TOKEN`: NASA ADS API token ([get one free](https://ui.adsabs.harvard.edu/#user/settings/token))

**MinerU (structured reading):**
- Configured via the `zotero-mcp setup` wizard, stored in the `mineru` block of `~/.config/zotero-mcp/config.json`

### Command-Line Options

```bash
zotero-mcp serve                              # Run the server
zotero-mcp serve --transport stdio|streamable-http|sse
zotero-mcp setup                              # Interactive setup
zotero-mcp setup --semantic-config-only       # Configure only semantic search
zotero-mcp setup-info                         # Show install path and config info
zotero-mcp update                             # Update to latest version
zotero-mcp update --check-only                # Check for updates without installing
zotero-mcp update-db                          # Update semantic search database
zotero-mcp update-db --fulltext --force-rebuild
zotero-mcp db-status                          # Show database status
zotero-mcp version
```

## ⌨️ CLI Mode (`zotero-cli`)

`zotero-cli` is a standalone terminal interface to your Zotero library — same tools as the MCP server but without needing an AI assistant. Useful for quick lookups, shell scripts, and automation.

```bash
zotero-cli search "machine learning"          # keyword search
zotero-cli s "neural networks" --limit 5      # short alias + limit
zotero-cli search --mode semantic "attention mechanisms"
zotero-cli g metadata ABC123 --format bibtex  # BibTeX export
zotero-cli ann list ABC123                    # annotations
zotero-cli add doi 10.1038/s41586-021-03819-2
zotero-cli add doi 10.1038/... -c "Reading List"  # import + file into collection
zotero-cli coll list                          # list collections
zotero-cli db update                          # update semantic search DB
zotero-cli -v search "CRISPR"                 # verbose mode
```

Both `zotero-mcp` and `zotero-cli` share the configuration set up by `zotero-mcp setup`.

## 📑 PDF Annotation Extraction

- **Direct PDF Processing**: Extract annotations directly from PDF files, even if not yet indexed by Zotero
- **Enhanced Search**: Search through PDF annotations and comments
- **Image Annotation Support**: Extract image annotations from PDFs
- **Seamless Integration**: Works alongside Zotero's native annotation system

For optimal annotation extraction, it is **highly recommended** to install the [Better BibTeX plugin](https://retorque.re/zotero-better-bibtex/installation/). The first time you use PDF annotation features, the necessary tools are automatically downloaded.

## 📚 Available Tools

### 🧠 Semantic Search
- `zotero_semantic_search` / `zotero_update_search_database` / `zotero_get_search_database_status`

### 🔍 Search
- `zotero_search_items` / `zotero_advanced_search` / `zotero_search_by_tag` / `zotero_search_by_citation_key`
- `zotero_get_collections` / `zotero_get_collection_items` / `zotero_get_tags` / `zotero_get_recent`

### 📚 Content
- `zotero_get_item_metadata` (supports `markdown` / `json` / `bibtex`) / `zotero_get_item_fulltext` / `zotero_get_item_children`
- `zotero_read_pdf_pages` (returns structured formulas/tables when MinerU is enabled)

### 🔭 NASA ADS (new)
- `zotero_add_by_bibcode` — import by bibcode
- `zotero_search_ads` — fielded ADS search
- `zotero_ads_citation_network` — citation graph analysis

### 📝 Annotations & Notes
- `zotero_get_annotations` / `zotero_get_notes` / `zotero_search_notes`
- `zotero_create_note` / `zotero_update_note` / `zotero_delete_note`
- `zotero_create_annotation` / `zotero_create_area_annotation` / `zotero_get_page_layout`
- `zotero_update_annotation` / `zotero_delete_annotation`

### ✏️ Item & Collection Management
- `zotero_add_by_doi` / `zotero_add_by_url` / `zotero_add_by_isbn` / `zotero_add_by_bibtex` / `zotero_add_by_csl_json` / `zotero_add_from_file`
- `zotero_create_collection` / `zotero_delete_collection` / `zotero_search_collections` / `zotero_manage_collections`
- `zotero_update_item` / `zotero_delete_item` / `zotero_find_duplicates` / `zotero_merge_duplicates`
- `zotero_batch_update_tags` / `zotero_batch_update_extra` / `zotero_get_pdf_outline`

All add tools take `collections` (keys, names, or `parent/child` paths), `if_exists` (`duplicate` / `file` / `skip`), and `create_missing_collections` parameters.

### 📊 Scite Citation Intelligence
- `scite_enrich_item` / `scite_enrich_search` / `scite_check_retractions`

### 🔗 Related Items
- `zotero_get_item_related` / `zotero_add_item_relation` / `zotero_remove_item_relation`

### Other
- `zotero_find_related_papers` (OpenAlex citation graph) / `zotero_library_coverage` (PDF coverage audit)
- `zotero_synthesize_annotations` / `zotero_export_bibliography`

## 🧪 Testing

```bash
uv run pytest tests/     # full test suite
```

## 🔍 Troubleshooting

- **No results found**: Ensure Zotero is running and the local API is enabled (`Allow other applications on this computer to communicate with Zotero` in preferences)
- **Full text not available**: Use Zotero 7+ for local full-text access
- **Semantic search returns no results**: Initialize with `zotero-mcp update-db`, check `zotero-mcp db-status`
- **404 after changing embedding model**: `zotero-mcp update-db --force-rebuild`
- **MinerU unavailable**: Check that `mineru` CLI is on PATH or `mineru.executable` is set; falls back to PyMuPDF automatically
- **ADS reports token not set**: Run `zotero-mcp setup` to configure `ADS_API_TOKEN`
- **Database issues after switching install/search methods**: `zotero-mcp update-db --force-rebuild`

## ☕ Support

If you find Zotero MCP useful, consider buying me a coffee!

<a href="https://buymeacoffee.com/stevenyuyy">
  <img src="https://img.shields.io/badge/Buy%20Me%20a%20Coffee-ffdd00?style=for-the-badge&logo=buy-me-a-coffee&logoColor=black" alt="Buy Me a Coffee">
</a>

## 📄 License

MIT
