"""Contract tests for the Riveter funding arm (no API calls).

Riveter is measured through its enrichment product (``riveter-enrich``,
POST /v1/enrich): a config generator turns the prompt into per-column agent
configs, then one cell agent runs per output column. The arm must ask the
shared instruction with the shared output schema; the schema rides inside the
prompt because the config generator is what pins column formats — the same
way the Seltz arms carry the schema inside their system prompt. That parity
is asserted here rather than left to review.
"""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

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

VALUE = {
    "latest_stage": "Series B", "latest_announced_on": "2026-01-15",
    "latest_amount": 45_000_000, "currency": "USD",
    "total_raised": 70_000_000, "funding_round_count": 3,
}

ENV = {"RIVETER_API_KEY": "test"}


def enrich_body(status: str = "success", extra_columns: dict | None = None) -> dict:
    output = {
        "Company Name": [{"value": CASE["company_name"]}],
        "Company Domain": [{"value": CASE["company_domain"]}],
        "latest_stage": [{"value": "Series B"}],
        "latest_announced_on": [{"value": "2026-01-15"}],
        "latest_amount": [{"value": "45000000"}],
        "currency": [{"value": "USD"}],
        "total_raised": [{"value": "70000000"}],
        "funding_round_count": [{"value": "3.0"}],
    }
    for header, value in (extra_columns or {}).items():
        output[header] = [{"value": value}]
    return {
        "id": "run_contract_enrich", "type": "enrichment", "status": status,
        "credits_used": 6.0,
        "output": None if status != "success" else output,
    }


class RiveterEnrichContractTest(unittest.TestCase):
    def test_request_shape_matches_the_documented_enrich_contract(self) -> None:
        captured: list[dict] = []

        def fake_request(url: str, headers: dict, payload: dict | None = None, timeout: int = 180) -> dict:
            captured.append({"url": url, "headers": headers, "body": payload})
            return enrich_body()

        with patch.dict(os.environ, ENV), patch.object(runner, "request_json", fake_request):
            normalized, raw = runner.riveter_enrich(CASE)

        self.assertEqual(normalized, VALUE)
        self.assertEqual(raw["credits_used"], 6.0)
        call = captured[0]
        self.assertTrue(call["url"].endswith("/v1/enrich"))
        self.assertEqual(call["headers"]["Authorization"], "Bearer test")
        self.assertEqual(set(call["body"]), {"prompt", "attributes", "input"})
        self.assertEqual(call["body"]["attributes"], list(runner.OUTPUT_SCHEMA["properties"]))
        self.assertEqual(call["body"]["input"], {
            "Company Name": [CASE["company_name"]],
            "Company Domain": [CASE["company_domain"]],
        })

    def test_prompt_carries_the_shared_instruction_and_schema_verbatim(self) -> None:
        """The prompt is the shared instruction plus the shared schema, nothing else."""
        captured: list[dict] = []

        def fake_request(url: str, headers: dict, payload: dict | None = None, timeout: int = 180) -> dict:
            captured.append({"url": url, "body": payload})
            return enrich_body()

        with patch.dict(os.environ, ENV), patch.object(runner, "request_json", fake_request):
            runner.riveter_enrich(CASE)

        sent = captured[0]["body"]
        self.assertTrue(sent["prompt"].startswith(runner.instruction(CASE)))
        self.assertEqual(
            sent["prompt"],
            runner.instruction(CASE)
            + " The output fields must exactly match this JSON Schema: "
            + json.dumps(runner.OUTPUT_SCHEMA),
        )
        self.assertIn(json.dumps(runner.OUTPUT_SCHEMA), sent["prompt"])
        self.assertEqual(sent["attributes"], list(runner.OUTPUT_SCHEMA["properties"]))

    def test_processing_response_is_polled_to_terminal(self) -> None:
        calls: list[str] = []

        def fake_request(url: str, headers: dict, payload: dict | None = None, timeout: int = 180) -> dict:
            calls.append(url)
            if url.endswith("/v1/enrich"):
                return enrich_body(status="processing")
            return enrich_body()

        with patch.dict(os.environ, ENV), patch.object(runner, "request_json", fake_request):
            normalized, _ = runner.riveter_enrich(CASE)

        self.assertEqual(normalized, VALUE)
        self.assertEqual(len(calls), 2)
        self.assertIn("/v1/runs/run_contract_enrich/result", calls[1])

    def test_stopped_run_is_an_error_not_an_empty_result(self) -> None:
        def fake_request(url: str, headers: dict, payload: dict | None = None, timeout: int = 180) -> dict:
            body = enrich_body(status="stopped")
            body["error"] = {"type": "run_error", "message": "Credit limit reached"}
            return body

        with patch.dict(os.environ, ENV), patch.object(runner, "request_json", fake_request), \
             self.assertRaises(ValueError):
            runner.riveter_enrich(CASE)

    def test_reference_values_never_reach_the_provider(self) -> None:
        captured: list[dict] = []

        def fake_request(url: str, headers: dict, payload: dict | None = None, timeout: int = 180) -> dict:
            captured.append(payload)
            return enrich_body()

        with patch.dict(os.environ, ENV), patch.object(runner, "request_json", fake_request):
            runner.riveter_enrich(CASE)

        sent = json.dumps(captured[0])
        for leaked in ("Series B", "2026-01-15", '"45000000"'):
            self.assertNotIn(leaked, sent)

    def test_cell_sentinels_and_numbers_are_normalized(self) -> None:
        self.assertIsNone(runner.riveter_enrich_cell_value([{"value": "not found"}]))
        self.assertIsNone(runner.riveter_enrich_cell_value([{"value": "null"}]))
        self.assertIsNone(runner.riveter_enrich_cell_value([{"value": ""}]))
        self.assertEqual(runner.riveter_enrich_cell_value([{"value": "3.0"}]), 3)
        self.assertEqual(runner.riveter_enrich_cell_value([{"value": "45,000,000"}]), 45_000_000)
        self.assertEqual(runner.riveter_enrich_cell_value([{"value": "Series B"}]), "Series B")

    def test_extra_generated_columns_stay_out_of_the_scored_schema(self) -> None:
        def fake_request(url: str, headers: dict, payload: dict | None = None, timeout: int = 180) -> dict:
            return enrich_body(extra_columns={"Funding Source URL": "https://example.com/press"})

        with patch.dict(os.environ, ENV), patch.object(runner, "request_json", fake_request):
            normalized, raw = runner.riveter_enrich(CASE)

        self.assertEqual(set(normalized), set(runner.OUTPUT_SCHEMA["properties"]))
        self.assertEqual(raw["unmapped_output_columns"], {"Funding Source URL": "https://example.com/press"})


class RiveterRegistrationTest(unittest.TestCase):
    def test_arm_is_registered_for_the_cli(self) -> None:
        self.assertIn("riveter-enrich", runner.PROVIDERS)
        self.assertEqual(runner.PROVIDERS["riveter-enrich"], runner.riveter_enrich)
        self.assertEqual(runner.REQUIRED_ENV["riveter-enrich"], "RIVETER_API_KEY")
        self.assertEqual(runner.DEFAULT_CONCURRENCY["riveter-enrich"], 2)


if __name__ == "__main__":
    unittest.main()
