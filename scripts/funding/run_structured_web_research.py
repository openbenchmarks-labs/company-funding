#!/usr/bin/env python3
"""Run resumable, structured funding research for the web-research providers.

Every completed API response is immediately written to a per-company JSON file.
Re-running with the default --resume mode never calls a provider for a saved
successful result. The raw response and wall-clock latency are retained for
auditability and later benchmark scoring.

Parallel is measured through two endpoints, as two separate benchmark providers:
``parallel`` (Task API) and ``parallel-responses-medium`` (Responses API at
medium reasoning effort). They share this module's OUTPUT_SCHEMA and
instruction() verbatim, so the endpoint is the only variable between them. Keep
it that way -- see scripts/funding/test_parallel_responses_contract.py, which
asserts the parity.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[2]
INPUT = ROOT / "data" / "funding" / "company-funding-benchmark-inputs-v1.csv"
RAW_ROOT = ROOT / "data" / "funding" / "provider-runs-v2" / "raw"
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "latest_stage": {"type": ["string", "null"], "description": "Most recent funding stage; null if unavailable."},
        "latest_announced_on": {"type": ["string", "null"], "description": "Most recent funding announcement date in YYYY-MM-DD; null if unavailable."},
        "latest_amount": {"type": ["integer", "null"], "description": "Latest round amount in whole major-currency units, never millions or billions; null if undisclosed."},
        "currency": {"type": ["string", "null"], "description": "ISO 4217 currency of latest_amount; null if unavailable."},
        "total_raised": {"type": ["integer", "null"], "description": "Total funding raised in whole major-currency units; null if unavailable."},
        "funding_round_count": {"type": ["integer", "null"], "description": "Number of funding rounds; null if unavailable."},
    },
    "required": ["latest_stage", "latest_announced_on", "latest_amount", "currency", "total_raised", "funding_round_count"],
    "additionalProperties": False,
}
# Parallel bills Responses by reasoning effort: $10/$50/$250 per 1,000 requests
# for low/medium/high. Only the medium arm is measured; the high arm was
# published on 2026-08-15 and withdrawn on 2026-08-17.
RESPONSES_REASONING_EFFORT = "medium"

# Firecrawl bills dynamic credits per agent run and defaults maxCredits to 2,500.
# That default is a per-run ceiling, so across a 300-domain cohort it is a
# runaway rather than a budget. Set it explicitly and low; the response reports
# creditsUsed, which is what the pricing row is derived from.
FIRECRAWL_MODEL = "spark-2"
FIRECRAWL_MAX_CREDITS = 1500
# A 10-case smoke run measured a 136s median, and two runs exceeded the 180s
# default and were recorded as timeouts that were ours, not the vendor's.
# The credit ceiling moved 500 -> 1500 for the same reason: on the full cohort
# roughly 3% of companies needed more than 500 and were being recorded as
# vendor refusals when they were our budget cutoff.
FIRECRAWL_TIMEOUT_S = 600
FIRECRAWL_POLL_ATTEMPTS = 120

# Seltz runs as two providers that differ only in search scope, the same way
# Parallel runs as two endpoints. Keep query, response_format and system_prompt
# identical between them; see test_seltz_scope_contract.py.
SELTZ_SCOPES = ("companies", "news")

# Exa is measured on two search types with the same query and schema, so the
# search type is the only variable; see test_exa_search_type_contract.py.
EXA_DEFAULT_SEARCH_TYPE = "deep-reasoning"
EXA_SEARCH_TYPES = ("deep-reasoning", "instant")

# Exa's Agent API is a third, separate arm: an agent that researches at request
# time rather than a search call. Priced per request by effort, and the effort
# MUST be pinned. The default "auto" is metered up to a $5 per-run ceiling, so
# leaving it unset risks $1,500 across a 300-domain cohort instead of $30.
# Fixed-effort pricing: minimal $0.012, low $0.025, medium $0.10, high $0.50,
# xhigh $1.00 per request.
EXA_AGENT_EFFORT = "medium"
# No budget is sent. The API rejects one on a fixed effort with "budget is
# currently supported only for metered efforts", and pinning the effort is
# itself the cost control: medium is a flat $0.10 per request.
EXA_AGENT_POLL_ATTEMPTS = 120
EXA_AGENT_TIMEOUT_S = 600
SELTZ_SYSTEM_PROMPT = (
    "You are a funding data extraction service. Answer only with a single JSON object "
    "matching this schema, and nothing else: "
    + json.dumps(OUTPUT_SCHEMA)
    + " Amounts are whole major-currency units, never millions or billions. "
    "Use null for any field you cannot verify. Do not infer."
)


def now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def instruction(case: dict[str, str]) -> str:
    return (
        f"Research {case['company_name']} ({case['company_domain']}) and identify its most recent funding event. "
        "Use company, investor, or company-issued wire sources where possible. "
        "Do not infer unknown fields; return null."
    )


RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 4


def request_json(url: str, headers: dict[str, str], payload: dict[str, Any] | None = None, timeout: int = 180) -> dict[str, Any]:
    """POST/GET with backoff on rate limits and transient server errors.

    Without this a 429 is recorded as a permanent per-case failure. That is
    wrong twice over: the cell looks like a vendor miss when it is our own
    request rate, and on a monthly cycle across eleven vendors it silently
    eats coverage. Exa returned 429 on 20 of 47 concurrent calls, which is
    what prompted this.

    Transport-level failures are retried for the same reason. A connection reset
    or a DNS blip is our network, not a vendor answer, and one lost a cell on the
    Exa Agent freshness run with "Connection reset by peer". HTTPError subclasses
    URLError, so it has to be caught first or every 4xx would be retried.

    Honours Retry-After when the server sends it, otherwise exponential.
    """
    body = json.dumps(payload).encode() if payload is not None else None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        request = Request(url, data=body, headers=headers, method="POST" if payload is not None else "GET")
        try:
            with urlopen(request, timeout=timeout) as response:
                return json.loads(response.read())
        except HTTPError as error:
            if error.code not in RETRY_STATUSES or attempt == MAX_ATTEMPTS:
                raise
            retry_after = error.headers.get("Retry-After") if error.headers else None
            try:
                delay = float(retry_after) if retry_after else 2 ** attempt
            except ValueError:
                delay = 2 ** attempt
            time.sleep(min(delay, 60))
        except (URLError, TimeoutError, ConnectionError) as error:
            if attempt == MAX_ATTEMPTS:
                raise
            del error
            time.sleep(min(2 ** attempt, 60))
    raise RuntimeError("unreachable")


def parallel(case: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
    headers = {"x-api-key": os.environ["PARALLEL_API_KEY"], "Content-Type": "application/json"}
    payload = {
        "input": {"company_name": case["company_name"], "company_website": case["company_domain"], "instruction": instruction(case)},
        "processor": "core",
        "task_spec": {
            "input_schema": {"type": "json", "json_schema": {"type": "object", "properties": {"company_name": {"type": "string"}, "company_website": {"type": "string"}, "instruction": {"type": "string"}}, "required": ["company_name", "company_website", "instruction"]}},
            "output_schema": {"type": "json", "json_schema": OUTPUT_SCHEMA},
        },
    }
    created = request_json("https://api.parallel.ai/v1/tasks/runs", headers, payload)
    run_id = created["run_id"]
    for _ in range(36):
        try:
            result = request_json(f"https://api.parallel.ai/v1/tasks/runs/{run_id}/result", headers)
        except HTTPError as error:
            if error.code not in {202, 404}:
                raise
            result = None
        if result and (result.get("status") in {"completed", "succeeded"} or result.get("output")):
            return result["output"]["content"], {"created": created, "result": result}
        time.sleep(5)
    raise TimeoutError(f"Parallel Task {run_id} did not complete within three minutes")


def parallel_responses(case: dict[str, str], effort: str = RESPONSES_REASONING_EFFORT) -> tuple[dict[str, Any], dict[str, Any]]:
    """Same instruction and schema as parallel(); only the endpoint differs.

    Parallel's docs specify ``x-api-key`` for this endpoint, but every adapter
    already working against it in this repo authenticates with a bearer token
    and Parallel accepts both. Match the proven code rather than the docs.
    """
    headers = {"Authorization": f"Bearer {os.environ['PARALLEL_API_KEY']}", "Content-Type": "application/json"}
    payload = {
        "model": "parallel",
        "input": instruction(case),
        "reasoning": {"effort": effort},
        "text": {"format": {"type": "json_schema", "name": "funding_event", "schema": OUTPUT_SCHEMA}},
    }
    body = request_json("https://api.parallel.ai/v1/responses", headers, payload)
    text, citations = responses_output(body)
    if text is None:
        raise ValueError("Parallel Responses returned no output text")
    try:
        normalized = json.loads(text)
    except json.JSONDecodeError:
        # A schema-enforced request that answers in prose is itself the result.
        # Record it verbatim rather than regex-salvaging a score out of it.
        normalized = {"unparsed_text": text}
    # Citations live in the raw envelope, never in ``normalized``: the scored
    # schema has to stay byte-identical to the Task API arm's.
    return normalized, {"response": body, "response_id": body.get("id"), "reasoning_effort": effort, "sources": citations}


def responses_output(body: dict[str, Any]) -> tuple[str | None, list[str]]:
    """Lift the output text and URL citations out of a Responses envelope."""
    text: str | None = None
    citations: list[str] = []
    for item in body.get("output") or []:
        if not isinstance(item, dict):
            continue
        for chunk in item.get("content") or []:
            if not isinstance(chunk, dict):
                continue
            if text is None and isinstance(chunk.get("text"), str):
                text = chunk["text"]
            for annotation in chunk.get("annotations") or []:
                if isinstance(annotation, dict) and isinstance(annotation.get("url"), str):
                    citations.append(annotation["url"])
    return text, list(dict.fromkeys(citations))


def firecrawl(case: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Firecrawl Agent, same instruction and schema as every other arm.

    The endpoint is synchronous by default but may hand back a job id under
    load, so the polling path exists even though it is usually unused.
    """
    headers = {"Authorization": f"Bearer {os.environ['FIRECRAWL_API_KEY']}", "Content-Type": "application/json"}
    payload = {
        "prompt": instruction(case),
        "model": FIRECRAWL_MODEL,
        "schema": OUTPUT_SCHEMA,
        "maxCredits": FIRECRAWL_MAX_CREDITS,
    }
    body = request_json("https://api.firecrawl.dev/v2/agent", headers, payload, timeout=FIRECRAWL_TIMEOUT_S)
    job_id = body.get("id") or body.get("jobId")
    for _ in range(FIRECRAWL_POLL_ATTEMPTS):
        status = body.get("status")
        if status in {"failed", "cancelled"}:
            raise ValueError(f"Firecrawl agent {status}: {body.get('error') or body.get('message')}")
        if status == "completed" or body.get("data") is not None:
            break
        if not job_id:
            raise ValueError(f"Firecrawl returned no data and no job id (status {status!r})")
        time.sleep(5)
        try:
            body = request_json(f"https://api.firecrawl.dev/v2/agent/{job_id}", headers)
        except HTTPError as error:
            # The job is not always queryable the instant its id comes back.
            # A 404 here is a race, not a missing job, and treating it as fatal
            # records a vendor failure for a run that is still executing.
            if error.code != 404:
                raise
            continue
    data = body.get("data")
    if data is None:
        raise TimeoutError(
            f"Firecrawl agent did not return data within {FIRECRAWL_TIMEOUT_S}s"
        )
    # Some agent responses nest the object under the schema name rather than
    # returning it flat. Unwrap only when the flat shape is clearly absent.
    if isinstance(data, dict) and "latest_stage" not in data and len(data) == 1:
        inner = next(iter(data.values()))
        if isinstance(inner, dict):
            data = inner
    return data, {"response": body, "credits_used": body.get("creditsUsed"), "model": FIRECRAWL_MODEL}


