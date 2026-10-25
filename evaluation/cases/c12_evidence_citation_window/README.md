# c12 - evidence citation window: never cite evidence captured after audit_at

- Case: `case_h12`, audit_at `2026-09-15T00:00:00Z`.
- Hand-written expected status: **COMPLETE**.

## Why the answer is COMPLETE and which evidence may be cited

1. The account profile is gone at audit_at. Both evidence files record
   `account_h12 USER_PROFILE NOT_FOUND` for `beatriz.silva@example.com`:
   - `ev_h12_pre` (shift handover) captured `2026-09-14T23:00:00Z`, *before*
     audit_at -> citable.
   - `ev_h12_post` captured `2026-09-20T10:00:00Z`, *after* audit_at -> must NOT
     be cited, even though it agrees with the verdict.
2. Rule `rule_01 USER_PROFILE / Always / DELETE` is satisfied at audit_at and
   no other copies, retained records, data flows or restore risks are
   evidenced, so a single `VERIFY_RECORD_ABSENT` verification proves the state.

## Expected steps

| # | phase | operation | state | system/resource | evidence |
|---|---|---|---|---|---|
| 1 | VERIFICATION | VERIFY_RECORD_ABSENT | ABSENT | account_service/account_h12 | ev_h12_pre |

The contract validator flags any citation of `ev_h12_post` with
`evidence_after_audit_at`; the evaluator's evidence accuracy also drops if a
pipeline cites it. This case exercises the "use only evidence at or before
audit_at" rule and the handover-text evidence format family.
