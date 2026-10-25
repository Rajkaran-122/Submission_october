# c11 - COMPLETE verification-only output (all compliant states)

- Case: `case_h11`, audit_at `2026-09-30T12:00:00Z`.
- Hand-written expected status: **COMPLETE** with verification steps only.

## Why the answer is COMPLETE

Every relevant copy already has the treatment required by `deletion_rules.csv`
at audit_at, and each fact is proven by evidence captured before audit_at:

| resource | rule | required action | compliant state | evidence |
|---|---|---|---|---|
| account_service/account_h11 | rule_01 USER_PROFILE | DELETE | NOT_FOUND | ev_h11a |
| search_service/search_h11 | rule_03 SEARCH_INDEX_ENTRY | DELETE | NOT_FOUND | ev_h11a |
| security_service/audit_h11 | rule_12 SECURITY_AUDIT_LOG | RETAIN | FOUND | ev_h11a |
| identity_service/tombstone_h11 | rule_15 ACCOUNT_TOMBSTONE | REDACT | direct identifiers null | ev_h11b |
| support_platform/support_h11 | rule_05 SUPPORT_MESSAGE | REDACT | REDACTED | ev_h11a |
| analytics_platform/analytics_h11 | rule_07 ANALYTICS_PROFILE | ANONYMIZE | ANONYMIZED | ev_h11a |
| integration_service/data_flow_h11 | active data flow | DISABLE | DISABLED | ev_h11a |
| backup_service/backup_h11 | rule_14 BACKUP_ARCHIVE | BLOCK_RESTORE | BLOCKED | ev_h11a |

No findings or remediation are permitted for a COMPLETE case; only
evidence-backed VERIFICATION steps are emitted. They are ordered by the
operation table: ABSENT (account, search), PRESENT (security), REDACTED
(identity tombstone before support message, system_id ascending), ANONYMIZED,
DATA_FLOW_DISABLED, RESTORE_BLOCKED.

## Expected steps

See `expected.csv`. Each step cites the evidence file that proves the state:
`ev_h11a` for the log-visible states, `ev_h11b` for the tombstone snapshot.
