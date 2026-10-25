# Usage Report — Token Usage and Cost Analysis

This file is part of `code.zip` as required by the challenge (`evaluation/usage_report.md`).
It documents the **final full-dataset run that produced the submitted `output.csv`**.

## Run identity

| Field | Value |
| --- | --- |
| Submission artifact | `submission/output.csv` (518 data rows) |
| Pipeline command | `uv run python -m ghost_audit run --dataset <DATASET_DIR> --output output.csv --llm-intent` |
| Run date/time (UTC) | 2026-10-25T03:51:53Z – 2026-10-25T03:52:02Z (model calls) |
| Dataset | official participant-facing `dataset/` (94 evidence files, 518 cases) |
| Code state | final delivery tree; `sha256(output.csv) = ea7ee8546a11981369bd606c72ec71f20331e2b8f9bacf017d8830d4b546eecb` |
| Wall-clock runtime | 12.5 s total on the development machine (deterministic core ≈2.5 s; 10 sequential model calls ≈10 s) |
| Determinism check | rerun with `--no-llm` produced a byte-identical `output.csv` (same sha256) |

## Model provider and model names

| Provider | Base URL | Model used in the final run | Model calls |
| --- | --- | --- | --- |
| DeepSeek | `https://api.deepseek.com` | `deepseek-flash` | 10 |

The provider and the model identifier were validated at runtime against the provider's
`/models` endpoint before the run. No other model or provider was called during the
final full-dataset run.

## What the model is used for (narrow, bounded role)

All factual work is deterministic and runs offline: evidence parsing (SQL/JSON/log/PDF/PNG-OCR),
current + historical identity resolution, point-in-time state reconstruction, policy
application, step planning/ordering, evidence selection and CSV/JSON serialization.
The model is used only as an **advisory multilingual intent cross-check** for the
low-signal cases: requests that either explicitly refuse deletion or contain no
recognized deletion vocabulary. For the official dataset that is exactly the 10
`NON_DELETION` cases (case_533–case_542); every other request is classified by the
deterministic multilingual classifier with explicit deletion vocabulary.

Design guarantees:

* a deterministic classifier is authoritative; the model never overrides it;
* calls are bounded by `--llm-budget` (default 40; 10 used);
* responses are cached by SHA-256 of the request text in `evaluation/intent_cache.json`
  (the cache stores no request text), making reruns reproducible and offline;
* the final run agreed with the deterministic decision on 10/10 reviewed cases.

## Token usage (measured, not estimated)

Source of truth: `evaluation/llm_usage.jsonl`, appended per API call from the provider's
`usage` object in the response (`prompt_tokens`, `completion_tokens`).

| Metric | deepseek-flash | All models |
| --- | --- | --- |
| Model calls | 10 | 10 |
| Input (prompt) tokens | 1,493 | 1,493 |
| Output (completion) tokens | 340 | 340 |
| Total tokens | 1,833 | 1,833 |
| Average tokens per reviewed case | 183.3 | 183.3 |
| Average tokens per dataset case (518) | 3.54 | 3.54 |
| Average input/output per call | 149.3 / 34.0 | 149.3 / 34.0 |

Cached reruns (`evaluation/intent_cache.json` shipped in `code.zip`) make **zero** model
calls; the same `output.csv` is produced offline.

## Cost estimate

Pricing source: DeepSeek public pricing page
<https://api-docs.deepseek.com/quick_start/pricing>, recorded on 2026-10-25 for
`deepseek-flash`: **off-peak** USD 0.15 per 1M input tokens and USD 0.60 per 1M output
tokens (peak rates are double; peak hours are 01:00–04:00 and 06:00–10:00 UTC on
weekdays — this run happened at 03:51Z on a Sunday, i.e. off-peak).

| Component | Calculation | Cost (USD) |
| --- | --- | --- |
| Input tokens | 1,493 × 0.15 / 1,000,000 | 0.00022395 |
| Output tokens | 340 × 0.60 / 1,000,000 | 0.00020400 |
| **Total model cost** | | **0.00042795** |
| Average per reviewed case | total / 10 | 0.00004280 |
| Average per dataset case | total / 518 | 0.00000083 |

No other paid resources were used for the final run (no vector database, no external
search API, no additional model calls). Image OCR used the local macOS Vision framework
(no cost, no network).

## What is *not* included in this report

* The development/interview session itself (coding-agent turns) is not part of the audited
  system's run and is therefore not billed here. The challenge asks for the run that
  produced `output.csv`; that is the run above.
* Development experiments during tuning are not counted; the shipped `intent_cache.json`
  corresponds to the final run's 10 reviewed cases.
* Costs are estimates calculated from measured token counts and the public list price
  above; they are not an invoice.
