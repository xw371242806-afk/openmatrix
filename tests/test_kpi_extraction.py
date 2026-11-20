import json
import mcp_server as m

def test_financial_rows_extract_current_columns_and_scale():
    page="(dollar amounts in thousands)\n2025 2024 2023\nTotal revenue $ 1,445,306 $ 1,252,608 $ 1,087,533\nNet income attributable to Shake Shack Inc. $ 45,725 $ 10,207 $ 20,264"
    revenue=m._extract_kpi_value("Total revenue",page)
    assert revenue["value"]==1445306 and revenue["scale"]=="thousands"
    assert revenue["normalized_value"]==1445306000
    assert revenue["other_columns"]==[1252608,1087533]
    assert m._extract_kpi_value("Net income",page)["value"]==45725

def test_same_shack_skips_footnote_and_reports_percent():
    hit=m._extract_kpi_value("Same-Shack sales","Same-Shack sales1 increased 2.3% in fiscal 2025.")
    assert hit["value"]==2.3 and hit["unit"]=="%"

def test_year_headers_and_percentage_word_are_not_amounts():
    assert m._extract_kpi_value("Net income","Net income\n2025 2024 2023") is None
    hit=m._extract_kpi_value("Total revenue","Percentage of Total revenue 96.3%\nTotal revenue $ 1,445,306")
    assert hit["value"]==1445306 and hit["unit"]!="percent"
    assert not 1900<=hit["value"]<=2100

def test_ambiguous_equal_rows_require_review():
    hit=m._extract_kpi_value("Revenue","Revenue $ 100\nRevenue $ 200")
    assert hit["needs_review"] is True and hit["confidence"]=="low"

def test_calendar_qualifiers_do_not_replace_sales_percent():
    hit=m._extract_kpi_value("Same-Shack sales","Same-Shack sales for the fiscal fourth quarter ended December 31, 2025 increased 2.1% compared to the same period last year.")
    assert hit["value"]==2.1 and hit["unit"]=="%"
    assert hit["period"]=="quarter"
    assert m._extract_kpi_value("Same-Shack sales","Same-Shack sales were calculated excluding the 53rd week.") is None

def test_annual_sales_table_outranks_quarterly_narrative():
    pages=["Same-Shack sales for the fiscal fourth quarter ended December 31, 2025 increased 2.1%."]+["filler information "*300]*58+["Fiscal year ended December 31, 2025\nSame-Shack sales 2.3% 1.5% 2.7%"]
    result=json.loads(m.extract_kpis(pages,["Same-Shack sales"],limit_per_question=1))
    hit=result["kpis"][0]["values"][0]
    assert hit["value"]==2.3 and hit["page"]==60
    assert hit["period"]=="annual"

def test_parent_net_income_is_labeled_and_month_count_is_not_sales():
    page="Fiscal year ended December 31, 2025\nNet income $ 49,706 $ 10,820\nNet income attributable to Acme Inc. $ 45,725 $ 10,207"
    hit=m._extract_kpi_value("Net income",page)
    assert hit["value"]==45725
    assert "attributable to Acme" in hit["metric_label"]
    assert m._extract_kpi_value("Same-Shack sales","Same-Shack sales include locations that have been open for 24 months.") is None
