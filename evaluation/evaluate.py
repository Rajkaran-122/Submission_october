#!/usr/bin/env python3
"""Independent evaluation harness for "Ghost Data Still Exists".

This module is intentionally **independent of the answer-generation pipeline**.
It never imports ``ghost_audit.planner`` / ``ghost_audit.scenario`` and it never
derives expected answers from the pipeline. Its two jobs are:

1. ``validate_output`` -- a strict, rule-by-rule check of the output contract in
   ``problem_statement.md`` (columns, row coverage, sorting, step schema,
   allowed operations, ordering, status-specific rules, evidence citation
   rules, and -- when a dataset directory is supplied -- that every cited
   ``file_id``/``activity_id``/``audit_id`` actually exists and is not captured
   after the case's ``audit_at``).
2. ``evaluate_predictions`` -- compare a prediction CSV against a gold CSV and
   report case-level status accuracy, step-level precision/recall/F1 (multiset),
   ordered-sequence accuracy, field-level accuracy, evidence accuracy and
   per-case diagnostics (missing/extra steps, wrong fields, evidence problems,
   likely failing stage).

The gold answers used by this harness are either
(a) the official ``sample_output.csv`` regression set (development-only, not a
hidden-set accuracy claim), or
(b) hand-written ``expected.csv`` files in ``evaluation/cases/``.

CLI usage::

    python evaluation/evaluate.py validate --output output.csv \
        --requests docs/refs/.../dataset/deletion_requests.csv \
        --dataset docs/refs/.../dataset

    python evaluation/evaluate.py samples            # runs the pipeline on the
                                                     # 32 public samples first
    python evaluation/evaluate.py regress --pred output.csv \
        --gold path/to/gold.csv --dataset DIR

    python evaluation/evaluate.py cases --run-pipeline

Exit codes: 0 = clean, 1 = contract errors or metric regressions,
2 = invocation/pipeline error.
"""

from __future__ import annotations

import argparse
import csv
import difflib
import json
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------

EVALUATION_DIR = Path(__file__).resolve().parent
REPO_ROOT = EVALUATION_DIR.parent
DEFAULT_DATASET_DIR = (
    REPO_ROOT / "docs" / "refs" / "hackerrank-orchestrate-october26-main" / "dataset"
)
DEFAULT_CASES_DIR = EVALUATION_DIR / "cases"
TMP_DIR_ROOT = EVALUATION_DIR / ".tmp"

OUTPUT_HEADER = ["case_id", "deletion_status", "steps_json"]
REQUEST_HEADER = [
    "case_id",
    "email",
    "requested_at",
    "reported_status",
    "reported_at",
    "audit_at",
    "request_text",
]
STEP_FIELDS = [
    "step_no",
    "phase",
    "system_id",
    "resource_id",
    "operation",
    "state",
    "evidence_id",
]

STATUSES = ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE")
PHASES = ("FINDING", "REMEDIATION", "VERIFICATION")
PHASE_INDEX = {phase: index for index, phase in enumerate(PHASES)}

#: The allowed (phase, operation, state) triples, in the order given by the
#: problem statement's operation table. That table order is also the required
#: within-phase ordering.
OPERATION_TABLE: tuple[tuple[str, str, str], ...] = (
    ("FINDING", "REPORT_REMAINING_RECORD", "PRESENT"),
    ("FINDING", "REPORT_EXPOSED_PII", "PRESENT"),
    ("FINDING", "REPORT_ACTIVE_DATA_FLOW", "ACTIVE"),
    ("FINDING", "REPORT_RESTORE_RISK", "ACTIVE"),
    ("REMEDIATION", "DISABLE_DATA_FLOW", "DISABLED"),
    ("REMEDIATION", "BLOCK_RESTORE", "BLOCKED"),
    ("REMEDIATION", "DELETE_RECORD", "ABSENT"),
    ("REMEDIATION", "REDACT_RECORD", "REDACTED"),
    ("REMEDIATION", "ANONYMIZE_RECORD", "ANONYMIZED"),
    ("VERIFICATION", "VERIFY_RECORD_ABSENT", "ABSENT"),
    ("VERIFICATION", "VERIFY_RECORD_PRESENT", "PRESENT"),
    ("VERIFICATION", "VERIFY_RECORD_REDACTED", "REDACTED"),
    ("VERIFICATION", "VERIFY_RECORD_ANONYMIZED", "ANONYMIZED"),
    ("VERIFICATION", "VERIFY_DATA_FLOW_DISABLED", "DISABLED"),
    ("VERIFICATION", "VERIFY_RESTORE_BLOCKED", "BLOCKED"),
)
ALLOWED_OPERATIONS: dict[str, tuple[str, str]] = {
    operation: (phase, state) for phase, operation, state in OPERATION_TABLE
}
OPERATION_ORDER: dict[str, int] = {
    operation: index for index, (_, operation, _) in enumerate(OPERATION_TABLE)
}

#: Field list used for field-level accuracy (``step_no`` included so an
#: off-by-one numbering is visible as a field mismatch).
ACCURACY_FIELDS = list(STEP_FIELDS)

EVIDENCE_SOURCES = {
    "evidence_files.csv": ("file_id", "captured_at", "evidence_file"),
    "system_activity.csv": ("activity_id", "event_time", "activity"),
    "user_audit_log.csv": ("audit_id", "event_time", "audit_record"),
}


# ---------------------------------------------------------------------------
# Issues
# ---------------------------------------------------------------------------

ERROR = "ERROR"
WARNING = "WARNING"


@dataclass(frozen=True)
class Issue:
    """One contract violation or warning."""

    severity: str
    code: str
    message: str
    case_id: str | None = None
    step_no: int | None = None

    def sort_key(self) -> tuple:
        return (
            0 if self.severity == ERROR else 1,
            self.case_id or "",
            self.step_no if self.step_no is not None else -1,
            self.code,
        )

    def __str__(self) -> str:
        where = []
        if self.case_id:
            where.append(self.case_id)
        if self.step_no is not None:
            where.append(f"step {self.step_no}")
        location = f" ({', '.join(where)})" if where else ""
        return f"[{self.severity}]{location} {self.code}: {self.message}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "case_id": self.case_id,
            "step_no": self.step_no,
        }


def errors(issues: Iterable[Issue]) -> list[Issue]:
    return [issue for issue in issues if issue.severity == ERROR]


def warnings(issues: Iterable[Issue]) -> list[Issue]:
    return [issue for issue in issues if issue.severity == WARNING]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def parse_timestamp(value: str | None) -> datetime | None:
    """Parse the ISO-8601 UTC timestamps used by the dataset (``Z`` suffix)."""

    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def as_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def read_csv_rows(path: str | Path) -> tuple[list[str], list[list[str]]]:
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        rows = [row for row in reader]
    if not rows:
        return [], []
    header = [cell.strip() for cell in rows[0]]
    body = [row for row in rows[1:] if row and any(cell.strip() for cell in row)]
    return header, body


def read_requests(path: str | Path) -> dict[str, dict[str, str]]:
    """Read ``deletion_requests.csv``-shaped input into ``case_id -> row``."""

    header, rows = read_csv_rows(path)
    if "case_id" not in header:
        raise ValueError(f"requests CSV has no case_id column: {path}")
    requests: dict[str, dict[str, str]] = {}
    for row in rows:
        padded = row + [""] * (len(header) - len(row))
        record = {name: padded[index] for index, name in enumerate(header)}
        requests[as_text(record["case_id"])] = record
    return requests


def _relative(path: Path) -> str:
    """Display a path relative to the repository root when possible."""

    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _case_id_sort_key(case_id: str) -> tuple[str, int] | tuple[str, str]:
    """Natural sort key for ``case_<n>``-style ids (numeric suffix aware)."""

    prefix, _, suffix = case_id.rpartition("_")
    if prefix and suffix.isdigit():
        return (prefix, int(suffix))
    return (case_id, "")


