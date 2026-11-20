import json
import os
import tempfile
import threading
import unittest
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

warnings.filterwarnings(
    "ignore",
    message="Using `httpx` with `starlette.testclient` is deprecated",
    category=UserWarning,
)

from fastapi.testclient import TestClient

from main import (
    Bm25SRetriever,
    DenseRetrieverAdapter,
    OcrPreprocessor,
    ParentExpander,
    ReciprocalRankFusion,
    RetrievalHit,
    SimpleSparseRetriever,
    app,
    build_page_chunks,
    extract_ocr_text,
    extract_pages_from_pdf,
    extract_tables_from_pdf,
    select_retrieved_context,
    verify_matrix_citations,
)
from tests.test_api import make_pdf


def setUpModule():
    global _mock_bge_m3, _mock_reranker
    _mock_bge_m3 = mock.patch("main.DenseRetrieverAdapter._bge_m3_available", return_value=False)
    _mock_reranker = mock.patch("main.BgeRerankerAdapter.is_available", return_value=False)
    _mock_bge_m3.start()
    _mock_reranker.start()

def tearDownModule():
    _mock_bge_m3.stop()
    _mock_reranker.stop()


class CaptureOpenAIHandler(BaseHTTPRequestHandler):
    requests = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length))
        self.__class__.requests.append(request)
        content = json.dumps(
            [
                {
                    "question": "What was research investment?",
                    "answer": "18 million dollars.",
                    "quote": "Research investment was 18 million dollars.",
                }
            ]
        )
        self.send_json(
            200,
            {
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 0,
                "model": request.get("model"),
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


class RetrievalTests(unittest.TestCase):
    def test_page_chunks_have_stable_ids_and_source_pages(self):
        chunks = build_page_chunks(["Alpha revenue page.", "Beta margin page."], max_chars=100)

        self.assertEqual(["p1:c0", "p2:c0"], [chunk.chunk_id for chunk in chunks])
        self.assertEqual([1, 2], [chunk.page for chunk in chunks])
        self.assertEqual("Beta margin page.", chunks[1].source_text)

    def test_sparse_retriever_orders_by_score_then_page(self):
        chunks = build_page_chunks(
            [
                "Revenue grew slowly.",
                "Revenue revenue and margin improved.",
                "Revenue margin improved.",
            ]
        )

        hits = SimpleSparseRetriever().retrieve("revenue margin", chunks, limit=3)

        self.assertEqual(["p2:c0", "p3:c0", "p1:c0"], [hit.chunk.chunk_id for hit in hits])

    def test_bm25s_retriever_ranks_top_hit_matching_baseline(self):
        chunks = build_page_chunks(
            [
                "Revenue grew slowly.",
                "Revenue revenue and margin improved.",
                "Revenue margin improved.",
            ]
        )

        hits = Bm25SRetriever().retrieve("revenue", chunks, limit=3)

        self.assertTrue(hits)
        self.assertEqual("p2:c0", hits[0].chunk.chunk_id)
        self.assertGreater(hits[0].score, 0)

    def test_bm25s_retriever_returns_empty_for_no_match(self):
        chunks = build_page_chunks(["Revenue was disclosed."])

        hits = Bm25SRetriever().retrieve("zzzz-no-match", chunks, limit=3)

        self.assertEqual([], hits)

    def test_select_retrieved_context_uses_bm25s_by_default(self):
        pages = ["Revenue was 125 million dollars.", "Research investment was 18 million dollars."]

        context, fallback = select_retrieved_context(["What was research investment?"], pages)

        self.assertFalse(fallback)
        self.assertIn("[Page 2 | p2:c0]", context)

    def test_parent_expander_returns_parent_context_for_child_hits(self):
        pages = [
            "Revenue page with a very long disclosure paragraph that keeps going "
            "beyond a single chunk boundary to ensure multiple child chunks exist.",
            "Margin page.",
        ]
        chunks = build_page_chunks(pages, max_chars=40)
        children = [chunk for chunk in chunks if chunk.page == 1]
        self.assertGreaterEqual(len(children), 2)

        expander = ParentExpander(chunks, max_parent_chars=500)
        hits = [RetrievalHit(chunk=children[0], score=1.0)]
        expanded = expander.expand(hits)

        self.assertTrue(expanded)
        self.assertIn("[Page 1 | ", f"[Page {expanded[0].chunk.page} | {expanded[0].chunk.chunk_id}]")
        self.assertIn("revenue", expanded[0].chunk.text.lower())
        self.assertGreaterEqual(expanded[0].chunk.text, children[0].text)

    def test_parent_expander_truncates_oversized_parent(self):
        pages = ["Word " * 2000]
        chunks = build_page_chunks(pages, max_chars=100)
        self.assertGreater(len(chunks), 1)

        expander = ParentExpander(chunks, max_parent_chars=300)
        expanded = expander.expand([RetrievalHit(chunk=chunks[0], score=1.0)])

        self.assertTrue(expanded)
        self.assertLessEqual(len(expanded[0].chunk.text), 300)

    def test_rrf_fuses_sparse_and_dense_lists_deterministically(self):
        chunks = build_page_chunks(["A B C", "B C D", "C D E", "D E F"])
        sparse_hits = [
            RetrievalHit(chunk=chunks[0], score=10.0),
            RetrievalHit(chunk=chunks[1], score=5.0),
        ]
        dense_hits = [
            RetrievalHit(chunk=chunks[1], score=0.9),
            RetrievalHit(chunk=chunks[2], score=0.8),
        ]

        fused = ReciprocalRankFusion().fuse(sparse_hits, dense_hits)
        fused_again = ReciprocalRankFusion().fuse(sparse_hits, dense_hits)

        self.assertEqual([hit.chunk.chunk_id for hit in fused], [hit.chunk.chunk_id for hit in fused_again])
        self.assertEqual(["p2:c0", "p1:c0", "p3:c0"], [hit.chunk.chunk_id for hit in fused])
        self.assertEqual(1.0 / 62.0 + 1.0 / 61.0, fused[0].score)

    def test_rrf_scores_are_bounded_and_independent_of_ranker_magnitude(self):
        chunks = build_page_chunks(["A B C"])
        big = [RetrievalHit(chunk=chunks[0], score=999.0)]
        small = [RetrievalHit(chunk=chunks[0], score=0.001)]

        fused_big = ReciprocalRankFusion().fuse(big)
        fused_small = ReciprocalRankFusion().fuse(small)

        self.assertEqual(fused_big[0].score, fused_small[0].score)
        self.assertLessEqual(fused_big[0].score, 1.0)
        self.assertGreater(fused_big[0].score, 0.0)


    def test_retrieved_context_preserves_exact_citation_source(self):
        pages = [
            "Revenue was 125 million dollars.",
            "Research investment was 18 million dollars.",
        ]

        context, fallback = select_retrieved_context(["What was research investment?"], pages)
        verified = verify_matrix_citations(
            [
                {
                    "question": "What was research investment?",
                    "answer": "18 million dollars.",
                    "quote": "Research investment was 18 million dollars.",
                }
            ],
            pages,
        )

        self.assertFalse(fallback)
        self.assertIn("[Page 2 | p2:c0]", context)
        self.assertEqual("verified_exact", verified[0]["citation_status"])
        self.assertEqual(2, verified[0]["page"])

    def test_analyze_uses_bounded_retrieved_context(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), CaptureOpenAIHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        CaptureOpenAIHandler.requests = []
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        pdf = make_pdf(
            "Revenue was 125 million dollars. Research investment was 18 million dollars. Supply risk was limited."
        )
        response = TestClient(app).post(
            "/api/analyze",
            data={
                "api_key": "test-key",
                "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                "model_name": "exact-model",
                "questions": json.dumps(["What was research investment?"]),
            },
            files={"file": ("fixture.pdf", pdf, "application/pdf")},
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual("success", response.json()["status"])
        prompt = CaptureOpenAIHandler.requests[0]["messages"][1]["content"]
        self.assertIn("[Page 1 | p1:c0]", prompt)
        self.assertIn("Research investment was 18 million dollars", prompt)

    def test_empty_retrieval_falls_back_to_review_path(self):
        context, fallback = select_retrieved_context(["zzzz-no-match"], ["Revenue was disclosed."])

        self.assertTrue(fallback)
        self.assertEqual("Revenue was disclosed.", context)

    def test_empty_retrieval_fallback_dedupes_long_page(self):
        # A single very long page that will be split into many chunks.
        long_page = "A" * 2500
        context, fallback = select_retrieved_context(["zzzz-no-match"], [long_page])
        self.assertTrue(fallback)
        # Context should be the original page text, not repeated per chunk.
        self.assertEqual(long_page, context)

    def test_fusion_mode_is_disabled_by_default(self):
        pages = ["Revenue was 125 million dollars.", "Research investment was 18 million dollars."]

        context, fallback = select_retrieved_context(["What was research investment?"], pages)

        self.assertFalse(fallback)
        self.assertNotIn("OPENMATRIX_RETRIEVAL_MODE", os.environ)

    def test_fusion_mode_expands_parent_context_and_preserves_bm25_default(self):
        pages = [
            "Revenue was 125 million dollars.",
            "Research investment was 18 million dollars across multiple lines of disclosure.",
        ]

        context, fallback = select_retrieved_context(
            ["What was research investment?"],
            pages,
            mode="fusion",
        )

        self.assertFalse(fallback)
        self.assertIn("[Page 2 | p2:c0]", context)
        self.assertIn("Research investment was 18 million dollars", context)
        self.assertIn("multiple lines of disclosure", context)

    @mock.patch("main.DenseRetrieverAdapter._fastembed_available", return_value=False)
    @mock.patch("main.DenseRetrieverAdapter._bge_m3_available", return_value=False)
    def test_fusion_mode_falls_back_to_full_text_on_no_match(self, mock_bge, mock_fastembed):
        context, fallback = select_retrieved_context(["zzzz-no-match"], ["Revenue was disclosed."], mode="fusion")

        self.assertTrue(fallback)
        self.assertEqual("Revenue was disclosed.", context)


class OcrFallbackTests(unittest.TestCase):
    def _blank_pdf(self) -> Path:
        from pypdf import PdfWriter

        path = Path(tempfile.mktemp(suffix=".pdf"))
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        with path.open("wb") as handle:
            writer.write(handle)
        self.addCleanup(path.unlink, missing_ok=True)
        return path

    def test_ocr_fallback_returns_searchable_text_for_scanned_pdf(self):
        path = self._blank_pdf()

        def fake_ocr(pdf_path, page_number):
            return "scanned revenue text"

        pages = extract_pages_from_pdf(str(path), ocr_backend=fake_ocr)

        self.assertEqual(["scanned revenue text"], pages)

    def test_no_ocr_backend_keeps_original_empty_pages(self):
        path = self._blank_pdf()

        pages = extract_pages_from_pdf(str(path), ocr_backend=None)

        self.assertEqual([""], pages)

    def test_ocr_result_parsing_extracts_recognized_text(self):
        result = [
            [
                [
                    [[0, 0], [1, 1], [2, 2], [3, 3]],
                    ("recognized line", 0.95),
                ]
            ]
        ]
        self.assertEqual("recognized line", extract_ocr_text(result))

    def test_text_pdf_skips_ocr_fallback(self):
        from tests.test_api import make_pdf

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "text.pdf"
            path.write_bytes(make_pdf("Revenue was 125 million dollars."))

            def fake_ocr(pdf_path, page_number):
                raise AssertionError("OCR should not be called for a text PDF")

            pages = extract_pages_from_pdf(str(path), ocr_backend=fake_ocr)

            self.assertTrue(any(text.strip() for text in pages))


class OcrPreprocessorTests(unittest.TestCase):
    def _blank_pdf(self) -> Path:
        from pypdf import PdfWriter

        path = Path(tempfile.mktemp(suffix=".pdf"))
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        with path.open("wb") as handle:
            writer.write(handle)
        self.addCleanup(path.unlink, missing_ok=True)
        return path

    def test_disabled_preprocessor_is_passthrough(self):
        import sys
        from unittest import mock

        adapter = OcrPreprocessor(enabled=False)

        with mock.patch.dict(sys.modules, {"ocrmypdf": mock.MagicMock()}):
            result = adapter.preprocess("input.pdf", "out.pdf")

        self.assertIsNone(result)

    def test_enabled_without_ocrmypdf_falls_back(self):
        import sys
        from unittest import mock

        adapter = OcrPreprocessor(enabled=True)

        with mock.patch.dict(sys.modules, {"ocrmypdf": None}):
            self.assertFalse(adapter.is_available())
            result = adapter.preprocess("input.pdf", "out.pdf")

        self.assertIsNone(result)

    def test_enabled_with_ocrmypdf_returns_ocr_output(self):
        import sys
        from unittest import mock

        adapter = OcrPreprocessor(enabled=True)
        fake_ocrmypdf = mock.MagicMock()

        with mock.patch.dict(sys.modules, {"ocrmypdf": fake_ocrmypdf}):
            self.assertTrue(adapter.is_available())
            result = adapter.preprocess("input.pdf", "out.pdf")

        self.assertEqual("out.pdf", result)
        fake_ocrmypdf.ocr.assert_called_once_with("input.pdf", "out.pdf", skip_text=True)

    def test_preprocessor_removes_temp_output_when_ocr_fails(self):
        import sys
        from unittest import mock

        adapter = OcrPreprocessor(enabled=True)
        fake_ocrmypdf = mock.MagicMock()
        fake_ocrmypdf.ocr.side_effect = RuntimeError("ghostscript missing")

        with mock.patch.dict(sys.modules, {"ocrmypdf": fake_ocrmypdf}):
            result = adapter.preprocess("input.pdf")

        self.assertIsNone(result)

    def test_scanned_pdf_uses_preprocessed_text_when_available(self):
        def fake_ocr(pdf_path, page_number):
            raise AssertionError("paddle fallback must not run when OCRmyPDF produced text")

        adapter = OcrPreprocessor(enabled=True)
        preprocessed = Path(tempfile.mktemp(suffix=".ocr.pdf"))
        self.addCleanup(preprocessed.unlink, missing_ok=True)
        from tests.test_api import make_pdf

        preprocessed.write_bytes(make_pdf("scanned revenue text from OCR"))

        with mock.patch.object(adapter, "preprocess", return_value=str(preprocessed)):
            pages = extract_pages_from_pdf(
                str(self._blank_pdf()),
                ocr_backend=fake_ocr,
                ocr_preprocessor=adapter,
            )

        self.assertEqual(["scanned revenue text from OCR"], pages)

    def test_scanned_pdf_with_unavailable_ocr_falls_back_to_backend(self):
        called = []

        def fake_ocr(pdf_path, page_number):
            called.append(page_number)
            return "paddle recognized text"

        adapter = OcrPreprocessor(enabled=True)

        with mock.patch.object(adapter, "preprocess", return_value=None):
            pages = extract_pages_from_pdf(
                str(self._blank_pdf()),
                ocr_backend=fake_ocr,
                ocr_preprocessor=adapter,
            )

        self.assertEqual(["paddle recognized text"], pages)
        self.assertEqual([0], called)

    def test_text_pdf_skips_preprocessor(self):
        import sys
        from unittest import mock as _mock

        from tests.test_api import make_pdf

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "text.pdf"
            path.write_bytes(make_pdf("Revenue was 125 million dollars."))

            adapter = OcrPreprocessor(enabled=True)
            with _mock.patch.object(adapter, "preprocess") as mock_preprocess:
                pages = extract_pages_from_pdf(str(path), ocr_backend=None, ocr_preprocessor=adapter)

            mock_preprocess.assert_not_called()
            self.assertTrue(any(text.strip() for text in pages))

    def test_disabled_preprocessor_never_touches_pdf(self):
        adapter = OcrPreprocessor(enabled=False)

        with mock.patch.object(adapter, "preprocess") as mock_preprocess:
            pages = extract_pages_from_pdf(
                str(self._blank_pdf()),
                ocr_backend=None,
                ocr_preprocessor=adapter,
            )

        mock_preprocess.assert_not_called()
        self.assertEqual([""], pages)


class BgeRerankerAdapterTests(unittest.TestCase):
    def setUp(self):
        import sys
        main_module = sys.modules.get("main")
        if main_module is not None:
            self._orig_reranker_models = getattr(main_module, "_bge_reranker_models", {}).copy()
            main_module._bge_reranker_models.clear()
        else:
            self._orig_reranker_models = {}

    def tearDown(self):
        import sys
        main_module = sys.modules.get("main")
        if main_module is not None:
            main_module._bge_reranker_models.clear()
            main_module._bge_reranker_models.update(self._orig_reranker_models)

    def test_degrades_to_identity_when_unavailable(self):
        from main import BgeRerankerAdapter, Chunk, RetrievalHit

        adapter = BgeRerankerAdapter()
        with mock.patch.object(adapter, "is_available", return_value=False):
            chunks = [Chunk(chunk_id=f"p{i}:c0", page=i, text="text", source_text="text") for i in range(1, 4)]
            hits = [RetrievalHit(chunk=c, score=float(i)) for i, c in enumerate(chunks, 1)]

            # Identity should preserve order and limit
            reranked = adapter.rerank("query", hits, limit=2)
            self.assertEqual(2, len(reranked))
            self.assertEqual([h.chunk.chunk_id for h in hits[:2]], [h.chunk.chunk_id for h in reranked])

    def test_degrades_to_identity_on_model_exception(self):
        from main import BgeRerankerAdapter, Chunk, RetrievalHit

        adapter = BgeRerankerAdapter()
        with mock.patch.object(adapter, "is_available", return_value=True):
            with mock.patch.object(adapter, "_get_model", side_effect=RuntimeError("offline")):
                chunks = [Chunk(chunk_id="p1:c0", page=1, text="test", source_text="test")]
                hits = [RetrievalHit(chunk=chunks[0], score=1.0)]
                reranked = adapter.rerank("query", hits)
                self.assertEqual(1, len(reranked))

    def test_reranks_using_cross_encoder_mock(self):
        from main import BgeRerankerAdapter, Chunk, RetrievalHit

        class FakeCrossEncoder:
            def predict(self, pairs):
                # Reverse the input order scores to prove it reranked
                return [float(len(pairs) - i) for i in range(len(pairs))]

        adapter = BgeRerankerAdapter(max_candidates=2)
        with mock.patch.object(adapter, "is_available", return_value=True):
            with mock.patch.object(adapter, "_get_model", return_value=FakeCrossEncoder()):
                chunks = [Chunk(chunk_id=f"p{i}:c0", page=i, text="test", source_text="test") for i in range(1, 4)]
                # Provide 3 hits
                hits = [RetrievalHit(chunk=c, score=float(i)) for i, c in enumerate(chunks)]

                # Should take top 2 candidates, score them as [2.0, 1.0], and sort descending
                # so p2 (index 1) gets score 1.0, p1 (index 0) gets score 2.0.
                reranked = adapter.rerank("query", hits)

                self.assertEqual(2, len(reranked))
                self.assertEqual("p1:c0", reranked[0].chunk.chunk_id)
                self.assertEqual("p2:c0", reranked[1].chunk.chunk_id)
                self.assertEqual(2.0, reranked[0].score)

    def test_falls_back_to_flag_reranker_when_cross_encoder_init_fails(self):
        import sys
        from main import BgeRerankerAdapter

        class FakeCrossEncoder:
            def __init__(self, *args, **kwargs):
                raise TypeError("bad activation kwarg")

        class FakeFlagReranker:
            def __init__(self, *args, **kwargs):
                pass

        fake_sentence_transformers = mock.MagicMock()
        fake_sentence_transformers.CrossEncoder = FakeCrossEncoder
        fake_flag_embedding = mock.MagicMock()
        fake_flag_embedding.FlagReranker = FakeFlagReranker

        # Inject fake modules WITHOUT importing the real optional packages, so
        # the test is hermetic and passes when sentence_transformers is absent.
        with mock.patch.dict(
            sys.modules,
            {
                "sentence_transformers": fake_sentence_transformers,
                "FlagEmbedding": fake_flag_embedding,
            },
        ):
            adapter = BgeRerankerAdapter()
            with mock.patch.object(adapter, "_cross_encoder_available", return_value=True):
                with mock.patch.object(adapter, "_flag_reranker_available", return_value=True):
                    model = adapter._get_model()
                    self.assertIsInstance(model, FakeFlagReranker)


    def test_falls_back_to_flag_reranker_when_cross_encoder_init_fails_and_reuses_cache(self):
        import sys
        from main import BgeRerankerAdapter

        counts = {"cross": 0, "flag": 0}

        class FakeCrossEncoder:
            def __init__(self, *args, **kwargs):
                counts["cross"] += 1
                raise TypeError("bad activation kwarg")

        class FakeFlagReranker:
            def __init__(self, *args, **kwargs):
                counts["flag"] += 1

        fake_sentence_transformers = mock.MagicMock()
        fake_sentence_transformers.CrossEncoder = FakeCrossEncoder
        fake_flag_embedding = mock.MagicMock()
        fake_flag_embedding.FlagReranker = FakeFlagReranker

        with mock.patch.dict(
            sys.modules,
            {
                "sentence_transformers": fake_sentence_transformers,
                "FlagEmbedding": fake_flag_embedding,
            },
        ):
            adapter = BgeRerankerAdapter()
            with mock.patch.object(adapter, "_cross_encoder_available", return_value=True):
                with mock.patch.object(adapter, "_flag_reranker_available", return_value=True):
                    model1 = adapter._get_model()
                    model2 = adapter._get_model()
                    self.assertIsInstance(model1, FakeFlagReranker)
                    self.assertIs(model1, model2)
                    self.assertEqual(1, counts["cross"])
                    self.assertEqual(1, counts["flag"])

    def test_flag_reranker_compute_score_scalar_is_handled(self):
        import sys
        from main import BgeRerankerAdapter, Chunk, RetrievalHit

        class FakeFlagReranker:
            def compute_score(self, pairs):
                return 0.42  # FlagReranker returns a bare float for one pair

        fake_flag_embedding = mock.MagicMock()
        fake_flag_embedding.FlagReranker = FakeFlagReranker

        with mock.patch.dict(sys.modules, {"FlagEmbedding": fake_flag_embedding}):
            adapter = BgeRerankerAdapter()
            with mock.patch.object(adapter, "is_available", return_value=True):
                with mock.patch.object(adapter, "_cross_encoder_available", return_value=False):
                    with mock.patch.object(adapter, "_flag_reranker_available", return_value=True):
                        with mock.patch.object(adapter, "_get_model", return_value=FakeFlagReranker()):
                            chunks = [Chunk(chunk_id="p1:c0", page=1, text="test", source_text="test")]
                            hits = [RetrievalHit(chunk=chunks[0], score=1.0)]
                            reranked = adapter.rerank("query", hits, limit=1)
                            self.assertEqual(1, len(reranked))
                            self.assertEqual(0.42, reranked[0].score)
                            self.assertEqual("bge-reranker-v2-m3", adapter.effective_backend())

    def test_preserves_all_hits_on_short_score_output(self):
        from main import BgeRerankerAdapter, Chunk, RetrievalHit

        class ShortScoreModel:
            def predict(self, pairs):
                return [0.9]  # fewer scores than candidates

        adapter = BgeRerankerAdapter()
        with mock.patch.object(adapter, "is_available", return_value=True):
            with mock.patch.object(adapter, "_get_model", return_value=ShortScoreModel()):
                chunks = [Chunk(chunk_id=f"p{i}:c0", page=i, text="test", source_text="test") for i in range(1, 4)]
                hits = [RetrievalHit(chunk=c, score=float(i)) for i, c in enumerate(chunks)]
                reranked = adapter.rerank("query", hits, limit=3)
                self.assertEqual(3, len(reranked))
                self.assertEqual([h.chunk.chunk_id for h in hits], [h.chunk.chunk_id for h in reranked])

    def test_single_candidate_scalar_score_is_handled(self):
        from main import BgeRerankerAdapter, Chunk, RetrievalHit

        class ScalarScoreModel:
            def predict(self, pairs):
                return 0.42  # single-candidate pool returns a bare float

        adapter = BgeRerankerAdapter()
        with mock.patch.object(adapter, "is_available", return_value=True):
            with mock.patch.object(adapter, "_get_model", return_value=ScalarScoreModel()):
                chunks = [Chunk(chunk_id="p1:c0", page=1, text="test", source_text="test")]
                hits = [RetrievalHit(chunk=chunks[0], score=1.0)]
                reranked = adapter.rerank("query", hits, limit=1)
                self.assertEqual(1, len(reranked))
                self.assertEqual(0.42, reranked[0].score)

    def test_identity_fallback_on_non_finite_scores(self):
        from main import BgeRerankerAdapter, Chunk, RetrievalHit

        class NaNScoreModel:
            def predict(self, pairs):
                return [float("nan")] * len(pairs)

        adapter = BgeRerankerAdapter()
        with mock.patch.object(adapter, "is_available", return_value=True):
            with mock.patch.object(adapter, "_get_model", return_value=NaNScoreModel()):
                chunks = [Chunk(chunk_id=f"p{i}:c0", page=i, text="test", source_text="test") for i in range(1, 4)]
                hits = [RetrievalHit(chunk=c, score=float(i)) for i, c in enumerate(chunks)]
                reranked = adapter.rerank("query", hits, limit=3)
                self.assertEqual(3, len(reranked))
                self.assertEqual([h.chunk.chunk_id for h in hits], [h.chunk.chunk_id for h in reranked])

class DenseRetrieverAdapterTests(unittest.TestCase):
    def setUp(self):
        import sys
        main_module = sys.modules.get("main")
        if main_module is not None:
            self._orig_dense_models = getattr(main_module, "_dense_models", {}).copy()
            self._orig_corpus_cache = getattr(main_module, "_corpus_embedding_cache", {}).copy()
            main_module._dense_models.clear()
            main_module._corpus_embedding_cache.clear()
        else:
            self._orig_dense_models = {}
            self._orig_corpus_cache = {}

    def tearDown(self):
        import sys
        main_module = sys.modules.get("main")
        if main_module is not None:
            main_module._dense_models.clear()
            main_module._dense_models.update(self._orig_dense_models)
            main_module._corpus_embedding_cache.clear()
            main_module._corpus_embedding_cache.update(self._orig_corpus_cache)

    def test_bge_m3_hybrid_scoring_logic(self):
        import sys
        from unittest import mock
        from main import Chunk

        class FakeBGEM3:
            def encode(self, texts, return_dense=True, return_sparse=True):
                # Dummy implementation for tests
                res = {
                    "dense_vecs": [[1.0, 0.0] for _ in texts],
                    "lexical_weights": [{"term": 0.5} for _ in texts]
                }
                return res

            def compute_lexical_matching_score(self, qs, ds):
                if not qs or not ds: return 0.0
                return 1.0

        adapter = DenseRetrieverAdapter(backend="bge-m3")
        with mock.patch.object(adapter, "is_available", return_value=True):
            with mock.patch.object(adapter, "_bge_m3_available", return_value=True):
                with mock.patch.object(adapter, "_get_bge_m3_model", return_value=FakeBGEM3()):
                    chunks = build_page_chunks(["alpha", "beta"])
                    hits = adapter.retrieve("query", chunks, limit=2)

                    self.assertEqual(2, len(hits))
                    self.assertAlmostEqual(1.0, hits[0].score)

    def test_bge_m3_cache_is_isolated_between_documents_with_same_chunk_ids(self):
        import sys
        from unittest import mock
        from main import Chunk

        encode_calls = []

        class FakeBGEM3:
            def encode(self, texts, return_dense=True, return_sparse=True):
                encode_calls.append(list(texts))
                dense = []
                sparse = []
                for text in texts:
                    dense.append([1.0 if "alpha" in text else 0.0, 0.0])
                    sparse.append({"alpha": 0.5} if "alpha" in text else {"beta": 0.5})
                return {"dense_vecs": dense, "lexical_weights": sparse}

            def compute_lexical_matching_score(self, qs, ds):
                if not qs or not ds:
                    return 0.0
                overlap = sum(q.get(t, 0.0) * d.get(t, 0.0) for t in q)
                return overlap

        def build_doc(texts):
            return build_page_chunks(texts)

        # Two documents with identical chunk ids (p1:c0, p2:c0) and equal-length
        # text but different content must NOT share a corpus-cache entry.
        doc_a = build_doc(["alpha alpha", "alpha alpha"])
        doc_b = build_doc(["beta beta", "beta beta"])

        adapter = DenseRetrieverAdapter(backend="bge-m3")
        with mock.patch.object(adapter, "is_available", return_value=True):
            with mock.patch.object(adapter, "_bge_m3_available", return_value=True):
                with mock.patch.object(adapter, "_get_bge_m3_model", return_value=FakeBGEM3()):
                    hits_a = adapter.retrieve("alpha", doc_a, limit=2)
                    hits_b = adapter.retrieve("alpha", doc_b, limit=2)

        self.assertEqual(2, len(hits_a))
        # Document B is "beta beta" - query "alpha" should NOT match it. If the
        # corpus cache collided with Document A, B would inherit A's "alpha"
        # vectors and rank B's beta chunks high. Zero hits proves B was encoded
        # with its own content and scored independently.
        all_encoded = " ".join(text for texts in encode_calls for text in texts)
        self.assertIn("beta", all_encoded, "document B must be encoded, not served from A's cache entry")
        self.assertEqual(0, len(hits_b))

    def test_chunk_digest_is_boundary_safe_against_embedded_nul(self):
        from main import Chunk, _chunk_text_digest

        # Different chunk boundaries with identical byte stream must hash
        # differently: ["a\\x00b","c"] vs ["a","b\\x00c"].
        doc_a = [Chunk(chunk_id="p1:c0", page=1, text="a\x00b", source_text="a\x00b"),
                 Chunk(chunk_id="p2:c0", page=2, text="c", source_text="c")]
        doc_b = [Chunk(chunk_id="p1:c0", page=1, text="a", source_text="a"),
                 Chunk(chunk_id="p2:c0", page=2, text="b\x00c", source_text="b\x00c")]
        self.assertNotEqual(_chunk_text_digest(doc_a), _chunk_text_digest(doc_b))

    def test_chunk_digest_distinguishes_surrogates_from_literal(self):
        from main import Chunk, _chunk_text_digest

        # An unpaired surrogate and a literal "?" must not hash identically
        # (utf-8 errors="replace" would collide them).
        doc_a = [Chunk(chunk_id="p1:c0", page=1, text="\ud800", source_text="\ud800")]
        doc_b = [Chunk(chunk_id="p1:c0", page=1, text="?", source_text="?")]
        self.assertNotEqual(_chunk_text_digest(doc_a), _chunk_text_digest(doc_b))

    def test_auto_backend_falls_back_to_fastembed_when_bge_m3_fails(self):
        import sys
        from unittest import mock

        class BrokenBGEM3:
            def encode(self, texts, return_dense=True, return_sparse=True):
                raise RuntimeError("model failed at inference")

        class FakeEmbedding:
            def __init__(self, *args, **kwargs):
                pass

            def query_embed(self, query):
                return iter([[1.0, 0.0]])

            def embed(self, texts):
                return iter([[1.0, 0.0] for _ in texts])

        fake_fastembed = mock.MagicMock()
        fake_fastembed.TextEmbedding = FakeEmbedding

        with mock.patch.dict(sys.modules, {"fastembed": fake_fastembed}):
            adapter = DenseRetrieverAdapter(backend="auto")
            with mock.patch.object(adapter, "_fastembed_available", return_value=True):
                with mock.patch.object(adapter, "_bge_m3_available", return_value=True):
                    with mock.patch.object(adapter, "_get_bge_m3_model", return_value=BrokenBGEM3()):
                        chunks = build_page_chunks(["alpha revenue grew", "beta margin improved"])
                        hits = adapter.retrieve("revenue", chunks, limit=2)

        self.assertEqual(2, len(hits))
        self.assertEqual("fastembed", adapter.effective_backend())

    def test_effective_backend_reports_fastembed_after_bge_fallback(self):
        import sys
        from unittest import mock

        class BrokenBGEM3:
            def encode(self, texts, return_dense=True, return_sparse=True):
                raise RuntimeError("offline")

        class FakeEmbedding:
            def __init__(self, *args, **kwargs):
                pass

            def query_embed(self, query):
                return iter([[1.0, 0.0]])

            def embed(self, texts):
                return iter([[1.0, 0.0] for _ in texts])

        fake_fastembed = mock.MagicMock()
        fake_fastembed.TextEmbedding = FakeEmbedding

        with mock.patch.dict(sys.modules, {"fastembed": fake_fastembed}):
            adapter = DenseRetrieverAdapter(backend="auto")
            with mock.patch.object(adapter, "_fastembed_available", return_value=True):
                with mock.patch.object(adapter, "_bge_m3_available", return_value=True):
                    with mock.patch.object(adapter, "_get_bge_m3_model", return_value=BrokenBGEM3()):
                        chunks = build_page_chunks(["alpha revenue grew"])
                        adapter.retrieve("revenue", chunks, limit=1)

        self.assertEqual("fastembed", adapter.effective_backend())

    def test_backend_state_resets_after_failed_call_does_not_leak_prior_bge(self):
        import sys
        from unittest import mock
        from main import Chunk

        class FlakyBGEM3:
            def __init__(self):
                self.calls = 0

            def encode(self, texts, return_dense=True, return_sparse=True):
                self.calls += 1
                # First retrieve: query encode (1) + chunk encode (2) succeed.
                # Second retrieve: any encode (3+) fails.
                if self.calls <= 2:
                    return {"dense_vecs": [[1.0, 0.0] for _ in texts], "lexical_weights": [{"q": 1.0} for _ in texts]}
                raise RuntimeError("bge failed on second call")

            def compute_lexical_matching_score(self, qs, ds):
                return 1.0

        class EmptyFastEmbed:
            def __init__(self, *args, **kwargs):
                pass

            def query_embed(self, query):
                return iter([[1.0, 0.0]])

            def embed(self, texts):
                return iter([[-1.0, 0.0] for _ in texts])

        fake_fastembed = mock.MagicMock()
        fake_fastembed.TextEmbedding = EmptyFastEmbed

        with mock.patch.dict(sys.modules, {"fastembed": fake_fastembed}):
            adapter = DenseRetrieverAdapter(backend="auto")
            with mock.patch.object(adapter, "_fastembed_available", return_value=True):
                with mock.patch.object(adapter, "_bge_m3_available", return_value=True):
                    model = FlakyBGEM3()
                    with mock.patch.object(adapter, "_get_bge_m3_model", return_value=model):
                        chunks = build_page_chunks(["alpha revenue grew"])
                        first = adapter.retrieve("revenue", chunks, limit=1)
                        self.assertEqual("bge-m3", adapter.effective_backend())
                        second = adapter.retrieve("revenue", chunks, limit=1)
                        # Second call: BGE fails, FastEmbed executes but returns
                        # no hits. The prior bge-m3 state must not leak: the
                        # reported backend is the FastEmbed fallback that ran,
                        # with no_hits status (not the stale bge-m3).
                        self.assertEqual("fastembed", adapter.effective_backend())
                        self.assertEqual("no_hits", adapter._last_status)
                        self.assertEqual([], second)

    def test_malformed_bge_sparse_output_degrades_to_fastembed(self):
        import sys
        from unittest import mock

        class BadSparseBGEM3:
            def encode(self, texts, return_dense=True, return_sparse=True):
                if len(texts) == 1:
                    return {"dense_vecs": [[1.0, 0.0]], "lexical_weights": [{"q": 1.0}]}
                return {
                    "dense_vecs": [[1.0, 0.0] for _ in texts],
                    "lexical_weights": [[object()] for _ in texts],
                }

            def compute_lexical_matching_score(self, qs, ds):
                raise RuntimeError("force sparse fallback")

        class FakeEmbedding:
            def __init__(self, *args, **kwargs):
                pass

            def query_embed(self, query):
                return iter([[1.0, 0.0]])

            def embed(self, texts):
                return iter([[1.0, 0.0] for _ in texts])

        fake_fastembed = mock.MagicMock()
        fake_fastembed.TextEmbedding = FakeEmbedding

        with mock.patch.dict(sys.modules, {"fastembed": fake_fastembed}):
            adapter = DenseRetrieverAdapter(backend="auto")
            with mock.patch.object(adapter, "_fastembed_available", return_value=True):
                with mock.patch.object(adapter, "_bge_m3_available", return_value=True):
                    with mock.patch.object(adapter, "_get_bge_m3_model", return_value=BadSparseBGEM3()):
                        chunks = build_page_chunks(["alpha", "beta"])
                        hits = adapter.retrieve("q", chunks, limit=2)

        self.assertEqual(2, len(hits))
        self.assertEqual("fastembed", adapter.effective_backend())

    def test_wrong_dense_dimension_degrades_to_fastembed(self):
        import sys
        from unittest import mock

        class BadDimsBGEM3:
            def encode(self, texts, return_dense=True, return_sparse=True):
                if len(texts) == 1:
                    return {"dense_vecs": [[1.0, 0.0]], "lexical_weights": [{}]}
                return {"dense_vecs": [[1.0] for _ in texts], "lexical_weights": [{} for _ in texts]}

            def compute_lexical_matching_score(self, qs, ds):
                return 0.0

        class FakeEmbedding:
            def __init__(self, *args, **kwargs):
                pass

            def query_embed(self, query):
                return iter([[1.0, 0.0]])

            def embed(self, texts):
                return iter([[1.0, 0.0] for _ in texts])

        fake_fastembed = mock.MagicMock()
        fake_fastembed.TextEmbedding = FakeEmbedding

        with mock.patch.dict(sys.modules, {"fastembed": fake_fastembed}):
            adapter = DenseRetrieverAdapter(backend="auto")
            with mock.patch.object(adapter, "_fastembed_available", return_value=True):
                with mock.patch.object(adapter, "_bge_m3_available", return_value=True):
                    with mock.patch.object(adapter, "_get_bge_m3_model", return_value=BadDimsBGEM3()):
                        chunks = build_page_chunks(["alpha", "beta"])
                        hits = adapter.retrieve("q", chunks, limit=2)

        self.assertEqual(2, len(hits))
        self.assertEqual("fastembed", adapter.effective_backend())

    def test_non_numeric_dense_value_degrades_to_fastembed(self):
        import sys
        from unittest import mock

        class BadValueBGEM3:
            def encode(self, texts, return_dense=True, return_sparse=True):
                if len(texts) == 1:
                    return {"dense_vecs": [[1.0, 0.0]], "lexical_weights": [{}]}
                return {"dense_vecs": [["bad", 0.0] for _ in texts], "lexical_weights": [{} for _ in texts]}

            def compute_lexical_matching_score(self, qs, ds):
                return 0.0

        class FakeEmbedding:
            def __init__(self, *args, **kwargs):
                pass

            def query_embed(self, query):
                return iter([[1.0, 0.0]])

            def embed(self, texts):
                return iter([[1.0, 0.0] for _ in texts])

        fake_fastembed = mock.MagicMock()
        fake_fastembed.TextEmbedding = FakeEmbedding

        with mock.patch.dict(sys.modules, {"fastembed": fake_fastembed}):
            adapter = DenseRetrieverAdapter(backend="auto")
            with mock.patch.object(adapter, "_fastembed_available", return_value=True):
                with mock.patch.object(adapter, "_bge_m3_available", return_value=True):
                    with mock.patch.object(adapter, "_get_bge_m3_model", return_value=BadValueBGEM3()):
                        chunks = build_page_chunks(["alpha", "beta"])
                        hits = adapter.retrieve("q", chunks, limit=2)

        self.assertEqual(2, len(hits))
        self.assertEqual("fastembed", adapter.effective_backend())

    def test_non_numeric_sparse_weight_degrades_to_fastembed(self):
        import sys
        from unittest import mock

        class BadSparseValueBGEM3:
            def encode(self, texts, return_dense=True, return_sparse=True):
                if len(texts) == 1:
                    return {"dense_vecs": [[1.0, 0.0]], "lexical_weights": [{"q": 1.0}]}
                return {"dense_vecs": [[1.0, 0.0] for _ in texts], "lexical_weights": [{"q": "bad-weight"} for _ in texts]}

            def compute_lexical_matching_score(self, qs, ds):
                return 1.0

        class FakeEmbedding:
            def __init__(self, *args, **kwargs):
                pass

            def query_embed(self, query):
                return iter([[1.0, 0.0]])

            def embed(self, texts):
                return iter([[1.0, 0.0] for _ in texts])

        fake_fastembed = mock.MagicMock()
        fake_fastembed.TextEmbedding = FakeEmbedding

        with mock.patch.dict(sys.modules, {"fastembed": fake_fastembed}):
            adapter = DenseRetrieverAdapter(backend="auto")
            with mock.patch.object(adapter, "_fastembed_available", return_value=True):
                with mock.patch.object(adapter, "_bge_m3_available", return_value=True):
                    with mock.patch.object(adapter, "_get_bge_m3_model", return_value=BadSparseValueBGEM3()):
                        chunks = build_page_chunks(["alpha", "beta"])
                        hits = adapter.retrieve("q", chunks, limit=2)

        self.assertEqual(2, len(hits))
        self.assertEqual("fastembed", adapter.effective_backend())


    def test_malformed_fastembed_output_degrades_to_empty(self):
        import sys
        from unittest import mock
        from main import _corpus_embedding_cache

        class BadQueryFastEmbed:
            def __init__(self, *args, **kwargs): pass
            def query_embed(self, query): return iter([["bad", 0.0]])
            def embed(self, texts): return iter([[1.0, 0.0] for _ in texts])

        class BadDocFastEmbed:
            def __init__(self, *args, **kwargs): pass
            def query_embed(self, query): return iter([[1.0, 0.0]])
            def embed(self, texts): return iter([["bad", 0.0] for _ in texts])

        class ShortDocFastEmbed:
            def __init__(self, *args, **kwargs): pass
            def query_embed(self, query): return iter([[1.0, 0.0]])
            def embed(self, texts): return iter([])

        class NaNFastEmbed:
            def __init__(self, *args, **kwargs): pass
            def query_embed(self, query): return iter([[float("nan"), 0.0]])
            def embed(self, texts): return iter([[1.0, 0.0] for _ in texts])

        fake_fastembed = mock.MagicMock()

        chunks = build_page_chunks(["alpha"])

        with mock.patch.dict(sys.modules, {"fastembed": fake_fastembed}):
            adapter = DenseRetrieverAdapter(backend="fastembed")
            with mock.patch.object(adapter, "_fastembed_available", return_value=True):
                for bad_model in [BadQueryFastEmbed, BadDocFastEmbed, ShortDocFastEmbed, NaNFastEmbed]:
                    fake_fastembed.TextEmbedding = bad_model
                    with mock.patch.object(adapter, "_get_model", return_value=bad_model()):
                        hits = adapter.retrieve("q", chunks, limit=1)
                        self.assertEqual([], hits)
                        self.assertEqual("failed", adapter._last_status)
                        # Cache must not be poisoned
                        key = (adapter.model_name, __import__("main")._chunk_text_digest(chunks))
                        self.assertNotIn(key, _corpus_embedding_cache)

    def test_bge_cache_not_poisoned_by_malformed_output(self):
        import sys
        from unittest import mock
        from main import _corpus_embedding_cache

        class FlakyBGEM3:
            def __init__(self):
                self.calls = 0

            def encode(self, texts, return_dense=True, return_sparse=True):
                self.calls += 1
                if self.calls == 1:
                    return {"dense_vecs": [[1.0, 0.0]], "lexical_weights": [{}]}  # query
                return {"dense_vecs": [[1.0]], "lexical_weights": [{}]}  # bad chunk dims

            def compute_lexical_matching_score(self, qs, ds):
                return 0.0

        class HealthyBGEM3:
            def encode(self, texts, return_dense=True, return_sparse=True):
                return {"dense_vecs": [[1.0, 0.0] for _ in texts], "lexical_weights": [{} for _ in texts]}

            def compute_lexical_matching_score(self, qs, ds):
                return 0.0

        class FakeEmbedding:
            def __init__(self, *args, **kwargs):
                pass

            def query_embed(self, query):
                return iter([[1.0, 0.0]])

            def embed(self, texts):
                return iter([[1.0, 0.0] for _ in texts])

        fake_fastembed = mock.MagicMock()
        fake_fastembed.TextEmbedding = FakeEmbedding

        chunks = build_page_chunks(["alpha"])

        with mock.patch.dict(sys.modules, {"fastembed": fake_fastembed}):
            adapter = DenseRetrieverAdapter(backend="bge-m3")
            with mock.patch.object(adapter, "_bge_m3_available", return_value=True):
                with mock.patch.object(adapter, "_get_bge_m3_model", return_value=FlakyBGEM3()):
                    bad_hits = adapter.retrieve("q", chunks, limit=1)
                self.assertEqual([], bad_hits)
                # The malformed payload must NOT remain cached.
                key = ("bge-m3", adapter.bge_m3_model, __import__("main")._chunk_text_digest(chunks))
                self.assertNotIn(key, _corpus_embedding_cache)
                with mock.patch.object(adapter, "_get_bge_m3_model", return_value=HealthyBGEM3()):
                    good_hits = adapter.retrieve("q", chunks, limit=1)
                self.assertEqual(1, len(good_hits))

    def test_fastembed_cache_isolates_same_id_equal_length_different_content(self):
        import sys
        from unittest import mock

        encoded = []

        class FakeEmbedding:
            def __init__(self, *args, **kwargs):
                pass

            def query_embed(self, query):
                return iter([[1.0, 0.0]])

            def embed(self, texts):
                encoded.append(list(texts))
                return iter([[1.0 if "alpha" in text else 0.0, 0.0] for text in texts])

        fake_fastembed = mock.MagicMock()
        fake_fastembed.TextEmbedding = FakeEmbedding

        with mock.patch.dict(sys.modules, {"fastembed": fake_fastembed}):
            adapter = DenseRetrieverAdapter(backend="fastembed")
            doc_a = build_page_chunks(["alpha alpha", "alpha alpha"])
            doc_b = build_page_chunks(["beta beta", "beta beta"])
            adapter.retrieve("alpha", doc_a, limit=2)
            adapter.retrieve("alpha", doc_b, limit=2)

        all_encoded = " ".join(text for texts in encoded for text in texts)
        self.assertIn("beta", all_encoded, "document B must be encoded, not served from A's cache entry")

    def test_dense_retriever_returns_ranked_hits_with_fastembed(self):
        import sys
        from unittest import mock

        vectors = {
            "alpha revenue grew": [1.0, 0.0],
            "beta margin improved": [0.8, 0.6],
        }

        class FakeEmbedding:
            def __init__(self, *args, **kwargs):
                pass

            def query_embed(self, query):
                return iter([[1.0, 0.0]])

            def embed(self, texts):
                return iter([vectors[text] for text in texts])

        fake_module = mock.MagicMock()
        fake_module.TextEmbedding = FakeEmbedding

        with mock.patch.dict(sys.modules, {"fastembed": fake_module}):
            chunks = build_page_chunks(["alpha revenue grew", "beta margin improved"])
            adapter = DenseRetrieverAdapter(backend="fastembed")

            hits = adapter.retrieve("revenue", chunks, limit=2)

        self.assertEqual(2, len(hits))
        self.assertEqual("p1:c0", hits[0].chunk.chunk_id)
        self.assertGreater(hits[0].score, hits[1].score)

    def test_dense_retriever_gracefully_skips_when_fastembed_absent(self):
        from unittest import mock

        adapter = DenseRetrieverAdapter()
        with mock.patch("main.DenseRetrieverAdapter.is_available", return_value=False):
            chunks = build_page_chunks(["some text"])
            hits = adapter.retrieve("query", chunks)

        self.assertEqual([], hits)

    def test_dense_retriever_reuses_singleton_model(self):
        import sys
        from unittest import mock

        created = []

        class FakeEmbedding:
            def __init__(self, *args, **kwargs):
                created.append(1)

            def query_embed(self, query):
                return iter([[1.0, 0.0]])

            def embed(self, texts):
                return iter([[1.0, 0.0] for _ in texts])

        fake_module = mock.MagicMock()
        fake_module.TextEmbedding = FakeEmbedding

        with mock.patch.dict(sys.modules, {"fastembed": fake_module}):
            chunks = build_page_chunks(["alpha revenue grew", "beta margin improved"])
            adapter = DenseRetrieverAdapter(backend="fastembed")
            adapter.retrieve("revenue", chunks, limit=2)
            adapter.retrieve("revenue", chunks, limit=2)

        self.assertEqual(1, len(created), "model should be initialized exactly once")

    def test_dense_only_mode_uses_dense_retriever(self):
        import sys
        from unittest import mock

        vectors = {
            "Research investment was 18 million dollars across multiple lines of disclosure.": [1.0, 0.0],
            "Revenue was 125 million dollars and the company grew strongly last year.": [0.0, 1.0],
        }

        class FakeEmbedding:
            def __init__(self, *args, **kwargs):
                pass

            def query_embed(self, query):
                return iter([[1.0, 0.0]])

            def embed(self, texts):
                return iter([vectors[text] for text in texts])

        fake_module = mock.MagicMock()
        fake_module.TextEmbedding = FakeEmbedding

        pages = [
            "Revenue was 125 million dollars and the company grew strongly last year.",
            "Research investment was 18 million dollars across multiple lines of disclosure.",
        ]
        with mock.patch.dict(sys.modules, {"fastembed": fake_module, "FlagEmbedding": None}):
            context, fallback = select_retrieved_context(["What was research investment?"], pages, mode="dense")
        self.assertFalse(fallback)
        self.assertIn("[Page 2 | p2:c0]", context)

    def test_dense_only_mode_falls_back_when_fastembed_absent(self):
        import sys
        from unittest import mock

        pages = [
            "Revenue was 125 million dollars and the company grew strongly last year.",
            "Research investment was 18 million dollars across multiple lines of disclosure.",
        ]
        with mock.patch("main.DenseRetrieverAdapter.is_available", return_value=False):
            context, fallback = select_retrieved_context(["What was research investment?"], pages, mode="dense")
        self.assertFalse(fallback)
        self.assertIn("[Page 2 | p2:c0]", context)

    def test_dense_mode_falls_back_to_bm25_on_model_init_or_inference_failure(self):
        import sys
        from unittest import mock

        class BrokenEmbedding:
            def __init__(self, *args, **kwargs):
                raise RuntimeError("offline and model not cached")

        fake_module = mock.MagicMock()
        fake_module.TextEmbedding = BrokenEmbedding

        with mock.patch.dict(sys.modules, {"fastembed": fake_module, "FlagEmbedding": None}):
            pages = [
                "Revenue was 125 million dollars and the company grew strongly last year.",
                "Research investment was 18 million dollars across multiple lines of disclosure.",
            ]
            # In dense mode with model init failure, it must fall back to BM25,
            # retrieving the exact matching page 2 rather than the whole document.
            context, fallback = select_retrieved_context(["What was research investment?"], pages, mode="dense")
            self.assertFalse(fallback, "should use BM25 hit instead of full-doc fallback")
            self.assertIn("[Page 2 | p2:c0]", context)

    def test_dense_retriever_recovers_semantic_synonym_without_exact_terms(self):
        import sys
        from unittest import mock

        vectors = {
            "The enterprise software firm reported record subscription revenue this quarter.": [1.0, 0.0, 0.0],
            "Database market opportunity exceeds sixty billion dollars globally.": [0.0, 1.0, 0.0],
            "Foreign exchange headwinds trimmed reported growth this year.": [0.0, 0.0, 1.0],
        }

        class FakeEmbedding:
            def __init__(self, *args, **kwargs):
                pass

            def query_embed(self, query):
                # Semantic query about software subscription income.
                return iter([[1.0, 0.2, 0.1]])

            def embed(self, texts):
                return iter([vectors[text] for text in texts])

        fake_module = mock.MagicMock()
        fake_module.TextEmbedding = FakeEmbedding

        with mock.patch.dict(sys.modules, {"fastembed": fake_module}):
            chunks = build_page_chunks(list(vectors.keys()))
            adapter = DenseRetrieverAdapter(backend="fastembed")
            hits = adapter.retrieve("income from software subscriptions", chunks, limit=3)

        self.assertTrue(hits)
        self.assertEqual("p1:c0", hits[0].chunk.chunk_id)

    def test_dense_retriever_isolates_different_model_names(self):
        import sys
        from unittest import mock

        created_models = []

        class FakeEmbedding:
            def __init__(self, model_name=None, *args, **kwargs):
                self.model_name = model_name
                created_models.append(model_name)

            def query_embed(self, query):
                return iter([[1.0, 0.0]])

            def embed(self, texts):
                return iter([[1.0, 0.0] for _ in texts])

        fake_module = mock.MagicMock()
        fake_module.TextEmbedding = FakeEmbedding

        with mock.patch.dict(sys.modules, {"fastembed": fake_module}):
            chunks = build_page_chunks(["alpha revenue grew"])
            a = DenseRetrieverAdapter(model_name="model-A", backend="fastembed")
            b = DenseRetrieverAdapter(model_name="model-B", backend="fastembed")
            a.retrieve("q", chunks)
            b.retrieve("q", chunks)

        self.assertEqual(["model-A", "model-B"], created_models)

    def test_dense_retriever_caches_corpus_embeddings_across_questions(self):
        import sys
        from unittest import mock

        embed_calls = []

        class FakeEmbedding:
            def __init__(self, *args, **kwargs):
                pass

            def query_embed(self, query):
                return iter([[1.0, 0.0]])

            def embed(self, texts):
                embed_calls.append(list(texts))
                return iter([[1.0, 0.0] for _ in texts])

        fake_module = mock.MagicMock()
        fake_module.TextEmbedding = FakeEmbedding

        with mock.patch.dict(sys.modules, {"fastembed": fake_module}):
            chunks = build_page_chunks(["page one text", "page two text"])
            adapter = DenseRetrieverAdapter(backend="fastembed")
            # 20 questions against the same document chunks.
            for q_idx in range(20):
                adapter.retrieve(f"question {q_idx}", chunks)

        self.assertEqual(1, len(embed_calls), "corpus embed() should be invoked exactly once for 20 questions")

    def test_retrieval_mode_normalization_and_whitelist(self):
        import sys
        from unittest import mock

        class FakeEmbedding:
            def __init__(self, *args, **kwargs):
                pass

            def query_embed(self, query):
                return iter([[1.0, 0.0]])

            def embed(self, texts):
                return iter([[1.0, 0.0] for _ in texts])

        fake_module = mock.MagicMock()
        fake_module.TextEmbedding = FakeEmbedding

        pages = ["Revenue was 125 million dollars.", "Research investment was 18 million dollars."]
        # Upper case "DENSE" normalized
        with mock.patch.dict(sys.modules, {"fastembed": fake_module, "FlagEmbedding": None}):
            ctx, fall = select_retrieved_context(["What was research investment?"], pages, mode="DENSE")
        self.assertFalse(fall)
        # Typo mode "denes" safely falls back to BM25
        ctx_typo, fall_typo = select_retrieved_context(["What was research investment?"], pages, mode="denes")
        self.assertFalse(fall_typo)
        self.assertIn("[Page 2 | p2:c0]", ctx_typo)

    def test_dense_retriever_thread_safe_initialization(self):
        import sys
        import threading
        from unittest import mock

        created = []

        class SlowInitEmbedding:
            def __init__(self, *args, **kwargs):
                import time
                time.sleep(0.01)
                created.append(1)

            def query_embed(self, query):
                return iter([[1.0, 0.0]])

            def embed(self, texts):
                return iter([[1.0, 0.0] for _ in texts])

        fake_module = mock.MagicMock()
        fake_module.TextEmbedding = SlowInitEmbedding

        with mock.patch.dict(sys.modules, {"fastembed": fake_module}):
            chunks = build_page_chunks(["thread safe test text"])
            threads = []
            for _ in range(8):
                t = threading.Thread(target=lambda: DenseRetrieverAdapter(backend="fastembed").retrieve("q", chunks))
                threads.append(t)
                t.start()
            for t in threads:
                t.join()

        self.assertEqual(1, len(created), "concurrent threads should initialize model exactly once")

    def test_cosine_similarity_ranks_and_matches(self):
        from main import _cosine_similarity

        self.assertEqual(1.0, _cosine_similarity([1.0, 0.0], [1.0, 0.0]))
        self.assertAlmostEqual(0.0, _cosine_similarity([1.0, 0.0], [0.0, 1.0]))
        self.assertEqual(0.0, _cosine_similarity([], [1.0]))
        self.assertEqual(0.0, _cosine_similarity([0.0, 0.0], [1.0, 0.0]))


def make_lattice_table_pdf(path: Path) -> None:
    """Write a minimal fixed-layout PDF containing a known 3x3 grid table."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    lines = ["0.5 w 0 0 0 RG"]
    for x in (72, 144, 216, 288):
        lines.append(f"{x} 600 m {x} 720 l S")
    for y in (600, 630, 660, 690, 720):
        lines.append(f"72 {y} m 288 {y} l S")
    text = [
        "BT /F1 10 Tf 80 708 Td (Product) Tj ET",
        "BT /F1 10 Tf 152 708 Td (Q1) Tj ET",
        "BT /F1 10 Tf 224 708 Td (Q2) Tj ET",
        "BT /F1 10 Tf 80 678 Td (Widget) Tj ET",
        "BT /F1 10 Tf 152 678 Td (100) Tj ET",
        "BT /F1 10 Tf 224 678 Td (200) Tj ET",
        "BT /F1 10 Tf 80 648 Td (Gadget) Tj ET",
        "BT /F1 10 Tf 152 648 Td (50) Tj ET",
        "BT /F1 10 Tf 224 648 Td (75) Tj ET",
    ]
    stream = ("\n".join(lines + text)).encode("ascii")
    objects.append(b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream")

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
    pdf.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("ascii"))
    path.write_bytes(bytes(pdf))


class CamelotTableTests(unittest.TestCase):
    def _table_pdf(self) -> Path:
        path = Path(tempfile.mktemp(suffix=".pdf"))
        make_lattice_table_pdf(path)
        self.addCleanup(self._remove_tolerant, path)
        return path

    @staticmethod
    def _remove_tolerant(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except PermissionError:
            pass

    def test_extracts_known_table_rows(self):
        path = self._table_pdf()

        rows = extract_tables_from_pdf(str(path), flavor="lattice")

        self.assertEqual(2, len(rows))
        self.assertEqual(["Product", "Q1", "Q2"], rows[0]["headers"])
        self.assertEqual({"Product": "Widget", "Q1": "100", "Q2": "200"}, rows[0]["row"])

    def test_adapter_returns_plain_dict_rows(self):
        from main import TableExtractorAdapter

        path = self._table_pdf()

        adapter = TableExtractorAdapter()
        rows = adapter.extract(str(path), flavor="lattice")

        self.assertEqual(2, len(rows))
        self.assertIsInstance(rows[0]["row"], dict)
        self.assertEqual("75", rows[1]["row"]["Q2"])

    def test_table_rows_render_to_plain_text_with_headers(self):
        from main import table_rows_to_text

        rows = [
            {"headers": ["Product", "Q1", "Q2"], "cells": ["Widget", "100", "200"]},
            {"headers": ["Product", "Q1", "Q2"], "cells": ["Gadget", "50", "75"]},
        ]

        text = table_rows_to_text(rows)

        self.assertIn("Product: Widget | Q1: 100 | Q2: 200", text)
        self.assertIn("Product: Gadget | Q1: 50 | Q2: 75", text)

    def test_table_rows_render_without_vendor_objects(self):
        from main import table_rows_to_text

        rows = [
            {"table_index": 0, "headers": ["Metric", "Value"], "cells": ["Revenue", "125m"]},
        ]

        text = table_rows_to_text(rows)

        self.assertIn("Metric: Revenue | Value: 125m", text)
        self.assertNotIn("table_index", text)

    def test_enrich_pages_appends_table_text_to_page(self):
        from main import enrich_pages_with_tables

        rows = [
            {"table_index": 0, "page": 1, "headers": ["Product", "Q1"], "cells": ["Widget", "100"]},
        ]

        enriched = enrich_pages_with_tables(["Page one text.", "Page two text."], rows)

        self.assertIn("Product: Widget | Q1: 100", enriched[0])
        self.assertEqual("Page two text.", enriched[1])

    def test_table_text_verifies_citations_against_enriched_page(self):
        from main import enrich_pages_with_tables, verify_matrix_citations

        pages = enrich_pages_with_tables(
            ["Revenue narrative."],
            [{"page": 1, "headers": ["Metric", "Value"], "cells": ["Revenue", "125m"]}],
        )
        verified = verify_matrix_citations(
            [
                {
                    "question": "What is revenue?",
                    "answer": "125m.",
                    "quote": "Metric: Revenue | Value: 125m",
                }
            ],
            pages,
        )

        self.assertEqual("verified_exact", verified[0]["citation_status"])
        self.assertEqual(1, verified[0]["page"])


class HealthStatusTests(unittest.TestCase):
    def test_health_status_reports_ok(self):
        from main import health_status

        status = health_status()

        self.assertEqual("ok", status["status"])
        self.assertEqual("openmatrix", status["service"])


class MatrixRunSummaryTests(unittest.TestCase):
    def test_summary_counts_terminal_cell_states(self):
        from main import matrix_run_summary, persist_matrix_cell

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "matrix.sqlite3"
            run_id = "run-summary-test"
            for index, status in enumerate(["completed", "completed", "failed", "needs_review"]):
                persist_matrix_cell(
                    run_id,
                    {
                        "cell_id": f"d0q{index}",
                        "document_index": 0,
                        "document_name": "doc.pdf",
                        "question_index": index,
                        "question": f"Q{index}",
                        "status": status,
                        "answer": "a",
                        "quote": "q",
                        "citation_status": None,
                        "page": None,
                        "error": None,
                    },
                    db_path=db_path,
                )

            summary = matrix_run_summary(run_id, db_path=db_path)

            self.assertEqual(4, summary["total"])
            self.assertEqual(2, summary["completed"])
            self.assertEqual(1, summary["failed"])
            self.assertEqual(1, summary["needs_review"])

    def test_summary_returns_none_for_missing_run(self):
        from main import matrix_run_summary

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "matrix.sqlite3"
            self.assertIsNone(matrix_run_summary("missing-run", db_path=db_path))


class ListRecentRunsTests(unittest.TestCase):
    def test_lists_most_recent_runs_first(self):
        from main import list_recent_runs, persist_matrix_cell

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "matrix.sqlite3"
            for run_id in ("run-old", "run-new"):
                persist_matrix_cell(
                    run_id,
                    {
                        "cell_id": "d0q0",
                        "document_index": 0,
                        "document_name": "doc.pdf",
                        "question_index": 0,
                        "question": "Q",
                        "status": "completed",
                        "answer": "a",
                        "quote": "q",
                        "citation_status": None,
                        "page": None,
                        "error": None,
                    },
                    db_path=db_path,
                )

            runs = list_recent_runs(limit=10, db_path=db_path)

            self.assertEqual(["run-new", "run-old"], runs)

    def test_limit_is_applied(self):
        from main import list_recent_runs, persist_matrix_cell

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "matrix.sqlite3"
            for run_id in ("run-1", "run-2", "run-3"):
                persist_matrix_cell(
                    run_id,
                    {
                        "cell_id": "d0q0",
                        "document_index": 0,
                        "document_name": "doc.pdf",
                        "question_index": 0,
                        "question": "Q",
                        "status": "completed",
                        "answer": "a",
                        "quote": "q",
                        "citation_status": None,
                        "page": None,
                        "error": None,
                    },
                    db_path=db_path,
                )

            runs = list_recent_runs(limit=2, db_path=db_path)

            self.assertEqual(2, len(runs))
            self.assertNotIn("run-1", runs)


class RunCellErrorsTests(unittest.TestCase):
    def test_returns_failed_cell_question_and_error(self):
        from main import persist_matrix_cell, run_cell_errors

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "matrix.sqlite3"
            run_id = "run-errors"
            for index, (status, error) in enumerate(
                [("completed", None), ("failed", "cell provider failed"), ("failed", "timeout")]
            ):
                persist_matrix_cell(
                    run_id,
                    {
                        "cell_id": f"d0q{index}",
                        "document_index": 0,
                        "document_name": "doc.pdf",
                        "question_index": index,
                        "question": f"Q{index}",
                        "status": status,
                        "answer": "a",
                        "quote": "q",
                        "citation_status": None,
                        "page": None,
                        "error": error,
                    },
                    db_path=db_path,
                )

            errors = run_cell_errors(run_id, db_path=db_path)

            self.assertEqual(2, len(errors))
            self.assertEqual("Q1", errors[0]["question"])
            self.assertEqual("cell provider failed", errors[0]["error"])

    def test_returns_empty_for_missing_run(self):
        from main import run_cell_errors

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "matrix.sqlite3"
            self.assertEqual([], run_cell_errors("missing-run", db_path=db_path))


class RunStatusCountsTests(unittest.TestCase):
    def test_counts_cells_grouped_by_status(self):
        from main import persist_matrix_cell, run_status_counts

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "matrix.sqlite3"
            statuses = ["completed", "completed", "failed"]
            for index, status in enumerate(statuses):
                persist_matrix_cell(
                    "run-counts",
                    {
                        "cell_id": f"d0q{index}",
                        "document_index": 0,
                        "document_name": "doc.pdf",
                        "question_index": index,
                        "question": f"Q{index}",
                        "status": status,
                        "answer": "a",
                        "quote": "q",
                        "citation_status": None,
                        "page": None,
                        "error": None,
                    },
                    db_path=db_path,
                )

            counts = run_status_counts(db_path=db_path)

            self.assertEqual(2, counts["completed"])
            self.assertEqual(1, counts["failed"])

    def test_returns_empty_counts_for_empty_store(self):
        from main import run_status_counts

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "matrix.sqlite3"
            self.assertEqual({}, run_status_counts(db_path=db_path))


class CitationStatusCountsTests(unittest.TestCase):
    def test_counts_cells_grouped_by_citation_status(self):
        from main import citation_status_counts, persist_matrix_cell

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "matrix.sqlite3"
            statuses = ["verified_exact", "verified_exact", "not_found", None]
            for index, citation_status in enumerate(statuses):
                persist_matrix_cell(
                    "run-citations",
                    {
                        "cell_id": f"d0q{index}",
                        "document_index": 0,
                        "document_name": "doc.pdf",
                        "question_index": index,
                        "question": f"Q{index}",
                        "status": "completed",
                        "answer": "a",
                        "quote": "q",
                        "citation_status": citation_status,
                        "page": None,
                        "error": None,
                    },
                    db_path=db_path,
                )

            counts = citation_status_counts(db_path=db_path)

            self.assertEqual(2, counts["verified_exact"])
            self.assertEqual(1, counts["not_found"])
            self.assertEqual(1, counts["unknown"])


class AverageAnswerLengthTests(unittest.TestCase):
    def test_averages_answer_length_by_status(self):
        from main import average_answer_length_by_status, persist_matrix_cell

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "matrix.sqlite3"
            for index, (status, answer) in enumerate(
                [("completed", "ab"), ("completed", "abcd"), ("failed", "x")]
            ):
                persist_matrix_cell(
                    "run-avg",
                    {
                        "cell_id": f"d0q{index}",
                        "document_index": 0,
                        "document_name": "doc.pdf",
                        "question_index": index,
                        "question": f"Q{index}",
                        "status": status,
                        "answer": answer,
                        "quote": "q",
                        "citation_status": None,
                        "page": None,
                        "error": None,
                    },
                    db_path=db_path,
                )

            averages = average_answer_length_by_status(db_path=db_path)

            self.assertEqual(3.0, averages["completed"])
            self.assertEqual(1.0, averages["failed"])

    def test_returns_empty_for_empty_store(self):
        from main import average_answer_length_by_status

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "matrix.sqlite3"
            self.assertEqual({}, average_answer_length_by_status(db_path=db_path))


class TotalCellCountTests(unittest.TestCase):
    def test_counts_all_cells(self):
        from main import persist_matrix_cell, total_cell_count

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "matrix.sqlite3"
            for index in range(4):
                persist_matrix_cell(
                    "run-total",
                    {
                        "cell_id": f"d0q{index}",
                        "document_index": 0,
                        "document_name": "doc.pdf",
                        "question_index": index,
                        "question": f"Q{index}",
                        "status": "completed",
                        "answer": "a",
                        "quote": "q",
                        "citation_status": None,
                        "page": None,
                        "error": None,
                    },
                    db_path=db_path,
                )

            self.assertEqual(4, total_cell_count(db_path=db_path))

    def test_returns_zero_for_empty_store(self):
        from main import total_cell_count

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "matrix.sqlite3"
            self.assertEqual(0, total_cell_count(db_path=db_path))


class MostFailedQuestionsTests(unittest.TestCase):
    def test_ranks_questions_by_failure_count(self):
        from main import most_failed_questions, persist_matrix_cell

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "matrix.sqlite3"
            for index, (question, status) in enumerate(
                [
                    ("hard-q", "failed"),
                    ("hard-q", "failed"),
                    ("hard-q", "completed"),
                    ("easy-q", "failed"),
                ]
            ):
                persist_matrix_cell(
                    "run-failures",
                    {
                        "cell_id": f"d0q{index}",
                        "document_index": 0,
                        "document_name": "doc.pdf",
                        "question_index": index,
                        "question": question,
                        "status": status,
                        "answer": "a",
                        "quote": "q",
                        "citation_status": None,
                        "page": None,
                        "error": None,
                    },
                    db_path=db_path,
                )

            failures = most_failed_questions(db_path=db_path)

            self.assertEqual(2, len(failures))
            self.assertEqual("hard-q", failures[0]["question"])
            self.assertEqual(2, failures[0]["fail_count"])

    def test_respects_limit_and_empty_store(self):
        from main import most_failed_questions, persist_matrix_cell

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "matrix.sqlite3"
            for index in range(3):
                persist_matrix_cell(
                    "run-limits",
                    {
                        "cell_id": f"d0q{index}",
                        "document_index": 0,
                        "document_name": "doc.pdf",
                        "question_index": index,
                        "question": f"failed-q{index}",
                        "status": "failed",
                        "answer": "a",
                        "quote": "q",
                        "citation_status": None,
                        "page": None,
                        "error": None,
                    },
                    db_path=db_path,
                )

            limited = most_failed_questions(db_path=db_path, limit=2)
            self.assertEqual(2, len(limited))

            empty_path = Path(tmpdir) / "empty.sqlite3"
            self.assertEqual([], most_failed_questions(db_path=empty_path))


class CitationVerificationRateTests(unittest.TestCase):
    def test_verifies_rate_over_completed_cells(self):
        from main import citation_verification_rate, persist_matrix_cell

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "matrix.sqlite3"
            statuses = [
                ("completed", "verified_exact"),
                ("completed", "verified_exact"),
                ("completed", "not_found"),
                ("failed", "verified_exact"),
            ]
            for index, (status, citation_status) in enumerate(statuses):
                persist_matrix_cell(
                    "run-rate",
                    {
                        "cell_id": f"d0q{index}",
                        "document_index": 0,
                        "document_name": "doc.pdf",
                        "question_index": index,
                        "question": f"Q{index}",
                        "status": status,
                        "answer": "a",
                        "quote": "q",
                        "citation_status": citation_status,
                        "page": None,
                        "error": None,
                    },
                    db_path=db_path,
                )

            rate = citation_verification_rate(db_path=db_path)

            self.assertAlmostEqual(2 / 3, rate)

    def test_returns_zero_for_empty_store(self):
        from main import citation_verification_rate

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "matrix.sqlite3"
            self.assertEqual(0.0, citation_verification_rate(db_path=db_path))


class FakePage:
    def __init__(self, words):
        self._words = words

    def extract_words(self):
        return self._words


class FakePlumberModule:
    def __init__(self, pages):
        self._pages = pages

    def open(self, pdf_path):
        return _FakePdfContext(self._pages)


class _FakePdfContext:
    def __init__(self, pages):
        self.pages = pages

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class PdfPlumberCoordinateAdapterTests(unittest.TestCase):
    def test_extracts_normalized_coordinates_with_fake_pdfplumber(self):
        import sys

        from unittest import mock

        from main import PdfPlumberCoordinateAdapter

        fake_page = FakePage(
            [
                {"x0": 72, "top": 720, "x1": 100, "bottom": 732, "text": "hello"},
                {"x0": 104, "top": 720, "x1": 130, "bottom": 732, "text": "world"},
            ]
        )
        fake_plumber = FakePlumberModule([fake_page, FakePage([])])

        with mock.patch.dict(sys.modules, {"pdfplumber": fake_plumber}):
            coordinates = PdfPlumberCoordinateAdapter().extract("unused.pdf")

        self.assertEqual(2, len(coordinates))
        first = coordinates[0]
        self.assertEqual(0, first["page"])
        self.assertEqual(72.0, first["x0"])
        self.assertEqual(720.0, first["y0"])
        self.assertEqual(100.0, first["x1"])
        self.assertEqual("hello", first["text"])

    def test_returns_empty_when_pdfplumber_missing(self):
        import sys

        from unittest import mock

        from main import PdfPlumberCoordinateAdapter

        with mock.patch.dict(sys.modules, {"pdfplumber": None}):
            self.assertEqual([], PdfPlumberCoordinateAdapter().extract("unused.pdf"))


class RetrievalEvalHarnessTests(unittest.TestCase):

    def test_precision_at_k_counts_relevant_chunks(self):
        from main import precision_at_k

        chunks = build_page_chunks(["alpha revenue grew", "beta margin improved", "gamma risk"])
        hits = [RetrievalHit(chunk=chunks[0], score=3.0), RetrievalHit(chunk=chunks[1], score=2.0)]
        self.assertEqual(0.5, precision_at_k(hits, {chunks[0].chunk_id}, k=2))
        self.assertEqual(1.0, precision_at_k(hits, {chunks[0].chunk_id}, k=1))
        self.assertEqual(0.0, precision_at_k(hits, {"none"}, k=2))

    def test_reciprocal_rank_and_mrr(self):
        from main import mean_reciprocal_rank, reciprocal_rank

        chunks = build_page_chunks(["alpha", "beta", "gamma"])
        v1 = [RetrievalHit(chunk=chunks[i], score=float(3 - i)) for i in range(3)]
        v2 = [RetrievalHit(chunk=chunks[i], score=float(3 - i)) for i in range(3)]
        self.assertEqual(1.0 / 3.0, reciprocal_rank(v1, {chunks[2].chunk_id}))
        self.assertEqual(0.0, reciprocal_rank(v1, {"absent"}))
        self.assertEqual(
            ((1.0 / 1.0) + (1.0 / 2.0)) / 2.0,
            mean_reciprocal_rank(
                [v1, v2],
                [{chunks[0].chunk_id}, {chunks[1].chunk_id}],
            ),
        )
        self.assertEqual(0.0, mean_reciprocal_rank([], []))

    def test_default_gold_corpus_is_deterministic(self):
        from main import evaluate_retrievers

        first = evaluate_retrievers(include_dense=False)
        second = evaluate_retrievers(include_dense=False)
        self.assertEqual(first, second)
        self.assertEqual(13, first["num_queries"])
        self.assertGreaterEqual(first["num_chunks"], 9)
        self.assertIn("bm25", first["results"])
        self.assertIn("simple_sparse", first["results"])

    def test_empty_corpus_backends_report_not_executed(self):
        from main import evaluate_retrievers

        report = evaluate_retrievers(
            corpus=[],
            queries=[{"query": "absent", "relevant_pages": set()}],
        )
        self.assertEqual(0, report["num_chunks"])
        # No dense or rerank query actually executed, so metadata must be null
        # rather than claiming an importable backend produced results.
        self.assertIsNone(report["backends"]["dense_backend"])
        self.assertIsNone(report["backends"]["reranker"])

    def test_empty_corpus_with_available_reranker_reports_not_executed(self):
        import sys
        from unittest import mock
        from main import BgeRerankerAdapter, evaluate_retrievers

        class AvailableReranker(BgeRerankerAdapter):
            def is_available(self):
                return True

        reranker = AvailableReranker()
        with mock.patch("main.get_reranker_adapter", return_value=reranker):
            report = evaluate_retrievers(
                corpus=[],
                queries=[{"query": "absent", "relevant_pages": set()}],
                include_dense=False,
                include_rerank=True,
            )
        # Empty corpus: BM25 has no candidates, so the reranker model never ran.
        # It must report not-executed (None), not a claimed "identity".
        self.assertEqual(0, report["num_chunks"])
        self.assertIsNone(report["backends"]["reranker"])

    def test_eval_backends_reflect_mixed_dense_fallback(self):
        import sys
        from unittest import mock
        from main import evaluate_retrievers

        class BrokenBGEM3:
            def encode(self, texts, return_dense=True, return_sparse=True):
                raise RuntimeError("offline")

        class FakeEmbedding:
            def __init__(self, *args, **kwargs):
                pass

            def query_embed(self, query):
                return iter([[1.0, 0.0]])

            def embed(self, texts):
                return iter([[1.0, 0.0] for _ in texts])

        fake_fastembed = mock.MagicMock()
        fake_fastembed.TextEmbedding = FakeEmbedding

        with mock.patch.dict(sys.modules, {"fastembed": fake_fastembed}):
            adapter_ctor_orig = __import__("main").DenseRetrieverAdapter
            created = []

            def factory(*args, **kwargs):
                adapter = adapter_ctor_orig(*args, **kwargs)
                created.append(adapter)
                return adapter

            with mock.patch.object(__import__("main"), "DenseRetrieverAdapter", side_effect=factory):
                with mock.patch("main.DenseRetrieverAdapter._fastembed_available", return_value=True):
                    with mock.patch("main.DenseRetrieverAdapter._bge_m3_available", return_value=True):
                        with mock.patch("main.DenseRetrieverAdapter._get_bge_m3_model", return_value=BrokenBGEM3()):
                            report = evaluate_retrievers(include_rerank=False)

        # Every dense query fell back to FastEmbed, so the aggregate metadata
        # must report fastembed, never bge-m3.
        self.assertEqual("fastembed", report["backends"]["dense_backend"])
        self.assertIsNone(report["backends"]["reranker"])

    def test_eval_reuses_dense_hits_between_dense_and_hybrid_metrics(self):
        from unittest import mock
        from main import evaluate_retrievers

        class CountingDense:
            calls = 0

            def __init__(self, *args, **kwargs):
                self._last_status = "not_executed"
                self._last_backend = None

            def is_available(self):
                return True

            def retrieve(self, query, chunks, limit=3):
                type(self).calls += 1
                self._last_backend = "fastembed"
                self._last_status = "success"
                return []

            def effective_backend(self):
                return self._last_backend

        queries = [
            {"query": "alpha", "relevant_pages": set()},
            {"query": "beta", "relevant_pages": set()},
        ]
        corpus = [{"text": "alpha beta"}]
        with mock.patch("main.DenseRetrieverAdapter", CountingDense):
            report = evaluate_retrievers(
                corpus=corpus,
                queries=queries,
                include_dense=True,
                include_rerank=False,
            )

        self.assertEqual(2, CountingDense.calls)
        self.assertEqual("fastembed", report["backends"]["dense_backend"])

    def test_eval_backends_mixed_reranker_reports_list(self):
        import sys
        from unittest import mock
        from main import evaluate_retrievers

        class FlakyReranker:
            def __init__(self):
                self.calls = 0
                self._last_status = "not_executed"

            def is_available(self):
                return True

            def effective_backend(self):
                return "identity" if self._last_error else "bge-reranker-v2-m3"

            def rerank(self, query, hits, limit=None):
                self._last_error = None
                if not hits or not query.strip():
                    self._last_status = "not_executed"
                    return hits if limit is None else hits[:limit]
                self.calls += 1
                if self.calls % 2 == 0:
                    self._last_status = "failed"
                    self._last_error = "score count mismatch"
                    return hits if limit is None else hits[:limit]
                self._last_backend = "bge-reranker-v2-m3"
                self._last_status = "success"
                return hits if limit is None else hits[:limit]

        reranker = FlakyReranker()
        with mock.patch("main.get_reranker_adapter", return_value=reranker):
            report = evaluate_retrievers(include_dense=False, include_rerank=True, limit=1)

        # Alternating success/failure across queries -> mixed statuses, so the
        # metadata is the counts dict, not a single backend name.
        backends = report["backends"]["reranker"]
        self.assertIsInstance(backends, dict)
        self.assertEqual(7, backends["backends"]["bge-reranker-v2-m3"])
        self.assertEqual(6, backends["backends"]["identity"])
        self.assertEqual({"success": 7, "failed": 6}, backends["statuses"])
        self.assertIsNone(report["backends"]["dense_backend"])

    def test_candidate_pool_floor_preserves_top_k_with_identity_reranker(self):
        import os
        from main import _select_sparse_context, Bm25SRetriever, IdentityReranker

        pages = [
            "Revenue was 125 million dollars and the company grew strongly last year.",
            "Research investment was 18 million dollars across multiple lines of disclosure.",
            "Operating margin improved to 21 percent in the latest quarter.",
        ]
        chunks = build_page_chunks(pages)

        # A tiny or invalid OPENMATRIX_RERANK_CANDIDATES must never shrink the
        # candidate pool below the requested per-question limit when the identity
        # reranker is in effect (that would drop the top-K contract to a single
        # hit). Restore any pre-existing value afterwards.
        original = os.environ.get("OPENMATRIX_RERANK_CANDIDATES")
        try:
            os.environ["OPENMATRIX_RERANK_CANDIDATES"] = "1"
            context, fallback = _select_sparse_context(
                ["million dollars"],
                chunks,
                retriever=Bm25SRetriever(),
                reranker=IdentityReranker(),
                limit_per_question=2,
            )
        finally:
            if original is None:
                os.environ.pop("OPENMATRIX_RERANK_CANDIDATES", None)
            else:
                os.environ["OPENMATRIX_RERANK_CANDIDATES"] = original

        self.assertFalse(fallback)
        self.assertEqual(2, context.count("[Page"))


    def test_evaluate_retrievers_does_not_duplicate_dense_status_counts(self):
        from unittest import mock
        from main import evaluate_retrievers

        class CountingDense:
            def __init__(self, *args, **kwargs):
                self._last_status = "not_executed"
                self._last_backend = None
                self.calls = 0

            def is_available(self):
                return True

            def retrieve(self, query, chunks, limit=3):
                self.calls += 1
                self._last_backend = "fastembed"
                self._last_status = "failed" if self.calls == 1 else "success"
                return []

            def effective_backend(self):
                return self._last_backend

        queries = [{"query": "a", "relevant_pages": set()}, {"query": "b", "relevant_pages": set()}]
        corpus = [{"text": "alpha"}]

        adapter = CountingDense()
        with mock.patch("main.DenseRetrieverAdapter", return_value=adapter):
            report = evaluate_retrievers(corpus=corpus, queries=queries, include_dense=True, include_rerank=False)

        # 2 queries total, one dense call per query
        metadata = report["backends"]["dense_backend"]
        self.assertIsInstance(metadata, dict)
        self.assertEqual(2, metadata["backends"].get("fastembed", 0))
        self.assertEqual(1, metadata["statuses"].get("failed", 0))
        self.assertEqual(1, metadata["statuses"].get("success", 0))

    def test_eval_emits_dense_and_hybrid_metrics_when_fastembed_available(self):
        import sys
        from unittest import mock
        from main import evaluate_retrievers

        class FakeEmbedding:
            def __init__(self, *args, **kwargs):
                pass

            def query_embed(self, query):
                return iter([[1.0, 0.0]])

            def embed(self, texts):
                return iter([[0.8, 0.6] for _ in texts])

        fake_module = mock.MagicMock()
        fake_module.TextEmbedding = FakeEmbedding

        main_module = sys.modules.get("main")
        original = main_module._dense_models.copy()
        main_module._dense_models.clear()
        try:
            with mock.patch.dict(sys.modules, {"fastembed": fake_module, "FlagEmbedding": None}):
                report = evaluate_retrievers(limit=3)
        finally:
            main_module._dense_models.clear()
            main_module._dense_models.update(original)

        for name in ("bm25", "dense", "hybrid"):
            self.assertIn(name, report["results"])
            self.assertIn("avg_recall@3", report["results"][name])
            self.assertIn("mrr", report["results"][name])

    def test_metrics_computed_for_both_retrievers(self):
        from main import evaluate_retrievers

        report = evaluate_retrievers(limit=3, include_dense=False)
        for name in ("bm25", "simple_sparse"):
            self.assertIn("precision@3", report["results"][name])
            self.assertIn("recall@3", report["results"][name])
            self.assertIn("ndcg@3", report["results"][name])
            self.assertIn("avg_precision@3", report["results"][name])
            self.assertIn("avg_recall@3", report["results"][name])
            self.assertIn("avg_ndcg@3", report["results"][name])
            self.assertIn("mrr", report["results"][name])
            self.assertGreaterEqual(report["results"][name]["mrr"], 0.0)
            self.assertLessEqual(report["results"][name]["mrr"], 1.0)

    def test_gold_corpus_covers_synonym_chinese_table_and_cross_page(self):
        from main import DEFAULT_GOLD_QUERIES, evaluate_retrievers

        queries = {entry["query"]: entry for entry in DEFAULT_GOLD_QUERIES}
        self.assertIn("adjusted earnings per share consensus", queries)
        self.assertIn("每股收益 adjusted earnings per share", queries)
        self.assertIn("数据库市场 database market opportunity", queries)
        self.assertIn("segment revenue by hardware", queries)
        self.assertIn("future business headwinds and trade barrier concerns", queries)
        self.assertEqual({2, 9}, queries["segment revenue by cloud"]["relevant_pages"])
        self.assertEqual({5}, queries["future business headwinds and trade barrier concerns"]["relevant_pages"])
        self.assertEqual(set(), queries["non relevant keyword entirely absent"]["relevant_pages"])

        report = evaluate_retrievers(limit=3, include_dense=False)
        self.assertEqual(13, report["num_queries"])

    def test_recall_at_k_and_ndcg_at_k(self):
        from main import build_page_chunks, ndcg_at_k, recall_at_k, RetrievalHit

        chunks = build_page_chunks(["alpha revenue grew", "beta margin improved", "gamma risk"])
        hits = [
            RetrievalHit(chunk=chunks[1], score=3.0),
            RetrievalHit(chunk=chunks[0], score=2.0),
            RetrievalHit(chunk=chunks[2], score=1.0),
        ]
        relevant = {chunks[0].chunk_id, chunks[2].chunk_id}

        self.assertEqual(1.0, recall_at_k(hits, relevant, k=3))
        self.assertEqual(0.0, recall_at_k(hits, relevant, k=1))
        self.assertEqual(0.0, recall_at_k(hits, set(), k=3))
        self.assertEqual(0.0, ndcg_at_k([], relevant, k=3))

        top1 = ndcg_at_k(hits, relevant, k=1)
        top3 = ndcg_at_k(hits, relevant, k=3)
        self.assertEqual(0.0, top1)
        self.assertGreater(top3, top1)
        self.assertGreaterEqual(top3, 0.0)
        self.assertLessEqual(top3, 1.0)

        first_relevant = [
            RetrievalHit(chunk=chunks[0], score=3.0),
            RetrievalHit(chunk=chunks[2], score=2.0),
            RetrievalHit(chunk=chunks[1], score=1.0),
        ]
        self.assertEqual(0.5, recall_at_k(first_relevant, relevant, k=1))
        self.assertGreater(ndcg_at_k(first_relevant, relevant, k=3), ndcg_at_k(hits, relevant, k=3))

    def test_relevant_chunk_ids_uses_pages(self):
        from main import relevant_chunk_ids

        chunks = build_page_chunks(["page one text", "page two text"])
        page_one_ids = relevant_chunk_ids(chunks, {1})
        self.assertTrue(all(chunk.page == 1 for chunk in chunks if chunk.chunk_id in page_one_ids))
        self.assertTrue(all(chunk.page != 1 for chunk in chunks if chunk.chunk_id not in page_one_ids))


if __name__ == "__main__":
    unittest.main()
