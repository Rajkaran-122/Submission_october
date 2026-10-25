# Ghost Data Still Exists — Architecture Design, Task Breakdown, and Acceptance Criteria

- Status: v0.1 (Architecture design, updated with implementation)
- Date: 2026-10-25
- Author: Core Team (Lead + teammates)
- Upstream: `docs/refs/hackerrank-orchestrate-october26-main/{AGENTS.md,README.md,problem_statement.md,dataset/}`

## 1. Problem Understanding (Contract Specifications)

For the 518 cases in `dataset/deletion_requests.csv`, produce `case_id,deletion_status,steps_json`:

- `deletion_status ∈ {COMPLETE, INCOMPLETE, NOT_APPLICABLE}`;
- `steps_json` is an ordered JSON array where each entry has 7 fields: `step_no, phase, system_id, resource_id, operation, state, evidence_id`;
- `NOT_APPLICABLE` -> `[]` (Unauthorized deletion requests: access / correction / export / unsubscribe, etc.);
- For authorized deletions: first reconstruct resource states as of `audit_at` using evidence, then apply `deletion_rules.csv` to determine the disposition of each data category (DELETE/REDACT/ANONYMIZE/RETAIN), determine COMPLETE vs. INCOMPLETE status, and output remediation + verification steps;
- Ordering: phase order FINDING -> REMEDIATION -> VERIFICATION; within a phase, sort by operation table order, then ascending by `system_id` and `resource_id`;
- Evidence citation: FINDING steps must cite `file_id`, `activity_id`, or `audit_id`; INCOMPLETE remediation and subsequent verification steps use empty strings; COMPLETE verification steps must cite evidence proving the current state; never cite organizer ground truth or evidence captured after `audit_at`.

Conditions for "complete deletion" (problem_statement):
1. Every relevant copy receives the disposition required by `deletion_rules.csv`;
2. No active sync/import/restore paths can reconstruct the data;
3. Records that must be retained only contain allowed identifiers;
4. Records that must be retained remain accessible.

## 2. Dataset Overview (Verified Observations)

| File | Size / Structure |
| --- | --- |
| `deletion_requests.csv` | 518 rows (`case_33` ... `case_550`); `reported_status` distribution: COMPLETE 254 / PARTIALLY_COMPLETE 152 / NOT_STARTED 112 |
| `sample_deletion_requests.csv` / `sample_output.csv` | 32 rows of official samples (`case_01`–`case_32`), with full steps |
| `user_identities.csv` | 850 users; fields: user_id, name, email, phone, email_hash, updated_at |
| `user_audit_log.csv` | 307 historical identity change records (email 107 / email_hash 107 / phone 92) |
| `system_activity.csv` | 4012 global activity records; event types: DELETE/UPDATE/SYNC/INDEX/SEND_EMAIL/REFRESH_CACHE/RETAIN/ANONYMIZE/REDACT/CREATE_EXPORT/CHECK_DATA_FLOW/DISABLE_DATA_FLOW/BLOCK_RESTORE/CHECK_RESTORE_ACCESS/LINK_RESOURCE_IDENTITY/CLASSIFY_REQUEST/RESTORE_BACKUP |
| `deletion_rules.csv` | 15 category -> action rules (see Section 3) |
| `evidence_files.csv` | 94 evidence files (28 JSON / 26 log / 24 SQL / 4 PDF / 11 PNG / 1 TXT) |
| `evidence/` | Spanning 2026-01-29 to 2026-10-01; 95 files |

Key Structural Findings:

1. **Unique case -> user resolution**: Using `user_identities` + `user_audit_log` (historical email changes), all 518 request emails uniquely resolve to a user_id; all 32 sample cases also uniquely resolve.
2. **Wave structure**: Hidden cases are mostly structured in waves of 10 cases, where cases in the same wave share the same `audit_at` date; each wave has exactly 1 wave evidence file (captured on the audit date or the day before, covering >= 75% of users in that wave). That file can reconstruct scenarios for the 10 cases in the wave.
3. **Special cases**: `case_33`–`case_72`, `case_533`–`case_550`, etc., are edge cases with dedicated evidence files (PNG images, PDFs, small SQL dumps, JSON, logs). Sample cases `case_01`–`case_32` also belong to this category.
4. **Non-deletion requests**: `system_activity.event_type=CLASSIFY_REQUEST` has exactly 10 entries, and exactly 10 request texts out of 518 are access, correction, export, marketing opt-out, or keep-account requests (`case_533`–`case_542`), expecting `NOT_APPLICABLE`.
5. **Evidence format families** (parsed into normalized Observation objects):
   - `record_lookup` log lines: `system / resource_id / record_type / result / subject_reference`;
   - Readback traces: query start/completed line pairs; completed lines contain full fields;
   - `service_resource_export_*.json`: grouped by service (profiles, restore_entries, exports, connectors, index_queries, audit_records, tickets);
   - `customer_data_inventory_*.sql`, `customer_data_*_replica_*.sql`: SQLite DDL + INSERT statements;
   - `customer_data_snapshot_bundle_*.json`: snapshots grouped by data_store/collection;
   - `connector_configurations_*.json` / `customer_export_manifest_*.json` / `security_audit_ledger_*.json`: records[] format;
   - `customer_account_handover_*.txt`: pipe-delimited format;
   - PDF: text extracted via pypdf;
   - PNG: OCR extracted via macOS Vision.