def _case_ids_naturally_sorted(case_ids: Sequence[str]) -> bool:
    """True when rows follow the canonical natural/numeric case-id order.

    Case ids are numbered, so the canonical order is ``case_33 < case_99 <
    case_100 < case_550`` (the official requests file uses this order and the
    pipeline writes it). Lexicographic order is reported as a warning only -- it
    is a common but non-canonical serialization; a shuffled order is an error.
    """

    return case_ids == sorted(case_ids, key=_case_id_sort_key)


# ---------------------------------------------------------------------------
# Output parsing
# ---------------------------------------------------------------------------


@dataclass
class OutputRow:
    """One parsed row of a predictions/gold CSV."""

    row_number: int  # 1-based data-row number in the file
    case_id: str
    deletion_status: str
    steps_raw: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    steps_ok: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "deletion_status": self.deletion_status,
            "steps": self.steps,
        }


def _check_step_shape(case_id: str, index: int, item: Any) -> list[Issue]:
    """Structural checks for one step object (types and exact field set)."""

    issues: list[Issue] = []
    step_no = index + 1
    if not isinstance(item, Mapping):
        return [
            Issue(
                ERROR,
                "step_not_object",
                f"step {step_no} is {type(item).__name__}, expected a JSON object",
                case_id,
                step_no,
            )
        ]
    keys = list(item.keys())
    missing = [name for name in STEP_FIELDS if name not in item]
    extra = [name for name in keys if name not in STEP_FIELDS]
    if missing:
        issues.append(
            Issue(
                ERROR,
                "step_missing_fields",
                f"missing field(s): {', '.join(missing)}",
                case_id,
                step_no,
            )
        )
    if extra:
        issues.append(
            Issue(
                ERROR,
                "step_extra_fields",
                f"unexpected field(s): {', '.join(extra)}",
                case_id,
                step_no,
            )
        )
    if missing:
        return issues
    number = item.get("step_no")
    if isinstance(number, bool) or not isinstance(number, int):
        issues.append(
            Issue(
                ERROR,
                "step_no_type",
                f"step_no must be a JSON integer, got {number!r}",
                case_id,
                step_no,
            )
        )
    for name in ("phase", "system_id", "resource_id", "operation", "state", "evidence_id"):
        value = item.get(name)
        if not isinstance(value, str):
            issues.append(
                Issue(
                    ERROR,
                    "step_field_type",
                    f"{name} must be a JSON string, got {value!r}",
                    case_id,
                    step_no,
                )
            )
    return issues


def parse_steps_json(case_id: str, raw: str) -> tuple[list[dict[str, Any]], list[Issue]]:
    """Parse and structurally validate one ``steps_json`` cell."""

    issues: list[Issue] = []
    text = (raw or "").strip()
    if not text:
        return [], [
            Issue(ERROR, "steps_json_missing", "steps_json is empty", case_id)
        ]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return [], [
            Issue(
                ERROR,
                "steps_json_invalid",
                f"steps_json is not valid JSON ({exc.msg} at char {exc.pos})",
                case_id,
            )
        ]
    if not isinstance(parsed, list):
        return [], [
            Issue(
                ERROR,
                "steps_json_not_array",
                f"steps_json must be a JSON array, got {type(parsed).__name__}",
                case_id,
            )
        ]
    steps: list[dict[str, Any]] = []
    for index, item in enumerate(parsed):
        issues.extend(_check_step_shape(case_id, index, item))
        if isinstance(item, Mapping):
            steps.append(dict(item))
    return steps, issues


def load_output(path: str | Path) -> tuple[list[OutputRow], list[Issue]]:
    """Read a predictions/gold CSV. Returns rows plus structural issues."""

    path = Path(path)
    issues: list[Issue] = []
    if not path.exists():
        return [], [
            Issue(ERROR, "output_missing", f"output CSV does not exist: {path}")
        ]
    header, body = read_csv_rows(path)
    if not header:
        return [], [Issue(ERROR, "output_empty", f"output CSV is empty: {path}")]
    if header != OUTPUT_HEADER:
        issues.append(
            Issue(
                ERROR,
                "output_header",
                f"header must be exactly {','.join(OUTPUT_HEADER)}; got {','.join(header)}",
            )
        )
        return [], issues
    rows: list[OutputRow] = []
    for index, raw in enumerate(body):
        row_number = index + 1
        if len(raw) != len(OUTPUT_HEADER):
            issues.append(
                Issue(
                    ERROR,
                    "row_columns",
                    f"data row {row_number} has {len(raw)} column(s), expected 3",
                )
            )
            # Still try to use the first three cells for diagnostics.
            raw = (raw + [""] * 3)[:3]
        case_id = as_text(raw[0])
        status = as_text(raw[1])
        steps, step_issues = parse_steps_json(case_id, raw[2])
        issues.extend(step_issues)
        rows.append(
            OutputRow(
                row_number=row_number,
                case_id=case_id,
                deletion_status=status,
                steps_raw=raw[2],
                steps=steps,
                steps_ok=not step_issues and all(
                    isinstance(step.get("step_no"), int) and not isinstance(step.get("step_no"), bool)
                    for step in steps
                ),
            )
        )
    return rows, issues


# ---------------------------------------------------------------------------
# Evidence reference index (for citation validation)
# ---------------------------------------------------------------------------


@dataclass
class EvidenceIndex:
    """Known participant-visible evidence identifiers and their timestamps."""

    file_captured_at: dict[str, str] = field(default_factory=dict)
    activity_event_at: dict[str, str] = field(default_factory=dict)
    audit_event_at: dict[str, str] = field(default_factory=dict)
    load_issues: list[Issue] = field(default_factory=list)

    def resolve(self, evidence_id: str) -> tuple[str, str | None] | None:
        """Return ``(kind, timestamp)`` for a known id, else ``None``."""

        if evidence_id in self.file_captured_at:
            return ("evidence_file", self.file_captured_at[evidence_id])
        if evidence_id in self.activity_event_at:
            return ("activity", self.activity_event_at[evidence_id])
        if evidence_id in self.audit_event_at:
            return ("audit_record", self.audit_event_at[evidence_id])
        return None


def load_evidence_index(dataset_dir: str | Path | None) -> EvidenceIndex | None:
    """Load every citable evidence id from the dataset directory."""

    if dataset_dir is None:
        return None
    root = Path(dataset_dir)
    index = EvidenceIndex()
    for filename, (id_column, time_column, kind) in EVIDENCE_SOURCES.items():
        path = root / filename
        if not path.exists():
            index.load_issues.append(
                Issue(
                    WARNING,
                    "evidence_manifest_missing",
                    f"cannot verify {kind} citations: {filename} not found under {root}",
                )
            )
            continue
        header, rows = read_csv_rows(path)
        if id_column not in header or time_column not in header:
            index.load_issues.append(
                Issue(
                    WARNING,
                    "evidence_manifest_schema",
                    f"{filename} lacks {id_column!r}/{time_column!r} columns",
                )
            )
            continue
        id_pos = header.index(id_column)
        time_pos = header.index(time_column)
        for row in rows:
            if id_pos >= len(row):
                continue
            identifier = as_text(row[id_pos])
            if not identifier:
                continue
            timestamp = as_text(row[time_pos]) if time_pos < len(row) else ""
            if kind == "evidence_file":
                index.file_captured_at[identifier] = timestamp
            elif kind == "activity":
                index.activity_event_at[identifier] = timestamp
            else:
                index.audit_event_at[identifier] = timestamp
    return index


# ---------------------------------------------------------------------------
# Contract validation
# ---------------------------------------------------------------------------


