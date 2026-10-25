# c07 - tombstone still exposing PII (INCOMPLETE)

- Case: `case_h07`, audit_at `2026-09-15T12:00:00Z`.
- Hand-written expected status: **INCOMPLETE**.

## Why the answer is INCOMPLETE

1. `rule_15 ACCOUNT_TOMBSTONE / deleted account and only a stable internal
   user_id is needed / REDACT` permits the stable reference `user_h07` to remain
   but requires the direct identifiers to be removed.
2. `ev_h07` shows tombstone `tombstone_h07` still carries `name`,
   `email`, `phone` and `email_hash`. That is exposed PII at audit_at, so a
   `REPORT_EXPOSED_PII` FINDING is required; the remediation is `REDACT_RECORD`.
3. The account profile itself is absent (empty `customer_profiles` collection)
   and the retained `security_service/audit_h07` record must remain present
   (rule `rule_12 ... RETAIN`).

## Expected steps

| # | phase | operation | state | system/resource |
|---|---|---|---|---|
| 1 | FINDING | REPORT_EXPOSED_PII | PRESENT | identity_service/tombstone_h07 |
| 2 | REMEDIATION | REDACT_RECORD | REDACTED | identity_service/tombstone_h07 |
| 3 | VERIFICATION | VERIFY_RECORD_ABSENT | ABSENT | account_service/account_h07 |
| 4 | VERIFICATION | VERIFY_RECORD_PRESENT | PRESENT | security_service/audit_h07 |
| 5 | VERIFICATION | VERIFY_RECORD_REDACTED | REDACTED | identity_service/tombstone_h07 |

Only step 1 cites evidence; steps 2 and the post-remediation verifications are
future work and use an empty evidence_id.
