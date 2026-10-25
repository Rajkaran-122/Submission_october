# 03 — Submission checklist (official requirements)

Pre-submission checklist. Run commands from the repository root:

```bash
cd /Users/Rajkaran_122/Desktop/GhostDataStillExists
export UV_CACHE_DIR="$PWD/.uv-cache"
DATASET_DIR=docs/refs/hackerrank-orchestrate-october26-main/dataset   # or specified dataset directory
```

Source: Official `problem_statement.md`, reference `README.md`, `AGENTS.md`.
Submission portal: <https://www.hackerrank.com/contests/hackerrank-orchestrate-october26/challenges/ghost-data-still-exists>

---

## A. Final Deliverable Gate (Single Command)

```bash
uv run python scripts/validate_submission.py --dataset "$DATASET_DIR" --verbose
```

- [ ] Exit code `0`, output `RESULT: PASS`
- [ ] `output.csv`, `submission/code.zip`, `submission/log.txt`, `submission/INTERVIEW_GUIDE.md` all exist
- [ ] `submission/` contains no superfluous files (`.DS_Store`, etc. removed)
- [ ] `scripts/validate_submission.py` and `evaluation/evaluate.py validate` report 0 ERRORs

## B. `output.csv` (Uploaded separately on HackerRank)

- [ ] File exists at repository root `output.csv` and is copied to `submission/output.csv`
- [ ] Header exactly `case_id,deletion_status,steps_json`
- [ ] Exactly **518** data rows (excluding header), covering all cases in `deletion_requests.csv`
- [ ] Sorted in **ascending canonical numeric case ID order** (`case_33, case_99, case_100, ..., case_550`), no duplicates
- [ ] `deletion_status ∈ {COMPLETE, INCOMPLETE, NOT_APPLICABLE}`
- [ ] Each `steps_json` is a valid JSON array
- [ ] Each step has exactly 7 required fields; `step_no` increments consecutively from 1
- [ ] (phase, operation, state) tuples are in the allowed schema; ordered by phase -> operation -> system_id -> resource_id
- [ ] `NOT_APPLICABLE` -> `[]`
- [ ] `COMPLETE` -> VERIFICATION steps only, each citing evidence with `captured_at <= audit_at`
- [ ] `INCOMPLETE` -> FINDING + REMEDIATION + subsequent VERIFICATION; remediation and future verification have `evidence_id=""`
- [ ] Spot checks: No citations after `audit_at`; no non-existent evidence IDs
- [ ] UTF-8 encoded

Verification command:

```bash
uv run python scripts/validate_submission.py --dataset "$DATASET_DIR"
uv run python evaluation/evaluate.py validate \
  --output output.csv \
  --requests "$DATASET_DIR/deletion_requests.csv" \
  --dataset "$DATASET_DIR"
```

## C. `submission/code.zip` (Uploaded separately on HackerRank)

- [ ] Built via `uv run python scripts/build_code_zip.py` (deterministic, outputs SHA-256)
- [ ] Contains `README.md` (English, with setup and execution commands)
- [ ] Contains `INTERVIEW_GUIDE.md` (English)
- [ ] Contains `evaluation/evaluation_report.md`
- [ ] Contains `evaluation/usage_report.md`
- [ ] Contains runnable entry point `src/ghost_audit/__main__.py` (`uv run python -m ghost_audit run ...`)
- [ ] Contains `pyproject.toml`, `uv.lock`, `.python-version`, `src/**`, `tests/**`, `evaluation/**`, `scripts/**`
- [ ] Contains `evaluation/intent_cache.json` (hashes + labels for offline `--llm-intent` reproducibility)
- [ ] **Excludes** `evaluation/llm_usage.jsonl` (local telemetry) and `evaluation/.tmp/**`
- [ ] **Excludes** `dataset/` and any raw official data files
- [ ] **Excludes** `.venv`, `.git`, `.uv-cache`, `.ocr-cache`, `__pycache__`, `.pytest_cache`, `work/`
- [ ] **Excludes** nested archives (`.zip/.tar/.gz/.tgz/.7z/.rar/.whl`)
- [ ] **Excludes** API keys, credentials, private keys; no `.env`, `api_key.txt`, `src/*/config/`
- [ ] **Excludes** `output.csv`, `log.txt`, `submission/` (standalone deliverables)
- [ ] Reproduces cleanly when unzipped in a fresh directory (Section F)

Verification commands:

```bash
uv run python scripts/build_code_zip.py --dry-run --list   # Human-readable list
uv run python scripts/build_code_zip.py                     # Generates submission/code.zip
unzip -l submission/code.zip | head -40
uv run python scripts/validate_submission.py --dataset "$DATASET_DIR"
```

## D. `log.txt` -> chat_transcript (Uploaded separately on HackerRank)

