"""Tests for P5-3 web_research MCP tool (Tavily, mocked - no external calls)."""

import json
import os
import unittest
from unittest import mock

import mcp_server
from mcp_server import web_research

TAVILY_OK = json.dumps({
    "query": "OpenAI funding",
    "results": [
        {"title": "OpenAI raises $12B", "url": "https://example.com/1", "content": "OpenAI added funds.", "score": 0.98},
        {"title": "AI market", "url": "https://example.com/2", "content": "Trends in AI.", "score": 0.85},
    ],
}).encode("utf-8")


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._payload


class WebResearchTests(unittest.TestCase):
    def setUp(self):
        os.environ["OPENMATRIX_TAVILY_KEY"] = "tvly-test-key"
        os.environ.pop("OPENMATRIX_ENABLE_WEB_RESEARCH", None)

    def tearDown(self):
        os.environ.pop("OPENMATRIX_TAVILY_KEY", None)
        os.environ.pop("OPENMATRIX_ENABLE_WEB_RESEARCH", None)

    def test_returns_results(self):
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse(TAVILY_OK)):
            out = web_research("OpenAI funding", max_results=2)
        data = json.loads(out)
        # contract: top-level flat list
        self.assertIsInstance(data, list)
        self.assertEqual(2, len(data))
        self.assertEqual("OpenAI raises $12B", data[0]["title"])
        self.assertEqual(0.98, data[0]["score"])
        self.assertEqual("https://example.com/1", data[0]["url"])

    def test_missing_key(self):
        os.environ.pop("OPENMATRIX_TAVILY_KEY", None)
        self.assertTrue(web_research("q").startswith("Error:"))

    def test_flag_disabled(self):
        os.environ["OPENMATRIX_ENABLE_WEB_RESEARCH"] = "0"
        self.assertTrue(web_research("q").startswith("Error:"))

    def test_invalid_input(self):
        self.assertTrue(web_research("").startswith("Error:"))
        self.assertTrue(web_research("q", max_results=0).startswith("Error:"))
        self.assertTrue(web_research("q", max_results=20).startswith("Error:"))

    def test_remote_error_sanitized(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("boom with key")):
            out = web_research("q")
        self.assertTrue(out.startswith("Error:"))
        self.assertNotIn("boom", out)
        self.assertNotIn("tvly-test-key", out)

    def test_key_not_reflected_in_request_payload(self):
        seen = {}

        def fake_urlopen(req, *a, **kw):
            seen["body"] = req.data.decode("utf-8")
            return FakeResponse(TAVILY_OK)

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            web_research("q", max_results=2)
        self.assertIn("tvly-test-key", seen["body"])  # key sent to Tavily...
        self.assertNotIn("OPENMATRIX", seen["body"])  # ...but env var name never leaked


if __name__ == "__main__":
    unittest.main()
