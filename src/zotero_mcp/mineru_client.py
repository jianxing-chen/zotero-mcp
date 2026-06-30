"""MinerU client — structured PDF parsing (formulas as LaTeX, tables as HTML).

Used by the ``zotero_read_pdf_pages`` tool for *precise reading* of a single
paper. This is deliberately separate from the semantic-search fulltext
extraction path (``local_db._extract_text_from_pdf``), which stays on the
fast PyMuPDF/pdfminer path — MinerU is ~100x slower and only worth it when an
LLM actually needs to read formula/table content.

Backend strategy (configured via ``~/.config/zotero-mcp/config.json`` →
``mineru.backend``):

- ``api``      — call a remote MinerU FastAPI service (``MINERU_API_URL``).
                 Zero local dependency on torch/ray.
- ``hybrid``   — local ``mineru`` CLI with ``-b hybrid-engine`` (GPU). Falls
                 back to ``pipeline`` on OOM / non-zero exit.
- ``pipeline`` — local ``mineru`` CLI with ``-b pipeline`` (CPU, always works).

Any failure returns ``None`` so the caller silently falls back to PyMuPDF.

Cache layout: ``~/.cache/zotero-mcp/mineru/<attachment_key>/{fulltext.md,
pages.json, meta.json}``. Invalidated when the source PDF's mtime+size change.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

logger = logging.getLogger(__name__)

# Subprocess sentinels and limits, mirroring local_db.py conventions.
_EXTRACTION_TIMEOUT = "__EXTRACTION_TIMEOUT__"
_DEFAULT_TIMEOUT = 600  # MinerU is heavy: 10-30s on GPU, minutes on CPU.

# API keys stripped from child-process env (copied from local_db.py:303-311).
_SENSITIVE_ENV_KEYS = (
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "ANTHROPIC_API_KEY",
    "ZOTERO_API_KEY",
)

_DEFAULT_CONFIG_PATH = Path.home() / ".config" / "zotero-mcp" / "config.json"
_DEFAULT_CACHE_DIR = Path.home() / ".cache" / "zotero-mcp" / "mineru"


@dataclass
class ParseResult:
    """Outcome of a MinerU parse, possibly cached.

    ``markdown`` is the full document (all pages, cross-page).
    ``pages`` is the per-page split (1-indexed in caller conventions; element 0 = page 1).
    ``source`` is one of {"mineru:hybrid", "mineru:pipeline", "mineru:api", "mineru:cached"}.
    """

    markdown: str
    pages: list[str]
    source: str


# --------------------------------------------------------------------------- #
# Config loading
# --------------------------------------------------------------------------- #
def load_mineru_config(config_path: str | Path | None = None) -> dict[str, Any]:
    """Return the ``mineru`` block from config.json, or an empty dict."""
    path = Path(config_path) if config_path else _DEFAULT_CONFIG_PATH
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg.get("mineru") or {}
    except Exception:
        return {}


def is_mineru_enabled(config: dict[str, Any] | None) -> bool:
    """True iff the user has explicitly enabled MinerU parsing."""
    return bool(config and config.get("enabled", False))


def _resolve_cache_dir(config: dict[str, Any]) -> Path:
    """Resolve the cache root, expanding ``~``."""
    raw = config.get("cache_dir")
    if raw:
        return Path(os.path.expanduser(raw))
    return _DEFAULT_CACHE_DIR


def _resolve_executable(config: dict[str, Any]) -> str | None:
    """Return the mineru CLI path, or None if unavailable."""
    configured = config.get("executable")
    if configured:
        expanded = os.path.expanduser(configured)
        if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
            return expanded
        # configured but missing — do not fall through to PATH; respect the
        # user's explicit choice and surface the failure as "unavailable".
        return None
    return shutil.which("mineru")


def _resolve_timeout(config: dict[str, Any]) -> int:
    raw = config.get("timeout")
    try:
        t = int(raw) if raw is not None else _DEFAULT_TIMEOUT
    except (TypeError, ValueError):
        t = _DEFAULT_TIMEOUT
    return t if t > 0 else _DEFAULT_TIMEOUT


# --------------------------------------------------------------------------- #
# Availability check
# --------------------------------------------------------------------------- #
def is_mineru_available(config: dict[str, Any]) -> bool:
    """Quick check: can we actually run MinerU with the current config?

    - ``api`` backend: requires a non-empty ``api_url`` (liveness probed lazily
      on first parse, not here — probing on every tool call is wasteful).
    - ``hybrid``/``pipeline``: requires the mineru executable to resolve.
    """
    backend = (config.get("backend") or "hybrid").lower()
    if backend == "api":
        return bool(config.get("api_url"))
    return _resolve_executable(config) is not None


# --------------------------------------------------------------------------- #
# CLI subprocess invocation (hybrid / pipeline)
# --------------------------------------------------------------------------- #
def _sanitize_child_env() -> dict[str, str]:
    """Copy env minus sensitive keys, force UTF-8 (mirrors local_db.py:303-319)."""
    child_env = os.environ.copy()
    for k in _SENSITIVE_ENV_KEYS:
        child_env.pop(k, None)
    child_env.setdefault("PYTHONIOENCODING", "utf-8")
    child_env.setdefault("PYTHONUTF8", "1")
    return child_env


def _call_mineru_cli(
    pdf_path: Path,
    start_page_0: int,
    end_page_0: int,
    backend: str,
    out_dir: Path,
    timeout: int,
    executable: str,
) -> tuple[str, list[dict] | None] | None:
    """Run ``mineru -p ... -o ... -b <backend>`` and return (.md text, content_list).

    Returns ``(markdown, content_list_blocks)`` where ``content_list_blocks`` is
    the parsed ``content_list.json`` (used for per-page splitting) or None when
    MinerU did not emit it. Returns None on any failure (non-zero exit, missing
    output, timeout). ``hybrid`` failures (often GPU OOM) are retried once with
    ``pipeline`` by the caller via :func:`_call_cli_with_fallback`.

    Only the .md text and content_list blocks leave this function — MinerU's
    other byproducts (_model.json, _middle.json, layout.pdf, images/) stay in
    the caller's TemporaryDirectory and are discarded automatically.
    """
    cmd = [
        executable,
        "-p", str(pdf_path),
        "-o", str(out_dir),
        "-b", backend,
        "-m", "auto",       # auto-detect scanned vs native text
        "-f", "true",       # formula recognition
        "-t", "true",       # table recognition
    ]
    if start_page_0 >= 0:
        cmd += ["-s", str(start_page_0)]
    if end_page_0 >= 0:
        cmd += ["-e", str(end_page_0)]

    child_env = _sanitize_child_env()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=child_env,
        )
    except subprocess.TimeoutExpired:
        sys.stderr.write(f"\r{' ' * 120}\r")
        logger.warning(f"MinerU ({backend}) timed out after {timeout}s: {pdf_path.name}")
        return None
    except Exception as e:
        sys.stderr.write(f"\r{' ' * 120}\r")
        logger.warning(f"MinerU ({backend}) failed to launch: {pdf_path.name}: {e}")
        return None

    if result.returncode != 0:
        logger.warning(
            f"MinerU ({backend}) exit {result.returncode}: {pdf_path.name}: "
            f"{result.stderr[:200] if result.stderr else 'no error output'}"
        )
        return None

    # Output file is <out_dir>/<pdf_stem>.md (MinerU names it after the input).
    # The CLI may place it in an auto subdir; search for the first .md.
    md_files = list(out_dir.rglob("*.md"))
    # Prefer the one matching the input stem, else the largest.
    md_files.sort(
        key=lambda p: (p.stem != pdf_path.stem, -p.stat().st_size)
    )
    md_text = None
    for md in md_files:
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
            if text.strip():
                md_text = text
                break
        except Exception:
            continue
    if md_text is None:
        logger.warning(f"MinerU ({backend}) produced no readable .md for {pdf_path.name}")
        return None

    # Read content_list.json (same stem) for per-page splitting. Read it now
    # while the temp dir still exists; return the parsed blocks, not the path.
    content_list = _read_content_list(out_dir, pdf_path.stem)
    return md_text, content_list


def _read_content_list(out_dir: Path, stem: str) -> list[dict] | None:
    """Find and parse MinerU's ``<stem>_content_list.json`` (or v2) if present."""
    candidates = [
        out_dir / f"{stem}_content_list.json",
        out_dir / f"{stem}_content_list_v2.json",
    ]
    # Also search recursively — MinerU sometimes nests output in an auto dir.
    candidates += list(out_dir.rglob("*_content_list*.json"))
    for cand in candidates:
        try:
            if cand.exists():
                data = json.loads(cand.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return data
        except Exception:
            continue
    return None


def _normalize_backend(raw: str | None) -> str:
    """Normalize a configured backend name to a MinerU-accepted value.

    MinerU's backend names changed across versions: early releases used
    ``hybrid``/``vlm``, while 3.x uses ``hybrid-auto-engine``/``vlm-auto-engine``
    (plus ``*-http-client`` variants). Accept both the short and long forms
    so configs stay portable. ``pipeline`` is stable across versions.
    """
    b = (raw or "hybrid-auto-engine").lower().strip()
    # Map short forms → 3.x canonical names.
    if b in ("hybrid", "hybrid-engine"):
        return "hybrid-auto-engine"
    if b in ("vlm", "vlm-engine"):
        return "vlm-auto-engine"
    # Pass through pipeline / *-auto-engine / *-http-client / api as-is.
    return b


def _is_gpu_backend(backend: str) -> bool:
    """True for backends that need GPU/MLX and may OOM (worth a pipeline retry)."""
    return backend in ("hybrid-auto-engine", "hybrid", "hybrid-engine",
                       "vlm-auto-engine", "vlm", "vlm-engine")


def _call_cli_with_fallback(
    pdf_path: Path,
    start_page_0: int,
    end_page_0: int,
    config: dict[str, Any],
    timeout: int,
) -> tuple[str, list[dict] | None, str] | None:
    """Try the configured local backend, falling GPU→pipeline on failure.

    Returns (markdown, content_list, source_label) or None.
    """
    executable = _resolve_executable(config)
    if not executable:
        return None

    backend = _normalize_backend(config.get("backend"))
    if backend == "api":
        # API handled elsewhere; if user misconfigured backend here, try hybrid.
        backend = "hybrid-auto-engine"

    # Build a stable temp work dir so we can inspect output.
    with tempfile.TemporaryDirectory(prefix="zotero_mineru_") as out_dir:
        out = Path(out_dir)
        result = _call_mineru_cli(pdf_path, start_page_0, end_page_0, backend, out, timeout, executable)
        if result is not None:
            md, content_list = result
            return md, content_list, f"mineru:{backend}"
        # GPU backend failed (likely OOM / unsupported) → retry with pipeline.
        if _is_gpu_backend(backend):
            logger.info(f"MinerU {backend} failed; retrying with pipeline backend.")
            # Fresh out dir to avoid reading stale hybrid output.
            with tempfile.TemporaryDirectory(prefix="zotero_mineru_") as out2:
                result2 = _call_mineru_cli(
                    pdf_path, start_page_0, end_page_0, "pipeline", Path(out2), timeout, executable
                )
                if result2 is not None:
                    md2, content_list2 = result2
                    return md2, content_list2, "mineru:pipeline"
    return None


# --------------------------------------------------------------------------- #
# API (remote FastAPI) invocation
# --------------------------------------------------------------------------- #
def _safe_extract_zip(zip_bytes: bytes, dest: Path) -> None:
    """Extract a zip blob with path-traversal protection (mirrors webdav.py:84-100)."""
    import io

    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            normalized = info.filename.replace("\\", "/")
            relative = Path(PurePosixPath(normalized))
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"Unsafe path in MinerU result zip: {info.filename}")
            target = dest / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)


