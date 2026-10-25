"""Input-transform tests: predictions must not depend on presentation details.

Two layers are tested:

1. Without the pipeline: the hand-written fixtures stay contract-valid when the
   request rows are shuffled, ``reported_status`` claims are flipped, or decoy
   records are appended to the global dataset. These tests prove the evaluation
   fixtures do not accidentally depend on claims/row order.
2. With the pipeline CLI: the same transforms applied to the official dataset
   must leave the per-case predictions unchanged (shuffle / reported_status /
   decoys). These skip gracefully until the CLI exists.
"""

from __future__ import annotations

import csv
import io
import shutil
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
VALID_OUTPUT = FIXTURES / "valid_output.csv"
OFFICIAL_DATASET = evaluate.DEFAULT_DATASET_DIR
SAMPLE_REQUESTS = OFFICIAL_DATASET / "sample_deletion_requests.csv"
PIPELINE_AVAILABLE, PIPELINE_REASON = evaluate.pipeline_available()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_table(path: Path) -> tuple[list[str], list[list[str]]]:
    return evaluate.read_csv_rows(path)


def _write_table(path: Path, header: list[str], rows: list[list[str]]) -> None:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(buffer.getvalue(), encoding="utf-8")


def _flip_reported_status(value: str) -> str:
    return {
        "COMPLETE": "NOT_STARTED",
        "PARTIALLY_COMPLETE": "COMPLETE",
        "NOT_STARTED": "PARTIALLY_COMPLETE",
    }.get(value, value)


def _write_flipped_requests(source: Path, target: Path) -> None:
    header, rows = _read_table(source)
    index = header.index("reported_status")
    mutated = [list(row) for row in rows]
    for row in mutated:
        row[index] = _flip_reported_status(row[index])
    _write_table(target, header, mutated)


def _write_shuffled_requests(source: Path, target: Path) -> None:
    header, rows = _read_table(source)
    _write_table(target, header, list(reversed(rows)))


def _add_decoy_records(dataset_dir: Path) -> None:
    """Append unrelated identities/activity/evidence to a copied dataset."""

    decoy_csv_rows = {
        "user_identities.csv": [
            "user_decoy_9001",
            "Zelda Decoy",
            "zelda.decoy@example.com",
            "+1 202 555 0199",
            "f" * 64,
            "2026-09-01T00:00:00Z",
        ],
        "user_audit_log.csv": [
            "audit_decoy_9001",
            "user_decoy_9001",
            "2026-08-01T00:00:00Z",
            "email",
            "zelda.older@example.com",
            "zelda.decoy@example.com",
        ],
        "system_activity.csv": [
            "activity_decoy_9001",
            "2026-09-10T00:00:00Z",
            "SYNC",
            "account_service",
            "search_service",
            "search_decoy_9001",
            "SUCCESS",
            "Unrelated decoy sync.",
        ],
    }
    for filename, row in decoy_csv_rows.items():
        path = dataset_dir / filename
        header, rows = _read_table(path)
        _write_table(path, header, rows + [row])

    decoy_log = "\n".join(
        [
            '2026-09-29T00:00:00Z INFO service=downstream_delivery_service event=record_lookup seq=1 system="account_service" resource_id="account_decoy_9001" record_type="USER_PROFILE" result="FOUND" subject_reference="zelda.decoy@example.com"',
            '2026-09-29T00:00:00Z INFO service=downstream_delivery_service event=record_lookup seq=2 system="support_platform" resource_id="support_decoy_9001" record_type="SUPPORT_ATTACHMENT" result="FOUND" subject_reference="zelda.decoy@example.com"',
            "",
        ]
    )
    log_path = dataset_dir / "evidence" / "logs" / "decoy_trace_20260929.log"
    log_path.write_text(decoy_log, encoding="utf-8")
    manifest = dataset_dir / "evidence_files.csv"
    header, rows = _read_table(manifest)
    _write_table(
        manifest,
        header,
        rows
        + [
            [
                "ev_decoy_9001",
                "evidence/logs/decoy_trace_20260929.log",
                "central_observability_export",
                "2026-09-29T00:00:00Z",
            ]
        ],
    )


def _predictions_by_case(output_csv: Path) -> dict[str, tuple[str, list[dict]]]:
    rows, issues = evaluate.load_output(output_csv)
    assert not issues, "\n".join(str(issue) for issue in issues)
    return {row.case_id: (row.deletion_status, row.steps) for row in rows}


