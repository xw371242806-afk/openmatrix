from pathlib import Path
import unittest


INDEX_HTML = Path(__file__).resolve().parents[1] / "static" / "index.html"
PDF_VIEWER_JS = Path(__file__).resolve().parents[1] / "static" / "js" / "pdf-viewer.js"


class FrontendStaticTests(unittest.TestCase):
    def test_citation_status_is_visible(self):
        html = INDEX_HTML.read_text(encoding="utf-8")

        self.assertIn("Citation Status", html)
        self.assertIn("verified_exact", html)
        self.assertIn("Quote not found", html)
        self.assertIn("Page ${escapeHtml(params.data.page)}", html)

    def test_model_output_is_escaped_before_grid_render(self):
        html = INDEX_HTML.read_text(encoding="utf-8")

        self.assertIn("function escapeHtml", html)
        self.assertIn("${escapeHtml(params.value)}", html)
        self.assertIn("escapeHtml(params.data.page)", html)
        self.assertIn("data-quote=\"${escapeHtml(params.value)}\"", html)

    def test_api_key_is_not_persisted_in_browser_storage(self):
        html = INDEX_HTML.read_text(encoding="utf-8")

        self.assertNotIn("localStorage.setItem('openmatrix_apiKey'", html)
        self.assertNotIn("localStorage.getItem('openmatrix_apiKey'", html)
        self.assertNotIn("sessionStorage.setItem('openmatrix_apiKey'", html)
        self.assertIn("localStorage.removeItem('openmatrix_apiKey')", html)
        self.assertIn("API Key 仅保留在当前页面输入框中", html)

    def test_csv_export_button_and_warning_are_present(self):
        html = INDEX_HTML.read_text(encoding="utf-8")

        self.assertIn("btnExportCsv", html)
        self.assertIn("exportCsv()", html)
        self.assertIn("Export CSV", html)
        self.assertIn("安全提示", html)
        self.assertIn("/api/export-csv/' + encodeURIComponent(currentRunId)", html)

    def test_export_bar_visibility_tracks_run_id(self):
        html = INDEX_HTML.read_text(encoding="utf-8")

        self.assertIn("currentRunId = result.run_id || null;", html)
        self.assertIn("exportBar').style.display = currentRunId ? 'block' : 'none'", html)

    def test_pdf_js_script_is_loaded(self):
        html = INDEX_HTML.read_text(encoding="utf-8")

        self.assertIn("pdf.min.js", html)
        self.assertIn("static/js/pdf-viewer.js", html)

    def test_pdf_viewer_container_is_present(self):
        html = INDEX_HTML.read_text(encoding="utf-8")

        self.assertIn('id="pdfViewer"', html)
        self.assertIn('id="pdfViewerStatus"', html)
        self.assertIn("Document Preview", html)

    def test_ag_grid_is_loaded(self):
        html = INDEX_HTML.read_text(encoding="utf-8")

        self.assertIn("ag-grid-community", html)
        self.assertIn("ag-theme-alpine", html)
        self.assertIn("id=\"matrixGrid\"", html)
        self.assertIn("renderMatrixGrid", html)
        self.assertIn("createGrid", html)

    def test_ag_grid_hover_triggers_highlight(self):
        html = INDEX_HTML.read_text(encoding="utf-8")

        self.assertIn("OpenMatrixPdfViewer.highlightQuote", html)
        self.assertIn("OpenMatrixPdfViewer.clearHighlights", html)
        self.assertIn("onCellMouseEnter", html)
        self.assertIn("onCellMouseLeave", html)

    def test_pdf_viewer_js_defines_highlight_api(self):
        js = PDF_VIEWER_JS.read_text(encoding="utf-8")

        self.assertIn("renderPdf", js)
        self.assertIn("highlightQuote", js)
        self.assertIn("clearHighlights", js)
        self.assertIn("window.OpenMatrixPdfViewer", js)

    def test_citation_navigator_is_rendered(self):
        html = INDEX_HTML.read_text(encoding="utf-8")

        self.assertIn('id="citationNavigator"', html)
        self.assertIn('id="citationList"', html)
        self.assertIn('id="citationEmpty"', html)
        self.assertIn("renderCitationNavigator", html)
        self.assertIn("citation-item", html)

    def test_citation_click_navigates_pdf(self):
        html = INDEX_HTML.read_text(encoding="utf-8")
        js = PDF_VIEWER_JS.read_text(encoding="utf-8")

        self.assertIn("navigateToCitation", html)
        self.assertIn("navigateToCitation", js)
        self.assertIn("highlightQuoteOnPage", js)
        self.assertIn("scrollIntoView", js)

    def test_comparison_panel_is_present(self):
        html = INDEX_HTML.read_text(encoding="utf-8")

        self.assertIn("Cross-run Comparison", html)
        self.assertIn('id="compareRunA"', html)
        self.assertIn('id="compareRunB"', html)
        self.assertIn('id="btnCompareRuns"', html)
        self.assertIn('id="comparisonTable"', html)
        self.assertIn('id="comparisonEmpty"', html)

    def test_comparison_uses_existing_run_endpoints(self):
        html = INDEX_HTML.read_text(encoding="utf-8")

        self.assertIn("/api/v1/runs?limit=50", html)
        self.assertIn("'/api/matrix-runs/' + encodeURIComponent(runId)", html)

    def test_comparison_marks_divergent_cells(self):
        html = INDEX_HTML.read_text(encoding="utf-8")

        self.assertIn("divergent-row", html)
        self.assertIn("cellValuesEqual", html)
        self.assertIn('normalizeComparisonValue', html)
        self.assertIn("row.classList.add('divergent-row')", html)

    def test_comparison_escapes_rendered_output(self):
        html = INDEX_HTML.read_text(encoding="utf-8")

        self.assertIn("escapeHtml(cell.answer || '')", html)
        self.assertIn("escapeHtml(cell.page)", html)
        self.assertIn("escapeHtml(citationStatusLabel(cell.status))", html)
        self.assertIn("escapeHtml(citationStatusLabel(cell.citation_status))", html)

    def test_comparison_key_builder_has_no_nullish_mix_syntax_error(self):
        html = INDEX_HTML.read_text(encoding="utf-8")

        # Regression: `a || b ?? c` is a SyntaxError in every JS engine; the
        # `??` operand must be parenthesized (P0-B browser smoke item).
        self.assertIn("cell.document_name || (cell.document_index ?? '')", html)
        self.assertNotIn("cell.document_name || cell.document_index ??", html)
        self.assertIn("function comparisonCellKey", html)
        self.assertIn("function buildComparisonMap", html)
        self.assertIn("function renderComparison", html)


if __name__ == "__main__":
    unittest.main()