def _call_mineru_api(
    pdf_path: Path,
    start_page_0: int,
    end_page_0: int,
    api_url: str,
    timeout: int,
) -> tuple[str, list[dict] | None, str] | None:
    """POST the PDF to a remote MinerU FastAPI ``/file_parse`` endpoint.

    Returns (markdown, content_list, "mineru:api") or None on failure.
    """
    import requests  # optional dependency, only needed for api backend

    # Build multipart form. MinerU's /file_parse accepts file + options.
    data: dict[str, str] = {
        "return_md": "true",
        "response_format_zip": "true",
        "parse_method": "auto",
        "formula_enable": "true",
        "table_enable": "true",
    }
    if start_page_0 >= 0:
        data["start_page_id"] = str(start_page_0)
    if end_page_0 >= 0:
        data["end_page_id"] = str(end_page_0)

    try:
        with open(pdf_path, "rb") as fh:
            resp = requests.post(
                f"{api_url.rstrip('/')}/file_parse",
                files={"files": (pdf_path.name, fh, "application/pdf")},
                data=data,
                timeout=timeout,
                stream=True,
            )
    except Exception as e:
        logger.warning(f"MinerU API request failed: {e}")
        return None

    if resp.status_code != 200:
        logger.warning(f"MinerU API returned HTTP {resp.status_code}: {resp.text[:200]}")
        return None

    with tempfile.TemporaryDirectory(prefix="zotero_mineru_") as out_dir:
        out = Path(out_dir)
        try:
            _safe_extract_zip(resp.content, out)
        except Exception as e:
            logger.warning(f"MinerU API zip extraction failed: {e}")
            return None
        md_files = list(out.rglob("*.md"))
        md_files.sort(key=lambda p: -p.stat().st_size)
        for md in md_files:
            try:
                text = md.read_text(encoding="utf-8", errors="replace")
                if text.strip():
                    content_list = _read_content_list(out, md.stem)
                    return text, content_list, "mineru:api"
            except Exception:
                continue
    logger.warning("MinerU API produced no readable .md")
    return None


