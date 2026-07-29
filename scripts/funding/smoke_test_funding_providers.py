#!/usr/bin/env python3
"""Run one small, resumable funding-contract smoke test per provider/domain.

Each provider receives the same two domain-only inputs.  Raw API responses are
stored locally for adapter work; no credentials or request headers are written.
Use --force only when deliberately paying to repeat a completed smoke call.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "data" / "funding" / "provider-smoke-v1.json"
DOMAINS = ("airwallex.com", "acuitymd.com")
TIMEOUT_SECONDS = 45


def load_environment() -> None:
    # This public runner only reads credentials the user places in this repo.
    for path in (ROOT / ".env.local", ROOT / ".env"):
        if path.exists():
            load_dotenv(path, override=False)


def response_body(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return {"non_json_body": response.text[:2000]}


def request(method: str, url: str, **kwargs: Any) -> dict[str, Any]:
    started = time.perf_counter()
    response = requests.request(method, url, timeout=TIMEOUT_SECONDS, **kwargs)
    body = response_body(response)
    return {
        "http_status": response.status_code,
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "response": body,
        "rate_limit": {
            key.lower(): value
            for key, value in response.headers.items()
            if key.lower() in {"retry-after", "x-ratelimit-limit", "x-ratelimit-remaining", "ratelimit-limit", "ratelimit-remaining", "x-call-credits-spent"}
        },
    }


def fiber(domain: str) -> dict[str, Any]:
    return request(
        "POST", "https://api.fiber.ai/v1/company-search",
        json={
            "apiKey": os.environ["FIBER_API_KEY"],
            "searchParams": {
                "exactCompanyV2": {"anyOf": [{"identifier": "domain", "domain": domain}]}
            },
        },
    )


def predictleads(domain: str) -> dict[str, Any]:
    return request(
        "GET", f"https://predictleads.com/api/v3/companies/{domain}/financing_events",
        headers={"X-Api-Key": os.environ["PREDICT_LEADS_API_KEY"], "X-Api-Token": os.environ["PREDICT_LEADS_API_TOKEN"], "Accept": "application/json"},
    )


def apollo(domain: str) -> dict[str, Any]:
    return request(
        "GET", "https://api.apollo.io/api/v1/organizations/enrich",
        headers={"x-api-key": os.environ["APOLLO_API_KEY"], "Accept": "application/json"},
        params={"domain": domain},
    )


def people_data_labs(domain: str) -> dict[str, Any]:
    return request(
        "GET", "https://api.peopledatalabs.com/v5/company/enrich",
        headers={"X-Api-Key": os.environ["PEOPLE_DATA_LABS_API_KEY"], "Accept": "application/json"},
        params={"website": domain},
    )


def ocean(domain: str) -> dict[str, Any]:
    fields = ["domain", "name", "fundingRound", "fundingRound.date", "fundingRound.type", "fundingRound.moneyRaisedInUsd", "rootUrl"]
    return request(
        "POST", "https://api.ocean.io/v2/enrich/company",
        headers={"X-Api-Token": os.environ["OCEAN_API_KEY"], "Accept": "application/json", "Content-Type": "application/json"},
        json={"company": {"domain": domain}, "fields": fields},
    )


def explorium(domain: str) -> dict[str, Any]:
    headers = {"api_key": os.environ["EXPLORIUM_API_KEY"], "Content-Type": "application/json"}
    matched = request(
        "POST", "https://api.explorium.ai/v1/businesses/match", headers=headers,
        json={"businesses_to_match": [{"domain": domain}]},
    )
    if not 200 <= matched["http_status"] < 300:
        return {"match": matched}
    body = matched["response"]
    candidates = body.get("matched_businesses", []) if isinstance(body, dict) else []
    business_id = next((item.get("business_id") or item.get("businessId") for item in candidates if isinstance(item, dict)), None)
    if not business_id:
        return {"match": matched, "error": "match returned no business_id"}
    enriched = request(
        "POST", "https://api.explorium.ai/v1/businesses/funding_and_acquisition/bulk_enrich",
        headers=headers, json={"business_ids": [business_id]},
    )
    return {"match": matched, "funding_enrichment": enriched}


def company_enrich(domain: str) -> dict[str, Any]:
    return request(
        "GET", "https://api.companyenrich.com/companies/enrich",
        headers={"Authorization": f"Bearer {os.environ['COMPANY_ENRICH_API_KEY']}", "Accept": "application/json"},
        params={"domain": domain},
    )


def fundable(domain: str) -> dict[str, Any]:
    return request(
        "GET", "https://www.tryfundable.ai/api/v1/company",
        headers={"Authorization": f"Bearer {os.environ['FUNDABLE_API_KEY']}", "Accept": "application/json"},
        params={"domain": domain},
    )


PROVIDERS: dict[str, tuple[tuple[str, ...], Callable[[str], dict[str, Any]]]] = {
    "fiber": (("FIBER_API_KEY",), fiber),
    "predictleads": (("PREDICT_LEADS_API_KEY", "PREDICT_LEADS_API_TOKEN"), predictleads),
    "apollo": (("APOLLO_API_KEY",), apollo),
    "people-data-labs": (("PEOPLE_DATA_LABS_API_KEY",), people_data_labs),
    "ocean": (("OCEAN_API_KEY",), ocean),
    "explorium": (("EXPLORIUM_API_KEY",), explorium),
    "company-enrich": (("COMPANY_ENRICH_API_KEY",), company_enrich),
    "fundable": (("FUNDABLE_API_KEY",), fundable),
}


def funding_paths(value: Any, path: str = "$") -> list[str]:
    hits: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if any(term in key.lower() for term in ("fund", "round", "investment", "amount", "raised", "valuation")):
                hits.append(child_path)
            hits.extend(funding_paths(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value[:5]):
            hits.extend(funding_paths(child, f"{path}[{index}]"))
    return hits


def summarize(result: dict[str, Any]) -> dict[str, Any]:
    leaf = result.get("funding_enrichment") or result
    if "http_status" not in leaf:
        leaf = result.get("match", {})
    raw = (result.get("funding_enrichment") or result).get("response") if isinstance(result.get("funding_enrichment") or result, dict) else None
    return {
        "http_status": leaf.get("http_status"),
        "latency_ms": sum(v.get("latency_ms", 0) for v in result.values() if isinstance(v, dict) and "latency_ms" in v) if "funding_enrichment" in result else leaf.get("latency_ms"),
        "funding_related_paths": funding_paths(raw)[:50],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="repeat already-recorded calls")
    parser.add_argument("--only", help="comma-separated provider slugs; useful for a targeted retry")
    args = parser.parse_args()
    load_environment()
    selected = {value.strip() for value in args.only.split(",")} if args.only else set(PROVIDERS)
    unknown = selected - set(PROVIDERS)
    if unknown:
        parser.error(f"unknown providers: {', '.join(sorted(unknown))}")
    prior = json.loads(OUTPUT.read_text()) if OUTPUT.exists() else {"results": {}}
    results: dict[str, Any] = prior.setdefault("results", {})
    pending = []
    for slug, (required, fn) in PROVIDERS.items():
        if slug not in selected:
            continue
        for domain in DOMAINS:
            key = f"{slug}:{domain}"
            if key in results and not args.force:
                continue
            missing = [name for name in required if not os.environ.get(name)]
            if missing:
                results[key] = {"provider": slug, "domain": domain, "error": f"missing env: {', '.join(missing)}"}
            else:
                pending.append((key, slug, domain, fn))
    with ThreadPoolExecutor(max_workers=7) as pool:
        futures = {pool.submit(fn, domain): (key, slug, domain) for key, slug, domain, fn in pending}
        for future in as_completed(futures):
            key, slug, domain = futures[future]
            try:
                raw = future.result()
                results[key] = {"provider": slug, "domain": domain, "summary": summarize(raw), "raw": raw}
            except Exception as exc:  # keep the smoke matrix resumable
                results[key] = {"provider": slug, "domain": domain, "error": f"{type(exc).__name__}: {exc}"}
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(prior, indent=2) + "\n")
    for key in sorted(results):
        row = results[key]
        print(key, json.dumps(row.get("summary") or {"error": row.get("error")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
