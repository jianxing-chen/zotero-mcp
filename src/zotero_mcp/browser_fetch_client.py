"""Browser-session publisher PDF fetcher via Chrome/Edge DevTools.

Reuses a live, already-authorized browser session (launched with
``--remote-debugging-port``) to download publisher-version PDFs that are
blocked by WAFs/captchas when fetched via plain ``requests.get``.

The browser carries the user's real session cookies, fingerprint, and
institutional authorization. The fetch is executed inside the page context
via ``fetch(url, {credentials: 'include'})`` or extracted from the
in-browser PDF.js viewer — so the request looks like a normal in-page
navigation, not a bot download.

Legal boundary: this module does NOT bypass paywalls, CAPTCHA, or
institutional gates. The user must manually sign in and pass any
verification in the browser window before calling the tool. It only
fetches PDFs the user is already authorized to access.

Adapted from https://github.com/Given-Dream/sciencedirect-live-session-fetcher
(used as reference; this is a trimmed reimplementation).
"""

from __future__ import annotations

import base64
import html
import json
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urljoin, urlparse
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG_PATH = Path.home() / ".config" / "zotero-mcp" / "config.json"

_DEFAULT_BROWSER_FETCH_CONFIG = {
    "enabled": False,
    "debug_port": 9222,
    "page_wait_seconds": 8,
    "inter_item_sleep_seconds": 6,
}


def load_browser_fetch_config(config_path: str | Path | None = None) -> dict[str, Any]:
    """Read the ``browser_fetch`` block from config.json.

    Mirrors ``scihub_client.load_scihub_config``. Returns the block with
    defaults filled in, or an empty dict when disabled / missing.
    """
    path = Path(config_path) if config_path else _DEFAULT_CONFIG_PATH
    if not path.exists():
        return dict(_DEFAULT_BROWSER_FETCH_CONFIG)
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f) or {}
    except (json.JSONDecodeError, OSError):
        return dict(_DEFAULT_BROWSER_FETCH_CONFIG)
    block = cfg.get("browser_fetch") or {}
    if not isinstance(block, dict):
        return dict(_DEFAULT_BROWSER_FETCH_CONFIG)
    # Merge defaults so callers can rely on keys being present.
    merged = dict(_DEFAULT_BROWSER_FETCH_CONFIG)
    merged.update(block)
    return merged


def is_browser_fetch_enabled(config: dict[str, Any] | None) -> bool:
    """True iff the user has explicitly enabled the browser-fetch feature."""
    return bool(config and config.get("enabled", False))


# ---------------------------------------------------------------------------
# ScienceDirect pdfDownload metadata regex
# ---------------------------------------------------------------------------

# Matches the JSON blob ScienceDirect embeds in the article page HTML:
#   "pdfDownload":{"isPdfFullText":false,"urlMetadata":{"queryParams":
#   {"md5":"...","pid":"..."},"pii":"...","pdfExtension":".pdf","path":"..."}}
_SD_PDF_RE = re.compile(
    r'"pdfDownload":\{"isPdfFullText":(?:true|false),"urlMetadata":\{"queryParams":\{"md5":"([^"]+)","pid":"([^"]+)"\},"pii":"([^"]+)","pdfExtension":"([^"]+)","path":"([^"]+)"\}\}'
)

# Short-lived signed PDF URL on sciencedirectassets.com.
_SIGNED_PDF_RE = re.compile(r"https://pdf\.sciencedirectassets\.com/[^\s\"'<>]+", re.I)


# ---------------------------------------------------------------------------
# DevTools client
# ---------------------------------------------------------------------------