# --------------------------------------------------------------------------- #
# Page splitting
# --------------------------------------------------------------------------- #
def _split_pages(markdown: str, content_list: list[dict] | None = None) -> list[str]:
    """Split a MinerU markdown document into per-page segments.

    Preference: use the parsed ``content_list.json`` blocks (MinerU emits a
    ``page_idx`` per block) for accurate page boundaries. Fallback: no split —
    return the whole document as a single page so the caller still gets content.
    """
    if content_list is not None:
        try:
            page_to_text: dict[int, list[str]] = {}
            for block in content_list:
                if not isinstance(block, dict):
                    continue
                page_idx = block.get("page_idx", block.get("page", 0))
                # content_list blocks carry "text" or "type"+"text"/"html".
                text = block.get("text") or block.get("html") or ""
                if text:
                    page_to_text.setdefault(int(page_idx), []).append(str(text))
            if page_to_text:
                max_page = max(page_to_text)
                pages: list[str] = []
                for i in range(max_page + 1):
                    pages.append("\n\n".join(page_to_text.get(i, [])))
                # Ensure at least the requested page exists; if MinerU returned
                # no text for a page, keep an empty marker so indices line up.
                return pages
        except Exception:
            pass
    # Fallback: whole document as one page. Callers requesting page N>0 will
    # see everything, which is better than nothing and is clearly marked by
    # the single-page list length in the output header.
    return [markdown] if markdown.strip() else []


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #
def _cache_dir_for(attachment_key: str, config: dict[str, Any]) -> Path:
    root = _resolve_cache_dir(config)
    return root / attachment_key


