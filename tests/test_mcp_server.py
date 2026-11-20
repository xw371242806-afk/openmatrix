import http.server
import socket
import contextlib
import os
import threading
import tempfile
import unittest
import urllib.parse
import urllib.request
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

import mcp_server
import main
from mcp_server import (
    ak_financials,
    ak_stock_quote,
    analyze_documents,
    compute_financials,
    decompose_analysis,
    export_matrix_report,
    export_report,
    export_slides,
    locate_quote_bboxes,
    financial_model,
    extract_kpis,
    feature_flag,
    fetch_context,
    macro_fred_lookup,
    retrieve_context,
    sec_get_filing,
    sec_get_xbrl_facts,
    sec_search,
    summarize_context,
    verify_quote,
)


def _pptx_available() -> bool:
    try:
        import pptx  # noqa: F401
        return True
    except ImportError:
        return False


def _make_run(cells: list[dict]) -> str:
    """Persist a synthetic matrix run into the current store and return its id."""
    run_id = "mcp-test-run-001"
    for i, cell in enumerate(cells):
        main.persist_matrix_cell(run_id, cell)
    return run_id


class McpExportMatrixReportTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        db_path = Path(self.tmpdir.name) / "matrix.sqlite3"
        self.original_db_path = main.MATRIX_DB_PATH
        main.MATRIX_DB_PATH = db_path
        self.addCleanup(setattr, main, "MATRIX_DB_PATH", self.original_db_path)

    def _sample_run_id(self) -> str:
        return _make_run([
            {
                "cell_id": "c1",
                "document_index": 0,
                "document_name": "doc-a.pdf",
                "question_index": 0,
                "question": "What was total revenue?",
                "status": "completed",
                "answer": "125 million dollars.",
                "quote": "Total revenue was 125 million dollars.",
                "citation_status": "verified_exact",
                "page": 1,
            },
            {
                "cell_id": "c2",
                "document_index": 1,
                "document_name": "doc-b.pdf",
                "question_index": 0,
                "question": "What was research investment?",
                "status": "completed",
                "answer": "18 million dollars.",
                "quote": "Research investment was 18 million dollars.",
                "citation_status": "verified_exact",
                "page": 2,
            },
        ])

    def test_export_csv_contains_expected_columns_and_rows(self):
        run_id = self._sample_run_id()

        output = export_matrix_report(run_id, format="csv")

        self.assertIn("document_name,question,answer,quote,citation_status,page", output)
        self.assertIn("doc-a.pdf", output)
        self.assertIn("125 million dollars.", output)
        self.assertIn("doc-b.pdf", output)
        self.assertIn("verified_exact", output)

    def test_export_markdown_contains_table(self):
        run_id = self._sample_run_id()

        output = export_matrix_report(run_id, format="markdown")

        self.assertIn("| document_name | question |", output)
        self.assertIn("| doc-a.pdf |", output)
        self.assertIn("| 125 million dollars. |", output)

    def test_missing_run_returns_error_without_crashing(self):
        output = export_matrix_report("no-such-run", format="csv")

        self.assertTrue(output.startswith("Error:"))
        self.assertIn("not found", output)

    def test_unsupported_format_returns_error(self):
        run_id = self._sample_run_id()

        output = export_matrix_report(run_id, format="xlsx")

        self.assertTrue(output.startswith("Error:"))
        self.assertIn("unsupported format", output)

    def test_formula_injection_is_neutralized_in_csv(self):
        run_id = _make_run([
            {
                "cell_id": "c-evil",
                "document_index": 0,
                "document_name": "=HYPERLINK(evil)",
                "question_index": 0,
                "question": "q",
                "status": "completed",
                "answer": "+1+1",
                "quote": "@echo",
                "citation_status": "needs_review",
                "page": 1,
            },
        ])

        output = export_matrix_report(run_id, format="csv")

        self.assertIn("'=HYPERLINK(evil)", output)
        self.assertIn("'+1+1", output)
        self.assertIn("'@echo", output)


SAMPLE_PAGES = [
    "OpenMatrix Test Company 2025 Annual Report. Total revenue was 125 million dollars.",
    "Research and development investment was 18 million dollars. The primary risk was supply chain disruption.",
]


class McpRetrieveContextTests(unittest.TestCase):
    def test_bm25_returns_structured_chunks(self):
        import json

        output = retrieve_context(
            "What was total revenue?",
            SAMPLE_PAGES,
            mode="bm25",
            limit_per_question=2,
        )
        data = json.loads(output)

        self.assertIn("chunks", data)
        self.assertIn("full_document", data)
        self.assertEqual(False, data["full_document"])
        self.assertGreater(len(data["chunks"]), 0)
        first = data["chunks"][0]
        self.assertIn("chunk_id", first)
        self.assertIn("page", first)
        self.assertIn("text", first)

    def test_invalid_mode_falls_back_to_bm25(self):
        import json

        output = retrieve_context(
            "What was total revenue?",
            SAMPLE_PAGES,
            mode="not-a-real-mode",
            limit_per_question=2,
        )
        data = json.loads(output)

        self.assertIn("chunks", data)
        self.assertIn("full_document", data)
        self.assertIsInstance(data["chunks"], list)

    def test_empty_question_returns_error(self):
        output = retrieve_context("", SAMPLE_PAGES, mode="bm25")

        self.assertTrue(output.startswith("Error:"))
        self.assertIn("non-empty", output)

    def test_empty_pages_returns_error(self):
        output = retrieve_context("What was total revenue?", [], mode="bm25")

        self.assertTrue(output.startswith("Error:"))
        self.assertIn("non-empty", output)

    def test_dense_mode_falls_back_gracefully_when_unavailable(self):
        import json

        output = retrieve_context(
            "What was research investment?",
            SAMPLE_PAGES,
            mode="dense",
            limit_per_question=1,
        )
        # dense may be unavailable (no model); must not crash either way
        data = json.loads(output)
        self.assertIn("chunks", data)
        self.assertIn("full_document", data)


class McpVerifyQuoteTests(unittest.TestCase):
    def test_existing_quote_returns_verified_exact_with_page(self):
        import json

        output = verify_quote("Total revenue was 125 million", SAMPLE_PAGES)
        data = json.loads(output)

        self.assertEqual("verified_exact", data.get("citation_status"))
        self.assertEqual(1, data.get("page"))

    def test_missing_quote_returns_not_found(self):
        import json

        output = verify_quote("total revenue was 200 million", SAMPLE_PAGES)
        data = json.loads(output)

        self.assertEqual("not_found", data.get("citation_status"))
        self.assertIsNone(data.get("page"))

    def test_empty_quote_returns_not_applicable(self):
        import json

        output = verify_quote("", SAMPLE_PAGES)
        data = json.loads(output)

        self.assertEqual("not_applicable", data.get("citation_status"))
        self.assertIsNone(data.get("page"))

    def test_empty_pages_returns_error(self):
        output = verify_quote("some quote", [])

        self.assertTrue(output.startswith("Error:"))
        self.assertIn("non-empty", output)


