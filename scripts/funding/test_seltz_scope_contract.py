"""Contract tests for the two Seltz funding arms (no API calls).

The point of running Seltz twice is to isolate the search scope. If the arms
ever stop sending the same query, system prompt and response_format, the
comparison silently stops meaning anything, so that parity is asserted here
rather than left to review.

Also pins the answer parsing, because Seltz is the one provider on the board
whose structured output arrives as a Markdown string rather than an object.
"""

from __future__ import annotations

import json
import unittest

from funding import run_structured_web_research as runner


CASE = {
    "candidate_id": "contract-case",
    "company_name": "Example Company",
    "company_domain": "example.com",
    # Reference columns ride along in the real CSV row. None of them may ever
    # reach a provider request.
    "ground_truth_stage": "Series B",
    "ground_truth_announced_on": "2026-01-15",
    "ground_truth_amount": "45000000",
}


class SeltzScopeParity(unittest.TestCase):
    def test_scope_is_the_only_difference(self) -> None:
        for json_schema in (True, False):
            companies = runner.seltz_payload(CASE, "companies", json_schema)
            news = runner.seltz_payload(CASE, "news", json_schema)
            self.assertEqual(companies["scope"], "companies")
            self.assertEqual(news["scope"], "news")
            del companies["scope"], news["scope"]
            self.assertEqual(
                companies,
                news,
                f"Seltz arms diverge beyond scope (json_schema={json_schema})",
            )

    def test_query_matches_the_shared_instruction(self) -> None:
        """Every provider on the board is asked the same thing."""
        for scope in runner.SELTZ_SCOPES:
            self.assertEqual(
                runner.seltz_payload(CASE, scope, True)["query"],
                runner.instruction(CASE),
            )

    def test_system_prompt_carries_the_shared_schema(self) -> None:
        payload = runner.seltz_payload(CASE, "companies", False)
        for field in runner.OUTPUT_SCHEMA["properties"]:
            self.assertIn(field, payload["system_prompt"])

    def test_no_reference_data_reaches_the_request(self) -> None:
        blob = json.dumps(runner.seltz_payload(CASE, "companies", True))
        for leak in ("Series B", "2026-01-15", "45000000", "ground_truth"):
            self.assertNotIn(leak, blob)

    def test_scopes_registered_as_providers(self) -> None:
        for scope in runner.SELTZ_SCOPES:
            self.assertIn(f"seltz-{scope}", runner.PROVIDERS)


class SeltzAnswerParsing(unittest.TestCase):
    """Seltz returns Markdown. These are the shapes actually seen in the wild."""

    def test_bare_json_object(self) -> None:
        self.assertEqual(
            runner.parse_json_answer('{"latest_stage": "Series B"}'),
            {"latest_stage": "Series B"},
        )

    def test_fenced_json_block(self) -> None:
        self.assertEqual(
            runner.parse_json_answer('```json\n{"latest_stage": "Series C"}\n```'),
            {"latest_stage": "Series C"},
        )

    def test_json_embedded_in_prose(self) -> None:
        answer = 'Here is what I found:\n\n{"latest_stage": "Seed"}\n\nHope that helps.'
        self.assertEqual(runner.parse_json_answer(answer), {"latest_stage": "Seed"})

    def test_prose_is_preserved_not_salvaged(self) -> None:
        """A prose answer is a result, not something to regex a score out of."""
        answer = "Example Company most recently raised a Series B."
        self.assertEqual(runner.parse_json_answer(answer), {"unparsed_text": answer})

    def test_json_array_is_not_a_result(self) -> None:
        self.assertEqual(
            runner.parse_json_answer('["Series B"]'),
            {"unparsed_text": '["Series B"]'},
        )


class FirecrawlRequest(unittest.TestCase):
    def test_credit_ceiling_is_explicit(self) -> None:
        """The API default is 2,500 per run, which is a runaway across a cohort."""
        self.assertLess(runner.FIRECRAWL_MAX_CREDITS, 2_500)

    def test_model_is_spark_2(self) -> None:
        self.assertEqual(runner.FIRECRAWL_MODEL, "spark-2")

    def test_registered_with_its_own_key(self) -> None:
        self.assertEqual(runner.REQUIRED_ENV["firecrawl"], "FIRECRAWL_API_KEY")


if __name__ == "__main__":
    unittest.main()
