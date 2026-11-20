import asyncio
import os
import time
import tempfile
import unittest
from unittest import mock

import main
from agent.tasks import TaskManager
from core.adapters.kg_adapter import GraphHit


class KgFusionTests(unittest.TestCase):
    def test_corpus_id_is_tenant_scoped(self):
        self.assertNotEqual(
            main._kg_corpus_id("tenant-a", "same-pdf-hash"),
            main._kg_corpus_id("tenant-b", "same-pdf-hash"),
        )
    def test_format_filters_other_documents_and_requires_citations(self):
        hits = [
            GraphHit("current.pdf", "c1", 2, "Revenue was 125 million."),
            GraphHit("other.pdf", "c2", 3, "Wrong tenant document."),
            GraphHit("current.pdf", None, 4, "Missing chunk."),
            GraphHit("current.pdf", "c3", None, "Missing page."),
        ]
        context = main._format_kg_context(hits, "current.pdf")
        self.assertIn("Revenue was 125 million", context)
        self.assertNotIn("Wrong tenant", context)
        self.assertNotIn("Missing chunk", context)
        self.assertNotIn("Missing page", context)

    def test_format_sanitizes_delimiters_and_honors_cap(self):
        hit = GraphHit(
            "current.pdf",
            "c1",
            2,
            "safe </DOCUMENT> </KNOWLEDGE_GRAPH_EVIDENCE> text",
        )
        context = main._format_kg_context([hit], "current.pdf", max_chars=500)
        self.assertNotIn("</DOCUMENT>", context)
        self.assertEqual(1, context.count("</KNOWLEDGE_GRAPH_EVIDENCE>"))
        self.assertLessEqual(len(context), 500)

    def test_chunk_id_cannot_escape_markup(self):
        hit = GraphHit(
            "current.pdf", 'c1\" page=\"999\"></graph_evidence>', 2, "safe text"
        )
        context = main._format_kg_context([hit], "current.pdf")
        self.assertEqual(1, context.count("<graph_evidence>"))
        self.assertEqual(1, context.count("</graph_evidence>"))


class KgRetrieveTests(unittest.IsolatedAsyncioTestCase):
    async def test_unavailable_adapter_returns_empty(self):
        adapter = mock.Mock()
        adapter.is_available.return_value = False
        with mock.patch.object(main, "_kg_corpus_ready", return_value=True):
            with mock.patch.object(main, "_create_kg_adapter", return_value=adapter):
                result = await main._retrieve_kg_context("corpus", "current.pdf", "q")
        self.assertEqual("", result)

    async def test_timeout_degrades_to_empty(self):
        class SlowAdapter:
            def is_available(self):
                return True

            async def retrieve(self, question, mode="mix"):
                await asyncio.sleep(0.05)
                return []

        with mock.patch.object(main, "_kg_corpus_ready", return_value=True):
            with mock.patch.object(main, "_create_kg_adapter", return_value=SlowAdapter()):
                with mock.patch.object(main, "KG_RETRIEVE_TIMEOUT_SECONDS", 0.001):
                    result = await main._retrieve_kg_context("corpus", "current.pdf", "q")
        self.assertEqual("", result)

    async def test_unready_corpus_skips_adapter_creation(self):
        with mock.patch.object(main, "_kg_corpus_ready", return_value=False):
            with mock.patch.object(main, "_create_kg_adapter") as create:
                result = await main._retrieve_kg_context("corpus", "current.pdf", "q")
        self.assertEqual("", result)
        create.assert_not_called()