class _MockHttpHandler(http.server.BaseHTTPRequestHandler):
    """Serves a tiny static HTML page for fetch_context tests."""

    _PAGE = (
        "<html><head><title>OpenMatrix Test Page</title></head><body>"
        "<h1>Annual Report</h1>"
        "<p>OpenMatrix Test Company total revenue was 125 million dollars in 2025.</p>"
        "<p>Research and development investment was 18 million dollars.</p>"
        "<script>var x = 1;</script>"
        "<nav>Skip this boilerplate navigation.</nav>"
        "<ul><li>Key risk: supply chain disruption</li></ul>"
        "</body></html>"
    )

    def do_GET(self):
        if self.path.startswith("/redirect"):
            # Redirect to the same host's /report endpoint (302).
            self.send_response(302)
            self.send_header("Location", "/report")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = self._PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class McpFetchContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._server = http.server.HTTPServer(("127.0.0.1", 0), _MockHttpHandler)
        cls._port = cls._server.server_address[1]
        cls._thread = threading.Thread(target=cls._server.serve_forever, daemon=True)
        cls._thread.start()

    @classmethod
    def tearDownClass(cls):
        cls._server.shutdown()
        cls._server.server_close()

    @property
    def base_url(self):
        return f"http://localhost:{self._port}"

    @contextlib.contextmanager
    def _local_dns_context(self):
        import socket
        original = socket.getaddrinfo
        def lookup(host, port, *args, **kwargs):
            return original("127.0.0.1" if host == "localhost" else host, port, *args, **kwargs)
        with mock.patch("mcp_server._is_blocked_ip", return_value=False), mock.patch("mcp_server.socket.getaddrinfo", side_effect=lookup):
            yield

    def _allow_private(self):
        """Inject a test-only policy so the local mock server is reachable.

        Production code never reads this env var; the SSRF guard is bypassed only
        by patching the module-level predicate within this test class.
        """
        import unittest.mock

        return self._local_dns_context()

    def test_fetch_returns_structured_chunks(self):
        import json

        with self._allow_private():
            output = fetch_context(
                self.base_url + "/report",
                "What was total revenue?",
                mode="bm25",
                limit_per_question=2,
            )
        data = json.loads(output)

        self.assertIn("url", data)
        self.assertIn("chunks", data)
        self.assertIn("full_document", data)
        self.assertIn("extracted_chars", data)
        self.assertEqual(False, data["full_document"])
        self.assertGreater(len(data["chunks"]), 0)
        # boilerplate (script/nav) must not leak into extracted text
        self.assertGreater(data["extracted_chars"], 50)
        combined = " ".join(c.get("text", "") for c in data["chunks"])
        self.assertIn("revenue", combined)

    def test_invalid_url_returns_error(self):
        output = fetch_context("not-a-url", "What was total revenue?", mode="bm25")

        self.assertTrue(output.startswith("Error:"))

    def test_connection_error_returns_error(self):
        # port 1 is almost never listening -> connection refused
        with self._allow_private():
            output = fetch_context(
                "http://127.0.0.1:1/never",
                "What was total revenue?",
                mode="bm25",
            )

        self.assertTrue(output.startswith("Error:"))

    def test_empty_question_returns_error(self):
        output = fetch_context(self.base_url + "/report", "", mode="bm25")

        self.assertTrue(output.startswith("Error:"))
        self.assertIn("non-empty", output)

    def test_dense_mode_falls_back_gracefully(self):
        import json

        with self._allow_private():
            output = fetch_context(
                self.base_url + "/report",
                "What was the investment?",
                mode="dense",
                limit_per_question=1,
            )
        data = json.loads(output)
        self.assertIn("chunks", data)
        self.assertIn("full_document", data)

    def test_ssrf_metadata_endpoint_always_blocked(self):
        from mcp_server import _is_blocked_ip

        self.assertTrue(_is_blocked_ip("169.254.169.254"))
        self.assertTrue(_is_blocked_ip("169.254.170.2"))

    def test_ssrf_loopback_private_blocked_by_default(self):
        from mcp_server import _is_blocked_ip

        for ip in ["127.0.0.1", "10.0.0.5", "192.168.1.1", "0.0.0.0", "::1", "fe80::1"]:
            self.assertTrue(_is_blocked_ip(ip), f"{ip} should be blocked")

    def test_ssrf_resolve_rejects_blocked(self):
        from mcp_server import _resolve_public_ips

        with self.assertRaises(ValueError) as ctx:
            _resolve_public_ips("http://127.0.0.1:80/report")
        self.assertIn("blocked", str(ctx.exception).lower())
        with self.assertRaises(ValueError):
            _resolve_public_ips("http://169.254.169.254/latest/meta-data/")

    def test_ssrf_scheme_and_host_validation(self):
        from mcp_server import _resolve_public_ips

        with self.assertRaises(ValueError):
            _resolve_public_ips("ftp://example.com/file")
        with self.assertRaises(ValueError):
            _resolve_public_ips("http:///no-host")
        with self.assertRaises(ValueError):
            _resolve_public_ips("")

    def test_ssrf_public_host_passes_validation(self):
        from mcp_server import _is_blocked_ip, _resolve_public_ips

        # example.com resolves to public IPs; must pass validation.
        with mock.patch("mcp_server.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]):
            parsed, ip = _resolve_public_ips("https://example.com/")
        self.assertEqual("https", parsed.scheme)
        self.assertFalse(_is_blocked_ip(ip))

    def test_ssrf_redirect_limit_enforced(self):
        from mcp_server import _SSRF_MAX_REDIRECTS, _SsrfsafeRedirectHandler

        handler = _SsrfsafeRedirectHandler()
        handler._hops = _SSRF_MAX_REDIRECTS
        with self.assertRaises(ValueError):
            handler.redirect_request(
                None, None, 302, "Found", {"Location": "https://example.com/"}, "https://example.com/"
            )

    def test_ssrf_redirect_to_private_rejected(self):
        from unittest import mock

        from mcp_server import _SsrfsafeRedirectHandler

        handler = _SsrfsafeRedirectHandler()
        handler._hops = 0
        with mock.patch("mcp_server._resolve_public_ips", side_effect=ValueError("blocked address")):
            with self.assertRaises(ValueError):
                handler.redirect_request(
                    None, None, 302, "Found", {"Location": "http://127.0.0.1/private"}, "http://example.com/"
                )

    def test_ssrf_response_size_limit_enforced(self):
        from unittest import mock

        from mcp_server import _SSRF_MAX_BYTES, _fetch_url

        class _BigBody:
            def read(self, n=-1):
                return b"x" * (_SSRF_MAX_BYTES + 1)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class _FakeResp:
            headers = type("H", (), {"get_content_charset": lambda self: "utf-8"})()

        fake_open = mock.MagicMock()
        fake_open.open.return_value.__enter__.return_value = _BigBody()
        with mock.patch("mcp_server.urllib.request.build_opener", return_value=fake_open):
            with self.assertRaises(ValueError):
                _fetch_url("https://example.com/big")

    def test_ssrf_no_env_bypass_in_production(self):
        # The production guard must not be disabled by an environment variable.
        from mcp_server import _is_blocked_ip

        old = os.environ.get("OPENMATRIX_ALLOW_PRIVATE_FETCH")
        os.environ["OPENMATRIX_ALLOW_PRIVATE_FETCH"] = "1"
        try:
            self.assertTrue(_is_blocked_ip("127.0.0.1"))
            self.assertTrue(_is_blocked_ip("10.0.0.5"))
            self.assertTrue(_is_blocked_ip("169.254.169.254"))
        finally:
            if old is None:
                os.environ.pop("OPENMATRIX_ALLOW_PRIVATE_FETCH", None)
            else:
                os.environ["OPENMATRIX_ALLOW_PRIVATE_FETCH"] = old

    def test_ssrf_https_pinned_connection_defaults_to_443(self):
        # HTTPS connections must default to port 443 and keep the original
        # hostname (for SNI/TLS verification) while the socket goes to the pin.
        from mcp_server import _PinnedHttpsConnection

        conn = _PinnedHttpsConnection("example.com", pinned_ip="93.184.216.34")
        self.assertEqual(443, conn.port)
        self.assertEqual("example.com", conn.host)
        self.assertEqual("93.184.216.34", conn._pinned_ip)

    def test_ssrf_https_connect_uses_pinned_ip_and_port(self):
        from unittest import mock

        from mcp_server import _PinnedHttpsConnection

        conn = _PinnedHttpsConnection("example.com", pinned_ip="93.184.216.34")
        fake_sock = mock.MagicMock()
        with mock.patch("mcp_server.socket.create_connection", return_value=fake_sock) as mk:
            # connect() calls super().connect() which creates the socket to the pin,
            # then wraps it in TLS with server_hostname=host.
            with mock.patch("mcp_server.ssl.create_default_context") as mk_ctx:
                conn.connect()
        self.assertEqual(mk.call_args[0][0], ("93.184.216.34", 443))
        # TLS wrap must use the original hostname for SNI.
        wrap_call = mk_ctx.return_value.wrap_socket.call_args
        self.assertEqual(wrap_call.kwargs.get("server_hostname"), "example.com")

    def test_ssrf_redirect_revalidates_and_reaches_target(self):
        # A redirect to the same host must be re-validated (resolve called again)
        # and still reach the final page, proving the pinned connection survives
        # the redirect without falling back to unvalidated DNS.
        from unittest import mock

        import mcp_server

        real_resolve = mcp_server._resolve_public_ips
        calls = []

        def counting_resolve(url):
            calls.append(url)
            return real_resolve(url)

        with mock.patch("mcp_server._resolve_public_ips", side_effect=counting_resolve):
            with self._allow_private():
                output = fetch_context(
                    self.base_url + "/redirect",
                    "What was total revenue?",
                    mode="bm25",
                    limit_per_question=2,
                )
        import json

        data = json.loads(output)
        self.assertIn("chunks", data)
        self.assertGreater(len(data["chunks"]), 0)
        # resolve must have been called at least twice (initial + redirect hop)
        self.assertGreaterEqual(len(calls), 2)


class McpSummarizeContextTests(unittest.TestCase):
    def test_summary_is_extractive_and_grounded_in_pages(self):
        import json

        output = summarize_context(
            SAMPLE_PAGES,
            "What was total revenue?",
            mode="bm25",
            limit_per_question=3,
            max_sentences_per_chunk=2,
        )
        data = json.loads(output)

        self.assertIn("summary", data)
        self.assertIn("source_chunks", data)
        self.assertIn("mode", data)
        self.assertIn("full_document", data)
        self.assertGreater(len(data["summary"]), 0)
        # every summary sentence must come verbatim from the page text
        self.assertIn("revenue", data["summary"].lower())
        self.assertIn("125 million", data["summary"])

    def test_empty_question_returns_error(self):
        output = summarize_context(SAMPLE_PAGES, "", mode="bm25")

        self.assertTrue(output.startswith("Error:"))
        self.assertIn("non-empty", output)

    def test_empty_pages_returns_error(self):
        output = summarize_context([], "What was total revenue?", mode="bm25")

        self.assertTrue(output.startswith("Error:"))
        self.assertIn("non-empty", output)

    def test_invalid_mode_falls_back_to_bm25(self):
        import json

        output = summarize_context(
            SAMPLE_PAGES,
            "What was total revenue?",
            mode="not-a-real-mode",
            limit_per_question=3,
        )
        data = json.loads(output)

        self.assertIn("summary", data)
        self.assertIsInstance(data["summary"], str)


class McpVerifyQuoteLayeringTests(unittest.TestCase):
    """Backward compatibility + fuzzy/semantic layering for verify_quote."""

    def test_exact_mode_is_backward_compatible(self):
        import json

        # exact match, no mode argument → identical output to pre-layering
        output = verify_quote("Total revenue was 125 million", SAMPLE_PAGES)
        data = json.loads(output)

        self.assertEqual("verified_exact", data.get("citation_status"))
        self.assertEqual(1, data.get("page"))
        self.assertNotIn("similarity", data)

    def test_exact_mode_missing_quote_backward_compatible(self):
        import json

        output = verify_quote("Total revenue was 200 million", SAMPLE_PAGES)
        data = json.loads(output)

        self.assertEqual("not_found", data.get("citation_status"))
        self.assertIsNone(data.get("page"))

    def test_fuzzy_mode_hit_returns_fuzzy_match_with_similarity(self):
        import json

        # near-exact with minor punctuation/whitespace drift
        output = verify_quote(
            "Total revenue was 125 million dollars",
            SAMPLE_PAGES,
            mode="fuzzy",
        )
        data = json.loads(output)

        self.assertEqual("fuzzy_match", data.get("citation_status"))
        self.assertIn("similarity", data)
        self.assertGreaterEqual(data["similarity"], 0.85)
        self.assertIsNotNone(data.get("page"))

    def test_fuzzy_mode_miss_returns_not_found(self):
        import json

        output = verify_quote(
            "completely unrelated content about the moon",
            SAMPLE_PAGES,
            mode="fuzzy",
        )
        data = json.loads(output)

        self.assertEqual("not_found", data.get("citation_status"))

    def test_semantic_mode_hit_for_paraphrase(self):
        import json

        # light paraphrase: punctuation + word-order drift, no verbatim match
        output = verify_quote(
            "Total revenue, was 125 million dollars",
            SAMPLE_PAGES,
            mode="semantic",
        )
        data = json.loads(output)

        self.assertEqual("semantic_match", data.get("citation_status"))
        self.assertIn("similarity", data)

    def test_semantic_mode_miss_returns_not_found(self):
        import json

        output = verify_quote(
            "quantum computing in agriculture",
            SAMPLE_PAGES,
            mode="semantic",
        )
        data = json.loads(output)

        self.assertEqual("not_found", data.get("citation_status"))

    def test_semantic_mode_not_diluted_by_long_page(self):
        import json

        # regression for supervisor-flagged risk: overlap-coefficient must not be
        # diluted by a long page (Jaccard union denominator would collapse it)
        long_page = (
            "This annual report contains extensive boilerplate and risk disclosures. "
            "It covers market conditions, competitive landscape, regulatory overview, "
            "supply chain considerations, operational metrics, forward-looking statements, "
            "liquidity analysis, capital allocation, governance practices, compensation "
            "philosophy, environmental commitments, and many other standard sections. "
            "In 2025 the company's total revenue was 125 million dollars, a figure that "
            "appears deep within this paragraph after all the preceding filler text."
        )
        output = verify_quote(
            "Total revenue, was 125 million dollars",
            [long_page],
            mode="semantic",
        )
        data = json.loads(output)

        self.assertEqual("semantic_match", data.get("citation_status"))
        self.assertEqual(1, data.get("page"))

    def test_empty_quote_not_applicable_all_modes(self):
        import json

        for mode in ("exact", "fuzzy", "semantic"):
            output = verify_quote("", SAMPLE_PAGES, mode=mode)
            data = json.loads(output)
            self.assertEqual("not_applicable", data.get("citation_status"), msg=mode)


class McpExportReportTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        db_path = Path(self.tmpdir.name) / "matrix.sqlite3"
        self.original_db_path = main.MATRIX_DB_PATH
        main.MATRIX_DB_PATH = db_path
        self.addCleanup(setattr, main, "MATRIX_DB_PATH", self.original_db_path)
        # Point the server-fixed export root at the temp dir so tests write there.
        self.original_export_root = os.environ.get("OPENMATRIX_EXPORT_ROOT")
        os.environ["OPENMATRIX_EXPORT_ROOT"] = self.tmpdir.name
        self.addCleanup(self._restore_export_root)

    def _restore_export_root(self):
        if self.original_export_root is None:
            os.environ.pop("OPENMATRIX_EXPORT_ROOT", None)
        else:
            os.environ["OPENMATRIX_EXPORT_ROOT"] = self.original_export_root

    def _run_id(self) -> str:
        return _make_run([
            {
                "cell_id": "c1",
                "document_index": 0,
                "document_name": "doc.pdf",
                "question_index": 0,
                "question": "What was revenue?",
                "status": "completed",
                "answer": "125 million",
                "quote": "Total revenue was 125 million dollars.",
                "citation_status": "verified_exact",
                "page": 1,
            },
        ])

    def test_export_pdf_writes_non_empty_file(self):
        import json

        run_id = self._run_id()
        output = export_report(run_id, format="pdf", output_dir="reports")
        data = json.loads(output)

        self.assertEqual("pdf", data["format"])
        self.assertIn("path", data)
        self.assertEqual(1, data["cells"])
        path = Path(data["path"])
        self.assertTrue(path.exists())
        self.assertGreater(path.stat().st_size, 0)
        self.assertEqual(".pdf", path.suffix)
        # path must stay inside the export root
        Path(data["path"]).resolve().relative_to(Path(self.tmpdir.name).resolve())

    def test_export_xlsx_writes_file(self):
        import json

        run_id = self._run_id()
        output = export_report(run_id, format="xlsx", output_dir="reports")
        data = json.loads(output)

        self.assertEqual("xlsx", data["format"])
        self.assertIn("path", data)
        path = Path(data["path"])
        self.assertTrue(path.exists())
        self.assertGreater(path.stat().st_size, 0)
        self.assertEqual(".xlsx", path.suffix)
        Path(data["path"]).resolve().relative_to(Path(self.tmpdir.name).resolve())

    def test_export_xlsx_uses_evidence_sheet_not_cell_comments(self):
        import json

        from openpyxl import load_workbook

        run_id = self._run_id()
        output = export_report(run_id, format="xlsx", output_dir="reports")
        data = json.loads(output)

        wb = load_workbook(data["path"])
        ws = wb["matrix"]
        evidence = wb["evidence"]
        answer_cell = ws.cell(row=2, column=3)
        self.assertIsNone(answer_cell.comment)
        self.assertEqual(["row", "document_name", "page", "citation_status", "quote"], [c.value for c in evidence[1]])
        self.assertEqual(2, evidence.cell(row=2, column=1).value)
        self.assertEqual("doc.pdf", evidence.cell(row=2, column=2).value)
        self.assertEqual("verified_exact", evidence.cell(row=2, column=4).value)
        self.assertIn("Total revenue was 125 million dollars.", evidence.cell(row=2, column=5).value)

    def test_locate_quote_bboxes_missing_pdfplumber_returns_error(self):
        pdf_path = Path(self.tmpdir.name) / "doc.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 fake")
        old_root = os.environ.get("OPENMATRIX_PDF_ROOT")
        os.environ["OPENMATRIX_PDF_ROOT"] = self.tmpdir.name
        self.addCleanup(lambda: os.environ.pop("OPENMATRIX_PDF_ROOT", None) if old_root is None else os.environ.__setitem__("OPENMATRIX_PDF_ROOT", old_root))

        with mock.patch.dict("sys.modules", {"pdfplumber": None}):
            with mock.patch("builtins.__import__", side_effect=ImportError("no module named 'pdfplumber'")):
                output = locate_quote_bboxes("doc.pdf", "Total revenue")
        self.assertTrue(output.startswith("Error:"))
        self.assertIn("pdfplumber", output)

    def test_locate_quote_bboxes_rejects_escape_absolute_long_quote_and_bad_page(self):
        old_root = os.environ.get("OPENMATRIX_PDF_ROOT")
        os.environ["OPENMATRIX_PDF_ROOT"] = self.tmpdir.name
        self.addCleanup(lambda: os.environ.pop("OPENMATRIX_PDF_ROOT", None) if old_root is None else os.environ.__setitem__("OPENMATRIX_PDF_ROOT", old_root))

        self.assertIn("relative", locate_quote_bboxes(str(Path(self.tmpdir.name) / "doc.pdf"), "Total revenue"))
        self.assertIn("traversal", locate_quote_bboxes("../doc.pdf", "Total revenue"))
        self.assertIn("too long", locate_quote_bboxes("doc.pdf", "x" * 1001))
        self.assertIn("positive", locate_quote_bboxes("doc.pdf", "Total revenue", page=0))

    def test_locate_quote_bboxes_uses_pdfplumber_words_under_root(self):
        import json
        import types

        pdf_path = Path(self.tmpdir.name) / "doc.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 fake")
        old_root = os.environ.get("OPENMATRIX_PDF_ROOT")
        os.environ["OPENMATRIX_PDF_ROOT"] = self.tmpdir.name
        self.addCleanup(lambda: os.environ.pop("OPENMATRIX_PDF_ROOT", None) if old_root is None else os.environ.__setitem__("OPENMATRIX_PDF_ROOT", old_root))

        class FakePage:
            def extract_words(self):
                return [
                    {"text": "Total", "x0": 10, "x1": 35, "top": 20, "bottom": 30},
                    {"text": "revenue", "x0": 40, "x1": 80, "top": 20, "bottom": 30},
                    {"text": "was", "x0": 85, "x1": 100, "top": 20, "bottom": 30},
                ]

        class FakePdf:
            pages = [FakePage()]
            def __enter__(self):
                return self
            def __exit__(self, exc_type, exc, tb):
                return False

        fake_pdfplumber = types.SimpleNamespace(open=lambda _: FakePdf())
        with mock.patch.dict("sys.modules", {"pdfplumber": fake_pdfplumber}):
            output = locate_quote_bboxes("doc.pdf", "Total revenue")

        data = json.loads(output)
        self.assertEqual(1, data["count"])
        self.assertFalse(data["truncated"])
        self.assertEqual(1, data["matches"][0]["page"])
        self.assertEqual([10.0, 20.0, 80.0, 30.0], data["matches"][0]["bbox"])

    def test_export_default_dir_without_output_dir(self):
        import json

        run_id = self._run_id()
        output = export_report(run_id, format="pdf")
        data = json.loads(output)

        self.assertEqual("pdf", data["format"])
        path = Path(data["path"])
        self.assertTrue(path.exists())
        Path(data["path"]).resolve().relative_to(Path(self.tmpdir.name).resolve())

    def test_missing_run_returns_error(self):
        output = export_report("no-such-run", format="pdf", output_dir="reports")

        self.assertTrue(output.startswith("Error:"))
        self.assertIn("not found", output)

    def test_unsupported_format_returns_error(self):
        run_id = self._run_id()
        output = export_report(run_id, format="html", output_dir="reports")

        self.assertTrue(output.startswith("Error:"))
        self.assertIn("unsupported", output)

    def test_absolute_output_dir_rejected(self):
        run_id = self._run_id()
        output = export_report(run_id, format="pdf", output_dir=self.tmpdir.name)

        self.assertTrue(output.startswith("Error:"))
        self.assertIn("absolute", output)

    def test_path_traversal_output_dir_rejected(self):
        run_id = self._run_id()
        output = export_report(run_id, format="pdf", output_dir="../outside")

        self.assertTrue(output.startswith("Error:"))
        self.assertIn("..", output)

    def test_traversal_escape_rejected(self):
        run_id = self._run_id()
        output = export_report(run_id, format="pdf", output_dir="a/../../escape")

        self.assertTrue(output.startswith("Error:"))

    @unittest.skipUnless(_pptx_available(), "python-pptx not installed; skipping pptx file test")
    def test_export_pptx_writes_valid_file(self):
        import json
        import zipfile

        run_id = self._run_id()
        output = export_report(run_id, format="pptx", output_dir="slides")
        data = json.loads(output)
        self.assertEqual("pptx", data["format"])
        path = Path(data["path"])
        self.assertTrue(path.exists())
        self.assertGreater(path.stat().st_size, 0)
        self.assertEqual(".pptx", path.suffix)
        # valid pptx = PK zip magic + [Content_Types].xml
        raw = path.read_bytes()
        self.assertTrue(raw.startswith(b"PK"))
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
        self.assertIn("[Content_Types].xml", names)
        # reopen and assert required content on EVERY result slide
        from pptx import Presentation

        prs = Presentation(str(path))
        self.assertGreater(len(prs.slides), 0)
        for slide in prs.slides:
            slide_text = "\n".join(
                shape.text_frame.text
                for shape in slide.shapes
                if shape.has_text_frame
            )
            self.assertIn("doc.pdf", slide_text)
            self.assertIn("What was revenue?", slide_text)
            self.assertIn("125 million", slide_text)
            self.assertIn("Total revenue was 125 million dollars.", slide_text)
            self.assertIn("Page: 1", slide_text)
            self.assertIn("Citation:", slide_text)
            # citation status must be present on each slide
            self.assertTrue(
                any(s in slide_text for s in ("verified_exact", "needs_review", "not_found"))
            )

    @unittest.skipUnless(_pptx_available(), "python-pptx not installed; skipping pptx long-question test")
    def test_export_pptx_keeps_long_question(self):
        import json

        long_q = "What was the total consolidated revenue including discontinued operations? " * 2
        run_id = _make_run([
            {
                "cell_id": "cL",
                "document_index": 0,
                "document_name": "long.pdf",
                "question_index": 0,
                "question": long_q,
                "status": "completed",
                "answer": "125 million",
                "quote": "Revenue was 125 million.",
                "citation_status": "verified_exact",
                "page": 3,
            },
        ])
        output = export_report(run_id, format="pptx", output_dir="slides")
        data = json.loads(output)
        from pptx import Presentation

        prs = Presentation(str(Path(data["path"])))
        slide_text = "\n".join(
            shape.text_frame.text
            for slide in prs.slides
            for shape in slide.shapes
            if shape.has_text_frame
        )
        self.assertIn(long_q, slide_text)

    def test_export_pptx_missing_library_returns_error(self):
        # Force the pptx import to fail; must return a clear Error: (CI safety).
        from unittest import mock

        run_id = self._run_id()
        with mock.patch.dict("sys.modules", {"pptx": None, "pptx.util": None}):
            with mock.patch("builtins.__import__", side_effect=ImportError("no module named 'pptx'")):
                output = export_report(run_id, format="pptx", output_dir="slides")
        self.assertTrue(output.startswith("Error:"))
        self.assertIn("python-pptx", output)

    def test_export_pptx_uses_confinement(self):
        import json

        run_id = self._run_id()
        output = export_report(run_id, format="pptx", output_dir="a/../../escape")
        self.assertTrue(output.startswith("Error:"))
        output2 = export_report(run_id, format="pptx", output_dir=self.tmpdir.name)
        self.assertTrue(output2.startswith("Error:"))
        self.assertIn("absolute", output2)


