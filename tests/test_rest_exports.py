import json
from io import BytesIO
from pathlib import Path

from docx import Document
from fastapi.testclient import TestClient
from pypdf import PdfReader
from pptx import Presentation
import main


def _completed_fixture(run_id="run-1"):
    return {"run_id": run_id, "cells": [{
        "document_name": "fixture.pdf", "question": "What was revenue?",
        "answer": "Revenue was 1,800 million.",
        "quote": "Revenue was 1,800 million.", "citation_status": "verified_exact", "page": 1,
    }]}


def test_rest_exports_are_real_openable_artifacts_and_confined(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENMATRIX_EXPORT_ROOT", str(tmp_path))
    fixture = _completed_fixture()
    monkeypatch.setattr(main, "load_matrix_run", lambda run_id: fixture if run_id == "run-1" else None)
    import mcp_server
    monkeypatch.setattr(mcp_server, "load_matrix_run", lambda run_id: fixture if run_id == "run-1" else None)
    client = TestClient(main.app)
    expected = {
        "pdf": "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    }
    for fmt, content_type in expected.items():
        response = client.get(f"/api/v1/export-{fmt}/run-1")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith(content_type)
        assert response.content
        if fmt == "pdf":
            assert "1,800 million" in "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(response.content)).pages)
        elif fmt == "docx":
            assert "1,800 million" in "\n".join(p.text for p in Document(BytesIO(response.content)).paragraphs) or "1,800 million" in "\n".join(c.text for t in Document(BytesIO(response.content)).tables for row in t.rows for c in row.cells)
        else:
            texts = [shape.text for slide in Presentation(BytesIO(response.content)).slides for shape in slide.shapes if hasattr(shape, "text")]
            assert any("1,800 million" in text for text in texts)

    assert main._rest_export("../outside", "pdf").status_code == 400
    assert client.get("/api/v1/export-pdf/missing-run").status_code == 404


def test_rest_exports_reject_artifact_outside_root(tmp_path, monkeypatch):
    import mcp_server
    monkeypatch.setenv("OPENMATRIX_EXPORT_ROOT", str(tmp_path))
    fixture = _completed_fixture()
    monkeypatch.setattr(main, "load_matrix_run", lambda run_id: fixture)
    outside = tmp_path.parent / "outside.pdf"
    outside.write_bytes(b"outside")
    monkeypatch.setattr(mcp_server, "export_report", lambda *args, **kwargs: json.dumps({"path": str(outside)}))
    response = TestClient(main.app).get("/api/v1/export-pdf/run-1")
    assert response.status_code == 500


def test_legacy_export_paths_require_auth_when_enabled(monkeypatch):
    monkeypatch.setenv("OPENMATRIX_ENABLE_AUTH", "on")
    monkeypatch.setattr(main, "load_matrix_run", lambda run_id: _completed_fixture(run_id))
    client = TestClient(main.app)
    for fmt in ("pdf", "docx", "pptx"):
        assert client.get(f"/api/export-{fmt}/run-1").status_code == 401