class DevToolsClient:
    """Minimal Chrome/Edge DevTools remote-debugging client.

    Uses only stdlib ``urllib`` for the HTTP endpoints and lazily imports
    ``websocket`` (from the ``websocket-client`` package) for the CDP
    WebSocket calls. The module is importable without ``websocket-client``
    installed; the import error surfaces only when ``call()`` is used.
    """

    def __init__(self, debug_port: int = 9222, *, host: str = "127.0.0.1") -> None:
        self.base = f"http://{host}:{debug_port}"

    def http_get(self, url: str, method: str = "GET", timeout: float = 20) -> str:
        req = Request(url, method=method)
        with urlopen(req, timeout=timeout) as resp:  # noqa: S310 — localhost DevTools
            return resp.read().decode("utf-8")

    def is_reachable(self) -> bool:
        """True iff the DevTools endpoint responds (browser is running)."""
        try:
            self.http_get(f"{self.base}/json/version")
            return True
        except Exception:
            return False

    def open_page(self, url: str) -> dict:
        """Open a new tab at ``url``; returns the page descriptor."""
        raw = self.http_get(f"{self.base}/json/new?{quote(url, safe=':/?&=%')}", method="PUT")
        return json.loads(raw)

    def close_page(self, page_id: str) -> None:
        try:
            self.http_get(f"{self.base}/json/close/{page_id}")
        except Exception:
            pass

    def list_pages(self) -> list[dict]:
        return json.loads(self.http_get(f"{self.base}/json"))

    def call(self, ws_url: str, method: str, params: dict | None = None, msg_id: int = 1) -> dict:
        """Send a CDP command over WebSocket and wait for the matching reply."""
        import websocket  # deferred — only needed when actually driving the browser

        ws = websocket.create_connection(ws_url, timeout=180, suppress_origin=True)
        try:
            ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
            while True:
                msg = json.loads(ws.recv())
                if msg.get("id") == msg_id:
                    return msg
        finally:
            ws.close()

    def evaluate(self, ws_url: str, expression: str, *, await_promise: bool = False, msg_id: int = 1):
        """Run ``expression`` in the page; return the JS value or None."""
        msg = self.call(
            ws_url,
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": await_promise,
            },
            msg_id=msg_id,
        )
        if "error" in msg:
            return None
        return msg.get("result", {}).get("result", {}).get("value")


# ---------------------------------------------------------------------------
# Challenge / bot-verification detection
# ---------------------------------------------------------------------------


def is_challenge_page(title: str, html_text: str, current_url: str) -> bool:
    """Detect a bot-verification / challenge / sign-in redirect page.

    Publishers (Elsevier, Cloudflare-fronted sites) gate article pages with
    "Please wait" / "tdm-reservation" / Cloudflare challenge / id.elsevier.com
    redirects when the session isn't fully authorized. The caller should skip
    the item and tell the user to complete verification in the browser.
    """
    lowered = (title + " " + current_url + " " + (html_text or "")[:2000]).lower()
    if "请稍候" in (title or ""):
        return True
    if "please wait" in lowered:
        return True
    if "tdm-reservation" in lowered:
        return True
    if "challenges.cloudflare.com" in lowered:
        return True
    if "id.elsevier.com" in (current_url or "").lower():
        return True
    return False


# ---------------------------------------------------------------------------
# PDF URL discovery (generic publisher fallback)
# ---------------------------------------------------------------------------


def _score_pdf_url(url: str, text: str, source: str = "") -> int:
    """Heuristic score for ranking candidate PDF URLs on a publisher page."""
    lowered = " ".join((url, text, source)).lower()
    score = 0
    if "citation_pdf_url" in lowered:
        score += 50
    if ".pdf" in lowered:
        score += 40
    if "/pdf" in lowered or "pdf/" in lowered:
        score += 30
    if "download" in lowered:
        score += 20
    if "pdf" in lowered:
        score += 10
    if any(noise in lowered for noise in ("privacy", "cookie", "terms", "cover-image")):
        score -= 40
    return score


def _normalize_pdf_url(current_url: str, value: str) -> str:
    value = (value or "").strip()
    if not value or value.startswith(("javascript:", "mailto:")):
        return ""
    return urljoin(current_url, html.unescape(value))


