import json
from unittest.mock import patch
import mcp_server as m

def test_sec_search_filing_url_uses_www_archives():
    submissions = {"filings":{"recent":{"form":["10-K"],"filingDate":["2026-02-20"],"accessionNumber":["0001620533-26-000018"],"primaryDocument":["shak-20251231.htm"]}}}
    with patch.object(m,"_sec_cik",return_value="0001620533"), patch.object(m,"_fetch_bytes",return_value=json.dumps(submissions).encode()):
        result=json.loads(m.sec_search("SHAK","10-K"))
    assert result["filings"][0]["filing_url"]=="https://www.sec.gov/Archives/edgar/data/1620533/000162053326000018/shak-20251231.htm"

def test_sec_get_filing_labels_char_chunks():
    text="Filing text " * 2000
    with patch.object(m,"_fetch_url",return_value=text):
        result=json.loads(m.sec_get_filing("https://www.sec.gov/filing.htm"))
    assert result["unit"]=="char_chunk" and result["page_is_physical"] is False
    assert result["pages"][0]["section"]==1
    assert all(page["page_is_physical"] is False for page in result["pages"])
    assert "".join(page["text"] for page in result["pages"])==text

def test_sec_user_agent_warns_when_missing(monkeypatch,caplog):
    monkeypatch.delenv("OPENMATRIX_SEC_USER_AGENT",raising=False)
    m._sec_user_agent()
    assert "OPENMATRIX_SEC_USER_AGENT" in caplog.text
    assert "contact" in caplog.text