def _run_pipeline(dataset_dir: Path, requests_csv: Path, output_csv: Path) -> None:
    evaluate.run_pipeline(
        dataset_dir,
        requests_csv,
        output_csv,
        skip_images=True,
    )


# ---------------------------------------------------------------------------
# Layer 1: fixture-level transforms (no pipeline required)
# ---------------------------------------------------------------------------


def test_fixture_validation_is_invariant_to_request_row_order(tmp_path) -> None:
    shuffled = tmp_path / "requests_shuffled.csv"
    _write_shuffled_requests(REQUESTS, shuffled)
    issues = evaluate.validate_output(VALID_OUTPUT, shuffled, DATASET)
    assert [issue for issue in issues if issue.severity == evaluate.ERROR] == []


def test_fixture_validation_is_invariant_to_reported_status_claims(tmp_path) -> None:
    flipped = tmp_path / "requests_flipped.csv"
    _write_flipped_requests(REQUESTS, flipped)
    issues = evaluate.validate_output(VALID_OUTPUT, flipped, DATASET)
    assert [issue for issue in issues if issue.severity == evaluate.ERROR] == []


def test_fixture_validation_is_invariant_to_decoy_records(tmp_path) -> None:
    copied = tmp_path / "dataset_decoy"
    shutil.copytree(DATASET, copied)
    _add_decoy_records(copied)
    issues = evaluate.validate_output(VALID_OUTPUT, REQUESTS, copied)
    assert [issue for issue in issues if issue.severity == evaluate.ERROR] == []


# ---------------------------------------------------------------------------
# Layer 2: pipeline-level transforms on the official sample requests
# ---------------------------------------------------------------------------


pipeline_dataset_missing = not (
    SAMPLE_REQUESTS.exists() and (OFFICIAL_DATASET / "evidence_files.csv").exists()
)
requires_pipeline = pytest.mark.skipif(
    not PIPELINE_AVAILABLE or pipeline_dataset_missing,
    reason=PIPELINE_REASON if not PIPELINE_AVAILABLE else "official dataset not present",
)


@pytest.fixture(scope="module")
def baseline_predictions(tmp_path_factory) -> dict[str, tuple[str, list[dict]]]:
    if not PIPELINE_AVAILABLE or pipeline_dataset_missing:
        pytest.skip(PIPELINE_REASON if not PIPELINE_AVAILABLE else "official dataset not present")
    output = tmp_path_factory.mktemp("baseline") / "baseline.csv"
    try:
        _run_pipeline(OFFICIAL_DATASET, SAMPLE_REQUESTS, output)
    except evaluate.PipelineError as exc:  # pragma: no cover - depends on Lead's CLI
        pytest.fail(f"pipeline CLI failed on official samples: {exc}")
    return _predictions_by_case(output)


@requires_pipeline
def test_shuffled_request_rows_do_not_change_predictions(
    baseline_predictions, tmp_path
) -> None:
    shuffled = tmp_path / "sample_requests_shuffled.csv"
    _write_shuffled_requests(SAMPLE_REQUESTS, shuffled)
    output = tmp_path / "shuffled_predictions.csv"
    _run_pipeline(OFFICIAL_DATASET, shuffled, output)
    assert _predictions_by_case(output) == baseline_predictions


@requires_pipeline
def test_reported_status_claims_do_not_change_predictions(
    baseline_predictions, tmp_path
) -> None:
    flipped = tmp_path / "sample_requests_flipped.csv"
    _write_flipped_requests(SAMPLE_REQUESTS, flipped)
    output = tmp_path / "flipped_predictions.csv"
    _run_pipeline(OFFICIAL_DATASET, flipped, output)
    assert _predictions_by_case(output) == baseline_predictions


@requires_pipeline
def test_decoy_records_do_not_change_predictions(baseline_predictions, tmp_path) -> None:
    dataset_decoy = tmp_path / "dataset_decoy"
    shutil.copytree(OFFICIAL_DATASET, dataset_decoy)
    _add_decoy_records(dataset_decoy)
    output = tmp_path / "decoy_predictions.csv"
    _run_pipeline(dataset_decoy, SAMPLE_REQUESTS, output)
    assert _predictions_by_case(output) == baseline_predictions
