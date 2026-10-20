"""Offline contract tests for the Prova funding adapter."""

from __future__ import annotations

import unittest
from unittest import mock

from funding import run_funding_benchmark as runner
from funding import smoke_test_funding_providers as smoke


def raw(body: dict, status: int = 200) -> dict:
    return {"http_status": status, "response": body, "latency_ms": 12}


FOUND = {
    "status": "found",
    "company": {"name": "Example Robotics", "domain": "example.com", "public": None},
    "latest_round": {"stage": None, "amount": 37_500_000, "currency": "USD", "date": "2026-10-08",
                     "investors": [], "lead_investors": [], "source": "confirmed"},
    "latest_stage": "series_b",
    "rounds": [
        {"stage": None, "amount": 37_500_000, "currency": "USD", "date": "2026-10-08", "investors": [], "lead_investors": [], "source": "confirmed"},
        {"stage": "series_b", "amount": 82_000_000, "currency": "USD", "date": "2025-10-07", "investors": [], "lead_investors": [], "source": "verified"},
    ],
}


class ProvaContractTest(unittest.TestCase):
    def test_provider_is_registered(self) -> None:
        required, call = smoke.PROVIDERS["prova"]
        self.assertEqual(required, ("PROVA_API_KEY",))
        self.assertIs(call, smoke.prova)

    def test_normalizes_the_stage_the_company_is_at(self) -> None:
        response = raw(FOUND)
        self.assertEqual(runner.status_for("prova", response), ("ok", None))
        self.assertEqual(runner.normalize("prova", response), {
            "latest_stage": "series_b",
            "latest_date": "2026-10-08",
            "latest_amount": 37_500_000,
            "total_raised": None,
            "round_count": 2,
        })

    def test_not_found_and_pending(self) -> None:
        self.assertEqual(runner.status_for("prova", raw({"status": "not_found", "message": "No funding found for this company."})),
                         ("not_found", "No funding found for this company."))
        self.assertEqual(runner.status_for("prova", raw({"status": "pending", "message": "..."}, 202)), ("error", "answer still pending"))

    def test_waits_for_a_pending_answer(self) -> None:
        answers = [raw({"status": "pending"}, 202), raw({"status": "pending"}, 202), raw(FOUND)]
        with mock.patch.dict("os.environ", {"PROVA_API_KEY": "test"}), \
             mock.patch.object(smoke, "request", side_effect=answers) as call, mock.patch.object(smoke.time, "sleep"):
            result = smoke.prova("example.com")
        self.assertEqual((result["http_status"], result["polls"], call.call_count), (200, 2, 3))
        self.assertEqual(call.call_args_list[1].kwargs["json"], {"domain": "example.com", "refresh": "auto"})


if __name__ == "__main__":
    unittest.main()
