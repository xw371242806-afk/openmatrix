import inspect
import os
import unittest
from unittest import mock

from core.adapters.kg_adapter import (
    GraphHit,
    IndexResult,
    SectionDocument,
    KnowledgeGraphAdapter,
    LightRagKnowledgeGraphAdapter,
    create_kg_adapter,
)


class FakeEngine:
    def __init__(self, insert_result=None, query_result=None):
        self.insert_result = insert_result
        self.query_result = query_result if query_result is not None else {"chunks": []}
        self.docs = {}
        self.query_modes = []

    async def ainsert(self, text, ids=None, file_paths=None):
        self.insert_calls = getattr(self, "insert_calls", 0) + 1
        self.insert_inputs = text
        self.insert_ids = ids
        self.insert_file_paths = file_paths
        for doc_id, content in zip(ids or [str(len(self.docs))], text if isinstance(text, list) else [text]):
            self.docs[doc_id] = content
        self.file_paths = file_paths
        return self.insert_result

    async def aquery_data(self, question, param=None):
        self.query_modes.append(getattr(param, "mode", None))
        return self.query_result

    async def aget_docs_by_ids(self, doc_id):
        ids = doc_id if isinstance(doc_id, list) else [doc_id]
        return {item: {"status": "processed"} for item in ids}


class KnowledgeGraphContractTests(unittest.TestCase):
    def test_protocol_surface_matches_issue_contract(self):
        members = {name for name, _ in inspect.getmembers(KnowledgeGraphAdapter, inspect.isfunction)}
        self.assertLessEqual({"is_available", "index_document", "retrieve"}, members)

    def test_index_result_exposes_explicit_failure(self):
        result = IndexResult(success=False, error="failed")
        self.assertFalse(result.success)
        self.assertEqual("failed", result.error)

    def test_graph_hit_carries_citation_fields(self):
        hit = GraphHit(doc_id="d1", chunk_id="c1", page=2, text="Revenue was 125m.", score=0.9)
        self.assertEqual("d1", hit.doc_id)
        self.assertEqual("c1", hit.chunk_id)
        self.assertEqual(2, hit.page)

    def test_page_source_id_round_trip(self):
        source_id = LightRagKnowledgeGraphAdapter.page_source_id("report.pdf", 7)
        self.assertEqual(
            ("report.pdf", 7),
            LightRagKnowledgeGraphAdapter._parse_source_path(source_id),
        )


