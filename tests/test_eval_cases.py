"""Independent-case tests: fixture integrity and pipeline end-to-end checks.

The hand-written ``evaluation/cases/*/expected.csv`` files are validated against
their own mini-dataset without the pipeline. The end-to-end tests run the
pipeline CLI per case and compare with the hand-written answer; they skip
gracefully until the Lead's ``python -m ghost_audit`` CLI exists.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evaluation import evaluate  # noqa: E402

CASES_DIR = evaluate.DEFAULT_CASES_DIR
OFFICIAL_DATASET = evaluate.DEFAULT_DATASET_DIR
PIPELINE_AVAILABLE, PIPELINE_REASON = evaluate.pipeline_available()

#: CI gating thresholds (documented in evaluation/evaluation_report.md and
#: evaluation/README.md). The official 32 samples are a *development* regression
#: set, so their thresholds allow some resource-attribution ambiguity; the 13
#: independent cases have hand-written deterministic answers, so they must be
#: exact.
SAMPLE_MIN_STEP_F1 = 0.90
SAMPLE_MIN_SEQUENCE_ACCURACY = 0.65
INDEPENDENT_REQUIRED = 1.0

REQUIRED_CASE_FILES = [
    "deletion_requests.csv",
    "user_identities.csv",
    "user_audit_log.csv",
    "system_activity.csv",
    "deletion_rules.csv",
    "evidence_files.csv",
    "expected.csv",
    "README.md",
]

REQUIRED_SCENARIO_KEYWORDS = {
    "historical": "historical email identity missed",
    "replica": "replica/secondary copy leftover",
    "resync": "deletion reported complete but re-synced afterwards",
    "restore": "backup restore risk",
    "retain": "must-retain security audit record",
    "tombstone_redacted": "permitted tombstone already redacted",
    "tombstone_exposing": "tombstone still exposing PII",
    "not_applicable": "unauthorized (non-deletion) request",
    "active_data_flow": "active integration data flow",
    "support": "support message shared context / attachment",
    "verification_only": "COMPLETE verification-only output",
    "citation": "evidence-citation window",
    "export": "leftover user export",
}


# ---------------------------------------------------------------------------
# Fixture self-checks (no pipeline)
# ---------------------------------------------------------------------------


def test_each_case_has_the_dataset_contract_files() -> None:
    case_dirs = evaluate.discover_case_dirs()
    assert len(case_dirs) >= 12, f"expected >= 12 cases, found {len(case_dirs)}"
    problems: list[str] = []
    for case_dir in case_dirs:
        for name in REQUIRED_CASE_FILES:
            if not (case_dir / name).exists():
                problems.append(f"{case_dir.name}: missing {name}")
        header, _ = evaluate.read_csv_rows(case_dir / "deletion_requests.csv")
        expected_header = [
            "case_id",
            "email",
            "requested_at",
            "reported_status",
            "reported_at",
            "audit_at",
            "request_text",
        ]
        if header != expected_header:
            problems.append(f"{case_dir.name}: wrong request header {header}")
    assert not problems, "\n".join(problems)


def test_evidence_manifest_points_at_existing_files() -> None:
    problems: list[str] = []
    for case_dir in evaluate.discover_case_dirs():
        manifest_path = case_dir / "evidence_files.csv"
        header, rows = evaluate.read_csv_rows(manifest_path)
        assert header == ["file_id", "file_path", "origin", "captured_at"]
        for row in rows:
            file_id, relative, origin, captured_at = row
            target = case_dir / relative
            if not target.exists():
                problems.append(f"{case_dir.name}/{file_id}: missing {relative}")
            if not captured_at.strip():
                problems.append(f"{case_dir.name}/{file_id}: empty captured_at")
    assert not problems, "\n".join(problems)


def test_case_answers_cover_the_required_scenarios() -> None:
    names = [case_dir.name for case_dir in evaluate.discover_case_dirs()]
    joined = " ".join(names)
    missing = [
        scenario for keyword, scenario in REQUIRED_SCENARIO_KEYWORDS.items() if keyword not in joined
    ]
    assert not missing, f"missing scenario cases: {missing} (found: {names})"

    statuses: dict[str, list[str]] = {"COMPLETE": [], "INCOMPLETE": [], "NOT_APPLICABLE": []}
    for case_dir in evaluate.discover_case_dirs():
        rows, _ = evaluate.load_output(case_dir / "expected.csv")
        assert len(rows) == 1
        statuses.setdefault(rows[0].deletion_status, []).append(case_dir.name)
    assert len(statuses["COMPLETE"]) >= 2
    assert len(statuses["INCOMPLETE"]) >= 5
    assert len(statuses["NOT_APPLICABLE"]) >= 1


def test_evidence_format_families_are_exercised() -> None:
    suffixes = {
        path.suffix
        for case_dir in evaluate.discover_case_dirs()
        for path in (case_dir / "evidence").rglob("*")
        if path.is_file()
    }
    for required in (".log", ".sql", ".json", ".txt"):
        assert required in suffixes, f"no {required} evidence file in any case: {suffixes}"


def test_case_readmes_trace_expected_answers_to_rules() -> None:
    problems: list[str] = []
    for case_dir in evaluate.discover_case_dirs():
        readme = (case_dir / "README.md").read_text(encoding="utf-8")
        if len(readme) < 400:
            problems.append(f"{case_dir.name}: README too short to explain rationale")
        if "rule_" not in readme and "problem statement" not in readme.lower():
            problems.append(f"{case_dir.name}: README does not cite deletion rules")
        rows, _ = evaluate.load_output(case_dir / "expected.csv")
        if rows and rows[0].deletion_status not in readme:
            problems.append(
                f"{case_dir.name}: README does not state expected status "
                f"{rows[0].deletion_status}"
            )
    assert not problems, "\n".join(problems)


def test_expected_answers_are_hand_written_static_data() -> None:
    """No case directory may contain generation code (expected answers are static)."""

    for case_dir in evaluate.discover_case_dirs():
        assert not list(case_dir.glob("*.py")), f"{case_dir.name} contains a python generator"
        expected = case_dir / "expected.csv"
        rows, issues = evaluate.load_output(expected)
        assert len(rows) == 1 and not issues
        # Hand-written expected answers are static JSON text; they must already
        # be contract-valid against their own dataset.
        assert [
            issue
            for issue in evaluate.validate_output(
                expected,
                requests_csv=case_dir / "deletion_requests.csv",
                dataset_dir=case_dir,
            )
            if issue.severity == evaluate.ERROR
        ] == []


# ---------------------------------------------------------------------------
# Official sample regression entry point
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not (OFFICIAL_DATASET / "sample_output.csv").exists(),
    reason="official dataset not present",
)
def test_official_sample_regression_harness_accepts_published_gold() -> None:
    report = evaluate.sample_regression(OFFICIAL_DATASET / "sample_output.csv")
    assert "development regression" in report.label
    assert report.is_clean()
    assert report.metrics["cases"] == 32


# ---------------------------------------------------------------------------
# End-to-end: pipeline CLI vs hand-written expected answers
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not PIPELINE_AVAILABLE, reason=PIPELINE_REASON)
def test_pipeline_matches_hand_written_case_statuses() -> None:
    """Independent cases are hand-authored -> status/F1/sequence must be exact."""

    try:
        report, _ = evaluate.run_cases(run_pipeline_flag=True, skip_images=True)
    except evaluate.PipelineError as exc:  # pragma: no cover - depends on Lead's CLI
        pytest.fail(f"pipeline CLI failed on the independent cases: {exc}")
    summary = report.summary()
    metrics = report.metrics
    assert metrics["contract_errors"] == 0, summary
    assert metrics["status_accuracy"] == INDEPENDENT_REQUIRED, summary
    assert metrics["step_f1"] == INDEPENDENT_REQUIRED, summary
    assert metrics["sequence_accuracy"] == INDEPENDENT_REQUIRED, summary


@pytest.mark.skipif(not PIPELINE_AVAILABLE, reason=PIPELINE_REASON)
@pytest.mark.skipif(
    not (OFFICIAL_DATASET / "sample_output.csv").exists(),
    reason="official dataset not present",
)
def test_official_sample_regression_with_pipeline_predictions() -> None:
    try:
        report = evaluate.sample_regression()
    except evaluate.PipelineError as exc:  # pragma: no cover - depends on Lead's CLI
        pytest.fail(f"pipeline CLI failed on the official samples: {exc}")
    summary = report.summary()
    metrics = report.metrics
    # Official 32-sample development regression thresholds (documented):
    # status must be exact; step F1 / sequence allow known resource-attribution
    # ambiguity in the public samples. These are regression gates, not a claim
    # of generalisation performance.
    assert metrics["contract_errors"] == 0, summary
    assert metrics["status_accuracy"] == 1.0, summary
    assert metrics["step_f1"] >= SAMPLE_MIN_STEP_F1, summary
    assert metrics["sequence_accuracy"] >= SAMPLE_MIN_SEQUENCE_ACCURACY, summary