def validate_output(
    output_csv: str | Path,
    requests_csv: str | Path | None = None,
    dataset_dir: str | Path | None = None,
) -> list[Issue]:
    """Validate an ``output.csv`` against the full output contract.

    Returns a list of :class:`Issue`. ``severity == ERROR`` means the output is
    not contract-compliant. ``requests_csv`` is required for row-coverage and
    ``audit_at`` checks; ``dataset_dir`` is required for evidence-reference
    checks. The function never raises on malformed input -- it reports issues.

    Row order: the required canonical order is natural/numeric by ``case_id``
    (``case_33 < case_99 < case_100``). A lexicographically sorted file is
    accepted with the ``rows_lexicographic_order`` warning; any other order is
    reported as ``rows_not_sorted``.
    """

    issues: list[Issue] = []
    rows, load_issues = load_output(output_csv)
    issues.extend(load_issues)
    if not rows:
        return sorted(issues, key=Issue.sort_key)

    requests: dict[str, dict[str, str]] | None = None
    if requests_csv is not None:
        try:
            requests = read_requests(requests_csv)
        except (OSError, ValueError) as exc:
            issues.append(
                Issue(ERROR, "requests_unreadable", f"cannot read requests CSV: {exc}")
            )

    evidence_index = load_evidence_index(dataset_dir)
    if evidence_index is not None:
        issues.extend(evidence_index.load_issues)

    # -- row set, duplicates, ordering ------------------------------------
    case_ids = [row.case_id for row in rows]
    duplicates = sorted({cid for cid in case_ids if case_ids.count(cid) > 1})
    for case_id in duplicates:
        issues.append(
            Issue(ERROR, "duplicate_case_row", "case_id appears more than once", case_id)
        )
    if not _case_ids_naturally_sorted(case_ids):
        if case_ids == sorted(case_ids):
            # Lexicographic ordering is *not* the canonical order (it would put
            # case_100 before case_33); keep it as a warning so a numerically
            # ordered output is the only contract-clean form.
            issues.append(
                Issue(
                    WARNING,
                    "rows_lexicographic_order",
                    "rows are lexicographically sorted; the canonical order is natural "
                    "numeric by case_id (e.g. case_33 < case_99 < case_100)",
                )
            )
        else:
            first_bad = next(
                (
                    index
                    for index in range(1, len(case_ids))
                    if _case_id_sort_key(case_ids[index - 1])
                    > _case_id_sort_key(case_ids[index])
                ),
                None,
            )
            detail = ""
            if first_bad is not None:
                detail = (
                    f" (first inversion: {case_ids[first_bad - 1]!r} "
                    f"before {case_ids[first_bad]!r})"
                )
            issues.append(
                Issue(
                    ERROR,
                    "rows_not_sorted",
                    f"data rows must be sorted by case_id{detail}",
                )
            )
    if requests is not None:
        expected_ids = sorted(requests, key=_case_id_sort_key)
        row_ids = set(case_ids)
        missing = sorted(set(expected_ids) - row_ids, key=_case_id_sort_key)
        extra = sorted(row_ids - set(expected_ids), key=_case_id_sort_key)
        for case_id in missing:
            issues.append(
                Issue(ERROR, "missing_case_row", "no output row for this request", case_id)
            )
        for case_id in extra:
            issues.append(
                Issue(ERROR, "extra_case_row", "output row has no matching request", case_id)
            )

    # -- per-row semantic checks ------------------------------------------
    for row in rows:
        request = requests.get(row.case_id) if requests is not None else None
        issues.extend(_validate_row(row, request, evidence_index))

    return sorted(issues, key=Issue.sort_key)


def _validate_row(
    row: OutputRow,
    request: Mapping[str, str] | None,
    evidence_index: EvidenceIndex | None,
) -> list[Issue]:
    issues: list[Issue] = []
    case_id = row.case_id
    audit_dt = parse_timestamp(request.get("audit_at")) if request else None

    if row.deletion_status not in STATUSES:
        issues.append(
            Issue(
                ERROR,
                "status_value",
                f"deletion_status must be one of {', '.join(STATUSES)}; got {row.deletion_status!r}",
                case_id,
            )
        )

    steps = row.steps
    if not row.steps_ok and not steps:
        # Structural problems were already reported by load_output.
        return issues

    # -- status-specific rules ---------------------------------------------
    if row.deletion_status == "NOT_APPLICABLE":
        if steps:
            issues.append(
                Issue(
                    ERROR,
                    "na_steps_not_empty",
                    "NOT_APPLICABLE must have an empty steps_json array ([])",
                    case_id,
                )
            )
        return issues

    if row.deletion_status == "COMPLETE":
        for index, step in enumerate(steps):
            if as_text(step.get("phase")) != "VERIFICATION":
                issues.append(
                    Issue(
                        ERROR,
                        "complete_non_verification",
                        "COMPLETE cases may contain only VERIFICATION steps",
                        case_id,
                        index + 1,
                    )
                )
        if not steps:
            issues.append(
                Issue(
                    ERROR,
                    "complete_no_verification",
                    "COMPLETE cases must include evidence-backed verification steps",
                    case_id,
                )
            )
        for index, step in enumerate(steps):
            if as_text(step.get("phase")) == "VERIFICATION" and not as_text(step.get("evidence_id")):
                issues.append(
                    Issue(
                        ERROR,
                        "complete_evidence_missing",
                        "VERIFICATION step for a COMPLETE case must cite evidence_id",
                        case_id,
                        index + 1,
                    )
                )

    if row.deletion_status == "INCOMPLETE":
        findings = [s for s in steps if as_text(s.get("phase")) == "FINDING"]
        if not findings:
            issues.append(
                Issue(
                    ERROR,
                    "incomplete_no_finding",
                    "INCOMPLETE cases must include at least one FINDING step",
                    case_id,
                )
            )
        for index, step in enumerate(steps):
            phase = as_text(step.get("phase"))
            evidence_id = as_text(step.get("evidence_id"))
            if phase == "FINDING" and not evidence_id:
                issues.append(
                    Issue(
                        ERROR,
                        "finding_evidence_missing",
                        "FINDING step must cite evidence_id",
                        case_id,
                        index + 1,
                    )
                )
            if phase in ("REMEDIATION", "VERIFICATION") and evidence_id:
                issues.append(
                    Issue(
                        ERROR,
                        "future_step_evidence",
                        f"{phase} step for an INCOMPLETE case is future work and must use an empty evidence_id",
                        case_id,
                        index + 1,
                    )
                )

    # -- step schema / enums / numbering / ordering -----------------------
    previous_phase_index = -1
    previous_key: tuple | None = None
    seen_steps: set[tuple] = set()
    for index, step in enumerate(steps):
        step_no = index + 1
        reported_no = step.get("step_no")
        if reported_no != step_no:
            issues.append(
                Issue(
                    ERROR,
                    "step_no_sequence",
                    f"step_no must be {step_no} (consecutive from 1); got {reported_no!r}",
                    case_id,
                    step_no,
                )
            )
        phase = as_text(step.get("phase"))
        operation = as_text(step.get("operation"))
        state = as_text(step.get("state"))
        system_id = as_text(step.get("system_id"))
        resource_id = as_text(step.get("resource_id"))
        evidence_id = as_text(step.get("evidence_id"))

        if phase not in PHASES:
            issues.append(
                Issue(
                    ERROR,
                    "phase_value",
                    f"phase must be one of {', '.join(PHASES)}; got {phase!r}",
                    case_id,
                    step_no,
                )
            )
            continue
        phase_index = PHASE_INDEX[phase]
        if phase_index < previous_phase_index:
            issues.append(
                Issue(
                    ERROR,
                    "phase_order",
                    "phases must be ordered FINDING -> REMEDIATION -> VERIFICATION",
                    case_id,
                    step_no,
                )
            )
        previous_phase_index = max(previous_phase_index, phase_index)

        expected = ALLOWED_OPERATIONS.get(operation)
        if expected is None:
            issues.append(
                Issue(
                    ERROR,
                    "operation_value",
                    f"operation {operation!r} is not in the allowed operation table",
                    case_id,
                    step_no,
                )
            )
            continue
        expected_phase, expected_state = expected
        if phase != expected_phase:
            issues.append(
                Issue(
                    ERROR,
                    "operation_phase_mismatch",
                    f"operation {operation} belongs to phase {expected_phase}, got {phase}",
                    case_id,
                    step_no,
                )
            )
        if state != expected_state:
            issues.append(
                Issue(
                    ERROR,
                    "operation_state_mismatch",
                    f"operation {operation} requires state {expected_state}, got {state!r}",
                    case_id,
                    step_no,
                )
            )
        if not system_id:
            issues.append(
                Issue(ERROR, "system_id_empty", "system_id must not be empty", case_id, step_no)
            )
        if not resource_id:
            issues.append(
                Issue(ERROR, "resource_id_empty", "resource_id must not be empty", case_id, step_no)
            )

        key = (phase_index, OPERATION_ORDER[operation], system_id, resource_id)
        if previous_key is not None and key < previous_key:
            issues.append(
                Issue(
                    ERROR,
                    "step_ordering",
                    "within a phase, steps must be ordered by operation-table order, then "
                    f"system_id, then resource_id ascending (got {operation} {system_id}/{resource_id})",
                    case_id,
                    step_no,
                )
            )
        previous_key = key

        identity = (phase, operation, state, system_id, resource_id, evidence_id)
        if identity in seen_steps:
            issues.append(
                Issue(
                    WARNING,
                    "duplicate_step",
                    "step duplicates an earlier step in the same case",
                    case_id,
                    step_no,
                )
            )
        seen_steps.add(identity)

        if evidence_id:
            issues.extend(
                _validate_evidence_reference(
                    case_id, step_no, phase, evidence_id, audit_dt, evidence_index
                )
            )
        elif phase in ("FINDING", "VERIFICATION") and row.deletion_status == "COMPLETE":
            # COMPLETE findings are already rejected; missing COMPLETE
            # verification evidence is reported above.
            pass

    return issues


