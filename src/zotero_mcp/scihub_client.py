"""Sci-Hub client for the PDF download cascade.

**GLOBALLY DISABLED** — this module is no longer wired into the PDF cascade
(``_helpers._try_attach_oa_pdf`` no longer imports or calls it). The code is
kept for reference in case someone wants to re-enable it in the future. The
cascade now uses: ADS (PUB_PDF then EPRINT_PDF) → arXiv → Unpaywall →
Semantic Scholar → PMC. For publisher PDFs blocked by WAFs, use
``zotero_upgrade_preprint_pdfs_via_browser`` with a live browser session.

--- Historical documentation (kept for reference) ---

Sci-Hub (https://en.wikipedia.org/wiki/Sci-Hub) is a shadow library that
provides free access to paywalled academic papers. It was **opt-in** and
**disabled by default** — the user had to explicitly set
``"scihub": {"enabled": true}`` in ``~/.config/zotero-mcp/config.json`` before
this module did anything. Enabling it was the user's choice; confirm your
jurisdiction's regulations before turning it on. This repo provided the
capability but did not enable it for you and offered no legal advice.

The module only *resolves* a PDF URL — it does not download the bytes.
The returned URL was passed to ``_helpers._download_and_attach_pdf``, which
applies the same SSRF guards used for every other PDF source (Unpaywall,
arXiv, etc.), so Sci-Hub PDFs went through the same safety net as the rest.

Config block (in ``~/.config/zotero-mcp/config.json``)::

    {
      "scihub": {
        "enabled": false,
        "domain": "sci-hub.ee"
      }
    }

The ``domain`` field is configurable because Sci-Hub's primary domain rotates
frequently; the default is ``sci-hub.ee`` (the POST-form endpoint that does
not trigger the captcha deployed on some other mirrors). Override it at any
time without changing code.

Resolution strategy
-------------------
The first attempt uses a **POST** request to the configured domain's form
action with a ``request=<identifier>`` payload and a randomized browser
User-Agent — this is how the upstream ``scihub`` PyPI library successfully
resolves PDFs on ``sci-hub.ee`` without hitting the altcha JS captcha that
the ``sci-hub.ru`` / ``sci-hub.jp`` mirrors deploy against plain GET requests.
The response HTML contains an ``#pdf`` element whose ``src`` attribute is the
direct PDF URL. If the POST path yields nothing, a GET fallback (parsing
``<iframe>`` / ``<embed>`` / JS redirect) is tried as a last resort.
"""

from __future__ import annotations

import json
import logging
import random
import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG_PATH = Path.home() / ".config" / "zotero-mcp" / "config.json"
DEFAULT_DOMAIN = "sci-hub.ee"

# A small pool of real browser User-Agent strings. Sci-Hub's captcha is
# triggered partly by non-browser UA strings (e.g. python-requests/x.y), so
# sending a randomized browser UA is what makes the POST path succeed.
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]

