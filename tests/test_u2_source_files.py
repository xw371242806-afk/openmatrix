import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

import main


class U2SourceFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_patch = mock.patch("main.MATRIX_DB_PATH", self.root / "runs.sqlite3")
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.old_data = os.environ.get("OPENMATRIX_DATA_DIR")
        os.environ["OPENMATRIX_DATA_DIR"] = str(self.root)
        path = self.root / "run_files" / "run-safe"
        path.mkdir(parents=True)
        (path / "0.pdf").write_bytes(b"%PDF-U2-test")
        self.client = TestClient(main.app)

    def tearDown(self):
        if self.old_data is None:
            os.environ.pop("OPENMATRIX_DATA_DIR", None)
        else:
            os.environ["OPENMATRIX_DATA_DIR"] = self.old_data
        self.tmp.cleanup()

    def test_file_route_reads_bytes_and_rejects_traversal(self):
        response = self.client.get("/api/v1/runs/run-safe/documents/0/file")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"%PDF-U2-test")
        self.assertEqual(response.headers["content-type"], "application/pdf")
        self.assertIn(self.client.get("/api/v1/runs/../documents/0/file").status_code, {400, 404})
        self.assertIn(self.client.get("/api/v1/runs/run-safe/documents/-1/file").status_code, {400, 404})

    def test_bbox_uses_request_root_without_global_env(self):
        cell = {"cell_id": "d0q0", "document_index": 0, "document_name": "0.pdf", "question_index": 0, "question": "q", "status": "completed", "answer": "a", "quote": "Revenue", "citation_status": "verified_exact", "page": 1, "error": None}
        main.persist_matrix_cell("run-safe", cell)
        with mock.patch("mcp_server._locate_quote_bboxes", return_value='{"matches": []}') as locate:
            response = self.client.post("/api/v1/runs/run-safe/cells/d0q0/bboxes", json={})
        self.assertEqual(response.status_code, 200)
        locate.assert_called_once()
        self.assertEqual(Path(locate.call_args.kwargs["root"]).resolve(), (self.root / "run_files").resolve())
        self.assertIsNone(os.environ.get("OPENMATRIX_PDF_ROOT"))

    def test_file_route_requires_bearer_when_auth_enabled(self):
        old_auth = os.environ.get("OPENMATRIX_ENABLE_AUTH")
        os.environ["OPENMATRIX_ENABLE_AUTH"] = "true"
        try:
            self.assertEqual(self.client.get("/api/v1/runs/run-safe/documents/0/file").status_code, 401)
        finally:
            if old_auth is None:
                os.environ.pop("OPENMATRIX_ENABLE_AUTH", None)
            else:
                os.environ["OPENMATRIX_ENABLE_AUTH"] = old_auth

    def test_bbox_route_reads_real_pdf(self):
        from reportlab.pdfgen import canvas
        pdf_path = self.root / "run_files" / "run-safe" / "0.pdf"
        writer = canvas.Canvas(str(pdf_path))
        writer.drawString(72, 720, "Revenue was 125 million")
        writer.save()
        cell = {"cell_id": "d0q0", "document_index": 0, "document_name": "0.pdf", "question_index": 0, "question": "q", "status": "completed", "answer": "a", "quote": "Revenue was 125 million", "citation_status": "verified_exact", "page": 1, "error": None}
        main.persist_matrix_cell("run-real", cell)
        real_path = self.root / "run_files" / "run-real"
        real_path.mkdir(parents=True)
        (real_path / "0.pdf").write_bytes(pdf_path.read_bytes())
        response = self.client.post("/api/v1/runs/run-real/cells/d0q0/bboxes", json={})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json().get("matches"))

    def test_batch_retains_exact_uploaded_pdf_and_keep_false_removes_it(self):
        import io, json
        from types import SimpleNamespace
        from reportlab.pdfgen import canvas
        content=io.BytesIO()
        writer=canvas.Canvas(content);writer.drawString(72,720,"Revenue was 125 million");writer.save()
        uploaded=content.getvalue()
        class Client:
            def __init__(self, **kwargs):
                async def create(**kwargs):
                    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps([{"question":"Revenue?","answer":"125 million","quote":"Revenue was 125 million"}])) )])
                self.chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        for keep in ("true", "false"):
            with mock.patch.dict(os.environ, {"OPENMATRIX_KEEP_SOURCE_FILES":keep}), mock.patch("main.AsyncOpenAI",Client):
                response=self.client.post("/api/v1/analyze-batch",data={"questions":'["Revenue?"]',"api_key":"fixture-only","base_url":"https://example.invalid/v1","model_name":"fixture"},files=[("files",("fixture.pdf",uploaded,"application/pdf"))])
            self.assertEqual(response.status_code,200,response.text)
            result=response.json()
            source=self.client.get(f"/api/v1/runs/{result['run_id']}/documents/0/file")
            if keep=="true":
                self.assertEqual(source.status_code,200)
                self.assertEqual(source.content,uploaded)
            else:self.assertEqual(source.status_code,404)

    def test_public_locator_cannot_override_the_server_root(self):
        import inspect, mcp_server
        self.assertNotIn("root", inspect.signature(mcp_server.locate_quote_bboxes).parameters)
