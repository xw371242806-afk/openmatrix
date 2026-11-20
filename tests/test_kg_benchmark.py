import argparse
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import kg_benchmark


class KgBenchmarkTests(unittest.IsolatedAsyncioTestCase):
    async def test_requires_three_real_pdfs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            args = argparse.Namespace(
                docs=tmpdir,
                working_dir=str(Path(tmpdir) / "kg"),
                mode="mix",
                question=["What was total revenue?"],
            )
            report, exit_code = await kg_benchmark.run(args)
        self.assertEqual(2, exit_code)
        self.assertEqual("failed", report["status"])
        self.assertIn("three real PDF", report["reason"])

    async def test_zero_hits_cannot_pass_citation_gate(self):
        documents = [
            {"name": f"report-{index}.pdf", "pages": ["text"], "text": "text"}
            for index in range(3)
        ]

        class SuccessfulIndex:
            success = True
            chunks_indexed = 1
            entities_indexed = 1
            error = None

        class EmptyHitAdapter:
            def is_available(self):
                return True

            async def index_document(self, doc_id, text):
                return SuccessfulIndex()

            async def retrieve(self, question, mode="mix"):
                return []

        args = argparse.Namespace(
            docs="unused",
            working_dir="unused",
            mode="mix",
            question=["What was total revenue?"],
        )
        with mock.patch.object(kg_benchmark, "collect_documents", return_value=documents):
            with mock.patch.object(kg_benchmark, "create_kg_adapter", return_value=EmptyHitAdapter()):
                report, exit_code = await kg_benchmark.run(args)

        self.assertEqual(2, exit_code)
        self.assertEqual("failed", report["status"])
        self.assertFalse(report["citation_metadata_complete"])

    async def test_unavailable_engine_is_distinct_failure(self):
        documents = [{"name": f"r-{index}.pdf", "pages": ["x"], "text": "x"}
                     for index in range(3)]

        class UnavailableAdapter:
            def is_available(self):
                return False

        args = argparse.Namespace(docs="unused", working_dir="unused", mode="mix", question=["q"])
        with mock.patch.object(kg_benchmark, "collect_documents", return_value=documents):
            with mock.patch.object(kg_benchmark, "create_kg_adapter", return_value=UnavailableAdapter()):
                report, exit_code = await kg_benchmark.run(args)
        self.assertEqual(2, exit_code)
        self.assertIn("unavailable", report["reason"])

    async def test_index_failure_is_reported(self):
        documents = [{"name": f"r-{index}.pdf", "pages": ["x"], "text": "x"}
                     for index in range(3)]

        class FailedIndex:
            success = False
            chunks_indexed = None
            entities_indexed = None
            error = "index failed"

        class FailingAdapter:
            def is_available(self):
                return True

            async def index_document(self, doc_id, text):
                return FailedIndex()

        args = argparse.Namespace(docs="unused", working_dir="unused", mode="mix", question=["q"])
        with mock.patch.object(kg_benchmark, "collect_documents", return_value=documents):
            with mock.patch.object(kg_benchmark, "create_kg_adapter", return_value=FailingAdapter()):
                report, exit_code = await kg_benchmark.run(args)
        self.assertEqual(2, exit_code)
        self.assertEqual("index failed", report["reason"])
        self.assertFalse(report["documents"][0]["success"])


if __name__ == "__main__":
    unittest.main()