class FactoryTests(unittest.TestCase):
    def test_disabled_by_default_without_importing_engine(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            adapter = create_kg_adapter()
        self.assertFalse(adapter.is_available())

    def test_only_exact_truthy_values_enable(self):
        for value in ("True ", "1\n", "yes!", "TRUE\t", "on ", "enabled"):
            with mock.patch.dict(os.environ, {"OPENMATRIX_KG_ENABLED": value}, clear=True):
                adapter = create_kg_adapter()
            self.assertFalse(adapter.is_available(), msg=repr(value))

    def test_exact_truthy_values_reach_enabled_constructor(self):
        for value in ("1", "true", "yes", "on"):
            with mock.patch.dict(os.environ, {"OPENMATRIX_KG_ENABLED": value}, clear=True):
                with mock.patch(
                    "core.adapters.kg_adapter.LightRagKnowledgeGraphAdapter",
                    return_value=LightRagKnowledgeGraphAdapter(engine=FakeEngine(), enabled=True),
                ) as constructor:
                    adapter = create_kg_adapter()
            self.assertTrue(adapter.is_available())
            self.assertTrue(constructor.call_args.kwargs["enabled"])

    def test_usage_snapshot_is_a_copy(self):
        adapter = LightRagKnowledgeGraphAdapter(engine=FakeEngine(), enabled=True)
        usage = adapter.usage_snapshot()
        usage["llm_calls"] = 99
        self.assertEqual(0, adapter.usage_snapshot()["llm_calls"])


class AdapterBehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def test_unavailable_operations_return_explicit_degradation(self):
        adapter = LightRagKnowledgeGraphAdapter(engine=None, enabled=False)
        result = await adapter.index_document("d1", "text")
        self.assertFalse(result.success)
        self.assertEqual([], await adapter.retrieve("question"))

    async def test_index_success_reports_engine_counts(self):
        engine = FakeEngine(insert_result={"chunks_indexed": 3, "entities": ["A", "B"]})
        adapter = LightRagKnowledgeGraphAdapter(engine=engine, enabled=True)
        result = await adapter.index_document("doc-1", "Revenue was 125 million dollars.")
        self.assertTrue(result.success)
        self.assertEqual("doc-1", result.doc_id)
        self.assertEqual(3, result.chunks_indexed)
        self.assertEqual(2, result.entities_indexed)
        self.assertIn("doc-1", engine.docs)
        self.assertEqual(["doc-1"], engine.file_paths)

    async def test_index_sections_uses_one_batch_and_preserves_section_sources(self):
        engine = FakeEngine(insert_result={"chunks_indexed": 4, "entities": ["A", "B"]})
        adapter = LightRagKnowledgeGraphAdapter(engine=engine, enabled=True)
        sections = [
            SectionDocument(section_id="sec-1", text="Revenue", source_id="report.pdf#sec-1"),
            SectionDocument(section_id="sec-2", text="Margin", source_id="report.pdf#sec-2"),
        ]

        result = await adapter.index_sections("report.pdf", sections)

        self.assertTrue(result.success)
        self.assertEqual(1, engine.insert_calls)
        self.assertEqual(["Revenue", "Margin"], engine.insert_inputs)
        self.assertEqual(["report.pdf:sec-1", "report.pdf:sec-2"], engine.insert_ids)
        self.assertEqual(["report.pdf#sec-1", "report.pdf#sec-2"], engine.insert_file_paths)
        self.assertEqual(4, result.chunks_indexed)
        self.assertEqual(2, result.entities_indexed)

    async def test_index_sections_rejects_dom_like_input(self):
        adapter = LightRagKnowledgeGraphAdapter(engine=FakeEngine(), enabled=True)

        result = await adapter.index_sections(
            "report.pdf",
            [SectionDocument(section_id="sec-1", text="<div><script>alert(1)</script>Revenue</div>")],
        )

        self.assertFalse(result.success)
        self.assertIn("DOM", result.error or "")

    async def test_index_sections_requires_all_batch_documents_processed(self):
        class PartialEngine(FakeEngine):
            async def aget_docs_by_ids(self, doc_id):
                ids = doc_id if isinstance(doc_id, list) else [doc_id]
                return {ids[0]: {"status": "processed"}}

        adapter = LightRagKnowledgeGraphAdapter(
            engine=PartialEngine(insert_result={"chunks_indexed": 1}), enabled=True
        )
        result = await adapter.index_sections(
            "report.pdf", [SectionDocument("sec-1", "Revenue"), SectionDocument("sec-2", "Margin")]
        )

        self.assertFalse(result.success)
        self.assertIn("processing", result.error or "")
        self.assertFalse(adapter.is_available())

    async def test_index_sections_rejects_duplicate_ids_without_calling_engine(self):
        engine = FakeEngine()
        adapter = LightRagKnowledgeGraphAdapter(engine=engine, enabled=True)

        result = await adapter.index_sections(
            "report.pdf",
            [SectionDocument("sec-1", "Revenue"), SectionDocument("sec-1", "Margin")],
        )

        self.assertFalse(result.success)
        self.assertIn("duplicate", result.error or "")
        self.assertEqual(0, getattr(engine, "insert_calls", 0))

    async def test_index_sections_accepts_plain_text_that_mentions_html_tags(self):
        engine = FakeEngine(insert_result={"chunks_indexed": 1})
        adapter = LightRagKnowledgeGraphAdapter(engine=engine, enabled=True)

        result = await adapter.index_sections(
            "report.pdf", [SectionDocument("sec-1", "Use <div> as an example tag.")]
        )

        self.assertTrue(result.success)

    async def test_index_sections_rejects_invalid_source_id(self):
        engine = FakeEngine()
        adapter = LightRagKnowledgeGraphAdapter(engine=engine, enabled=True)

        result = await adapter.index_sections(
            "report.pdf", [SectionDocument("sec-1", "Revenue", source_id=123)]
        )

        self.assertFalse(result.success)
        self.assertIn("source_id", result.error or "")
        self.assertEqual(0, getattr(engine, "insert_calls", 0))

    async def test_index_failure_persistently_degrades_adapter(self):
        class ExplodingEngine(FakeEngine):
            async def ainsert(self, text, ids=None, file_paths=None):
                raise TimeoutError("provider timeout")

        adapter = LightRagKnowledgeGraphAdapter(engine=ExplodingEngine(), enabled=True)
        result = await adapter.index_document("doc-1", "text")
        self.assertFalse(result.success)
        self.assertIn("provider timeout", result.error)
        self.assertFalse(adapter.is_available())
        self.assertFalse((await adapter.index_document("doc-1", "text")).success)

    async def test_retrieve_maps_only_citation_preserving_chunks(self):
        engine = FakeEngine(query_result={"status": "success", "data": {"chunks": [
            {"file_path": "doc-1", "reference_id": "p2:c3", "page": 2,
             "content": "Debt stood at 40 million.", "score": 0.8},
            {"content": "Unattributed synthesis must not become evidence."},
        ]}})
        adapter = LightRagKnowledgeGraphAdapter(engine=engine, enabled=True)
        hits = await adapter.retrieve("debt", mode="global")
        self.assertEqual(1, len(hits))
        self.assertEqual("doc-1", hits[0].doc_id)
        self.assertEqual("p2:c3", hits[0].chunk_id)
        self.assertEqual(2, hits[0].page)
        self.assertEqual("global", engine.query_modes[0])

    async def test_retrieve_failure_degrades_to_empty_hits(self):
        class ExplodingEngine(FakeEngine):
            async def aquery_data(self, question, param=None):
                raise RuntimeError("query failed")

        adapter = LightRagKnowledgeGraphAdapter(engine=ExplodingEngine(), enabled=True)
        self.assertEqual([], await adapter.retrieve("q"))
        self.assertFalse(adapter.is_available())

    async def test_missing_structured_query_api_degrades(self):
        class TextOnlyEngine:
            async def ainsert(self, text, ids=None, file_paths=None):
                return None

            async def aquery(self, question, param=None):
                return "generated answer"

        adapter = LightRagKnowledgeGraphAdapter(engine=TextOnlyEngine(), enabled=True)
        self.assertEqual([], await adapter.retrieve("q"))
        self.assertFalse(adapter.is_available())

    async def test_malformed_query_payload_degrades(self):
        for payload in ("text", {"status": "error", "message": "bad query"},
                        {"status": "success", "data": []},
                        {"status": "success", "data": {"chunks": "bad"}},
                        {"status": "success", "data": {"chunks": [{"content": "no citation"}]}}):
            adapter = LightRagKnowledgeGraphAdapter(
                engine=FakeEngine(query_result=payload), enabled=True
            )
            self.assertEqual([], await adapter.retrieve("q"), msg=repr(payload))
            self.assertFalse(adapter.is_available(), msg=repr(payload))

    async def test_doc_id_round_trips_from_index_to_graph_hit(self):
        class RoundTripEngine(FakeEngine):
            async def aquery_data(self, question, param=None):
                doc_id = next(iter(self.docs))
                return {"status": "success", "data": {"chunks": [{
                    "file_path": self.file_paths[0],
                    "reference_id": f"{doc_id}:p1:c0",
                    "page": 1,
                    "content": self.docs[doc_id],
                }]}}

        engine = RoundTripEngine(insert_result={"chunks_indexed": 1})
        adapter = LightRagKnowledgeGraphAdapter(engine=engine, enabled=True)
        result = await adapter.index_document("doc-1", "Revenue was 125 million.")
        self.assertTrue(result.success)
        hits = await adapter.retrieve("revenue")
        self.assertEqual("doc-1", hits[0].doc_id)
        self.assertEqual("doc-1:p1:c0", hits[0].chunk_id)
        self.assertEqual(1, hits[0].page)


if __name__ == "__main__":
    unittest.main()
