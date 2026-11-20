import asyncio
import io
import tempfile
import unittest
import warnings
from pathlib import Path

warnings.filterwarnings(
    "ignore",
    message="Using `httpx` with `starlette.testclient` is deprecated",
    category=UserWarning,
)

from fastapi.testclient import TestClient

import main
from main import TenacityRetryAdapter, AiosqliteResultStore, execute_matrix_run, load_matrix_run

import httpx


def run(coro):
    return asyncio.run(coro)


class MatrixExecutionTests(unittest.TestCase):
    def test_three_by_three_run_persists_nine_terminal_cells(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "matrix.sqlite3"
            documents = [
                {"name": f"doc-{index}.pdf", "pages": [f"Document {index} says revenue was {index}00."]}
                for index in range(3)
            ]
            questions = ["What was revenue?", "What was margin?", "What was risk?"]

            async def answer_cell(document, question):
                return {
                    "question": question,
                    "answer": "The document disclosed revenue.",
                    "quote": document["pages"][0],
                }

            result = run(execute_matrix_run(documents, questions, answer_cell, db_path=db_path))

            self.assertEqual("completed", result["status"])
            self.assertEqual(9, len(result["cells"]))
            self.assertTrue(all(cell["status"] == "completed" for cell in result["cells"]))

            stored = load_matrix_run(result["run_id"], db_path=db_path)
            self.assertIsNotNone(stored)
            self.assertEqual(9, len(stored["cells"]))
            self.assertEqual(
                {"completed"},
                {cell["status"] for cell in stored["cells"]},
            )

    def test_concurrency_limit_is_enforced(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "matrix.sqlite3"
            documents = [{"name": f"doc-{index}.pdf", "pages": ["Exact quote."]} for index in range(3)]
            questions = ["Q1", "Q2", "Q3"]
            active = 0
            max_seen = 0
            lock = asyncio.Lock()

            async def answer_cell(document, question):
                nonlocal active, max_seen
                async with lock:
                    active += 1
                    max_seen = max(max_seen, active)
                await asyncio.sleep(0.01)
                async with lock:
                    active -= 1
                return {"question": question, "answer": "ok", "quote": "Exact quote."}

            run(execute_matrix_run(documents, questions, answer_cell, max_concurrency=2, db_path=db_path))

            self.assertLessEqual(max_seen, 2)

    def test_default_concurrency_limit_is_three(self):
        active = 0
        max_seen = 0
        lock = asyncio.Lock()

        async def answer_cell(document, question):
            nonlocal active, max_seen
            async with lock:
                active += 1
                max_seen = max(max_seen, active)
            await asyncio.sleep(0.01)
            async with lock:
                active -= 1
            return {"question": question, "answer": "ok", "quote": "Exact quote."}

        documents = [{"name": f"doc-{index}.pdf", "pages": ["Exact quote."]} for index in range(4)]
        questions = ["Q1", "Q2"]

        result = run(execute_matrix_run(documents, questions, answer_cell))

        self.assertEqual(8, len(result["cells"]))
        self.assertLessEqual(max_seen, 3)

    def test_cell_failure_does_not_cancel_full_run(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "matrix.sqlite3"
            documents = [{"name": f"doc-{index}.pdf", "pages": ["Exact quote."]} for index in range(3)]
            questions = ["Q1", "Q2", "Q3"]

            async def answer_cell(document, question):
                if document["name"] == "doc-1.pdf" and question == "Q2":
                    raise RuntimeError("cell provider failed")
                return {"question": question, "answer": "ok", "quote": "Exact quote."}

            result = run(execute_matrix_run(documents, questions, answer_cell, db_path=db_path))

            self.assertEqual(9, len(result["cells"]))
            statuses = [cell["status"] for cell in result["cells"]]
            self.assertEqual(1, statuses.count("failed"))
            self.assertEqual(8, statuses.count("completed"))

            stored = load_matrix_run(result["run_id"], db_path=db_path)
            failed = [cell for cell in stored["cells"] if cell["status"] == "failed"]
            self.assertEqual(1, len(failed))
            self.assertIn("cell provider failed", failed[0]["error"])

    def test_empty_quote_maps_to_not_disclosed_terminal_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "matrix.sqlite3"
            documents = [{"name": "doc.pdf", "pages": ["No disclosure."]}]
            questions = ["Q1"]

            async def answer_cell(document, question):
                return {"question": question, "answer": "The document does not say.", "quote": ""}

            result = run(execute_matrix_run(documents, questions, answer_cell, db_path=db_path))

            self.assertEqual("not_disclosed", result["cells"][0]["status"])

    def test_run_state_is_inspectable_through_api(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "matrix.sqlite3"
            original_db_path = main.MATRIX_DB_PATH
            main.MATRIX_DB_PATH = db_path
            self.addCleanup(setattr, main, "MATRIX_DB_PATH", original_db_path)
            documents = [{"name": "doc.pdf", "pages": ["Exact quote."]}]
            questions = ["Q1"]

            async def answer_cell(document, question):
                return {"question": question, "answer": "ok", "quote": "Exact quote."}

            result = run(execute_matrix_run(documents, questions, answer_cell, db_path=db_path))

            response = TestClient(main.app).get(f"/api/matrix-runs/{result['run_id']}")

            self.assertEqual(200, response.status_code)
            payload = response.json()
            self.assertEqual(result["run_id"], payload["run_id"])
            self.assertEqual("completed", payload["cells"][0]["status"])

    def test_csv_export_prefixes_formula_like_cells(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "matrix.sqlite3"
            original_db_path = main.MATRIX_DB_PATH
            main.MATRIX_DB_PATH = db_path
            self.addCleanup(setattr, main, "MATRIX_DB_PATH", original_db_path)
            documents = [{"name": "doc.pdf", "pages": ["Exact quote."]}]
            questions = ["Q1"]

            async def answer_cell(document, question):
                return {
                    "question": "=SUM(1,2)",
                    "answer": "=SUM(1,2)",
                    "quote": "Exact quote.",
                }

            result = run(execute_matrix_run(documents, questions, answer_cell, db_path=db_path))

            response = TestClient(main.app).get(f"/api/export-csv/{result['run_id']}")

            self.assertEqual(200, response.status_code)
            content = response.text
            self.assertIn("document_name", content)
            self.assertIn("'=SUM(1,2)", content)
            self.assertNotIn("\n=SUM(1,2)", content)

    def test_csv_export_missing_run_returns_404(self):
        response = TestClient(main.app).get("/api/export-csv/does-not-exist")
        self.assertEqual(404, response.status_code)

    def test_analyze_persists_run_id_for_export(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "matrix.sqlite3"
            original_db_path = main.MATRIX_DB_PATH
            main.MATRIX_DB_PATH = db_path
            self.addCleanup(setattr, main, "MATRIX_DB_PATH", original_db_path)

            run_id = main.persist_analyzed_run(
                "doc.pdf",
                ["Q1"],
                [
                    {
                        "question": "Q1",
                        "answer": "=NOW()",
                        "quote": "",
                        "citation_status": "not_applicable",
                        "page": None,
                    }
                ],
            )

            response = TestClient(main.app).get(f"/api/export-csv/{run_id}")
            self.assertEqual(200, response.status_code)
            self.assertIn("'=NOW()", response.text)

    def test_xlsx_export_produces_valid_workbook(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "matrix.sqlite3"
            original_db_path = main.MATRIX_DB_PATH
            main.MATRIX_DB_PATH = db_path
            self.addCleanup(setattr, main, "MATRIX_DB_PATH", original_db_path)

            run_id = main.persist_analyzed_run(
                "doc.pdf",
                ["Q1"],
                [
                    {
                        "question": "Q1",
                        "answer": "42",
                        "quote": "exact quote",
                        "citation_status": "verified_exact",
                        "page": 1,
                    }
                ],
            )

            response = TestClient(main.app).get(f"/api/export-xlsx/{run_id}")
            self.assertEqual(200, response.status_code)
            self.assertEqual(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                response.headers["content-type"],
            )

            from openpyxl import load_workbook

            workbook = load_workbook(io.BytesIO(response.content))
            sheet = workbook.active
            rows = list(sheet.iter_rows(values_only=True))
            self.assertEqual(["document_name", "question", "answer", "quote", "citation_status", "page", "support_status", "support_reason"], list(rows[0]))
            self.assertEqual("Q1", rows[1][1])
            self.assertEqual("42", rows[1][2])
            self.assertEqual("verified_exact", rows[1][4])

    def test_xlsx_export_neutralizes_formula_injection(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "matrix.sqlite3"
            original_db_path = main.MATRIX_DB_PATH
            main.MATRIX_DB_PATH = db_path
            self.addCleanup(setattr, main, "MATRIX_DB_PATH", original_db_path)

            run_id = main.persist_analyzed_run(
                "doc.pdf",
                ["Q1"],
                [
                    {
                        "question": "Q1",
                        "answer": "=CMD()",
                        "quote": "@evil",
                        "citation_status": "not_applicable",
                        "page": None,
                    }
                ],
            )

            response = TestClient(main.app).get(f"/api/export-xlsx/{run_id}")
            self.assertEqual(200, response.status_code)

            from openpyxl import load_workbook

            workbook = load_workbook(io.BytesIO(response.content))
            sheet = workbook.active
            rows = list(sheet.iter_rows(values_only=True))
            self.assertEqual("'=CMD()", rows[1][2])
            self.assertEqual("'@evil", rows[1][3])

    def test_xlsx_export_returns_404_for_missing_run(self):
        response = TestClient(main.app).get("/api/export-xlsx/no-such-run")
        self.assertEqual(404, response.status_code)

    def test_transient_failure_retried_up_to_bounded_attempts(self):
        attempts = []

        async def flaky_call():
            attempts.append(1)
            raise httpx.ConnectTimeout("temporary network failure", request=None)

        wrapped = TenacityRetryAdapter(max_attempts=3).decorate(flaky_call)

        with self.assertRaises(httpx.ConnectTimeout):
            run(wrapped())

        self.assertEqual(3, len(attempts))

    def test_transient_failure_succeeds_after_retry(self):
        attempts = []

        async def flaky_call():
            attempts.append(1)
            if len(attempts) < 3:
                raise TimeoutError("provider timed out")
            return "ok"

        wrapped = TenacityRetryAdapter(max_attempts=3).decorate(flaky_call)

        result = run(wrapped())

        self.assertEqual("ok", result)
        self.assertEqual(3, len(attempts))

    def test_non_transient_error_fails_fast(self):
        attempts = []

        async def auth_failure():
            attempts.append(1)
            raise PermissionError("invalid api key")

        wrapped = TenacityRetryAdapter(max_attempts=3).decorate(auth_failure)

        with self.assertRaises(PermissionError):
            run(wrapped())

        self.assertEqual(1, len(attempts))

    def test_transient_marker_detection(self):
        adapter = TenacityRetryAdapter()

        self.assertTrue(adapter.is_transient(TimeoutError("service unavailable")))
        self.assertTrue(adapter.is_transient(httpx.ConnectError("connection refused", request=None)))
        self.assertTrue(adapter.is_transient(RuntimeError("upstream 429 rate limit reached")))
        self.assertFalse(adapter.is_transient(ValueError("invalid request schema")))
        self.assertFalse(adapter.is_transient(FileNotFoundError("pdf missing")))

    def test_aiosqlite_store_persists_and_loads_cells(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmpdir:
                store = AiosqliteResultStore(Path(tmpdir) / "matrix.sqlite3")
                try:
                    await store.persist_cell(
                        "run-abc",
                        {
                            "cell_id": "d0q0",
                            "document_index": 0,
                            "document_name": "doc.pdf",
                            "question_index": 0,
                            "question": "Q1",
                            "status": "completed",
                            "answer": "42",
                            "quote": "exact quote",
                            "citation_status": "verified_exact",
                            "page": 3,
                            "error": None,
                        },
                    )
                    stored = await store.load_run("run-abc")
                    self.assertIsNotNone(stored)
                    self.assertEqual(1, len(stored["cells"]))
                    self.assertEqual("verified_exact", stored["cells"][0]["citation_status"])
                    self.assertEqual(3, stored["cells"][0]["page"])
                finally:
                    await store.close()

        run(scenario())

    def test_aiosqlite_store_load_returns_none_for_missing_run(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmpdir:
                store = AiosqliteResultStore(Path(tmpdir) / "matrix.sqlite3")
                try:
                    stored = await store.load_run("no-such-run")
                    self.assertIsNone(stored)
                finally:
                    await store.close()

        run(scenario())

    def test_aiosqlite_store_matches_sync_loads(self):
        def sync_scenario():
            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = Path(tmpdir) / "matrix.sqlite3"
                run_id = "run-sync-1"
                main.persist_matrix_cell(
                    run_id,
                    {
                        "cell_id": "d0q0",
                        "document_index": 0,
                        "document_name": "doc.pdf",
                        "question_index": 0,
                        "question": "Q1",
                        "status": "completed",
                        "answer": "x",
                        "quote": "q",
                        "citation_status": "verified_exact",
                        "page": 1,
                        "error": None,
                    },
                    db_path=db_path,
                )
                return load_matrix_run(run_id, db_path=db_path)

        async def async_scenario():
            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = Path(tmpdir) / "matrix.sqlite3"
                store = AiosqliteResultStore(db_path)
                try:
                    await store.persist_cell(
                        "run-async-1",
                        {
                            "cell_id": "d0q0",
                            "document_index": 0,
                            "document_name": "doc.pdf",
                            "question_index": 0,
                            "question": "Q1",
                            "status": "completed",
                            "answer": "x",
                            "quote": "q",
                            "citation_status": "verified_exact",
                            "page": 1,
                            "error": None,
                        },
                    )
                    return await store.load_run("run-async-1")
                finally:
                    await store.close()

        sync = sync_scenario()
        async_result = run(async_scenario())

        self.assertEqual(sync["cells"][0]["answer"], async_result["cells"][0]["answer"])
        self.assertEqual(sync["cells"][0]["citation_status"], async_result["cells"][0]["citation_status"])

    def test_sync_store_creates_missing_parent_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "missing" / "nested" / "matrix.sqlite3"
            run_id = "run-new-parent"

            main.persist_matrix_cell(
                run_id,
                {
                    "cell_id": "d0q0",
                    "document_index": 0,
                    "document_name": "doc.pdf",
                    "question_index": 0,
                    "question": "Q1",
                    "status": "completed",
                    "answer": "x",
                    "quote": "q",
                    "citation_status": "verified_exact",
                    "page": 1,
                    "error": None,
                },
                db_path=db_path,
            )

            self.assertTrue(db_path.parent.is_dir())
            self.assertTrue(db_path.is_file())
            self.assertEqual("x", load_matrix_run(run_id, db_path=db_path)["cells"][0]["answer"])

    def test_sync_store_supports_memory_database(self):
        main.init_matrix_store(":memory:")

    def test_sync_store_reuses_existing_parent_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "matrix.sqlite3"
            main.init_matrix_store(db_path)
            main.init_matrix_store(db_path)
            self.assertTrue(db_path.is_file())

    def test_matrix_db_path_none_uses_configured_default(self):
        self.assertEqual(main.MATRIX_DB_PATH, main.matrix_db_path(None))


if __name__ == "__main__":
    unittest.main()