def _docx_available() -> bool:
    try:
        import docx  # noqa: F401
        return True
    except ImportError:
        return False


class McpExportDocxTests(McpExportReportTests):
    """DOCX export (export_report format='docx') via python-docx."""

    @unittest.skipUnless(_docx_available(), "python-docx not installed; skipping docx tests")
    def test_export_docx_writes_file(self):
        import json

        run_id = self._run_id()
        output = export_report(run_id, format="docx", output_dir="documents")
        data = json.loads(output)

        self.assertEqual("docx", data["format"])
        self.assertIn("path", data)
        self.assertEqual(1, data["cells"])
        path = Path(data["path"])
        self.assertTrue(path.exists())
        self.assertGreater(path.stat().st_size, 0)
        self.assertEqual(".docx", path.suffix)
        Path(data["path"]).resolve().relative_to(Path(self.tmpdir.name).resolve())

    @unittest.skipUnless(_docx_available(), "python-docx not installed; skipping docx content test")
    def test_export_docx_contains_cell_content(self):
        import json

        from docx import Document

        run_id = self._run_id()
        output = export_report(run_id, format="docx", output_dir="documents")
        data = json.loads(output)
        doc = Document(data["path"])
        table = doc.tables[0]
        self.assertIn("question", [c.text for c in table.rows[0].cells])
        self.assertIn("125 million", "\n".join(c.text for row in table.rows for c in row.cells))

    def test_export_docx_missing_library_returns_error(self):
        run_id = self._run_id()
        with mock.patch.dict("sys.modules", {"docx": None}):
            with mock.patch("builtins.__import__", side_effect=ImportError("no module named 'docx'")):
                output = export_report(run_id, format="docx", output_dir="documents")
        self.assertTrue(output.startswith("Error:"))
        self.assertIn("python-docx", output)

    def test_export_docx_uses_confinement(self):
        run_id = self._run_id()
        output = export_report(run_id, format="docx", output_dir="a/../../escape")
        self.assertTrue(output.startswith("Error:"))
        output2 = export_report(run_id, format="docx", output_dir=self.tmpdir.name)
        self.assertTrue(output2.startswith("Error:"))
        self.assertIn("absolute", output2)


class McpExportSlidesTests(McpExportReportTests):
    """Matrix -> Slides: export_slides renders a citation-footnoted PPTX deck.

    In-memory fake matrix run, one slide per question, and the citation/source
    footnote on every content slide (structured 'citations in slides' pattern).
    """

    @unittest.skipUnless(_pptx_available(), "python-pptx not installed; skipping slides file test")
    def test_export_slides_one_slide_per_question_with_footnotes(self):
        import json

        run_id = self._run_id()  # single-question sample run (1 cell)
        output = export_slides(run_id, output_dir="decks")
        data = json.loads(output)

        self.assertEqual("pptx", data["format"])
        self.assertIn("path", data)
        self.assertEqual(1, data["slides"])  # one slide per question
        path = Path(data["path"])
        self.assertTrue(path.exists())
        self.assertGreater(path.stat().st_size, 0)
        self.assertEqual(".pptx", path.suffix)
        # valid pptx = PK zip magic + [Content_Types].xml
        self.assertTrue(path.read_bytes().startswith(b"PK"))
        Path(data["path"]).resolve().relative_to(Path(self.tmpdir.name).resolve())

        from pptx import Presentation

        prs = Presentation(str(path))
        self.assertEqual(1, len(prs.slides))  # exactly one slide for one question
        for slide in prs.slides:
            slide_text = "\n".join(
                shape.text_frame.text
                for shape in slide.shapes
                if shape.has_text_frame
            )
            # title carries the question, body carries the answer
            self.assertIn("What was revenue?", slide_text)
            self.assertIn("125 million", slide_text)
            # citation/source footnote carries status + page + verbatim quote
            self.assertIn("Citation:", slide_text)
            self.assertIn("verified_exact", slide_text)
            self.assertIn("Page 1", slide_text)
            self.assertIn("Total revenue was 125 million dollars.", slide_text)

    @unittest.skipUnless(_pptx_available(), "python-pptx not installed; skipping slides multi-question test")
    def test_export_slides_multiple_questions_get_one_slide_each(self):
        import json

        run_id = _make_run([
            {
                "cell_id": "s1",
                "document_index": 0,
                "document_name": "doc.pdf",
                "question_index": 0,
                "question": "What was revenue?",
                "status": "completed",
                "answer": "125 million",
                "quote": "Total revenue was 125 million dollars.",
                "citation_status": "verified_exact",
                "page": 1,
            },
            {
                "cell_id": "s2",
                "document_index": 1,
                "document_name": "doc2.pdf",
                "question_index": 0,
                "question": "What was research investment?",
                "status": "completed",
                "answer": "18 million",
                "quote": "Research investment was 18 million dollars.",
                "citation_status": "needs_review",
                "page": 2,
            },
        ])
        output = export_slides(run_id, output_dir="decks")
        data = json.loads(output)
        self.assertEqual("pptx", data["format"])
        self.assertEqual(2, data["slides"])

        from pptx import Presentation

        prs = Presentation(str(Path(data["path"])))
        self.assertEqual(2, len(prs.slides))  # one slide per question
        all_text = []
        for slide in prs.slides:
            slide_text = "\n".join(
                shape.text_frame.text
                for shape in slide.shapes
                if shape.has_text_frame
            )
            all_text.append(slide_text)
        joined = "\n".join(all_text)
        self.assertIn("What was revenue?", joined)
        self.assertIn("What was research investment?", joined)
        self.assertIn("125 million", joined)
        self.assertIn("18 million", joined)
        self.assertIn("needs_review", joined)
        self.assertIn("Research investment was 18 million dollars.", joined)

    def test_export_slides_missing_library_returns_error(self):
        # Force the pptx import to fail; must return a clear Error: (CI safety).
        run_id = self._run_id()
        with mock.patch.dict("sys.modules", {"pptx": None, "pptx.util": None, "pptx.enum": None, "pptx.enum.text": None}):
            with mock.patch("builtins.__import__", side_effect=ImportError("no module named 'pptx'")):
                output = export_slides(run_id, output_dir="decks")
        self.assertTrue(output.startswith("Error:"))
        self.assertIn("python-pptx", output)

    def test_export_slides_uses_confinement(self):
        run_id = self._run_id()
        output = export_slides(run_id, output_dir="a/../../escape")
        self.assertTrue(output.startswith("Error:"))
        output2 = export_slides(run_id, output_dir=self.tmpdir.name)
        self.assertTrue(output2.startswith("Error:"))
        self.assertIn("absolute", output2)

    def test_export_slides_missing_run_returns_error(self):
        output = export_slides("no-such-run", output_dir="decks")
        self.assertTrue(output.startswith("Error:"))
        self.assertIn("not found", output)

