import asyncio
from pathlib import Path
import pytest
import main

class FakeUpload:
    def __init__(self, size, filename="fixture.pdf"):
        self.remaining = size
        self.filename = filename
        self.read_limits = []
    async def read(self, limit=-1):
        self.read_limits.append(limit)
        if self.remaining <= 0: return b""
        count = self.remaining if limit < 0 else min(self.remaining, limit)
        self.remaining -= count
        return b"x" * count

def test_stream_upload_rejects_26mb_and_cleans_partial_file(tmp_path, monkeypatch):
    monkeypatch.setattr(main.tempfile, "gettempdir", lambda: str(tmp_path))
    upload = FakeUpload(26 * 1024 * 1024)
    with pytest.raises(main.PdfTooLargeError):
        asyncio.run(main._stream_upload_to_temp(upload, 25 * 1024 * 1024))
    assert upload.read_limits and max(upload.read_limits) <= 1024 * 1024
    assert list(Path(tmp_path).iterdir()) == []

def test_single_analyze_api_rejects_26mb_and_cleans_partial_file(tmp_path, monkeypatch):
    monkeypatch.setattr(main.tempfile, "gettempdir", lambda: str(tmp_path))
    upload = FakeUpload(26 * 1024 * 1024)
    response = asyncio.run(main.analyze_document(
        file=upload, api_key="test", base_url="http://127.0.0.1:9/v1",
        model_name="test", questions='["revenue"]', max_concurrency=1,
    ))
    assert response.status_code == 400
    assert max(upload.read_limits) <= 1024 * 1024
    assert list(Path(tmp_path).iterdir()) == []

def test_batch_analyze_api_rejects_26mb_and_cleans_partial_file(tmp_path, monkeypatch):
    monkeypatch.setattr(main.tempfile, "gettempdir", lambda: str(tmp_path))
    upload = FakeUpload(26 * 1024 * 1024)
    response = asyncio.run(main.analyze_documents_batch(
        files=[upload], api_key="test", base_url="http://127.0.0.1:9/v1",
        model_name="test", questions='["revenue"]', max_concurrency=1,
    ))
    assert response.status_code == 400
    assert max(upload.read_limits) <= 1024 * 1024
    assert list(Path(tmp_path).iterdir()) == []

def test_chain_route_has_no_unbounded_upload_read():
    source = Path(main.__file__).read_text(encoding="utf-8")
    chain = source[source.index("async def analyze_chain"):source.index("async def analyze_documents_batch")]
    assert "await file.read())" not in chain

def test_local_fallback_default_uses_model_server_port():
    source = Path(main.__file__).read_text(encoding="utf-8")
    assert 'os.getenv("LOCAL_OPENAI_BASE", "http://localhost:11434/v1")' in source

def test_malformed_docx_returns_bad_request_without_leaking_temp_path(monkeypatch):
    from fastapi.testclient import TestClient
    class Library:
        async def get_project(self, project_id): return {"id":project_id}
    monkeypatch.setattr(main,"_library_store",lambda:Library())
    response=TestClient(main.app).post('/api/v1/projects/fixture/upload',files={'file':('bad.docx',b'not a DOCX','application/octet-stream')})
    assert response.status_code==400
    assert response.json()['error']=='Could not read DOCX document'
