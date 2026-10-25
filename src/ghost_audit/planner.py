"""Planner: turn a reconstructed scenario into the required output steps.

Input: one :class:`~ghost_audit.scenario.ResourceState` per relevant resource at
``audit_at``. Output: the case ``deletion_status`` plus the ordered step list.

The planning rules are deliberately simple and are validated against the 32
labelled sample cases:

* findings describe resources whose current state violates the policy (or an
  active data flow / restorable backup);
* remediation actions fix each finding, ordered by the operation table;
* verification checks cover the target state of every resource in the scenario,
  including the policy-required retained records and the account baseline;
* for COMPLETE cases only verification steps are emitted, each citing evidence;
* for INCOMPLETE cases the remediation and future verification steps carry an
  empty ``evidence_id`` (they describe future work).
"""

from __future__ import annotations

from dataclasses import dataclass

from .evidence.base import (
    ST_ABSENT,
    ST_ACTIVE,
    ST_ANONYMIZED,
    ST_BLOCKED,
    ST_DISABLED,
    ST_PRESENT,
    ST_REDACTED,
    ST_RETAINED,
    ST_UNKNOWN,
)
from .policy import (
    ACTION_BLOCK_RESTORE,
    ACTION_DELETE,
    ACTION_DISABLE_FLOW,
    ACTION_RETAIN,
    OPERATION_ORDER,
    PHASE_ORDER,
    OP_ACTIVE_FLOW,
    OP_ANONYMIZE,
    OP_BLOCK_RESTORE,
    OP_DELETE,
    OP_DISABLE_FLOW,
    OP_EXPOSED,
    OP_REDACT,
    OP_REMAINING,
    OP_RESTORE_RISK,
    OP_VERIFY_ABSENT,
    OP_VERIFY_ANONYMIZED,
    OP_VERIFY_FLOW_DISABLED,
    OP_VERIFY_PRESENT,
    OP_VERIFY_REDACTED,
    OP_VERIFY_RESTORE_BLOCKED,
    treatment_for,
)

STATUS_COMPLETE = "COMPLETE"
STATUS_INCOMPLETE = "INCOMPLETE"
STATUS_NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass(frozen=True)
class ResourceState:
    """One resource's state at ``audit_at`` plus the evidence that proves it."""

    resource_id: str
    system_id: str
    record_type: str
    state: str
    evidence_id: str = ""
    observed_at: str | None = None
    source: str = "evidence"  # evidence | activity | baseline
    identifiers: tuple[tuple[str, str], ...] = ()

    def ident(self) -> dict[str, str]:
        return dict(self.identifiers)


@dataclass(frozen=True)
class Step:
    step_no: int
    phase: str
    system_id: str
    resource_id: str
    operation: str
    state: str
    evidence_id: str

    def to_dict(self) -> dict[str, str | int]:
        return {
            "step_no": self.step_no,
            "phase": self.phase,
            "system_id": self.system_id,
            "resource_id": self.resource_id,
            "operation": self.operation,
            "state": self.state,
            "evidence_id": self.evidence_id,
        }


@dataclass
class CasePlan:
    case_id: str
    status: str
    steps: list[Step]
    notes: list[str]


class _Builder:
    def __init__(self) -> None:
        self.items: list[tuple[int, int, str, str, str, str, str]] = []

    def add(self, phase: str, system: str, resource: str, operation: str, state: str, evidence: str) -> None:
        self.items.append(
            (
                PHASE_ORDER[phase],
                OPERATION_ORDER[operation],
                system,
                resource,
                operation,
                state,
                evidence,
            )
        )

    def ordered(self) -> list[Step]:
        self.items.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
        steps: list[Step] = []
        for number, item in enumerate(self.items, start=1):
            _, _, system, resource, operation, state, evidence = item
            steps.append(Step(number, self.phase_name(item), system, resource, operation, state, evidence))
        return steps

    @staticmethod
    def phase_name(item) -> str:
        for phase, order in PHASE_ORDER.items():
            if order == item[0]:
                return phase
        raise AssertionError(item)


