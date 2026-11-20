import json
import os
import sys
import logging
import threading
import tempfile
import unittest
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

def setUpModule():
    global _mock_bge_m3, _mock_reranker
    _mock_bge_m3 = mock.patch("main.DenseRetrieverAdapter._bge_m3_available", return_value=False)
    _mock_reranker = mock.patch("main.BgeRerankerAdapter.is_available", return_value=False)
    _mock_bge_m3.start()
    _mock_reranker.start()

def tearDownModule():
    _mock_bge_m3.stop()
    _mock_reranker.stop()

# fastapi/starlette's TestClient emits a one-time library-level deprecation
# notice at import time ("install httpx2 instead"). The production app does not
# depend on TestClient; the notice is environment noise from the test venv, so
# it is filtered here before the import that would emit it.
warnings.filterwarnings(
    "ignore",
    message="Using `httpx` with `starlette.testclient` is deprecated",
    category=UserWarning,
)

from fastapi.testclient import TestClient

from main import app


def make_pdf(text: str) -> bytes:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream",
    ]

    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf.extend(f"{number} 0 obj\n".encode("ascii"))
        pdf.extend(body)
        pdf.extend(b"\nendobj\n")

    xref_offset = len(pdf)
    pdf.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    pdf.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    pdf.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    return bytes(pdf)


