# c10 - support message with shared context must be REDACT, attachment must be DELETE

- Case: `case_h10`, audit_at `2026-09-13T12:00:00Z`.
- Hand-written expected status: **INCOMPLETE**.

## Why the two support records get different treatments

1. The support **message** `support_h10` is still `IDENTIFIABLE` (contains
   `eva.novak@example.com`) but the request explicitly says the conversation also
   relates to another customer, so it must be retained as conversation context.
   Rule `rule_05 SUPPORT_MESSAGE / Conversation contains shared customer, agent,
   or order context that must be retained / REDACT` therefore requires
   `REDACT_RECORD` and a `REPORT_EXPOSED_PII` FINDING while it is identifiable.
2. The support **attachment** `support_attach_h10` belongs only to the deletion
   subject (`result: FOUND`, subject email), so rule
   `rule_06 SUPPORT_ATTACHMENT / Attachment belongs only to the deletion
   subject / DELETE` requires `DELETE_RECORD` and a
   `REPORT_REMAINING_RECORD` FINDING.
3. The account profile is absent, so a baseline `VERIFY_RECORD_ABSENT` for
   `account_h10` is part of the final-state verification.

## Expected steps

Findings in operation-table order: `REPORT_REMAINING_RECORD` (attachment)
before `REPORT_EXPOSED_PII` (message). Remediation follows the operation table:
`DELETE_RECORD` before `REDACT_RECORD`. Verifications: absent account, absent
attachment, redacted message. Only findings cite evidence; remediation and
future verification use an empty evidence_id.

This case fails a pipeline that blindly applies one treatment per
`SUPPORT_*` category or that deletes shared-context conversations.