6. **Evidence conflict and point-in-time**: Multiple observations for the same resource across different timestamps resolve by taking the most recent observation at or before `audit_at`.
7. **Anonymous record attribution**: Analytics profiles that were anonymized lose direct identifiers; attribution is determined via snapshot bundle ordering, activity time windows, or process of elimination.
8. **Evidence citation selection**: Each step cites an evidence file proving the resource state at `audit_at` where `captured_at <= audit_at`. INCOMPLETE remediation and future verification steps use empty string evidence IDs.

## 3. Policy and Disposition Rules (from deletion_rules.csv)

| Rule | Category | Condition | Action | System Decision |
| --- | --- | --- | --- | --- |
| 01 | USER_PROFILE | Always | DELETE | Present -> REPORT_REMAINING_RECORD + DELETE_RECORD |
| 02 | SECONDARY_IDENTIFIER | Always | DELETE | Present -> DELETE |
| 03 | SEARCH_INDEX_ENTRY | Always | DELETE | FOUND -> DELETE |
| 04 | CACHE_ENTRY | Always | DELETE | Present -> DELETE |
| 05 | SUPPORT_MESSAGE | Contains shared context | REDACT | IDENTIFIABLE -> REPORT_EXPOSED_PII + REDACT |
| 06 | SUPPORT_ATTACHMENT | Solely owned | DELETE | FOUND -> DELETE |
| 07 | ANALYTICS_PROFILE | Still attributable | ANONYMIZE | IDENTIFIABLE -> REPORT_EXPOSED_PII + ANONYMIZE |
| 08 | PRODUCT_TELEMETRY | Still attributable | ANONYMIZE | Same as 07 |
| 09 | USER_EXPORT | Always | DELETE | FOUND -> DELETE |
| 10 | MARKETING_PROFILE | Always | DELETE | FOUND -> DELETE |
| 11 | EMAIL_DELIVERY_LOG | Contains email | REDACT | Contains email -> REDACT |
| 12 | SECURITY_AUDIT_LOG | Within retention period | RETAIN | FOUND -> VERIFY_RECORD_PRESENT |
| 13 | LEGAL_HOLD_RECORD | Active legal hold | RETAIN | FOUND -> VERIFY_RECORD_PRESENT |
| 14 | BACKUP_ARCHIVE | Restore can be blocked | RETAIN | restore_allowed -> REPORT_RESTORE_RISK + BLOCK_RESTORE; blocked -> VERIFY_RESTORE_BLOCKED |
| 15 | ACCOUNT_TOMBSTONE | Deleted & needs stable user_id | REDACT | Contains direct identifiers -> REPORT_EXPOSED_PII + REDACT; redacted -> VERIFY_RECORD_REDACTED |

Categories that must be retained (SECURITY_AUDIT_LOG, LEGAL_HOLD) are handled as required retention and verified present.

## 4. Step Generation Rules

For each case:

1. **Intent**: Non-deletion request -> `NOT_APPLICABLE` and `[]`.
2. **Scenario collection**: Extract Observations belonging to the user from the case's evidence package.
3. **Status**: If any resource violates policy (e.g. record still present, PII exposed, data flow active, restore permitted) -> `INCOMPLETE`; otherwise `COMPLETE`.
4. **FINDING** (INCOMPLETE only):
   - Record that should be deleted still present -> `REPORT_REMAINING_RECORD`;
   - Record that should be redacted/anonymized still identifiable -> `REPORT_EXPOSED_PII`;
   - Connector enabled -> `REPORT_ACTIVE_DATA_FLOW`;
   - Backup restore permitted -> `REPORT_RESTORE_RISK`.
5. **REMEDIATION** (INCOMPLETE only): Each finding pairs with a remediation operation (DISABLE_DATA_FLOW / BLOCK_RESTORE / DELETE_RECORD / REDACT_RECORD / ANONYMIZE_RECORD) with empty evidence ID.
6. **VERIFICATION**: Covers target states of each resource in the scenario + baseline `account_service.account_<id>` `VERIFY_RECORD_ABSENT`; retained -> `VERIFY_RECORD_PRESENT`. COMPLETE cites evidence; INCOMPLETE uses empty string.
7. **Ordering**: phase order -> operation order -> system_id -> resource_id.
8. **Evidence selection**: Prioritize explicit observations of the resource state with `captured_at <= audit_at`.

## 5. System Architecture

