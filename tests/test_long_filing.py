from unittest.mock import patch
import main


def test_total_revenue_line_is_retrieved_from_long_filing():
    pages = ["Filler legal and operational disclosure." * 40 for _ in range(60)]
    pages[12] = "Shack sales $ 1,391,166 $ 1,207,561. Percentage of Total revenue 96.3 %"
    pages[48] = "Consolidated statements of income (in thousands)\nTotal revenue $ 1,445,306 $ 1,252,608"
    with patch("main.BgeRerankerAdapter.is_available", return_value=False):
        context, _ = main.select_retrieved_context(["What was total revenue for fiscal 2025?"], pages)
    assert "1,445,306" in context
    assert len(context) <= 48000


def test_context_budget_applies_to_fallback(monkeypatch):
    monkeypatch.setenv("OPENMATRIX_CONTEXT_MAX_CHARS", "100")
    context, _ = main.select_retrieved_context(["zzzz"], ["filler " * 3000])
    assert len(context) <= 100


def test_chunks_overlap_without_losing_page():
    chunks = main.build_page_chunks(["abcdefghijklmnopqrst"], max_chars=10, overlap=2)
    assert chunks[0].text[-2:] == chunks[1].text[:2]
    assert all(chunk.page == 1 for chunk in chunks)


def test_long_batch_labels_per_question_context(monkeypatch):
    monkeypatch.setenv("OPENMATRIX_CONTEXT_MAX_CHARS", "500")
    with patch("main.BgeRerankerAdapter.is_available", return_value=False):
        context, _ = main.select_batch_context(["What was revenue?", "How much net income?"], ["Revenue 100. Net income 10."] * 60)
    assert "Context for Q1:" in context and "Context for Q2:" in context
    assert len(context) <= 500
