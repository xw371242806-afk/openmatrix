import json

import pytest
from reportlab.pdfgen import canvas
from reportlab.pdfbase.pdfmetrics import stringWidth

from mcp_server import _locate_quote_bboxes


@pytest.fixture
def footnote_pdf(tmp_path):
    path = tmp_path / "footnote.pdf"
    pdf = canvas.Canvas(str(path))
    pdf.setFont("Helvetica", 10)
    prefix = "Same-Shack sales"
    pdf.drawString(40, 720, prefix)
    x = 40 + stringWidth(prefix, "Helvetica", 10)
    pdf.setFont("Helvetica", 6.5)
    pdf.drawString(x, 723.5, "1")
    pdf.setFont("Helvetica", 10)
    pdf.drawString(x + stringWidth("1", "Helvetica", 6.5), 720, " for the fiscal year increased 2.3%.")
    pdf.drawString(40, 680, "Revenue 1 was 45,725 dollars.")
    pdf.save()
    return tmp_path


def test_quote_with_raised_footnote_matches_body_and_correct_coordinates(footnote_pdf):
    data = json.loads(_locate_quote_bboxes(
        "footnote.pdf", "Same-Shack sales for the fiscal year increased 2.3%.",
        page=1, root=footnote_pdf,
    ))
    assert data["count"] == 1
    match = data["matches"][0]
    assert match["page"] == 1
    assert match["bbox"][0] == pytest.approx(40)
    assert 110 < match["bbox"][1] < match["bbox"][3] < 130
    assert match["bbox"][2] > 250
    assert match["bbox"][3] - match["bbox"][1] == pytest.approx(10)


def test_baseline_digit_is_not_discarded_as_footnote(footnote_pdf):
    exact = json.loads(_locate_quote_bboxes("footnote.pdf", "Revenue 1 was 45,725 dollars.", page=1, root=footnote_pdf))
    missing = json.loads(_locate_quote_bboxes("footnote.pdf", "Revenue was 45,725 dollars.", page=1, root=footnote_pdf))
    assert exact["count"] == 1
    assert missing["count"] == 0


def test_numeric_answer_mismatch_still_has_no_highlight(footnote_pdf):
    data = json.loads(_locate_quote_bboxes("footnote.pdf", "Same-Shack sales for the fiscal year increased 3.1%.", page=1, root=footnote_pdf))
    assert data["count"] == 0


def test_explicitly_quoted_superscript_digit_is_retained(footnote_pdf):
    data = json.loads(_locate_quote_bboxes("footnote.pdf", "Same-Shack sales1 for the fiscal year increased 2.3%.", page=1, root=footnote_pdf))
    assert data["count"] == 1
