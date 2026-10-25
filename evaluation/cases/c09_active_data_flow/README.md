# c09 - active integration data flow recreating data (INCOMPLETE)

- Case: `case_h09`, audit_at `2026-09-20T00:00:00Z`.
- Hand-written expected status: **INCOMPLETE**.

## Why the answer is INCOMPLETE

1. `ev_h09` shows `account_service/account_h09` as `NOT_FOUND`; the primary
   profile is gone. It also contains a decoy `account_9998` FOUND for an
   unrelated email, which must not be attributed to this case.
2. The same evidence shows `integration_service/data_flow_h09` as `ENABLED`
   (`result: ENABLED`) for `hana.sato@example.com`. An enabled connector can
   copy or recreate the user's data; per the problem statement a deletion is
   complete only when no active sync/import path can recreate the data. This is
   a `REPORT_ACTIVE_DATA_FLOW` FINDING and requires `DISABLE_DATA_FLOW`.
3. No retained security record exists for this subject in the evidence, so no
   `VERIFY_RECORD_PRESENT` step is required.

## Expected steps

| # | phase | operation | state | system/resource |
|---|---|---|---|---|
| 1 | FINDING | REPORT_ACTIVE_DATA_FLOW | ACTIVE | integration_service/data_flow_h09 |
| 2 | REMEDIATION | DISABLE_DATA_FLOW | DISABLED | integration_service/data_flow_h09 |
| 3 | VERIFICATION | VERIFY_RECORD_ABSENT | ABSENT | account_service/account_h09 |
| 4 | VERIFICATION | VERIFY_DATA_FLOW_DISABLED | DISABLED | integration_service/data_flow_h09 |

The decoy `account_9998` proves that global evidence contains unrelated records;
it must be ignored. `activity_h14`'s `SUCCESS` claim does not outrank evidence.