```text
src/ghost_audit/
  __init__.py
  config.py            # Paths, model config (keys from env/local untracked file)
  dataset.py           # Reads CSVs + evidence manifest; dataset integrity checks
  identity.py          # Current + historical identifiers; case->user resolution
  intent.py            # Deletion intent classification (multilingual rules + optional LLM review)
  evidence/
    __init__.py
    base.py            # Observation dataclass / enums / normalization
    sql_dump.py        # SQL dumps -> Observations
    json_docs.py       # bundle / service export / records[] JSON
    logs.py            # record_lookup / readback / semi-structured logs
    handover.py        # handover text format
    documents.py       # PDF text extraction + PNG OCR
    registry.py        # File type routing + evidence indexing
  activity.py          # system_activity index: time windows, resource linkages
  scenario.py          # Case evidence packaging, Observation attribution, timeline reconstruction
  policy.py            # Category -> action, finding -> remediation mappings
  planner.py           # Status + step generation + sorting + evidence selection
  output.py            # output.csv writer (strict JSON, sorting, contract verification)
  llm.py               # Advisory LLM client (optional intent review; records usage)
  cli.py               # Command-line interface (`python -m ghost_audit run ...`)
evaluation/
  evaluate.py          # Sample regression + contract + metrics harness
  cases/               # Independent evaluation test cases
  evaluation_report.md
  usage_report.md
tests/                 # pytest suite: contracts, identity, policy, transforms, mutations
scripts/               # build_code_zip.py / validate_submission.py / ocr_images.swift
submission/            # output.csv / code.zip / log.txt / INTERVIEW_GUIDE.md
```

Design principle: Evidence parsing, identity resolution, state reconstruction, and policy application are 100% deterministic; the model is only used for narrow advisory semantic checks.

## 6. Task Breakdown and Acceptance Criteria

| # | Task | Owner | Acceptance |
| --- | --- | --- | --- |
| 1 | Repository, docs, scaffolding | Lead | Clean project structure, readable documentation |
| 2 | Evidence parsing layer (`evidence/*`, `activity.py`) | evidence-parser | 95 evidence files parse cleanly; test coverage for all formats |
| 3 | Identity resolution + intent classification | Lead | 518 cases uniquely resolve; 10 non-deletion requests detected |
| 4 | Scenario reconstruction + policy + planner + CLI | Lead | 32 samples match status and steps; contract tests pass |
| 5 | Independent evaluation test cases (>= 10) + transforms + mutations + contract checks | eval-harness | Handcrafted expected outputs and rationale; mutation tests catch injected bugs |
| 6 | Full 518-case run + `output.csv` | Lead | Exactly 518 rows, sorted, contract-valid, valid evidence citations |
| 7 | Evaluation/usage reports, README, INTERVIEW_GUIDE, packaging | docs-packaging | Reproducible clean-room unzip; measured tokens and costs |
| 8 | GitHub PR / CI / merge, final audit | Lead | CI green; PRs reviewed and merged; clean local and remote state |

## 7. Evaluation Plan

1. **Official sample regression** (development set for regression testing): 32 samples; reports status match rate, step F1, field-level match rate, sequence match rate, and evidence match rate.
2. **Independent evaluation cases**: Custom mini-dataset in `evaluation/cases/` covering historical identity drift, replica lags, re-sync after deletion, restore risks, mandatory retention, tombstones, and active data flows.
3. **Input invariance**: Shuffling rows, injecting dummy records, or changing `reported_status` must not alter calculated outputs.
4. **Test suite effectiveness**: Mutation tests deliberately inject typical errors (ignoring historical emails, trusting claims, incorrect sorting) and assert that evaluation catches them.
5. **Output contract**: Row count, sorting, columns, JSON validity, consecutive `step_no`, phase order, operation order, and cross-artifact consistency.

## 8. Risks, Open Questions, and Decisions

| Item | Status | Description |
| --- | --- | --- |
| Hidden canonical ground truth | Unverified | Only 32 samples are labeled; wave case logic follows rule deduction and contract constraints |
| Anonymous analytics attribution | Unverified | Approximated via snapshot bundle ordering, activity time windows, and elimination |
| Scoring weight details | Unverified | Contract correctness is the primary goal |
| LLM role in this system | Decided | Advisory intent check only (temperature 0, cached responses, usage recorded); all state logic is deterministic |
| Evidence captured after audit_at | Decided | Used only for static resource attribution (identity linking); never alters audit_at state conclusions |
| Image OCR | Decided | Uses macOS Vision OCR; tests gracefully skip when OCR toolchain is absent on CI |

## 9. Cost and Execution

- Full run: 518 cases + 94 evidence files; pure Python deterministic pipeline with optional cached LLM intent review. Measured tokens and cost documented in `evaluation/usage_report.md`.
- Zero-API-key execution: System runs completely offline without API keys; deterministic classifier handles all intent decisions.
