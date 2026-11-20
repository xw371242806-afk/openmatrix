import os
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from main import (
    AiosqliteResultStore,
    Bm25SRetriever,
    IdentityReranker,
    OcrPreprocessor,
    OcrPreprocessorAdapter,
    ParserAdapter,
    ProgressEvent,
    RerankerAdapter,
    ResultStoreAdapter,
    RetrieverAdapter,
    SimpleSparseRetriever,
    TableExtractionAdapter,
    TableExtractorAdapter,
    execute_matrix_run,
)

def setUpModule():
    global _mock_bge_m3, _mock_reranker
    _mock_bge_m3 = mock.patch("main.DenseRetrieverAdapter._bge_m3_available", return_value=False)
    _mock_reranker = mock.patch("main.BgeRerankerAdapter.is_available", return_value=False)
    _mock_bge_m3.start()
    _mock_reranker.start()

def tearDownModule():
    _mock_bge_m3.stop()
    _mock_reranker.stop()


class ParserAdapterContractTests(unittest.TestCase):
    def test_extract_pages_from_pdf_conforms_to_parser_contract(self):
        from main import extract_pages_from_pdf

        self.assertIsInstance(extract_pages_from_pdf, ParserAdapter)

    def test_pypdf_parser_adapter_conforms_to_contract(self):
        from main import PypdfParserAdapter

        adapter = PypdfParserAdapter()
        self.assertIsInstance(adapter, ParserAdapter)

    def test_docling_parser_adapter_conforms_to_contract(self):
        from main import DoclingParserAdapter

        adapter = DoclingParserAdapter()
        self.assertIsInstance(adapter, ParserAdapter)

    def test_get_parser_adapter_factory(self):
        from main import DoclingParserAdapter, PypdfParserAdapter, get_parser_adapter

        self.assertIsInstance(get_parser_adapter("pypdf"), PypdfParserAdapter)
        self.assertIsInstance(get_parser_adapter("docling"), DoclingParserAdapter)
        self.assertIsInstance(get_parser_adapter("unknown"), PypdfParserAdapter)

    def test_docling_adapter_gracefully_falls_back_when_not_installed(self):
        import sys
        from unittest import mock
        from main import DoclingParserAdapter
        from tests.test_api import make_pdf

        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            tmp.write(make_pdf("sample text on page 1"))
            tmp_path = tmp.name

        try:
            adapter = DoclingParserAdapter()
            with mock.patch.object(adapter, "is_available", return_value=False):
                pages = adapter(tmp_path)
            self.assertEqual(1, len(pages))
            self.assertIn("sample text on page 1", pages[0])
        finally:
            os.remove(tmp_path)


class VectorStoreAdapterContractTests(unittest.TestCase):
    def test_vector_store_adapters_conform_to_contract(self):
        from main import InMemoryVectorStore, QdrantVectorStoreAdapter, VectorStoreAdapter, get_vector_store_adapter

        self.assertIsInstance(InMemoryVectorStore(), VectorStoreAdapter)
        self.assertIsInstance(QdrantVectorStoreAdapter(), VectorStoreAdapter)
        self.assertIsInstance(get_vector_store_adapter(), VectorStoreAdapter)

    def test_in_memory_vector_store_upsert_and_query(self):
        from main import Chunk, InMemoryVectorStore

        store = InMemoryVectorStore()
        chunks = [
            Chunk(chunk_id="p1:c0", page=1, text="cloud revenue growth", source_text="full p1"),
            Chunk(chunk_id="p2:c0", page=2, text="hardware margin decline", source_text="full p2"),
        ]
        vectors = [[1.0, 0.0], [0.0, 1.0]]
        count = store.upsert_chunks("test_col", chunks, vectors)
        self.assertEqual(2, count)

        hits = store.query_similar("test_col", [1.0, 0.0], limit=1, min_score=0.5)
        self.assertEqual(1, len(hits))
        self.assertEqual("p1:c0", hits[0].chunk.chunk_id)
        self.assertAlmostEqual(1.0, hits[0].score)

    def test_qdrant_adapter_falls_back_when_client_missing(self):
        import sys
        from unittest import mock
        from main import Chunk, QdrantVectorStoreAdapter

        adapter = QdrantVectorStoreAdapter(location=":memory:")
        with mock.patch.object(adapter, "is_available", return_value=False):
            chunks = [Chunk(chunk_id="p1:c0", page=1, text="test chunk", source_text="test text")]
            count = adapter.upsert_chunks("test_col", chunks, [[1.0, 0.0]])
            self.assertEqual(1, count)
            hits = adapter.query_similar("test_col", [1.0, 0.0], limit=1, min_score=0.5)
            self.assertEqual(1, len(hits))
            self.assertEqual("p1:c0", hits[0].chunk.chunk_id)


class RetrievalAdapterContractTests(unittest.TestCase):
    def test_sparse_retrievers_conform_to_retriever_contract(self):
        self.assertIsInstance(Bm25SRetriever(), RetrieverAdapter)
        self.assertIsInstance(SimpleSparseRetriever(), RetrieverAdapter)

    def test_retriever_contract_shape(self):
        from main import build_page_chunks, Chunk, RetrievalHit

        chunks = build_page_chunks(["alpha revenue grew"])
        hits = Bm25SRetriever().retrieve("revenue", chunks, limit=1)

        self.assertLessEqual(len(hits), 1)
        for hit in hits:
            self.assertIsInstance(hit, RetrievalHit)
            self.assertIsInstance(hit.chunk, Chunk)
            self.assertIsInstance(hit.score, float)


