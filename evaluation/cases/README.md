# Independent evaluation cases (team-authored, hand-written expected answers)

These 13 mini-datasets are **authored independently of `src/ghost_audit/`**. Each
case is a complete miniature dataset with the same file contract as the official
`dataset/` directory. The answer in `expected.csv` was written by hand from
`deletion_rules.csv` and `problem_statement.md`; it was **not** produced by,
derived from, or copied out of the pipeline under test.

## Layout

```text
cXX_<scenario>/
├── deletion_requests.csv      # one request (some cases could be extended)
├── user_identities.csv        # current identity snapshot
├── user_audit_log.csv         # historical identifier changes
├── system_activity.csv        # global operation stream (claims, decoys)
├── deletion_rules.csv         # copy of the participant-facing policy
├── evidence_files.csv         # evidence manifest for this case
├── evidence/                  # small evidence files in real format families
├── expected.csv               # hand-written canonical answer
└── README.md                  # rule-by-rule rationale for the answer
```

The pipeline can be run on a case exactly like on the official dataset:

```bash
uv run python -m ghost_audit run \
  --dataset evaluation/cases/c01_historical_email \
  --requests evaluation/cases/c01_historical_email/deletion_requests.csv \
  --output /tmp/c01.csv --skip-images
```

or all cases at once through the harness:

```bash
uv run python evaluation/evaluate.py cases --run-pipeline
```

## Cases and the scenario each one pins down

| Case | Expected status | Scenario / trap |
|---|---|---|
| `c01_historical_email` | INCOMPLETE | Request email is a **historical** address; the current-identity snapshot alone cannot find the user. A `DELETE ... SUCCESS` activity claims completion but evidence shows the profile present. |
| `c02_replica_leftover` | INCOMPLETE | Primary lookup returns 404, but a **read-replica SQL snapshot** still holds the full profile (rule 01 applies to every copy). |
| `c03_post_deletion_resync` | INCOMPLETE | Reported complete; search document was **recreated after deletion** by a still-enabled sync connector. Requires remaining-record finding + active-data-flow finding, and disabling the flow before deleting the document. |
| `c04_backup_restore_risk` | INCOMPLETE | Account deleted, but a backup is still `restore_allowed: true` (rule 14) -> `REPORT_RESTORE_RISK` + `BLOCK_RESTORE`. |
| `c05_must_retain_security` | COMPLETE | Everything deleted except a **retained security audit record** (rule 12); retaining it is compliant and must be verified present. |
| `c06_tombstone_redacted_complete` | COMPLETE | **Permitted tombstone**: stable `user_h06` reference remains, all direct identifiers null (rule 15 satisfied). |
| `c07_tombstone_exposing_pii` | INCOMPLETE | Tombstone still carries name/email/phone/email hash -> `REPORT_EXPOSED_PII` + `REDACT_RECORD`. |
| `c08_not_applicable_access_request` | NOT_APPLICABLE + `[]` | Request asks for **access/correction**, explicitly not deletion -> no steps authorized. |
| `c09_active_data_flow` | INCOMPLETE | Account gone but an **enabled integration connector** can copy the data (plus an unrelated decoy account in the same evidence file). |
| `c10_support_shared_context` | INCOMPLETE | Support **message** has shared context -> `REDACT`; support **attachment** belongs only to the subject -> `DELETE`; different findings/remediations for the same system. |
| `c11_complete_verification_only` | COMPLETE | All compliant states (absent, retained, redacted, anonymized, flow disabled, restore blocked) -> **verification-only** output in operation-table order. |
| `c12_evidence_citation_window` | COMPLETE | Two evidence files prove the same state, but one is captured **after `audit_at`**; only the in-window handover file may be cited. |
| `c13_export_leftover` | INCOMPLETE | Account deleted but a downloadable **user export** still exists (rule 09) -> delete the export and verify it absent. |

## Evidence format families covered

- `record_lookup` log lines (`c01`, `c02`, `c08`, `c10`, `c11`, `c12`)
- `service_resource_export` JSON (`c03`, `c04`)
- snapshot-bundle JSON (`c05`, `c06`, `c07`, `c11`)
- SQLite-compatible replica SQL dumps (`c02`)
- handover text (`c12`)
- `records[]` manifest JSON (`c09`)

## Conventions

- All timestamps are UTC ISO-8601 with `Z`, all data is fictional, and all
  evidence files are small enough to review by hand.
- Each case's `evidence_files.csv` captured_at values that are cited by
  `expected.csv` are `<= audit_at`; cases may also ship post-window decoys.
- `expected.csv` uses the same header (`case_id,deletion_status,steps_json`) and
  step schema as the real submission, and is itself checked by
  `python evaluation/evaluate.py cases` / `tests/test_eval_*.py`.