class FakeOpenAIHandler(BaseHTTPRequestHandler):
    post_count = 0
    provider_authorizations = []
    prompts = []

    def do_GET(self):
        if self.path == "/v1/models":
            self.send_json(200, {"object": "list", "data": [{"id": "exact-model"}]})
        else:
            self.send_json(404, {"error": "not found"})

    def do_POST(self):
        self.__class__.post_count += 1
        self.__class__.provider_authorizations.append(self.headers.get("Authorization"))
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length))
        model = request.get("model")
        prompt = (request.get("messages") or [{}])[-1].get("content", "")
        self.__class__.prompts.append(prompt)

        if model == "api-error-model":
            self.send_json(401, {"error": {"message": "Invalid API key"}})
            return

        quote = "Total revenue was 125 million dollars."
        if model == "hallucination-model":
            quote = "Revenue exceeded 900 million dollars."

        if model == "chain-malformed-json-model":
            content = "not-json"
        elif model == "chain-model":
            answer = "25% margin." if "125 million dollars." in prompt else "125 million dollars."
            content = json.dumps({"answer": answer, "quote": quote})
        else:
            content = json.dumps(
                [
                    {
                        "question": "What was total revenue?",
                        "answer": "125 million dollars.",
                        "quote": quote,
                    }
                ]
            )
        self.send_json(
            200,
            {
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 0,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    def send_json(self, status: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


class FlakyOpenAIHandler(BaseHTTPRequestHandler):
    """Returns 503 for the first ``failures_remaining`` requests, then a valid response.

    Used to prove the production model path applies bounded retry to transient
    provider failures instead of surfacing them as 500s.
    """

    failures_remaining = 2

    def do_POST(self):
        if self.__class__.failures_remaining > 0:
            self.__class__.failures_remaining -= 1
            self.send_json(503, {"error": {"message": "service unavailable"}})
            return
        content = json.dumps(
            [
                {
                    "question": "What was total revenue?",
                    "answer": "125 million dollars.",
                    "quote": "Total revenue was 125 million dollars.",
                }
            ]
        )
        self.send_json(
            200,
            {
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 0,
                "model": "exact-model",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
            },
        )

    def send_json(self, status: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


class AnalyzeApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeOpenAIHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}/v1"
        cls.client = TestClient(app)
        cls.pdf = make_pdf(
            "OpenMatrix Test Company 2025 Annual Report. Total revenue was 125 million dollars."
        )

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def analyze(self, model: str, pdf: bytes | None = None):
        return self.client.post(
            "/api/analyze",
            data={
                "api_key": "test-key",
                "base_url": self.base_url,
                "model_name": model,
                "questions": json.dumps(["What was total revenue?"]),
            },
            files={"file": ("fixture.pdf", self.pdf if pdf is None else pdf, "application/pdf")},
        )

    def test_fixed_pdf_returns_answer_and_verified_exact_quote(self):
        response = self.analyze("exact-model")

        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertEqual("success", payload["status"])
        self.assertEqual("125 million dollars.", payload["data"][0]["answer"])
        self.assertEqual("verified_exact", payload["data"][0]["citation_status"])
        self.assertEqual(1, payload["data"][0]["page"])

    def test_hallucinated_quote_marks_response_for_review(self):
        response = self.analyze("hallucination-model")

        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertEqual("needs_review", payload["status"])
        self.assertEqual("not_found", payload["data"][0]["citation_status"])

    def test_api_error_is_explicit(self):
        response = self.analyze("api-error-model")

        self.assertEqual(500, response.status_code)
        self.assertIn("Invalid API key", response.json()["error"])

    def test_transient_provider_failure_is_retried_through_production_path(self):
        FlakyOpenAIHandler.failures_remaining = 2
        flaky_server = ThreadingHTTPServer(("127.0.0.1", 0), FlakyOpenAIHandler)
        flaky_thread = threading.Thread(target=flaky_server.serve_forever, daemon=True)
        flaky_thread.start()
        # Cleanups run LIFO: shutdown must precede server_close so the
        # serve_forever thread stops selecting before the socket is closed.
        self.addCleanup(flaky_server.server_close)
        self.addCleanup(flaky_server.shutdown)
        self.addCleanup(flaky_thread.join, timeout=5)

        flaky_url = f"http://127.0.0.1:{flaky_server.server_port}/v1"
        response = self.client.post(
            "/api/analyze",
            data={
                "api_key": "test-key",
                "base_url": flaky_url,
                "model_name": "exact-model",
                "questions": json.dumps(["What was total revenue?"]),
            },
            files={"file": ("fixture.pdf", self.pdf, "application/pdf")},
        )

        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertEqual("success", payload["status"])
        self.assertEqual("verified_exact", payload["data"][0]["citation_status"])
        self.assertEqual(0, FlakyOpenAIHandler.failures_remaining)

    def test_retry_exhaustion_still_reports_provider_error(self):
        FlakyOpenAIHandler.failures_remaining = 99
        flaky_server = ThreadingHTTPServer(("127.0.0.1", 0), FlakyOpenAIHandler)
        flaky_thread = threading.Thread(target=flaky_server.serve_forever, daemon=True)
        flaky_thread.start()
        self.addCleanup(flaky_server.server_close)
        self.addCleanup(flaky_server.shutdown)
        self.addCleanup(flaky_thread.join, timeout=5)

        flaky_url = f"http://127.0.0.1:{flaky_server.server_port}/v1"
        response = self.client.post(
            "/api/analyze",
            data={
                "api_key": "test-key",
                "base_url": flaky_url,
                "model_name": "exact-model",
                "questions": json.dumps(["What was total revenue?"]),
            },
            files={"file": ("fixture.pdf", self.pdf, "application/pdf")},
        )

        self.assertEqual(500, response.status_code)
        self.assertIn("service unavailable", response.json()["error"])
        FlakyOpenAIHandler.failures_remaining = 0

    def test_invalid_pdf_error_is_explicit(self):
        response = self.analyze("exact-model", b"not a pdf")

        self.assertEqual(400, response.status_code)
        self.assertIn("Invalid PDF", response.json()["error"])

    def test_batch_analyze_endpoint_accepts_multiple_pdfs(self):
        FakeOpenAIHandler.post_count = 0
        pdf1 = make_pdf("Company Alpha 2025: Cloud revenue was 1800 million.")
        pdf2 = make_pdf("Company Beta 2025: Software revenue was 950 million.")
        response = self.client.post(
            "/api/v1/analyze-batch",
            data={
                "api_key": "test-key",
                "base_url": self.base_url,
                "model_name": "exact-model",
                "questions": json.dumps(["What was total revenue?", "What was software revenue?"]),
            },
            files=[
                ("files", ("alpha.pdf", pdf1, "application/pdf")),
                ("files", ("beta.pdf", pdf2, "application/pdf")),
            ],
        )
        self.assertEqual(200, response.status_code)
        body = response.json()
        self.assertEqual(2, body.get("documents_count"))
        self.assertEqual(4, len(body.get("data", [])))
        self.assertEqual("alpha.pdf", body["data"][0]["document_name"])
        self.assertEqual("beta.pdf", body["data"][2]["document_name"])
        self.assertEqual(2, FakeOpenAIHandler.post_count)

        FakeOpenAIHandler.post_count = 0
        # Also test /api/analyze_batch alias
        response_alias = self.client.post(
            "/api/analyze_batch",
            data={
                "api_key": "test-key",
                "base_url": self.base_url,
                "model_name": "exact-model",
                "questions": json.dumps(["What was total revenue?"]),
            },
            files=[
                ("files", ("alpha.pdf", pdf1, "application/pdf")),
            ],
        )
        self.assertEqual(200, response_alias.status_code)
        self.assertEqual(1, response_alias.json().get("documents_count"))
        self.assertEqual(1, FakeOpenAIHandler.post_count)

    def test_v1_batch_and_exports_require_openmatrix_bearer_while_provider_key_stays_form_data(self):
        old_enable = os.environ.get("OPENMATRIX_ENABLE_AUTH")
        old_master = os.environ.get("OPENMATRIX_MASTER_KEY")
        os.environ["OPENMATRIX_ENABLE_AUTH"] = "1"
        os.environ["OPENMATRIX_MASTER_KEY"] = "test-master"
        self.addCleanup(lambda: os.environ.pop("OPENMATRIX_ENABLE_AUTH", None) if old_enable is None else os.environ.__setitem__("OPENMATRIX_ENABLE_AUTH", old_enable))
        self.addCleanup(lambda: os.environ.pop("OPENMATRIX_MASTER_KEY", None) if old_master is None else os.environ.__setitem__("OPENMATRIX_MASTER_KEY", old_master))
        FakeOpenAIHandler.post_count = 0
        FakeOpenAIHandler.provider_authorizations = []
        pdf1 = make_pdf("Company Alpha 2025: Total revenue was 125 million dollars.")
        data = {
            "api_key": "provider-key",
            "base_url": self.base_url,
            "model_name": "exact-model",
            "questions": json.dumps(["What was total revenue?"]),
        }
        files = [("files", ("alpha.pdf", pdf1, "application/pdf"))]

        no_auth = self.client.post("/api/v1/analyze-batch", data=data, files=files)
        self.assertEqual(401, no_auth.status_code)
        self.assertEqual(0, FakeOpenAIHandler.post_count)

        authed = self.client.post(
            "/api/v1/analyze-batch",
            headers={"Authorization": "Bearer test-master"},
            data=data,
            files=files,
        )
        self.assertEqual(200, authed.status_code)
        self.assertEqual(["Bearer provider-key"], FakeOpenAIHandler.provider_authorizations)
        run_id = authed.json()["run_id"]

        self.assertEqual(401, self.client.get(f"/api/v1/export-csv/{run_id}").status_code)
        csv_resp = self.client.get(f"/api/v1/export-csv/{run_id}", headers={"Authorization": "Bearer test-master"})
        self.assertEqual(200, csv_resp.status_code)
        self.assertEqual(401, self.client.get(f"/api/v1/export-xlsx/{run_id}").status_code)
        xlsx_resp = self.client.get(f"/api/v1/export-xlsx/{run_id}", headers={"Authorization": "Bearer test-master"})
        self.assertEqual(200, xlsx_resp.status_code)


    def test_analyze_chain_uses_prior_answers_for_follow_up_steps(self):
        FakeOpenAIHandler.post_count = 0
        FakeOpenAIHandler.prompts = []
        steps = [
            {"id": "revenue", "question": "What was total revenue?"},
            {"id": "margin", "question": "Based on revenue, calculate margin quality.", "depends_on": ["revenue"]},
        ]
        response = self.client.post(
            "/api/v1/analyze-chain",
            data={
                "api_key": "test-key",
                "base_url": self.base_url,
                "model_name": "chain-model",
                "steps": json.dumps(steps),
            },
            files={"file": ("fixture.pdf", self.pdf, "application/pdf")},
        )

        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertEqual("success", payload["status"])
        self.assertEqual(2, payload["steps_count"])
        self.assertEqual(["revenue", "margin"], [row["step_id"] for row in payload["data"]])
        self.assertEqual(["revenue"], payload["data"][1]["depends_on"])
        self.assertEqual(2, FakeOpenAIHandler.post_count)
        self.assertIn("125 million dollars.", FakeOpenAIHandler.prompts[1])

    def test_analyze_chain_kg_is_disabled_by_default(self):
        import main

        with mock.patch.object(main, "_retrieve_kg_context") as retrieve:
            response = self.client.post(
                "/api/v1/analyze-chain",
                data={
                    "api_key": "test-key",
                    "base_url": self.base_url,
                    "model_name": "chain-model",
                    "steps": json.dumps([{"id": "revenue", "question": "What was total revenue?"}]),
                },
                files={"file": ("fixture.pdf", self.pdf, "application/pdf")},
            )
        self.assertEqual(200, response.status_code)
        retrieve.assert_not_called()
        self.assertIsNone(response.json()["kg_index_task_id"])

    def test_analyze_chain_fuses_current_document_kg_context(self):
        import main

        async def kg_context(corpus_id, document_name, question):
            self.assertEqual("fixture.pdf", document_name)
            self.assertEqual("What was total revenue?", question)
            return '<KNOWLEDGE_GRAPH_EVIDENCE>[Page 1 | c1] revenue evidence</KNOWLEDGE_GRAPH_EVIDENCE>'

        with mock.patch.object(main, "_retrieve_kg_context", side_effect=kg_context):
            response = self.client.post(
                "/api/v1/analyze-chain",
                data={
                    "api_key": "test-key",
                    "base_url": self.base_url,
                    "model_name": "chain-model",
                    "steps": json.dumps([{"id": "revenue", "question": "What was total revenue?"}]),
                    "use_kg": "true",
                },
                files={"file": ("fixture.pdf", self.pdf, "application/pdf")},
            )
        self.assertEqual(200, response.status_code)
        self.assertIn("KNOWLEDGE_GRAPH_EVIDENCE", FakeOpenAIHandler.prompts[-1])

    def test_analyze_chain_schedules_kg_index_without_secrets(self):
        import main

        class RecordingManager:
            def status(self, task_id):
                return None

            def submit(self, operation, payload, fn):
                self.operation = operation
                self.payload = payload
                self.fn = fn
                return "t-kg-index"

        manager = RecordingManager()
        with mock.patch.object(main, "default_task_manager", return_value=manager):
            with mock.patch.object(main, "_kg_index_tasks", {}):
                with mock.patch.object(main, "_kg_task_owners", {}):
                    with mock.patch.object(main, "_kg_acquire_index_lock", return_value=True):
                        response = self.client.post(
                            "/api/v1/analyze-chain",
                            data={
                                "api_key": "provider-secret",
                                "base_url": self.base_url,
                                "model_name": "chain-model",
                                "steps": json.dumps(
                                    [{"id": "revenue", "question": "What was total revenue?"}]
                                ),
                                "index_kg": "true",
                            },
                            files={"file": ("fixture.pdf", self.pdf, "application/pdf")},
                        )
        self.assertEqual(200, response.status_code)
        self.assertEqual("t-kg-index", response.json()["kg_index_task_id"])
        self.assertEqual("kg_index_document", manager.operation)
        self.assertNotIn("api_key", manager.payload)
        self.assertNotIn("provider-secret", json.dumps(manager.payload))
        self.assertNotIn("pages", manager.payload)

    def test_legacy_analyze_chain_alias_requires_auth_and_remains_compatible(self):
        import main

        old_auth = os.environ.get("OPENMATRIX_ENABLE_AUTH")
        os.environ["OPENMATRIX_ENABLE_AUTH"] = "true"
        self.addCleanup(
            lambda: os.environ.__setitem__("OPENMATRIX_ENABLE_AUTH", old_auth)
            if old_auth is not None
            else os.environ.pop("OPENMATRIX_ENABLE_AUTH", None)
        )
        old_master = os.environ.get("OPENMATRIX_MASTER_KEY")
        os.environ["OPENMATRIX_MASTER_KEY"] = "test-master"
        self.addCleanup(
            lambda: os.environ.__setitem__("OPENMATRIX_MASTER_KEY", old_master)
            if old_master is not None
            else os.environ.pop("OPENMATRIX_MASTER_KEY", None)
        )
        request = {
            "data": {
                "api_key": "test-key",
                "base_url": self.base_url,
                "model_name": "chain-model",
                "steps": json.dumps(
                    [{"id": "revenue", "question": "What was total revenue?"}]
                ),
            },
            "files": {"file": ("fixture.pdf", self.pdf, "application/pdf")},
        }
        anonymous = self.client.post("/api/analyze_chain", **request)
        self.assertEqual(401, anonymous.status_code)

        response = self.client.post(
            "/api/analyze_chain",
            headers={"Authorization": "Bearer test-master"},
            **request,
        )
        self.assertEqual(200, response.status_code)

    def test_analyze_chain_rejects_cycles(self):
        steps = [
            {"id": "a", "question": "Question A?", "depends_on": ["b"]},
            {"id": "b", "question": "Question B?", "depends_on": ["a"]},
        ]
        response = self.client.post(
            "/api/v1/analyze-chain",
            data={
                "api_key": "test-key",
                "base_url": self.base_url,
                "model_name": "chain-model",
                "steps": json.dumps(steps),
            },
            files={"file": ("fixture.pdf", self.pdf, "application/pdf")},
        )

        self.assertEqual(400, response.status_code)
        self.assertIn("Cycle", response.json()["error"])


    def test_analyze_chain_invalid_steps_json_returns_400(self):
        response = self.client.post(
            "/api/v1/analyze-chain",
            data={
                "api_key": "test-key",
                "base_url": self.base_url,
                "model_name": "chain-model",
                "steps": "not-json",
            },
            files={"file": ("fixture.pdf", self.pdf, "application/pdf")},
        )

        self.assertEqual(400, response.status_code)
        self.assertIn("Invalid JSON", response.json()["error"])

    def test_analyze_chain_malformed_model_json_marks_step_without_500(self):
        steps = [{"id": "a", "question": "What was total revenue?"}]
        response = self.client.post(
            "/api/v1/analyze-chain",
            data={
                "api_key": "test-key",
                "base_url": self.base_url,
                "model_name": "chain-malformed-json-model",
                "steps": json.dumps(steps),
            },
            files={"file": ("fixture.pdf", self.pdf, "application/pdf")},
        )

        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertEqual("success", payload["status"])
        self.assertEqual("not_disclosed", payload["data"][0]["status"])
        self.assertEqual("not_applicable", payload["data"][0]["citation_status"])


    def test_analyze_chain_rejects_oversized_upload_during_chunked_read(self):
        import main

        old_limit = main.MAX_PDF_BYTES
        main.MAX_PDF_BYTES = 8
        self.addCleanup(setattr, main, "MAX_PDF_BYTES", old_limit)
        steps = [{"id": "a", "question": "What was total revenue?"}]
        response = self.client.post(
            "/api/v1/analyze-chain",
            data={
                "api_key": "test-key",
                "base_url": self.base_url,
                "model_name": "chain-model",
                "steps": json.dumps(steps),
            },
            files={"file": ("fixture.pdf", b"0123456789", "application/pdf")},
        )

        self.assertEqual(400, response.status_code)
        self.assertIn("PDF exceeds", response.json()["error"])

    def test_task_queue_endpoints_enqueue_status_and_cancel(self):
        import main

        # Ensure a fresh in-process queue for this test.
        original_tasks = main._tasks
        main._tasks = None
        self.addCleanup(setattr, main, "_tasks", original_tasks)

        response = self.client.post(
            "/api/v1/tasks",
            data={"task_type": "analyze", "payload": json.dumps({"doc": "a.pdf"})},
        )
        self.assertEqual(200, response.status_code)
        task_id = response.json()["task_id"]
        self.assertEqual("queued", response.json()["status"])

        status_resp = self.client.get(f"/api/v1/tasks/{task_id}")
        self.assertEqual(200, status_resp.status_code)
        self.assertEqual("queued", status_resp.json()["status"])

        cancel_resp = self.client.delete(f"/api/v1/tasks/{task_id}")
        self.assertEqual(200, cancel_resp.status_code)
        self.assertEqual("cancelled", cancel_resp.json()["status"])

        missing_resp = self.client.get("/api/v1/tasks/does-not-exist")
        self.assertEqual(404, missing_resp.status_code)

    def test_task_status_routes_task_manager_ids(self):
        import main

        manager = mock.Mock()
        manager.result.return_value = {
            "task_id": "t-kg123",
            "operation": "kg_index_document",
            "status": "succeeded",
            "result": {"indexed_pages": 2},
        }
        with mock.patch.object(main, "default_task_manager", return_value=manager):
            with mock.patch.dict(main._kg_task_owners, {"t-kg123": "dev"}):
                response = self.client.get("/api/v1/tasks/t-kg123")
        self.assertEqual(200, response.status_code)
        self.assertEqual(2, response.json()["result"]["indexed_pages"])

    def test_task_cancel_routes_task_manager_ids(self):
        import main

        manager = mock.Mock()
        manager.cancel.return_value = True
        with mock.patch.object(main, "default_task_manager", return_value=manager):
            with mock.patch.dict(main._kg_task_owners, {"t-kg123": "dev"}):
                response = self.client.delete("/api/v1/tasks/t-kg123")
        self.assertEqual(200, response.status_code)
        manager.cancel.assert_called_once_with("t-kg123")

    def test_task_manager_id_is_tenant_scoped(self):
        import main

        with mock.patch.dict(main._kg_task_owners, {"t-kg123": "tenant-a"}):
            response = self.client.get("/api/v1/tasks/t-kg123")
        self.assertEqual(404, response.status_code)

    def test_sse_stream_emits_cell_and_terminal_events(self):
        import asyncio
        import tempfile
        from pathlib import Path

        import main
        from main import execute_matrix_run

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "matrix.sqlite3"
            original_db_path = main.MATRIX_DB_PATH
            main.MATRIX_DB_PATH = db_path
            self.addCleanup(setattr, main, "MATRIX_DB_PATH", original_db_path)

            documents = [
                {"name": "doc-0.pdf", "pages": ["Exact quote one."]},
                {"name": "doc-1.pdf", "pages": ["Exact quote two."]},
            ]
            questions = ["Q1", "Q2"]

            async def answer_cell(document, question):
                return {
                    "question": question,
                    "answer": "disclosed",
                    "quote": document["pages"][0],
                }

            run_id = "sse-test-run"

            async def run_job():
                return await execute_matrix_run(
                    documents,
                    questions,
                    answer_cell,
                    max_concurrency=2,
                    db_path=db_path,
                    run_id=run_id,
                )

            loop = asyncio.new_event_loop()
            loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
            loop_thread.start()
            job = asyncio.run_coroutine_threadsafe(run_job(), loop)

            received = []

            with self.client.stream("GET", f"/api/runs/{run_id}/events") as response:
                self.assertEqual(200, response.status_code)
                for line in response.iter_lines():
                    if line.startswith("data: "):
                        received.append(json.loads(line[len("data: "):]))
                    if any(evt.get("type") == "run_complete" for evt in received):
                        break

            job.result()
            loop.call_soon_threadsafe(loop.stop)
            loop_thread.join(timeout=5)
            loop.close()

            cell_events = [evt for evt in received if evt.get("type") == "cell"]
            self.assertEqual(4, len(cell_events))
            self.assertTrue(any(evt.get("status") == "completed" for evt in cell_events))
            self.assertEqual("run_complete", received[-1]["type"])

            expected_cells = {
                (0, 0, "d0q0"),
                (0, 1, "d0q1"),
                (1, 0, "d1q0"),
                (1, 1, "d1q1"),
            }
            observed_cells = {
                (evt["document_index"], evt["question_index"], evt["cell_id"]) for evt in cell_events
            }
            self.assertEqual(expected_cells, observed_cells)
            for evt in cell_events:
                self.assertEqual(run_id, evt["run_id"])
                self.assertIn(evt["status"], {"completed", "failed", "needs_review", "not_disclosed"})

    def test_sse_stream_emits_events_for_analyze_production_run(self):
        """Drive SSE through the production /api/analyze path.

        Submits a real document, then reads the persisted run's event stream and
        asserts one per-cell event per question plus a terminal run_complete.
        """
        import asyncio
        import tempfile
        from pathlib import Path

        import main

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "matrix.sqlite3"
            original_db_path = main.MATRIX_DB_PATH
            main.MATRIX_DB_PATH = db_path
            self.addCleanup(setattr, main, "MATRIX_DB_PATH", original_db_path)

            response = self.analyze("exact-model")
            self.assertEqual(200, response.status_code)
            run_id = response.json()["run_id"]
            self.assertTrue(run_id)

            received = []
            with self.client.stream("GET", f"/api/runs/{run_id}/events") as stream:
                self.assertEqual(200, stream.status_code)
                for line in stream.iter_lines():
                    if line.startswith("data: "):
                        received.append(json.loads(line[len("data: "):]))
                    if any(evt.get("type") == "run_complete" for evt in received):
                        break

            cell_events = [evt for evt in received if evt.get("type") == "cell"]
            self.assertEqual(1, len(cell_events))
            self.assertEqual("d0q0", cell_events[0]["cell_id"])
            self.assertEqual(0, cell_events[0]["document_index"])
            self.assertEqual(0, cell_events[0]["question_index"])
            self.assertEqual("What was total revenue?", cell_events[0]["question"])
            self.assertIn(cell_events[0]["status"], {"completed", "failed", "needs_review", "not_disclosed"})
            self.assertEqual("run_complete", received[-1]["type"])
            self.assertEqual(run_id, received[-1]["run_id"])

    def test_sse_stream_reports_missing_run(self):
        with self.client.stream("GET", "/api/runs/does-not-exist/events") as response:
            chunks = list(response.iter_lines())

        self.assertEqual(200, response.status_code)
        self.assertTrue(any("run not found" in line for line in chunks))

    def test_oversized_pdf_is_rejected(self):
        import main

        original = main.MAX_PDF_BYTES
        main.MAX_PDF_BYTES = 100
        self.addCleanup(setattr, main, "MAX_PDF_BYTES", original)

        response = self.analyze("exact-model", b"x" * 200)

        self.assertEqual(400, response.status_code)
        self.assertIn("size limit", response.json()["error"])

    def test_extract_pages_enforces_page_cap(self):
        from main import PdfTooManyPagesError, extract_pages_from_pdf

        with tempfile.TemporaryDirectory() as tmpdir:
            pdf_path = Path(tmpdir) / "one.pdf"
            pdf_path.write_bytes(make_pdf("single page content"))

            with self.assertRaises(PdfTooManyPagesError):
                extract_pages_from_pdf(str(pdf_path), max_pages=0)

    def test_persistence_helpers_exposed_as_v1_api(self):
        import main

        original_db_path = main.MATRIX_DB_PATH
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "matrix.sqlite3"
            main.MATRIX_DB_PATH = db_path
            self.addCleanup(setattr, main, "MATRIX_DB_PATH", original_db_path)

            run_id = main.persist_analyzed_run(
                "doc.pdf",
                ["Q1", "Q2"],
                [
                    {
                        "question": "Q1",
                        "answer": "answer one",
                        "quote": "quote one",
                        "citation_status": "verified_exact",
                        "page": 1,
                    },
                    {
                        "question": "Q2",
                        "answer": "answer two",
                        "quote": "",
                        "citation_status": "not_found",
                        "page": None,
                    },
                ],
            )

            client = TestClient(main.app)

            health = client.get("/api/v1/health")
            self.assertEqual(200, health.status_code)
            self.assertEqual("ok", health.json()["status"])

            runs = client.get("/api/v1/runs")
            self.assertEqual(200, runs.status_code)
            self.assertIn(run_id, runs.json()["runs"])

            summary = client.get(f"/api/v1/runs/{run_id}")
            self.assertEqual(200, summary.status_code)
            self.assertEqual(2, summary.json()["total"])
            self.assertEqual(1, summary.json()["completed"])
            self.assertEqual(1, summary.json()["needs_review"])

            errors = client.get(f"/api/v1/runs/{run_id}/errors")
            self.assertEqual(200, errors.status_code)
            self.assertEqual([], errors.json()["errors"])

            status_counts = client.get("/api/v1/stats/status-counts")
            self.assertEqual(200, status_counts.status_code)
            self.assertEqual(1, status_counts.json()["completed"])
            self.assertEqual(1, status_counts.json()["needs_review"])

            citation_counts = client.get("/api/v1/stats/citation-status-counts")
            self.assertEqual(200, citation_counts.status_code)
            self.assertEqual(1, citation_counts.json()["verified_exact"])
            self.assertEqual(1, citation_counts.json()["not_found"])

            avg_length = client.get("/api/v1/stats/average-answer-length")
            self.assertEqual(200, avg_length.status_code)
            self.assertIn("completed", avg_length.json())

            total = client.get("/api/v1/stats/total-cells")
            self.assertEqual(200, total.status_code)
            self.assertEqual(2, total.json()["total"])

            failed = client.get("/api/v1/stats/most-failed-questions")
            self.assertEqual(200, failed.status_code)
            self.assertEqual([], failed.json()["questions"])

            rate = client.get("/api/v1/stats/citation-verification-rate")
            self.assertEqual(200, rate.status_code)
            self.assertEqual(1.0, rate.json()["rate"])

    def test_persistence_api_returns_404_for_missing_run(self):
        response = self.client.get("/api/v1/runs/does-not-exist")

        self.assertEqual(404, response.status_code)


class UiShellTests(unittest.TestCase):
    """P6-1: React UI shell is served under /ui when built (ui/dist/index.html exists)."""

    def test_ui_shell_served_when_built(self):
        from fastapi import FastAPI

        import main
        from fastapi.testclient import TestClient

        index = Path(__file__).resolve().parents[1] / "ui" / "dist" / "index.html"
        if not index.is_file():
            self.skipTest("ui/dist not built; skipping /ui route test")
        test_app = FastAPI()
        mounted = main._mount_ui_shell(test_app, index)  # noqa: SLF001
        self.assertTrue(mounted)
        client = TestClient(test_app)
        resp = client.get("/ui/")
        self.assertEqual(200, resp.status_code)
        self.assertIn("<div id=\"root\">", resp.text)
        self.assertIn("/ui/assets/", resp.text)

    def test_ui_not_mounted_without_index_html(self):
        from fastapi import FastAPI
        from unittest import mock

        import main

        missing = Path("ui") / "dist" / "index.html"  # a path that does not exist
        test_app = FastAPI()
        with mock.patch.object(type(missing), "is_file", return_value=False):
            mounted = main._mount_ui_shell(test_app, missing)  # noqa: SLF001
        self.assertFalse(mounted)
        self.assertFalse(any(getattr(r, "path", None) == "/ui" for r in test_app.routes))


class DocumentLibraryApiTests(unittest.TestCase):
    """P6-3: REST endpoints wrapping library.default_library_store."""

    def setUp(self):
        import tempfile

        import library
        import main

        self._tmp = tempfile.TemporaryDirectory()
        self._orig_root = os.environ.get("OPENMATRIX_LIBRARY_ROOT")
        os.environ["OPENMATRIX_LIBRARY_ROOT"] = self._tmp.name
        library._default_store = None  # fresh store under the temp root
        from library import default_library_store

        self._store = default_library_store()
        self._patcher = mock.patch.object(main, "_library_store", return_value=self._store)
        self._patcher.start()
        self.client = TestClient(main.app)

    def tearDown(self):
        import asyncio

        import library

        self._patcher.stop()
        asyncio.run(self._store.close())
        library._default_store = None
        if self._orig_root is None:
            os.environ.pop("OPENMATRIX_LIBRARY_ROOT", None)
        else:
            os.environ["OPENMATRIX_LIBRARY_ROOT"] = self._orig_root
        self._tmp.cleanup()

    def test_search_page_follows_form_feed(self):
        self.client.post("/api/v1/projects", json={"project_id":"physical", "name":"Physical pages"})
        response=self.client.post("/api/v1/projects/physical/documents",json={"doc_id":"d1","filename":"physical.txt","content":"a\fb\fTotal revenue 1,445,306","indexed":True})
        self.assertEqual(200,response.status_code)
        hits=self.client.post("/api/v1/projects/physical/search",json={"question":"Total revenue"}).json()["hits"]
        match=next(hit for hit in hits if "1,445,306" in hit["quote"])
        self.assertEqual(3,match["page"])
        self.client.post("/api/v1/projects/physical/documents",json={"doc_id":"d2","filename":"sections.txt","content":"intro\n\nNet income 777.7","indexed":True})
        hits=self.client.post("/api/v1/projects/physical/search",json={"question":"Net income"}).json()["hits"]
        match=next(hit for hit in hits if hit["filename"]=="sections.txt" and "777.7" in hit["quote"])
        self.assertIsNone(match["page"])
        self.assertEqual(2,match["section"])

    def test_pages_preserve_blank_physical_pages(self):
        self.client.post("/api/v1/projects",json={"project_id":"blank","name":"Blank page"})
        response=self.client.post("/api/v1/projects/blank/documents",json={"doc_id":"d1","filename":"blank.txt","pages":["intro","","Total revenue 333,444"],"indexed":True})
        self.assertEqual(200,response.status_code)
        self.assertEqual(3,response.json()["document"]["page_count"])
        hits=self.client.post("/api/v1/projects/blank/search",json={"question":"Total revenue"}).json()["hits"]
        self.assertEqual(3,next(hit for hit in hits if "333,444" in hit["quote"])["page"])

    def test_generated_pdf_upload_preserves_page_and_searches(self):
        self.client.post("/api/v1/projects", json={"project_id": "pdf-pages", "name": "PDF pages"})
        response = self.client.post(
            "/api/v1/projects/pdf-pages/upload",
            data={"indexed": "true"},
            files={"file": ("report.pdf", make_pdf("Total revenue was 1,445,306 thousand."), "application/pdf")},
        )
        self.assertEqual(200, response.status_code)
        document = response.json()["document"]
        self.assertEqual(1, document["page_count"])
        result = self.client.post("/api/v1/projects/pdf-pages/search", json={"question": "total revenue"})
        self.assertEqual(200, result.status_code)
        self.assertTrue(result.json()["hits"])
        self.assertEqual(1, result.json()["hits"][0]["page"])

    def test_project_crud_roundtrip(self):
        # create
        resp = self.client.post("/api/v1/projects", json={
            "project_id": "proj1", "name": "Q3 reports", "description": "quarterly"
        })
        self.assertEqual(200, resp.status_code)
        self.assertEqual("proj1", resp.json()["project"]["id"])

        # list
        listed = self.client.get("/api/v1/projects")
        self.assertEqual(200, listed.status_code)
        self.assertIn("proj1", [p["id"] for p in listed.json()["projects"]])

        # delete
        removed = self.client.delete("/api/v1/projects/proj1")
        self.assertEqual(200, removed.status_code)
        after = self.client.get("/api/v1/projects")
        self.assertNotIn("proj1", [p["id"] for p in after.json()["projects"]])

    def test_project_requires_id_and_name(self):
        resp = self.client.post("/api/v1/projects", json={"project_id": "x"})
        self.assertEqual(400, resp.status_code)

    def test_document_add_list_delete(self):
        self.client.post("/api/v1/projects", json={"project_id": "pd", "name": "Doc project"})
        add = self.client.post("/api/v1/projects/pd/documents", json={
            "doc_id": "d1", "filename": "a.txt",
            "content": "Revenue was 125 million dollars in 2025.",
        })
        self.assertEqual(200, add.status_code)

        listed = self.client.get("/api/v1/projects/pd/documents")
        self.assertEqual(200, listed.status_code)
        self.assertEqual(1, len(listed.json()["documents"]))

        deleted = self.client.delete("/api/v1/documents/d1")
        self.assertEqual(200, deleted.status_code)

    def test_browser_evidence_snapshot_persists_and_searches(self):
        self.client.post("/api/v1/projects", json={"project_id": "ev", "name": "Evidence"})
        payload = {
            "title": "Quarterly revenue page",
            "source_url": "https://example.com/investor/q1",
            "canonical_url": "https://example.com/investor/q1",
            "captured_at": "2026-08-28T10:00:00Z",
            "content": "Revenue was 125 million dollars.\n\nMargin improved to 25 percent.",
            "sections": ["Revenue was 125 million dollars.", "Margin improved to 25 percent."],
            "indexed": True,
        }
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            response = self.client.post("/api/v1/projects/ev/evidence-snapshots", json=payload)
        self.assertEqual(200, response.status_code)
        snapshot = response.json()["evidence_snapshot"]
        self.assertEqual("browser_current_page", snapshot["source_kind"])
        self.assertEqual("indexed", snapshot["status"])
        self.assertEqual(2, snapshot["section_count"])
        listed = self.client.get("/api/v1/projects/ev/documents").json()["documents"]
        self.assertEqual("https://example.com/investor/q1", listed[0]["provenance"]["source_url"])
        searched = self.client.post("/api/v1/projects/ev/search", json={"question": "revenue"})
        self.assertEqual(200, searched.status_code)
        self.assertTrue(searched.json()["hits"])

    def test_browser_evidence_snapshot_rejects_invalid_input(self):
        self.client.post("/api/v1/projects", json={"project_id": "bad_ev", "name": "Bad evidence"})
        valid = {
            "title": "Page",
            "source_url": "https://example.com/page",
            "content": "Captured page content.",
        }
        for key, value in (("source_url", "file:///secret"), ("source_url", "javascript:alert(1)"), ("content", "")):
            payload = dict(valid)
            payload[key] = value
            response = self.client.post("/api/v1/projects/bad_ev/evidence-snapshots", json=payload)
            self.assertEqual(400, response.status_code, msg=repr(payload))
        missing = self.client.post("/api/v1/projects/nope/evidence-snapshots", json=valid)
        self.assertEqual(404, missing.status_code)

    def test_browser_evidence_indexed_is_strict_boolean(self):
        self.client.post("/api/v1/projects", json={"project_id": "strict_ev", "name": "Strict evidence"})
        payload = {
            "title": "Page",
            "source_url": "https://example.com/page",
            "content": "Captured page content.",
        }
        for value in ("true", 1, 0, None, [], {}):
            response = self.client.post(
                "/api/v1/projects/strict_ev/evidence-snapshots",
                json={**payload, "indexed": value},
            )
            self.assertEqual(400, response.status_code, msg=repr(value))

    def test_browser_evidence_requires_admin_when_auth_is_enabled(self):
        import main
        import asyncio

        self.client.post("/api/v1/projects", json={"project_id": "auth_ev", "name": "Auth evidence"})
        payload = {
            "title": "Page",
            "source_url": "https://example.com/page",
            "content": "Captured page content.",
        }
        for role in ("member", "readonly", "user", ""):
            response = asyncio.run(
                main.api_projects_evidence_snapshot_add(
                    "auth_ev", payload, caller={"id": f"{role}-1", "role": role}
                )
            )
            self.assertEqual(403, response.status_code, msg=role)

    def test_browser_evidence_marks_url_as_client_asserted(self):
        import asyncio
        import main

        self.client.post("/api/v1/projects", json={"project_id": "asserted_ev", "name": "Asserted evidence"})
        payload = {
            "title": "Page",
            "source_url": "https://example.com/page",
            "content": "Captured page content.",
        }
        response = self.client.post("/api/v1/projects/asserted_ev/evidence-snapshots", json=payload)
        self.assertEqual(200, response.status_code)
        snapshot = response.json()["evidence_snapshot"]
        self.assertEqual("client_asserted", snapshot["provenance_basis"])
        listed = self.client.get("/api/v1/projects/asserted_ev/documents").json()["documents"]
        self.assertEqual("client_asserted", listed[0]["provenance"]["provenance_basis"])

    def test_add_document_bad_page_count_returns_400(self):
        self.client.post("/api/v1/projects", json={"project_id": "pb", "name": "Bad page"})
        for bad in ["abc", -5, None, True, [], {}, 1.5]:
            resp = self.client.post("/api/v1/projects/pb/documents", json={
                "doc_id": f"d{len(str(bad))}", "filename": "x.txt", "content": "c",
                "page_count": bad,
            })
            self.assertEqual(400, resp.status_code, msg=f"page_count={bad!r}")

    def test_add_document_bad_indexed_returns_400(self):
        self.client.post("/api/v1/projects", json={"project_id": "pi", "name": "Bad indexed"})
        for bad in ["false", 1, 0, [], {}, None, 1.5]:
            resp = self.client.post("/api/v1/projects/pi/documents", json={
                "doc_id": f"di{len(str(bad))}", "filename": "x.txt", "content": "c",
                "indexed": bad,
            })
            self.assertEqual(400, resp.status_code, msg=f"indexed={bad!r}")

    def test_search_bad_limit_returns_400(self):
        self.client.post("/api/v1/projects", json={"project_id": "pl", "name": "Bad limit"})
        for bad in ["abc", 0, 101, None, True, 1.9, [], {}]:
            resp = self.client.post("/api/v1/projects/pl/search", json={"question": "x", "limit": bad})
            self.assertEqual(400, resp.status_code, msg=f"limit={bad!r}")

    def test_search_returns_hits_via_indexed_flag(self):
        # end-to-end via public REST endpoints only: add with indexed=true,
        # then search - no direct store mutation.
        self.client.post("/api/v1/projects", json={"project_id": "ps", "name": "Search"})
        add = self.client.post("/api/v1/projects/ps/documents", json={
            "doc_id": "ds", "filename": "b.txt",
            "content": "Research investment was 18 million dollars in 2025.",
            "indexed": True,
        })
        self.assertEqual(200, add.status_code)
        self.assertEqual("indexed", add.json()["document"]["status"])

        resp = self.client.post("/api/v1/projects/ps/search", json={
            "question": "research investment", "limit": 2
        })
        self.assertEqual(200, resp.status_code)
        hits = resp.json()["hits"]
        self.assertTrue(len(hits) >= 1)
        self.assertEqual("ds", hits[0]["document_id"])

    def test_missing_project_returns_404(self):
        resp = self.client.get("/api/v1/projects/nope/documents")
        self.assertEqual(404, resp.status_code)
        resp2 = self.client.post("/api/v1/projects/nope/search", json={"question": "x"})
        self.assertEqual(404, resp2.status_code)
        resp3 = self.client.post("/api/v1/projects/nope/documents", json={
            "doc_id": "d", "filename": "f.txt", "content": "c",
        })
        self.assertEqual(404, resp3.status_code)

    def test_defaults_when_keys_absent(self):
        self.client.post("/api/v1/projects", json={"project_id": "pdft", "name": "Defaults"})
        # absent page_count -> 0, absent indexed -> false
        add = self.client.post("/api/v1/projects/pdft/documents", json={
            "doc_id": "dflt", "filename": "f.txt", "content": "Some content.",
        })
        self.assertEqual(200, add.status_code)
        doc = add.json()["document"]
        self.assertEqual(0, doc["page_count"])
        self.assertEqual("uploaded", doc["status"])
        # absent limit -> 3 (valid search)
        resp = self.client.post("/api/v1/projects/pdft/search", json={"question": "some"})
        self.assertEqual(200, resp.status_code)

    def test_page_count_zero_and_limit_boundaries_accepted(self):
        self.client.post("/api/v1/projects", json={"project_id": "pbd", "name": "Boundary"})
        add = self.client.post("/api/v1/projects/pbd/documents", json={
            "doc_id": "db", "filename": "b.txt", "content": "Boundary content.",
            "page_count": 0,
        })
        self.assertEqual(200, add.status_code)
        for limit in (1, 100):
            resp = self.client.post("/api/v1/projects/pbd/search", json={"question": "boundary", "limit": limit})
            self.assertEqual(200, resp.status_code, msg=f"limit={limit}")

    def test_non_object_and_null_body_returns_400(self):
        self.client.post("/api/v1/projects", json={"project_id": "pnb", "name": "Null body"})
        for bad_body in (None, [], "text", 42):
            resp = self.client.post("/api/v1/projects/pnb/documents", content=json.dumps(bad_body),
                                    headers={"Content-Type": "application/json"})
            self.assertEqual(400, resp.status_code, msg=f"body={bad_body!r}")
            resp2 = self.client.post("/api/v1/projects/pnb/search", content=json.dumps(bad_body),
                                     headers={"Content-Type": "application/json"})
            self.assertEqual(400, resp2.status_code, msg=f"search body={bad_body!r}")

    def test_required_string_fields_reject_non_strings(self):
        # create project with non-string fields -> 400
        resp = self.client.post("/api/v1/projects", json={
            "project_id": True, "name": ["x"],
        })
        self.assertEqual(400, resp.status_code)
        self.client.post("/api/v1/projects", json={"project_id": "pst", "name": "Strict"})
        # add document with non-string doc_id/filename/content -> 400
        for bad in (True, 5, ["x"], {"k": 1}):
            resp = self.client.post("/api/v1/projects/pst/documents", json={
                "doc_id": bad, "filename": "f.txt", "content": "c",
            })
            self.assertEqual(400, resp.status_code, msg=f"doc_id={bad!r}")
            resp2 = self.client.post("/api/v1/projects/pst/documents", json={
                "doc_id": "ok", "filename": bad, "content": "c",
            })
            self.assertEqual(400, resp2.status_code, msg=f"filename={bad!r}")
        # non-string content -> 400
        for bad_content in (42, ["x"], {"k": 1}, True):
            resp3 = self.client.post("/api/v1/projects/pst/documents", json={
                "doc_id": "ok", "filename": "f.txt", "content": bad_content,
            })
            self.assertEqual(400, resp3.status_code, msg=f"content={bad_content!r}")
        # missing / null / empty / whitespace content -> 400
        for bad_content in (None, "", "   "):
            resp5 = self.client.post("/api/v1/projects/pst/documents", json={
                "doc_id": "ok2", "filename": "f.txt", "content": bad_content,
            })
            self.assertEqual(400, resp5.status_code, msg=f"content={bad_content!r}")
        resp6 = self.client.post("/api/v1/projects/pst/documents", json={
            "doc_id": "ok3", "filename": "f.txt",
        })
        self.assertEqual(400, resp6.status_code)  # content key absent
        # non-string question -> 400
        resp4 = self.client.post("/api/v1/projects/pst/search", json={"question": True})
        self.assertEqual(400, resp4.status_code)

    def test_valid_content_add_persists(self):
        # positive regression: valid non-empty content still adds and persists
        self.client.post("/api/v1/projects", json={"project_id": "pvc", "name": "Valid content"})
        add = self.client.post("/api/v1/projects/pvc/documents", json={
            "doc_id": "dvc", "filename": "f.txt", "content": "Real document content.",
        })
        self.assertEqual(200, add.status_code)
        listed = self.client.get("/api/v1/projects/pvc/documents")
        self.assertEqual(200, listed.status_code)
        self.assertEqual("dvc", listed.json()["documents"][0]["id"])

    def test_search_rejects_invalid_mode(self):
        self.client.post("/api/v1/projects", json={"project_id": "pm", "name": "Mode"})
        resp = self.client.post("/api/v1/projects/pm/search", json={"question": "x", "mode": "vector"})
        self.assertEqual(400, resp.status_code)
        self.assertIn("mode", resp.json()["error"])

    def test_search_accepts_dense_and_fusion_modes(self):
        # dense/fusion are valid modes; project missing -> 404 (mode accepted first)
        self.client.post("/api/v1/projects", json={"project_id": "pdm", "name": "Dense mode"})
        for mode in ("dense", "fusion", "bm25"):
            resp = self.client.post("/api/v1/projects/pdm/search", json={"question": "x", "mode": mode})
            self.assertEqual(200, resp.status_code, msg=f"mode={mode}")

    def test_empty_body_whitespace_and_description_type(self):
        # empty dict body -> field validation 400
        resp = self.client.post("/api/v1/projects", json={})
        self.assertEqual(400, resp.status_code)
        # whitespace-only strings rejected
        resp2 = self.client.post("/api/v1/projects", json={"project_id": "  ", "name": "x"})
        self.assertEqual(400, resp2.status_code)
        # non-string description -> 400
        self.client.post("/api/v1/projects", json={"project_id": "pdesc", "name": "D"})
        resp3 = self.client.post("/api/v1/projects", json={
            "project_id": "pdesc2", "name": "D2", "description": 42,
        })
        self.assertEqual(400, resp3.status_code)


class ResearchApiTests(unittest.TestCase):
    """P6-6: research-run REST endpoints (chat UI backend), manager mocked."""

    def setUp(self):
        import main

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # fake manager with controllable results
        self.fake = mock.MagicMock()
        self.fake.start.return_value = mock.MagicMock(status=lambda: {"phase": "running"})
        self.fake.status.return_value = {
            "research_id": "r1", "objective": "o", "phase": "completed", "error": None,
            "steps": [{"step_id": "s1", "tool": "retrieve_context", "status": "completed", "attempts": 1, "error": None}],
        }
        self.fake.cancel.return_value = True
        self.fake.list.return_value = ["r1", "r2"]
        self._patcher = mock.patch.object(main, "_run_manager", return_value=self.fake)
        self._patcher.start()
        self.client = TestClient(main.app)

    def tearDown(self):
        self._patcher.stop()

    def test_research_start_status_cancel(self):
        resp = self.client.post("/api/v1/research", json={
            "research_id": "r1", "objective": "analyze Q3",
        })
        self.assertEqual(200, resp.status_code)
        self.assertEqual("r1", resp.json()["task_id"])

        st = self.client.get("/api/v1/research/r1")
        self.assertEqual(200, st.status_code)
        self.assertEqual("completed", st.json()["phase"])
        self.assertEqual("retrieve_context", st.json()["steps"][0]["tool"])

        cancelled = self.client.delete("/api/v1/research/r1")
        self.assertEqual(200, cancelled.status_code)
        self.assertTrue(cancelled.json()["cancelled"])

    def test_research_list(self):
        resp = self.client.get("/api/v1/research")
        self.assertEqual(200, resp.status_code)
        self.assertIn("r1", resp.json()["research"])

    def test_research_validation(self):
        # missing objective
        resp = self.client.post("/api/v1/research", json={"research_id": "r2"})
        self.assertEqual(400, resp.status_code)
        # reserved t- prefix
        resp2 = self.client.post("/api/v1/research", json={"research_id": "t-x", "objective": "o"})
        self.assertEqual(400, resp2.status_code)
        # non-object context
        resp3 = self.client.post("/api/v1/research", json={
            "research_id": "r3", "objective": "o", "context": "not-an-object",
        })
        self.assertEqual(400, resp3.status_code)
        # whitespace owner -> 400
        resp4 = self.client.post("/api/v1/research", json={
            "research_id": "r4", "objective": "o", "owner": "   ",
        })
        self.assertEqual(400, resp4.status_code)

    def test_research_non_object_body_returns_400(self):
        for bad_body in (None, [], "text", 42):
            resp = self.client.post("/api/v1/research", content=json.dumps(bad_body),
                                    headers={"Content-Type": "application/json"})
            self.assertEqual(400, resp.status_code, msg=f"body={bad_body!r}")

    def test_research_unknown_returns_404(self):
        self.fake.status.return_value = None
        resp = self.client.get("/api/v1/research/nope")
        self.assertEqual(404, resp.status_code)


class ObservabilityTests(unittest.TestCase):
    """Observability: detailed health + structured request logging."""

    def setUp(self):
        import main

        from fastapi.testclient import TestClient

        self.client = TestClient(main.app)

    def test_health_detailed_returns_components(self):
        r = self.client.get("/api/v1/health/detailed")
        self.assertEqual(200, r.status_code)
        data = r.json()
        self.assertEqual("ok", data["status"])
        self.assertIn("components", data)
        self.assertIn("metadata_store", data["components"])
        self.assertIn("vector_store", data["components"])

    def test_request_emits_structured_log(self):
        import io
        from unittest import mock

        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            self.client.get("/api/v1/health")
        log = buf.getvalue()
        self.assertIn('"event": "request"', log)
        self.assertIn('"path": "/api/v1/health"', log)
        self.assertIn('"status": 200', log)
        self.assertIn("duration_ms", log)


import main


class S4ModelsAndLoggingTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)

    def test_models_get_is_explicitly_rejected(self):
        response = self.client.get("/api/models")
        self.assertEqual(response.status_code, 405)
        self.assertIn("POST /api/models", response.json()["error"])

    def test_models_post_accepts_json_and_lists_models(self):
        class _Model:
            id = "mock-model"

        class _Response:
            data = [_Model()]

        class _Models:
            async def list(self):
                return _Response()

        class _Client:
            def __init__(self, *args, **kwargs):
                self.models = _Models()

        with mock.patch("main.AsyncOpenAI", return_value=_Client()), \
             mock.patch("main.TenacityRetryAdapter.decorate", side_effect=lambda fn: fn):
            response = self.client.post(
                "/api/models",
                json={"base_url": "https://proxy.example/v1", "api_key": "real-api-secret"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["models"], ["mock-model"])

    def test_model_exception_redacts_real_key(self):
        secret = "real-api-secret-without-sk-prefix"
        class _Client:
            def __init__(self, *args, **kwargs):
                raise RuntimeError(f"upstream rejected {secret}")

        with mock.patch("main.AsyncOpenAI", side_effect=_Client):
            response = self.client.post(
                "/api/models",
                json={"base_url": "https://proxy.example/v1", "api_key": secret},
            )
        self.assertEqual(response.status_code, 500)
        self.assertNotIn(secret, response.text)
        self.assertIn("[REDACTED_SECRET]", response.text)

    def test_sensitive_log_filter_redacts_bearer_and_query(self):
        record = logging.LogRecord("test", logging.ERROR, __file__, 1, "Bearer %s /?api_key=%s", ("real-token", "real-key"), None)
        self.assertTrue(main.SensitiveDataFilter().filter(record))
        self.assertNotIn("real-token", record.getMessage())
        self.assertNotIn("real-key", record.getMessage())
        self.assertIn("api_key=***", record.getMessage())

    def test_sensitive_log_filter_redacts_exception_info_and_sk_keys(self):
        try:
            raise RuntimeError("upstream sk-super-secret-token-xyz")
        except RuntimeError:
            record = logging.LogRecord("test", logging.ERROR, __file__, 1, "request failed", (), None)
            record.exc_info = sys.exc_info()
            self.assertTrue(main.SensitiveDataFilter().filter(record))
        self.assertIsNone(record.exc_info)
        self.assertNotIn("sk-super-secret-token-xyz", record.exc_text or "")

    def test_access_formatter_keeps_request_tuple_and_redacts_url(self):
        from uvicorn.logging import AccessFormatter
        record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1,
            '%s - "%s %s HTTP/%s" %d',
            ("127.0.0.1:1234", "GET", "/api/models?api_key=sk-SECRET123456", "1.1", 405), None)
        main.SensitiveDataFilter().filter(record)
        rendered = AccessFormatter('%(request_line)s %(status_code)s').format(record)
        self.assertIn("api_key=***", rendered)
        self.assertNotIn("SECRET123456", rendered)

if __name__ == "__main__":
    unittest.main()