# Sci-Hub embeds the PDF in an element with id="pdf" (POST response), or in an
# <iframe>/<embed> tag (older GET pages). We try all three patterns.
_PDF_ID_RE = re.compile(r'<[^>]+id=["\']pdf["\'][^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)
_IFRAME_RE = re.compile(r'<iframe[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)
_EMBED_RE = re.compile(r'<embed[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)
# Some Sci-Hub responses use a JS redirect: location.href='...'
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
def _random_ua() -> str:
    return random.choice(_USER_AGENTS)


def _resolve_pdf_url_from_html(html: str, base_url: str) -> str | None:
    """Extract a PDF URL from a Sci-Hub HTML response.

    Tries the ``#pdf`` element (POST response), then ``<iframe>``,
    ``<embed>``, and a JS ``location.href`` redirect. Relative URLs are
    resolved against ``base_url``. Returns ``None`` if no pattern matches.
    """
    page_origin = f"{urlparse(base_url).scheme}://{urlparse(base_url).hostname}/"
    for pattern in (_PDF_ID_RE, _IFRAME_RE, _EMBED_RE, _JS_REDIRECT_RE):
        m = pattern.search(html)
        if m:
            raw_src = m.group(1).strip()
            if not raw_src:
                continue
            absolute = urljoin(page_origin, raw_src)
            if absolute.startswith("http://"):
                absolute = "https://" + absolute[len("http://") :]
            return absolute
    return None


def _resolve_form_action(html: str, base_url: str) -> str:
    """Extract the form action URL from a Sci-Hub landing page.

    The ``sci-hub.ee`` home page contains ``<form method = "POST"
    action ="https://www.tesble.com/">`` — the actual POST target is a
    different domain. We must GET the landing page first, parse the form
    action, then POST there. Direct POST to the landing domain returns 403.
    Note: the markup has spaces around ``=`` (``method = "POST"``), so the
    regex tolerates that. Falls back to ``base_url`` when no form action found.
    """
    pattern = re.compile(
        r'<form[^>]*method\s*=\s*["\']\s*POST\s*["\'][^>]*action\s*=\s*["\']([^"\']+)["\']',
        re.IGNORECASE,
    )
    m = pattern.search(html)
    if m and m.group(1).strip():
        return m.group(1).strip()
    # action may appear before method in the tag.
    pattern2 = re.compile(
        r'<form[^>]*action\s*=\s*["\']([^"\']+)["\'][^>]*method\s*=\s*["\']\s*POST\s*["\']',
        re.IGNORECASE,
    )
    m2 = pattern2.search(html)
    if m2 and m2.group(1).strip():
        return m2.group(1).strip()
    return base_url


def _try_post(identifier: str, domain: str, ctx: Any) -> str | None:
    """Resolve a PDF URL via Sci-Hub's POST form (the captcha-free path).

    The ``sci-hub.ee`` mirror's landing page contains a ``<form method="POST"
    action="https://www.tesble.com/">`` pointing to a *different* domain.
    We therefore GET the landing page first, parse the form action, then POST
    the ``request=<identifier>`` payload to that action URL with a randomized
    browser User-Agent. This mirrors the upstream ``scihub`` PyPI library's
    approach, which succeeds on ``sci-hub.ee`` where the GET path on
    ``sci-hub.ru`` triggers an altcha JS captcha. Direct POST to the landing
    domain (without resolving the form action) returns 403.
    """
    base_url = f"https://{domain}/"
    headers = {"User-Agent": _random_ua()}

    # Step 1: GET the landing page to find the form action URL.
    try:
        landing = requests.get(base_url, headers=headers, timeout=15, allow_redirects=True)
    except Exception as e:
        _log_failure(ctx, f"Sci-Hub landing GET failed: {e}")
        return None
    if landing.status_code != 200:
        _log_failure(ctx, f"Sci-Hub landing GET returned HTTP {landing.status_code}")
        return None
    post_url = _resolve_form_action(landing.text or "", base_url)

    # Step 2: POST the request payload to the form action URL.
    payload = {"sci-hub-plugin-check": "", "request": identifier}
    try:
        _log_info(ctx, f"Sci-Hub: POST {post_url} (request={identifier})")
    except Exception:
        pass
    try:
        resp = requests.post(post_url, data=payload, headers=headers, timeout=20, allow_redirects=True)
    except Exception as e:
        _log_failure(ctx, f"Sci-Hub POST failed: {e}")
        return None
    if resp.status_code != 200:
        _log_failure(ctx, f"Sci-Hub POST returned HTTP {resp.status_code}")
        return None
    html = resp.text or ""
    final_url = resp.url or base_url
    pdf_url = _resolve_pdf_url_from_html(html, final_url)
    if pdf_url:
        _log_info(ctx, f"Sci-Hub: POST found PDF at {pdf_url}")
    else:
        _log_failure(ctx, "Sci-Hub POST response had no #pdf/iframe/embed/redirect")
    return pdf_url


def _try_get(identifier: str, domain: str, ctx: Any) -> str | None:
    """Fallback: resolve a PDF URL via a plain GET ``/{identifier}``.

    Some mirrors accept GET without a captcha; this is the last-resort path
    used when POST yields nothing.
    """
    lookup_url = f"https://{domain}/{identifier.strip()}"
    headers = {"User-Agent": _random_ua()}
    try:
        _log_info(ctx, f"Sci-Hub: GET {lookup_url}")
    except Exception:
        pass
    try:
        resp = requests.get(lookup_url, headers=headers, timeout=20, allow_redirects=True)
    except Exception as e:
        _log_failure(ctx, f"Sci-Hub GET failed: {e}")
        return None
    if resp.status_code != 200:
        _log_failure(ctx, f"Sci-Hub GET returned HTTP {resp.status_code}")
        return None
    html = resp.text or ""
    final_url = resp.url or lookup_url
    pdf_url = _resolve_pdf_url_from_html(html, final_url)
    if pdf_url:
        _log_info(ctx, f"Sci-Hub: GET found PDF at {pdf_url}")
    else:
        _log_failure(ctx, "Sci-Hub GET response had no #pdf/iframe/embed/redirect")
    return pdf_url


def find_pdf_url(identifier: str, ctx: Any) -> str | None:
    """Resolve a direct PDF URL for ``identifier`` from Sci-Hub.

    ``identifier`` is a DOI (``10.xxxx/yyy``) or an arXiv ID
    (``2401.12345``). Sci-Hub accepts both as the ``request`` field of its
    POST form.

    Returns the PDF's direct URL (absolute, ready for
    ``_download_and_attach_pdf``), or ``None`` if Sci-Hub returned no
    usable link. The actual PDF bytes are fetched by the caller so the
    SSRF guard in ``_guarded_pdf_get`` applies uniformly to all sources.

    The POST path is tried first (it avoids the captcha on some mirrors);
    if it yields nothing, a GET fallback is attempted.

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
    identifier = identifier.strip()

    # 1. POST path (captcha-free on sci-hub.ee).
    pdf_url = _try_post(identifier, domain, ctx)
    if pdf_url:
        return pdf_url

    # 2. GET fallback (last resort; some mirrors accept GET without captcha).
    return _try_get(identifier, domain, ctx)


# --------------------------------------------------------------------------- #
# Logging helpers (tolerate a missing ctx, e.g. in CLI scripts)
# --------------------------------------------------------------------------- #
def _log_info(ctx: Any, message: str) -> None:
    try:
        if ctx is not None and hasattr(ctx, "info"):
            ctx.info(message)
        else:
            logger.info(message)
    except Exception:
        pass


def _log_failure(ctx: Any, message: str) -> None:
    _log_info(ctx, message)
