import hashlib
import unittest
from unittest import mock

from core.evidence import build_browser_snapshot


class BrowserEvidenceSnapshotTests(unittest.TestCase):
    def _payload(self):
        return {
            "title": "Quarterly revenue table",
            "source_url": "https://example.com/research?q=1",
            "canonical_url": "https://example.com/research",
            "captured_at": "2026-08-28T10:00:00Z",
            "content": "Revenue was 125 million.\n\nOperating margin was 25%.",
            "sections": ["Revenue was 125 million.", "Operating margin was 25%."],
        }

    def test_server_recomputes_content_hash(self):
        payload = self._payload()
        payload["content_sha256"] = "untrusted"
        snapshot = build_browser_snapshot(payload)
        expected = hashlib.sha256(
            "Revenue was 125 million.\n\nOperating margin was 25%.".encode("utf-8")
        ).hexdigest()
        self.assertEqual(expected, snapshot.content_sha256)
        self.assertEqual("browser_current_page", snapshot.source_kind)

    def test_no_network_access_is_needed(self):
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            snapshot = build_browser_snapshot(self._payload())
        self.assertEqual("https://example.com/research?q=1", snapshot.source_url)

    def test_rejects_invalid_urls_and_timestamps(self):
        for key, value in (
            ("source_url", "file:///private/page"),
            ("source_url", "javascript:alert(1)"),
            ("source_url", "https://user:password@example.com/page"),
            ("source_url", "https://exämple.com/page"),
            ("canonical_url", "ftp://example.com/file"),
            ("captured_at", "2026-08-28T10:00:00"),
        ):
            payload = self._payload()
            payload[key] = value
            with self.assertRaises(ValueError, msg=key):
                build_browser_snapshot(payload)

    def test_sections_are_cleaned_and_not_pdf_pages(self):
        payload = self._payload()
        payload["sections"] = [" first ", "", "second"]
        snapshot = build_browser_snapshot(payload)
        self.assertEqual(("first", "second"), snapshot.sections)
        self.assertNotIn("page", snapshot.provenance)

    def test_hash_normalizes_line_endings_and_outer_whitespace(self):
        first = self._payload()
        first["sections"] = [" Revenue\r\nwas 125 million. ", " Margin was 25%. "]
        second = self._payload()
        second["sections"] = ["Revenue\nwas 125 million.", "Margin was 25%."]
        self.assertEqual(
            build_browser_snapshot(first).content_sha256,
            build_browser_snapshot(second).content_sha256,
        )

    def test_optional_client_hash_is_not_used(self):
        payload = self._payload()
        payload["content_sha256"] = "client-controlled-hash"
        snapshot = build_browser_snapshot(payload)
        self.assertNotEqual("client-controlled-hash", snapshot.content_sha256)


if __name__ == "__main__":
    unittest.main()
