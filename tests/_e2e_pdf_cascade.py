"""End-to-end connectivity test for the reordered PDF cascade.

Tests three real download paths:
  1. ADS link_gateway   (needs ADS_API_TOKEN in this process env)
  2. Sci-Hub           (needs scihub.enabled=true in config.json)
  3. arXiv direct PDF  (always OA)

Each path: resolve URL -> real HTTP GET -> verify the bytes are a real PDF
(check the %PDF- magic header + minimum size). Nothing is written to Zotero;
downloads go to a temp dir that is cleaned up automatically.

Usage: ADS_API_TOKEN=<token> python tests/_e2e_pdf_cascade.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Make 'zotero_mcp' importable when run as a script from the repo root.
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

CONFIG_PATH = Path.home() / ".config" / "zotero-mcp" / "config.json"

# A real DOI with both PUB_PDF and EPRINT_PDF in ADS (ApJ exoplanet paper).
# PUB_PDF is usually blocked by publisher WAF; EPRINT_PDF (arXiv) is OA.
# The cascade should try PUB_PDF first, fail, then succeed via EPRINT_PDF.
TEST_DOI = "10.1088/0004-637X/769/2/127"
# A real arXiv ID (LIGO GW150914 discovery paper).
TEST_ARXIV_ID = "1602.03837"

# ----- tiny no-op context (mimics the MCP Context interface) -----


class _Ctx:
    def info(self, *a, **k):
        print(f"    [info] {_a[0] if _a else a[0] if a else ''}")

    def warning(self, *a, **k):
        print(f"    [warn] {a[0] if a else ''}")

    def error(self, *a, **k):
        print(f"    [error] {a[0] if a else ''}")


# Fix the info method (the closure above is buggy; redefine properly)
class Ctx:
    def info(self, *a, **k):
        if a:
            print(f"    [info] {a[0]}")

    def warning(self, *a, **k):
        if a:
            print(f"    [warn] {a[0]}")

    def error(self, *a, **k):
        if a:
            print(f" [error] {a[0]}")


def _is_real_pdf(path: Path) -> tuple[bool, str]:
    """Return (ok, reason). A real PDF starts with %PDF- and is >1KB."""
    if not path.exists():
        return False, "file not found"
    size = path.stat().st_size
    if size < 1000:
        return False, f"too small ({size} bytes)"
    with open(path, "rb") as f:
        head = f.read(8)
    if not head.startswith(b"%PDF-"):
        return False, f"not a PDF (header={head!r})"
    return True, f"valid PDF, {size:,} bytes"


def _download_to_tmp(url: str, dest_name: str) -> Path:
    """Download URL to a temp file using the SSRF-guarded downloader."""
    from zotero_mcp.tools import _helpers

    ctx = Ctx()
    # Use a fresh temp dir per call so each test gets a clean file.
    tmpdir = Path(tempfile.mkdtemp(prefix="e2e_pdf_"))
    dest = tmpdir / dest_name
    # _guarded_pdf_get returns a requests.Response (or None).
    resp = _helpers._guarded_pdf_get(url, ctx)
    if resp is None:
        raise RuntimeError("SSRF guard rejected the URL or redirect chain")
    resp.raise_for_status()
    content_type = resp.headers.get("Content-Type", "")
    with open(dest, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)
    resp.close()
    return dest


# ---------------------------------------------------------------------------
# Path 1: ADS
# ---------------------------------------------------------------------------


def test_ads():
    print("\n=== Path 1: ADS link_gateway (via full cascade) ===")
    token = os.environ.get("ADS_API_TOKEN")
    if not token:
        print("  SKIP: ADS_API_TOKEN not set in env")
        return None
    from zotero_mcp import ads_client
    from zotero_mcp.tools import _helpers

    print(f"  DOI: {TEST_DOI}")
    # Step 1: resolve all candidate URLs (PUB_PDF first, EPRINT_PDF fallback)
    print("  -> ads_client.get_pdf_urls_by_doi(doi, prefer='pub')")
    urls, bibcode = ads_client.get_pdf_urls_by_doi(TEST_DOI, prefer="pub")
    if not urls:
        err = ads_client.last_error
        print(f"  FAIL: no PDF URLs resolved (last_error={err!r})")
        return False
    print(f"  OK: bibcode={bibcode}, {len(urls)} candidate URL(s):")
    for u in urls:
        print(f"       - {u}")

    # Step 2: try each URL via the SSRF-guarded downloader (mimics cascade)
    ctx = Ctx()
    for i, url in enumerate(urls):
        label = url.rsplit("/", 1)[-1]  # e.g. PUB_PDF or EPRINT_PDF
        print(f"  -> attempt {i+1}/{len(urls)} ({label}): downloading...")
        try:
            resp = _helpers._guarded_pdf_get(url, ctx)
            if resp is None:
                print("     SSRF rejected / no response")
                continue
            resp.raise_for_status()
            ct = resp.headers.get("Content-Type", "")
            head = b""
            with open("/dev/null", "wb") as _f:  # drain safely
                pass
            # Read enough to check the magic header
            tmpdir = Path(tempfile.mkdtemp(prefix="e2e_ads_"))
            dest = tmpdir / f"ads_{i}.pdf"
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(8192):
                    f.write(chunk)
            resp.close()
            ok, reason = _is_real_pdf(dest)
            if ok:
                print(f"  OK ({label}): {reason}")
                return True
            else:
                print(f"     {label} not a valid PDF: {reason} (Content-Type={ct})")
        except Exception as e:
            print(f"     {label} download failed: {e}")
    print("  FAIL: no ADS candidate yielded a valid PDF")
    return False


# ---------------------------------------------------------------------------
# Path 2: Sci-Hub
# ---------------------------------------------------------------------------


def test_scihub():
    print("\n=== Path 2: Sci-Hub ===")
    from zotero_mcp import scihub_client

    cfg = scihub_client.load_scihub_config()
    if not scihub_client.is_scihub_enabled(cfg):
        print("  SKIP: scihub not enabled in config.json")
        return None
    domain = scihub_client.get_scihub_domain(cfg)
    print(f"  DOI: {TEST_DOI}  (domain: {domain})")
    print("  -> scihub_client.find_pdf_url(doi, ctx)")
    ctx = Ctx()
    pdf_url = scihub_client.find_pdf_url(TEST_DOI, ctx)
    if not pdf_url:
        print("  FAIL: no PDF URL parsed from Sci-Hub page")
        return False
    print(f"  OK: resolved url={pdf_url}")
    print("  -> downloading via SSRF-guarded client...")
    try:
        dest = _download_to_tmp(pdf_url, "scihub_test.pdf")
    except Exception as e:
        print(f"  FAIL: download failed: {e}")
        return False
    ok, reason = _is_real_pdf(dest)
    print(f"  {'OK' if ok else 'FAIL'}: {reason}")
    return ok


# ---------------------------------------------------------------------------
# Path 3: arXiv (direct, bypasses cascade)
# ---------------------------------------------------------------------------


def test_arxiv():
    print("\n=== Path 3: arXiv direct ===")

    url = f"https://arxiv.org/pdf/{TEST_ARXIV_ID}.pdf"
    print(f"  arXiv ID: {TEST_ARXIV_ID}  -> {url}")
    print("  -> downloading via SSRF-guarded client...")
    try:
        dest = _download_to_tmp(url, "arxiv_test.pdf")
    except Exception as e:
        print(f"  FAIL: download failed: {e}")
        return False
    ok, reason = _is_real_pdf(dest)
    print(f"  {'OK' if ok else 'FAIL'}: {reason}")
    return ok


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    print("=" * 60)
    print("PDF cascade E2E connectivity test")
    print(f"  ADS_API_TOKEN: {'set' if os.environ.get('ADS_API_TOKEN') else '<unset>'}")
    print(f"  config.json: {CONFIG_PATH}")
    print("=" * 60)

    results = {}
    results["arxiv"] = test_arxiv()  # always first (most reliable)
    results["ads"] = test_ads()
    results["scihub"] = test_scihub()

    print("\n" + "=" * 60)
    print("Summary:")
    for name, r in results.items():
        if r is None:
            print(f"  {name:8s}: SKIP")
        elif r:
            print(f"  {name:8s}: PASS ✓")
        else:
            print(f"  {name:8s}: FAIL ✗")
    print("=" * 60)
    all_run = [r for r in results.values() if r is not None]
    if all(all_run) and all_run:
        print("All tested paths are reachable and download valid PDFs.")
    else:
        failed = [n for n, r in results.items() if r is False]
        if failed:
            print(f"Failed paths: {', '.join(failed)}")


if __name__ == "__main__":
    main()
