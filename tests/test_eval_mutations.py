"""Mutation-validity tests: prove the evaluator catches classic pipeline bugs.

Each fixture under ``tests/fixtures/eval/metrics_*.csv`` is a deliberately
mutated prediction. The evaluator must (a) report a failure for the mutated
case, (b) name the case and the missing/extra steps, and (c) attribute a likely
pipeline stage. A separate test asserts the unmutated gold answer passes, so the
evaluator is not simply flagging everything.
"""

from __future__ import annotations

import ast
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

#: (fixture, mutation name, expected failing case, step operations that must be
#: named as missing, expected failing status pair)
MUTATIONS = [
    pytest.param(
        "metrics_trust_reported_status.csv",
        "trust reported_status claim",
        "case_001",
        {"REPORT_REMAINING_RECORD"},
        ("INCOMPLETE", "COMPLETE"),
        id="trust-reported-status",
    ),
    pytest.param(
        "metrics_ignore_historical_email.csv",
        "ignore historical email",
        "case_001",
        {"REPORT_REMAINING_RECORD", "DELETE_RECORD"},
        ("INCOMPLETE", "COMPLETE"),
        id="ignore-historical-email",
    ),
    pytest.param(
        "metrics_drop_remediation.csv",
        "drop remediation step",
        "case_001",
        {"DELETE_RECORD"},
        None,
        id="drop-remediation",
    ),
    pytest.param(
        "metrics_wrong_order.csv",
        "wrong step order",
        "case_001",
        set(),
        None,
        id="wrong-order",
    ),
    pytest.param(
        "metrics_wrong_remediation.csv",
        "wrong remediation operation",
        "case_001",
        {"DELETE_RECORD"},
        None,
        id="wrong-remediation-operation",
    ),
]


def _evaluate(fixture_name: str) -> evaluate.Report:
    return evaluate.evaluate_predictions(
        FIXTURES / fixture_name, GOLD, requests_csv=REQUESTS, dataset_dir=DATASET
    )


def test_baseline_prediction_is_clean() -> None:
    report = _evaluate("metrics_gold.csv")
    assert report.is_clean()
    assert report.failures() == []


@pytest.mark.parametrize(
    "fixture_name,mutation,case_id,missing_operations,status_pair", MUTATIONS
)
def test_evaluator_catches_mutation(
    fixture_name: str,
    mutation: str,
    case_id: str,
    missing_operations: set[str],
    status_pair: tuple[str, str] | None,
) -> None:
    report = _evaluate(fixture_name)
    failing_ids = {case.case_id for case in report.failures()}
    assert case_id in failing_ids, f"mutation {mutation!r} was not detected"

    case = report.case(case_id)
    assert case is not None
    assert case.likely_stage, f"no likely pipeline stage reported for {mutation!r}"
    named_operations = {step["operation"] for step in case.missing}
    if missing_operations:
        assert missing_operations <= named_operations, (
            f"mutation {mutation!r}: expected missing operations "
            f"{sorted(missing_operations)}, got {sorted(named_operations)}"
        )
    if status_pair is not None:
        assert (case.gold_status, case.pred_status) == status_pair
    summary = report.summary()
    assert case_id in summary
    assert case.likely_stage in summary


def test_evaluator_detects_at_least_four_distinct_mutations() -> None:
    detected = []
    for fixture_name, mutation, case_id, _, _ in [
        (param.values[0], param.values[1], param.values[2], None, None)
        for param in MUTATIONS
    ]:
        report = _evaluate(fixture_name)
        if any(case.case_id == case_id for case in report.failures()):
            detected.append(mutation)
    assert len(detected) >= 4, f"only detected {detected}"


def test_wrong_order_mutation_is_reported_as_ordering_problem() -> None:
    report = _evaluate("metrics_wrong_order.csv")
    case = report.case("case_001")
    assert case is not None
    assert case.matched == len(case.gold_steps)
    assert not case.ordered_treatment_ok
    codes = " ".join(issue.code for issue in report.issues)
    assert any(
        code in codes for code in ("phase_order", "step_ordering", "step_no_sequence")
    )


def test_evaluator_harness_never_imports_the_pipeline() -> None:
    """Independence guarantee: the harness must not import ghost_audit internals."""

    source = (REPO_ROOT / "evaluation" / "evaluate.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    pipeline_imports = [name for name in imported if name.startswith("ghost_audit")]
    assert pipeline_imports == [], f"evaluator must stay independent: {pipeline_imports}"


def test_expected_answers_are_static_files_not_generated() -> None:
    """Every case ships a static expected.csv; no generated-answer code exists."""

    cases_dir = REPO_ROOT / "evaluation" / "cases"
    case_dirs = sorted(path for path in cases_dir.iterdir() if path.is_dir())
    assert len(case_dirs) >= 12
    for case_dir in case_dirs:
        expected = case_dir / "expected.csv"
        assert expected.exists(), f"{case_dir.name} missing expected.csv"
        rows, issues = evaluate.load_output(expected)
        assert not issues, f"{case_dir.name} expected.csv is invalid: {issues}"
        assert len(rows) == 1
    python_files = list(cases_dir.rglob("*.py"))
    assert python_files == [], (
        "expected answers must be static fixtures; found generator scripts: "
        f"{[str(path) for path in python_files]}"
    )
