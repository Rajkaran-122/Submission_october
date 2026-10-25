# Failure report — evaluation runs on real predictions

Status: **skeleton** (no fabricated numbers). Fill this file with results from
actual runs of `evaluation/evaluate.py`. Every section lists the command used so
results stay reproducible.

- Maintainer: Lead / docs owners (eval-harness provides the harness only).
- Last updated: _YYYY-MM-DDTHH:MM:SSZ_
- Pipeline commit / branch: _fill from `git rev-parse --short HEAD`_
- Output under evaluation: _path to the predictions CSV_

## 1. Commands used

```bash
# official 32-case public sample regression (development set, not hidden accuracy)
UV_CACHE_DIR="$PWD/.uv-cache" uv run python evaluation/evaluate.py samples \
  --json evaluation/reports/sample_report.json

# independent team-authored cases
UV_CACHE_DIR="$PWD/.uv-cache" uv run python evaluation/evaluate.py cases --run-pipeline \
  --json evaluation/reports/cases_report.json

# final full-dataset output contract
uv run python evaluation/evaluate.py validate \
  --output output.csv \
  --requests docs/refs/hackerrank-orchestrate-october26-main/dataset/deletion_requests.csv \
  --dataset docs/refs/hackerrank-orchestrate-october26-main/dataset
```

## 2. Headline metrics

| Run | cases | status accuracy | step F1 | sequence accuracy | field accuracy | evidence accuracy | contract errors |
| --- | --- | --- | --- | --- | --- | --- | --- |
| official samples (32) | _ | _ | _ | _ | _ | _ | _ |
| independent cases (12) | _ | _ | _ | _ | _ | _ | _ |
| full dataset contract | 518 | n/a | n/a | n/a | n/a | n/a | _ |

Copy the generated values from the JSON/Markdown reports — do not estimate.
The latest measured run (metrics + per-case diagnostics) is also kept in
`evaluation/reports/sample_report.{json,md}` and
`evaluation/reports/cases_report.{json,md}`, and folded into the draft
`evaluation/evaluation_report.md`.

## 3. Failures found and improvements made

| # | Run / case | Symptom (from the evaluator) | Likely stage | Root cause | Fix | Status |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | _ | _ | _ | _ | _ | open |

The evaluator prints, per failing case: expected vs predicted status,
missing/extra steps, field mismatches (including evidence ids), ordering
problems and a likely pipeline stage. Paste those diagnostics verbatim in the
"Root cause" work.

## 4. Contract issues (full dataset)

```text
<output of `evaluate.py validate` for the final output.csv>
```

## 5. Known gaps / unverifiable items

- Hidden canonical answers are not available; only the 32 public samples are
  labelled, so no hidden-set accuracy is claimed anywhere.
- The team-authored cases are hand-written and exercise the rules independently,
  but they are not the official grader.
- Anything the pipeline cannot decide deterministically must be listed here
  rather than silently assumed.
