"""Tests for P2-3 find_contradictions (cross-document numeric discrepancy, v1 heuristic)."""

import json
import unittest

from mcp_server import find_contradictions


class FindContradictionsTests(unittest.TestCase):
    def test_detects_numeric_conflict_across_docs(self):
        docs = [
            {"name": "report_a", "pages": ["Revenue was 100 million dollars in 2025."]},
            {"name": "report_b", "pages": ["Revenue was 150 million dollars in 2025."]},
        ]
        out = json.loads(find_contradictions(docs, "revenue", tolerance=0.05))
        self.assertEqual(1, len(out))
        c = out[0]
        self.assertEqual("numeric_discrepancy", c["conflict_type"])
        self.assertEqual("report_a", c["doc_a"])
        self.assertEqual("report_b", c["doc_b"])
        self.assertEqual(100.0, c["value_a"])
        self.assertEqual(150.0, c["value_b"])
        self.assertGreater(c["relative_difference"], 0.05)

    def test_consistent_values_no_conflict(self):
        docs = [
            {"name": "a", "pages": ["Debt was 40 million dollars."]},
            {"name": "b", "pages": ["Debt stood at 40 million dollars."]},
        ]
        out = json.loads(find_contradictions(docs, "debt", tolerance=0.05))
        self.assertEqual([], out)

    def test_missing_kpi_skipped(self):
        docs = [
            {"name": "a", "pages": ["Revenue was 100 million."]},
            {"name": "b", "pages": ["Nothing about capex here."]},
        ]
        out = json.loads(find_contradictions(docs, "capex", tolerance=0.05))
        self.assertEqual([], out)

    def test_close_values_within_tolerance_no_conflict(self):
        docs = [
            {"name": "a", "pages": ["Revenue was 100 million dollars."]},
            {"name": "b", "pages": ["Revenue was 102 million dollars."]},
        ]
        out = json.loads(find_contradictions(docs, "revenue", tolerance=0.10))
        self.assertEqual([], out)

    def test_invalid_input(self):
        self.assertTrue(find_contradictions([], "x").startswith("Error:"))
        self.assertTrue(find_contradictions([{"name": "a", "pages": ["x"]}], "").startswith("Error:"))
        self.assertTrue(find_contradictions([{"name": "a", "pages": ["x"]}], "t", tolerance=1.5).startswith("Error:"))


if __name__ == "__main__":
    unittest.main()
