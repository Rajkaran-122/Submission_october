"""Policy application: map data categories to required treatment and steps.

The authoritative source is ``dataset/deletion_rules.csv``. This module keeps a
small, auditable translation table from the policy categories to
(findings, remediation, target state, verification) semantics from the problem
statement. Category conditions are encoded as logic in the scenario layer; this
module only answers "given this category and current state, what is required?".
"""

from __future__ import annotations

from dataclasses import dataclass

from .evidence.base import (
    RT_ACCOUNT_TOMBSTONE,
    RT_ANALYTICS_PROFILE,
    RT_BACKUP_ARCHIVE,
    RT_CACHE_ENTRY,
    RT_DATA_FLOW,
    RT_EMAIL_DELIVERY_LOG,
    RT_LEGAL_HOLD_RECORD,
    RT_MARKETING_PROFILE,
    RT_PRODUCT_TELEMETRY,
    RT_SEARCH_INDEX_ENTRY,
    RT_SECONDARY_IDENTIFIER,
    RT_SECURITY_AUDIT_LOG,
    RT_SUPPORT_ATTACHMENT,
    RT_SUPPORT_MESSAGE,
    RT_USER_EXPORT,
    RT_USER_PROFILE,
    ST_ABSENT,
    ST_ANONYMIZED,
    ST_BLOCKED,
    ST_DISABLED,
    ST_PRESENT,
    ST_REDACTED,
    ST_RETAINED,
    ST_UNKNOWN,
)

# Actions.
ACTION_DELETE = "DELETE"
ACTION_REDACT = "REDACT"
ACTION_ANONYMIZE = "ANONYMIZE"
ACTION_RETAIN = "RETAIN"
ACTION_DISABLE_FLOW = "DISABLE_FLOW"
ACTION_BLOCK_RESTORE = "BLOCK_RESTORE"

# Finding / remediation / verification operations from the allowed table.
OP_REMAINING = "REPORT_REMAINING_RECORD"
OP_EXPOSED = "REPORT_EXPOSED_PII"
OP_ACTIVE_FLOW = "REPORT_ACTIVE_DATA_FLOW"
OP_RESTORE_RISK = "REPORT_RESTORE_RISK"

OP_DISABLE_FLOW = "DISABLE_DATA_FLOW"
OP_BLOCK_RESTORE = "BLOCK_RESTORE"
OP_DELETE = "DELETE_RECORD"
OP_REDACT = "REDACT_RECORD"
OP_ANONYMIZE = "ANONYMIZE_RECORD"

OP_VERIFY_ABSENT = "VERIFY_RECORD_ABSENT"
OP_VERIFY_PRESENT = "VERIFY_RECORD_PRESENT"
OP_VERIFY_REDACTED = "VERIFY_RECORD_REDACTED"
OP_VERIFY_ANONYMIZED = "VERIFY_RECORD_ANONYMIZED"
OP_VERIFY_FLOW_DISABLED = "VERIFY_DATA_FLOW_DISABLED"
OP_VERIFY_RESTORE_BLOCKED = "VERIFY_RESTORE_BLOCKED"

#: Allowed (phase, operation, state) triples, used by the contract validator too.
ALLOWED_STEPS: dict[tuple[str, str], str] = {
    ("FINDING", OP_REMAINING): ST_PRESENT,
    ("FINDING", OP_EXPOSED): ST_PRESENT,
    ("FINDING", OP_ACTIVE_FLOW): "ACTIVE",
    ("FINDING", OP_RESTORE_RISK): "ACTIVE",
    ("REMEDIATION", OP_DISABLE_FLOW): ST_DISABLED,
    ("REMEDIATION", OP_BLOCK_RESTORE): ST_BLOCKED,
    ("REMEDIATION", OP_DELETE): ST_ABSENT,
    ("REMEDIATION", OP_REDACT): ST_REDACTED,
    ("REMEDIATION", OP_ANONYMIZE): ST_ANONYMIZED,
    ("VERIFICATION", OP_VERIFY_ABSENT): ST_ABSENT,
    ("VERIFICATION", OP_VERIFY_PRESENT): ST_PRESENT,
    ("VERIFICATION", OP_VERIFY_REDACTED): ST_REDACTED,
    ("VERIFICATION", OP_VERIFY_ANONYMIZED): ST_ANONYMIZED,
    ("VERIFICATION", OP_VERIFY_FLOW_DISABLED): ST_DISABLED,
    ("VERIFICATION", OP_VERIFY_RESTORE_BLOCKED): ST_BLOCKED,
}

