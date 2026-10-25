# c13 - leftover user export archive (INCOMPLETE)

- Case: `case_h13`, audit_at `2026-09-14T00:00:00Z`.
- Hand-written expected status: **INCOMPLETE**.

## Why the answer is INCOMPLETE

1. `ev_h13` shows the account profile `account_h13` as `NOT_FOUND`, so rule
   `rule_01 USER_PROFILE / Always / DELETE` is satisfied for the primary copy.
2. The same evidence shows `export_service/export_h13` as `FOUND` for
   `olga.petrova@example.com`. Rule `rule_09 USER_EXPORT / Always / DELETE`
   requires the downloadable export to be removed; a still-present export can be
   downloaded and recreates the personal data outside the platform.
3. No retained security record, data flow or restore risk is evidenced, so the
   plan is: report the remaining export, delete it, and verify both the account
   and the export are absent in system_id order.

## Expected steps

| # | phase | operation | state | system/resource | evidence |
|---|---|---|---|---|---|
| 1 | FINDING | REPORT_REMAINING_RECORD | PRESENT | export_service/export_h13 | ev_h13 |
| 2 | REMEDIATION | DELETE_RECORD | ABSENT | export_service/export_h13 | (empty: future work) |
| 3 | VERIFICATION | VERIFY_RECORD_ABSENT | ABSENT | account_service/account_h13 | (empty: future work) |
| 4 | VERIFICATION | VERIFY_RECORD_ABSENT | ABSENT | export_service/export_h13 | (empty: future work) |
