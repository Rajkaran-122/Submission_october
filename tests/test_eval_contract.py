"""Contract-validator tests for the independent evaluation harness.

These tests only use hand-written static fixtures under
``tests/fixtures/eval/`` (plus the read-only official dataset when present);
they do not need the answer-generation pipeline.
"""

from __future__ import annotations

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
OFFICIAL_DATASET = evaluate.DEFAULT_DATASET_DIR


def error_codes(issues) -> set[str]:
    return {issue.code for issue in issues if issue.severity == evaluate.ERROR}


def error_issues(issues):
    return [issue for issue in issues if issue.severity == evaluate.ERROR]


# ---------------------------------------------------------------------------
# Positive cases
# ---------------------------------------------------------------------------


def test_valid_output_is_contract_clean() -> None:
    issues = evaluate.validate_output(FIXTURES / "valid_output.csv", REQUESTS, DATASET)
    assert error_issues(issues) == []
    assert issues == []


def test_valid_activity_citation_is_accepted() -> None:
    """A FINDING may cite a participant-visible activity_id."""

    issues = evaluate.validate_output(
        FIXTURES / "valid_activity_citation.csv", REQUESTS, DATASET
    )
    assert error_issues(issues) == []


def test_without_dataset_only_structural_checks_run() -> None:
    """Unknown evidence ids cannot be cross-checked without a dataset directory."""

    issues = evaluate.validate_output(
        FIXTURES / "fake_evidence_id.csv", REQUESTS, dataset_dir=None
    )
    assert error_issues(issues) == []

    with_dataset = evaluate.validate_output(
        FIXTURES / "fake_evidence_id.csv", REQUESTS, DATASET
    )
    assert "unknown_evidence_id" in error_codes(with_dataset)


def test_missing_output_file_is_reported_not_raised(tmp_path) -> None:
    issues = evaluate.validate_output(tmp_path / "does_not_exist.csv", REQUESTS, DATASET)
    assert "output_missing" in error_codes(issues)


# ---------------------------------------------------------------------------
# Negative fixtures: every documented rule-violation is caught
# ---------------------------------------------------------------------------

INVALID_FIXTURES = [
    pytest.param("bad_header.csv", "output_header", id="header"),
    pytest.param("missing_row.csv", "missing_case_row", id="missing-row"),
    pytest.param("extra_row.csv", "extra_case_row", id="extra-row"),
    pytest.param("duplicate_row.csv", "duplicate_case_row", id="duplicate-row"),
    pytest.param("unsorted.csv", "rows_not_sorted", id="unsorted-rows"),
    pytest.param("bad_status.csv", "status_value", id="status-enum"),
    pytest.param("malformed_json.csv", "steps_json_invalid", id="malformed-json"),
    pytest.param("unknown_phase.csv", "phase_value", id="phase-enum"),
    pytest.param("bad_operation_state.csv", "operation_state_mismatch", id="op-state-pair"),
    pytest.param("bad_step_no.csv", "step_no_sequence", id="step-no"),
    pytest.param("wrong_phase_order.csv", "phase_order", id="phase-order"),
    pytest.param("wrong_op_order.csv", "step_ordering", id="operation-order"),
    pytest.param("na_with_steps.csv", "na_steps_not_empty", id="na-not-empty"),
    pytest.param(
        "complete_with_finding.csv", "complete_non_verification", id="complete-finding"
    ),
    pytest.param(
        "complete_missing_evidence.csv",
        "complete_evidence_missing",
        id="complete-evidence",
    ),
    pytest.param(
        "incomplete_no_finding.csv", "incomplete_no_finding", id="incomplete-finding"
    ),
    pytest.param(
        "incomplete_future_evidence.csv",
        "future_step_evidence",
        id="future-evidence",
    ),
    pytest.param(
        "finding_missing_evidence.csv", "finding_evidence_missing", id="finding-evidence"
    ),
    pytest.param("fake_evidence_id.csv", "unknown_evidence_id", id="unknown-evidence"),
    pytest.param(
        "post_audit_evidence.csv", "evidence_after_audit_at", id="post-audit-file"
    ),
    pytest.param(
        "post_audit_activity.csv", "evidence_after_audit_at", id="post-audit-activity"
    ),
]


@pytest.mark.parametrize("fixture_name,expected_code", INVALID_FIXTURES)
def test_invalid_fixture_is_rejected(fixture_name: str, expected_code: str) -> None:
    issues = evaluate.validate_output(FIXTURES / fixture_name, REQUESTS, DATASET)
    codes = error_codes(issues)
    assert expected_code in codes, (
        f"{fixture_name} should raise {expected_code}, got {sorted(codes)}"
    )
    assert error_issues(issues)


# ---------------------------------------------------------------------------
# Harness self-checks against the official sample and the team fixtures
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not (OFFICIAL_DATASET / "sample_output.csv").exists(),
    reason="official dataset not present",
)
def test_official_sample_output_satisfies_the_contract() -> None:
    """The published sample answers must pass our validator (no false positives)."""

    issues = evaluate.validate_output(
        OFFICIAL_DATASET / "sample_output.csv",
        OFFICIAL_DATASET / "sample_deletion_requests.csv",
        OFFICIAL_DATASET,
    )
    assert error_issues(issues) == [], "\n".join(str(issue) for issue in issues)


def test_team_case_fixtures_are_contract_clean() -> None:
    results = evaluate.validate_case_fixtures()
    assert len(results) >= 12, "expected at least 12 independently authored cases"
    problems = [
        f"{name}: {issue}"
        for name, issues in results
        for issue in error_issues(issues)
    ]
    assert not problems, "\n".join(problems)

def test_natural_numeric_case_id_order_is_canonical(tmp_path) -> None:
    """Canonical order is natural numeric: case_9 < case_33 < case_100 < case_550."""

    import csv

    requests_path = tmp_path / "requests.csv"
    output_path = tmp_path / "output.csv"
    natural_order = ["case_9", "case_33", "case_100"]
    with requests_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(evaluate.REQUEST_HEADER)
        for case_id in natural_order:
            writer.writerow(
                [case_id, f"{case_id}@example.com", "2026-09-01T00:00:00Z",
                 "NOT_STARTED", "2026-09-02T00:00:00Z", "2026-09-10T00:00:00Z",
                 "Please send me a copy of my data."]
            )

    def write_rows(order):
        with output_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(evaluate.OUTPUT_HEADER)
            for case_id in order:
                writer.writerow([case_id, "NOT_APPLICABLE", "[]"])

    # Natural/numeric order is contract-clean.
    write_rows(natural_order)
    issues = evaluate.validate_output(output_path, requests_path)
    assert error_issues(issues) == []
    assert issues == []

    # Lexicographic order is NOT canonical: warning only, not an error.
    lexicographic_order = ["case_100", "case_33", "case_9"]
    write_rows(lexicographic_order)
    issues = evaluate.validate_output(output_path, requests_path)
    assert error_issues(issues) == []
    assert {issue.code for issue in issues} == {"rows_lexicographic_order"}

    # A genuinely shuffled order is still an error.
    write_rows(["case_33", "case_9", "case_100"])
    issues = evaluate.validate_output(output_path, requests_path)
    assert "rows_not_sorted" in error_codes(issues)
