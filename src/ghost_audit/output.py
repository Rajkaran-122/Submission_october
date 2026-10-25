"""Output writer and contract self-check.

The final ``output.csv`` must have exactly the header ``case_id,deletion_status,
steps_json``, one row per case sorted by ``case_id``, and a JSON array (compact,
deterministic key order) in the third column.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

from .planner import CasePlan
from .policy import ALLOWED_STEPS, OPERATION_ORDER, PHASE_ORDER

HEADER = ["case_id", "deletion_status", "steps_json"]


def case_sort_key(case_id: str) -> tuple:
    """Natural sort: case_33 < case_99 < case_100 < case_550."""

    match = re.search(r"(\d+)", case_id)
    return (int(match.group(1)) if match else 0, case_id)


def write_output(path: str | Path, plans: list[CasePlan]) -> None:
    """Write plans sorted by case_id using the exact required format."""

    ordered = sorted(plans, key=lambda plan: case_sort_key(plan.case_id))
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(HEADER)
        for plan in ordered:
            steps = [step.to_dict() for step in plan.steps]
            writer.writerow([plan.case_id, plan.status, json.dumps(steps, ensure_ascii=False)])


class ContractError(ValueError):
    pass


def check_steps(case_id: str, status: str, steps: list[dict]) -> list[str]:
    """Validate one row against the required step contract."""

    problems: list[str] = []
    if status not in {"COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"}:
        problems.append(f"{case_id}: invalid status {status!r}")
    if status == "NOT_APPLICABLE":
        if steps:
            problems.append(f"{case_id}: NOT_APPLICABLE must have an empty steps array")
        return problems
    if status == "COMPLETE":
        if any(step.get("phase") != "VERIFICATION" for step in steps):
            problems.append(f"{case_id}: COMPLETE may only contain VERIFICATION steps")
        if not steps:
            problems.append(f"{case_id}: COMPLETE must contain verification steps")
    if status == "INCOMPLETE":
        phases = [step.get("phase") for step in steps]
        if "FINDING" not in phases:
            problems.append(f"{case_id}: INCOMPLETE must contain at least one FINDING")
        if "REMEDIATION" not in phases:
            problems.append(f"{case_id}: INCOMPLETE must contain REMEDIATION steps")
        if "VERIFICATION" not in phases:
            problems.append(f"{case_id}: INCOMPLETE must contain VERIFICATION steps")
    expected_keys = {"step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id"}
    last_phase = -1
    last_operation = -1
    last_system = ""
    last_resource = ""
    for index, step in enumerate(steps, start=1):
        if set(step) != expected_keys:
            problems.append(f"{case_id} step {index}: keys {sorted(set(step))} != required {sorted(expected_keys)}")
            continue
        if step["step_no"] != index:
            problems.append(f"{case_id} step {index}: step_no {step['step_no']!r} not consecutive")
        phase = step["phase"]
        operation = step["operation"]
        state = step["state"]
        if phase not in PHASE_ORDER:
            problems.append(f"{case_id} step {index}: invalid phase {phase!r}")
            continue
        if (phase, operation) not in ALLOWED_STEPS:
            problems.append(f"{case_id} step {index}: operation {operation!r} not allowed in phase {phase!r}")
            continue
        expected_state = ALLOWED_STEPS[(phase, operation)]
        if state != expected_state:
            problems.append(f"{case_id} step {index}: operation {operation} must use state {expected_state}, got {state}")
        order = PHASE_ORDER[phase]
        if order < last_phase:
            problems.append(f"{case_id} step {index}: phase order violation after {last_phase}")
        if order > last_phase:
            last_phase, last_operation, last_system, last_resource = order, -1, "", ""
        op_order = OPERATION_ORDER[operation]
        if order == last_phase:
            if op_order < last_operation:
                problems.append(f"{case_id} step {index}: operation order violation")
            elif op_order == last_operation:
                key = (step["system_id"], step["resource_id"])
                if key < (last_system, last_resource):
                    problems.append(f"{case_id} step {index}: system/resource order violation ({key})")
            last_operation = op_order
            last_system, last_resource = step["system_id"], step["resource_id"]
        evidence = step["evidence_id"]
        if phase == "FINDING" and not evidence:
            problems.append(f"{case_id} step {index}: FINDING must cite evidence")
        if status == "COMPLETE" and phase == "VERIFICATION" and not evidence:
            problems.append(f"{case_id} step {index}: COMPLETE verification must cite evidence")
        if status == "INCOMPLETE" and phase in ("REMEDIATION", "VERIFICATION") and evidence:
            problems.append(f"{case_id} step {index}: future {phase} step must use empty evidence_id")
    return problems


def self_check(path: str | Path, expected_case_ids: list[str] | None = None) -> list[str]:
    """Read back a written output.csv and validate the full contract."""

    problems: list[str] = []
    target = Path(path)
    with target.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration:
            return [f"{target}: empty file"]
        if header != HEADER:
            problems.append(f"{target}: header {header} != {HEADER}")
        rows = list(reader)
    case_ids = [row[0] for row in rows if row]
    if expected_case_ids is not None:
        expected = sorted(expected_case_ids, key=case_sort_key)
        if case_ids != expected:
            missing = sorted(set(expected) - set(case_ids))
            extra = sorted(set(case_ids) - set(expected))
            if missing:
                problems.append(f"{target}: missing cases {missing[:10]}")
            if extra:
                problems.append(f"{target}: unexpected cases {extra[:10]}")
            if not missing and not extra and case_ids != expected:
                problems.append(f"{target}: rows not sorted by case_id")
    for row in rows:
        if len(row) != 3:
            problems.append(f"{target}: malformed row {row[:2]}")
            continue
        case_id, status, payload = row
        try:
            steps = json.loads(payload)
        except json.JSONDecodeError as exc:
            problems.append(f"{case_id}: steps_json is not valid JSON ({exc})")
            continue
        if not isinstance(steps, list):
            problems.append(f"{case_id}: steps_json must be a JSON array")
            continue
        problems.extend(check_steps(case_id, status, steps))
    return problems