def seltz_payload(case: dict[str, str], scope: str, json_schema: bool) -> dict[str, Any]:
    """Request body for one Seltz arm. Scope is the only per-arm difference."""
    return {
        "query": instruction(case),
        "scope": scope,
        "system_prompt": SELTZ_SYSTEM_PROMPT,
        "response_format": (
            {"type": "json_schema", "json_schema": {"name": "funding_event", "schema": OUTPUT_SCHEMA}}
            if json_schema
            else {"type": "json_object"}
        ),
    }


def parse_json_answer(text: str) -> dict[str, Any]:
    """Seltz returns Markdown, not an object, so the JSON has to be lifted out.

    Unlike the schema-enforced arms there is no guarantee the answer is JSON at
    all. A prose answer is recorded verbatim as unparsed_text rather than
    regex-salvaged into a score, matching the Responses arm.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1] if "\n" in stripped else stripped
        if stripped.endswith("```"):
            stripped = stripped[: stripped.rindex("```")]
        stripped = stripped.strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        start, end = stripped.find("{"), stripped.rfind("}")
        if start == -1 or end <= start:
            return {"unparsed_text": text}
        try:
            parsed = json.loads(stripped[start : end + 1])
        except json.JSONDecodeError:
            return {"unparsed_text": text}
    return parsed if isinstance(parsed, dict) else {"unparsed_text": text}


def seltz(case: dict[str, str], scope: str = "companies") -> tuple[dict[str, Any], dict[str, Any]]:
    """Seltz Answer API for one scope.

    The docs only show response_format {"type": "json_object"}; json_schema is
    attempted first because it is strictly stronger, and the mode that actually
    worked is recorded on the envelope so the board can disclose that this
    vendor may not be schema-enforced the way the others are.
    """
    headers = {"x-api-key": os.environ["SELTZ_API_KEY"], "Content-Type": "application/json"}
    mode = "json_schema"
    try:
        body = request_json("https://api.seltz.ai/v1/answer", headers, seltz_payload(case, scope, True))
    except HTTPError as error:
        if error.code not in {400, 404, 415, 422}:
            raise
        mode = "json_object"
        body = request_json("https://api.seltz.ai/v1/answer", headers, seltz_payload(case, scope, False))
    answer = body.get("answer")
    if not isinstance(answer, str):
        raise ValueError("Seltz returned no answer text")
    citations = [c.get("url") for c in (body.get("citations") or []) if isinstance(c, dict) and c.get("url")]
    # Citations stay on the envelope, never in normalized: the scored schema has
    # to stay identical across every provider on the board.
    return parse_json_answer(answer), {
        "response": body,
        "scope": scope,
        "response_format_mode": mode,
        "sources": list(dict.fromkeys(citations)),
    }


def exa_payload(case: dict[str, str], search_type: str) -> dict[str, Any]:
    """Request body for one Exa arm. Search type is the only per-arm difference."""
    return {"query": instruction(case), "type": search_type, "output_schema": OUTPUT_SCHEMA}


def exa(case: dict[str, str], search_type: str = EXA_DEFAULT_SEARCH_TYPE) -> tuple[dict[str, Any], dict[str, Any]]:
    """Exa search for one search type.

    Measured as two providers so the accuracy cost of Exa's cheap path is
    visible rather than asserted. The raw envelope stays the bare response so
    the deep-reasoning cells already on disk keep their shape; the request type
    rides along under a namespaced key that cannot collide with Exa's fields.
    """
    headers = {"x-api-key": os.environ["EXA_API_KEY"], "Content-Type": "application/json"}
    response = request_json("https://api.exa.ai/search", headers, exa_payload(case, search_type))
    output = response.get("output")
    if not isinstance(output, dict) or "content" not in output:
        raise ValueError(
            f"Exa {search_type} returned no structured output "
            f"(keys: {sorted(response)[:8]})"
        )
    return output["content"], {**response, "_request_type": search_type}


def exa_agent_payload(case: dict[str, str], effort: str) -> dict[str, Any]:
    """Request body for the Exa Agent arm.

    Same instruction and same output schema as every other provider on the
    board, so what is being measured is the agent, not a different question.
    Note ``outputSchema`` here against ``output_schema`` on /search: the two Exa
    endpoints spell it differently.
    """
    return {
        "query": instruction(case),
        "effort": effort,
        "outputSchema": OUTPUT_SCHEMA,
    }


def exa_agent(case: dict[str, str], effort: str = EXA_AGENT_EFFORT) -> tuple[dict[str, Any], dict[str, Any]]:
    """Exa Agent API: an agent that researches each company at request time.

    Async by design. The POST returns an id with status ``queued`` and the run
    is polled to a terminal state, unlike /search which answers inline. Auth
    also differs: this endpoint takes a bearer token while /search takes
    ``x-api-key``.
    """
    headers = {
        "Authorization": f"Bearer {os.environ['EXA_API_KEY']}",
        "Content-Type": "application/json",
    }
    body = request_json(
        "https://api.exa.ai/agent/runs",
        headers,
        exa_agent_payload(case, effort),
        timeout=EXA_AGENT_TIMEOUT_S,
    )
    run_id = body.get("id")
    for _ in range(EXA_AGENT_POLL_ATTEMPTS):
        status = body.get("status")
        if status in {"failed", "cancelled"}:
            raise ValueError(f"Exa agent run {status}: {body.get('error') or body.get('message')}")
        if status == "completed":
            break
        if not run_id:
            raise ValueError(f"Exa agent returned no run id (status {status!r})")
        time.sleep(5)
        body = request_json(
            f"https://api.exa.ai/agent/runs/{run_id}", headers, timeout=EXA_AGENT_TIMEOUT_S
        )
    output = body.get("output") or {}
    structured = output.get("structured") if isinstance(output, dict) else None
    if not isinstance(structured, dict):
        # A terminal run that produced prose instead of schema is itself the
        # result. Record it rather than salvaging a score out of the text.
        text = output.get("text") if isinstance(output, dict) else None
        if body.get("status") != "completed":
            raise TimeoutError(
                f"Exa agent run did not complete within {EXA_AGENT_TIMEOUT_S}s"
            )
        if not isinstance(text, str):
            raise ValueError("Exa agent returned neither structured output nor text")
        structured = {"unparsed_text": text}
    # Grounding and cost stay on the envelope; the scored schema has to remain
    # identical across every provider on the board.
    return structured, {
        "response": body,
        "run_id": run_id,
        "effort": effort,
        "cost_dollars": body.get("costDollars"),
        "usage": body.get("usage"),
    }


PROVIDERS = {
    "exa": exa,
    "exa-instant": partial(exa, search_type="instant"),
    "exa-agent": exa_agent,
    "parallel": parallel,
    "parallel-responses-medium": parallel_responses,
    "firecrawl": firecrawl,
    "seltz-companies": partial(seltz, scope="companies"),
    "seltz-news": partial(seltz, scope="news"),
}
REQUIRED_ENV = {
    "exa": "EXA_API_KEY", "exa-instant": "EXA_API_KEY", "exa-agent": "EXA_API_KEY",
    "parallel": "PARALLEL_API_KEY",
    "parallel-responses-medium": "PARALLEL_API_KEY",
    "firecrawl": "FIRECRAWL_API_KEY",
    "seltz-companies": "SELTZ_API_KEY", "seltz-news": "SELTZ_API_KEY",
}
DEFAULT_CONCURRENCY = {
    "exa": 12, "exa-instant": 12, "parallel": 8, "parallel-responses-medium": 8,
    # Agentic search runs are long and metered; keep these low until a smoke
    # test shows what each vendor tolerates.
    "exa-agent": 4, "firecrawl": 4, "seltz-companies": 6, "seltz-news": 6,
}


def output_path(provider: str, case: dict[str, str], raw_dir: Path | None = None) -> Path:
    return (raw_dir or RAW_ROOT / provider) / f"{case['company_domain']}.json"


def atomic_write(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(row, indent=2) + "\n")
    temp.replace(path)


def run_one(provider: str, case: dict[str, str], raw_dir: Path | None = None) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        normalized, raw = PROVIDERS[provider](case)
        row = {
            "provider": provider,
            "case_slug": case["candidate_id"],
            "input": {"company_name": case["company_name"], "domain": case["company_domain"]},
            "source": "live_api",
            "completed_at": now(),
            "status": "ok",
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "normalized": normalized,
            "raw": raw,
        }
    except Exception as error:  # Persist failures too, so their cause is auditable.
        row = {
            "provider": provider,
            "case_slug": case["candidate_id"],
            "input": {"company_name": case["company_name"], "domain": case["company_domain"]},
            "source": "live_api",
            "completed_at": now(),
            "status": "error",
            "failure_reason": f"{type(error).__name__}: {error}",
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "normalized": {},
        }
    atomic_write(output_path(provider, case, raw_dir), row)
    return row


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("provider", choices=sorted(PROVIDERS))
    # The cohort is no longer a single frozen 300-domain file. Freshness
    # snapshots are their own cohorts of their own size, so the input and the
    # raw output directory are arguments, and the case count is checked against
    # what the caller says to expect rather than a hardcoded 300.
    parser.add_argument("--input", type=Path, default=INPUT, help="Cohort CSV. Defaults to the enrichment cohort.")
    parser.add_argument("--raw-dir", type=Path, default=None,
                        help="Directory for saved cells. Defaults to <provider-runs-v2>/raw/<provider>.")
    parser.add_argument("--expect-cases", type=int, default=None,
                        help="Fail unless the cohort has exactly this many rows. Omit to accept any non-empty cohort.")
    parser.add_argument("--concurrency", type=int, default=None)
    parser.add_argument("--limit", type=int, help="Run only the first N cases (for a smoke test).")
    parser.add_argument("--no-resume", action="store_true", help="Re-run even saved successful cases.")
    args = parser.parse_args()
    required_key = REQUIRED_ENV[args.provider]
    if not os.environ.get(required_key):
        raise RuntimeError(f"{required_key} is required")
    cases = list(csv.DictReader(args.input.open(newline="", encoding="utf-8")))
    if not cases:
        raise RuntimeError(f"{args.input} has no rows")
    if args.expect_cases is not None and len(cases) != args.expect_cases:
        raise RuntimeError(f"Expected {args.expect_cases} benchmark cases; found {len(cases)}")
    missing = [c for c in cases if not c.get("company_domain", "").strip()]
    if missing:
        raise RuntimeError(f"{len(missing)} rows have no company_domain")
    if args.limit:
        cases = cases[:args.limit]
    raw_dir = args.raw_dir or RAW_ROOT / args.provider
    pending = [case for case in cases if args.no_resume or not (output_path(args.provider, case, raw_dir).exists() and json.loads(output_path(args.provider, case, raw_dir).read_text()).get("status") == "ok")]
    concurrency = args.concurrency or DEFAULT_CONCURRENCY[args.provider]
    print(json.dumps({"provider": args.provider, "input": str(args.input), "total": len(cases), "skipped_saved_successes": len(cases) - len(pending), "to_run": len(pending), "concurrency": concurrency}, indent=2), flush=True)
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(run_one, args.provider, case, raw_dir) for case in pending]
        for index, future in enumerate(as_completed(futures), start=1):
            row = future.result()
            results.append(row)
            print(json.dumps({"completed": index, "of": len(pending), "domain": row["input"]["domain"], "status": row["status"], "latency_ms": row["latency_ms"]}), flush=True)
    print(json.dumps({"provider": args.provider, "completed": len(results), "ok": sum(row["status"] == "ok" for row in results), "errors": sum(row["status"] == "error" for row in results), "raw_dir": str(raw_dir)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
