# c01 - historical email identity missed by naive matching

- Case: `case_h01`, request email `amina.old@example.com`, audit_at `2026-09-10T00:00:00Z`.
- Hand-written expected status: **INCOMPLETE**.

## Why the answer is INCOMPLETE (traceable to the rules)

1. The request email is a *historical* address: `user_audit_log.csv` shows
   `user_h01` changed `amina.old@example.com` to `amina.new@example.com` on
   2026-05-01. The current `user_identities.csv` snapshot only carries the new
   address, so a matcher that only uses current identities cannot connect this
   request to `user_h01`.
2. `ev_h01` (captured 2026-09-09, before audit_at) records
   `account_service/account_h01` as `FOUND` for `amina.old@example.com`. Rule
   `rule_01 USER_PROFILE / Always / DELETE` therefore requires the whole profile
   to be removed.
3. The `system_activity.csv` row `activity_h01` reports `DELETE ... SUCCESS`, but
   the problem statement says reported workflow results are claims, not proof.
   The evidence shows the profile is still present at audit_at.

## Expected steps

| # | phase | operation | state | system/resource | evidence |
|---|---|---|---|---|---|
| 1 | FINDING | REPORT_REMAINING_RECORD | PRESENT | account_service/account_h01 | ev_h01 |
| 2 | REMEDIATION | DELETE_RECORD | ABSENT | account_service/account_h01 | (empty: future work) |
| 3 | VERIFICATION | VERIFY_RECORD_ABSENT | ABSENT | account_service/account_h01 | (empty: future work) |

No retained security record exists for this subject, so no
`VERIFY_RECORD_PRESENT` step is required.