def find_generic_pdf_url(devtools: DevToolsClient, ws_url: str, current_url: str, *, msg_id: int = 41) -> str:
    """Scrape a publisher article page for the best PDF URL candidate.

    Checks ``citation_pdf_url`` meta tags, then ``<a href>``, ``<iframe src>``,
    ``<embed src>``, ``<object data>`` elements. Returns the highest-scoring
    absolute URL, or ``""`` if none found.
    """
    candidates = devtools.evaluate(
        ws_url,
        """
(() => {
  const out = [];
  const push = (url, text, source) => {
    if (url) out.push({url: String(url), text: String(text || ''), source});
  };
  document.querySelectorAll('meta').forEach(meta => {
    const key = (meta.getAttribute('name') || meta.getAttribute('property') || '').toLowerCase();
    if (key.includes('citation_pdf_url') || key.includes('bepress_citation_pdf_url')) {
      push(meta.getAttribute('content'), key, 'meta');
    }
  });
  document.querySelectorAll('a[href], iframe[src], embed[src], object[data]').forEach(el => {
    const url = el.getAttribute('href') || el.getAttribute('src') || el.getAttribute('data');
    const text = (el.innerText || el.getAttribute('aria-label') || el.getAttribute('title') || '').trim();
    push(url, text, el.tagName.toLowerCase());
  });
  return out;
})()
        """.strip(),
        msg_id=msg_id,
    )
    ranked: list[tuple[int, str]] = []
    for item in candidates or []:
        url = _normalize_pdf_url(current_url, item.get("url", ""))
        if not url:
            continue
        score = _score_pdf_url(url, item.get("text", ""), item.get("source", ""))
        if score >= 30:
            ranked.append((score, url))
    if not ranked:
        return ""
    ranked.sort(reverse=True)
    return ranked[0][1]


# ---------------------------------------------------------------------------
# PDF byte extraction (in-page fetch + PDF.js viewer)
# ---------------------------------------------------------------------------


def fetch_pdf_in_page_context(
    devtools: DevToolsClient, ws_url: str, pdf_url: str, *, msg_id: int = 31
) -> bytes | None:
    """``fetch(pdf_url, {credentials: 'include'})`` inside the authorized page.

    This is the key WAF-bypass: the request carries the page's real cookies
    and origin. Returns the PDF bytes (validated by ``%PDF-`` magic) or None.

    When ``pdf_url`` is ``"location.href"``, the fetch uses the page's own
    URL (``location.href``) instead of a literal string. This matters when
    the PDF was loaded via a redirect to a different domain (e.g. OUP ->
    silverchair CDN) — fetching ``location.href`` from the redirected tab's
    context is same-origin and avoids CORS, whereas fetching the original
    URL string from the redirected tab would be cross-origin.
    """
    # When pdf_url is "location.href", use the page's own URL in the fetch
    # to stay same-origin after a redirect.
    fetch_arg = "location.href" if pdf_url == "location.href" else json.dumps(pdf_url)
    value = devtools.evaluate(
        ws_url,
        f"""
new Promise(resolve => {{
  fetch({fetch_arg}, {{ credentials: 'include' }})
    .then(resp => resp.arrayBuffer())
    .then(buf => {{
      const data = new Uint8Array(buf);
      const chunk = 0x8000;
      let binary = '';
      for (let i = 0; i < data.length; i += chunk) {{
        binary += String.fromCharCode.apply(null, data.subarray(i, i + chunk));
      }}
      resolve(btoa(binary));
    }})
    .catch(err => resolve('ERR:' + String(err)));
}})
        """.strip(),
        await_promise=True,
        msg_id=msg_id,
    )
    if not value or (isinstance(value, str) and value.startswith("ERR:")):
        return None
    try:
        data = base64.b64decode(value)
    except Exception:
        return None
    return data if data.startswith(b"%PDF-") else None


