import unittest

from main import verify_matrix_citations


DOCUMENT_PAGES = [
    "OpenMatrix Test Company 2025 Annual Report. Total revenue was 125 million dollars.",
    "Research and development investment was 18 million dollars. The primary risk was supply chain disruption.",
]


class CitationVerificationTests(unittest.TestCase):
    def test_exact_quote_is_verified_with_page_number(self):
        rows = [
            {
                "question": "What was total revenue?",
                "answer": "125 million dollars.",
                "quote": "Total revenue was 125 million dollars.",
            }
        ]

        verified = verify_matrix_citations(rows, DOCUMENT_PAGES)

        self.assertEqual("verified_exact", verified[0]["citation_status"])
        self.assertEqual(1, verified[0]["page"])

    def test_missing_quote_is_not_marked_successful(self):
        rows = [
            {
                "question": "What was total revenue?",
                "answer": "125 million dollars.",
                "quote": "Revenue exceeded 900 million dollars.",
            }
        ]

        verified = verify_matrix_citations(rows, DOCUMENT_PAGES)

        self.assertEqual("not_found", verified[0]["citation_status"])
        self.assertIsNone(verified[0]["page"])

    def test_empty_quote_is_not_applicable(self):
        rows = [
            {
                "question": "Who was the main competitor?",
                "answer": "The document does not say.",
                "quote": "",
            }
        ]

        verified = verify_matrix_citations(rows, DOCUMENT_PAGES)

        self.assertEqual("not_applicable", verified[0]["citation_status"])
        self.assertIsNone(verified[0]["page"])
        self.assertEqual(0.0, verified[0]["match_score"])

    def test_normalized_quote_with_extra_spaces_and_newlines(self):
        rows = [
            {
                "question": "What was the primary risk?",
                "answer": "Supply chain disruption.",
                "quote": "The  primary  risk\nwas  supply  chain  disruption.",
            }
        ]

        verified = verify_matrix_citations(rows, DOCUMENT_PAGES)

        self.assertEqual("verified_normalized", verified[0]["citation_status"])
        self.assertEqual(2, verified[0]["page"])
        self.assertEqual(0.95, verified[0]["match_score"])

    def test_fuzzy_quote_with_minor_ocr_typo(self):
        # "Research and development investment was 18 million dollars." with minor typo
        rows = [
            {
                "question": "What was R&D investment?",
                "answer": "18 million dollars.",
                "quote": "Research and development investmnt was 18 millon dollars.",
            }
        ]

        verified = verify_matrix_citations(rows, DOCUMENT_PAGES)

        self.assertEqual("verified_fuzzy", verified[0]["citation_status"])
        self.assertEqual(2, verified[0]["page"])
        self.assertGreaterEqual(verified[0]["match_score"], 0.82)


if __name__ == "__main__":
    unittest.main()
