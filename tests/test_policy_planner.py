"""Tests for the policy table, planner ordering and the output contract."""

from __future__ import annotations

import csv
import json

import pytest

from ghost_audit.evidence.base import (
    RT_ACCOUNT_TOMBSTONE,
    RT_ANALYTICS_PROFILE,
    RT_BACKUP_ARCHIVE,
    RT_DATA_FLOW,
    RT_SECURITY_AUDIT_LOG,
    RT_SEARCH_INDEX_ENTRY,
    RT_SUPPORT_ATTACHMENT,
    RT_SUPPORT_MESSAGE,
    RT_USER_EXPORT,
    RT_USER_PROFILE,
    ST_ABSENT,
    ST_ACTIVE,
    ST_ANONYMIZED,
    ST_BLOCKED,
    ST_DISABLED,
    ST_PRESENT,
    ST_RETAINED,
)
from ghost_audit.output import check_steps, self_check, write_output
from ghost_audit.planner import ResourceState, plan_case
from ghost_audit.policy import OP_ANONYMIZE, OP_REDACT, treatment_for


def _state(resource_id, system_id, record_type, state, evidence="evidence_01"):
    return ResourceState(resource_id=resource_id, system_id=system_id, record_type=record_type, state=state, evidence_id=evidence)


def test_treatment_table_matches_deletion_rules():
    assert treatment_for(RT_USER_PROFILE).target_state == ST_ABSENT
    assert treatment_for(RT_SEARCH_INDEX_ENTRY).target_state == ST_ABSENT
    assert treatment_for(RT_SUPPORT_ATTACHMENT).target_state == ST_ABSENT
    assert treatment_for(RT_USER_EXPORT).target_state == ST_ABSENT
    assert treatment_for(RT_SUPPORT_MESSAGE).target_state == "REDACTED"
    assert treatment_for(RT_ANALYTICS_PROFILE).target_state == ST_ANONYMIZED
    assert treatment_for(RT_ACCOUNT_TOMBSTONE).target_state == "REDACTED"
    assert treatment_for(RT_SECURITY_AUDIT_LOG).target_state == ST_RETAINED
    assert treatment_for(RT_BACKUP_ARCHIVE).target_state == ST_BLOCKED
    assert treatment_for(RT_DATA_FLOW).target_state == ST_DISABLED


def test_complete_case_is_verification_only():
    scenario = [
        _state("account_001", "account_service", RT_USER_PROFILE, ST_ABSENT),
        _state("audit_record_001", "security_service", RT_SECURITY_AUDIT_LOG, ST_RETAINED),
        _state("analytics_001", "analytics_platform", RT_ANALYTICS_PROFILE, ST_ANONYMIZED),
    ]
    plan = plan_case("case_01", scenario, intent_authorizes_deletion=True)
    assert plan.status == "COMPLETE"
    assert [step.operation for step in plan.steps] == [
        "VERIFY_RECORD_ABSENT",
        "VERIFY_RECORD_PRESENT",
        "VERIFY_RECORD_ANONYMIZED",
    ]
    assert all(step.phase == "VERIFICATION" for step in plan.steps)
    assert all(step.evidence_id for step in plan.steps)
    assert [step.step_no for step in plan.steps] == [1, 2, 3]


def test_incomplete_case_has_full_plan_and_empty_future_evidence():
    scenario = [
        _state("account_002", "account_service", RT_USER_PROFILE, ST_PRESENT),
        _state("search_002", "search_service", RT_SEARCH_INDEX_ENTRY, ST_PRESENT),
        _state("audit_record_002", "security_service", RT_SECURITY_AUDIT_LOG, ST_RETAINED),
    ]
    plan = plan_case("case_02", scenario, intent_authorizes_deletion=True)
    assert plan.status == "INCOMPLETE"
    operations = [step.operation for step in plan.steps]
    assert operations == [
        "REPORT_REMAINING_RECORD",
        "REPORT_REMAINING_RECORD",
        "DELETE_RECORD",
        "DELETE_RECORD",
        "VERIFY_RECORD_ABSENT",
        "VERIFY_RECORD_ABSENT",
        "VERIFY_RECORD_PRESENT",
    ]
    assert plan.steps[0].phase == "FINDING" and plan.steps[0].evidence_id == "evidence_01"
    assert plan.steps[2].phase == "REMEDIATION" and plan.steps[2].evidence_id == ""
    assert plan.steps[4].phase == "VERIFICATION" and plan.steps[4].evidence_id == ""


def test_restore_risk_and_flow_order():
    scenario = [
        _state("backup_001", "backup_service", RT_BACKUP_ARCHIVE, ST_ACTIVE),
        _state("data_flow_001", "integration_service", RT_DATA_FLOW, ST_ACTIVE),
        _state("account_003", "account_service", RT_USER_PROFILE, ST_ABSENT),
    ]
    plan = plan_case("case_03", scenario, intent_authorizes_deletion=True)
    assert plan.status == "INCOMPLETE"
    # Remediation follows the operation table: DISABLE_DATA_FLOW before BLOCK_RESTORE.
    assert [step.operation for step in plan.steps if step.phase == "REMEDIATION"] == [
        "DISABLE_DATA_FLOW",
        "BLOCK_RESTORE",
    ]


def test_not_applicable_has_empty_steps():
    plan = plan_case("case_09", [], intent_authorizes_deletion=False)
    assert plan.status == "NOT_APPLICABLE"
    assert plan.steps == []


def test_contract_catches_bad_rows():
    good = [
        {
            "step_no": 1,
            "phase": "VERIFICATION",
            "system_id": "account_service",
            "resource_id": "account_001",
            "operation": "VERIFY_RECORD_ABSENT",
            "state": "ABSENT",
            "evidence_id": "evidence_01",
        }
    ]
    assert check_steps("case_01", "COMPLETE", good) == []
    bad_state = [dict(good[0], state="PRESENT")]
    assert any("state" in problem for problem in check_steps("case_01", "COMPLETE", bad_state))
    bad_phase = [dict(good[0], phase="REMEDIATION", operation="VERIFY_RECORD_ABSENT")]
    assert check_steps("case_01", "COMPLETE", bad_phase)
    missing_evidence = [dict(good[0], evidence_id="")]
    assert check_steps("case_01", "COMPLETE", missing_evidence)
    na_with_steps = check_steps("case_01", "NOT_APPLICABLE", good)
    assert na_with_steps


def test_numeric_case_id_sorting(tmp_path):
    from ghost_audit.planner import CasePlan

    plans = [CasePlan("case_550", "NOT_APPLICABLE", [], []), CasePlan("case_33", "NOT_APPLICABLE", [], []), CasePlan("case_100", "NOT_APPLICABLE", [], [])]
    target = tmp_path / "out.csv"
    write_output(target, plans)
    with target.open(encoding="utf-8") as handle:
        ids = [row[0] for row in csv.reader(handle)][1:]
    assert ids == ["case_33", "case_100", "case_550"]
    assert self_check(target, ["case_33", "case_100", "case_550"]) == []


def test_output_rows_are_valid_json(tmp_path):
    from ghost_audit.planner import CasePlan

    plan = plan_case("case_01", [_state("account_001", "account_service", RT_USER_PROFILE, ST_ABSENT)], intent_authorizes_deletion=True)
    target = tmp_path / "out.csv"
    write_output(target, [plan])
    with target.open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    steps = json.loads(rows[0]["steps_json"])
    assert steps[0]["step_no"] == 1