def _validate_evidence_reference(
    case_id: str,
    step_no: int,
    phase: str,
    evidence_id: str,
    audit_dt: datetime | None,
    evidence_index: EvidenceIndex | None,
) -> list[Issue]:
    if evidence_index is None:
        return []
    resolved = evidence_index.resolve(evidence_id)
    if resolved is None:
        return [
            Issue(
                ERROR,
                "unknown_evidence_id",
                f"evidence_id {evidence_id!r} is not a known file_id, activity_id or audit_id",
                case_id,
                step_no,
            )
        ]
    kind, timestamp = resolved
    cited_dt = parse_timestamp(timestamp)
    if cited_dt is None or audit_dt is None:
        return []
    if cited_dt > audit_dt:
        # Rules: never cite evidence captured after the case's audit_at. This is
        # an ERROR for evidence files (explicitly required by the task); for
        # activity/audit ids it is reported as an ERROR too because the same
        # "state at audit_at" rule applies.
        return [
            Issue(
                ERROR,
                "evidence_after_audit_at",
                f"{kind} {evidence_id!r} is dated {timestamp} which is after audit_at "
                f"{audit_dt.strftime('%Y-%m-%dT%H:%M:%SZ')}",
                case_id,
                step_no,
            )
        ]
    return []


# ---------------------------------------------------------------------------
# Step normalization / comparison
# ---------------------------------------------------------------------------


def step_treatment_key(step: Mapping[str, Any]) -> tuple[str, str, str, str, str]:
    """Key used for step-level matching (order-independent).

    Evidence and step number are intentionally excluded: they are scored by the
    evidence-accuracy and sequence metrics instead.
    """

    return (
        as_text(step.get("phase")),
        as_text(step.get("operation")),
        as_text(step.get("state")),
        as_text(step.get("system_id")),
        as_text(step.get("resource_id")),
    )


def step_ordered_key(step: Mapping[str, Any]) -> tuple:
    """Key used for ordered-sequence comparisons (includes step_no/evidence)."""

    return (
        step.get("step_no"),
        as_text(step.get("phase")),
        as_text(step.get("operation")),
        as_text(step.get("state")),
        as_text(step.get("system_id")),
        as_text(step.get("resource_id")),
        as_text(step.get("evidence_id")),
    )


def format_step(step: Mapping[str, Any]) -> str:
    return (
        f"{as_text(step.get('phase'))} {as_text(step.get('operation'))} "
        f"{as_text(step.get('system_id'))}/{as_text(step.get('resource_id'))} "
        f"[{as_text(step.get('state'))}] ev={as_text(step.get('evidence_id')) or '-'}"
    )


def _multiset_difference(
    gold_steps: Sequence[Mapping[str, Any]], pred_steps: Sequence[Mapping[str, Any]]
) -> tuple[int, list[dict[str, Any]], list[dict[str, Any]]]:
    """Multiset match on treatment keys. Returns (matched, missing, extra)."""

    gold_counter = Counter(step_treatment_key(step) for step in gold_steps)
    pred_counter = Counter(step_treatment_key(step) for step in pred_steps)
    matched = sum((gold_counter & pred_counter).values())
    remaining = pred_counter.copy()
    missing: list[dict[str, Any]] = []
    for step in gold_steps:
        key = step_treatment_key(step)
        if remaining[key] > 0:
            remaining[key] -= 1
        else:
            missing.append(dict(step))
    remaining_gold = gold_counter.copy()
    extra: list[dict[str, Any]] = []
    for step in pred_steps:
        key = step_treatment_key(step)
        if remaining_gold[key] > 0:
            remaining_gold[key] -= 1
        else:
            extra.append(dict(step))
    return matched, missing, extra


# ---------------------------------------------------------------------------
# Per-case diagnostics
# ---------------------------------------------------------------------------


@dataclass
class CaseDiff:
    """Comparison of one case's prediction against its gold answer."""

    case_id: str
    gold_status: str = ""
    pred_status: str = ""
    gold_present: bool = True
    pred_present: bool = True
    gold_steps: list[dict[str, Any]] = field(default_factory=list)
    pred_steps: list[dict[str, Any]] = field(default_factory=list)
    matched: int = 0
    missing: list[dict[str, Any]] = field(default_factory=list)
    extra: list[dict[str, Any]] = field(default_factory=list)
    ordered_matched: int = 0
    evidence_matched: int = 0
    field_mismatches: list[dict[str, Any]] = field(default_factory=list)
    sequence_ok: bool = False
    ordered_treatment_ok: bool = False
    likely_stage: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def status_ok(self) -> bool:
        return self.gold_present and self.pred_present and self.gold_status == self.pred_status

    @property
    def steps_ok(self) -> bool:
        return (
            self.ordered_treatment_ok
            and not self.missing
            and not self.extra
            and self.sequence_ok
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "gold_status": self.gold_status,
            "pred_status": self.pred_status,
            "status_ok": self.status_ok,
            "gold_steps": self.gold_steps,
            "pred_steps": self.pred_steps,
            "matched_steps": self.matched,
            "missing_steps": self.missing,
            "extra_steps": self.extra,
            "ordered_matched_steps": self.ordered_matched,
            "evidence_matched_steps": self.evidence_matched,
            "field_mismatches": self.field_mismatches,
            "sequence_ok": self.sequence_ok,
            "ordered_treatment_ok": self.ordered_treatment_ok,
            "likely_stage": self.likely_stage,
            "notes": self.notes,
        }


