# c02 - replica/secondary copy leftover

- Case: `case_h02`, audit_at `2026-09-14T00:00:00Z`.
- Hand-written expected status: **INCOMPLETE**.

## Why the answer is INCOMPLETE

1. `ev_h02a` shows the **primary** account lookup for `account_h02` returned
   `404`/`rows=0` at 2026-09-12T08:55:00Z: the primary copy is gone.
2. `ev_h02b` is a `read_replica` snapshot captured at 2026-09-12T09:00:00Z and
   still contains the full `USER_PROFILE` row for `user_h02` (name, email,
   phone). Rule `rule_01 USER_PROFILE / Always / DELETE` applies to every
   relevant copy, not only the primary, so the deletion is not complete.
3. The replica copy is a policy violation at audit_at, therefore a FINDING is
   required and a remediation/verification plan must follow.

## Expected steps

| # | phase | operation | state | system/resource | evidence |
|---|---|---|---|---|---|
| 1 | FINDING | REPORT_REMAINING_RECORD | PRESENT | account_service/account_h02 | ev_h02b (the replica snapshot) |
| 2 | REMEDIATION | DELETE_RECORD | ABSENT | account_service/account_h02 | (empty: future work) |
| 3 | VERIFICATION | VERIFY_RECORD_ABSENT | ABSENT | account_service/account_h02 | (empty: future work) |

`ev_h02a` alone would support a COMPLETE answer; the replica drift is the trap.