def extract_pdf_from_pdfjs_viewer(
    devtools: DevToolsClient, ws_url: str, *, timeout_s: float = 15.0, msg_id: int = 21
) -> bytes | None:
    """Extract PDF bytes from the browser's built-in PDF.js viewer.

    When the browser opens a PDF directly, it loads PDF.js. We poll
    ``window.PDFViewerApplication.pdfDocument`` and call ``.getData()`` to
    pull the raw bytes. Falls back when in-page ``fetch`` is blocked.
    """
    value = devtools.evaluate(
        ws_url,
        f"""
new Promise(resolve => {{
  const deadline = Date.now() + {int(timeout_s * 1000)};
  const tick = () => {{
    const app = window.PDFViewerApplication;
    if (app && app.pdfDocument) {{
      app.pdfDocument.getData().then(data => {{
        const chunk = 0x8000;
        let binary = '';
        for (let i = 0; i < data.length; i += chunk) {{
          binary += String.fromCharCode.apply(null, data.subarray(i, i + chunk));
        }}
        resolve(btoa(binary));
      }}).catch(err => resolve('ERR:' + String(err)));
    }} else if (Date.now() > deadline) {{
      resolve('ERR:timeout_waiting_for_pdf_viewer');
    }} else {{
      setTimeout(tick, 1000);
    }}
  }};
  tick();
}})
        """.strip(),
        await_promise=True,
        msg_id=msg_id,
    )
    if not value or (isinstance(value, str) and value.startswith("ERR:")):
        return None
    try:
        data = base64.b64decode(value)
    except Exception:
        return None
    return data if data.startswith(b"%PDF-") else None


# ---------------------------------------------------------------------------
# Signed-URL extraction helpers (ScienceDirect)
# ---------------------------------------------------------------------------


def _extract_file_param_from_viewer_url(url: str) -> str:
    """If the URL is an ``extension://.../pdfjs/web/viewer.html?file=...``,
    return the ``file`` param (the real PDF URL)."""
    parsed = urlparse(url)
    if parsed.scheme not in {"chrome-extension", "extension"}:
        return ""
    values = parse_qs(parsed.query).get("file") or []
    return values[0] if values else ""


def _extract_signed_pdf_url_from_text(text: str) -> str:
    if not text:
        return ""
    decoded = html.unescape(text)
    match = _SIGNED_PDF_RE.search(decoded)
    return match.group(0) if match else ""


# ---------------------------------------------------------------------------
# Main per-item fetch
# ---------------------------------------------------------------------------


