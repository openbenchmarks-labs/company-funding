"""Drop-in Fundable adapter for the company-funding benchmark."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any


SLUG = "fundable"
NAME = "Fundable"
REQUIRED_ENV = ("FUNDABLE_API_KEY",)
MIN_START_INTERVAL_SECONDS = 0.35


def request(
    domain: str,
    request_fn: Callable[..., dict[str, Any]],
    environment: Mapping[str, str],
) -> dict[str, Any]:
    """Fetch one company by domain using the benchmark HTTP wrapper."""
    return request_fn(
        "GET",
        "https://www.tryfundable.ai/api/v1/company",
        headers={
            "Authorization": f"Bearer {environment['FUNDABLE_API_KEY']}",
            "Accept": "application/json",
        },
        params={"domain": domain},
    )


def not_found_reason(response: dict[str, Any]) -> str | None:
    """Return a reason when a successful HTTP response has no company match."""
    if (response.get("data") or {}).get("company"):
        return None
    error = response.get("error")
    if isinstance(error, dict):
        return error.get("message") or "no company"
    if isinstance(error, str) and error:
        return error
    return "no company"


def normalize(response: dict[str, Any]) -> dict[str, Any]:
    """Map Fundable's company response to the benchmark's five fields."""
    company = (response.get("data") or {}).get("company") or {}
    latest_deal = company.get("latest_deal") or {}
    stage = latest_deal.get("type")
    if stage and latest_deal.get("pre"):
        stage = f"pre {stage}"
    return {
        "latest_stage": stage,
        "latest_date": latest_deal.get("date"),
        "latest_amount": latest_deal.get("total_round_raised"),
        "total_raised": company.get("total_raised"),
        "round_count": company.get("num_funding_rounds"),
    }