def _compare_case(
    case_id: str,
    gold_row: OutputRow | None,
    pred_row: OutputRow | None,
) -> CaseDiff:
    diff = CaseDiff(
        case_id=case_id,
        gold_status=gold_row.deletion_status if gold_row else "",
        pred_status=pred_row.deletion_status if pred_row else "",
        gold_present=gold_row is not None,
        pred_present=pred_row is not None,
        gold_steps=list(gold_row.steps) if gold_row else [],
        pred_steps=list(pred_row.steps) if pred_row else [],
    )
    if gold_row is None:
        diff.notes.append("gold answer missing this case")
    if pred_row is None:
        diff.notes.append("prediction missing this case")

    matched, missing, extra = _multiset_difference(diff.gold_steps, diff.pred_steps)
    diff.matched = matched
    diff.missing = missing
    diff.extra = extra

    gold_keys = [step_treatment_key(step) for step in diff.gold_steps]
    pred_keys = [step_treatment_key(step) for step in diff.pred_steps]
    diff.ordered_treatment_ok = gold_keys == pred_keys
    diff.sequence_ok = [step_ordered_key(step) for step in diff.gold_steps] == [
        step_ordered_key(step) for step in diff.pred_steps
    ]

    matcher = difflib.SequenceMatcher(None, gold_keys, pred_keys, autojunk=False)
    pairs: list[tuple[int, int]] = []
    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            pairs.append((block.a + offset, block.b + offset))
    diff.ordered_matched = len(pairs)
    mismatches: list[dict[str, Any]] = []
    for gold_index, pred_index in pairs:
        gold_step = diff.gold_steps[gold_index]
        pred_step = diff.pred_steps[pred_index]
        if as_text(gold_step.get("evidence_id")) == as_text(pred_step.get("evidence_id")):
            diff.evidence_matched += 1
        else:
            mismatches.append(
                {
                    "step_no": gold_index + 1,
                    "field": "evidence_id",
                    "expected": as_text(gold_step.get("evidence_id")),
                    "got": as_text(pred_step.get("evidence_id")),
                }
            )
    diff.field_mismatches = mismatches

    if diff.ordered_treatment_ok and not diff.sequence_ok:
        diff.notes.append(
            "step treatments are in the expected order but the steps_json sequence differs "
            "(step_no or evidence_id mismatch)"
        )
    if not diff.ordered_treatment_ok and not diff.missing and not diff.extra:
        diff.notes.append(
            "same step multiset but a different order (ordering error)"
        )
    if diff.gold_present and diff.pred_present:
        if len(diff.gold_steps) != len(diff.pred_steps):
            diff.notes.append(
                f"step count differs: expected {len(diff.gold_steps)}, got {len(diff.pred_steps)}"
            )

    diff.likely_stage = _infer_likely_stage(diff)
    return diff


