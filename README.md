# Company Funding Benchmark

Open data and reproducible runner code for the [OpenBenchmarks Company Funding Benchmark](https://openbenchmarks.com/company-funding).

The benchmark is **two boards**, split on how old the round is:

- **Enrichment** — rounds announced more than 30 days ago. A durable historical set that grows each cycle as freshness snapshots age into it.
- **Freshness** — rounds announced in the trailing 30 days, as dated snapshots. The benchmark cycle re-runs every vendor against each new snapshot, which is what exposes update lag; the public artifact contains the arms that have been promoted to production for that snapshot.

Finding a round announced this week and holding a correct historical record are different capabilities, and a vendor can be strong at one and weak at the other. Averaging them into one number hid that, so they are measured separately.

It is designed for GTM account prioritization and qualification: companies with known funding transitions are intentionally over-sampled so that the benchmark tests an actionable signal rather than mostly empty records.

### Denominators differ per provider, on purpose

A provider is scored against the company observations it was actually measured on, never a board-wide total. A vendor added later is measured on more of the enrichment cohort than one added earlier, and vendors join freshness at different snapshots. If the same company appears in two dated freshness snapshots, those are two measurements rather than one deduplicated company. Every leaderboard row therefore carries its own `case_count` and `snapshot_count`, and the verifier asserts they reconcile against that provider's own cells.

## Evaluated fields and headline metric

Every provider response is normalized to five funding fields:

- Latest funding stage
- Latest funding date
- Latest round amount
- Total raised
- Funding-round count

The headline metric is **correct stage yield**: LLM-judged correct latest stages divided by the Ground Truth-reviewed companies that provider was measured on. The rubric handles documented funding-stage equivalents and exact date/amount evidence for non-Series disagreements. Missing or incorrect provider stages lower yield; blank Ground Truth is a pass-through case and Undisclosed Ground Truth accepts a blank vendor stage.

The other four fields contribute to the separate returned-data coverage metric; they are retained but not headline-scored in this release.

## Repository map

| Path | Contents |
|---|---|
| `data/funding/company-funding-inputs-v1.csv` | Frozen enrichment input list and primary-source funding references |
| `data/latest-funding.json` | Both boards: enrichment cases/runs/leaderboard, plus the pooled freshness leaderboard and its snapshot manifest |
| `data/freshness/<YYYY-MM[-DD]>.json` | One dated freshness snapshot: its own cases, runs and leaderboard. Full dates allow multiple snapshots in one month while preserving the original month-only paths |
| `data/latest-funding-v2-frozen.json` | The 2026-08-04 single-board publication, preserved unchanged so prior citations still resolve |
| `data/funding/pricing-v1.json` | Dated public entry-tier cost assumptions used for the estimated USD cost display |
| `scripts/funding/run_funding_benchmark.py` | Credit-safe, resumable provider runner |
| `scripts/funding/smoke_test_funding_providers.py` | Small contract smoke test for the original endpoint adapters |
| `scripts/funding/run_structured_web_research.py` | Resumable runner for every natural-language arm: Exa search, Exa Agent, Parallel Task and Responses, Seltz, Firecrawl; raw outputs remain local |
| `scripts/funding/run_crustdata_funding_batch.py` | Credit-safe submit/poll runner for Crustdata's 300-company batch enrichment |
| `scripts/funding/run_zoominfo_funding.py` | Credit-safe, resumable ZoomInfo GTM CLI runner; serial 10-domain requests with a two-second interval |
| `scripts/funding/score_funding_stage_dry_run.py` | Transparent latest-stage taxonomy and offline scoring report |
| `scripts/funding/judge_funding_stage.py` | Exact GPT-5.6 Terra v2 judge prompt and structured-output runner |
| `docs/company-funding/llm-judge-v2.md` | Public v2 matching policy and judge contract |
| `scripts/export_llm_judged_snapshot.py` | Current schema-v3 exporter from the published benchmark database; writes both boards and collision-safe dated freshness files without literal API response bodies |
| `scripts/build_public_snapshot.py` | Legacy schema-v1 checkpoint builder retained for reproducing the original publication; it is not the current publishing path |
| `scripts/recompute_funding_snapshot.py` | Recomputes every metric and leaderboard row from the committed snapshot without API calls |
| `scripts/verify_public_artifacts.py` | Zero-network integrity, cohort, and leaderboard checks |

See [DATA.md](DATA.md) for the schemas and [the methodology](docs/company-funding/methodology.md) for the complete public sourcing and evaluation contract.

## Verify without API calls

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
PYTHONPATH=scripts .venv/bin/python scripts/verify_public_artifacts.py
```

The verifier checks invariants rather than fixed counts, so it keeps working as the cohorts change: every leaderboard row reconciles against its own cells, each provider's denominator equals the cases it was measured on, each dated freshness file matches the manifest, the pooled freshness board equals the pooled recomputation of its snapshots, and no literal vendor response bodies are present. It makes no network calls.

`scripts/recompute_funding_snapshot.py --check` additionally rebuilds every leaderboard from the committed verdicts and exits non-zero on any drift.

## Re-run live APIs

Copy `.env.example` to `.env.local` and configure only the providers you intend to run. Live calls require `--confirm-paid`. The endpoint runner skips existing cells of every status unless a retry status is explicitly selected. The natural-language runner skips saved successes; non-success cells are attempted again unless you preserve their checkpoint directory separately.

```bash
PYTHONPATH=scripts .venv/bin/python scripts/funding/run_funding_benchmark.py \
  --only fiber --confirm-paid
```

Every runner takes `--input` and an output directory, so a freshness snapshot is
run exactly like the enrichment cohort with a different cohort file:

```bash
PYTHONPATH=scripts .venv/bin/python scripts/funding/run_structured_web_research.py exa \
  --input data/funding/company-funding-inputs-v1.csv --confirm-paid
```

`run_structured_web_research.py` covers every provider that is asked the
question in natural language: `exa` and `exa-instant` (Search API at two search
types), `exa-agent` (Agent API), `tavily-basic` and `tavily-advanced` (Search
API at the two pinned search depths), `parallel` (Task API) and
`parallel-responses-medium` (Responses API), `seltz-companies` and `seltz-news`
(Answer API at two search scopes), and `firecrawl` and `firecrawl-spark-2`
(Agent API with Spark 1 Mini and Spark 2 respectively). All of them send the
same instruction and the same output schema, so the endpoint or its one varied
parameter is the only difference between arms; the contract tests beside the
runner assert that.

Exa Agent is priced per request by effort and the runner pins it. Leaving the
API default of `effort=auto` meters up to $5 per run, which is roughly fifty
times the cost of the pinned `medium` across a 300-domain cohort.

Firecrawl bills dynamic credits per run and the API defaults to a 2,500-credit
ceiling per call, which is a runaway across a cohort. The runner sets an
explicit cap and records the `creditsUsed` each run reports, which is where its
published cost comes from. The `firecrawl` slug remains pinned to
`spark-1-mini`; `firecrawl-spark-2` is a separate arm pinned to `spark-2`, so a
rerun cannot silently change the model behind an existing leaderboard row.

Use `run_crustdata_funding_batch.py submit` then
`poll` for Crustdata. Crunchbase is deliberately not rerun by a script: it
is a self-serve-plan CSV export, recorded as such in the snapshot. Harmonic is
also a supplied, identity-audited export; it is published as normalized output
and scored with the shared judge, but has no inferred request latency or cost.

ZoomInfo uses the logged-in [`gtm`](https://gtm.ai) CLI rather than a key in
`.env.local`. It sends serial batches of ten domains and requests only
`companyFunding`, `recentFundingAmount`, `recentFundingDate`, and
`totalFundingAmount`:

```bash
PYTHONPATH=scripts .venv/bin/python scripts/funding/run_zoominfo_funding.py --run
```

Point it at a freshness cohort with its own input list and output directories.
Pass absolute paths: each cell stores its batch file as a repo-relative path, so
a relative `--batch-dir` fails *after* the billed call has been made.

```bash
PYTHONPATH=scripts .venv/bin/python scripts/funding/run_zoominfo_funding.py \
  --input "$PWD/data/freshness/2026-08-inputs.csv" \
  --raw-dir "$PWD/data/funding/provider-runs/freshness-2026-08/raw/zoominfo" \
  --batch-dir "$PWD/data/funding/provider-runs/freshness-2026-08/raw/zoominfo-batches" \
  --expect-cases 50 --run
```

The committed snapshot contains the normalized benchmark contract for every provider answer, including status, latency, errors, LLM stage verdicts/reasons, and safe adapter audit metadata. Literal vendor HTTP response bodies stay in local, ignored runner checkpoints and are not redistributed here.

## Providers

Sixteen vendors, measured across twenty-one arms: a vendor with more than one
endpoint is scored once per endpoint, because those endpoints have different
accuracy, latency and price. Both boards group the arms by how they produce an
answer, which is a reading aid rather than a scoring rule. Every arm is asked
the same question about the same companies and judged by the same policy.

**Long Running Agent APIs** dispatch an agent that researches each company at
request time.

- Exa (Agent API)
- Firecrawl (Agent API, Spark 1 Mini)
- Firecrawl (Agent API, Spark 2)
- Parallel (Task API)

**Web search APIs** query an index and extract the answer from it.

- Exa (Search API, deep-reasoning)
- Exa (Search API, instant)
- Tavily (Search API, Basic)
- Tavily (Search API, Advanced)
- Parallel (Responses API, medium reasoning effort)
- Seltz (Answer API, companies scope)
- Seltz (Answer API, news scope)

**GTM data providers** return a stored record from a maintained database.

- Apollo
- CompanyEnrich
- Crunchbase (exported dataset; no API-latency or cost score)
- Crustdata
- Explorium
- Fiber
- Fundable
- Harmonic (identity-audited exported dataset; no API-latency or cost score)
- Ocean.io
- People Data Labs
- PredictLeads
- ZoomInfo (GTM Studio company enrichment)

A high-effort Parallel Responses arm was published on 2026-08-15 and withdrawn
on 2026-08-17. Its rows were removed rather than left to go stale, so it is
absent from the current snapshot; `data/latest-funding-v2-frozen.json` predates
it and never contained it.

No vendor sponsors or controls this benchmark.
