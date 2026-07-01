# Zotero MCP：与你的文献库对话——本地或云端——接入 Claude、ChatGPT、ZCode 等

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
  <a href="https://zcode.dev/">
    <img src="https://img.shields.io/badge/ZCode-FF6B35?style=for-the-badge&logoColor=white" alt="ZCode">
  </a>
</p>

**Zotero MCP** 通过 [Model Context Protocol](https://modelcontextprotocol.io/introduction) 将你的 [Zotero](https://www.zotero.org/) 文献库与 [ChatGPT](https://openai.com)、[Claude](https://www.anthropic.com/claude)、[ZCode](https://zcode.dev/) 等 AI 助手（如 [Cherry Studio](https://cherry-ai.com/)、[Chorus](https://chorus.sh)、[Cursor](https://www.cursor.com/)）无缝连接。审阅论文、获取摘要、分析引用关系、提取 PDF 批注、导入天体物理文献……一句话搞定。

> 本 fork 在原版基础上新增了 **MinerU 结构化 PDF 精读** 和 **NASA ADS 文献接入**两个特性。

---

## ✨ 功能特性

### 🧠 AI 语义搜索
- 基于向量的相似度检索，覆盖整个文献库（需 `[semantic]` extra）
- 支持多种 embedding 模型：本地默认模型（免费）、OpenAI、Gemini、Ollama
- 返回相似度分数与上下文匹配
- 可配置的自动更新数据库

### 🔍 搜索文献库
- 按标题、作者、内容检索论文、文章、书籍
- 多条件结构化高级搜索
- 浏览 collection（文件夹）、标签、最近添加
- 语义搜索实现基于概念和主题的发现

### 📚 访问文献内容
- 获取任意条目的详细元数据（Markdown / BibTeX / JSON）
- 提取全文内容
- 按 Better BibTeX 引用键查找条目

### 📄 MinerU 结构化精读（新增 `[mineru]` extra）
- **精确提取 PDF 公式为 LaTeX、表格为 HTML**——这是 PyMuPDF 文本层提取做不到的
- 当 LLM 调用 `zotero_read_pdf_pages` 精读论文时，自动用 MinerU 解析，公式如 `$\text{Attention}(Q,K,V)=\text{softmax}(\frac{QK^T}{\sqrt{d_k}})V$` 能正确返回
- **按场景分流**：语义搜索（批量分析）保持 PyMuPDF 的毫秒级速度；只有精读单篇时才上 MinerU
- 四种后端：`cloud`（推荐，mineru.net 云端，精度最高）、`api`（远程服务，零本地依赖）、`hybrid`（本地 GPU）、`pipeline`（本地 CPU 兜底），自动降级链：`cloud-vlm → cloud-pipeline → 本地 hybrid → 本地 pipeline → PyMuPDF`
- 结果按 attachment key 缓存，避免重复解析
- 任何失败都静默回退到 PyMuPDF，不装 MinerU 时行为与原版 100% 一致

### 🔭 NASA ADS 天体物理文献（新增）
- **`zotero_add_by_bibcode`**：按 bibcode 导入论文，自动从 ADS 拉取元数据转为 Zotero 条目，bibcode 存入 Extra 字段供查重，并尝试下载 OA PDF（Unpaywall 级联 + ADS link_gateway 兜底）
- **`zotero_search_ads`**：ADS 字段化搜索（如 `title:exoplanets`、`author:"Riess, A"`），结果标注哪些已在你的 Zotero 库（DOI + bibcode 双重匹配）
- **`zotero_ads_citation_network`**：引用图分析——一篇论文引用了谁、被谁引用，并标出库里缺漏的高影响力文献
- **`zotero_export_ads`**：导出引用格式（BibTeX、AASTeX、MNRAS 等）
- Token 免费（[申请地址](https://ui.adsabs.harvard.edu/#user/settings/token)），通过 `ADS_API_TOKEN` 环境变量配置

### 📝 批注与笔记
- 提取并搜索 PDF 批注（含页码）
- 访问 Zotero 原生批注
- 创建和更新笔记与批注
- 提取 PDF 目录 / 大纲（需 `[pdf]` extra）

### ✏️ 写入操作
- **按 DOI 添加论文**，自动获取元数据 + OA PDF 级联下载（Unpaywall、arXiv、Semantic Scholar、PMC）
- **按 URL 添加**（arXiv、DOI 链接、普通网页）或从本地文件导入
- 创建和管理 collection，更新条目元数据，批量改标签
- 查重并合并重复条目（带 dry-run 预览）
- **混合模式**：本地读取 + Web API 写入

### 📊 Scite 引用情报（可选 `[scite]` extra）
- **引用计数**：查看每篇文献被支持 / 反对 / 提及的次数
- **撤稿预警**：扫描库中已撤稿或更正的论文
- 无需 Scite 账号，使用公开 API

### 🌐 灵活的访问方式
- 本地模式：离线访问（无需 API key）
- Web API：云端文献库访问
- 混合模式：本地读取 + Web API 写入
- 支持 WebDAV 附件存储（如堅果云）的直接下载

### ⌨️ 独立 CLI（`zotero-cli`）
- 无需 AI 助手，终端直接搜索、浏览、编辑文献库
- 适合脚本、自动化、快速查询
- 交互式短别名（`s`、`g`、`ann`、`coll`）

## 🚀 快速安装

### 默认安装（仅核心工具）

基础安装很轻量——包含搜索、元数据获取、批注、写入操作，不拉取任何 ML/AI 依赖。

```bash
# uv（推荐）
uv tool install zotero-mcp-server
zotero-mcp setup

# 或 pip
pip install zotero-mcp-server
zotero-mcp setup

# 或 pipx
pipx install zotero-mcp-server
zotero-mcp setup
```

### 可选 Extras

重型 ML/PDF 依赖拆分为可选 extras，保持基础安装的轻量：

| Extra | 增加的功能 | 安装命令 |
|-------|-----------|---------|
| `semantic` | 语义搜索（ChromaDB、sentence-transformers、OpenAI/Gemini embedding） | `pip install "zotero-mcp-server[semantic]"` |
| `pdf` | PDF 大纲提取（PyMuPDF）和 EPUB 批注支持 | `pip install "zotero-mcp-server[pdf]"` |
| `mineru` | **MinerU 结构化精读**——公式转 LaTeX、表格转 HTML | `pip install "zotero-mcp-server[mineru]"` |
| `scite` | [Scite](https://scite.ai) 引用情报——计数与撤稿预警 | `pip install "zotero-mcp-server[scite]"` |
| `all` | 以上全部 | `pip install "zotero-mcp-server[all]"` |

> **ADS 不需要额外 extra**：NASA ADS 接入仅需 `requests`（已是核心依赖），设置 `ADS_API_TOKEN` 环境变量即可。
>
> **MinerU 依赖隔离**：`[mineru]` extra 会拉取 `mineru[all]`（含 torch）。为避免依赖冲突，建议在独立 venv 装 MinerU，然后通过 `mineru.executable` 配置项指向 CLI 路径——主项目不引入 torch。`api` 后端模式则零本地依赖。

```bash
# 全功能安装
uv tool install "zotero-mcp-server[all]"

# 仅语义搜索
uv tool install "zotero-mcp-server[semantic]"
```

如果你只需要基础的文献库访问（搜索、读取、批注、写入），默认安装（无 extra）就够了。

## 🧠 语义搜索

基于 AI 的语义搜索让你能按概念和含义查找文献，而不仅是关键词。

### 配置

```bash
# 初始安装时一起配置（推荐）
zotero-mcp setup

# 或单独配置语义搜索
zotero-mcp setup --semantic-config-only
```

**支持的 Embedding 模型：**
- **默认（all-MiniLM-L6-v2）**：免费、本地运行，适合大多数场景
- **OpenAI**：质量更好，需 API key（`text-embedding-3-small` / `text-embedding-3-large`）
- **Gemini**：质量更好，需 API key（`gemini-embedding-001`）
- **Ollama**：本地运行（需模型名，如 `qwen3-embedding`）

**更新频率选项：**
- 手动：仅在运行 `zotero-mcp update-db` 时更新
- 启动时自动：每次服务器启动更新
- 每日：每天自动更新一次
- 每 N 天：自定义间隔

### 使用

```bash
zotero-mcp update-db                       # 构建数据库（快速，仅元数据）
zotero-mcp update-db --fulltext            # 含全文提取（更慢但更全面）
zotero-mcp update-db --force-rebuild       # 强制完全重建
zotero-mcp update-db --openai-batch        # 通过 OpenAI Batch API 提交（更便宜，异步）
zotero-mcp openai-batch-status             # 查看批次状态
zotero-mcp openai-batch-import             # 导入完成的批次
zotero-mcp db-status                       # 查看数据库状态
```

**在 AI 助手中的语义搜索示例：**
- "找和机器学习在神经科学中的应用相似的文献"
- "讨论气候变化对农业影响的论文"
- "找和这段摘要概念相似的文献：[粘贴摘要]"

## 📄 MinerU 结构化精读

启用后，`zotero_read_pdf_pages` 工具会返回结构化 Markdown——**公式为 LaTeX、表格为 HTML**，远比 PyMuPDF 文本层提取准确。

### 配置

```bash
zotero-mcp setup   # 向导会询问是否配置 MinerU
```

向导会引导你选择后端：
- **`api`**：调用远程 MinerU FastAPI 服务（零本地 torch/ray 依赖），需配置 `api_url`
- **`hybrid`**（默认）：本地 `mineru` CLI + GPU，OOM 时自动降级 `pipeline`
- **`pipeline`**：本地 CPU，始终可用但较慢

```json
// ~/.config/zotero-mcp/config.json 的 mineru 块
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

### 为什么按场景分流？

| 场景 | 用什么 | 原因 |
|------|--------|------|
| 语义搜索（批量分析几十上百篇） | PyMuPDF | 毫秒级，embedding 对错乱公式容忍度够用 |
| 精读单篇（LLM 读公式表格） | MinerU | 秒~分钟级，但公式/表格准确 |

MinerU 结果按 attachment key 缓存（`~/.cache/zotero-mcp/mineru/<key>/`），只存 `.md` 和切分数据，MinerU 的其他副产物（模型 JSON、可视化 PDF、图片）自动丢弃。任何失败都静默回退 PyMuPDF。

## 🔭 NASA ADS 天体物理文献

接入 NASA ADS（天体物理数据系统）的文献搜索、导入和引用图分析。

### 配置

1. 在 [ui.adsabs.harvard.edu](https://ui.adsabs.harvard.edu/#user/settings/token) 免费申请 API token
2. 运行 `zotero-mcp setup`，向导会引导输入 token（getpass 隐藏，防 shell 历史泄露）
3. token 写入 `ADS_API_TOKEN` 环境变量，由 MCP 客户端传给服务器

### 三个工具

**`zotero_add_by_bibcode`** — 按 bibcode 导入：
```
bibcode → ADS 搜索 API 取结构化字段 → 转 CSL-JSON → 复用现有批量管线创建 Zotero 条目
→ bibcode 存入 Extra 字段 → OA PDF 级联（Unpaywall/arXiv）+ ADS link_gateway 兜底
```

**`zotero_search_ads`** — 字段化搜索：
```
query="title:exoplanets author:\"Riess, A\"" fq="property:refereed" sort="citation_count desc"
→ 返回结果，每条标注 ✓已在库 / 不在库
```

**`zotero_ads_citation_network`** — 引用图：
```
identifier="2003ApJ...589L..21B" direction="both"
→ references(它引用了谁) + citations(被谁引用)
→ 每条标已在库/不在库，顶部汇总"Found N references, M citations, K already in library"
```

### 容错
- token 未配置 → 返回清晰错误提示
- ADS 不可达/限流 → 指数退避重试（2s/4s/8s），读 `X-RateLimit-Reset`
- PDF 受版权限制 → 静默跳过，导入仍成功（有元数据无 PDF）
- 所有第三方 PDF URL 经 SSRF 防护（拒绝内网/云元数据端点，逐跳重验重定向）

## 🖥️ 安装与使用

**要求**
- Python 3.10+
- Zotero 7+（本地 API 全文访问）
- MCP 兼容客户端（Claude Desktop、ChatGPT、Cherry Studio 等）

### Claude Desktop 配置

安装后两种方式：

1. **自动配置**（推荐）：
   ```bash
   zotero-mcp setup
   ```

2. **手动配置**：编辑 `claude_desktop_config.json`：
   ```json
   {
     "mcpServers": {
       "zotero": {
         "command": "zotero-mcp",
         "env": {
           "ZOTERO_LOCAL": "true",
           "ZOTERO_API_KEY": "你的API_KEY",
           "ZOTERO_LIBRARY_ID": "你的库ID",
           "ADS_API_TOKEN": "你的ADS_token（可选）"
         }
       }
     }
   }
   ```

   仅本地只读使用，`ZOTERO_LOCAL: "true"` 即可——删掉 API_KEY 和 LIBRARY_ID。加它们才启用**写入模式**：本地 API 快但只读，所以服务器用 Web API 执行写操作。

   API key 在 <https://www.zotero.org/settings/security#applications> 生成；`ZOTERO_LIBRARY_ID` 是你的数字 userID（群组库用群组 ID 并设 `ZOTERO_LIBRARY_TYPE: "group"`）。

   > **提示**：Claude Desktop 找不到 `zotero-mcp` 命令时，用绝对路径（`zotero-mcp setup-info` 或 `which zotero-mcp`）——GUI 应用不一定继承 shell 的 PATH。

### 使用示例

1. 启动 Zotero 桌面端（确保偏好设置里启用了本地 API）
2. 启动 Claude Desktop
3. 在对话中直接提问：
   - "搜一下我库里关于机器学习的论文"
   - "读一下这篇 transformer 论文第4页，解释它的 attention 公式"（启用 MinerU 时拿到正确 LaTeX）
   - "从 ADS 导入 2003ApJ...589L..21B，放进 cosmology collection"
   - "分析这篇论文的引用图，看看我库里还缺哪些高被引文献"
   - "把库里带 'survey' 标签的论文都归到 'surveys' 文件夹"
   - "提取我那篇神经网络论文的所有 PDF 批注"
   - "找和深度学习在计算机视觉中应用概念相似的论文"（语义搜索）

### ZCode / Claude Code / 任意 MCP 客户端的完整配置

本节介绍一套**完整生产配置**，整合本地 Zotero 访问、Web API 写入、NASA ADS、语义搜索（embedding + reranker）、MinerU 结构化 PDF 精读——全部在一个配置里。

#### 步骤 1：安装并运行配置向导

```bash
# 克隆并安装（editable 模式，改代码立即生效，无需重装）
git clone https://github.com/your-fork/zotero-mcp.git
cd zotero-mcp
uv pip install -e ".[all]"   # 或: pip install -e ".[all]"

# 运行交互式配置向导（配置 Zotero、语义搜索、MinerU、ADS）
zotero-mcp setup
```

向导会写入 `~/.config/zotero-mcp/config.json`（权限 600）。也可以手动编辑——完整示例见下方。

#### 步骤 2：配置 MCP 客户端

将 `zotero-mcp` 作为 stdio MCP 服务器添加。`command` 用 `zotero-mcp` 的绝对路径（运行 `which zotero-mcp` 获取——GUI 应用不一定继承 shell 的 PATH）。

```json
{
  "zotero": {
    "type": "stdio",
    "command": "/absolute/path/to/zotero-mcp",
    "args": ["serve", "--transport", "stdio"],
    "env": {
      "ZOTERO_LOCAL": "true",
      "ZOTERO_API_KEY": "你的-zotero-api-key",
      "ZOTERO_LIBRARY_ID": "你的数字-user-id",
      "ZOTERO_LIBRARY_TYPE": "user",
      "ADS_API_TOKEN": "你的-ads-api-token",
      "ZOTERO_MCP_LOG_LEVEL": "WARNING"
    }
  }
}
```

| 环境变量 | 是否必需 | 用途 |
|---|---|---|
| `ZOTERO_LOCAL` | ✅ | `true` = 通过本地 API 快速读取（需 Zotero 桌面端运行） |
| `ZOTERO_API_KEY` | 写入需要 | 本地 API 只读；Web API 处理写入（导入/编辑/删除） |
| `ZOTERO_LIBRARY_ID` | 写入需要 | 你的数字 userID（zotero.org/settings/security） |
| `ZOTERO_LIBRARY_TYPE` | 可选 | `user`（默认）或 `group` |
| `ADS_API_TOKEN` | ADS 需要 | 免费申请：<https://ui.adsabs.harvard.edu/#user/settings/token> |
| `ZOTERO_MCP_LOG_LEVEL` | 可选 | `WARNING`（默认）、`INFO` 或 `DEBUG` |

> **一次安装，多客户端共用**：`zotero-mcp` 是 editable 安装，同一个 `command` 路径可同时给 ZCode、Claude Code、Claude Desktop、Cherry Studio 等使用。每个客户端各自 fork 一个 `zotero-mcp` 进程，但跑的是同一份代码。改 `src/*.py` 后重启客户端即可生效，无需重装。

#### 步骤 3：配置语义搜索（embedding + reranker）

编辑 `~/.config/zotero-mcp/config.json` 的 `semantic_search` 块。可用**本地模型服务**（oMLX、Ollama）或**云端 API**（zenmux、OpenAI、Google）：

```jsonc
{
  "semantic_search": {
    "embedding_model": "openai",
    "embedding_config": {
      "model_name": "openai/text-embedding-3-large",
      "api_key": "你的-api-key",
      "base_url": "https://zenmux.ai/api/v1",
      "request_batch_size": 64,
      "rate_limit_rps": 10
    },
    "chunking": {
      "enabled": true,
      "chunk_size": 1500,
      "overlap": 200,
      "max_chunks_per_item": 20
    },
    "reranker": {
      "enabled": true,
      "type": "api",
      "model": "qwen/qwen3-rerank",
      "api_key": "你的-api-key",
      "base_url": "https://zenmux.ai/api/v1",
      "request_format": "nested",
      "candidate_multiplier": 3
    },
    "update_config": {
      "auto_update": false,
      "update_frequency": "manual"
    }
  }
}
```

**Embedding 提供商选项**（设置 `embedding_model` + `embedding_config`）：

| 提供商 | `embedding_model` | `base_url` | 说明 |
|---|---|---|---|
| zenmux（云端） | `"openai"` | `https://zenmux.ai/api/v1` | OpenAI 兼容；索引 1000+ 篇约 $3 |
| OpenAI（云端） | `"openai"` | （省略，用默认） | 默认 `text-embedding-3-small` |
| oMLX（本地，Apple Silicon） | `"openai"` | `http://localhost:8000/v1` | 免费但 8B 模型较慢 |
| Gemini（云端） | `"gemini"` | — | 用 `GEMINI_API_KEY` |
| HuggingFace（本地） | `"qwen"` 或任意 HF 模型名 | — | 进程内运行，sentence-transformers |
| Ollama（本地） | `"ollama"` | `http://localhost:11434` | `OLLAMA_BASE_URL` |
| ChromaDB 默认 | `"default"` | — | `all-MiniLM-L6-v2`，零配置，256 token 上限 |

**Reranker 选项**（设置 `reranker.type`）：

| 类型 | 工作方式 | 配置 |
|---|---|---|
| `"api"`（云端/本地 HTTP） | 调用 `/v1/rerank` 端点（zenmux、oMLX、Jina） | `base_url` + `api_key` + `request_format` |
| `"local"`（默认） | 进程内加载 HuggingFace CrossEncoder | `model`：如 `cross-encoder/ms-marco-MiniLM-L-6-v2` |

> **请求格式**：`"flat"`（默认，oMLX/Jina/Cohere 风格：顶层 `query`/`documents`）或 `"nested"`（zenmux 风格：`input.{query,documents}` + `parameters`）。不确定就先试 `"flat"`；如果报 400 要求 `input.query`，就改 `"nested"`。

> **Reranker 失败不影响搜索**：reranker 端点挂了时，搜索自动回退到向量排序（日志有 warning），语义搜索绝不会因 reranker 而中断。

#### 步骤 4：配置 MinerU 结构化 PDF 精读（可选）

MinerU 让 `zotero_read_pdf_pages` 输出正确的公式（LaTeX）和表格（HTML），而非 PyMuPDF 的乱码文本层。三种后端，自动降级：

```jsonc
{
  "mineru": {
    "enabled": true,
    "backend": "cloud",
    "cloud_token": "你的-mineru-net-token",
    "cloud_model": "vlm",
    "executable": "/path/to/mineru",
    "timeout": 600
  }
}
```

| 后端 | 速度 | 精度 | 需要 |
|---|---|---|---|
| `"cloud"`（推荐） | ~15秒/篇 | 最高（vlm 95+） | `cloud_token`，来自 <https://mineru.net/apiManage/docs> |
| `"hybrid"` / `"hybrid-auto-engine"` | ~3分钟（本地 GPU） | 高（85+） | 本地 `mineru` CLI（MinerU 3.x） |
| `"pipeline"` | 较慢（CPU） | 高（85+） | 本地 `mineru` CLI |

**降级链**（全自动）：`cloud-vlm → cloud-pipeline → 本地 hybrid → 本地 pipeline → PyMuPDF`。完全不配 MinerU 时，`zotero_read_pdf_pages` 用 PyMuPDF（快，但 LaTeX 论文的公式/表格可能错乱）。

> **缓存**：MinerU 结果缓存在 `~/.cache/zotero-mcp/mineru/<附件key>/`（只存 `fulltext.md` + `pages.json` + `meta.json`，每篇约 50KB）。首次读一篇触发全篇解析；后续读任意页命中缓存 <0.1 秒。PDF 大小变化时缓存失效。

#### 步骤 5：构建语义搜索索引

配置好 embedding 后，构建向量索引（语义搜索可用前必需）：

```bash
# 全量构建（索引所有论文——耗时取决于提供商，几分钟到几小时）
zotero-mcp update-db

# 查看状态
zotero-mcp status

# 强制重建（切换 embedding 模型时需要）
zotero-mcp update-db --force-rebuild
```

**存储**：ChromaDB 向量库在 `~/.config/zotero-mcp/chroma_db/`（1000+ 篇 + 3072 维 embedding 约 2-3GB）。全文提取优先用 Zotero 自己的 `.zotero-ft-cache`（无需重新提取 PDF）。

#### 步骤 6：在 MCP 客户端中使用

启动 Zotero 桌面端（本地 API），然后启动你的 MCP 客户端。试试这些：

- *"搜一下我库里关于球状星团的论文"* → `zotero_search_items` / `zotero_semantic_search`
- *"读一下这篇论文第4页，解释公式"* → `zotero_read_pdf_pages`（MinerU 返回正确 LaTeX）
- *"从 ADS 搜暗能量巡天，然后导入前3篇"* → `zotero_search_ads` + `zotero_add_by_bibcode`
- *"这篇论文引用了哪些我库里还没有的？"* → `zotero_ads_citation_network`
- *"把库里带 'survey' 标签的论文都归到 'surveys' 文件夹"* → `zotero_batch_update_tags` + `zotero_manage_collections`

#### 完整 `config.json` 示例

```jsonc
{
  "semantic_search": {
    "embedding_model": "openai",
    "embedding_config": {
      "model_name": "openai/text-embedding-3-large",
      "api_key": "你的-zenmux-或-openai-key",
      "base_url": "https://zenmux.ai/api/v1",
      "request_batch_size": 64,
      "rate_limit_rps": 10
    },
    "include_fulltext": true,
    "zotero_db_path": "/Users/你/Documents/Zotero/zotero.sqlite",
    "chunking": { "enabled": true, "chunk_size": 1500, "overlap": 200, "max_chunks_per_item": 20 },
    "reranker": {
      "enabled": true, "type": "api", "model": "qwen/qwen3-rerank",
      "api_key": "你的-key", "base_url": "https://zenmux.ai/api/v1",
      "request_format": "nested", "candidate_multiplier": 3
    },
    "update_config": { "auto_update": false, "update_frequency": "manual" }
  },
  "mineru": {
    "enabled": true, "backend": "cloud", "cloud_token": "你的-mineru-token",
    "cloud_model": "vlm", "executable": "/usr/local/bin/mineru", "timeout": 600
  },
  "client_env": {
    "ZOTERO_LOCAL": "true",
    "ZOTERO_API_KEY": "你的-zotero-key",
    "ZOTERO_LIBRARY_ID": "1234567",
    "ADS_API_TOKEN": "你的-ads-token"
  }
}
```

> **安全**：`config.json` 含 API key，被 `.gitignore` 覆盖（任意路径），永远不会被提交。文件创建时 `chmod 600`。如不慎泄露，在提供商控制台重新生成即可。

## 🔧 高级配置

### WebDAV 附件存储

若你的 Zotero 通过 WebDAV（如堅果云）同步 PDF 附件，配置以下环境变量可让服务器直接从 WebDAV 下载附件（本地 API 不可用时的回退）：

```bash
ZOTERO_WEBDAV_URL=https://dav.jianguoyun.com/dav/
ZOTERO_WEBDAV_USERNAME=你的坚果云账号
ZOTERO_WEBDAV_PASSWORD=应用密码   # 坚果云用"应用密码"，非登录密码
```

> **注意**：整理分类（collection 归属）只操作 Zotero 元数据，**完全不受 WebDAV 影响**。只有读 PDF 内容类功能（read_pdf_pages、提取批注、语义搜索全文提取）才可能受影响——配好 WebDAV 后会自动从云端拉取。

### 环境变量

**Zotero 连接：**
- `ZOTERO_LOCAL=true`：使用本地 Zotero API（默认 false）
- `ZOTERO_API_KEY`：Zotero API key（Web API）
- `ZOTERO_LIBRARY_ID`：Zotero 库 ID（Web API）
- `ZOTERO_LIBRARY_TYPE`：库类型（user 或 group，默认 user）
- `ZOTERO_WEBDAV_URL` / `ZOTERO_WEBDAV_USERNAME` / `ZOTERO_WEBDAV_PASSWORD`：WebDAV 附件存储（可选）

**语义搜索：**
- `ZOTERO_EMBEDDING_MODEL`：embedding 模型（default、openai、gemini、ollama）
- `OPENAI_API_KEY` / `OPENAI_EMBEDDING_MODEL` / `OPENAI_BASE_URL`
- `GEMINI_API_KEY` / `GEMINI_EMBEDDING_MODEL` / `GEMINI_BASE_URL`
- `OLLAMA_EMBEDDING_MODEL` / `OLLAMA_BASE_URL`
- `ZOTERO_DB_PATH`：自定义 `zotero.sqlite` 路径

**ADS（天体物理文献）：**
- `ADS_API_TOKEN`：NASA ADS API token（[免费申请](https://ui.adsabs.harvard.edu/#user/settings/token)）

**MinerU（结构化精读）：**
- 通过 `zotero-mcp setup` 向导配置，写入 `~/.config/zotero-mcp/config.json` 的 `mineru` 块

### 命令行选项

```bash
zotero-mcp serve                              # 启动服务器
zotero-mcp serve --transport stdio|streamable-http|sse
zotero-mcp setup                              # 交互式配置
zotero-mcp setup --semantic-config-only       # 仅配置语义搜索
zotero-mcp setup-info                         # 显示安装路径和配置信息
zotero-mcp update                             # 更新到最新版
zotero-mcp update-db                          # 更新语义搜索数据库
zotero-mcp update-db --fulltext --force-rebuild
zotero-mcp db-status                          # 查看数据库状态
zotero-mcp version
```

## ⌨️ CLI 模式（`zotero-cli`）

`zotero-cli` 是终端下直接操作 Zotero 库的独立工具，用与 MCP 服务器相同的工具但不需要 AI 助手——适合快速查询、shell 脚本、自动化。

```bash
zotero-cli search "机器学习"                  # 关键词搜索
zotero-cli s "神经网络" --limit 5            # 短别名 + 限制
zotero-cli search --mode semantic "注意力机制"
zotero-cli g metadata ABC123 --format bibtex # BibTeX 导出
zotero-cli ann list ABC123                   # 批注
zotero-cli add doi 10.1038/s41586-021-03819-2
zotero-cli add doi 10.1038/... -c "阅读列表"  # 导入并归档
zotero-cli coll list                         # 列出 collection
zotero-cli db update                          # 更新语义搜索库
zotero-cli -v search "CRISPR"                # 详细模式
```

## 📑 PDF 批注提取

- 直接从 PDF 文件提取批注（即使 Zotero 尚未索引）
- 搜索 PDF 批注和评论
- 支持图片批注提取
- 与 Zotero 原生批注系统无缝集成

建议安装 [Better BibTeX 插件](https://retorque.re/zotero-better-bibtex/installation/) 以获得最佳批注提取效果。首次使用 PDF 批注功能时，所需工具会自动下载。

## 📚 可用工具

### 语义搜索
- `zotero_semantic_search` / `zotero_update_search_database` / `zotero_get_search_database_status`

### 搜索
- `zotero_search_items` / `zotero_advanced_search` / `zotero_search_by_tag` / `zotero_search_by_citation_key`
- `zotero_get_collections` / `zotero_get_collection_items` / `zotero_get_tags` / `zotero_get_recent`
- `zotero_audit_collection_membership` — 一次性审计全库的文件夹归属：多少条目已分类/未分类、哪些条目不属于任何文件夹、哪些条目同时出现在多个文件夹

### 内容
- `zotero_get_item_metadata`（支持 markdown / json / bibtex）/ `zotero_get_item_fulltext` / `zotero_get_item_children`
- `zotero_read_pdf_pages`（启用 MinerU 时返回结构化公式/表格）

### NASA ADS（新增）
- `zotero_add_by_bibcode` — 按 bibcode 导入
- `zotero_search_ads` — ADS 字段化搜索
- `zotero_ads_citation_network` — 引用图分析
- `zotero_export_ads` — 导出引用格式（BibTeX、AASTeX、MNRAS 等）

### 批注与笔记
- `zotero_get_annotations` / `zotero_get_notes` / `zotero_search_notes`
- `zotero_create_note` / `zotero_update_note` / `zotero_delete_note`
- `zotero_create_annotation` / `zotero_create_area_annotation` / `zotero_get_page_layout`
- `zotero_update_annotation` / `zotero_delete_annotation`

### 写入与 collection 管理
- `zotero_add_by_doi` / `zotero_add_by_url` / `zotero_add_by_isbn` / `zotero_add_by_bibtex` / `zotero_add_by_csl_json` / `zotero_add_from_file`
- `zotero_create_collection` / `zotero_delete_collection` / `zotero_update_collection`（重命名/移动文件夹）/ `zotero_search_collections` / `zotero_manage_collections`
- `zotero_update_item` / `zotero_delete_item` / `zotero_find_duplicates` / `zotero_merge_duplicates`
- `zotero_batch_update_tags` / `zotero_batch_update_extra` / `zotero_get_pdf_outline`
- `zotero_enrich_item_metadata` / `zotero_enrich_batch` — 从 NASA ADS 补全 date、期刊缩写、bibcode、ADS 链接（无 DOI/arXiv 的条目用标题搜索兜底；preprint 自动升级为 journalArticle）
- `zotero_upgrade_preprints` — 将 arXiv 预印本升级为正式发表的 journalArticle

所有 add 工具支持 `collections`（key / 名称 / `父/子` 路径）、`if_exists`（duplicate / file / skip）、`create_missing_collections` 参数。

### Scite 引用情报
- `scite_enrich_item` / `scite_enrich_search` / `scite_check_retractions`

### 关联条目
- `zotero_get_item_related` / `zotero_add_item_relation` / `zotero_remove_item_relation`

### 其他
- `zotero_find_related_papers`（OpenAlex 引用图）/ `zotero_library_coverage`（PDF 覆盖率审计）
- `zotero_synthesize_annotations` / `zotero_export_bibliography`

## 🧪 测试

```bash
uv run pytest tests/     # 全套测试
```

## 🔍 故障排查

- **找不到结果**：确保 Zotero 正在运行且本地 API 已启用（偏好设置里勾选"允许其他应用与 Zotero 通信"）
- **全文不可用**：本地全文访问需 Zotero 7+
- **语义搜索无结果**：运行 `zotero-mcp update-db` 初始化，`zotero-mcp db-status` 检查状态
- **改了 embedding 模型报 404**：`zotero-mcp update-db --force-rebuild` 重建
- **MinerU 不可用**：检查 `mineru` CLI 是否在 PATH 或 `mineru.executable` 配置正确；不可用时会自动回退 PyMuPDF
- **ADS 报 token 未设置**：运行 `zotero-mcp setup` 配置 `ADS_API_TOKEN`
- **安装/搜索方式切换后数据库异常**：`zotero-mcp update-db --force-rebuild`

## 📄 许可证

MIT
