# How these results were produced

## The run

Every one of the 300 companies in the enrichment cohort (`data/funding/company-funding-inputs-v1.csv`,
the rounds older than 30 days) was sent to Pvalyou the way a customer request is: the company's
domain and name go into the intake, which either finds the company already on file or reads it
live (its website and newsroom, its own LinkedIn page, public registries, SEC filings for a US
company, the wires and third-party pages) and writes it to the catalogue. The record returned is
the same record a paid call returns, and the adapter in
`scripts/funding/smoke_test_funding_providers.py` is that call. The answers here were read from
the records on 2026-09-14, after the last pipeline change, in one pass.

Nothing was tuned per company. Where a case exposed a weakness, the fix went into the pipeline as
a general rule (for example: a provider's "Later Stage VC" label names no series letter and is
stored as such until a source states one; the company's own newsroom articles about a raise are
read, not only the press index page; a US company's Form D and Form C filings are read; a title
naming a round pre-IPO makes it Pre-IPO). All of it runs for every company we serve.

The freshness board (rounds announced in the trailing 30 days) is not measured here. We will
join it once we have measured a dated snapshot ourselves.

## The normalization

`normalize("pvalyou", ...)` in `scripts/funding/run_funding_benchmark.py` takes the newest
equity-side round by date from the record's funding block, the same rule the other branches
apply: debt, grants, secondaries and accelerator programme money stay in the record but are not a
financing stage; a stake bought by an investor (filed in the record under acquisitions) is a
Private Equity stage; undated rounds fall behind dated ones; among equal dates the later ladder
stage is the latest. An accelerator batch is answered as Pre-Seed.

## The judging

The published policy (`docs/company-funding/llm-judge-v2.md`), run with the prompt and schema in
`scripts/funding/judge_funding_stage.py` on `openai/gpt-5.6` through OpenRouter, medium reasoning.
`judged.json` holds every verdict with its decision basis and reason. Two consecutive judge runs
on the same answers differed by one verdict, so the number below is one run's, not a ceiling.

## The numbers, by the board's definitions

| metric | value |
|---|---|
| stage correct yield | 94.33% (283 correct of 300) |
| stage fill rate | 99.67% (299 of 300 returned a stage) |
| resolution | 100% (300 of 300 companies) |

Decision basis of the correct answers: equivalent stage 120, same series letter 143, exact date
18, exact amount 1, blank ground truth 1.

## Where we disagree with the reference

Seventeen answers. Nine are the reference's vocabulary or its date (a "Seed Plus", a "Series A/B",
a round we hold that is newer than the reference's, a letter the provider itself states); eight
are ours, and each is listed in `reference_notes.md` with what we hold and why.
