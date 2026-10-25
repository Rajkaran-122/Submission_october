# c06 - permitted account tombstone already redacted (COMPLETE)

- Case: `case_h06`, audit_at `2026-09-16T20:00:00Z`.
- Hand-written expected status: **COMPLETE**.

## Why the answer is COMPLETE

1. `ev_h06` shows an empty `account_service.customer_profiles` collection and a
   `identity_service` tombstone `tombstone_h06` whose `name`, `email`, `phone`
   and `email_hash` are all null while the stable `subject_reference=user_h06`
   remains. Rule `rule_15 ACCOUNT_TOMBSTONE / deleted account and only a stable
   internal user_id is needed / REDACT` is satisfied: the stable reference is
   permitted, direct identifiers are gone.
2. A retained `security_service/audit_h06` record is required by rule
   `rule_12 ... RETAIN` and is present, so `VERIFY_RECORD_PRESENT` is required.
3. No remaining copy, active data flow or restore risk exists. All copies have
   the required treatment -> COMPLETE.

## Expected steps (verification only)

| # | phase | operation | state | system/resource | evidence |
|---|---|---|---|---|---|
| 1 | VERIFICATION | VERIFY_RECORD_ABSENT | ABSENT | account_service/account_h06 | ev_h06 |
| 2 | VERIFICATION | VERIFY_RECORD_PRESENT | PRESENT | security_service/audit_h06 | ev_h06 |
| 3 | VERIFICATION | VERIFY_RECORD_REDACTED | REDACTED | identity_service/tombstone_h06 | ev_h06 |

Ordering follows the operation table: ABSENT, PRESENT, REDACTED.
