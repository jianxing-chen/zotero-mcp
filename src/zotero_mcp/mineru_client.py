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

import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urljoin, urlparse

logger = logging.getLogger(__name__)

# Subprocess limits.
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

    - ``cloud`` backend: requires a cloud_token (liveness probed lazily).
    - ``api`` backend: requires a non-empty ``api_url``.
    - ``hybrid``/``pipeline``: requires the mineru executable to resolve.
    """
    backend = _normalize_backend(config.get("backend"))
    if backend == "cloud":
        return bool(config.get("cloud_token") or os.getenv("MINERU_API_TOKEN"))
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
        "-p",
        str(pdf_path),
        "-o",
        str(out_dir),
        "-b",
        backend,
        "-m",
        "auto",  # auto-detect scanned vs native text
        "-f",
        "true",  # formula recognition
        "-t",
        "true",  # table recognition
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
    md_files.sort(key=lambda p: (p.stem != pdf_path.stem, -p.stat().st_size))
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
    """Normalize a configured backend name to a canonical value.

    Backend families:
      - ``cloud`` / ``online``: MinerU cloud API (mineru.net, token-authed)
      - ``hybrid`` / ``hybrid-engine``: local hybrid-auto-engine (MinerU 3.x)
      - ``vlm`` / ``vlm-engine``: local vlm-auto-engine
      - ``pipeline``: local CPU pipeline (stable across versions)
      - ``api``: local mineru-api server (legacy /file_parse)
    """
    b = (raw or "hybrid-auto-engine").lower().strip()
    # Map short forms → canonical names.
    if b in ("cloud", "online"):
        return "cloud"
    if b in ("hybrid", "hybrid-engine"):
        return "hybrid-auto-engine"
    if b in ("vlm", "vlm-engine"):
        return "vlm-auto-engine"
    # Pass through pipeline / *-auto-engine / *-http-client / api as-is.
    return b


def _is_gpu_backend(backend: str) -> bool:
    """True for backends that need GPU/MLX and may OOM (worth a pipeline retry)."""
    return backend in ("hybrid-auto-engine", "hybrid", "hybrid-engine", "vlm-auto-engine", "vlm", "vlm-engine")


def _call_cli_with_fallback(
    pdf_path: Path,
    start_page_0: int,
    end_page_0: int,
    config: dict[str, Any],
    timeout: int,
    *,
    backend: str | None = None,
    no_pipeline_fallback: bool = False,
) -> tuple[str, list[dict] | None, str] | None:
    """Try the configured local backend, falling GPU→pipeline on failure.

    Args:
        backend: When set, use this backend instead of ``config['backend']``
            (used by the ``backend_override`` path from ``_dispatch_parse``).
        no_pipeline_fallback: When True, do NOT retry with pipeline if the
            GPU backend fails — return None. Used when the caller pinned a
            single backend and wants no cross-backend degradation.

    Returns (markdown, content_list, source_label) or None.
    """
    executable = _resolve_executable(config)
    if not executable:
        return None

    backend = _normalize_backend(backend or config.get("backend"))
    if backend in ("api", "cloud"):
        # API/cloud handled elsewhere; if we reach the local CLI from those
        # backends (e.g. cloud failed and fell through), start the local
        # degradation chain from the GPU backend so the -b flag gets a valid
        # value (not "cloud"/"api"). Falls back to pipeline on GPU failure.
        backend = "hybrid-auto-engine"

    # Build a stable temp work dir so we can inspect output.
    with tempfile.TemporaryDirectory(prefix="zotero_mineru_") as out_dir:
        out = Path(out_dir)
        result = _call_mineru_cli(pdf_path, start_page_0, end_page_0, backend, out, timeout, executable)
        if result is not None:
            md, content_list = result
            return md, content_list, f"mineru:{backend}"
        # GPU backend failed (likely OOM / unsupported) → retry with pipeline.
        if _is_gpu_backend(backend) and not no_pipeline_fallback:
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
# SSRF guards (mirrors tools/_helpers.py:_url_resolves_to_public_host + _guarded_pdf_get)
# --------------------------------------------------------------------------- #
_MAX_DOWNLOAD_REDIRECTS = 5
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


def _url_is_public(url: str) -> bool:
    """Return True only if ``url`` is http(s) and its host resolves entirely
    to globally-routable IPs.

    SSRF guard for MinerU's cloud/api backends. The cloud ``full_zip_url`` is
    server-controlled (returned by mineru.net), and ``api_url`` is
    user-configured; both are fetched by the server, so we reject
    private/loopback/link-local addresses — including 169.254.169.254 (cloud
    metadata), which matters for HTTP/SSE-transport deployments.

    The 198.18.0.0/15 RFC-2544 range is allowed (transparent proxies like
    Clash/Surge route public domains through it on macOS). Mirrors
    ``tools/_helpers.py:_url_resolves_to_public_host``.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or None)
    except (socket.gaierror, UnicodeError, ValueError):
        return False
    if not infos:
        return False
    for info in infos:
        try:
            ip = ip_address(info[4][0])
        except (ValueError, IndexError):
            return False
        if int(ip) >> 17 == (0xC6120000 >> 17):  # 198.18.0.0/15
            continue
        if not ip.is_global or ip.is_reserved or ip.is_multicast:
            return False
    return True