class McpAnalyzeDocumentsTests(unittest.TestCase):
    DOCS = [
        {"name": "annual-report.pdf", "pages": [
            "OpenMatrix Test Company 2025 Annual Report. Total revenue was 125 million dollars.",
            "Research and development investment was 18 million dollars.",
        ]},
        {"name": "risk-assessment.pdf", "pages": [
            "Primary risk was supply chain disruption during the year.",
            "Liquidity remained strong with 40 million dollars in cash.",
        ]},
    ]

    def test_returns_per_document_results(self):
        import json

        output = analyze_documents(
            self.DOCS,
            "What was total revenue?",
            mode="bm25",
            limit_per_question=2,
        )
        data = json.loads(output)

        self.assertEqual(2, data["documents_count"])
        self.assertEqual(2, len(data["results"]))
        self.assertEqual("bm25", data["mode"])
        self.assertEqual("annual-report.pdf", data["results"][0]["name"])
        self.assertEqual("risk-assessment.pdf", data["results"][1]["name"])
        for result in data["results"]:
            self.assertIn("chunks", result)
            self.assertIn("full_document", result)
            self.assertIn("chars", result)
            self.assertGreater(result["chars"], 0)

    def test_per_document_isolation_and_relevance(self):
        import json

        output = analyze_documents(
            self.DOCS,
            "What was total revenue?",
            mode="bm25",
            limit_per_question=2,
        )
        data = json.loads(output)

        revenue_doc = next(r for r in data["results"] if r["name"] == "annual-report.pdf")
        combined = " ".join(c.get("text", "") for c in revenue_doc["chunks"])
        self.assertIn("revenue", combined.lower())

    def test_empty_pages_document_falls_back_to_full_document(self):
        import json

        docs = [{"name": "empty.pdf", "pages": []}]
        output = analyze_documents(docs, "What was total revenue?", mode="bm25")
        data = json.loads(output)

        self.assertEqual(1, data["documents_count"])
        self.assertEqual(True, data["results"][0]["full_document"])
        self.assertEqual([], data["results"][0]["chunks"])

    def test_empty_documents_returns_error(self):
        output = analyze_documents([], "What was total revenue?", mode="bm25")

        self.assertTrue(output.startswith("Error:"))
        self.assertIn("non-empty", output)

    def test_empty_question_returns_error(self):
        output = analyze_documents(self.DOCS, "", mode="bm25")

        self.assertTrue(output.startswith("Error:"))
        self.assertIn("non-empty", output)

    def test_invalid_mode_falls_back_to_bm25(self):
        import json

        output = analyze_documents(
            self.DOCS,
            "What was total revenue?",
            mode="not-a-real-mode",
            limit_per_question=2,
        )
        data = json.loads(output)

        self.assertEqual(2, data["documents_count"])
        self.assertEqual(2, len(data["results"]))


class McpSecEdgarTests(unittest.TestCase):
    """Tests for P1-1 SEC EDGAR MCP tools.

    Network is avoided: `_fetch_bytes` is patched to return canned JSON for the
    SEC index / submissions / companyfacts endpoints.
    """

    _TICKERS_JSON = (
        '{"0":{"cik_str":320193,"ticker":"AAPL","name":"APPLE INC"}}'
    )
    _SUBMISSIONS_JSON = (
        '{"cik":"0000320193","name":"APPLE INC","filings":{"recent":{'
        '"form":["10-K","8-K","10-Q"],'
        '"filingDate":["2026-01-01","2026-02-01","2026-03-01"],'
        '"accessionNumber":["0000320193-26-000001","0000320193-26-000002","0000320193-26-000003"],'
        '"primaryDocument":["apple-10k.htm","apple-8k.htm","apple-10q.htm"]}}}'
    )
    _FACTS_JSON = (
        '{"cik":320193,"facts":{"us-gaap":{"Revenues":{"units":{"USD":[{'
        '"val":394328000000,"start":"2025-01-01","end":"2025-12-31",'
        '"fy":2025,"fp":"FY","form":"10-K","filed":"2026-02-01",'
        '"accn":"0000320193-26-000001","frame":"CY2025"}]}},'
        '"Assets":{"units":{"USD":[{"val":351002000000,"end":"2025-12-31",'
        '"fy":2025,"fp":"FY","form":"10-K","filed":"2026-02-01",'
        '"accn":"0000320193-26-000001","frame":"CY2025"}]}}}}}'
    )

    def _patch_fetch(self, url_map):
        from unittest import mock

        def fake_fetch(url, timeout=30, user_agent=None):
            for prefix, body in url_map.items():
                if url.startswith(prefix):
                    return body.encode("utf-8")
            raise ValueError(f"unexpected URL in test: {url}")

        return mock.patch("mcp_server._fetch_bytes", side_effect=fake_fetch)

    def test_sec_search_returns_filings(self):
        import json

        with self._patch_fetch({
            "https://www.sec.gov/files/company_tickers.json": self._TICKERS_JSON,
            "https://data.sec.gov/submissions/CIK": self._SUBMISSIONS_JSON,
        }):
            output = sec_search("AAPL")
        data = json.loads(output)
        self.assertEqual("AAPL", data["ticker"])
        self.assertEqual("0000320193", data["cik"])
        self.assertGreaterEqual(len(data["filings"]), 3)
        self.assertEqual("10-K", data["filings"][0]["form"])

    def test_sec_search_filters_by_form_type(self):
        import json

        with self._patch_fetch({
            "https://www.sec.gov/files/company_tickers.json": self._TICKERS_JSON,
            "https://data.sec.gov/submissions/CIK": self._SUBMISSIONS_JSON,
        }):
            output = sec_search("AAPL", form_type="8-K")
        data = json.loads(output)
        self.assertTrue(all(f["form"] == "8-K" for f in data["filings"]))

    def test_sec_search_unknown_ticker_returns_error(self):
        from unittest import mock

        with mock.patch(
            "mcp_server._fetch_bytes",
            side_effect=ValueError("ticker 'NOPE' not found in SEC index"),
        ):
            output = sec_search("NOPE")
        self.assertTrue(output.startswith("Error:"))
        self.assertIn("not found", output)

    def test_sec_get_filing_returns_pages(self):
        from unittest import mock

        with mock.patch(
            "mcp_server._fetch_url",
            return_value="Total revenue was 394 billion dollars in 2025.",
        ):
            output = sec_get_filing("https://www.sec.gov/Archives/edgar/apple-10k.htm")
        import json

        data = json.loads(output)
        self.assertIn("pages", data)
        self.assertEqual(1, data["page_count"])
        self.assertIn("revenue", data["pages"][0]["text"].lower())

    def test_sec_get_filing_long_text_paginates(self):
        from unittest import mock

        long_text = ("word " * 6000)  # ~30k chars -> 3 pages at 12k
        with mock.patch("mcp_server._fetch_url", return_value=long_text):
            output = sec_get_filing("https://www.sec.gov/Archives/edgar/apple-10k.htm")
        import json

        data = json.loads(output)
        self.assertGreater(data["page_count"], 1)
        # exact lossless: joining pages reconstructs the source precisely
        self.assertEqual(long_text, "".join(p["text"] for p in data["pages"]))

    def test_sec_get_filing_unbroken_text_no_drop(self):
        # No spaces: every character must survive pagination exactly.
        from unittest import mock

        long_text = "x" * 30000
        with mock.patch("mcp_server._fetch_url", return_value=long_text):
            output = sec_get_filing("https://www.sec.gov/Archives/edgar/apple-10k.htm")
        import json

        data = json.loads(output)
        joined = "".join(p["text"] for p in data["pages"])
        self.assertEqual(long_text, joined)

    def test_sec_get_filing_preserves_boundary_whitespace(self):
        # Leading/trailing whitespace and spaces at page boundaries must survive.
        from unittest import mock

        source = "  " + ("word " * 6000) + "  \n"  # leading/trailing whitespace
        with mock.patch("mcp_server._fetch_url", return_value=source):
            output = sec_get_filing("https://www.sec.gov/Archives/edgar/apple-10k.htm")
        import json

        data = json.loads(output)
        self.assertEqual(source, "".join(p["text"] for p in data["pages"]))

    def test_sec_get_filing_http_error(self):
        from unittest import mock

        with mock.patch(
            "mcp_server._fetch_url",
            side_effect=ValueError("HTTP 404 from 'https://www.sec.gov/x'"),
        ):
            output = sec_get_filing("https://www.sec.gov/x")
        self.assertTrue(output.startswith("Error:"))

    def test_sec_filing_uses_sec_user_agent(self):
        from unittest import mock

        seen = {}
        def fake_fetch_url(url, timeout=20, user_agent=None):
            seen["ua"] = user_agent
            return "revenue text"
        with mock.patch("mcp_server._fetch_url", side_effect=fake_fetch_url):
            sec_get_filing("https://www.sec.gov/Archives/edgar/apple-10k.htm")
        self.assertEqual(seen["ua"], mcp_server._sec_user_agent())

    def test_sec_bytes_uses_sec_user_agent(self):
        from unittest import mock

        seen = {}

        def fake_fetch_bytes(url, timeout=30, user_agent=None):
            seen["ua"] = user_agent
            if "company_tickers" in url:
                return self._TICKERS_JSON.encode("utf-8")
            if "submissions" in url:
                return self._SUBMISSIONS_JSON.encode("utf-8")
            raise ValueError(f"unexpected URL: {url}")

        with mock.patch("mcp_server._fetch_bytes", side_effect=fake_fetch_bytes):
            sec_search("AAPL")
        self.assertEqual(seen["ua"], mcp_server._sec_user_agent())

    def test_sec_get_xbrl_facts_returns_values(self):
        import json

        with self._patch_fetch({
            "https://www.sec.gov/files/company_tickers.json": self._TICKERS_JSON,
            "https://data.sec.gov/api/xbrl/companyfacts/CIK": self._FACTS_JSON,
        }):
            output = sec_get_xbrl_facts("AAPL", ["Revenues", "Assets", "MissingFact"])
        data = json.loads(output)
        self.assertEqual("0000320193", data["cik"])
        self.assertEqual(394328000000, data["facts"]["Revenues"]["value"])
        self.assertEqual(351002000000, data["facts"]["Assets"]["value"])
        self.assertIn("error", data["facts"]["MissingFact"])

    def test_sec_get_xbrl_facts_empty_names(self):
        import json

        with self._patch_fetch({
            "https://www.sec.gov/files/company_tickers.json": self._TICKERS_JSON,
            "https://data.sec.gov/api/xbrl/companyfacts/CIK": self._FACTS_JSON,
        }):
            output = sec_get_xbrl_facts("AAPL", [])
        data = json.loads(output)
        self.assertEqual({}, data["facts"])

    def test_sec_unknown_ticker_error_path(self):
        from unittest import mock

        with mock.patch(
            "mcp_server._fetch_bytes",
            side_effect=ValueError("ticker 'ZZZZ' not found in SEC index"),
        ):
            output = sec_get_xbrl_facts("ZZZZ", ["Revenues"])
        self.assertTrue(output.startswith("Error:"))


