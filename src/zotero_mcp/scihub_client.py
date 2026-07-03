"""Sci-Hub client for the PDF download cascade.

Sci-Hub (https://en.wikipedia.org/wiki/Sci-Hub) is a shadow library that
provides free access to paywalled academic papers. It is **opt-in** and
**disabled by default** — the user must explicitly set
``"scihub": {"enabled": true}`` in ``~/.config/zotero-mcp/config.json`` before
this module does anything. Enabling it is the user's choice; confirm your
jurisdiction's regulations before turning it on. This repo provides the
capability but does not enable it for you.

The module only *resolves* a PDF URL — it does not download the bytes.
The returned URL is passed to ``_helpers._download_and_attach_pdf``, which
applies the same SSRF guards used for every other PDF source (Unpaywall,
arXiv, etc.), so Sci-Hub PDFs go through the same safety net as the rest.

Config block (in ``~/.config/zotero-mcp/config.json``)::

    {
      "scihub": {
        "enabled": false,
        "domain": "sci-hub.ru"
      }
    }

The ``domain`` field is configurable because Sci-Hub's primary domain rotates
frequently; the default is ``sci-hub.ru`` but you can override it at any time
without changing code.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG_PATH = Path.home() / ".config" / "zotero-mcp" / "config.json"
DEFAULT_DOMAIN = "sci-hub.ru"

# Sci-Hub embeds the PDF in either an <iframe> or an <embed> tag. The page
# structure has been stable for years, but we keep the patterns here at the
# top of the module so they're easy to adjust if the markup shifts.
_IFRAME_RE = re.compile(r'<iframe[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)
_EMBED_RE = re.compile(r'<embed[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)
# Some Sci-Hub responses use a JS redirect: location.href='...' or
# window.location='...'. Treat that as a PDF URL too.
_JS_REDIRECT_RE = re.compile(r"location\.href\s*=\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Config loading (mirrors mineru_client.load_mineru_config)
# --------------------------------------------------------------------------- #
def load_scihub_config(config_path: str | Path | None = None) -> dict[str, Any]:
    """Return the ``scihub`` block from config.json, or an empty dict."""
    path = Path(config_path) if config_path else _DEFAULT_CONFIG_PATH
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg.get("scihub") or {}
    except Exception:
        return {}


def is_scihub_enabled(config: dict[str, Any] | None) -> bool:
    """True iff the user has explicitly enabled the Sci-Hub source."""
    return bool(config and config.get("enabled", False))


def get_scihub_domain(config: dict[str, Any]) -> str:
    """Return the configured Sci-Hub domain, falling back to the default."""
    raw = (config or {}).get("domain")
    if raw and isinstance(raw, str):
        return raw.strip().strip("/")
    return DEFAULT_DOMAIN


# --------------------------------------------------------------------------- #
# PDF URL resolution
# --------------------------------------------------------------------------- #
def find_pdf_url(identifier: str, ctx: Any) -> str | None:
    """Resolve a direct PDF URL for ``identifier`` from Sci-Hub.

    ``identifier`` is a DOI (``10.xxxx/yyy``) or an arXiv ID
    (``2401.12345``). Sci-Hub accepts both as the path segment of its
    lookup URL, e.g. ``https://sci-hub.ru/10.1038/nature12373``.

    Returns the PDF's direct URL (absolute, ready for
    ``_download_and_attach_pdf``), or ``None`` if Sci-Hub returned no
    usable link. The actual PDF bytes are fetched by the caller so the
    SSRF guard in ``_guarded_pdf_get`` applies uniformly to all sources.

    .. note::
        This function checks ``is_scihub_enabled`` itself; callers don't
        need to pre-gate. When disabled it returns ``None`` immediately.
    """
    if not identifier:
        return None

    config = load_scihub_config()
    if not is_scihub_enabled(config):
        return None

    domain = get_scihub_domain(config)
    base = f"https://{domain}"
    lookup_url = f"{base}/{identifier.strip()}"

    try:
        if ctx is not None and hasattr(ctx, "info"):
            ctx.info(f"Sci-Hub: querying {lookup_url}")
        else:
            logger.info("Sci-Hub: querying %s", lookup_url)
    except Exception:
        pass

    try:
        resp = requests.get(lookup_url, timeout=20, allow_redirects=True)
    except Exception as e:
        _log_failure(ctx, f"Sci-Hub request failed: {e}")
        return None

    if resp.status_code != 200:
        _log_failure(ctx, f"Sci-Hub returned HTTP {resp.status_code}")
        return None

    html = resp.text or ""
    # The iframe/embed src is often a relative path like "/downloads/...pdf"
    # or "//sci-hub.ru/..."; resolve it against the lookup URL's origin.
    page_origin = f"https://{domain}/"

    for pattern in (_IFRAME_RE, _EMBED_RE, _JS_REDIRECT_RE):
        m = pattern.search(html)
        if m:
            raw_src = m.group(1).strip()
            if not raw_src:
                continue
            absolute = urljoin(page_origin, raw_src)
            if absolute.startswith("http://"):
                absolute = "https://" + absolute[len("http://") :]
            try:
                if ctx is not None and hasattr(ctx, "info"):
                    ctx.info(f"Sci-Hub: found PDF at {absolute}")
                else:
                    logger.info("Sci-Hub: found PDF at %s", absolute)
            except Exception:
                pass
            return absolute

    _log_failure(ctx, "Sci-Hub page had no iframe/embed/redirect to a PDF")
    return None


def _log_failure(ctx: Any, message: str) -> None:
    try:
        if ctx is not None and hasattr(ctx, "info"):
            ctx.info(message)
        else:
            logger.info(message)
    except Exception:
        pass
