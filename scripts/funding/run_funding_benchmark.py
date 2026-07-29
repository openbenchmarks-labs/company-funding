#!/usr/bin/env python3
"""Run the 300-company funding benchmark without repaying completed cells.

One worker processes each provider sequentially; providers run in parallel. Every
completed attempt (including a rate limit or transport error) is atomically
written as a separate raw-response file before the worker moves on. Resumes skip
all existing cells by default. Reattempting a failed cell requires an explicit
``--retry-status`` choice.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from funding.adapters import ADAPTERS
from smoke_test_funding_providers import (
    DOMAINS as SMOKE_DOMAINS,
    OUTPUT as SMOKE_OUTPUT,
    PROVIDERS,
    funding_paths,
    load_environment,
)


ROOT = Path(__file__).resolve().parents[2]
INPUT = ROOT / "data" / "funding" / "company-funding-inputs-v1.csv"
RUN_DIR = ROOT / "data" / "funding" / "provider-runs-v1"
RAW_DIR = RUN_DIR / "raw"
SUMMARY = RUN_DIR / "summary.json"
MANIFEST = RUN_DIR / "manifest.json"

# Keep documented provider limits safely below their requests-per-minute caps.
MIN_START_INTERVAL_SECONDS = {
    "ocean": 1.1,
    **{slug: adapter.MIN_START_INTERVAL_SECONDS for slug, adapter in ADAPTERS.items()},
}
WRITE_LOCK = threading.Lock()


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with WRITE_LOCK:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            temporary = Path(handle.name)
        temporary.replace(path)


def safe_filename(value: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "_", value.lower()).strip("_")


def cell_path(provider: str, domain: str) -> Path:
    return RAW_DIR / provider / f"{safe_filename(domain)}.json"


def load_cases() -> list[dict[str, str]]:
    cases = list(csv.DictReader(INPUT.open(newline="")))
    domains = [row["company_domain"].lower().strip() for row in cases]
    if len(cases) != 300 or len(set(domains)) != len(domains):
        raise RuntimeError(f"expected 300 unique inputs, found {len(cases)} rows / {len(set(domains))} domains")
    return cases


def leaf(raw: dict[str, Any]) -> dict[str, Any]:
    return raw.get("funding_enrichment") or raw.get("match") or raw


def body(raw: dict[str, Any]) -> dict[str, Any]:
    value = leaf(raw).get("response")
    return value if isinstance(value, dict) else {}


def status_for(provider: str, raw: dict[str, Any]) -> tuple[str, str | None]:
    current = leaf(raw)
    code = current.get("http_status")
    if not isinstance(code, int):
        return "error", raw.get("error") or "response missing HTTP status"
    if code == 429:
        return "rate_limited", "HTTP 429"
    if code == 404:
        return "not_found", "HTTP 404"
    if code >= 500:
        return "server_error", f"HTTP {code}"
    if code < 200 or code >= 300:
        return "http_error", f"HTTP {code}"
    response = body(raw)
    if provider == "fiber" and not ((response.get("output") or {}).get("data") or []):
        return "not_found", ((response.get("output") or {}).get("message") or "no company match")
    if provider == "predictleads" and not response.get("data"):
        return "not_found", "no financing events"
    if provider == "apollo" and not response.get("organization"):
        return "not_found", "no organization"
    if provider == "people-data-labs" and not response.get("id"):
        return "not_found", "no company"
    if provider == "ocean" and not response.get("domain"):
        return "not_found", "no company"
    if provider == "explorium" and not response.get("data"):
        return "not_found", "no funding enrichment"
    if provider == "company-enrich" and not response.get("id"):
        return "not_found", "no company"
    adapter = ADAPTERS.get(provider)
    if adapter:
        reason = adapter.not_found_reason(response)
        if reason:
            return "not_found", reason
    return "ok", None


def first_value(value: Any) -> Any:
    if isinstance(value, dict):
        return value.get("value") if "value" in value else value
    return value


def latest_round(rounds: Any, *, date_key: str, stage_key: str, amount_key: str) -> dict[str, Any]:
    rows = [row for row in rounds or [] if isinstance(row, dict)]
    rows.sort(key=lambda row: str(row.get(date_key) or ""), reverse=True)
    row = rows[0] if rows else {}
    return {
        "latest_stage": row.get(stage_key),
        "latest_date": row.get(date_key),
        "latest_amount": row.get(amount_key),
        "round_count": len(rows),
    }


def normalize(provider: str, raw: dict[str, Any]) -> dict[str, Any]:
    response = body(raw)
    if provider == "fiber":
        item = ((response.get("output") or {}).get("data") or [{}])[0]
        rounds = item.get("full_funding_rounds") or item.get("funding_rounds") or []
        result = latest_round(rounds, date_key="round_date", stage_key="round_type", amount_key="round_raised_usd")
        if not result["latest_stage"]:
            result["latest_stage"] = first_value(item.get("latest_funding_consensus")) or item.get("funding_stage")
        result["total_raised"] = first_value(item.get("total_funding_consensus"))
        return result
    if provider == "predictleads":
        records = [row.get("attributes") or {} for row in response.get("data") or [] if isinstance(row, dict)]
        records.sort(key=lambda row: str(row.get("effective_date") or row.get("found_at") or ""), reverse=True)
        record = records[0] if records else {}
        return {"latest_stage": record.get("financing_type_normalized") or record.get("financing_type"), "latest_date": record.get("effective_date"), "latest_date_observed_at": record.get("found_at"), "latest_amount": record.get("amount_normalized") or record.get("amount"), "total_raised": None, "round_count": len(records)}
    if provider == "apollo":
        item = response.get("organization") or {}
        return {"latest_stage": item.get("latest_funding_stage"), "latest_date": item.get("latest_funding_round_date"), "latest_amount": None, "total_raised": item.get("total_funding"), "round_count": len(item.get("funding_events") or [])}
    if provider == "people-data-labs":
        return {"latest_stage": response.get("latest_funding_stage"), "latest_date": response.get("last_funding_date"), "latest_amount": None, "total_raised": response.get("total_funding_raised"), "round_count": response.get("number_funding_rounds")}
    if provider == "ocean":
        item = response.get("fundingRound") or {}
        return {"latest_stage": item.get("type"), "latest_date": item.get("date"), "latest_amount": item.get("moneyRaisedInUsd"), "total_raised": None, "round_count": None}
    if provider == "explorium":
        item = (((response.get("data") or [{}])[0].get("data") or {}) if response else {})
        return {"latest_stage": item.get("last_funding_round_type"), "latest_date": item.get("last_funding_round_date"), "latest_amount": item.get("last_funding_round_value_usd"), "total_raised": item.get("known_funding_total_value"), "round_count": item.get("number_of_funding_rounds")}
    if provider == "company-enrich":
        item = response.get("financial") or {}
        rounds = item.get("funding") or []
        result = latest_round(rounds, date_key="date", stage_key="type", amount_key="amount")
        result["latest_stage"] = result["latest_stage"] or item.get("funding_stage")
        result["latest_date"] = result["latest_date"] or item.get("funding_date")
        result["total_raised"] = item.get("total_funding")
        return result
    adapter = ADAPTERS.get(provider)
    if adapter:
        return adapter.normalize(response)
    raise KeyError(provider)


def total_latency_ms(raw: dict[str, Any]) -> int | None:
    if "funding_enrichment" in raw:
        return sum(value.get("latency_ms", 0) for value in raw.values() if isinstance(value, dict))
    value = leaf(raw).get("latency_ms")
    return int(value) if isinstance(value, (int, float)) else None


def record(case: dict[str, str], provider: str, raw: dict[str, Any], *, source: str) -> dict[str, Any]:
    status, reason = status_for(provider, raw)
    return {
        "provider": provider,
        "case_slug": case["candidate_id"],
        "input": {"company_name": case["company_name"], "domain": case["company_domain"]},
        "source": source,
        "completed_at": now_iso(),
        "status": status,
        "failure_reason": reason,
        "latency_ms": total_latency_ms(raw),
        "normalized": normalize(provider, raw) if status == "ok" else {},
        "funding_related_paths": funding_paths(body(raw))[:100],
        "raw": raw,
    }


def retryable(path: Path, retry_statuses: set[str], retry_dns_errors: bool) -> bool:
    if not path.exists():
        return True
    try:
        existing = json.loads(path.read_text())
        if retry_dns_errors and existing.get("status") == "error":
            reason = str(existing.get("failure_reason") or "")
            return "NameResolutionError" in reason or "Network is unreachable" in reason
        return bool(retry_statuses) and existing.get("status") in retry_statuses
    except json.JSONDecodeError:
        return True


def import_smoke(cases: list[dict[str, str]]) -> int:
    if not SMOKE_OUTPUT.exists():
        return 0
    smoke = json.loads(SMOKE_OUTPUT.read_text()).get("results") or {}
    by_domain = {case["company_domain"]: case for case in cases}
    imported = 0
    for provider in PROVIDERS:
        for domain in SMOKE_DOMAINS:
            case = by_domain.get(domain)
            source = smoke.get(f"{provider}:{domain}") or {}
            raw = source.get("raw")
            path = cell_path(provider, domain)
            if case and isinstance(raw, dict) and not path.exists():
                atomic_json(path, record(case, provider, raw, source="smoke_reused"))
                imported += 1
    return imported


def run_provider(provider: str, cases: list[dict[str, str]], retry_statuses: set[str], retry_dns_errors: bool, dry_run: bool) -> dict[str, int]:
    required, call = PROVIDERS[provider]
    missing = [key for key in required if not os.environ.get(key)]
    if missing and not dry_run:
        raise RuntimeError(f"{provider}: missing environment variables: {', '.join(missing)}")
    counts = {"skipped": 0, "attempted": 0, "ok": 0, "not_found": 0, "failed": 0}
    last_started = 0.0
    for case in cases:
        path = cell_path(provider, case["company_domain"])
        if not retryable(path, retry_statuses, retry_dns_errors):
            counts["skipped"] += 1
            continue
        if dry_run:
            counts["attempted"] += 1
            continue
        delay = MIN_START_INTERVAL_SECONDS.get(provider, 0) - (time.monotonic() - last_started)
        if delay > 0:
            time.sleep(delay)
        last_started = time.monotonic()
        try:
            raw = call(case["company_domain"])
            row = record(case, provider, raw, source="live_api")
        except Exception as exc:
            row = {"provider": provider, "case_slug": case["candidate_id"], "input": {"company_name": case["company_name"], "domain": case["company_domain"]}, "source": "live_api", "completed_at": now_iso(), "status": "error", "failure_reason": f"{type(exc).__name__}: {exc}", "latency_ms": None, "normalized": {}, "funding_related_paths": [], "raw": None}
        if path.exists():
            previous = json.loads(path.read_text())
            row["prior_attempts"] = [*previous.get("prior_attempts", []), previous]
        atomic_json(path, row)
        counts["attempted"] += 1
        if row["status"] == "ok": counts["ok"] += 1
        elif row["status"] == "not_found": counts["not_found"] += 1
        else: counts["failed"] += 1
    return counts


def write_summary(cases: list[dict[str, str]]) -> dict[str, Any]:
    providers: dict[str, Any] = {}
    for provider in PROVIDERS:
        rows = []
        for case in cases:
            path = cell_path(provider, case["company_domain"])
            if path.exists():
                rows.append(json.loads(path.read_text()))
        statuses: dict[str, int] = {}
        for row in rows:
            statuses[row["status"]] = statuses.get(row["status"], 0) + 1
        latencies = sorted(row["latency_ms"] for row in rows if row.get("latency_ms") is not None)
        providers[provider] = {"completed": len(rows), "status_counts": statuses, "median_latency_ms": latencies[len(latencies) // 2] if latencies else None}
    summary = {"updated_at": now_iso(), "case_count": len(cases), "providers": providers}
    atomic_json(SUMMARY, summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", help="comma-separated providers")
    parser.add_argument("--retry-status", help="explicit statuses to retry, e.g. rate_limited,server_error")
    parser.add_argument("--retry-dns-errors", action="store_true", help="retry only saved NameResolutionError/network-unreachable cells; preserves prior attempt records")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--confirm-paid", action="store_true", help="required before any live provider API call")
    args = parser.parse_args()
    load_environment()
    selected = {value.strip() for value in args.only.split(",")} if args.only else set(PROVIDERS)
    unknown = selected - set(PROVIDERS)
    if unknown:
        parser.error(f"unknown providers: {', '.join(sorted(unknown))}")
    retry_statuses = {value.strip() for value in (args.retry_status or "").split(",") if value.strip()}
    cases = load_cases()
    if not args.dry_run and not args.confirm_paid:
        parser.error("live provider calls require --confirm-paid")
    if not args.dry_run:
        imported = import_smoke(cases)
        atomic_json(MANIFEST, {"version": "v1", "input": str(INPUT), "case_count": len(cases), "providers": sorted(PROVIDERS), "smoke_cells_reused": imported, "started_at": now_iso(), "automatic_retries": False})
        print(f"reused {imported} completed smoke cells; no paid repeat calls")
    with ThreadPoolExecutor(max_workers=len(selected)) as pool:
        futures = {pool.submit(run_provider, provider, cases, retry_statuses, args.retry_dns_errors, args.dry_run): provider for provider in selected}
        for future in as_completed(futures):
            provider = futures[future]
            result = future.result()
            print(provider, json.dumps(result, sort_keys=True))
    if not args.dry_run:
        print(json.dumps(write_summary(cases), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