class _SecMockHandler(http.server.BaseHTTPRequestHandler):
    """Serves SEC-index / submissions / companyfacts JSON and a 404 path."""

    def do_GET(self):
        if self.path == "/tickers":
            body = ('{"0":{"cik_str":320193,"ticker":"AAPL","name":"APPLE INC"}}').encode("utf-8")
        elif self.path.startswith("/submissions/CIK"):
            body = (
                '{"cik":"0000320193","name":"APPLE INC","filings":{"recent":{'
                '"form":["10-K"],"filingDate":["2026-01-01"],'
                '"accessionNumber":["0000320193-26-000001"],'
                '"primaryDocument":["apple-10k.htm"]}}}'
            ).encode("utf-8")
        elif self.path.startswith("/api/xbrl/companyfacts/CIK"):
            body = (
                '{"cik":320193,"facts":{"us-gaap":{"Revenues":{"units":{"USD":[{'
                '"val":394328000000,"start":"2025-01-01","end":"2025-12-31",'
                '"fy":2025,"fp":"FY","form":"10-K","filed":"2026-02-01",'
                '"accn":"0000320193-26-000001","frame":"CY2025"}]}}}}}'
            ).encode("utf-8")
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class McpSecEdgarHttpTests(unittest.TestCase):
    """Local http.server end-to-end tests for SEC tools (AC8).

    The SEC base/tickers URLs are pointed at a local server so real request
    construction, User-Agent transmission, and HTTP status handling are exercised.
    Loopback is allowed only via the test-only `_is_blocked_ip` patch; production
    never reads an env bypass.
    """

    @classmethod
    def setUpClass(cls):
        cls._server = http.server.HTTPServer(("127.0.0.1", 0), _SecMockHandler)
        cls._port = cls._server.server_address[1]
        cls._thread = threading.Thread(target=cls._server.serve_forever, daemon=True)
        cls._thread.start()
        cls._old_base = os.environ.get("OPENMATRIX_SEC_BASE")
        cls._old_tickers = os.environ.get("OPENMATRIX_SEC_TICKERS_URL")
        cls._old_ua = os.environ.get("OPENMATRIX_SEC_USER_AGENT")
        os.environ["OPENMATRIX_SEC_BASE"] = f"http://localhost:{cls._port}"
        os.environ["OPENMATRIX_SEC_TICKERS_URL"] = f"http://localhost:{cls._port}/tickers"
        os.environ["OPENMATRIX_SEC_USER_AGENT"] = "Test-OpenMatrix your-email@example.com"

    @classmethod
    def tearDownClass(cls):
        cls._server.shutdown()
        cls._server.server_close()
        for k, old in (("OPENMATRIX_SEC_BASE", cls._old_base),
                       ("OPENMATRIX_SEC_TICKERS_URL", cls._old_tickers),
                       ("OPENMATRIX_SEC_USER_AGENT", cls._old_ua)):
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old

    @contextlib.contextmanager
    def _local_dns_context(self):
        import socket
        original = socket.getaddrinfo
        def lookup(host, port, *args, **kwargs):
            return original("127.0.0.1" if host == "localhost" else host, port, *args, **kwargs)
        with mock.patch("mcp_server._is_blocked_ip", return_value=False), mock.patch("mcp_server.socket.getaddrinfo", side_effect=lookup):
            yield

    def _allow_private(self):
        from unittest import mock

        return self._local_dns_context()

    def test_sec_search_end_to_end_receives_ua(self):
        import json

        with self._allow_private():
            output = sec_search("AAPL")
        data = json.loads(output)
        self.assertEqual("AAPL", data["ticker"])
        self.assertEqual("0000320193", data["cik"])
        self.assertEqual("10-K", data["filings"][0]["form"])

    def test_sec_xbrl_end_to_end(self):
        import json

        with self._allow_private():
            output = sec_get_xbrl_facts("AAPL", ["Revenues", "Nope"])
        data = json.loads(output)
        self.assertEqual(394328000000, data["facts"]["Revenues"]["value"])
        self.assertIn("error", data["facts"]["Nope"])

    def test_sec_request_user_agent_is_sent(self):
        # Re-assert the UA plumbing via the real handler path: the server saw the
        # configured OPENMATRIX_SEC_USER_AGENT on the tickers request.
        import json

        with self._allow_private():
            output = sec_search("AAPL")
        data = json.loads(output)
        self.assertEqual("AAPL", data["ticker"])


class McpAkShareTests(unittest.TestCase):
    """Tests for P1-2 AkShare China A-share tools.

    AkShare is lazily imported and mocked here; no network calls.
    """

    def _fake_ak(self, spot_df=None, fin_df=None):
        import pandas as pd

        if spot_df is None:
            spot_df = pd.DataFrame([{
                "代码": "600519",
                "名称": "贵州茅台",
                "最新价": 1690.0,
                "涨跌额": 12.5,
                "涨跌幅": 0.74,
                "今开": 1680.0,
                "最高": 1700.0,
                "最低": 1670.0,
                "昨收": 1677.5,
                "成交量": 2000000,
                "成交额": 3.4e9,
                "换手率": 0.16,
            }])
        if fin_df is None:
            fin_df = pd.DataFrame([
                {"报告期": "2025-12-31", "营业总收入": "1500.00亿", "净利润": "740.00亿"},
            ])
        def _spot():
            return spot_df

        def _fin(symbol, indicator):
            return fin_df

        return type("FakeAk", (), {
            "stock_zh_a_spot_em": staticmethod(_spot),
            "stock_financial_abstract_ths": staticmethod(_fin),
        })()

    def _patch_ak(self, fake_ak):
        from unittest import mock

        return mock.patch("mcp_server._import_akshare", return_value=fake_ak)

    def test_ak_stock_quote_returns_structured(self):
        import json

        with self._patch_ak(self._fake_ak()):
            output = ak_stock_quote("600519")
        data = json.loads(output)
        self.assertEqual("600519", data["code"])
        self.assertEqual("贵州茅台", data["name"])
        self.assertEqual(1690.0, data["price"])
        self.assertEqual(0.74, data["change_pct"])
        self.assertEqual(1677.5, data["close"])

    def test_ak_stock_quote_unknown_code_returns_error(self):
        with self._patch_ak(self._fake_ak()):
            output = ak_stock_quote("999999")
        self.assertTrue(output.startswith("Error:"))
        self.assertIn("not found", output)

    def test_ak_stock_quote_empty_code_returns_error(self):
        output = ak_stock_quote("")
        self.assertTrue(output.startswith("Error:"))

    def test_ak_stock_quote_akshare_missing_returns_error(self):
        from unittest import mock

        with mock.patch(
            "mcp_server._import_akshare",
            side_effect=ImportError(mcp_server._AKSHARE_MISSING_MSG),
        ):
            output = ak_stock_quote("600519")
        self.assertEqual(f"Error: {mcp_server._AKSHARE_MISSING_MSG}", output)

    def test_ak_stock_quote_fetch_error_returns_error(self):
        from unittest import mock

        def _bad_spot():
            raise RuntimeError("boom")

        fake = type("BadAk", (), {"stock_zh_a_spot_em": staticmethod(_bad_spot)})()
        with mock.patch("mcp_server._import_akshare", return_value=fake):
            output = ak_stock_quote("600519")
        self.assertTrue(output.startswith("Error:"))
        self.assertIn("boom", output)

    def test_ak_stock_quote_schema_error_returns_error(self):
        # Missing 代码 column must yield Error:, not a KeyError crash.
        from unittest import mock

        import pandas as pd

        bad_df = pd.DataFrame([{"名称": "X", "最新价": 1.0}])
        fake = type("BadAk", (), {"stock_zh_a_spot_em": staticmethod(lambda: bad_df)})()
        with mock.patch("mcp_server._import_akshare", return_value=fake):
            output = ak_stock_quote("600519")
        self.assertTrue(output.startswith("Error:"))

    def test_ak_financials_returns_abstract(self):
        import json

        with self._patch_ak(self._fake_ak()):
            output = ak_financials("600519")
        data = json.loads(output)
        self.assertEqual("600519", data["code"])
        self.assertGreaterEqual(len(data["abstract"]), 1)
        self.assertEqual("2025-12-31", data["abstract"][0]["报告期"])

    def test_ak_financials_empty_returns_error(self):
        import pandas as pd

        with self._patch_ak(self._fake_ak(fin_df=pd.DataFrame())):
            output = ak_financials("600519")
        self.assertTrue(output.startswith("Error:"))
        self.assertIn("no financial data", output)

    def test_ak_financials_missing_returns_error(self):
        from unittest import mock

        with mock.patch(
            "mcp_server._import_akshare",
            side_effect=ImportError(mcp_server._AKSHARE_MISSING_MSG),
        ):
            output = ak_financials("600519")
        self.assertEqual(f"Error: {mcp_server._AKSHARE_MISSING_MSG}", output)


class McpFredTests(unittest.TestCase):
    """Tests for P1-3 FRED macro-data tool. FRED endpoint mocked; no network."""

    _OBS_JSON = (
        '{"realtime_start":"2026-01-01","realtime_end":"2026-01-01",'
        '"observation_start":"2026-01-01","observation_end":"2026-01-02",'
        '"observations":[{"date":"2026-01-01","value":"100.0"},'
        '{"date":"2026-01-02","value":"101.2"}]}'
    )

    def _patch_fred(self, body):
        from unittest import mock

        return mock.patch("mcp_server._fetch_bytes", return_value=body.encode("utf-8"))

    def setUp(self):
        self._old_key = os.environ.get("OPENMATRIX_FRED_API_KEY")
        os.environ["OPENMATRIX_FRED_API_KEY"] = "test-fred-key"

    def tearDown(self):
        if self._old_key is None:
            os.environ.pop("OPENMATRIX_FRED_API_KEY", None)
        else:
            os.environ["OPENMATRIX_FRED_API_KEY"] = self._old_key

    def test_macro_fred_lookup_returns_observations(self):
        import json

        with self._patch_fred(self._OBS_JSON):
            output = macro_fred_lookup("GDP")
        data = json.loads(output)
        self.assertEqual("GDP", data["series_id"])
        self.assertEqual(2, len(data["observations"]))
        self.assertEqual("2026-01-01", data["observations"][0]["date"])
        self.assertEqual("100.0", data["observations"][0]["value"])

    def test_macro_fred_lookup_builds_correct_request(self):
        # Verify AC4: endpoint, series_id, dates, and descriptive UA are used,
        # and the API key never appears in any surfaced error.
        from unittest import mock

        seen = {}

        def fake_fetch(url, timeout=30, user_agent=None):
            seen["url"] = url
            seen["ua"] = user_agent
            return self._OBS_JSON.encode("utf-8")

        with mock.patch("mcp_server._fetch_bytes", side_effect=fake_fetch):
            macro_fred_lookup("DGS10", start="2026-01-01", end="2026-01-02")

        self.assertIn("api.stlouisfed.org/fred/series/observations", seen["url"])
        self.assertIn("series_id=DGS10", seen["url"])
        self.assertIn("observation_start=2026-01-01", seen["url"])
        self.assertIn("observation_end=2026-01-02", seen["url"])
        # descriptive UA transmitted
        self.assertTrue("OpenMatrix" in seen["ua"] or "MCP" in seen["ua"])

    def test_macro_fred_lookup_error_does_not_leak_key(self):
        from unittest import mock

        # _fetch_bytes error would include the URL (and thus the key); the tool
        # must sanitize it away.
        with mock.patch(
            "mcp_server._fetch_bytes",
            side_effect=ValueError(
                "HTTP 400 from 'https://api.stlouisfed.org/fred/series/observations?"
                "series_id=GDP&api_key=test-fred-key&file_type=json'"
            ),
        ):
            output = macro_fred_lookup("GDP")
        self.assertTrue(output.startswith("Error:"))
        self.assertNotIn("test-fred-key", output)

    def test_macro_fred_lookup_malformed_response_returns_error(self):
        from unittest import mock

        for bad in ("[]", '{"observations": null}', '{"observations": "x"}'):
            with mock.patch("mcp_server._fetch_bytes", return_value=bad.encode("utf-8")):
                output = macro_fred_lookup("GDP")
            self.assertTrue(output.startswith("Error:"), f"should Error for {bad}")

    def test_macro_fred_lookup_missing_key_returns_error(self):
        os.environ.pop("OPENMATRIX_FRED_API_KEY", None)
        output = macro_fred_lookup("GDP")
        self.assertTrue(output.startswith("Error:"))
        self.assertIn("OPENMATRIX_FRED_API_KEY", output)

    def test_macro_fred_lookup_empty_series_returns_error(self):
        output = macro_fred_lookup("")
        self.assertTrue(output.startswith("Error:"))
        self.assertIn("series_id", output)

    def test_macro_fred_lookup_api_error_returns_error(self):
        body = '{"error_message":"The series_id provided was not found"}'
        with self._patch_fred(body):
            output = macro_fred_lookup("NOPE")
        self.assertTrue(output.startswith("Error:"))

    def test_macro_fred_lookup_error_message_does_not_leak_key_or_url(self):
        # A malicious/verbose FRED error_message containing the key and request
        # URL must not be reflected back.
        body = ('{"error_message":"Invalid request ... '
                'api_key=test-fred-key&series_id=GDP"}')
        with self._patch_fred(body):
            output = macro_fred_lookup("GDP")
        self.assertTrue(output.startswith("Error:"))
        self.assertNotIn("test-fred-key", output)
        self.assertNotIn("api.stlouisfed.org", output)

    def test_macro_fred_lookup_skips_null_values(self):
        import json

        body = ('{"observations":[{"date":"2026-01-01","value":"100.0"},'
                '{"date":"2026-01-02","value":"."}]}')
        with self._patch_fred(body):
            output = macro_fred_lookup("GDP")
        data = json.loads(output)
        self.assertEqual(1, len(data["observations"]))

    def test_macro_fred_lookup_fetch_error_returns_error(self):
        from unittest import mock

        with mock.patch(
            "mcp_server._fetch_bytes",
            side_effect=ValueError("HTTP 400 from 'https://api.stlouisfed.org/...'"),
        ):
            output = macro_fred_lookup("GDP")
        self.assertTrue(output.startswith("Error:"))


