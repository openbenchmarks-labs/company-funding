#!/usr/bin/env python3
"""Local-only experiment: score latest funding stage with deterministic rules.

No API calls are made. The report exposes every raw stage label that could not
be mapped, so taxonomy decisions remain reviewable rather than hidden in an
LLM judgement.
"""

from __future__ import annotations

import csv
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
INPUT = ROOT / "data" / "funding" / "company-funding-inputs-v1.csv"
RAW = ROOT / "data" / "funding" / "provider-runs-v1" / "raw"
OUTPUT = ROOT / "data" / "funding" / "provider-runs-v1" / "stage-dry-run.json"


def canonical_stage(value: Any) -> str | None:
    """Map observed funding-stage labels to a transparent comparison taxonomy."""
    raw = str(value or "").strip().lower()
    if not raw:
        return None
    text = re.sub(r"[^a-z0-9]+", " ", raw).strip()
    # Preserve pre-series labels before the general Series matching.
    match = re.search(r"\bpre\s+series\s+([a-h])\b", text)
    if match:
        return f"pre_series_{match.group(1)}"
    if re.search(r"\bpre\s*seed\b", text):
        return "pre_seed"
    match = re.search(r"\bseries\s+([a-h])(?:\s*\d+|\s*(?:plus|bridge))?\b", text)
    if match:
        return f"series_{match.group(1)}"
    if re.search(r"\bseed\b", text):
        return "seed"
    if "private equity" in text:
        return "private_equity"
    if "growth equity" in text or "growth investment" in text:
        return "growth_equity"
    if "strategic" in text or "corporate" in text:
        return "strategic_or_corporate"
    if "equity crowdfunding" in text:
        return "equity_crowdfunding"
    if "angel" in text:
        return "angel"
    if "venture" in text:
        return "venture_unspecified"
    if "convertible" in text:
        return "convertible_note"
    if "debt" in text or "credit facility" in text or "warehouse financing" in text:
        return "debt_or_credit"
    if "grant" in text or "non equity assistance" in text:
        return "grant_or_non_equity"
    if "pre ipo" in text:
        return "pre_ipo"
    if "post ipo" in text or text == "ipo":
        return "post_ipo"
    # These say no usable stage, so are excluded from a stage denominator.
    if text in {"undisclosed", "series unknown", "funding", "funding round", "investment", "financing round", "fundraise", "capital", "other"}:
        return None
    return None


def main() -> int:
    truth_rows = list(csv.DictReader(INPUT.open(newline="")))
    truth = {row["company_domain"]: row["ground_truth_stage"] for row in truth_rows}
    mapped_truth = {domain: canonical_stage(value) for domain, value in truth.items()}
    eligible = {domain for domain, value in mapped_truth.items() if value is not None}
    report: dict[str, Any] = {
        "metric": "stage_correct_yield = correct canonical stage / eligible canonical stage truth",
        "taxonomy": ["pre_seed", "seed", "pre_series_a", "pre_series_b", "series_a-h", "private_equity", "growth_equity", "strategic_or_corporate", "equity_crowdfunding", "angel", "venture_unspecified", "convertible_note", "debt_or_credit", "grant_or_non_equity", "pre_ipo", "post_ipo"],
        "truth": {
            "total_cases": len(truth),
            "eligible_stage_cases": len(eligible),
            "excluded_null_or_unusable": len(truth) - len(eligible),
            "canonical_counts": dict(sorted(Counter(mapped_truth[d] for d in eligible).items())),
            "unmapped_raw_labels": dict(sorted(Counter(v or "<null>" for d, v in truth.items() if mapped_truth[d] is None).items())),
        },
        "providers": {},
    }
    for provider_dir in sorted(path for path in RAW.iterdir() if path.is_dir()):
        rows = [json.loads(path.read_text()) for path in provider_dir.glob("*.json")]
        by_domain = {row["input"]["domain"]: row for row in rows}
        predicted_raw = {domain: (by_domain.get(domain, {}).get("normalized") or {}).get("latest_stage") for domain in eligible}
        predicted = {domain: canonical_stage(value) for domain, value in predicted_raw.items()}
        correct = sum(predicted[domain] == mapped_truth[domain] for domain in eligible)
        present = sum(predicted[domain] is not None for domain in eligible)
        present_correct = sum(predicted[domain] == mapped_truth[domain] for domain in eligible if predicted[domain] is not None)
        report["providers"][provider_dir.name] = {
            "eligible_truth_cases": len(eligible),
            "correct_stage_count": correct,
            "stage_correct_yield_pct": round(100 * correct / len(eligible), 2),
            "stage_fill_count": present,
            "stage_fill_rate_pct": round(100 * present / len(eligible), 2),
            "stage_accuracy_when_present_pct": round(100 * present_correct / present, 2) if present else None,
            "unmapped_provider_labels": dict(sorted(Counter(value for domain, value in predicted_raw.items() if value and predicted[domain] is None).items())),
        }
    OUTPUT.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
