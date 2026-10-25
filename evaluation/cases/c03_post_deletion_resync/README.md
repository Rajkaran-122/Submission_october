# c03 - deletion reported complete but re-synced afterwards

- Case: `case_h03`, audit_at `2026-09-20T00:00:00Z`.
- Hand-written expected status: **INCOMPLETE**.

## Why the answer is INCOMPLETE

1. `reported_status=COMPLETE` and `activity_h05` claims the account deletion
   succeeded. The account itself is indeed gone: `ev_h03` shows
   `account_h03` profile lookup -> 404.
2. However `ev_h03` also shows `search_service.search_h03` returned as a hit at
   2026-09-18 (after the deletion) with direct identifiers, and
   `activity_h06` records a successful `SYNC` from account_service to
   search_service on 2026-09-18. Rule `rule_03 SEARCH_INDEX_ENTRY / Always /
   DELETE` is violated, and an active sync path can recreate the data.
3. Therefore two FINDINGs are required: the remaining search document
   (`REPORT_REMAINING_RECORD`) and the active data flow
   (`REPORT_ACTIVE_DATA_FLOW`). Because the data flow can re-create records,
   `DISABLE_DATA_FLOW` is planned before `DELETE_RECORD`.

## Expected steps

Findings (`REPORT_REMAINING_RECORD` before `REPORT_ACTIVE_DATA_FLOW`),
remediation (`DISABLE_DATA_FLOW` before `DELETE_RECORD`), then verification in
operation-table order: absent account, absent search document, retained security
audit record still present, data flow disabled.

`audit_h03` is a `SECURITY_AUDIT_LOG` within the retention period, so rule
`rule_12 ... RETAIN` means it must remain available and be verified present.