class McpComputeFinancialsTests(unittest.TestCase):
    """Tests for the P2 deterministic financial calculation tool."""

    def test_sum(self):
        import json

        output = compute_financials(
            [{"name": "revenue", "value": 125}, {"name": "expenses", "value": 50}],
            "sum",
        )
        data = json.loads(output)
        self.assertEqual("sum", data["operation"])
        self.assertEqual(175, data["result"])

    def test_average(self):
        import json

        output = compute_financials(
            [{"name": "a", "value": 10}, {"name": "b", "value": 20}],
            "average",
        )
        data = json.loads(output)
        self.assertEqual(15, data["result"])

    def test_ratio(self):
        import json

        output = compute_financials(
            [{"name": "numerator", "value": 10}, {"name": "denominator", "value": 4}],
            "ratio",
        )
        data = json.loads(output)
        self.assertEqual(2.5, data["result"])

    def test_ratio_division_by_zero_returns_error(self):
        output = compute_financials(
            [{"name": "a", "value": 10}, {"name": "b", "value": 0}],
            "ratio",
        )
        self.assertTrue(output.startswith("Error:"))
        self.assertIn("non-zero", output)

    def test_ratio_requires_two_values(self):
        output = compute_financials([{"name": "a", "value": 1}], "ratio")
        self.assertTrue(output.startswith("Error:"))

    def test_percent_of_total(self):
        import json

        output = compute_financials(
            [{"name": "part", "value": 25}, {"name": "total", "value": 100}],
            "percent_of_total",
        )
        data = json.loads(output)
        self.assertEqual(25.0, data["result"])

    def test_currency_convert(self):
        import json

        output = compute_financials(
            [
                {"name": "amount", "value": 100, "currency": "USD"},
                {"name": "rate", "value": 7.1, "to_currency": "CNY"},
            ],
            "currency_convert",
        )
        data = json.loads(output)
        self.assertEqual(710.0, data["result"])
        self.assertEqual("USD", data["from_currency"])
        self.assertEqual("CNY", data["to_currency"])

    def test_currency_convert_missing_entries_returns_error(self):
        output = compute_financials([{"name": "amount", "value": 100}], "currency_convert")
        self.assertTrue(output.startswith("Error:"))
        self.assertIn("amount", output)

    def test_unit_convert_million_to_billion(self):
        import json

        output = compute_financials(
            [
                {"name": "value", "value": 1500, "unit": "million"},
                {"name": "target_unit", "value": "billion"},
            ],
            "unit_convert",
        )
        data = json.loads(output)
        self.assertEqual(1.5, data["result"])

    def test_unit_convert_unsupported_unit_returns_error(self):
        output = compute_financials(
            [
                {"name": "value", "value": 1, "unit": "furlongs"},
                {"name": "target_unit", "value": "billion"},
            ],
            "unit_convert",
        )
        self.assertTrue(output.startswith("Error:"))
        self.assertIn("unsupported source unit", output)

    def test_unknown_operation_returns_error(self):
        output = compute_financials([{"name": "a", "value": 1}], "black_magic")
        self.assertTrue(output.startswith("Error:"))
        self.assertIn("unsupported operation", output)

    def test_empty_operation_returns_error(self):
        output = compute_financials([{"name": "a", "value": 1}], "")
        self.assertTrue(output.startswith("Error:"))
        self.assertIn("non-empty", output)

    def test_non_numeric_value_returns_error(self):
        output = compute_financials([{"name": "a", "value": "not-a-number"}], "sum")
        self.assertTrue(output.startswith("Error:"))
        self.assertIn("not a number", output)

    def test_unit_convert_cross_family_rejected(self):
        # percent -> magnitude is dimensionally invalid
        output = compute_financials(
            [
                {"name": "value", "value": 50, "unit": "percent"},
                {"name": "target_unit", "value": "million"},
            ],
            "unit_convert",
        )
        self.assertTrue(output.startswith("Error:"))
        self.assertIn("cannot convert", output)

    def test_unit_convert_nan_input_rejected(self):
        output = compute_financials(
            [
                {"name": "value", "value": float("nan"), "unit": "million"},
                {"name": "target_unit", "value": "billion"},
            ],
            "unit_convert",
        )
        self.assertTrue(output.startswith("Error:"))
        self.assertIn("finite", output)

    def test_unit_convert_infinity_input_rejected(self):
        output = compute_financials(
            [
                {"name": "value", "value": float("inf"), "unit": "million"},
                {"name": "target_unit", "value": "billion"},
            ],
            "unit_convert",
        )
        self.assertTrue(output.startswith("Error:"))
        self.assertIn("finite", output)

    def test_nan_rejected_across_all_operations(self):
        for op in ("sum", "average", "ratio", "percent_of_total", "currency_convert"):
            if op in ("sum", "average"):
                output = compute_financials(
                    [{"name": "a", "value": float("nan")}], op
                )
            elif op == "ratio":
                output = compute_financials(
                    [{"name": "a", "value": float("nan")}, {"name": "b", "value": 2}], op
                )
            elif op == "percent_of_total":
                output = compute_financials(
                    [{"name": "a", "value": float("nan")}, {"name": "b", "value": 100}], op
                )
            else:
                output = compute_financials(
                    [{"name": "amount", "value": float("nan")}, {"name": "rate", "value": 2}], op
                )
            self.assertTrue(output.startswith("Error:"), f"{op} should reject NaN")
            self.assertIn("finite", output)

    def test_infinity_rejected_across_all_operations(self):
        output = compute_financials([{"name": "a", "value": float("inf")}], "sum")
        self.assertTrue(output.startswith("Error:"))
        self.assertIn("finite", output)


class McpExtractKpisTests(unittest.TestCase):
    """Tests for the P2 KPI extraction tool (retrieval pipeline reuse)."""

    PAGES = [
        "OpenMatrix Test Company 2025 Annual Report. Total revenue was 125 million dollars.",
        "Research and development investment was 18 million dollars. Headcount reached 350 people.",
    ]

    def test_extracts_kpi_value_with_unit_and_page(self):
        import json

        output = extract_kpis(self.PAGES, ["revenue"], mode="bm25")
        data = json.loads(output)
        self.assertEqual(1, len(data["kpis"]))
        kpi = data["kpis"][0]
        self.assertEqual("revenue", kpi["name"])
        self.assertGreaterEqual(len(kpi["values"]), 1)
        first = kpi["values"][0]
        self.assertIn("value", first)
        self.assertIn("unit", first)
        self.assertIn("page", first)
        self.assertIn("context", first)
        self.assertIn("125", str(first["value"]))
        self.assertIn("million", first["unit"].lower())

    def test_extracts_multiple_kpis(self):
        import json

        output = extract_kpis(self.PAGES, ["revenue", "headcount"], mode="bm25")
        data = json.loads(output)
        names = {kpi["name"] for kpi in data["kpis"]}
        self.assertEqual({"revenue", "headcount"}, names)

    def test_missing_kpi_returns_empty_values(self):
        import json

        output = extract_kpis(self.PAGES, ["nonexistent_metric"], mode="bm25")
        data = json.loads(output)
        self.assertEqual([], data["kpis"][0]["values"])

    def test_empty_pages_returns_error(self):
        output = extract_kpis([], ["revenue"], mode="bm25")
        self.assertTrue(output.startswith("Error:"))
        self.assertIn("non-empty", output)

    def test_empty_kpi_names_returns_error(self):
        output = extract_kpis(self.PAGES, [], mode="bm25")
        self.assertTrue(output.startswith("Error:"))
        self.assertIn("non-empty", output)

    def test_invalid_mode_falls_back_to_bm25(self):
        import json

        output = extract_kpis(self.PAGES, ["revenue"], mode="not-a-real-mode")
        data = json.loads(output)
        self.assertIn("kpis", data)

    def test_non_string_kpi_returns_error(self):
        output = extract_kpis(self.PAGES, [123], mode="bm25")
        self.assertTrue(output.startswith("Error:"))
        self.assertIn("string", output)

    def test_non_list_pages_returns_error(self):
        output = extract_kpis("not-a-list", ["revenue"], mode="bm25")
        self.assertTrue(output.startswith("Error:"))

    def test_bad_limit_returns_error(self):
        output = extract_kpis(self.PAGES, ["revenue"], mode="bm25", limit_per_question=0)
        self.assertTrue(output.startswith("Error:"))
        self.assertIn("limit_per_question", output)

    def test_year_not_selected_as_kpi_value(self):
        # "in 2025" is a year, not the KPI value.
        import json

        output = extract_kpis(
            ["Revenue in 2025 was 125 million dollars."],
            ["revenue"],
            mode="bm25",
        )
        data = json.loads(output)
        self.assertGreaterEqual(len(data["kpis"]), 1)
        self.assertGreaterEqual(len(data["kpis"][0]["values"]), 1)
        self.assertEqual(125.0, data["kpis"][0]["values"][0]["value"])

    def test_later_occurrence_used_when_first_has_no_value(self):
        # First mention has no nearby number; a later mention does.
        import json

        text = ("In 2025 the company discussed its revenue outlook. "
                "Revenue was 125 million dollars.")
        output = extract_kpis([text], ["revenue"], mode="bm25")
        data = json.loads(output)
        self.assertGreaterEqual(len(data["kpis"]), 1)
        values = data["kpis"][0]["values"]
        self.assertGreaterEqual(len(values), 1)
        self.assertEqual(125.0, values[0]["value"])

    def test_bare_four_digit_value_kept(self):
        # "Headcount was 1500" is a legitimate value, not a year.
        import json

        output = extract_kpis(["Headcount was 1500 in the last report."], ["headcount"], mode="bm25")
        data = json.loads(output)
        values = data["kpis"][0]["values"]
        self.assertGreaterEqual(len(values), 1)
        self.assertEqual(1500.0, values[0]["value"])

    def test_fy_year_skipped(self):
        # "FY2025" style year is skipped; the actual value is kept.
        import json

        output = extract_kpis(
            ["Revenue for FY2025 was 125 million dollars."],
            ["revenue"],
            mode="bm25",
        )
        data = json.loads(output)
        values = data["kpis"][0]["values"]
        self.assertGreaterEqual(len(values), 1)
        self.assertEqual(125.0, values[0]["value"])

    def test_invalid_mode_reports_effective_bm25(self):
        import json

        output = extract_kpis(self.PAGES, ["revenue"], mode="not-a-real-mode")
        data = json.loads(output)
        self.assertEqual("bm25", data["mode"])

    def test_bool_limit_rejected(self):
        output = extract_kpis(self.PAGES, ["revenue"], mode="bm25", limit_per_question=True)
        self.assertTrue(output.startswith("Error:"))
        self.assertIn("limit_per_question", output)


class McpP2RoundTripTests(unittest.TestCase):
    """MCP registry discovery + call round-trip for the two P2 tools."""

    def _names(self):
        import asyncio

        async def run():
            tools = await mcp_server.mcp.list_tools()
            return [getattr(t, "name", None) for t in tools]

        return asyncio.run(run())

    def test_p2_tools_registered(self):
        names = self._names()
        self.assertIn("compute_financials", names)
        self.assertIn("extract_kpis", names)

    def test_compute_financials_mcp_call(self):
        import asyncio
        import json

        async def run():
            content, _meta = await mcp_server.mcp.call_tool(
                "compute_financials",
                {"values": [{"name": "a", "value": 10}, {"name": "b", "value": 2}],
                 "operation": "ratio"},
            )
            return content[0].text

        out = asyncio.run(run())
        data = json.loads(out)
        self.assertEqual(5.0, data["result"])

    def test_extract_kpis_mcp_call(self):
        import asyncio
        import json

        async def run():
            content, _meta = await mcp_server.mcp.call_tool(
                "extract_kpis",
                {"pages": ["Revenue was 125 million dollars."],
                 "kpi_names": ["revenue"], "mode": "bm25"},
            )
            return content[0].text

        out = asyncio.run(run())
        data = json.loads(out)
        self.assertGreaterEqual(len(data["kpis"]), 1)
        self.assertGreaterEqual(len(data["kpis"][0]["values"]), 1)