def _cache_meta_path(attachment_key: str, config: dict[str, Any]) -> Path:
    return _cache_dir_for(attachment_key, config) / "meta.json"


def _cache_is_valid(attachment_key: str, pdf_path: Path, config: dict[str, Any]) -> bool:
    """True if a cached parse exists and the source PDF hasn't changed.

    Invalidation is by ``pdf_size`` only — NOT ``mtime``. The same attachment
    may be served from different paths (local storage vs a temp download dir
    via the WebDAV/cloud fallback in ``_get_pdf_path``), and each copy gets a
    different mtime even though the content is identical. Size is stable across
    copies and reliably changes when Zotero replaces the attachment (a new
    attachment would also get a new key, so the cache dir would differ anyway).
    """
    meta_path = _cache_meta_path(attachment_key, config)
    if not meta_path.exists():
        return False
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    try:
        stat = pdf_path.stat()
    except OSError:
        return False
    return meta.get("pdf_size") == stat.st_size


def _write_cache(
    attachment_key: str,
    pdf_path: Path,
    markdown: str,
    pages: list[str],
    source: str,
    config: dict[str, Any],
) -> None:
    """Persist parse results + invalidation metadata."""
    cache_dir = _cache_dir_for(attachment_key, config)
    cache_dir.mkdir(parents=True, exist_ok=True)
    try:
        (cache_dir / "fulltext.md").write_text(markdown, encoding="utf-8")
        (cache_dir / "pages.json").write_text(
            json.dumps(pages, ensure_ascii=False), encoding="utf-8"
        )
        stat = pdf_path.stat()
        meta = {
            "pdf_mtime": stat.st_mtime,
            "pdf_size": stat.st_size,
            "mineru_source": source,
            "created_at": _now_iso(),
        }
        (cache_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    except Exception as e:
        logger.warning(f"Failed to write MinerU cache for {attachment_key}: {e}")


def _read_cache(attachment_key: str, config: dict[str, Any]) -> ParseResult | None:
    """Load a cached parse result (assumes validity already checked)."""
    cache_dir = _cache_dir_for(attachment_key, config)
    try:
        meta = json.loads((cache_dir / "meta.json").read_text(encoding="utf-8"))
        pages = json.loads((cache_dir / "pages.json").read_text(encoding="utf-8"))
        markdown = (cache_dir / "fulltext.md").read_text(encoding="utf-8")
    except Exception as e:
        logger.warning(f"Failed to read MinerU cache for {attachment_key}: {e}")
        return None
    if not isinstance(pages, list) or not markdown:
        return None
    return ParseResult(markdown=markdown, pages=pages, source="mineru:cached")


def _now_iso() -> str:
    import datetime

    return datetime.datetime.now().isoformat()


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def read_cached_or_parse(
    attachment_key: str,
    pdf_path: Path,
    config: dict[str, Any],
    *,
    force_rebuild: bool = False,
) -> ParseResult | None:
    """Return a (possibly cached) MinerU parse of the whole PDF.

    Always parses the *entire* document (not a page range) so that subsequent
    reads of other pages hit the cache. Page-range slicing is the caller's job.

    Returns None if MinerU is unavailable or parsing fails — the caller should
    then fall back to PyMuPDF.
    """
    if not attachment_key or not pdf_path.exists():
        return None

    # 1. Cache hit?
    if not force_rebuild and _cache_is_valid(attachment_key, pdf_path, config):
        cached = _read_cache(attachment_key, config)
        if cached is not None:
            return cached
        # Cache corrupt → drop and rebuild.
        _invalidate_cache(attachment_key, config)

    # 2. Parse whole document (start_page=0, end_page unset = all pages).
    parsed = _dispatch_parse(pdf_path, -1, -1, config)
    if parsed is None:
        return None
    markdown, content_list, source = parsed

    pages = _split_pages(markdown, content_list)
    # If page split failed, expose whole doc as page 1 so callers degrade
    # gracefully (they slice [start-1:end] and may get the whole thing).
    if not pages:
        pages = [markdown]

    _write_cache(attachment_key, pdf_path, markdown, pages, source, config)
    return ParseResult(markdown=markdown, pages=pages, source=source)


def _dispatch_parse(
    pdf_path: Path,
    start_page_0: int,
    end_page_0: int,
    config: dict[str, Any],
) -> tuple[str, list[dict] | None, str] | None:
    """Route to api or local-CLI backend per config.

    Degradation chain: ``api`` failure falls back to the local CLI (hybrid,
    which itself falls back to pipeline inside ``_call_cli_with_fallback``).
    A purely-local config skips the API hop entirely. Returns
    (markdown, content_list, source_label) or None.
    """
    backend = _normalize_backend(config.get("backend"))
    timeout = _resolve_timeout(config)

    if backend == "api":
        api_url = config.get("api_url")
        if api_url:
            result = _call_mineru_api(pdf_path, start_page_0, end_page_0, api_url, timeout)
            if result is not None:
                return result
            logger.info("MinerU api backend failed; falling back to local CLI.")
        else:
            logger.warning("MinerU backend=api but no api_url configured; trying local CLI.")
        # Fall through to local CLI.

    return _call_cli_with_fallback(pdf_path, start_page_0, end_page_0, config, timeout)


def _invalidate_cache(attachment_key: str, config: dict[str, Any]) -> None:
    cache_dir = _cache_dir_for(attachment_key, config)
    shutil.rmtree(cache_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Optional plaintext conversion (reserved for future semantic-search use)
# --------------------------------------------------------------------------- #
def markdown_to_plaintext(md: str) -> str:
    """Strip Markdown structure to a plain text string for embedding.

    NOT used by the current read_pdf integration (which keeps Markdown for
    LLM consumption), but provided so a future semantic-search path can reuse
    MinerU output without re-parsing. Keeps LaTeX symbol characters (drop only
    the ``$``/``$$`` delimiters), flattens tables to space-separated cells,
    drops image refs.
    """
    import re

    text = md
    # Drop image refs ![alt](path)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    # Drop heading markers
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    # Drop emphasis/strong markers
    text = re.sub(r"\*{1,3}|_{1,3}", "", text)
    # Drop $$...$$ delimiters but keep contents (LaTeX symbols are signal)
    text = re.sub(r"\$\$", "", text)
    # Drop $...$ inline delimiters (heuristic — bare $ are rare in prose)
    text = re.sub(r"(?<!\\)\$", "", text)
    # Flatten HTML tables: <tr>/<td>...</td></tr> → space-joined cells
    text = re.sub(r"<table[^>]*>|</table>|<tr[^>]*>|</tr>|<t[hd][^>]*>|</t[hd]>", " ", text)
    # Drop any remaining stray HTML tags
    text = re.sub(r"<[^>]+>", "", text)
    # Collapse whitespace
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
