"""Contract tests for the Tavily Basic and Advanced funding arms (no API calls)."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from funding import run_structured_web_research as runner


CASE = {
    "candidate_id": "contract-case",
    "company_name": "Example Company",
    "company_domain": "example.com",
    "ground_truth_stage": "Series B",
    "ground_truth_announced_on": "2026-01-15",
    "ground_truth_amount": "45000000",
}


class TavilySearchDepthParity(unittest.TestCase):
    def test_basic_and_advanced_differ_only_by_search_depth(self) -> None:
        basic = runner.tavily_payload(CASE, "basic")
        advanced = runner.tavily_payload(CASE, "advanced")
        self.assertEqual(basic["search_depth"], "basic")
        self.assertEqual(advanced["search_depth"], "advanced")
        del basic["search_depth"], advanced["search_depth"]
        self.assertEqual(basic, advanced)

    def test_depths_and_arms_are_pinned(self) -> None:
        self.assertEqual(runner.TAVILY_SEARCH_DEPTHS, ("basic", "advanced"))
        for depth in runner.TAVILY_SEARCH_DEPTHS:
            self.assertIn(f"tavily-{depth}", runner.PROVIDERS)
            self.assertEqual(runner.REQUIRED_ENV[f"tavily-{depth}"], "TAVILY_API_KEY")

    def test_request_does_not_leak_reference_data(self) -> None:
        blob = json.dumps(runner.tavily_payload(CASE, "basic"))
        for leak in ("Series B", "2026-01-15", "45000000", "ground_truth"):
            self.assertNotIn(leak, blob)

    def test_response_parsing_and_citations(self) -> None:
        def fake_request(url: str, headers: dict, payload: dict | None = None, timeout: int = 180) -> dict:
            self.assertEqual(url, "https://api.tavily.com/search")
            self.assertEqual(payload and payload["search_depth"], "advanced")
            return {
                "answer": '{"latest_stage": "Series B"}',
                "results": [{"url": "https://example.com/funding"}, {"url": "https://example.com/funding"}],
            }

        with patch.dict("os.environ", {"TAVILY_API_KEY": "test-key"}), patch.object(runner, "request_json", fake_request):
            normalized, raw = runner.PROVIDERS["tavily-advanced"](CASE)
        self.assertEqual(normalized, {"latest_stage": "Series B"})
        self.assertEqual(raw["search_depth"], "advanced")
        self.assertEqual(raw["sources"], ["https://example.com/funding"])


if __name__ == "__main__":
    unittest.main()
