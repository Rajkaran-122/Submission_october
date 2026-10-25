# c05 - must-retain security audit record (COMPLETE)

- Case: `case_h05`, audit_at `2026-09-16T12:00:00Z`.
- Hand-written expected status: **COMPLETE**.

## Why the answer is COMPLETE

1. `ev_h05` snapshot bundle shows an empty `account_service.customer_profiles`
   collection: the user profile has been deleted (rule `rule_01` satisfied).
2. The bundle also shows `security_service/audit_h05` for `user_h05`. Rule
   `rule_12 SECURITY_AUDIT_LOG / Record is within the supplied retention period
   / RETAIN` requires this record to stay available; the snapshot proves it is
   present and untouched. Retaining it is compliant, not a violation.
3. No other copies, active data flows or restorable backups appear in the
   evidence, and no tombstone exists for this subject. Every relevant copy has
   the required treatment, so the deletion is complete.

## Expected steps (verification only)

| # | phase | operation | state | system/resource | evidence |
|---|---|---|---|---|---|
| 1 | VERIFICATION | VERIFY_RECORD_ABSENT | ABSENT | account_service/account_h05 | ev_h05 |
| 2 | VERIFICATION | VERIFY_RECORD_PRESENT | PRESENT | security_service/audit_h05 | ev_h05 |

A COMPLETE case contains only evidence-backed VERIFICATION steps.