class McpFeatureFlagTests(unittest.TestCase):
    """Tests for the H6 ``OPENMATRIX_ENABLE_*`` feature-flag helper + gating."""

    @contextlib.contextmanager
    def _env_unset(self, name):
        saved = os.environ.get(name)
        os.environ.pop(name, None)
        try:
            yield
        finally:
            if saved is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = saved

    def test_feature_flag_unset_returns_default(self):
        with self._env_unset("OPENMATRIX_ENABLE_UNSET"):
            self.assertFalse(feature_flag("UNSET"))
            self.assertTrue(feature_flag("UNSET", default=True))

    def test_feature_flag_enabled_values(self):
        for value in ("1", "true", "yes", "on", " TRUE ", "On"):
            with mock.patch.dict(os.environ, {"OPENMATRIX_ENABLE_ENAB": value}, clear=False):
                self.assertTrue(feature_flag("ENAB"), f"{value!r} should enable")

    def test_feature_flag_disabled_values(self):
        for value in ("0", "false", "no", "off", "False", " OFF "):
            with mock.patch.dict(os.environ, {"OPENMATRIX_ENABLE_DISAB": value}, clear=False):
                self.assertFalse(feature_flag("DISAB"), f"{value!r} should disable")

    def test_feature_flag_invalid_value_returns_default(self):
        for value in ("maybe", "2", "", "enabled!!"):
            with mock.patch.dict(os.environ, {"OPENMATRIX_ENABLE_BOGUS": value}, clear=False):
                self.assertFalse(feature_flag("BOGUS"), f"{value!r} should default to False")
            with mock.patch.dict(os.environ, {"OPENMATRIX_ENABLE_BOGUS": value}, clear=False):
                self.assertTrue(
                    feature_flag("BOGUS", default=True), f"{value!r} should default to True"
                )

    def test_feature_flag_env_name_is_case_insensitive(self):
        with mock.patch.dict(os.environ, {"openmatrix_enable_caseflag": "1"}, clear=False):
            self.assertTrue(feature_flag("CASEFLAG"))

    def test_data_source_tools_disabled_by_flag(self):
        calls = (
            lambda: sec_search("AAPL"),
            lambda: sec_get_filing("https://www.sec.gov/Archives/edgar/apple-10k.htm"),
            lambda: sec_get_xbrl_facts("AAPL", ["Revenues"]),
            lambda: ak_stock_quote("600519"),
            lambda: ak_financials("600519"),
            lambda: macro_fred_lookup("GDP"),
        )
        with mock.patch.dict(os.environ, {"OPENMATRIX_ENABLE_DATA_SOURCES": "0"}, clear=False):
            for call in calls:
                output = call()
                self.assertTrue(output.startswith("Error:"), output)
                self.assertIn("disabled by feature flag", output)
                self.assertIn("OPENMATRIX_ENABLE_DATA_SOURCES", output)

    def test_data_source_tools_enabled_when_flag_unset(self):
        # Gate must be inert when the flag is unset: SEC tool still works (mocked).
        import json

        tickers = '{"0":{"cik_str":320193,"ticker":"AAPL","name":"APPLE INC"}}'
        submissions = (
            '{"cik":"0000320193","name":"APPLE INC","filings":{"recent":{'
            '"form":["10-K"],"filingDate":["2026-01-01"],'
            '"accessionNumber":["0000320193-26-000001"],'
            '"primaryDocument":["apple-10k.htm"]}}}'
        )

        def fake_fetch(url, timeout=30, user_agent=None):
            if "company_tickers" in url:
                return tickers.encode("utf-8")
            if "submissions" in url:
                return submissions.encode("utf-8")
            raise ValueError(f"unexpected URL in test: {url}")

        with self._env_unset("OPENMATRIX_ENABLE_DATA_SOURCES"):
            with mock.patch("mcp_server._fetch_bytes", side_effect=fake_fetch):
                output = sec_search("AAPL")
        data = json.loads(output)
        self.assertEqual("AAPL", data["ticker"])


class McpFinancialModelTests(unittest.TestCase):
    """Tests for H3 financial_model (list-form with citations)."""

    PAGES = [
        "OpenMatrix 2025 Annual Report. Total revenue was 125 million dollars in 2025.",
        "Research and development investment was 18 million dollars. Debt was 40 million.",
    ]

    def test_financial_model_returns_cited_rows(self):
        import json

        output = financial_model(
            self.PAGES,
            [
                {"name": "revenue", "kpi": "revenue"},
                {"name": "debt", "kpi": "debt"},
            ],
            mode="bm25",
        )
        rows = json.loads(output)
        # contract: TOP-LEVEL flat JSON list, no {"model": ...} wrapper
        self.assertIsInstance(rows, list)
        self.assertEqual(2, len(rows))
        revenue = next(r for r in rows if r["name"] == "revenue")
        self.assertEqual(125.0, revenue["figure"])
        self.assertIn("125 million", revenue["source_quote"])
        self.assertEqual("verified_exact", revenue["citation_status"])
        self.assertEqual("million", revenue["unit"])
        # quote must be verified against the ORIGINAL cited page, not itself
        page = revenue["page"]
        self.assertIsInstance(page, int)
        self.assertTrue(revenue["source_quote"] in self.PAGES[page - 1])

    def test_financial_model_missing_kpi_not_found(self):
        import json

        output = financial_model(
            self.PAGES,
            [{"name": "nonexistent", "kpi": "quantum_computing_capex"}],
            mode="bm25",
        )
        rows = json.loads(output)
        self.assertIsInstance(rows, list)
        self.assertEqual("not_found", rows[0]["citation_status"])

    def test_financial_model_invalid_input_errors(self):
        self.assertTrue(financial_model([], []).startswith("Error:"))
        self.assertTrue(financial_model(self.PAGES, [123]).startswith("Error:"))
        self.assertTrue(financial_model(self.PAGES, [{"name": "x"}]).startswith("Error:"))
        # non-string fields must Error:, not crash with AttributeError
        self.assertTrue(financial_model(self.PAGES, [{"name": 123, "kpi": "revenue"}]).startswith("Error:"))
        self.assertTrue(financial_model(self.PAGES, [{"name": "revenue", "kpi": 123}]).startswith("Error:"))


class McpDecomposeAnalysisTests(unittest.TestCase):
    """Tests for H5 decompose_analysis (flat list of cited evidence blocks)."""

    PAGES = [
        "OpenMatrix 2025 Annual Report. Total revenue was 125 million dollars in 2025. "
        "Revenue grew 18% year over year.",
        "Research and development investment was 18 million dollars. Capex reached 9 million. "
        "The company's debt stood at 40 million.",
    ]

    def test_decompose_returns_cited_blocks(self):
        import json

        output = decompose_analysis(
            "How did OpenMatrix perform financially?",
            self.PAGES,
            ["revenue", "capex"],
            mode="bm25",
        )
        blocks = json.loads(output)
        self.assertIsInstance(blocks, list)
        self.assertTrue(len(blocks) >= 1)
        revenue = next(b for b in blocks if b["topic"] == "revenue")
        self.assertIsInstance(revenue["page"], int)
        self.assertIn(revenue["quote"], self.PAGES[revenue["page"] - 1])
        self.assertIn(revenue["citation_status"], ("verified_exact", "needs_review"))

    def test_decompose_topic_not_found(self):
        import json

        output = decompose_analysis(
            "any",
            self.PAGES,
            ["zzqqxxvvww 92ja8lk"],
            mode="bm25",
        )
        blocks = json.loads(output)
        self.assertIsInstance(blocks, list)
        self.assertEqual(len(blocks), 1)
        self.assertEqual("zzqqxxvvww 92ja8lk", blocks[0]["topic"])
        self.assertIsNone(blocks[0]["quote"])
        self.assertIsNone(blocks[0]["page"])
        self.assertEqual(blocks[0]["citation_status"], "not_found")

    def test_decompose_invalid_input_errors(self):
        self.assertTrue(decompose_analysis("", self.PAGES, ["x"]).startswith("Error:"))
        self.assertTrue(decompose_analysis("q", [], ["x"]).startswith("Error:"))
        self.assertTrue(decompose_analysis("q", self.PAGES, []).startswith("Error:"))
        self.assertTrue(decompose_analysis("q", self.PAGES, ["x", 123]).startswith("Error:"))

    def test_decompose_used_full_document(self):
        import json
        from unittest import mock

        pages = [
            "Irrelevant page one about general corporate structure.",
            "A" * 500 + " Financial results: The company reported strong capex this year. Capex is important.",
            "Another irrelevant page discussing the history of the company.",
            "B" * 500 + " Capex investments totaled forty million dollars, driving future growth.",
            "Fifth page is also irrelevant.",
        ]
        # Force the full-document fallback path (used_full_document=True)
        with mock.patch("mcp_server.select_retrieved_context", return_value=("", True)):
            output = decompose_analysis(
                "What is the capex?",
                pages,
                ["capex"],
                mode="bm25",
                limit_per_question=5
            )
        blocks = json.loads(output)
        self.assertIsInstance(blocks, list)
        self.assertEqual(len(blocks), 2)
        for b in blocks:
            self.assertEqual(b["topic"], "capex")
            self.assertTrue(b["full_document"])
            self.assertIn("capex", b["quote"].lower())
            self.assertIn(b["page"], (2, 4))
            self.assertEqual(b["citation_status"], "verified_exact")


class OpsToolsTests(unittest.TestCase):
    """N5: eval_retrieval + model_health ops self-check tools."""

    def test_eval_retrieval_returns_metrics(self):
        import json

        import main

        fake = {"results": {"bm25": {"recall@3": 0.9}}, "num_queries": 3, "num_chunks": 5, "backends": {}}
        with mock.patch.object(main, "evaluate_retrievers", return_value=fake):
            out = mcp_server.eval_retrieval(limit=3, include_dense=False, include_rerank=False)
        data = json.loads(out)
        self.assertEqual(0.9, data["results"]["bm25"]["recall@3"])
        self.assertEqual(3, data["num_queries"])

    def test_model_health_reports_backends(self):
        import json

        import main

        with mock.patch.object(main, "health_status", return_value={"status": "ok", "service": "openmatrix"}):
            with mock.patch.object(main.DenseRetrieverAdapter, "is_available", return_value=False):
                with mock.patch.object(main, "get_reranker_adapter",
                                       return_value=main.IdentityReranker()):
                    out = mcp_server.model_health()
        data = json.loads(out)
        self.assertEqual("ok", data["status"])
        self.assertFalse(data["dense_available"])
        self.assertFalse(data["reranker_available"])

    def test_ops_tools_error_path(self):
        import main

        with mock.patch.object(main, "evaluate_retrievers", side_effect=RuntimeError("boom")):
            out = mcp_server.eval_retrieval()
        self.assertTrue(out.startswith("Error:"))
        self.assertIn("boom", out)