def fetch_publisher_pdf_via_browser(
    devtools: DevToolsClient,
    article_url: str,
    *,
    page_wait_seconds: int = 8,
) -> tuple[bytes | None, str, str]:
    """Fetch a publisher PDF through the live browser session.

    Args:
        devtools: connected DevToolsClient.
        article_url: the article landing URL (typically ``https://doi.org/<doi>``).
        page_wait_seconds: seconds to sleep after opening each page.

    Returns ``(pdf_bytes, source_url, label)``:
    - On success: ``(bytes, url_or_route, method_label)``.
    - On failure: ``(None, url_or_route, reason)``.
    """
    import time as _time

    article_page = None
    pdf_page = None
    try:
        article_page = devtools.open_page(article_url)
        _time.sleep(page_wait_seconds)
        ws_url = article_page["webSocketDebuggerUrl"]
        article_html = devtools.evaluate(ws_url, "document.documentElement.outerHTML", msg_id=10) or ""
        title = devtools.evaluate(ws_url, "document.title", msg_id=11) or ""
        current_url = devtools.evaluate(ws_url, "location.href", msg_id=12) or article_url

        if is_challenge_page(title, article_html, current_url):
            return None, current_url, "challenge_page"

        # 1. ScienceDirect: extract pdfDownload metadata.
        match = _SD_PDF_RE.search(article_html)
        if match:
            md5, pid, pii, pdf_ext, path = match.groups()
            pdf_url = f"https://www.sciencedirect.com/{path}/{pii}{pdf_ext}?md5={md5}&pid={pid}"
            pdf_page = devtools.open_page(pdf_url)
            _time.sleep(page_wait_seconds)
            pdf_ws = pdf_page["webSocketDebuggerUrl"]
            viewer_url = devtools.evaluate(pdf_ws, "location.href", msg_id=20) or ""
            viewer_html = devtools.evaluate(pdf_ws, "document.documentElement.outerHTML", msg_id=22) or ""
            signed_pdf_url = _extract_file_param_from_viewer_url(viewer_url)
            if not signed_pdf_url:
                signed_pdf_url = (
                    _extract_signed_pdf_url_from_text(viewer_url)
                    or _extract_signed_pdf_url_from_text(viewer_html)
                )
            if signed_pdf_url:
                pdf_url = signed_pdf_url

            # Try in-page fetch from article context first (carries article cookies),
            # then from the PDF viewer tab, then PDF.js extraction.
            pdf_bytes = fetch_pdf_in_page_context(devtools, ws_url, pdf_url, msg_id=30)
            if not pdf_bytes:
                pdf_bytes = fetch_pdf_in_page_context(devtools, pdf_ws, pdf_url, msg_id=31)
            if pdf_bytes:
                return pdf_bytes, pdf_url, "sd_in_page_fetch"
            # Cross-domain redirect fallback: fetch location.href from the PDF tab.
            pdf_bytes = fetch_pdf_in_page_context(devtools, pdf_ws, "location.href", msg_id=35)
            if pdf_bytes:
                return pdf_bytes, pdf_url, "sd_pdf_self_fetch"
            pdf_bytes = extract_pdf_from_pdfjs_viewer(devtools, pdf_ws)
            if pdf_bytes:
                return pdf_bytes, pdf_url, "sd_pdfjs_extract"
            return None, pdf_url, "sd_pdf_fetch_failed"

        # 2. Generic publisher: scrape citation_pdf_url / iframe / link.
        generic_pdf_url = find_generic_pdf_url(devtools, ws_url, current_url, msg_id=13)
        if not generic_pdf_url:
            return None, current_url, "no_pdf_metadata"

        pdf_bytes = fetch_pdf_in_page_context(devtools, ws_url, generic_pdf_url, msg_id=40)
        if pdf_bytes:
            return pdf_bytes, generic_pdf_url, "generic_in_page_fetch"

        # Open the PDF URL in a new tab and try fetch + PDF.js from there.
        pdf_page = devtools.open_page(generic_pdf_url)
        _time.sleep(page_wait_seconds)
        pdf_ws = pdf_page["webSocketDebuggerUrl"]
        viewer_url = devtools.evaluate(pdf_ws, "location.href", msg_id=70) or ""
        nested_url = (
            _extract_file_param_from_viewer_url(viewer_url)
            or _extract_signed_pdf_url_from_text(
                devtools.evaluate(pdf_ws, "document.documentElement.outerHTML", msg_id=71) or ""
            )
            or find_generic_pdf_url(devtools, pdf_ws, viewer_url or generic_pdf_url, msg_id=72)
        )
        for candidate in (nested_url, viewer_url, generic_pdf_url):
            if not candidate:
                continue
            pdf_bytes = fetch_pdf_in_page_context(devtools, pdf_ws, candidate, msg_id=80)
            if pdf_bytes:
                return pdf_bytes, candidate, "generic_pdf_context_fetch"
        # Last resort: fetch location.href from the PDF tab's own context.
        # This handles cross-domain redirects (e.g. OUP -> silverchair CDN)
        # where fetching the original URL string would be CORS-blocked but
        # fetching location.href (the redirected URL) is same-origin.
        pdf_bytes = fetch_pdf_in_page_context(devtools, pdf_ws, "location.href", msg_id=85)
        if pdf_bytes:
            return pdf_bytes, viewer_url or generic_pdf_url, "generic_pdf_self_fetch"
        pdf_bytes = extract_pdf_from_pdfjs_viewer(devtools, pdf_ws)
        if pdf_bytes:
            return pdf_bytes, generic_pdf_url, "generic_pdfjs_extract"
        return None, generic_pdf_url, "generic_pdf_fetch_failed"
    except Exception as e:
        logger.warning(f"Browser fetch failed for {article_url}: {e}")
        return None, article_url, f"error: {e}"
    finally:
        if article_page:
            devtools.close_page(article_page["id"])
        if pdf_page:
            devtools.close_page(pdf_page["id"])
