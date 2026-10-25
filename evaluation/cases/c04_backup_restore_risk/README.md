# c04 - backup restore risk

- Case: `case_h04`, audit_at `2026-09-22T00:00:00Z`.
- Hand-written expected status: **INCOMPLETE**.

## Why the answer is INCOMPLETE

1. `ev_h04` shows the account profile lookup returns 404, so the primary copy is
   gone.
2. The same evidence shows `backup_service/backup_h04` with
   `restore_allowed: true` and a direct email in the backup catalogue. Rule
   `rule_14 BACKUP_ARCHIVE` requires a blocked restore path; a restorable backup
   is an active recreation path, so a `REPORT_RESTORE_RISK` FINDING is required
   and `BLOCK_RESTORE` is the remediation.
3. A security audit record `audit_h04` is within retention; rule
   `rule_12 ... RETAIN` requires it to stay available and to be verified
   present.

## Expected steps

FINDING `REPORT_RESTORE_RISK`, REMEDIATION `BLOCK_RESTORE`, then VERIFICATION
`VERIFY_RECORD_ABSENT` (account), `VERIFY_RECORD_PRESENT` (retained audit
record), `VERIFY_RESTORE_BLOCKED` (backup). The future verification steps use an
empty evidence_id because they describe work after audit_at.