def _guarded_download(url: str, timeout: int, method: str = "GET", files=None, data=None, stream: bool = False):
    """Fetch ``url`` with SSRF protection and manual redirect re-validation.

    Returns the final ``requests`` response, or ``None`` if any URL in the
    redirect chain is rejected as non-public, or there are too many redirects.
    For POST (the api-backend upload) redirects are not followed — the
    initial URL is validated and the request issued once.
    """
    import requests

    if not _url_is_public(url):
        logger.warning(f"MinerU URL rejected by SSRF guard: {url}")
        return None
    if method.upper() == "POST":
        try:
            return requests.post(url, files=files, data=data, timeout=timeout, stream=stream)
        except Exception as e:
            logger.warning(f"MinerU API request failed: {e}")
            return None
    current = url
    for _ in range(_MAX_DOWNLOAD_REDIRECTS + 1):
        if not _url_is_public(current):
            logger.warning(f"MinerU URL rejected by SSRF guard: {current}")
            return None
        try:
            resp = requests.get(current, timeout=timeout, stream=stream, allow_redirects=False)
        except Exception as e:
            logger.warning(f"MinerU download failed: {e}")
            return None
        if resp.status_code in _REDIRECT_STATUSES:
            location = resp.headers.get("Location")
            try:
                resp.close()
            except Exception:
                pass
            if not location:
                return None
            current = urljoin(current, location)
            continue
        return resp
    logger.warning("Too many redirects while fetching MinerU result")
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
    The URL is validated through the SSRF guard before the request.
    """
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

    endpoint = f"{api_url.rstrip('/')}/file_parse"
    with open(pdf_path, "rb") as fh:
        resp = _guarded_download(
            endpoint,
            timeout,
            method="POST",
            files={"files": (pdf_path.name, fh, "application/pdf")},
            data=data,
            stream=True,
        )

    if resp is None:
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
# Cloud API (mineru.net online) — token-authed async upload + poll
# --------------------------------------------------------------------------- #
_MINERU_CLOUD_BASE = "https://mineru.net/api/v4"
_CLOUD_POLL_INTERVAL = 3.0  # seconds between status checks


def _cloud_request(method: str, path: str, token: str, **kwargs) -> dict | None:
    """Authenticated request to the MinerU cloud API. Returns parsed JSON or None."""
    import requests

    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {token}"
    try:
        resp = requests.request(method, f"{_MINERU_CLOUD_BASE}{path}", headers=headers, timeout=30, **kwargs)
    except Exception as e:
        logger.warning(f"MinerU cloud {method} {path} failed: {e}")
        return None
    if resp.status_code != 200:
        logger.warning(f"MinerU cloud {method} {path}: HTTP {resp.status_code}: {resp.text[:200]}")
        return None
    try:
        return resp.json()
    except Exception:
        return None


def _cloud_download_zip(zip_url: str) -> bytes | None:
    """Download the result zip from a CDN URL. Returns bytes or None.

    The URL is server-controlled (returned by mineru.net) and therefore
    validated through the SSRF guard with manual redirect re-validation.
    """
    resp = _guarded_download(zip_url, timeout=120)
    if resp is None:
        return None
    if resp.status_code != 200:
        return None
    return resp.content


def _cloud_extract_result(zip_bytes: bytes, source: str) -> tuple[str, list[dict] | None, str] | None:
    """Extract markdown + content_list from a cloud result zip."""
    with tempfile.TemporaryDirectory(prefix="zotero_mineru_cloud_") as out_dir:
        out = Path(out_dir)
        try:
            _safe_extract_zip(zip_bytes, out)
        except Exception as e:
            logger.warning(f"MinerU cloud zip extraction failed: {e}")
            return None
        # Cloud zip layout: full.md (main markdown) + *_content_list.json
        md_files = list(out.rglob("*.md"))
        md_files.sort(key=lambda p: -p.stat().st_size)
        for md in md_files:
            try:
                text = md.read_text(encoding="utf-8", errors="replace")
                if text.strip():
                    # content_list: try matching stem, then any content_list file
                    content_list = _read_content_list(out, md.stem)
                    if content_list is None:
                        cl_files = list(out.rglob("*content_list*.json"))
                        for cl in cl_files:
                            try:
                                data = json.loads(cl.read_text(encoding="utf-8"))
                                if isinstance(data, list):
                                    content_list = data
                                    break
                            except Exception:
                                continue
                    return text, content_list, source
            except Exception:
                continue
    logger.warning("MinerU cloud zip contained no readable .md")
    return None


def _call_mineru_cloud(
    pdf_path: Path,
    start_page_0: int,
    end_page_0: int,
    token: str,
    model_version: str = "vlm",
    timeout: int = 600,
) -> tuple[str, list[dict] | None, str] | None:
    """Parse a PDF via the MinerU cloud API (mineru.net).

    Async flow: apply for upload URL → PUT file → poll batch → download zip.
    On vlm failure, retries once with pipeline. Returns
    (markdown, content_list, "mineru:cloud-vlm"|"mineru:cloud-pipeline") or None.
    """

    for attempt_model in (model_version, "pipeline") if model_version != "pipeline" else ("pipeline",):
        result = _cloud_parse_single(pdf_path, start_page_0, end_page_0, token, attempt_model, timeout)
        if result is not None:
            return result
        if attempt_model != "pipeline":
            logger.info("MinerU cloud vlm failed; retrying with pipeline model.")
    return None


def _cloud_parse_single(
    pdf_path: Path,
    start_page_0: int,
    end_page_0: int,
    token: str,
    model_version: str,
    timeout: int,
) -> tuple[str, list[dict] | None, str] | None:
    """Single cloud parse attempt with a given model version."""
    import time

    source = f"mineru:cloud-{model_version}"

    # 1. Apply for upload URL.
    file_entry: dict[str, Any] = {"name": pdf_path.name}
    # Cloud uses 1-indexed page_ranges string like "2,4-6"; we have 0-indexed.
    if start_page_0 >= 0 and end_page_0 >= 0:
        # Convert 0-based to 1-based for the API.
        page_ranges = f"{start_page_0 + 1}-{end_page_0 + 1}"
        file_entry["page_ranges"] = page_ranges
    body = {"files": [file_entry], "model_version": model_version}

    resp = _cloud_request("POST", "/file-urls/batch", token, json=body)
    if not resp or resp.get("code") != 0:
        logger.warning(f"MinerU cloud apply-upload failed: {resp}")
        return None
    data = resp.get("data") or {}
    batch_id = data.get("batch_id")
    file_urls = data.get("file_urls") or []
    if not batch_id or not file_urls:
        logger.warning("MinerU cloud: missing batch_id or file_urls")
        return None

    # 2. PUT upload the PDF (no Content-Type header per docs).
    #    upload_url is server-controlled → validate via SSRF guard.
    upload_url = file_urls[0]
    if not _url_is_public(upload_url):
        logger.warning(f"MinerU cloud upload URL rejected by SSRF guard: {upload_url}")
        return None
    try:
        import requests

        # Upload timeout scales with the overall timeout — thick books
        # produce large PDFs that need more than the old hardcoded 120s.
        upload_timeout = max(120, timeout // 3)
        with open(pdf_path, "rb") as fh:
            put_resp = requests.put(upload_url, data=fh, timeout=upload_timeout)
    except Exception as e:
        logger.warning(f"MinerU cloud file upload failed: {e}")
        return None
    if put_resp.status_code not in (200, 201):
        logger.warning(f"MinerU cloud upload HTTP {put_resp.status_code}")
        return None

    # 3. Poll for completion.
    deadline = time.time() + timeout
    while time.time() < deadline:
        poll = _cloud_request("GET", f"/extract-results/batch/{batch_id}", token)
        if poll is None:
            # Network/transport error — may be transient, keep polling.
            time.sleep(_CLOUD_POLL_INTERVAL)
            continue
        if poll.get("code") != 0:
            # Structured API error (e.g. auth, invalid batch). Retrying the
            # same batch won't help — abort rather than burn the full timeout.
            logger.warning(
                f"MinerU cloud poll returned error (code={poll.get('code')}, "
                f"msg={poll.get('msg')!r}) for {model_version}; aborting"
            )
            return None
        results = (poll.get("data") or {}).get("extract_result") or []
        if not results:
            time.sleep(_CLOUD_POLL_INTERVAL)
            continue
        item = results[0]
        state = item.get("state")
        if state == "done":
            zip_url = item.get("full_zip_url")
            if not zip_url:
                logger.warning("MinerU cloud: done but no full_zip_url")
                return None
            # 4. Download + extract.
            zip_bytes = _cloud_download_zip(zip_url)
            if zip_bytes is None:
                return None
            return _cloud_extract_result(zip_bytes, source)
        if state == "failed":
            err = item.get("err_msg", "unknown")
            logger.warning(f"MinerU cloud parse failed ({model_version}): {err}")
            return None
        # running / pending / converting — keep polling.
        time.sleep(_CLOUD_POLL_INTERVAL)

    logger.warning(f"MinerU cloud poll timed out after {timeout}s ({model_version})")
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
    """Persist parse results + invalidation metadata.

    Each file is written to a ``.tmp`` sibling first and then atomically
    replaced (``os.replace``), so a crash mid-write cannot leave a mismatched
    cache (e.g. new ``fulltext.md`` with a stale ``meta.json``). The cache is
    either fully old or fully new at any point on disk.
    """
    cache_dir = _cache_dir_for(attachment_key, config)
    cache_dir.mkdir(parents=True, exist_ok=True)
    try:
        pages_json = json.dumps(pages, ensure_ascii=False)
        stat = pdf_path.stat()
        meta = json.dumps(
            {
                "pdf_mtime": stat.st_mtime,
                "pdf_size": stat.st_size,
                "mineru_source": source,
                "created_at": _now_iso(),
            }
        )

        def _atomic(path: Path, content: str) -> None:
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, path)

        _atomic(cache_dir / "fulltext.md", markdown)
        _atomic(cache_dir / "pages.json", pages_json)
        _atomic(cache_dir / "meta.json", meta)
    except Exception as e:
        logger.warning(f"Failed to write MinerU cache for {attachment_key}: {e}")


def _read_cache(attachment_key: str, config: dict[str, Any]) -> ParseResult | None:
    """Load a cached parse result (assumes validity already checked)."""
    cache_dir = _cache_dir_for(attachment_key, config)
    try:
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
    backend_override: str | None = None,
) -> ParseResult | None:
    """Return a (possibly cached) MinerU parse of the whole PDF.

    Always parses the *entire* document (not a page range) so that subsequent
    reads of other pages hit the cache. Page-range slicing is the caller's job.

    Args:
        attachment_key: Zotero attachment key (the MinerU cache directory name).
        pdf_path: Path to the PDF on disk.
        config: The ``mineru`` config block from config.json.
        force_rebuild: Ignore any valid cache and re-parse.
        backend_override: Pin a single backend (e.g. ``"cloud"``, ``"pipeline"``,
            ``"hybrid"``) and skip the cross-backend fallback chain. None (the
            default) uses the configured backend with the full degradation chain.
            The cloud backend's internal vlm→pipeline model downgrade still
            applies — that is a model choice within cloud, not a backend crossing.

    Returns None if MinerU is unavailable or parsing fails — the caller should
    then fall back to PyMuPDF.
    """
    if not attachment_key or not pdf_path.exists():
        return None

    # 1. Cache hit? (Cache validity is backend-agnostic — a cached parse is
    #    reused regardless of which backend produced it or is being overridden.)
    if not force_rebuild and _cache_is_valid(attachment_key, pdf_path, config):
        cached = _read_cache(attachment_key, config)
        if cached is not None:
            return cached
        # Cache corrupt → drop and rebuild.
        _invalidate_cache(attachment_key, config)

    # 2. Parse whole document (start_page=0, end_page unset = all pages).
    parsed = _dispatch_parse(pdf_path, -1, -1, config, backend_override=backend_override)
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
    *,
    backend_override: str | None = None,
) -> tuple[str, list[dict] | None, str] | None:
    """Route to cloud / api / local-CLI backend per config.

    Degradation chain (most-preferred first), when ``backend_override`` is
    None (the default — full fallback):
      1. ``cloud`` — MinerU online API (mineru.net, vlm→pipeline), if token set
      2. ``api`` — local mineru-api server (/file_parse), if api_url set
      3. local CLI — hybrid-auto-engine → pipeline (inside _call_cli_with_fallback)

    When ``backend_override`` is set, the caller pinned a single backend and
    NO cross-backend fallback occurs — only that path runs, and a failure
    returns None (cloud's internal vlm→pipeline model downgrade still applies,
    since that is a model choice within the cloud backend, not a backend
    crossing). This lets 精读 callers force a specific extraction method.

    Returns (markdown, content_list, source_label) or None.
    """
    backend = _normalize_backend(backend_override or config.get("backend"))
    timeout = _resolve_timeout(config)
    pinned = backend_override is not None

    # 1. Cloud backend.
    cloud_token = config.get("cloud_token") or os.getenv("MINERU_API_TOKEN")
    if backend == "cloud" and cloud_token:
        cloud_model = config.get("cloud_model", "vlm")
        result = _call_mineru_cloud(pdf_path, start_page_0, end_page_0, cloud_token, cloud_model, timeout)
        if result is not None:
            return result
        if pinned:
            # Pinned cloud: don't fall through to local CLI.
            logger.info("MinerU cloud failed (pinned backend); not falling back.")
            return None
        logger.info("MinerU cloud failed; falling back to local CLI.")

    # 2. Local mineru-api backend (legacy /file_parse).
    if backend == "api":
        api_url = config.get("api_url")
        if api_url:
            result = _call_mineru_api(pdf_path, start_page_0, end_page_0, api_url, timeout)
            if result is not None:
                return result
            if pinned:
                logger.info("MinerU api failed (pinned backend); not falling back.")
                return None
            logger.info("MinerU api backend failed; falling back to local CLI.")
        else:
            logger.warning("MinerU backend=api but no api_url configured; trying local CLI.")
        # Fall through to local CLI (only when not pinned).

    if pinned:
        # Pinned local backend (hybrid/pipeline/etc.): run only that, no
        # GPU→pipeline fallback within the local CLI either.
        return _call_cli_with_fallback(
            pdf_path,
            start_page_0,
            end_page_0,
            config,
            timeout,
            backend=backend,
            no_pipeline_fallback=True,
        )

    return _call_cli_with_fallback(pdf_path, start_page_0, end_page_0, config, timeout)


def _invalidate_cache(attachment_key: str, config: dict[str, Any]) -> None:
    cache_dir = _cache_dir_for(attachment_key, config)
    shutil.rmtree(cache_dir, ignore_errors=True)


def read_cached_pages_joined(attachment_key: str, config: dict[str, Any] | None = None) -> str | None:
    """Return cached MinerU fulltext with form-feed page separators, or None.

    Cache-only read (no parse triggered). Reads ``pages.json`` (a
    ``list[str]``, one element per page) and joins with ``\\f`` so that
    ``semantic_search._page_for_offset`` can resolve a chunk's character
    offset back to a 1-indexed page number.

    Unlike the reverted ``read_cached_fulltext`` (which read the flat
    ``fulltext.md`` and lost page boundaries), this preserves page
    boundaries — essential for the "embedding定位 → MinerU精读验证"
    workflow where a semantic hit must report a page number the agent
    can pass to ``zotero_read_pdf_pages``.

    Returns None if no cache exists, the cache is unreadable, or
    ``pages.json`` is missing/invalid. The caller (``local_db``) treats
    None as "fall back to pdfminer".
    """
    if not attachment_key:
        return None
    cfg = config if config is not None else load_mineru_config()
    try:
        cache_dir = _cache_dir_for(attachment_key, cfg)
        pages_path = cache_dir / "pages.json"
        if not pages_path.exists():
            return None
        pages = json.loads(pages_path.read_text(encoding="utf-8", errors="replace"))
    except Exception as e:
        logger.debug(f"read_cached_pages_joined({attachment_key}) read failed: {e}")
        return None
    if not isinstance(pages, list) or not pages:
        return None
    # Filter out empty pages but keep page numbering contiguous — a
    # blank page in the middle still counts as a page boundary.
    joined = "\f".join(p if isinstance(p, str) else "" for p in pages)
    return joined if joined.strip() else None


def list_cached_attachment_keys(config: dict[str, Any] | None = None) -> set[str]:
    """Return the set of attachment keys that have a usable MinerU cache.

    Scans the MinerU cache root for subdirectories containing a non-empty
    ``pages.json`` (a parse that produced at least one page). Keys with only
    a partial/failed parse (no ``pages.json`` or empty list) are excluded so
    the batch reindex path doesn't try to build an index from nothing.

    Used by ``reindex_cached_mineru`` to find every item the user has 精读'd
    without keeping a separate registry: the cache directory *is* the registry.
    """
    cfg = config if config is not None else load_mineru_config()
    try:
        root = _resolve_cache_dir(cfg)
    except Exception:
        return set()
    if not root.is_dir():
        return set()
    keys: set[str] = set()
    for child in root.iterdir():
        if not child.is_dir():
            continue
        pages_path = child / "pages.json"
        if not pages_path.exists():
            continue
        # Mirror read_cached_pages_joined's validity gate: a cache entry
        # only counts when pages.json holds a non-empty list. This keeps
        # the "pending reindex" count honest — a half-written or corrupt
        # cache won't be reported as ready.
        try:
            pages = json.loads(pages_path.read_text(encoding="utf-8", errors="replace"))
            if isinstance(pages, list) and pages and any((p if isinstance(p, str) else "").strip() for p in pages):
                keys.add(child.name)
        except Exception:
            continue
    return keys
