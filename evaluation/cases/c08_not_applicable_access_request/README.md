# c08 - unauthorized (non-deletion) request => NOT_APPLICABLE + []

- Case: `case_h08`, audit_at `2026-09-15T00:00:00Z`.
- Hand-written expected status: **NOT_APPLICABLE**, `steps_json=[]`.

## Why the answer is NOT_APPLICABLE

1. `request_text` asks for a *copy* of the personal data ("access") and a
   *correction* of the mailing address, and explicitly says the account should
   **not** be deleted. The problem statement says a request for access,
   correction, export, marketing opt-out or another non-deletion action is
   `NOT_APPLICABLE`, even if other fields resemble a deletion case.
2. The workflow status `NOT_STARTED` and the existing account record in `ev_h08`
   are consistent with an access/correction request; they are not deletion
   evidence.
3. No deletion is authorized, so no findings, remediation or verification may
   be planned: `steps_json` must be the empty array.

This case guards against a pipeline that treats every request row as a deletion
case (possibly driven by `reported_status` or by an account that exists).
