"""Tests for the MinerU cloud API (mineru.net online) backend.

Mocks all HTTP (requests.request / requests.put / requests.get). Verifies the
async upload→poll→download→extract flow, vlm→pipeline degradation, and error
handling. No real network calls.
"""

import io
import json
import sys
import types
import zipfile
from unittest.mock import MagicMock

import pytest

from zotero_mcp import mineru_client as M


@pytest.fixture(autouse=True)
def _bypass_mineru_ssrf_guard(monkeypatch):
    """Bypass the SSRF host check so tests can use fake hostnames (``upload``,
    ``cdn``, ``u``, ``r``) without real DNS resolution. The SSRF guard itself
    is tested directly in ``test_mineru_client.py::TestMineruSSRFGuard``."""
    monkeypatch.setattr(M, "_url_is_public", lambda url: True)


def _make_zip_bytes(md_text: str, content_list: list | None = None) -> bytes:
    """Build a fake MinerU cloud result zip containing full.md + content_list.json."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("full.md", md_text)
        if content_list is not None:
            zf.writestr("paper_content_list.json", json.dumps(content_list))
    return buf.getvalue()


class _FakeResp:
    def __init__(self, status_code=200, json_data=None, content=b"", text=""):
        self.status_code = status_code
        self._json = json_data
        self.content = content
        self.text = text

    def json(self):
        return self._json


class TestCloudNormalize:
    def test_cloud_alias(self):
        assert M._normalize_backend("cloud") == "cloud"
        assert M._normalize_backend("online") == "cloud"
        assert M._normalize_backend("CLOUD") == "cloud"

    def test_cloud_available_with_token(self):
        assert M.is_mineru_available({"backend": "cloud", "cloud_token": "tok"}) is True

    def test_cloud_unavailable_without_token(self, monkeypatch):
        monkeypatch.delenv("MINERU_API_TOKEN", raising=False)
        assert M.is_mineru_available({"backend": "cloud"}) is False


class TestCloudParseFlow:
    def test_success_vlm(self, tmp_path, monkeypatch):
        pdf = tmp_path / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")

        md = "# Title\n\n$$E=mc^2$$\n\nbody text"
        cl = [{"page_idx": 0, "text": "Title"}, {"page_idx": 1, "text": "body text"}]
        zip_bytes = _make_zip_bytes(md, cl)

        # Mock _cloud_request (apply + poll) and _cloud_download_zip.
        call_state = {"polls": 0}

        def fake_cloud_request(method, path, token, **kwargs):
            if "/file-urls/batch" in path:
                return {"code": 0, "data": {"batch_id": "b123", "file_urls": ["https://upload/x"]}}
            if "/extract-results/batch/" in path:
                call_state["polls"] += 1
                if call_state["polls"] < 2:
                    return {"code": 0, "data": {"extract_result": [{"state": "running"}]}}
                return {"code": 0, "data": {"extract_result": [
                    {"state": "done", "full_zip_url": "https://cdn/result.zip"}
                ]}}
            return None

        monkeypatch.setattr(M, "_cloud_request", fake_cloud_request)
        monkeypatch.setattr(M, "_cloud_download_zip", lambda url: zip_bytes)
        # Mock the PUT upload.
        fake_requests = types.ModuleType("requests")
        fake_requests.put = MagicMock(return_value=_FakeResp(200))
        monkeypatch.setitem(sys.modules, "requests", fake_requests)

        result = M._call_mineru_cloud(pdf, -1, -1, "tok", "vlm", timeout=30)
        assert result is not None
        md_out, cl_out, source = result
        assert "E=mc^2" in md_out
        assert source == "mineru:cloud-vlm"
        assert cl_out is not None and len(cl_out) == 2
        # Verify upload PUT was called.
        fake_requests.put.assert_called_once()

    def test_vlm_failure_falls_back_to_pipeline(self, tmp_path, monkeypatch):
        pdf = tmp_path / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        md = "# Pipeline result"
        zip_bytes = _make_zip_bytes(md)

        attempt = {"n": 0}

        def fake_cloud_request(method, path, token, **kwargs):
            if "/file-urls/batch" in path:
                attempt["n"] += 1
                # vlm (attempt 1) fails; pipeline (attempt 2) succeeds.
                if attempt["n"] == 1:
                    return {"code": -1, "msg": "vlm error"}
                return {"code": 0, "data": {"batch_id": "b2", "file_urls": ["https://upload/x"]}}
            if "/extract-results/batch/" in path:
                return {"code": 0, "data": {"extract_result": [
                    {"state": "done", "full_zip_url": "https://cdn/r.zip"}
                ]}}
            return None

        monkeypatch.setattr(M, "_cloud_request", fake_cloud_request)
        monkeypatch.setattr(M, "_cloud_download_zip", lambda url: zip_bytes)
        fake_requests = types.ModuleType("requests")
        fake_requests.put = MagicMock(return_value=_FakeResp(200))
        monkeypatch.setitem(sys.modules, "requests", fake_requests)

        result = M._call_mineru_cloud(pdf, -1, -1, "tok", "vlm", timeout=30)
        assert result is not None
        _, _, source = result
        assert source == "mineru:cloud-pipeline"
        assert attempt["n"] == 2  # vlm tried, then pipeline

    def test_parse_failed_state_returns_none(self, tmp_path, monkeypatch):
        pdf = tmp_path / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")

        def fake_cloud_request(method, path, token, **kwargs):
            if "/file-urls/batch" in path:
                return {"code": 0, "data": {"batch_id": "b", "file_urls": ["https://u"]}}
            if "/extract-results/batch/" in path:
                return {"code": 0, "data": {"extract_result": [
                    {"state": "failed", "err_msg": "too many pages"}
                ]}}
            return None

        monkeypatch.setattr(M, "_cloud_request", fake_cloud_request)
        fake_requests = types.ModuleType("requests")
        fake_requests.put = MagicMock(return_value=_FakeResp(200))
        monkeypatch.setitem(sys.modules, "requests", fake_requests)

        # vlm fails with state=failed, pipeline also fails → None
        result = M._call_mineru_cloud(pdf, -1, -1, "tok", "vlm", timeout=10)
        assert result is None

    def test_apply_upload_failure_returns_none(self, tmp_path, monkeypatch):
        pdf = tmp_path / "paper.pdf"
        pdf.write_bytes(b"%PDF fake")
        monkeypatch.setattr(M, "_cloud_request", lambda *a, **k: {"code": -1, "msg": "bad token"})
        result = M._call_mineru_cloud(pdf, -1, -1, "badtoken", "vlm", timeout=10)
        assert result is None

    def test_upload_http_error_returns_none(self, tmp_path, monkeypatch):
        pdf = tmp_path / "paper.pdf"
        pdf.write_bytes(b"%PDF fake")

        def fake_cloud_request(method, path, token, **kwargs):
            if "/file-urls/batch" in path:
                return {"code": 0, "data": {"batch_id": "b", "file_urls": ["https://u"]}}
            return None

        monkeypatch.setattr(M, "_cloud_request", fake_cloud_request)
        fake_requests = types.ModuleType("requests")
        fake_requests.put = MagicMock(return_value=_FakeResp(403, text="forbidden"))
        monkeypatch.setitem(sys.modules, "requests", fake_requests)

        result = M._call_mineru_cloud(pdf, -1, -1, "tok", "vlm", timeout=10)
        assert result is None


class TestCloudPageRanges:
    def test_page_range_converted_to_1_indexed(self, tmp_path, monkeypatch):
        """0-indexed start/end from caller → 1-indexed page_ranges for cloud API."""
        pdf = tmp_path / "paper.pdf"
        pdf.write_bytes(b"%PDF fake")
        captured = {}

        def fake_cloud_request(method, path, token, **kwargs):
            if "/file-urls/batch" in path:
                captured["body"] = kwargs.get("json")
                return {"code": 0, "data": {"batch_id": "b", "file_urls": ["https://u"]}}
            if "/extract-results/batch/" in path:
                return {"code": 0, "data": {"extract_result": [
                    {"state": "done", "full_zip_url": "https://r.zip"}
                ]}}
            return None

        monkeypatch.setattr(M, "_cloud_request", fake_cloud_request)
        monkeypatch.setattr(M, "_cloud_download_zip", lambda url: _make_zip_bytes("# md"))
        fake_requests = types.ModuleType("requests")
        fake_requests.put = MagicMock(return_value=_FakeResp(200))
        monkeypatch.setitem(sys.modules, "requests", fake_requests)

        # Caller passes 0-indexed pages 3-5 (4th-6th pages).
        M._call_mineru_cloud(pdf, 3, 5, "tok", "vlm", timeout=10)
        file_entry = captured["body"]["files"][0]
        # Cloud API uses 1-indexed: 0-based 3-5 → 1-based 4-6.
        assert file_entry["page_ranges"] == "4-6"
