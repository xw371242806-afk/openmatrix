"""Tests for A2/A3/A5 tools (tasks_list/task_list, web_crawl/web_extract, component_status)."""

import json
import os
import unittest
from unittest import mock

import agent.tasks as tasks_mod
from mcp_server import (
    component_status,
    task_list,
    web_crawl,
    web_extract,
)


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._payload


class WebCrawlExtractTests(unittest.TestCase):
    def setUp(self):
        os.environ["OPENMATRIX_TAVILY_KEY"] = "tvly-test"
        os.environ.pop("OPENMATRIX_ENABLE_WEB_RESEARCH", None)

    def tearDown(self):
        os.environ.pop("OPENMATRIX_TAVILY_KEY", None)

    def test_web_crawl_returns_pages(self):
        payload = json.dumps({"results": [{"url": "https://x.com/a", "title": "A"}, {"url": "https://x.com/b", "title": "B"}]}).encode()
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse(payload)):
            out = web_crawl("https://x.com", max_pages=2)
        data = json.loads(out)
        self.assertIsInstance(data, list)
        self.assertEqual("https://x.com/a", data[0]["url"])
        self.assertEqual("A", data[0]["title"])

    def test_web_crawl_malformed_results_is_error(self):
        payload = json.dumps({"pages": []}).encode()  # wrong key (real schema uses results)
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse(payload)):
            out = web_crawl("https://x.com")
        self.assertTrue(out.startswith("Error:"))

    def test_web_crawl_empty_results_is_error(self):
        payload = json.dumps({"results": []}).encode()
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse(payload)):
            out = web_crawl("https://x.com")
        self.assertTrue(out.startswith("Error:"))

    def test_web_crawl_sends_limit_not_max_pages(self):
        payload = json.dumps({"results": [{"url": "https://x.com/a"}]}).encode()
        seen = {}

        def fake_urlopen(req, *a, **kw):
            seen["body"] = json.loads(req.data.decode("utf-8"))
            return FakeResponse(payload)

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            web_crawl("https://x.com", max_depth=2, max_pages=7)
        self.assertEqual(7, seen["body"].get("limit"))
        self.assertNotIn("max_pages", seen["body"])

    def test_web_crawl_invalid_input(self):
        self.assertTrue(web_crawl("").startswith("Error:"))
        self.assertTrue(web_crawl("https://x.com", max_depth=0).startswith("Error:"))

    def test_web_crawl_missing_key(self):
        os.environ.pop("OPENMATRIX_TAVILY_KEY", None)
        self.assertTrue(web_crawl("https://x.com").startswith("Error:"))

    def test_web_extract_returns_content(self):
        payload = json.dumps({"results": [{"raw_content": "hello world"}]}).encode()
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse(payload)):
            out = web_extract("https://x.com/a")
        data = json.loads(out)
        self.assertEqual("hello world", data["content"])

    def test_web_extract_invalid_input(self):
        self.assertTrue(web_extract("").startswith("Error:"))

    def test_web_extract_error_sanitized(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("boom with key")):
            out = web_extract("https://x.com")
        self.assertTrue(out.startswith("Error:"))
        self.assertNotIn("boom", out)
        self.assertNotIn("tvly-test", out)

    def test_web_extract_malformed_is_error(self):
        payload = json.dumps({}).encode()
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse(payload)):
            out = web_extract("https://x.com")
        self.assertTrue(out.startswith("Error:"))

    def test_web_extract_empty_results_is_error(self):
        payload = json.dumps({"results": []}).encode()
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse(payload)):
            out = web_extract("https://x.com")
        self.assertTrue(out.startswith("Error:"))


class ComponentStatusTests(unittest.TestCase):
    def test_returns_component_map(self):
        data = json.loads(component_status())
        self.assertIn("components", data)
        self.assertIn("env", data)
        self.assertIn("pypdf", data["components"])
        self.assertIn("tavily_key", data["env"])
        self.assertIn(data["components"]["pypdf"], ("available", "missing"))


class TaskListTests(unittest.TestCase):
    def test_task_list_empty_or_entries(self):
        data = json.loads(task_list())
        self.assertIn("tasks", data)


if __name__ == "__main__":
    unittest.main()
