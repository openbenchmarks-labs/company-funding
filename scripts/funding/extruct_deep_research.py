"""Extruct Deep Research funding arm. Same instruction as the other NL providers."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any


API = "https://api.extruct.ai/v1/deep_research_tasks"
DEPTH = os.environ.get("EXTRUCT_DR_DEPTH", "high")
OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "latest_stage": {
            "type": "string",
            "description": "Most recent funding stage (Seed, Series A, Private Equity, …).",
        },
        "latest_announced_on": {
            "type": "string",
            "description": "Most recent funding announcement date in YYYY-MM-DD.",
        },
        "latest_amount": {
            "type": "integer",
            "description": "Latest round amount in whole major-currency units, never millions.",
        },
        "currency": {"type": "string", "description": "ISO 4217 currency of latest_amount."},
        "total_raised": {
            "type": "integer",
            "description": "Total funding raised in whole major-currency units.",
        },
        "funding_round_count": {"type": "integer", "description": "Number of funding rounds."},
    },
}


def _request(method: str, url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    token = os.environ["EXTRUCT_API_TOKEN"].strip()
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    for attempt in range(1, 5):
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:800]
            if error.code not in {429, 500, 502, 503, 504} or attempt == 4:
                raise RuntimeError(f"HTTP {error.code}: {detail}") from error
            time.sleep(min(2 ** attempt, 60))
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == 4:
                raise
            time.sleep(min(2 ** attempt, 60))
    raise RuntimeError("unreachable")


def _unwrap(value: Any) -> Any:
    if isinstance(value, dict) and "value" in value and ("sources" in value or "reasoning" in value):
        return value.get("value")
    return value


def _text(value: Any) -> str | None:
    value = _unwrap(value)
    if value in (None, "", [], {}):
        return None
    text = str(value).strip()
    return text or None


def _int(value: Any) -> int | None:
    value = _unwrap(value)
    if value in (None, "", [], {}) or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(float(str(value).replace(",", "").replace("$", "").strip()))
    except (TypeError, ValueError):
        return None


def _run_task(brief: str) -> dict[str, Any]:
    started = time.perf_counter()
    created = _request("POST", API, {"brief": brief, "depth": DEPTH, "output_schema": OUTPUT_SCHEMA})
    task_id = created.get("id")
    if not task_id:
        raise RuntimeError(f"create returned no id: {created!r}"[:400])
    deadline = time.time() + 1500
    latest = created
    while time.time() < deadline:
        latest = _request("GET", f"{API}/{task_id}")
        status = latest.get("status")
        if status == "done":
            report = latest.get("report") or {}
            fields = report.get("fields") if isinstance(report, dict) else None
            if not isinstance(fields, dict):
                raise RuntimeError(f"task {task_id} done without schema fields")
            agents = latest.get("agents")
            agent_count = agents if isinstance(agents, int) else len(agents or [])
            return {
                "task_id": task_id,
                "fields": fields,
                "agents": agent_count,
                "latency_ms": round((time.perf_counter() - started) * 1000),
                "cost_units": agent_count * 2,
                "degradation_reasons": report.get("degradation_reasons") or [],
            }
        if status == "failed":
            raise RuntimeError(f"task {task_id} failed: {latest.get('failure_reason') or 'no reason'}")
        time.sleep(15)
    raise TimeoutError(f"task {task_id} timed out (status={latest.get('status')})")


def extruct(case: dict[str, str]) -> tuple[dict, dict]:
    brief = (
        f"Research {case['company_name']} ({case['company_domain']}) and identify its most recent funding event. "
        "Use company, investor, or company-issued wire sources where possible. "
        "Do not infer unknown fields; omit any field you cannot verify. "
        "Amounts are whole major-currency units, never millions or billions. "
        "Equity crowdfunding, Crowdcube, Seedrs, SEIS/EIS rounds, and retail "
        "crowd rounds are equity_crowdfunding — not Seed. "
        "Return only the JSON object required by the schema."
    )
    result = _run_task(brief)
    normalized = {
        "latest_stage": _text(result["fields"].get("latest_stage")),
        "latest_announced_on": _text(result["fields"].get("latest_announced_on")),
        "latest_amount": _int(result["fields"].get("latest_amount")),
        "currency": _text(result["fields"].get("currency")),
        "total_raised": _int(result["fields"].get("total_raised")),
        "funding_round_count": _int(result["fields"].get("funding_round_count")),
    }
    return normalized, {
        "task_id": result["task_id"],
        "agents": result["agents"],
        "degradation_reasons": result["degradation_reasons"],
        "cost_units": result["cost_units"],
        "depth": DEPTH,
        "latency_ms": result["latency_ms"],
    }