def _finding_for(state: ResourceState) -> tuple[str, str] | None:
    """Return ``(operation, state_word)`` for a violating resource, else None."""

    treatment = treatment_for(state.record_type)
    if treatment.finding_operation is None:
        return None
    if state.state == ST_UNKNOWN:
        return None
    if treatment.finding_operation == OP_REMAINING:
        return (OP_REMAINING, ST_PRESENT) if state.state == ST_PRESENT else None
    if treatment.finding_operation == OP_EXPOSED:
        return (OP_EXPOSED, ST_PRESENT) if state.state == ST_PRESENT else None
    if treatment.finding_operation == OP_ACTIVE_FLOW:
        return (OP_ACTIVE_FLOW, ST_ACTIVE) if state.state == ST_ACTIVE else None
    if treatment.finding_operation == OP_RESTORE_RISK:
        return (OP_RESTORE_RISK, ST_ACTIVE) if state.state == ST_ACTIVE else None
    return None


def plan_case(
    case_id: str,
    scenario: list[ResourceState],
    *,
    intent_authorizes_deletion: bool,
    notes: list[str] | None = None,
) -> CasePlan:
    """Produce the canonical answer for one case from its reconstructed scenario."""

    if not intent_authorizes_deletion:
        return CasePlan(case_id, STATUS_NOT_APPLICABLE, [], list(notes or []))

    builder = _Builder()
    notes_out = list(notes or [])
    findings: list[ResourceState] = []
    for resource in scenario:
        finding = _finding_for(resource)
        if finding is not None:
            operation, state_word = finding
            builder.add("FINDING", resource.system_id, resource.resource_id, operation, state_word, resource.evidence_id)
            findings.append(resource)

    # Remediation: follow the operation table order; only remediation actions
    # for resources that produced a finding. Retained records are never touched.
    if findings:
        for resource in findings:
            treatment = treatment_for(resource.record_type)
            if treatment.action == ACTION_DELETE:
                builder.add("REMEDIATION", resource.system_id, resource.resource_id, OP_DELETE, ST_ABSENT, "")
            elif treatment.action in ("REDACT",):
                builder.add("REMEDIATION", resource.system_id, resource.resource_id, OP_REDACT, ST_REDACTED, "")
            elif treatment.action in ("ANONYMIZE",):
                builder.add("REMEDIATION", resource.system_id, resource.resource_id, OP_ANONYMIZE, ST_ANONYMIZED, "")
            elif treatment.action == ACTION_DISABLE_FLOW:
                builder.add("REMEDIATION", resource.system_id, resource.resource_id, OP_DISABLE_FLOW, ST_DISABLED, "")
            elif treatment.action == ACTION_BLOCK_RESTORE:
                builder.add("REMEDIATION", resource.system_id, resource.resource_id, OP_BLOCK_RESTORE, ST_BLOCKED, "")

    has_findings = bool(findings)
    status = STATUS_INCOMPLETE if has_findings else STATUS_COMPLETE
    for resource in scenario:
        treatment = treatment_for(resource.record_type)
        evidence = "" if has_findings else resource.evidence_id
        if not has_findings and not evidence:
            # A COMPLETE verification must cite the evidence that proves the
            # current state; without available evidence the check cannot be
            # asserted and is omitted (recorded as a scenario note instead).
            notes_out.append(f"omitted uncited verification for {resource.resource_id}")
            continue
        if treatment.action == ACTION_RETAIN:
            builder.add(
                "VERIFICATION", resource.system_id, resource.resource_id, OP_VERIFY_PRESENT, ST_PRESENT, evidence
            )
        elif treatment.action == ACTION_BLOCK_RESTORE:
            builder.add(
                "VERIFICATION", resource.system_id, resource.resource_id, OP_VERIFY_RESTORE_BLOCKED, ST_BLOCKED, evidence
            )
        elif treatment.action == ACTION_DISABLE_FLOW:
            builder.add(
                "VERIFICATION", resource.system_id, resource.resource_id, OP_VERIFY_FLOW_DISABLED, ST_DISABLED, evidence
            )
        elif treatment.action == ACTION_DELETE:
            builder.add("VERIFICATION", resource.system_id, resource.resource_id, OP_VERIFY_ABSENT, ST_ABSENT, evidence)
        elif treatment.action == "REDACT":
            builder.add(
                "VERIFICATION", resource.system_id, resource.resource_id, OP_VERIFY_REDACTED, ST_REDACTED, evidence
            )
        elif treatment.action == "ANONYMIZE":
            builder.add(
                "VERIFICATION", resource.system_id, resource.resource_id, OP_VERIFY_ANONYMIZED, ST_ANONYMIZED, evidence
            )
        else:  # pragma: no cover - defensive
            notes_out.append(f"unknown treatment action for {resource.record_type}")
    return CasePlan(case_id, status, builder.ordered(), notes_out)
