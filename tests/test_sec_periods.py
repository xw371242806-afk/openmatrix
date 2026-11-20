import json
from unittest import TestCase, mock

import mcp_server


class SecXbrlPeriodTests(TestCase):
    """Regression tests for deterministic SEC XBRL period selection."""

    TICKERS = '{"0":{"cik_str":1620533,"ticker":"SHAK","name":"SHAKE SHACK INC"}}'

    @staticmethod
    def _facts():
        annual_old = {
            "val": 1400000000, "start": "2025-01-01", "end": "2025-12-31",
            "fy": 2025, "fp": "FY", "form": "10-K", "filed": "2026-02-10",
            "accn": "0001620533-26-000010", "frame": "CY2025",
        }
        annual_restatement = {**annual_old, "val": 1445306000, "filed": "2026-03-01",
                              "accn": "0001620533-26-000018"}
        quarter = {
            "val": 417618000, "start": "2026-04-01", "end": "2026-06-30",
            "fy": 2026, "fp": "Q2", "form": "10-Q", "filed": "2026-08-01",
            "accn": "0001620533-26-000030", "frame": "CY2026Q2",
        }
        instant = {
            "val": 600000000, "end": "2025-12-31", "fy": 2025, "fp": "FY",
            "form": "10-K", "filed": "2026-02-20", "accn": "0001620533-26-000011",
        }
        return {
            "cik": 1620533,
            "facts": {
                "us-gaap": {
                    "RevenueFromContractWithCustomerExcludingAssessedTax": {
                        "units": {"USD": [annual_old, annual_restatement, quarter]}
                    },
                    "Assets": {"units": {"USD": [instant]}},
                    "QuarterOnly": {"units": {"USD": [quarter]}},
                    "ShortFY": {"units": {"USD": [{
                        "val": 99, "start": "2025-10-01", "end": "2025-12-31",
                        "fy": 2025, "fp": "FY", "form": "10-K", "filed": "2026-02-01",
                    }]}},
                    "HalfYear": {"units": {"USD": [{
                        "val": 100, "start": "2026-01-01", "end": "2026-06-30",
                        "fy": 2026, "fp": "Q2", "form": "10-Q", "filed": "2026-08-01",
                    }]}},
                    "NoMetadata": {"units": {"USD": [{"val": 123}]}},
                    "MixedMetadata": {"units": {"USD": [
                        {"val": 1},
                        {**annual_restatement, "val": 1445306000},
                    ]}},
                }
            },
        }

    def _patch_fetch(self):
        facts = json.dumps(self._facts()).encode()

        def fake_fetch(url, timeout=30, user_agent=None):
            if "company_tickers.json" in url:
                return self.TICKERS.encode()
            if "/companyfacts/CIK" in url:
                return facts
            raise AssertionError(f"unexpected SEC URL: {url}")

        return mock.patch("mcp_server._fetch_bytes", side_effect=fake_fetch)

    def _call(self, names, period="latest_annual"):
        with self._patch_fetch():
            return json.loads(mcp_server.sec_get_xbrl_facts("SHAK", names, period=period))

    def test_prefers_latest_annual_with_period(self):
        data = self._call(["Revenues"])
        fact = data["facts"]["Revenues"]
        self.assertEqual(1445306000, fact["value"])
        self.assertEqual("FY", fact["fp"])
        self.assertEqual("2025-12-31", fact["end"])
        self.assertEqual("RevenueFromContractWithCustomerExcludingAssessedTax", fact["resolved_concept"])

    def test_selects_latest_quarter(self):
        data = self._call(["Revenue"], period="latest_quarter")
        fact = data["facts"]["Revenue"]
        self.assertEqual(417618000, fact["value"])
        self.assertEqual("Q2", fact["fp"])
        self.assertEqual("10-Q", fact["form"])

    def test_all_deduplicates_restatements_by_period(self):
        data = self._call(["Revenue"], period="all")
        values = data["facts"]["Revenue"]
        self.assertEqual(2, len(values))
        self.assertEqual(1445306000, next(v["value"] for v in values if v["fp"] == "FY"))

    def test_instant_fact_uses_latest_fy_value(self):
        data = self._call(["Assets"])
        fact = data["facts"]["Assets"]
        self.assertEqual(600000000, fact["value"])
        self.assertIsNone(fact["start"])
        self.assertEqual("FY", fact["fp"])

    def test_no_qualifying_annual_returns_clear_error(self):
        data = self._call(["QuarterOnly"])
        self.assertIn("error", data["facts"]["QuarterOnly"])
        self.assertIn("latest_annual", data["facts"]["QuarterOnly"]["error"])

    def test_short_fy_duration_is_not_annual(self):
        data = self._call(["ShortFY"])
        self.assertIn("error", data["facts"]["ShortFY"])

    def test_half_year_q2_ytd_is_not_quarter(self):
        data = self._call(["HalfYear"], period="latest_quarter")
        self.assertIn("error", data["facts"]["HalfYear"])

    def test_missing_period_metadata_is_clear_error(self):
        data = self._call(["NoMetadata"])
        self.assertIn("error", data["facts"]["NoMetadata"])
        self.assertIn("period metadata", data["facts"]["NoMetadata"]["error"])

    def test_mixed_metadata_keeps_valid_period(self):
        data = self._call(["MixedMetadata"])
        self.assertEqual(1445306000, data["facts"]["MixedMetadata"]["value"])

    def test_invalid_period_returns_error(self):
        with self._patch_fetch():
            output = mcp_server.sec_get_xbrl_facts("SHAK", ["Revenue"], period="year_to_date")
        self.assertTrue(output.startswith("Error:"))
        self.assertIn("latest_quarter", output)


if __name__ == "__main__":
    import unittest
    unittest.main()