PHASE_ORDER = {"FINDING": 0, "REMEDIATION": 1, "VERIFICATION": 2}

OPERATION_ORDER = {
    # Within each phase, the problem statement's table order is used.
    OP_REMAINING: 0,
    OP_EXPOSED: 1,
    OP_ACTIVE_FLOW: 2,
    OP_RESTORE_RISK: 3,
    OP_DISABLE_FLOW: 0,
    OP_BLOCK_RESTORE: 1,
    OP_DELETE: 2,
    OP_REDACT: 3,
    OP_ANONYMIZE: 4,
    OP_VERIFY_ABSENT: 0,
    OP_VERIFY_PRESENT: 1,
    OP_VERIFY_REDACTED: 2,
    OP_VERIFY_ANONYMIZED: 3,
    OP_VERIFY_FLOW_DISABLED: 4,
    OP_VERIFY_RESTORE_BLOCKED: 5,
}


@dataclass(frozen=True)
class Treatment:
    action: str
    target_state: str
    finding_operation: str | None
    remediation_operation: str | None
    verification_operation: str

    @property
    def is_retained(self) -> bool:
        return self.action in (ACTION_RETAIN, ACTION_BLOCK_RESTORE)


def treatment_for(record_type: str) -> Treatment:
    """Return the required treatment for a data category.

    ``BACKUP_ARCHIVE`` is retained but must not be restorable: the observable
    final state is BLOCKED and the finding is a restore risk. ``SECURITY_AUDIT``
    and ``LEGAL_HOLD`` records are policy-required retained records.
    """

    if record_type in (
        RT_USER_PROFILE,
        RT_SECONDARY_IDENTIFIER,
        RT_SEARCH_INDEX_ENTRY,
        RT_CACHE_ENTRY,
        RT_SUPPORT_ATTACHMENT,
        RT_USER_EXPORT,
        RT_MARKETING_PROFILE,
    ):
        return Treatment(ACTION_DELETE, ST_ABSENT, OP_REMAINING, OP_DELETE, OP_VERIFY_ABSENT)
    if record_type in (RT_SUPPORT_MESSAGE, RT_EMAIL_DELIVERY_LOG, RT_ACCOUNT_TOMBSTONE):
        return Treatment(ACTION_REDACT, ST_REDACTED, OP_EXPOSED, OP_REDACT, OP_VERIFY_REDACTED)
    if record_type in (RT_ANALYTICS_PROFILE, RT_PRODUCT_TELEMETRY):
        return Treatment(ACTION_ANONYMIZE, ST_ANONYMIZED, OP_EXPOSED, OP_ANONYMIZE, OP_VERIFY_ANONYMIZED)
    if record_type in (RT_SECURITY_AUDIT_LOG, RT_LEGAL_HOLD_RECORD):
        return Treatment(ACTION_RETAIN, ST_RETAINED, None, None, OP_VERIFY_PRESENT)
    if record_type == RT_BACKUP_ARCHIVE:
        return Treatment(ACTION_BLOCK_RESTORE, ST_BLOCKED, OP_RESTORE_RISK, OP_BLOCK_RESTORE, OP_VERIFY_RESTORE_BLOCKED)
    if record_type == RT_DATA_FLOW:
        return Treatment(ACTION_DISABLE_FLOW, ST_DISABLED, OP_ACTIVE_FLOW, OP_DISABLE_FLOW, OP_VERIFY_FLOW_DISABLED)
    # Unknown categories are never guessed: treat as a retained/unknown record
    # that will surface as a scenario warning instead of an invented deletion.
    return Treatment(ACTION_RETAIN, ST_UNKNOWN, None, None, OP_VERIFY_PRESENT)


def is_compliant(record_type: str, state: str) -> bool:
    """True when a resource already has the treatment required by policy."""

    treatment = treatment_for(record_type)
    if state == ST_UNKNOWN:
        return False
    if treatment.action == ACTION_RETAIN:
        return state in (ST_RETAINED, ST_PRESENT)
    if treatment.action == ACTION_BLOCK_RESTORE:
        return state in (ST_BLOCKED, ST_DISABLED)
    return state == treatment.target_state
