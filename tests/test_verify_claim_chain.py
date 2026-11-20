"""Tests for P2-4 verify_claim_chain (conclusion -> citation -> source audit)."""

import json
import unittest

from mcp_server import verify_claim_chain


class VerifyClaimChainTests(unittest.TestCase):
    PAGES = [
        "Revenue was 125 million dollars in 2025. The company grew 18% year over year.",
        "Debt stood at 40 million dollars.",
    ]

    def test_exact_sentences_verified(self):
        out = json.loads(verify_claim_chain("Revenue was 125 million dollars in 2025.", self.PAGES, depth=1))
        self.assertEqual(1, len(out))
        self.assertEqual("verified_exact", out[0]["citation_status"])
        self.assertEqual(1, out[0]["page"])
        self.assertEqual("exact", out[0]["layer"])

    def test_multi_sentence_chain(self):
        out = json.loads(verify_claim_chain(
            "Revenue was 125 million dollars in 2025. Debt stood at 40 million dollars.",
            self.PAGES, depth=1,
        ))
        self.assertEqual(2, len(out))
        self.assertEqual("verified_exact", out[0]["citation_status"])
        self.assertEqual("verified_exact", out[1]["citation_status"])
        self.assertEqual(2, out[1]["page"])

    def test_unverifiable_sentence_not_found(self):
        out = json.loads(verify_claim_chain("Quantum computing will dominate markets.", self.PAGES, depth=1))
        self.assertEqual("not_found", out[0]["citation_status"])
        self.assertIsNone(out[0]["page"])

    def test_depth2_fuzzy_fallback(self):
        # exact fails on the inserted-words paraphrase; fuzzy layer rescues it
        pages = ["Total revenue was 125 million dollars in 2025 growing strongly."]
        out = json.loads(verify_claim_chain("Revenue was 125 million dollars in 2025.", pages, depth=2))
        self.assertNotEqual("not_found", out[0]["citation_status"])
        self.assertEqual("fuzzy", out[0]["layer"])

    def test_depth1_no_fuzzy(self):
        pages = ["Total revenue was 125 million dollars in 2025 growing strongly."]
        out = json.loads(verify_claim_chain("Revenue was 125 million dollars in 2025.", pages, depth=1))
        self.assertEqual("not_found", out[0]["citation_status"])
        self.assertEqual("exact", out[0]["layer"])

    def test_invalid_input(self):
        self.assertTrue(verify_claim_chain("", self.PAGES).startswith("Error:"))
        self.assertTrue(verify_claim_chain("x", []).startswith("Error:"))
        self.assertTrue(verify_claim_chain("x", self.PAGES, depth=3).startswith("Error:"))

    def test_question_exclamation_split(self):
        pages = ["Revenue was 125 million dollars in 2025. Debt stood at 40 million dollars."]
        out = json.loads(verify_claim_chain("Revenue was 125 million dollars? Debt stood at 40 million!", pages, depth=1))
        self.assertEqual(2, len(out))
        # both sentences were split out (question/exclamation terminators honored)
        self.assertEqual("Revenue was 125 million dollars", out[0]["sentence"])
        self.assertEqual("Debt stood at 40 million", out[1]["sentence"])
        self.assertEqual("verified_exact", out[1]["citation_status"])

    def test_delimiters_only_is_error(self):
        self.assertTrue(verify_claim_chain(".;;!?", self.PAGES).startswith("Error:"))

    def test_verify_quote_failure_is_error(self):
        import mcp_server

        original = mcp_server.verify_quote
        mcp_server.verify_quote = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            out = verify_claim_chain("Revenue was 125 million dollars.", self.PAGES, depth=1)
        finally:
            mcp_server.verify_quote = original
        self.assertTrue(out.startswith("Error:"))

    def test_verify_quote_malformed_is_error(self):
        import mcp_server

        original = mcp_server.verify_quote
        mcp_server.verify_quote = lambda *a, **k: "not json"
        try:
            out = verify_claim_chain("Revenue was 125 million dollars.", self.PAGES, depth=1)
        finally:
            mcp_server.verify_quote = original
        self.assertTrue(out.startswith("Error:"))


if __name__ == "__main__":
    unittest.main()