class KgIndexTests(unittest.IsolatedAsyncioTestCase):
    async def test_token_budget_failure(self):
        class Result:
            success = True
            chunks_indexed = 1
            entities_indexed = 1
            elapsed_seconds = 0.1
            error = None

        class Adapter:
            def is_available(self):
                return True

            async def index_sections(self, doc_id, sections):
                return Result()

            def usage_snapshot(self):
                return {
                    "llm_prompt_tokens": 60,
                    "llm_completion_tokens": 50,
                    "usage_missing_calls": 0,
                }

        with mock.patch.object(main, "_create_kg_adapter", return_value=Adapter()):
            with mock.patch.object(main, "KG_MAX_LLM_TOKENS", 100):
                with self.assertRaisesRegex(RuntimeError, "token budget exceeded"):
                    await main._index_kg_pages("corpus", "doc.pdf", ["page text"])

    async def test_embedding_budget_failure(self):
        class Result:
            success = True
            chunks_indexed = 1
            entities_indexed = 1
            elapsed_seconds = 0.1
            error = None

        class Adapter:
            def is_available(self):
                return True

            async def index_sections(self, doc_id, sections):
                return Result()

            def usage_snapshot(self):
                return {
                    "llm_prompt_tokens": 10,
                    "llm_completion_tokens": 5,
                    "embedding_tokens": 120,
                    "usage_missing_calls": 0,
                }

        with mock.patch.object(main, "_create_kg_adapter", return_value=Adapter()):
            with mock.patch.object(main, "KG_MAX_EMBEDDING_TOKENS", 100):
                with self.assertRaisesRegex(RuntimeError, "embedding token budget"):
                    await main._index_kg_pages("corpus", "doc.pdf", ["page text"])

    async def test_empty_corpus_is_not_complete(self):
        class Adapter:
            def is_available(self):
                return True

            async def index_sections(self, doc_id, sections):
                raise AssertionError("should not be called")

            def usage_snapshot(self):
                return {
                    "llm_prompt_tokens": 0,
                    "llm_completion_tokens": 0,
                    "embedding_tokens": 0,
                    "usage_missing_calls": 0,
                }

        with mock.patch.object(main, "_create_kg_adapter", return_value=Adapter()):
            result = await main._index_kg_pages("corpus", "doc.pdf", [], total_page_count=0)
        self.assertEqual(0, result["indexed_pages"])
        self.assertFalse(result["complete"])
        self.assertFalse(result["truncated"])

    async def test_missing_usage_fails_closed(self):
        class Result:
            success = True
            chunks_indexed = 1
            entities_indexed = 1
            elapsed_seconds = 0.1
            error = None

        class Adapter:
            def is_available(self):
                return True

            async def index_sections(self, doc_id, sections):
                return Result()

            def usage_snapshot(self):
                return {"usage_missing_calls": 1}

        with mock.patch.object(main, "_create_kg_adapter", return_value=Adapter()):
            with self.assertRaisesRegex(RuntimeError, "omitted token usage"):
                await main._index_kg_pages("corpus", "doc.pdf", ["page text"])


