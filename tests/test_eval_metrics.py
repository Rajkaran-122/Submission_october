"""Metric-math tests for the independent evaluation harness.

The fixtures under ``tests/fixtures/eval/`` ship a static gold answer
(``metrics_gold.csv``) and several mutated predictions that reproduce classic
pipeline bugs. The expected metric values below are derived by hand from the
fixture contents; no pipeline is involved.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evaluation import evaluate  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "eval"
REQUESTS = FIXTURES / "requests_min.csv"
DATASET = FIXTURES / "dataset_min"
GOLD = FIXTURES / "metrics_gold.csv"

# The fixture gold has four steps in total:
#   case_001: FINDING + REMEDIATION + VERIFICATION (3)
#   case_002: VERIFICATION (1)
#   case_003: [] (NOT_APPLICABLE)
GOLD_STEP_COUNT = 4
CASE_COUNT = 3


def _evaluate(fixture_name: str) -> evaluate.Report:
    return evaluate.evaluate_predictions(
        FIXTURES / fixture_name, GOLD, requests_csv=REQUESTS, dataset_dir=DATASET
    )


# ---------------------------------------------------------------------------
# Perfect prediction
# ---------------------------------------------------------------------------


def test_gold_self_comparison_is_perfect() -> None:
    report = _evaluate("metrics_gold.csv")
    metrics = report.metrics
    assert metrics["cases"] == CASE_COUNT
    assert metrics["status_accuracy"] == 1.0
    assert metrics["step_precision"] == 1.0
    assert metrics["step_recall"] == 1.0
    assert metrics["step_f1"] == 1.0
    assert metrics["sequence_accuracy"] == 1.0
    assert metrics["ordered_treatment_accuracy"] == 1.0
    assert metrics["evidence_accuracy"] == 1.0
    assert metrics["field_accuracy_overall"] == 1.0
    assert metrics["contract_errors"] == 0
    assert report.failures() == []
    assert report.is_clean()


# ---------------------------------------------------------------------------
# Mutation 1: trust reported_status (claim treated as proof)
# ---------------------------------------------------------------------------


def test_trusted_reported_status_is_detected() -> None:
    report = _evaluate("metrics_trust_reported_status.csv")
    metrics = report.metrics
    assert metrics["status_accuracy"] == pytest.approx(2 / 3)
    assert metrics["step_recall"] < 1.0
    case = report.case("case_001")
    assert case is not None
    assert case.gold_status == "INCOMPLETE"
    assert case.pred_status == "COMPLETE"
    assert not case.status_ok
    missing_operations = {step["operation"] for step in case.missing}
    assert "REPORT_REMAINING_RECORD" in missing_operations
    assert "DELETE_RECORD" in missing_operations
    assert case.likely_stage
    assert any("case_001" in line for line in report.summary().splitlines())


# ---------------------------------------------------------------------------
# Mutation 2: ignore the historical email (wrong identity resolution)
# ---------------------------------------------------------------------------


def test_ignored_historical_identity_is_detected() -> None:
    report = _evaluate("metrics_ignore_historical_email.csv")
    metrics = report.metrics
    assert metrics["status_accuracy"] == pytest.approx(2 / 3)
    case = report.case("case_001")
    assert case is not None
    assert not case.status_ok
    assert case.pred_status == "COMPLETE"
    # The mutation "found" a different (current-email) account and cited late
    # evidence; both are reported.
    assert {step["resource_id"] for step in case.extra} == {"account_101"}
    assert "evidence_after_audit_at" in {issue.code for issue in report.issues}


# ---------------------------------------------------------------------------
# Mutation 3: drop a remediation step
# ---------------------------------------------------------------------------


def test_dropped_remediation_step_is_detected() -> None:
    report = _evaluate("metrics_drop_remediation.csv")
    metrics = report.metrics
    assert metrics["status_accuracy"] == 1.0  # status still right
    assert metrics["step_precision"] == 1.0
    assert metrics["step_recall"] == pytest.approx(3 / 4)
    assert metrics["step_f1"] == pytest.approx(6 / 7)
    assert metrics["sequence_accuracy"] == pytest.approx(2 / 3)
    case = report.case("case_001")
    assert case is not None
    assert [step["operation"] for step in case.missing] == ["DELETE_RECORD"]
    assert "remediation planning" in case.likely_stage


# ---------------------------------------------------------------------------
# Mutation 4: wrong step order
# ---------------------------------------------------------------------------


def test_wrong_step_order_is_detected() -> None:
    report = _evaluate("metrics_wrong_order.csv")
    metrics = report.metrics
    assert metrics["status_accuracy"] == 1.0
    assert metrics["step_f1"] == 1.0  # same multiset of steps
    assert metrics["sequence_accuracy"] == pytest.approx(2 / 3)
    assert metrics["contract_errors"] > 0
    codes = {issue.code for issue in report.issues}
    assert codes & {"phase_order", "step_no_sequence", "step_ordering"}
    case = report.case("case_001")
    assert case is not None
    assert case.matched == len(case.gold_steps)
    assert any("order" in note for note in case.notes)


# ---------------------------------------------------------------------------
# Mutation 5: wrong remediation operation
# ---------------------------------------------------------------------------


def test_wrong_remediation_operation_is_detected() -> None:
    report = _evaluate("metrics_wrong_remediation.csv")
    metrics = report.metrics
    assert metrics["status_accuracy"] == 1.0
    assert metrics["step_f1"] == pytest.approx(3 / 4)
    case = report.case("case_001")
    assert case is not None
    assert [step["operation"] for step in case.missing] == ["DELETE_RECORD"]
    assert [step["operation"] for step in case.extra] == ["REDACT_RECORD"]
    assert "remediation planning" in case.likely_stage


# ---------------------------------------------------------------------------
# Report serialization
# ---------------------------------------------------------------------------


def test_report_json_and_markdown_are_written(tmp_path) -> None:
    report = _evaluate("metrics_trust_reported_status.csv")
    json_path = report.write_json(tmp_path / "report.json")
    markdown_path = report.write_markdown(tmp_path / "evaluation_report.md")
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["metrics"]["status_accuracy"] == pytest.approx(2 / 3)
    assert any(case["case_id"] == "case_001" for case in payload["cases"])
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "Case-level deletion-status accuracy" in markdown
    assert "case_001" in markdown


# ---------------------------------------------------------------------------
# CLI entry points (exit codes are the CI gate)
# ---------------------------------------------------------------------------


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "evaluation" / "evaluate.py"), *args],
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_cli_validate_exit_codes() -> None:
    valid = _run_cli(
        "validate",
        "--output",
        str(FIXTURES / "valid_output.csv"),
        "--requests",
        str(REQUESTS),
        "--dataset",
        str(DATASET),
    )
    assert valid.returncode == 0, valid.stdout + valid.stderr
    assert "0 error(s)" in valid.stdout

    invalid = _run_cli(
        "validate",
        "--output",
        str(FIXTURES / "wrong_op_order.csv"),
        "--requests",
        str(REQUESTS),
        "--dataset",
        str(DATASET),
    )
    assert invalid.returncode == 1
    assert "ERROR" in invalid.stdout


def test_cli_regress_exit_codes_and_json(tmp_path) -> None:
    report_path = tmp_path / "regression.json"
    clean = _run_cli(
        "regress",
        "--pred",
        str(GOLD),
        "--gold",
        str(GOLD),
        "--requests",
        str(REQUESTS),
        "--dataset",
        str(DATASET),
        "--json",
        str(report_path),
    )
    assert clean.returncode == 0, clean.stdout + clean.stderr
    assert "Status accuracy" in clean.stdout
    assert report_path.exists()

    mutated = _run_cli(
        "regress",
        "--pred",
        str(FIXTURES / "metrics_trust_reported_status.csv"),
        "--gold",
        str(GOLD),
        "--requests",
        str(REQUESTS),
        "--dataset",
        str(DATASET),
    )
    assert mutated.returncode == 1
    assert "case_001" in mutated.stdout