class TaskQueueAdapterContractTests(unittest.TestCase):
    def test_in_process_task_queue_conforms_to_contract(self):
        import asyncio
        from main import InProcessTaskQueue, TaskQueueAdapter

        queue = InProcessTaskQueue()
        self.assertIsInstance(queue, TaskQueueAdapter)
        self.assertTrue(queue.is_available())

        async def run():
            task_id = await queue.enqueue("analyze", {"doc": "a.pdf"})
            self.assertEqual("queued", await queue.status(task_id))
            # Cancel while queued -> success.
            self.assertTrue(await queue.cancel(task_id))
            self.assertEqual("cancelled", await queue.status(task_id))
            # Second cancel after already cancelled -> False.
            self.assertFalse(await queue.cancel(task_id))

            task_id2 = await queue.enqueue("analyze", {"doc": "b.pdf"})
            await queue.mark(task_id2, "completed", {"answer": "42"})
            self.assertEqual("completed", await queue.status(task_id2))
            result = await queue.result(task_id2)
            self.assertEqual("42", result["answer"])
            # Cancelling a completed task -> False.
            self.assertFalse(await queue.cancel(task_id2))
            self.assertEqual("unknown", await queue.status("missing"))

        asyncio.run(run())

    def test_task_queue_factory_returns_in_process_adapter(self):
        from main import InProcessTaskQueue, get_task_queue_adapter

        self.assertIsInstance(get_task_queue_adapter(), InProcessTaskQueue)


class RerankerAdapterContractTests(unittest.TestCase):
    def test_identity_reranker_conforms_to_reranker_contract(self):
        self.assertIsInstance(IdentityReranker(), RerankerAdapter)

    def test_reranker_contract_shape(self):
        from main import build_page_chunks, RetrievalHit

        chunks = build_page_chunks(["alpha revenue grew", "beta margin improved"])
        hits = [RetrievalHit(chunk=chunks[0], score=2.0), RetrievalHit(chunk=chunks[1], score=1.0)]
        ranked = IdentityReranker().rerank("revenue", hits, limit=1)

        self.assertEqual(1, len(ranked))
        self.assertEqual("p1:c0", ranked[0].chunk.chunk_id)


class OcrPreprocessorAdapterContractTests(unittest.TestCase):
    def test_ocr_preprocessor_conforms_to_contract(self):
        self.assertIsInstance(OcrPreprocessor(enabled=False), OcrPreprocessorAdapter)

    def test_ocr_contract_methods(self):
        adapter = OcrPreprocessor(enabled=False)
        self.assertTrue(callable(adapter.is_available))
        self.assertTrue(callable(adapter.preprocess))
        self.assertIsNone(adapter.preprocess("input.pdf", "out.pdf"))


class TableExtractionAdapterContractTests(unittest.TestCase):
    def test_table_extractor_conforms_to_contract(self):
        self.assertIsInstance(TableExtractorAdapter(), TableExtractionAdapter)

    def test_table_contract_returns_plain_dicts(self):
        adapter = TableExtractorAdapter()
        rows = adapter.extract("unused.pdf")

        self.assertIsInstance(rows, list)
        for row in rows:
            self.assertIsInstance(row, dict)


class ResultStoreAdapterContractTests(unittest.TestCase):
    def test_aiosqlite_store_conforms_to_contract(self):
        self.assertIsInstance(AiosqliteResultStore(), ResultStoreAdapter)

    def test_store_contract_persists_and_loads(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmpdir:
                store = AiosqliteResultStore(Path(tmpdir) / "matrix.sqlite3")
                try:
                    await store.persist_cell(
                        "run-1",
                        {
                            "cell_id": "d0q0",
                            "document_index": 0,
                            "document_name": "doc.pdf",
                            "question_index": 0,
                            "question": "Q1",
                            "status": "completed",
                            "answer": "a",
                            "quote": "q",
                            "citation_status": "verified_exact",
                            "page": 1,
                            "error": None,
                        },
                    )
                    stored = await store.load_run("run-1")
                    self.assertIsNotNone(stored)
                    self.assertEqual(1, len(stored["cells"]))
                finally:
                    await store.close()

        asyncio.run(scenario())


class ProgressEventContractTests(unittest.TestCase):
    def test_progress_event_has_stable_wire_shape(self):
        event = ProgressEvent(
            type="cell",
            run_id="run-abc",
            cell_id="d0q0",
            document_index=0,
            question_index=0,
            question="What was revenue?",
            status="completed",
        )

        self.assertEqual("cell", event.type)
        self.assertEqual("run-abc", event.run_id)
        self.assertEqual("d0q0", event.cell_id)
        self.assertEqual(0, event.document_index)
        self.assertEqual(0, event.question_index)
        self.assertEqual("completed", event.status)

    def test_progress_event_matches_sse_payload_shape(self):
        import json

        from main import TERMINAL_CELL_STATUSES

        payload = {
            "type": "cell",
            "run_id": "run-abc",
            "cell_id": "d0q0",
            "document_index": 0,
            "question_index": 0,
            "question": "What was revenue?",
            "status": "completed",
        }
        event = ProgressEvent(**payload)

        self.assertEqual(payload, {k: getattr(event, k) for k in payload})
        self.assertIn(event.status, TERMINAL_CELL_STATUSES)


if __name__ == "__main__":
    unittest.main()