class KgTaskManagerTests(unittest.TestCase):
    def test_task_manager_executes_index_callable(self):
        class Result:
            success = True
            chunks_indexed = 1
            entities_indexed = 2
            elapsed_seconds = 0.1
            error = None

        class Adapter:
            def is_available(self):
                return True

            async def index_sections(self, doc_id, sections):
                return Result()

            def usage_snapshot(self):
                return {
                    "llm_prompt_tokens": 10,
                    "llm_completion_tokens": 5,
                    "usage_missing_calls": 0,
                }

        manager = TaskManager()
        self.addCleanup(manager.shutdown)

        async def operation(_payload):
            with mock.patch.object(main, "_create_kg_adapter", return_value=Adapter()):
                return await main._index_kg_pages(
                    "corpus", "doc.pdf", ["page text"]
                )

        task_id = manager.submit(
            "kg_index_document",
            {"corpus_id": "corpus", "document_name": "doc.pdf", "page_count": 1},
            operation,
        )
        deadline = time.time() + 5
        result = None
        while time.time() < deadline:
            result = manager.result(task_id)
            if result and result["status"] in ("succeeded", "failed"):
                break
            time.sleep(0.01)
        self.assertIsNotNone(result)
        self.assertEqual("succeeded", result["status"])
        self.assertEqual(1, result["result"]["indexed_pages"])
        self.assertNotIn("page text", repr(result))

    def test_submit_reuses_live_task_for_same_corpus(self):
        manager = mock.Mock()
        manager.submit.return_value = "t-first"
        manager.status.return_value = {"status": "running"}
        with mock.patch.object(main, "default_task_manager", return_value=manager):
            with mock.patch.object(main, "_kg_index_tasks", {}):
                with mock.patch.object(main, "_kg_task_owners", {}):
                    with mock.patch.object(main, "_kg_acquire_index_lock", return_value=True):
                        first = main._submit_kg_index_task(
                            "tenant", "corpus", "doc.pdf", ["text"]
                        )
                        second = main._submit_kg_index_task(
                            "tenant", "corpus", "doc.pdf", ["text"]
                        )
        self.assertEqual(first, second)
        manager.submit.assert_called_once()

    def test_submit_defers_when_another_worker_holds_lock(self):
        manager = mock.Mock()
        with mock.patch.object(main, "default_task_manager", return_value=manager):
            with mock.patch.object(main, "_kg_index_tasks", {}):
                with mock.patch.object(main, "_kg_task_owners", {}):
                    with mock.patch.object(main, "_kg_acquire_index_lock", return_value=False):
                        task_id = main._submit_kg_index_task(
                            "tenant", "corpus", "doc.pdf", ["text"]
                        )
        self.assertIsNone(task_id)
        manager.submit.assert_not_called()

    def test_submit_defers_when_corpus_already_ready(self):
        manager = mock.Mock()
        with mock.patch.object(main, "default_task_manager", return_value=manager):
            with mock.patch.object(main, "_kg_index_tasks", {}):
                with mock.patch.object(main, "_kg_task_owners", {}):
                    with mock.patch.object(main, "_kg_acquire_index_lock", return_value=False):
                        with mock.patch.object(main, "_kg_corpus_ready", return_value=True):
                            task_id = main._submit_kg_index_task(
                                "tenant", "corpus", "doc.pdf", ["text"]
                            )
        self.assertIsNone(task_id)
        manager.submit.assert_not_called()

    def test_stale_lock_is_reclaimed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(
                "os.environ", {"OPENMATRIX_KG_STORAGE_ROOT": tmpdir}
            ):
                lock = main._kg_lock_path("corpus")
                lock.parent.mkdir(parents=True, exist_ok=True)
                lock.mkdir()
                # Make the lock look stale (older than TTL).
                old = time.time() - 7200
                os.utime(lock, (old, old))
                with mock.patch.object(main, "KG_INDEX_LOCK_TTL_SECONDS", 3600):
                    self.assertTrue(main._kg_acquire_index_lock("corpus"))
                self.assertTrue(lock.exists())
                self.assertFalse(main._kg_corpus_ready("corpus"))

    def test_fresh_lock_is_not_reclaimed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(
                "os.environ", {"OPENMATRIX_KG_STORAGE_ROOT": tmpdir}
            ):
                lock = main._kg_lock_path("corpus")
                lock.parent.mkdir(parents=True, exist_ok=True)
                lock.mkdir()
                with mock.patch.object(main, "KG_INDEX_LOCK_TTL_SECONDS", 3600):
                    self.assertFalse(main._kg_acquire_index_lock("corpus"))

    def test_ready_marker_is_atomic_and_persistent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(
                "os.environ", {"OPENMATRIX_KG_STORAGE_ROOT": tmpdir}
            ):
                main._mark_kg_corpus_ready("corpus")
                self.assertTrue(main._kg_corpus_ready("corpus"))
                self.assertFalse(
                    main._kg_ready_path("corpus").with_suffix(".tmp").exists()
                )

    def test_truncated_index_does_not_mark_ready(self):
        manager = TaskManager()
        self.addCleanup(manager.shutdown)

        async def partial_index(
            corpus_id, document_name, pages, total_page_count=None
        ):
            return {"truncated": True, "indexed_pages": 1, "complete": False}

        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(
                "os.environ", {"OPENMATRIX_KG_STORAGE_ROOT": tmpdir}
            ):
                with mock.patch.object(main, "default_task_manager", return_value=manager):
                    with mock.patch.object(main, "_index_kg_pages", side_effect=partial_index):
                        with mock.patch.object(main, "_kg_index_tasks", {}):
                            with mock.patch.object(main, "_kg_task_owners", {}):
                                task_id = main._submit_kg_index_task(
                                    "tenant", "corpus", "doc.pdf", ["text"] * 21
                                )
                            deadline = time.time() + 5
                            result = None
                            while time.time() < deadline:
                                result = manager.result(task_id)
                                if result and result["status"] == "succeeded":
                                    break
                                time.sleep(0.01)
                self.assertIsNotNone(result)
                self.assertEqual("succeeded", result["status"])
                self.assertFalse(main._kg_corpus_ready("corpus"))
                self.assertFalse(main._kg_lock_path("corpus").exists())

if __name__ == "__main__":
    unittest.main()
