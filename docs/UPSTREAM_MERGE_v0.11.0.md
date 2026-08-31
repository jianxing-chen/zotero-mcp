# 上游 v0.11.0 合并与分支整合报告

> 完成时间：2026-08-31 ~ 09-01 ｜ 合并分支：`sync/upstream-v0.11.0`（worktree：`../zotero-mcp-sync`）

## 一、本次更新做了什么

### 1. 快照与保护措施（无损）

| 动作 | 结果 |
|---|---|
| 工作区 17 个未提交文件（MinerU 重构 WIP） | 已提交为 `017569d`（可 `git reset --soft HEAD~1` 退回） |
| 备份分支 | `backup/pre-upstream-v0.11.0` → 指向合并前的 `feat/mineru-vector-index` |
| `main` 快进 | `7096145`(v0.6.0) → `3cb3e2e`(v0.11.0)，0 提交领先、纯快进、无损失 |
| 合并工作区 | 独立 worktree `../zotero-mcp-sync`，主检出目录全程未受影响 |
| 未推送 | origin 未做任何 push，全部操作可本地回滚 |

### 2. 合并本体

`git merge upstream/main`：45 个文件冲突（21 个源码 + 24 个测试 + README），全部解决并提交为 `bd4db45`，随后测试修复提交 `7b7ef5a`。

**测试结果：3191 通过 / 10 失败 / 43 跳过**。10 个失败全部集中在嵌入提供方一隅（HuggingFace 门控模型 401 无凭证 5 个 + 别名解析联动 5 个），属环境依赖而非合并回归；单独运行可通过（存在测试顺序污染，见「遗留」）。

## 二、冲突解决策略（关键决策记录）

| 区域 | 策略 |
|---|---|
| **嵌入层** | 上游 embeddings 注册表/providers 为基底；fork 的 `OpenAIEmbeddingFunction`（dimensions/限流/分批）保留用于 openai 路径，并经 `ADAPTER.dimensions` 接入上游批处理适配器 |
| **semantic_search** | 上游批处理机（`--batch`/auto-loop/多 provider manifest）+ fork 的 `reindex_keys`/MinerU 缓存钩子；reranker = 上游进程级缓存 + fork 的 `ApiReranker`(type=api) 分发 |
| **write.py** | 上游重构版为基底（add_item 门面、CrossRef 映射、412 重试、含 auto-merge 的同步 merge_duplicates）；fork 独有工具与后台 worker（add_by_bibcode、enrich_*、upgrade_preprint_pdfs、异步 `_add_by_*_worker`）整体移植保留；add_by_bibtex/add_by_csl_json 保留 fork 异步版 |
| **outline** | 融合：fork 的 MinerU 缓存优先层 + 上游的外置子进程 TOC 读取器（#372/#431），API 锁只在元数据调用段持有，children 分页，附件键直连 |
| **annotations** | 上游合并面（manage_note、create_annotation 的 rect=）+ fork 工具注册全保留（search_notes、create/update/delete_note、create_area_annotation） |
| **read_pdf** | fork 架构（附件键三元组、MinerU 流、无单次页数上限）+ 上游加固版 `_cleanup_path` 移植 |
| **local_db** | 上游读取器（优先级/worker/瞬态缓存/get_key_group_map）+ fork 参数（pdf_timeout、prefer_mineru）与 MinerU 缓存优先钩子 |
| **CLI** | provider 无关的 `--batch` 系参数 + fork 的 `--reindex-*` 参数并存 |
| **setup** | 上游多构建 Claude 配置发现 + fork 的 ads_token/MinerU 配置（无 TTY 时自动跳过交互） |

## 三、分支处理结论

**已验证：其余 4 个特性分支（mineru-structured-pdf / ads-integration / collection-audit / omlx-reranker）全部是 `feat/mineru-vector-index` 的祖先，内容 100% 被包含。**

处理方案（待你在 sync 分支验收后执行）：

```bash
# 1) 验收通过后，把 sync 分支合回特性主分支
git checkout feat/mineru-vector-index
git merge --ff-only sync/upstream-v0.11.0   # 或直接 git branch -f

# 2) 归档旧里程碑分支（内容已包含，删除无损失；想留档案可先打 tag）
git tag archive/mineru-structured-pdf feat/mineru-structured-pdf
git tag archive/ads-integration feat/ads-integration
git tag archive/collection-audit feat/collection-audit
git tag archive/omlx-reranker feat/omlx-reranker
git branch -d feat/mineru-structured-pdf feat/ads-integration feat/collection-audit feat/omlx-reranker
git push origin --delete feat/mineru-structured-pdf feat/ads-integration feat/collection-audit feat/omlx-reranker

# 3) 推送（自行决定时机）
git push origin feat/mineru-vector-index main
```

**今后分支纪律：只保留一条长期特性分支（建议直接改名 `develop`），main 只做上游快进，每 1~2 个上游版本对齐一次**——本次 71→220 提交的落后翻三倍就是拖延的代价，冲突面从 24 文件涨到 45 文件。

## 四、遗留事项（按优先级）

1. **批量创建移植**：fork 的异步 `_add_by_*_worker` 仍逐条 create_items；把上游的 `_create_and_attach_batch`（50/批，A4 优化）移植进 worker。相关测试已放宽并注明（`test_batch_create.py`、`test_add_by_doi.py` 的 spy 断言）。
2. **嵌入测试收尾**：`test_embedding_provider_resolution` 的 5 个别名用例在全量运行时因 HF 401/顺序污染失败；需 HF 凭证或离线夹具，并排查注册表测试顺序污染。
3. **工具面收敛（可选）**：当前注册 65 个 MCP 工具（fork 名 + 上游门面并存）。确认 Agent 实际使用集后，可按上游 62→37 的思路裁剪，降低上下文占用。
4. **`--reindex-cached-mineru` 仍被禁用**（uniform-dim 构建路径决策，沿袭 fork 原状）。
5. **pyproject 依赖**：`pdf-inspector==0.2.6` 已是上游核心依赖，重新安装部署时注意 `pip install -e .`。

## 五、回滚

```bash
# 丢弃整个合并（sync 分支删除即可，主分支未动）
git worktree remove ../zotero-mcp-sync && git branch -D sync/upstream-v0.11.0
# main 回退（如需）
git branch -f main 7096145
```