def _infer_likely_stage(diff: CaseDiff) -> str:
    """Heuristic mapping from symptoms to the pipeline stage likely at fault."""

    if not diff.pred_present:
        return "output generation (case missing from predictions)"
    if not diff.gold_present:
        return "evaluation fixture (no gold answer)"
    if not diff.status_ok:
        pair = {diff.gold_status, diff.pred_status}
        if "NOT_APPLICABLE" in pair:
            return "intent classification (request_text -> deletion authorization)"
        if diff.gold_status == "INCOMPLETE" and diff.pred_status == "COMPLETE":
            return (
                "state reconstruction / identity resolution / evidence parsing "
                "(missed a remaining copy or recreation risk; a reported claim may have been trusted)"
            )
        if diff.gold_status == "COMPLETE" and diff.pred_status == "INCOMPLETE":
            return "policy application or evidence parsing (false-positive finding)"
        return "status decision"
    missing_phases = {as_text(step.get("phase")) for step in diff.missing}
    if "FINDING" in missing_phases:
        return "FINDING detection (evidence parsing, identity resolution, policy application)"
    if "REMEDIATION" in missing_phases:
        return "remediation planning (finding -> required action mapping)"
    if "VERIFICATION" in missing_phases:
        return "verification planning (final-state checks)"
    if diff.extra and not diff.missing:
        return "over-reporting: extra step(s) not supported by policy"
    if diff.field_mismatches and not diff.sequence_ok:
        return "evidence selection (cited evidence_id does not match the proving evidence)"
    if not diff.ordered_treatment_ok:
        return "step ordering (phase -> operation table -> system_id -> resource_id)"
    if not diff.sequence_ok:
        return "step serialization (step_no / evidence_id sequence)"
    return "match"


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _safe_div(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


@dataclass
class Report:
    """Evaluation report for one prediction file against one gold file."""

    pred_path: str
    gold_path: str
    cases: list[CaseDiff] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    gold_issues: list[Issue] = field(default_factory=list)
    requests_path: str | None = None
    dataset_dir: str | None = None
    label: str = (
        "development regression metrics (public sample or team-authored cases; "
        "not hidden-set accuracy)"
    )

    # -- aggregate metrics -------------------------------------------------
    @property
    def metrics(self) -> dict[str, Any]:
        total = len(self.cases)
        status_correct = sum(1 for case in self.cases if case.status_ok)
        gold_total = sum(len(case.gold_steps) for case in self.cases)
        pred_total = sum(len(case.pred_steps) for case in self.cases)
        matched_total = sum(case.matched for case in self.cases)
        if pred_total == 0 and gold_total == 0:
            # A run with no steps anywhere (all NOT_APPLICABLE) is trivially perfect.
            precision = recall = f1 = 1.0
        else:
            precision = _safe_div(matched_total, pred_total)
            recall = _safe_div(matched_total, gold_total)
            f1 = (
                2 * precision * recall / (precision + recall)
                if (precision + recall) > 0
                else 0.0
            )
        sequence_accuracy = _safe_div(
            sum(1 for case in self.cases if case.sequence_ok), total
        )
        ordered_treatment_accuracy = _safe_div(
            sum(1 for case in self.cases if case.ordered_treatment_ok), total
        )

        field_hits: Counter = Counter()
        field_denominator = 0
        evidence_positions = 0
        evidence_hits = 0
        for case in self.cases:
            length = max(len(case.gold_steps), len(case.pred_steps))
            field_denominator += length
            for index in range(length):
                gold_step = case.gold_steps[index] if index < len(case.gold_steps) else None
                pred_step = case.pred_steps[index] if index < len(case.pred_steps) else None
                both = gold_step is not None and pred_step is not None
                for name in ACCURACY_FIELDS:
                    if both and as_text(gold_step.get(name)) == as_text(pred_step.get(name)):
                        field_hits[name] += 1
                if both:
                    treatment_equal = step_treatment_key(gold_step) == step_treatment_key(
                        pred_step
                    )
                    if treatment_equal:
                        evidence_positions += 1
                        if as_text(gold_step.get("evidence_id")) == as_text(
                            pred_step.get("evidence_id")
                        ):
                            evidence_hits += 1
        field_accuracy = {
            name: _safe_div(field_hits[name], field_denominator) for name in ACCURACY_FIELDS
        }
        overall_field_accuracy = _safe_div(
            sum(field_hits.values()), field_denominator * len(ACCURACY_FIELDS)
        )

        confusion: Counter = Counter()
        for case in self.cases:
            if case.gold_present and case.pred_present:
                confusion[f"{case.gold_status}->{case.pred_status}"] += 1

        error_issues = errors(self.issues)
        return {
            "cases": total,
            "gold_rows": sum(1 for case in self.cases if case.gold_present),
            "pred_rows": sum(1 for case in self.cases if case.pred_present),
            "status_accuracy": _safe_div(status_correct, total),
            "status_correct": status_correct,
            "status_confusion": dict(sorted(confusion.items())),
            "gold_steps": gold_total,
            "pred_steps": pred_total,
            "matched_steps": matched_total,
            "step_precision": precision,
            "step_recall": recall,
            "step_f1": f1,
            "sequence_accuracy": sequence_accuracy,
            "ordered_treatment_accuracy": ordered_treatment_accuracy,
            "field_accuracy": field_accuracy,
            "field_accuracy_overall": overall_field_accuracy,
            "evidence_accuracy": _safe_div(evidence_hits, evidence_positions),
            "evidence_positions": evidence_positions,
            "contract_errors": len(error_issues),
            "contract_warnings": len(warnings(self.issues)),
        }

    # -- helpers -----------------------------------------------------------
    def failures(self) -> list[CaseDiff]:
        return [case for case in self.cases if not case.status_ok or not case.steps_ok]

    def case(self, case_id: str) -> CaseDiff | None:
        for case in self.cases:
            if case.case_id == case_id:
                return case
        return None

    def is_clean(self) -> bool:
        """True when the prediction is contract-clean and exactly matches gold."""

        m = self.metrics
        return (
            m["cases"] > 0
            and m["status_accuracy"] == 1.0
            and m["step_f1"] == 1.0
            and m["sequence_accuracy"] == 1.0
            and m["contract_errors"] == 0
        )

    # -- rendering ---------------------------------------------------------
    def summary(self, max_failures: int = 20) -> str:
        m = self.metrics
        lines: list[str] = []
        lines.append("=" * 78)
        lines.append("Ghost Data Still Exists -- evaluation report")
        lines.append("=" * 78)
        lines.append(f"prediction : {self.pred_path}")
        lines.append(f"gold       : {self.gold_path}")
        if self.requests_path:
            lines.append(f"requests   : {self.requests_path}")
        if self.dataset_dir:
            lines.append(f"dataset    : {self.dataset_dir}")
        lines.append(f"label      : {self.label}")
        lines.append("")
        lines.append(f"Cases                : {m['cases']}")
        lines.append(
            f"Status accuracy      : {m['status_accuracy']:.4f} "
            f"({m['status_correct']}/{m['cases']})"
        )
        confusion = m["status_confusion"]
        if confusion and len(confusion) > 1:
            lines.append(
                "Status confusion     : "
                + ", ".join(f"{pair} x{count}" for pair, count in confusion.items())
            )
        lines.append(
            f"Steps (gold/pred)    : {m['gold_steps']} / {m['pred_steps']} "
            f"(matched {m['matched_steps']})"
        )
        lines.append(
            f"Step precision       : {m['step_precision']:.4f}"
        )
        lines.append(f"Step recall          : {m['step_recall']:.4f}")
        lines.append(f"Step F1              : {m['step_f1']:.4f}")
        lines.append(
            f"Sequence accuracy    : {m['sequence_accuracy']:.4f} (exact ordered steps_json)"
        )
        lines.append(
            f"Ordered treatments   : {m['ordered_treatment_accuracy']:.4f} "
            "(ordered phase/op/state/system/resource)"
        )
        field_parts = ", ".join(
            f"{name}={value:.3f}" for name, value in m["field_accuracy"].items()
        )
        lines.append(
            f"Field accuracy       : {m['field_accuracy_overall']:.4f} overall ({field_parts})"
        )
        lines.append(
            f"Evidence accuracy    : {m['evidence_accuracy']:.4f} "
            f"({m['evidence_positions']} matched treatment positions)"
        )
        lines.append(
            f"Contract issues      : {m['contract_errors']} error(s), "
            f"{m['contract_warnings']} warning(s)"
        )

        if self.issues:
            lines.append("")
            lines.append("Contract issues:")
            for issue in self.issues[: max_failures * 3]:
                lines.append(f"  {issue}")
            remaining = len(self.issues) - max_failures * 3
            if remaining > 0:
                lines.append(f"  ... {remaining} more issue(s)")

        failures = self.failures()
        lines.append("")
        if failures:
            lines.append(f"Per-case failures ({len(failures)}):")
            for case in failures[:max_failures]:
                lines.extend(_format_case_failure(case, indent="  "))
            if len(failures) > max_failures:
                lines.append(f"  ... {len(failures) - max_failures} more failing case(s)")
        else:
            lines.append("Per-case failures: none")
        lines.append("=" * 78)
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "prediction": self.pred_path,
            "gold": self.gold_path,
            "requests": self.requests_path,
            "dataset": self.dataset_dir,
            "label": self.label,
            "metrics": self.metrics,
            "issues": [issue.to_dict() for issue in self.issues],
            "gold_issues": [issue.to_dict() for issue in self.gold_issues],
            "cases": [case.to_dict() for case in self.cases],
        }

    def write_json(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return target

    def write_markdown(self, path: str | Path, generated_at: str | None = None) -> Path:
        """Write a machine-generated report section that the team can extend."""

        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        m = self.metrics
        stamp = generated_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines: list[str] = []
        lines.append("# Evaluation report (generated metrics section)")
        lines.append("")
        lines.append(f"_Generated by `evaluation/evaluate.py` at {stamp}._")
        lines.append("")
        lines.append(f"- Prediction: `{self.pred_path}`")
        lines.append(f"- Gold: `{self.gold_path}`")
        lines.append(f"- Label: {self.label}")
        lines.append("")
        lines.append("## Case-level deletion-status accuracy")
        lines.append("")
        lines.append(f"- {m['status_accuracy']:.4f} ({m['status_correct']}/{m['cases']} cases)")
        if m["status_confusion"]:
            lines.append("")
            lines.append("| expected -> predicted | cases |")
            lines.append("| --- | --- |")
            for pair, count in m["status_confusion"].items():
                lines.append(f"| {pair} | {count} |")
        lines.append("")
        lines.append("## Step-level, field-level and sequence accuracy")
        lines.append("")
        lines.append("| metric | value |")
        lines.append("| --- | --- |")
        lines.append(f"| step precision (multiset) | {m['step_precision']:.4f} |")
        lines.append(f"| step recall (multiset) | {m['step_recall']:.4f} |")
        lines.append(f"| step F1 | {m['step_f1']:.4f} |")
        lines.append(f"| exact sequence accuracy | {m['sequence_accuracy']:.4f} |")
        lines.append(f"| ordered treatment accuracy | {m['ordered_treatment_accuracy']:.4f} |")
        lines.append(f"| field accuracy overall | {m['field_accuracy_overall']:.4f} |")
        lines.append(f"| evidence accuracy | {m['evidence_accuracy']:.4f} |")
        lines.append(f"| contract errors | {m['contract_errors']} |")
        lines.append("")
        lines.append("| field | accuracy |")
        lines.append("| --- | --- |")
        for name, value in m["field_accuracy"].items():
            lines.append(f"| {name} | {value:.4f} |")
        lines.append("")
        lines.append("## Failures found")
        lines.append("")
        failures = self.failures()
        if failures:
            for case in failures[:50]:
                lines.extend(_format_case_failure(case, indent=""))
            if len(failures) > 50:
                lines.append(f"... {len(failures) - 50} more failing case(s)")
        else:
            lines.append("None.")
        lines.append("")
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return target


def _format_case_failure(case: CaseDiff, indent: str = "") -> list[str]:
    lines: list[str] = []
    head = f"{indent}{case.case_id}: "
    problems: list[str] = []
    if not case.status_ok:
        problems.append(f"status expected {case.gold_status}, got {case.pred_status}")
    if case.missing:
        problems.append(f"missing {len(case.missing)}: {', '.join(format_step(s) for s in case.missing)}")
    if case.extra:
        problems.append(f"extra {len(case.extra)}: {', '.join(format_step(s) for s in case.extra)}")
    if not case.sequence_ok:
        problems.append("sequence mismatch")
    for mismatch in case.field_mismatches:
        problems.append(
            f"step {mismatch['step_no']} {mismatch['field']}: "
            f"expected {mismatch['expected']!r}, got {mismatch['got']!r}"
        )
    for note in case.notes:
        problems.append(note)
    if case.likely_stage and case.likely_stage != "match":
        problems.append(f"likely stage: {case.likely_stage}")
    lines.append(head + " | ".join(problems) if problems else head + "ok")
    return lines


def _combine_reports(reports: Sequence[Report], label: str) -> Report:
    combined = Report(
        pred_path="; ".join(report.pred_path for report in reports),
        gold_path="; ".join(report.gold_path for report in reports),
        label=label,
    )
    for report in reports:
        combined.cases.extend(report.cases)
        combined.issues.extend(report.issues)
        combined.gold_issues.extend(report.gold_issues)
    combined.cases.sort(key=lambda case: case.case_id)
    combined.issues.sort(key=Issue.sort_key)
    return combined


# ---------------------------------------------------------------------------
# Prediction vs gold evaluation
# ---------------------------------------------------------------------------


def evaluate_predictions(
    pred_csv: str | Path,
    gold_csv: str | Path,
    requests_csv: str | Path | None = None,
    dataset_dir: str | Path | None = None,
) -> Report:
    """Compare a prediction CSV against a gold CSV and build a :class:`Report`."""

    pred_path = Path(pred_csv)
    gold_path = Path(gold_csv)
    pred_rows, pred_load_issues = load_output(pred_path)
    gold_rows, gold_load_issues = load_output(gold_path)

    report = Report(
        pred_path=_relative(pred_path),
        gold_path=_relative(gold_path),
        issues=list(pred_load_issues),
        gold_issues=list(gold_load_issues),
        requests_path=_relative(Path(requests_csv)) if requests_csv else None,
        dataset_dir=_relative(Path(dataset_dir)) if dataset_dir else None,
    )

    # Full contract validation of the prediction (includes load issues).
    report.issues = validate_output(pred_path, requests_csv, dataset_dir)

    pred_by_id = {row.case_id: row for row in pred_rows}
    gold_by_id = {row.case_id: row for row in gold_rows}
    # Ignore empty case ids (malformed rows) in case-level comparison.
    pred_by_id.pop("", None)
    gold_by_id.pop("", None)
    for case_id in sorted(set(pred_by_id) | set(gold_by_id)):
        report.cases.append(
            _compare_case(case_id, gold_by_id.get(case_id), pred_by_id.get(case_id))
        )
    return report


# ---------------------------------------------------------------------------
# Pipeline invocation (CLI contract under test)
# ---------------------------------------------------------------------------

PIPELINE_COMMAND_DOC = (
    "python -m ghost_audit run --dataset DIR --requests CSV --output CSV [--skip-images]"
)


class PipelineError(RuntimeError):
    """Raised when the pipeline CLI cannot be run or exits unsuccessfully."""


def python_executable() -> str:
    """Python interpreter used for subprocesses (the current env by default)."""

    return sys.executable


def run_pipeline(
    dataset_dir: str | Path,
    requests_csv: str | Path,
    output_csv: str | Path,
    *,
    skip_images: bool = True,
    python_exe: str | None = None,
    cwd: str | Path | None = REPO_ROOT,
    timeout: float = 1800.0,
) -> subprocess.CompletedProcess:
    """Run the pipeline CLI on one dataset/requests pair and return the process.

    Raises :class:`PipelineError` when the command cannot be executed, when it
    exits non-zero, or when it does not write the requested output file.
    """

    python = python_exe or python_executable()
    output_path = Path(output_csv)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        python,
        "-m",
        "ghost_audit",
        "run",
        "--dataset",
        str(dataset_dir),
        "--requests",
        str(requests_csv),
        "--output",
        str(output_path),
    ]
    if skip_images:
        command.append("--skip-images")
    try:
        process = subprocess.run(
            command,
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PipelineError(f"could not run pipeline ({' '.join(command)}): {exc}") from exc
    if process.returncode != 0:
        stderr = (process.stderr or "").strip().splitlines()[-10:]
        raise PipelineError(
            "pipeline exited with code "
            f"{process.returncode} for {PIPELINE_COMMAND_DOC}\n"
            + "\n".join(stderr)
        )
    if not output_path.exists():
        raise PipelineError(
            f"pipeline exited 0 but did not write output: {output_path}"
        )
    return process


def pipeline_available(
    *, python_exe: str | None = None, cwd: str | Path | None = REPO_ROOT
) -> tuple[bool, str]:
    """Probe whether the pipeline CLI exists. Returns ``(available, reason)``."""

    python = python_exe or python_executable()
    try:
        process = subprocess.run(
            [python, "-m", "ghost_audit", "--help"],
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"cannot execute {python} -m ghost_audit --help: {exc}"
    if process.returncode != 0:
        detail = (process.stderr or "").strip().splitlines()
        return False, (
            "the pipeline CLI is not implemented yet (expected "
            f"{PIPELINE_COMMAND_DOC}): " + (detail[-1] if detail else f"exit {process.returncode}")
        )
    return True, "pipeline CLI available"


def _tmp_dir() -> tempfile.TemporaryDirectory:
    TMP_DIR_ROOT.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(prefix="eval-", dir=str(TMP_DIR_ROOT))


# ---------------------------------------------------------------------------
# Official 32-case sample regression entry point
# ---------------------------------------------------------------------------


def sample_regression(
    predictions_csv: str | Path | None = None,
    *,
    dataset_dir: str | Path | None = None,
    run_pipeline_flag: bool = False,
    skip_images: bool = True,
    python_exe: str | None = None,
) -> Report:
    """Run the official sample regression.

    gold = ``dataset/sample_output.csv``; requests =
    ``dataset/sample_deletion_requests.csv``. When ``predictions_csv`` is None
    the pipeline CLI is executed on the sample requests to produce predictions.
    """

    dataset = Path(dataset_dir) if dataset_dir else DEFAULT_DATASET_DIR
    requests = dataset / "sample_deletion_requests.csv"
    gold = dataset / "sample_output.csv"
    if not requests.exists() or not gold.exists():
        raise FileNotFoundError(
            f"official sample files not found under {dataset} "
            "(expected sample_deletion_requests.csv and sample_output.csv)"
        )
    if predictions_csv is not None:
        return evaluate_predictions(
            predictions_csv, gold, requests_csv=requests, dataset_dir=dataset
        )

    # Stable, inspectable location for the predictions produced by the pipeline.
    TMP_DIR_ROOT.mkdir(parents=True, exist_ok=True)
    pred_path = TMP_DIR_ROOT / "sample_predictions.csv"
    run_pipeline(
        dataset,
        requests,
        pred_path,
        skip_images=skip_images,
        python_exe=python_exe,
    )
    report = evaluate_predictions(
        pred_path, gold, requests_csv=requests, dataset_dir=dataset
    )
    report.label = (
        "official 32-case public sample regression (development regression set; "
        "not hidden-set accuracy)"
    )
    return report


# ---------------------------------------------------------------------------
# Team-authored independent cases
# ---------------------------------------------------------------------------


def discover_case_dirs(cases_dir: str | Path | None = None) -> list[Path]:
    root = Path(cases_dir) if cases_dir else DEFAULT_CASES_DIR
    if not root.is_dir():
        return []
    return sorted(
        path
        for path in root.iterdir()
        if path.is_dir()
        and (path / "deletion_requests.csv").exists()
        and (path / "expected.csv").exists()
        and (path / "evidence_files.csv").exists()
    )


def validate_case_fixtures(
    cases_dir: str | Path | None = None,
) -> list[tuple[str, list[Issue]]]:
    """Contract-validate every hand-written ``expected.csv`` fixture.

    This is a self-check of the evaluation fixtures: each expected answer must
    itself satisfy the contract and cite evidence that exists in that case's
    mini-dataset. It never needs the pipeline.
    """

    results: list[tuple[str, list[Issue]]] = []
    for case_dir in discover_case_dirs(cases_dir):
        issues = validate_output(
            case_dir / "expected.csv",
            requests_csv=case_dir / "deletion_requests.csv",
            dataset_dir=case_dir,
        )
        if not (case_dir / "README.md").exists():
            issues.append(
                Issue(
                    WARNING,
                    "case_readme_missing",
                    "case directory must document its hand-written rationale",
                    case_id=case_dir.name,
                )
            )
        results.append((case_dir.name, issues))
    return results


def evaluate_case(
    case_dir: str | Path,
    prediction_csv: str | Path,
) -> Report:
    """Evaluate one pipeline prediction against one case's ``expected.csv``."""

    case_path = Path(case_dir)
    report = evaluate_predictions(
        prediction_csv,
        case_path / "expected.csv",
        requests_csv=case_path / "deletion_requests.csv",
        dataset_dir=case_path,
    )
    report.label = (
        f"team-authored independent case {case_path.name} "
        "(hand-written expected answer; not generated by the pipeline)"
    )
    return report


def run_cases(
    *,
    cases_dir: str | Path | None = None,
    predictions_dir: str | Path | None = None,
    run_pipeline_flag: bool = False,
    keep_predictions: bool = False,
    skip_images: bool = True,
    python_exe: str | None = None,
) -> tuple[Report, dict[str, Path]]:
    """Evaluate all independent cases and return a combined report.

    Predictions come from ``predictions_dir/<case-name>.csv`` when supplied, or
    from running the pipeline CLI per case when ``run_pipeline_flag`` is set.
    """

    case_dirs = discover_case_dirs(cases_dir)
    if not case_dirs:
        raise FileNotFoundError(f"no independent case directories found under {cases_dir or DEFAULT_CASES_DIR}")
    reports: list[Report] = []
    prediction_paths: dict[str, Path] = {}
    tmp = _tmp_dir() if run_pipeline_flag or predictions_dir is None else None
    try:
        for case_dir in case_dirs:
            name = case_dir.name
            if predictions_dir is not None:
                prediction = Path(predictions_dir) / f"{name}.csv"
                if not prediction.exists():
                    raise FileNotFoundError(f"missing prediction file: {prediction}")
            elif run_pipeline_flag:
                assert tmp is not None
                prediction = Path(tmp.name) / f"{name}.csv"
                run_pipeline(
                    case_dir,
                    case_dir / "deletion_requests.csv",
                    prediction,
                    skip_images=skip_images,
                    python_exe=python_exe,
                )
            else:
                raise ValueError(
                    "provide predictions_dir or set run_pipeline_flag=True"
                )
            prediction_paths[name] = prediction
            reports.append(evaluate_case(case_dir, prediction))
    finally:
        if tmp is not None and not keep_predictions:
            tmp.cleanup()
    combined = _combine_reports(
        reports, label="team-authored independent cases (hand-written expected answers)"
    )
    return combined, prediction_paths


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _add_report_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", dest="json_path", help="write a machine-readable JSON report")
    parser.add_argument("--markdown", dest="markdown_path", help="write a Markdown report section")
    parser.add_argument(
        "--failures",
        type=int,
        default=20,
        help="maximum number of failing cases to print (default: 20)",
    )


def _write_reports(report: Report, args: argparse.Namespace) -> None:
    print(report.summary(max_failures=args.failures))
    if args.json_path:
        path = report.write_json(args.json_path)
        print(f"[evaluate] JSON report written to {_relative(path)}")
    if args.markdown_path:
        path = report.write_markdown(args.markdown_path)
        print(f"[evaluate] Markdown report written to {_relative(path)}")


def _cmd_validate(args: argparse.Namespace) -> int:
    issues = validate_output(
        args.output, requests_csv=args.requests, dataset_dir=args.dataset
    )
    error_list = errors(issues)
    warning_list = warnings(issues)
    print(
        f"[evaluate] validate {args.output}: {len(error_list)} error(s), "
        f"{len(warning_list)} warning(s)"
    )
    for issue in issues:
        print(f"  {issue}")
    return 0 if not error_list else 1


def _cmd_regress(args: argparse.Namespace) -> int:
    try:
        report = evaluate_predictions(
            args.pred, args.gold, requests_csv=args.requests, dataset_dir=args.dataset
        )
    except FileNotFoundError as exc:
        print(f"[evaluate] error: {exc}", file=sys.stderr)
        return 2
    _write_reports(report, args)
    return 0 if report.is_clean() else 1


def _cmd_samples(args: argparse.Namespace) -> int:
    try:
        report = sample_regression(
            args.pred,
            dataset_dir=args.dataset,
            run_pipeline_flag=args.run_pipeline,
            skip_images=not args.keep_images,
        )
    except (FileNotFoundError, PipelineError) as exc:
        print(f"[evaluate] error: {exc}", file=sys.stderr)
        if isinstance(exc, PipelineError):
            print(
                "[evaluate] implement the CLI first: " + PIPELINE_COMMAND_DOC,
                file=sys.stderr,
            )
        return 2
    finally:
        pass
    _write_reports(report, args)
    return 0 if report.is_clean() else 1


def _cmd_cases(args: argparse.Namespace) -> int:
    # Self-check the hand-written fixtures first.
    fixture_issues = validate_case_fixtures(args.cases_dir)
    fixture_errors = [
        (name, issue) for name, issues in fixture_issues for issue in errors(issues)
    ]
    for name, issue in fixture_errors:
        print(f"[evaluate] fixture {name}: {issue}")
    if fixture_errors:
        print(
            f"[evaluate] {len(fixture_errors)} expected-answer fixture error(s); "
            "fix the case data before running the pipeline"
        )
        return 1
    try:
        report, _ = run_cases(
            cases_dir=args.cases_dir,
            predictions_dir=args.predictions_dir,
            run_pipeline_flag=args.run_pipeline or args.predictions_dir is None,
            keep_predictions=False,
            skip_images=not args.keep_images,
        )
    except (FileNotFoundError, PipelineError) as exc:
        print(f"[evaluate] error: {exc}", file=sys.stderr)
        if isinstance(exc, PipelineError):
            print(
                "[evaluate] implement the CLI first: " + PIPELINE_COMMAND_DOC,
                file=sys.stderr,
            )
        return 2
    _write_reports(report, args)
    return 0 if report.is_clean() else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="evaluate.py",
        description=(
            "Independent evaluation harness for Ghost Data Still Exists. "
            f"Pipeline CLI under test: {PIPELINE_COMMAND_DOC}"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser(
        "validate", help="strictly validate one output.csv against the contract"
    )
    validate_parser.add_argument("--output", required=True, help="predictions output.csv")
    validate_parser.add_argument("--requests", help="requests CSV for row coverage and audit_at")
    validate_parser.add_argument(
        "--dataset", help="dataset directory for evidence_id cross-checks"
    )
    validate_parser.set_defaults(func=_cmd_validate)

    regress_parser = subparsers.add_parser(
        "regress", help="compare predictions with any gold CSV and report metrics"
    )
    regress_parser.add_argument("--pred", required=True)
    regress_parser.add_argument("--gold", required=True)
    regress_parser.add_argument("--requests")
    regress_parser.add_argument("--dataset")
    _add_report_options(regress_parser)
    regress_parser.set_defaults(func=_cmd_regress)

    samples_parser = subparsers.add_parser(
        "samples",
        help="official 32-case public sample regression (runs pipeline when --pred is omitted)",
    )
    samples_parser.add_argument("--pred", help="precomputed predictions CSV")
    samples_parser.add_argument("--dataset", help="official dataset directory")
    samples_parser.add_argument(
        "--run-pipeline",
        action="store_true",
        help="(default when --pred is omitted) run the pipeline CLI first",
    )
    samples_parser.add_argument(
        "--keep-images", action="store_true", help="do not pass --skip-images"
    )
    _add_report_options(samples_parser)
    samples_parser.set_defaults(func=_cmd_samples)

    cases_parser = subparsers.add_parser(
        "cases", help="run the team-authored independent cases"
    )
    cases_parser.add_argument("--cases-dir", help="directory of case directories")
    cases_parser.add_argument(
        "--predictions-dir",
        help="directory with <case-name>.csv predictions (default: run the pipeline)",
    )
    cases_parser.add_argument(
        "--run-pipeline", action="store_true", help="run the pipeline CLI on every case"
    )
    cases_parser.add_argument(
        "--keep-images", action="store_true", help="do not pass --skip-images"
    )
    _add_report_options(cases_parser)
    cases_parser.set_defaults(func=_cmd_cases)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