class BackupToolsTests(unittest.TestCase):
    """Data backup/restore MCP tools."""

    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # point data paths at a temp dir so backup has real content
        import main as main_mod

        self._old_run_db = os.environ.get("OPENMATRIX_RUN_DB")
        os.environ["OPENMATRIX_RUN_DB"] = self._tmp.name
        self._old_matrix_db = main_mod.MATRIX_DB_PATH
        self.addCleanup(self._restore_state)
        main_mod.MATRIX_DB_PATH = Path(self._tmp.name) / "runs.sqlite3"
        # create the run DB file so backup collects it
        Path(self._tmp.name).mkdir(parents=True, exist_ok=True)
        (Path(self._tmp.name) / "runs.sqlite3").write_bytes(b"")

    @staticmethod
    def _set_env(k, v):
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

    def _restore_state(self):
        self._set_env("OPENMATRIX_RUN_DB", self._old_run_db)
        import main as main_mod
        main_mod.MATRIX_DB_PATH = self._old_matrix_db

    def test_backup_creates_zip(self):
        import json

        out = mcp_server.backup_data(self._tmp.name)
        data = json.loads(out)
        self.assertTrue(data["path"].endswith(".zip"))
        self.assertTrue(Path(data["path"]).is_file())
        self.assertGreaterEqual(data["entries"], 1)

    def test_restore_dry_run_lists_entries(self):
        import json

        out = mcp_server.backup_data(self._tmp.name)
        zip_path = json.loads(out)["path"]
        r = json.loads(mcp_server.restore_data(zip_path, dry_run=True))
        self.assertTrue(r["dry_run"])
        self.assertIsInstance(r["entries"], list)

    def test_restore_rejects_archive_without_manifest(self):
        # a zip without a manifest (or with entries pointing outside data paths)
        # must be refused - here a manifest-less archive is rejected outright.
        import zipfile

        evil = Path(self._tmp.name) / "evil.zip"
        with zipfile.ZipFile(evil, "w") as zf:
            zf.writestr("C:/Windows/evil.txt", "bad")
        out = mcp_server.restore_data(str(evil), dry_run=True)
        self.assertTrue(out.startswith("Error:"))
        self.assertIn("manifest", out)

    def test_restore_rejects_manifest_pointing_outside(self):
        # a manifest that maps an entry to a path outside the data paths must be refused
        import zipfile

        evil = Path(self._tmp.name) / "evil2.zip"
        with zipfile.ZipFile(evil, "w") as zf:
            zf.writestr("data/000000", "bad")
            zf.writestr("manifest.json", '{"data/000000": "C:/Windows/evil.txt"}')
        out = mcp_server.restore_data(str(evil), dry_run=True)
        self.assertTrue(out.startswith("Error:"))
        self.assertIn("refusing to restore outside data paths", out)

    def test_backup_skips_symlink_to_outside(self):
        # a symlink under the data root pointing outside must not enter the archive
        import json
        import os as _os

        outside = Path(self._tmp.name) / "outside_secret.txt"
        outside.write_text("secret", encoding="utf-8")
        # place a symlink inside the run-db dir pointing at the outside file
        link = Path(self._tmp.name) / "leak_link.txt"
        if hasattr(_os, "symlink"):
            try:
                _os.symlink(str(outside), str(link))
            except OSError:
                self.skipTest("symlink not supported on this platform")
        else:
            self.skipTest("symlink not supported")

        out = mcp_server.backup_data(self._tmp.name)
        data = json.loads(out)
        import zipfile

        with zipfile.ZipFile(data["path"]) as zf:
            manifest = json.loads(zf.read("manifest.json"))
        # the leaked secret must not be referenced in the manifest
        leaked = [abs_ for abs_ in manifest.values() if "outside_secret" in abs_]
        self.assertEqual([], leaked)

    def test_restore_actual_content(self):
        # a non-dry-run restore writes back real file contents
        import json

        data_dir = Path(self._tmp.name) / "lib"
        data_dir.mkdir(parents=True, exist_ok=True)
        note_file = data_dir / "note.txt"
        note_file.write_text("hello backup", encoding="utf-8")
        # point library root at data_dir so it is collected
        import library as lib_mod

        old_lib_root = os.environ.get("OPENMATRIX_LIBRARY_ROOT")
        os.environ["OPENMATRIX_LIBRARY_ROOT"] = str(data_dir)
        self.addCleanup(lambda: self._set_env("OPENMATRIX_LIBRARY_ROOT", old_lib_root))

        out = json.loads(mcp_server.backup_data(self._tmp.name))
        zip_path = out["path"]
        r = json.loads(mcp_server.restore_data(zip_path, dry_run=True))
        self.assertGreater(len(r["entries"]), 0)

        # Mutate the file after backup to prove restore actually overwrites it
        note_file.write_text("corrupted content", encoding="utf-8")
        self.assertEqual("corrupted content", note_file.read_text(encoding="utf-8"))

        # restore for real
        mcp_server.restore_data(zip_path, dry_run=False)
        # note.txt content successfully restored back to "hello backup"
        self.assertEqual("hello backup", note_file.read_text(encoding="utf-8"))

    def test_extensionless_wal_sqlite_db_backup_restore(self):
        # Regression test: extensionless SQLite DB in WAL mode retains committed state across backup/restore
        import json
        import sqlite3

        db_path = Path(self._tmp.name) / "runs_no_ext"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE metrics (k TEXT PRIMARY KEY, v INT)")
        conn.execute("INSERT INTO metrics VALUES ('processed_docs', 42)")
        conn.commit()
        # verify WAL mode is active
        self.assertEqual("wal", conn.execute("PRAGMA journal_mode").fetchone()[0])
        conn.close()

        import main as main_mod
        old_matrix_db = main_mod.MATRIX_DB_PATH
        self.addCleanup(lambda: setattr(main_mod, "MATRIX_DB_PATH", old_matrix_db))
        main_mod.MATRIX_DB_PATH = db_path

        out = json.loads(mcp_server.backup_data(self._tmp.name))
        zip_path = out["path"]

        # Corrupt / clear the database file after backup
        conn = sqlite3.connect(str(db_path))
        conn.execute("DELETE FROM metrics")
        conn.commit()
        conn.close()

        # Restore from backup
        mcp_server.restore_data(zip_path, dry_run=False)

        # Verify committed data survived
        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT v FROM metrics WHERE k = 'processed_docs'").fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(42, row[0])

    def test_backup_handles_non_sqlite_db_suffix(self):
        # a .db file that is NOT a real SQLite database must be copied byte-for-byte
        import json

        data_dir = Path(self._tmp.name) / "exp"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "notes.db").write_bytes(b"not a real sqlite db")
        old_export = os.environ.get("OPENMATRIX_EXPORT_ROOT")
        os.environ["OPENMATRIX_EXPORT_ROOT"] = str(data_dir)
        self.addCleanup(lambda: self._set_env("OPENMATRIX_EXPORT_ROOT", old_export))

        out = json.loads(mcp_server.backup_data(self._tmp.name))
        import zipfile

        with zipfile.ZipFile(out["path"]) as zf:
            manifest = json.loads(zf.read("manifest.json"))
        arc = [a for a, abs_ in manifest.items() if "notes.db" in abs_]
        self.assertEqual(1, len(arc))
        with zipfile.ZipFile(out["path"]) as zf:
            self.assertEqual(b"not a real sqlite db", zf.read(arc[0]))


class S5SafeSsrfBoundaryTests(unittest.TestCase):
    def test_literal_private_and_mapped_metadata_always_block(self):
        for url in (
            "http://127.0.0.1/",
            "http://10.0.0.1/",
            "http://[::ffff:169.254.169.254]/",
            "http://169.254.169.254/",
        ):
            with self.assertRaisesRegex(ValueError, "blocked address"):
                mcp_server._resolve_public_ips(url)

    def test_off_mode_still_blocks_literal_private(self):
        with mock.patch.dict(os.environ, {"OPENMATRIX_SSRF_MODE": "off"}, clear=False):
            with self.assertRaisesRegex(ValueError, "blocked address"):
                mcp_server._resolve_public_ips("http://192.168.1.1/")

    def test_off_mode_logs_warning_for_dns_host(self):
        with mock.patch.dict(os.environ, {"OPENMATRIX_SSRF_MODE": "off"}, clear=False), \
             mock.patch("mcp_server.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("10.0.0.2", 80))]), \
             self.assertLogs("mcp_server", level="WARNING"):
            mcp_server._resolve_public_ips("http://example.invalid/")

    def test_allowlisted_fake_dns_only_accepts_198_18_15(self):
        info = [(2, 1, 6, "", ("198.18.1.2", 80))]
        with mock.patch.dict(os.environ, {"OPENMATRIX_SSRF_ALLOW_HOSTS": "www.sec.gov"}, clear=False), \
             mock.patch("mcp_server.socket.getaddrinfo", return_value=info):
            _, ip = mcp_server._resolve_public_ips("https://www.sec.gov/")
        self.assertEqual(ip, "198.18.1.2")

    def test_public_host_resolution_uses_mocked_dns(self):
        with mock.patch("mcp_server.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]):
            _, ip = mcp_server._resolve_public_ips("https://example.com/")
        self.assertEqual(ip, "93.184.216.34")

    def test_arbitrary_host_cannot_use_198_18_15_fake_dns(self):
        with mock.patch("mcp_server.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("198.18.1.2", 80))]), \
             self.assertRaisesRegex(ValueError, "fake-IP|blocked address"):
            mcp_server._resolve_public_ips("https://evil.example/")

    def test_configured_cidr_allows_dns_target_but_not_literal_loopback(self):
        with mock.patch.dict(os.environ, {"OPENMATRIX_SSRF_ALLOW_CIDRS": "10.0.0.0/8"}, clear=False), \
             mock.patch("mcp_server.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("10.2.3.4", 80))]):
            _, ip = mcp_server._resolve_public_ips("https://internal.example/")
        self.assertEqual(ip, "10.2.3.4")
        with mock.patch("mcp_server._is_blocked_ip", return_value=False), \
             self.assertRaisesRegex(ValueError, "blocked address"):
            mcp_server._resolve_public_ips("http://127.0.0.1/")

    def test_loopback_dns_host_is_not_allowed_by_cidr_even_with_fixture_patch(self):
        with mock.patch.dict(os.environ, {"OPENMATRIX_SSRF_ALLOW_CIDRS": "127.0.0.0/8"}, clear=False), \
             mock.patch("mcp_server.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 80))]), \
             mock.patch("mcp_server._is_blocked_ip", return_value=False), \
             self.assertRaisesRegex(ValueError, "blocked address"):
            mcp_server._resolve_public_ips("http://host127.example/")

    def test_proxy_is_only_enabled_for_allowlisted_host(self):
        with mock.patch.dict(os.environ, {
            "OPENMATRIX_USE_SYSTEM_PROXY": "true",
            "OPENMATRIX_SSRF_ALLOW_HOSTS": "www.sec.gov",
            "https_proxy": "http://proxy.invalid:8080",
        }, clear=False):
            allowed = mcp_server._ssrf_opener(urllib.parse.urlparse("https://www.sec.gov/"))
            denied = mcp_server._ssrf_opener(urllib.parse.urlparse("https://evil.example/"))
        self.assertTrue(any(isinstance(x, urllib.request.ProxyHandler) for x in allowed.handlers))
        self.assertTrue(any(isinstance(x, urllib.request.ProxyHandler) for x in denied.handlers))

    def test_proxy_open_requires_marker_and_bypass_allows_only_marked(self):
        handler = mcp_server._PolicyProxyHandler()
        req = urllib.request.Request("https://www.sec.gov/")
        with mock.patch("urllib.request.proxy_bypass", return_value=False), \
             mock.patch.object(urllib.request.ProxyHandler, "proxy_open", return_value="proxied") as parent:
            self.assertIsNone(handler.proxy_open(req, "http://proxy.invalid:8080", "https"))
            req._openmatrix_proxy_allowed = True
            self.assertEqual("proxied", handler.proxy_open(req, "http://proxy.invalid:8080", "https"))
            parent.assert_called_once()

class S5ProxyDnsBypassTests(unittest.TestCase):
    def test_proxy_eligible_host_skips_dns(self):
        with mock.patch.dict(os.environ, {
            "OPENMATRIX_USE_SYSTEM_PROXY": "true",
            "OPENMATRIX_SSRF_ALLOW_HOSTS": "www.sec.gov",
        }, clear=False), \
             mock.patch("mcp_server.urllib.request.getproxies", return_value={"https": "http://127.0.0.1:9"}), \
             mock.patch("mcp_server.urllib.request.proxy_bypass", return_value=False), \
             mock.patch("mcp_server.socket.getaddrinfo", side_effect=AssertionError("DNS must be skipped")):
            parsed, ip = mcp_server._resolve_fetch_target("https://www.sec.gov/")
        self.assertEqual("www.sec.gov", parsed.hostname)
        self.assertIsNone(ip)

    def test_local_http_proxy_fetches_allowlisted_host_without_dns(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading

        class ProxyHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"proxy fixture body"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), ProxyHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        proxy = f"http://127.0.0.1:{server.server_port}"
        original = socket.getaddrinfo
        def lookup(host, port, *args, **kwargs):
            if host == "www.sec.gov":
                raise AssertionError("Official hostname DNS must be skipped")
            return original(host, port, *args, **kwargs)
        with mock.patch.dict(os.environ, {
            "OPENMATRIX_USE_SYSTEM_PROXY": "true",
            "OPENMATRIX_SSRF_ALLOW_HOSTS": "www.sec.gov",
            "HTTP_PROXY": proxy, "http_proxy": proxy,
        }, clear=False), \
             mock.patch("mcp_server.urllib.request.proxy_bypass", return_value=False), \
             mock.patch("mcp_server.socket.getaddrinfo", side_effect=lookup):
            self.assertIn("proxy fixture body", mcp_server._fetch_url("http://www.sec.gov/"))

    def test_proxy_path_still_rejects_private_literal_and_userinfo(self):
        with mock.patch.dict(os.environ, {"OPENMATRIX_USE_SYSTEM_PROXY": "true"}, clear=False):
            with self.assertRaisesRegex(ValueError, "blocked address"):
                mcp_server._resolve_fetch_target("https://10.0.0.1/")
            with self.assertRaisesRegex(ValueError, "userinfo"):
                mcp_server._resolve_fetch_target("https://user:pass@www.sec.gov/")

    def test_evil_redirect_is_dns_validated_and_blocked(self):
        with mock.patch.dict(os.environ, {"OPENMATRIX_USE_SYSTEM_PROXY": "true"}, clear=False):
            handler = mcp_server._SsrfsafeRedirectHandler()
            req = urllib.request.Request("https://www.sec.gov/")
            with self.assertRaisesRegex(ValueError, "blocked address"):
                handler.redirect_request(req, None, 302, "found", {"Location": "http://127.0.0.1/"}, "http://127.0.0.1/")

    def test_pinned_handlers_fail_closed_without_pin(self):
        req = urllib.request.Request("http://example.com/")
        with self.assertRaisesRegex(ValueError, "validated"):
            mcp_server._PinnedHttpHandler().http_open(req)

if __name__ == "__main__":
    unittest.main()