- [ ] Repository root `log.txt` (PART I) is an append-only agent session log
- [ ] Entries contain `tool=` and `model=` lines
- [ ] `submission/log.txt` is compiled via `python3 scripts/build_transcript.py`: retains root log as verbatim prefix, followed by sanitized PART II session appendices
- [ ] `python3 scripts/build_transcript.py --check` passes
- [ ] Appendices include source SHA-256 prefixes and UTC event time spans; internal reasoning blocks and raw tool dumps omitted; credentials masked
- [ ] Uploaded to the **chat_transcript** slot on HackerRank (not inside code.zip)

Verification commands:

```bash
python3 scripts/build_transcript.py --check
grep -c 'tool=' log.txt && grep -c 'model=' log.txt
grep -n 'PART II — FULL CONVERSATION TRANSCRIPTS' submission/log.txt
grep -nE 'sk-[A-Za-z0-9_-]{16,}|-----BEGIN .*PRIVATE KEY-----|Bearer [A-Za-z0-9._-]{20,}' log.txt submission/log.txt || echo "no secrets found"
```

## E. Evaluation and Usage Reports (Inside `code.zip`)

- [ ] `evaluation/evaluation_report.md` includes:
  - [ ] case-level deletion-status accuracy
  - [ ] step-level and field-level accuracy
  - [ ] sequence accuracy
  - [ ] >= 3 custom team evaluation cases (data, expected steps, rationale)
  - [ ] failures found and improvements made
- [ ] `evaluation/usage_report.md` includes (for the final full run):
  - [ ] model provider(s) and model name(s)
  - [ ] model call counts
  - [ ] input / output token counts
  - [ ] total and average tokens per request
  - [ ] estimated total and per-request cost in USD (with pricing citations)
- [ ] Numbers come from empirical final runs without placeholders
- [ ] Reports contain no secrets or credentials

Verification commands:

```bash
grep -n "TBD\|TODO" evaluation/evaluation_report.md evaluation/usage_report.md || echo "no placeholders"
unzip -p submission/code.zip evaluation/evaluation_report.md | head -20
unzip -p submission/code.zip evaluation/usage_report.md | head -20
```

## F. Clean-Room Reproduction

- [ ] Unzip into an empty directory, supply dataset, runs cleanly per README
- [ ] Produced `output.csv` matches submitted `output.csv`
- [ ] No hardcoded machine-specific absolute path dependencies
- [ ] Offline run (`--no-llm` or default) produces complete 518 rows

```bash
rm -rf /tmp/ghost-clean && mkdir -p /tmp/ghost-clean
cd /tmp/ghost-clean
unzip /Users/Rajkaran_122/Desktop/GhostDataStillExists/submission/code.zip
export UV_CACHE_DIR="$PWD/.uv-cache"
uv sync
uv run pytest -q
uv run python -m ghost_audit run --dataset "$DATASET_DIR" --output output.csv
uv run python scripts/validate_submission.py --csv output.csv --skip-missing --verbose
diff <(sort output.csv) <(sort /Users/Rajkaran_122/Desktop/GhostDataStillExists/output.csv) && echo "reproduced identically"
```

## G. AI Interview Preparation (`INTERVIEW_GUIDE.md`)

- [ ] `submission/INTERVIEW_GUIDE.md` completed (English standard, no `TODO` placeholders)
- [ ] Covers: Problem understanding, architecture, evidence handling, identity resolution, point-in-time reconstruction, deterministic vs. model boundaries, testing & evaluation, failures & improvements, costs and known limitations
- [ ] Contains empirical Q&A consistent with `evaluation_report.md` and `usage_report.md`
- [ ] Accurate description of implemented functionality

Verification command:

```bash
grep -c "TODO" submission/INTERVIEW_GUIDE.md || echo "no TODOs"
uv run python scripts/validate_submission.py --dataset "$DATASET_DIR"
```

## H. Final HackerRank Submission Mapping

| HackerRank Slot | Local File | Notes |
| --- | --- | --- |
| `output.csv` | `submission/output.csv` | 518 rows, sorted, contract-valid |
| `code.zip` | `submission/code.zip` | Contains README + evaluation reports, no dataset/secrets |
| `chat_transcript` | `submission/log.txt` | Root `log.txt` (PART I) + compiled session appendices (PART II) |
| AI interview | `submission/INTERVIEW_GUIDE.md` | Architecture, evaluation, Q&A guide |

- [ ] Execute Section A gate command immediately prior to submission
- [ ] Do not submit `docs/`, `work/`, `.venv/`, `.uv-cache/`, or any keys

## I. Known Limitations and Disclosures

- [ ] Hidden canonical answers unverified: sample regression does not guarantee hidden accuracy
- [ ] Anonymous analytics attribution uses heuristics (ordering, time windows, elimination)
- [ ] `--skip-images` omits PNG evidence, causing expected accuracy drop
- [ ] `--llm-intent` is optional; model impact is documented
- [ ] Official deadline is unconfigured upstream
