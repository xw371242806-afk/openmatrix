import asyncio
import os
import unittest
from unittest import mock

from fastapi.testclient import TestClient

import main
from core.xeno_matrix_pipeline import XenoMatrixDispatcher


class _Message:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.message = _Message(content)


class _Response:
    def __init__(self, content):
        self.choices = [_Choice(content)]


class _Completions:
    def __init__(self):
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return _Response("[OBJ: Extract] [ACT: Parse] [OUT: JSON]")
        if self.calls in (2, 3):
            return _Response('{"claim":"grounded"}')
        return _Response(
            '{"CLAIMS":["grounded"],"UNKNOWNS":[],"GATES":{"data":"PASS"},"VERDICT":"GO"}'
        )


class _Client:
    def __init__(self):
        self.chat = type("Chat", (), {"completions": _Completions()})()


class MatrixDispatcherTests(unittest.TestCase):
    def test_pipeline_returns_real_model_outputs(self):
        dispatcher = XenoMatrixDispatcher(_Client(), "fixture-model")

        async def recall(*_args, **_kwargs):
            return ["chunk one", "chunk two"]

        dispatcher.semantic_recall = recall
        result = asyncio.run(dispatcher.run_pipeline("fixture", "extract facts", top_k=2))

        self.assertEqual(2, result["processed_chunks"])
        self.assertEqual("GO", result["envelope"]["VERDICT"])
        self.assertEqual(2, len(result["shared_board_findings"]))
        self.assertNotIn("mock", str(result).lower())

    def test_missing_xlat_configuration_is_explicit(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            dispatcher = XenoMatrixDispatcher(_Client(), "fixture-model")
            with self.assertRaisesRegex(RuntimeError, "OPENMATRIX_XLAT_PATH"):
                asyncio.run(dispatcher.semantic_recall("query", "index"))


class MatrixApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)

    def test_status_reports_configuration_without_secrets(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            response = self.client.get("/api/matrix/status")
        self.assertEqual(200, response.status_code)
        self.assertFalse(response.json()["xlat_configured"])
        self.assertNotIn("api_key", response.json())

    def test_run_rejects_blank_query(self):
        response = self.client.post("/api/matrix/run", json={"query": "  ", "index": "docs"})
        self.assertEqual(400, response.status_code)

    def test_run_requires_llm_configuration(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            response = self.client.post("/api/matrix/run", json={"query": "facts", "index": "docs"})
        self.assertEqual(503, response.status_code)

    def test_run_rejects_non_string_fields_and_invalid_url_scheme(self):
        response = self.client.post("/api/matrix/run", json={"query": 42, "index": "docs"})
        self.assertEqual(422, response.status_code)

        response = self.client.post(
            "/api/matrix/run",
            json={
                "query": "facts",
                "index": "docs",
                "api_key": "test-key",
                "base_url": "file:///tmp/model",
                "model_name": "fixture-model",
            },
        )
        self.assertEqual(400, response.status_code)

    def test_run_returns_dispatcher_result(self):
        expected = {
            "xeno_ir": "[OBJ: Extract]",
            "processed_chunks": 1,
            "shared_board_findings": ["finding"],
            "envelope": {"CLAIMS": ["fact"], "UNKNOWNS": [], "GATES": {}, "VERDICT": "GO"},
            "raw_findings": [],
        }

        class FakeDispatcher:
            def __init__(self, openai_client, model_name):
                self.model_name = model_name

            async def run_pipeline(self, index_name, query, top_k):
                return expected

        with mock.patch.object(main, "XenoMatrixDispatcher", FakeDispatcher):
            response = self.client.post(
                "/api/matrix/run",
                json={
                    "query": "facts",
                    "index": "docs",
                    "api_key": "test-key",
                    "base_url": "http://127.0.0.1:9999/v1",
                    "model_name": "fixture-model",
                },
            )
        self.assertEqual(200, response.status_code)
        self.assertEqual(expected, response.json())


if __name__ == "__main__":
    unittest.main()
