#!/usr/bin/env python3
"""Standalone validator for the Ghost Data Still Exists submission artifacts.

This script deliberately depends on the Python standard library only, so it can
run before/without the project virtualenv:

    .venv/bin/python scripts/validate_submission.py            # full check
    uv run python scripts/validate_submission.py               # same, via uv
    uv run python scripts/validate_submission.py --skip-missing  # dev mode
    uv run python scripts/validate_submission.py --dataset <DATASET_DIR>

What it validates
=================

``output.csv`` (root of the repository, by contract):
  * exact header ``case_id,deletion_status,steps_json``;
  * exactly one row per request (518 by default, or the row count from
    ``dataset/deletion_requests.csv`` when ``--dataset`` is given);
  * rows sorted ascending by canonical **numeric** case id
    (``case_33, case_99, case_100, ..., case_550``; lexicographic order is
    flagged as an error), no duplicates;
  * ``deletion_status`` in {COMPLETE, INCOMPLETE, NOT_APPLICABLE};
  * ``steps_json`` is a JSON array; every step has exactly the 7 required keys
    with the right JSON types and consecutive ``step_no`` starting at 1;
  * (phase, operation, state) pairs match the allowed table;
  * phase order FINDING -> REMEDIATION -> VERIFICATION and, inside a phase,
    operation order from the problem statement followed by ``system_id`` and
    ``resource_id`` ascending;
  * NOT_APPLICABLE -> empty array; COMPLETE -> verification steps only, each
    citing evidence; INCOMPLETE -> FINDING + REMEDIATION + VERIFICATION, with
    findings citing evidence and proposed remediation/future verification
    steps using an empty ``evidence_id``.

With ``--dataset DIR`` it additionally cross-checks:
  * full case coverage against ``deletion_requests.csv``;
  * every cited evidence id exists in ``evidence_files.csv``
    (``file_id``), ``system_activity.csv`` (``activity_id``) or
    ``user_audit_log.csv`` (``audit_id``);
  * cited evidence was captured at or before the case's ``audit_at``
    (late evidence must never justify a finding/verification).

``code.zip`` (``submission/code.zip`` by default):
  * is a readable zip with no absolute/``..`` entry paths and no duplicate
    entries; no nested archives (``.zip``/``.tar``/``.gz``/...);
  * contains no ``dataset/``, ``.venv``/``.git``/caches/``work/``/``__pycache__``;
  * contains no secrets: ``sk-`` API keys, literal credential assignments,
    bearer tokens or private key blocks;
  * contains the required entries: ``README.md``,
    ``evaluation/evaluation_report.md``, ``evaluation/usage_report.md`` and a
    runnable package entry point (``src/ghost_audit/__main__.py``).

``submission/`` directory:
  * contains exactly the four deliverables ``output.csv``, ``code.zip``,
    ``log.txt`` (the chat transcript) and ``INTERVIEW_GUIDE.md``;
  * ``log.txt`` is non-empty, does not leak secrets, looks like the AGENTS.md
    session log (contains ``tool=`` and ``model=`` lines), keeps the root
    ``log.txt`` as a verbatim prefix and contains the generated PART II
    conversation appendices (``scripts/build_transcript.py``);
  * the interview guide is non-empty and contains no TODO placeholders.

Exit codes
==========
  0  everything checked passed
  1  validation errors were found (warnings alone do not fail the run)
  2  one or more required artifacts are missing (unless ``--skip-missing``)

``--skip-missing`` downgrades missing artifacts to warnings so partial work can
be validated while the pipeline is still being built. It never downgrades a
malformed artifact: a file that exists but violates the contract is exit 1.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Contract constants (taken verbatim from problem_statement.md / AGENTS.md).
# ---------------------------------------------------------------------------

REPO_ROOT_DEFAULT = Path(__file__).resolve().parents[1]

EXPECTED_HEADER = ["case_id", "deletion_status", "steps_json"]
EXPECTED_ROWS = 518
STATUSES = ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE")
STEP_FIELDS = (
    "step_no",
    "phase",
    "system_id",
    "resource_id",
    "operation",
    "state",
    "evidence_id",
)

PHASE_ORDER = {"FINDING": 0, "REMEDIATION": 1, "VERIFICATION": 2}

#: (phase, operation) -> the only allowed state.
ALLOWED_STEPS = {
    ("FINDING", "REPORT_REMAINING_RECORD"): "PRESENT",
    ("FINDING", "REPORT_EXPOSED_PII"): "PRESENT",
    ("FINDING", "REPORT_ACTIVE_DATA_FLOW"): "ACTIVE",
    ("FINDING", "REPORT_RESTORE_RISK"): "ACTIVE",
    ("REMEDIATION", "DISABLE_DATA_FLOW"): "DISABLED",
    ("REMEDIATION", "BLOCK_RESTORE"): "BLOCKED",
    ("REMEDIATION", "DELETE_RECORD"): "ABSENT",
    ("REMEDIATION", "REDACT_RECORD"): "REDACTED",
    ("REMEDIATION", "ANONYMIZE_RECORD"): "ANONYMIZED",
    ("VERIFICATION", "VERIFY_RECORD_ABSENT"): "ABSENT",
    ("VERIFICATION", "VERIFY_RECORD_PRESENT"): "PRESENT",
    ("VERIFICATION", "VERIFY_RECORD_REDACTED"): "REDACTED",
    ("VERIFICATION", "VERIFY_RECORD_ANONYMIZED"): "ANONYMIZED",
    ("VERIFICATION", "VERIFY_DATA_FLOW_DISABLED"): "DISABLED",
    ("VERIFICATION", "VERIFY_RESTORE_BLOCKED"): "BLOCKED",
}

#: Within-phase ordering: operation order from the problem statement table.
OPERATION_ORDER = {
    "FINDING": {
        "REPORT_REMAINING_RECORD": 0,
        "REPORT_EXPOSED_PII": 1,
        "REPORT_ACTIVE_DATA_FLOW": 2,
        "REPORT_RESTORE_RISK": 3,
    },
    "REMEDIATION": {
        "DISABLE_DATA_FLOW": 0,
        "BLOCK_RESTORE": 1,
        "DELETE_RECORD": 2,
        "REDACT_RECORD": 3,
        "ANONYMIZE_RECORD": 4,
    },
    "VERIFICATION": {
        "VERIFY_RECORD_ABSENT": 0,
        "VERIFY_RECORD_PRESENT": 1,
        "VERIFY_RECORD_REDACTED": 2,
        "VERIFY_RECORD_ANONYMIZED": 3,
        "VERIFY_DATA_FLOW_DISABLED": 4,
        "VERIFY_RESTORE_BLOCKED": 5,
    },
}

#: Required remediation for every finding operation (same system_id/resource_id).
FINDING_TO_REMEDIATION = {
    "REPORT_REMAINING_RECORD": ("DELETE_RECORD",),
    "REPORT_EXPOSED_PII": ("REDACT_RECORD", "ANONYMIZE_RECORD"),
    "REPORT_ACTIVE_DATA_FLOW": ("DISABLE_DATA_FLOW",),
    "REPORT_RESTORE_RISK": ("BLOCK_RESTORE",),
}

#: Required future verification for every remediation operation.
REMEDIATION_TO_VERIFICATION = {
    "DISABLE_DATA_FLOW": "VERIFY_DATA_FLOW_DISABLED",
    "BLOCK_RESTORE": "VERIFY_RESTORE_BLOCKED",
    "DELETE_RECORD": "VERIFY_RECORD_ABSENT",
    "REDACT_RECORD": "VERIFY_RECORD_REDACTED",
    "ANONYMIZE_RECORD": "VERIFY_RECORD_ANONYMIZED",
}

#: Dataset ids used in evidence_id: file_id, activity_id, audit_id.
EVIDENCE_ID_RE = re.compile(r"^(?:evidence_\d+|activity_\d+|audit_\d+)$")
CASE_ID_RE = re.compile(r"^case_\d+$")

SUBMISSION_ITEMS = (
    "output.csv",
    "code.zip",
    "log.txt",
    "INTERVIEW_GUIDE.md",
)
#: Marker written by scripts/build_transcript.py into submission/log.txt.
TRANSCRIPT_APPENDIX_MARKER = "# PART II — FULL CONVERSATION TRANSCRIPTS"
INTERVIEW_GUIDES = ("INTERVIEW_GUIDE.md",)
REQUIRED_ZIP_ENTRIES = (
    "README.md",
    "evaluation/evaluation_report.md",
    "evaluation/usage_report.md",
)
#: Any of these makes ``uv run python -m ghost_audit ...`` runnable.
ZIP_ENTRY_POINT_CANDIDATES = (
    "src/ghost_audit/__main__.py",
    "code/main.py",
    "main.py",
)

FORBIDDEN_ZIP_DIR_NAMES = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    ".uv-cache",
    ".ocr-cache",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "work",
    "dataset",
}
NESTED_ARCHIVE_SUFFIXES = {
    ".zip",
    ".tar",
    ".gz",
    ".tgz",
    ".bz2",
    ".xz",
    ".7z",
    ".rar",
    ".whl",
}
FORBIDDEN_ZIP_BASENAMES = {
    ".DS_Store",
    ".env",
    "api_key.txt",
    "output.csv",
    "log.txt",
}

#: Patterns that indicate leaked credentials inside a packaged text artifact.
SECRET_PATTERNS = (
    (
        "private key block",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"),
    ),
    ("sk- style API key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")),
    (
        "bearer token",
        re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{20,}\b"),
    ),
    (
        "literal credential assignment",
        re.compile(
            r"(?i)\b(?:[a-z0-9_]*api[_-]?key[a-z0-9_]*|[a-z0-9_]*secret[a-z0-9_]*"
            r"|token|password)\b\s*[:=]\s*[\"'][^\"'\s]{16,}[\"']"
        ),
    ),
    ("AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
)

TEXT_SUFFIXES = {
    ".py",
    ".md",
    ".txt",
    ".toml",
    ".lock",
    ".json",
    ".yaml",
    ".yml",
    ".swift",
    ".sh",
    ".cfg",
    ".ini",
    ".sql",
    ".csv",
    ".log",
    "",
}


# ---------------------------------------------------------------------------
# Reporting helper.
# ---------------------------------------------------------------------------


@dataclass
class Report:
    """Collects errors/warnings framed by validation section."""

    sections: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    _current: str = "general"

    def section(self, title: str) -> None:
        self._current = title
        self.sections.setdefault(title, {"error": [], "warn": [], "info": []})

    def _add(self, level: str, message: str) -> None:
        self.sections.setdefault(self._current, {"error": [], "warn": [], "info": []})
        self.sections[self._current][level].append(message)

    def error(self, message: str) -> None:
        self._add("error", message)

    def warn(self, message: str) -> None:
        self._add("warn", message)

    def info(self, message: str) -> None:
        self._add("info", message)

    def missing(self, message: str) -> None:
        """A required artifact is missing: forces exit code 2 when not skipped."""

        self._add("error", message)

    @property
    def error_count(self) -> int:
        return sum(len(s["error"]) for s in self.sections.values())

    @property
    def warn_count(self) -> int:
        return sum(len(s["warn"]) for s in self.sections.values())

    def print_report(self, *, verbose: bool) -> None:
        if not self.sections:
            print("No checks ran.")
            return
        for title, buckets in self.sections.items():
            errors, warns, infos = buckets["error"], buckets["warn"], buckets["info"]
            print(f"\n== {title} ==")
            for message in errors:
                print(f"  FAIL {message}")
            for message in warns:
                print(f"  WARN {message}")
            if verbose:
                for message in infos:
                    print(f"  INFO {message}")
            if not errors and not warns:
                print("  OK")


# ---------------------------------------------------------------------------
# Small utilities.
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate output.csv, submission/code.zip and the submission/ directory "
            "against the Ghost Data Still Exists contract."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  %(prog)s                                      # full validation\n"
            "  %(prog)s --skip-missing                       # validate what exists\n"
            "  %(prog)s --dataset /path/to/dataset           # + dataset cross-checks\n"
            "  %(prog)s --csv output.csv --json-report /tmp/validation.json\n"
        ),
    )
    parser.add_argument(
        "--repo-root",
        default=str(REPO_ROOT_DEFAULT),
        help="repository root (default: parent of scripts/)",
    )
    parser.add_argument(
        "--csv",
        default="output.csv",
        help="prediction CSV relative to the repo root (default: output.csv)",
    )
    parser.add_argument(
        "--zip",
        default="submission/code.zip",
        help="code archive relative to the repo root (default: submission/code.zip)",
    )
    parser.add_argument(
        "--submission-dir",
        default="submission",
        help="submission directory relative to the repo root (default: submission)",
    )
    parser.add_argument(
        "--dataset",
        default=None,
        help=(
            "official participant-facing dataset directory; enables coverage, "
            "evidence-id and evidence-timestamp cross-checks"
        ),
    )
    parser.add_argument(
        "--expected-rows",
        type=int,
        default=EXPECTED_ROWS,
        help=f"expected number of data rows without --dataset (default: {EXPECTED_ROWS})",
    )
    parser.add_argument(
        "--skip-missing",
        action="store_true",
        help="downgrade missing artifacts to warnings (exit 2 otherwise)",
    )
    parser.add_argument(
        "--lenient-pairing",
        action="store_true",
        help=(
            "downgrade finding->remediation and remediation->verification pairing "
            "mismatches to warnings"
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="print informational checks as well",
    )
    parser.add_argument(
        "--json-report",
        default=None,
        help="optional path to write a machine-readable summary JSON",
    )
    return parser.parse_args(argv)


def parse_timestamp(value: str) -> datetime | None:
    """Parse the ISO-8601 UTC timestamps used by the dataset."""

    text = (value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def timestamp_le(earlier: str, later: str) -> bool | None:
    """True when ``earlier <= later``; None if either value is unparseable."""

    left, right = parse_timestamp(earlier), parse_timestamp(later)
    if left is None or right is None:
        return None
    return left <= right


def case_sort_key(case_id: str) -> tuple[int, int, str]:
    """Canonical row order for case ids.

    The canonical order is numeric by case number: ``case_33 < case_99 <
    case_100 < ... < case_550``. Lexicographic sorting would put ``case_100``
    before ``case_33``, which the grader does not expect. Anything that does not
    match the ``prefix_<number>`` shape is ordered after numeric ids, by text.
    """

    match = re.match(r"^([A-Za-z_]+)_(\d+)$", case_id)
    if match:
        return (0, int(match.group(2)), case_id)
    return (1, 0, case_id)


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def read_bytes(path: Path) -> bytes:
    return path.read_bytes()


def decode_text(data: bytes) -> str | None:
    for encoding in ("utf-8", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def scan_secrets(text: str, where: str, report: Report) -> None:
    for label, pattern in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            snippet = match.group(0)
            if len(snippet) > 60:
                snippet = snippet[:57] + "..."
            report.error(f"{where}: possible {label} found: {snippet!r}")


def flush(report: Report) -> None:
    report.section("summary")
    report.info(
        f"{report.error_count} error(s), {report.warn_count} warning(s) "
        f"across {len(report.sections)} section(s)"
    )


# ---------------------------------------------------------------------------
# output.csv contract validation.
# ---------------------------------------------------------------------------


def parse_steps(case_id: str, line_no: int, raw: str, report: Report):
    try:
        steps = json.loads(raw)
    except json.JSONDecodeError as exc:
        report.error(
            f"line {line_no} ({case_id}): steps_json is not valid JSON "
            f"({exc.msg} at char {exc.pos}); the grader parses this as JSON"
        )
        return None
    if not isinstance(steps, list):
        report.error(
            f"line {line_no} ({case_id}): steps_json must be a JSON array, "
            f"got {type(steps).__name__}"
        )
        return None
    return steps


def validate_case_steps(
    line_no: int,
    case_id: str,
    status: str,
    steps: list,
    report: Report,
    *,
    lenient_pairing: bool,
) -> None:
    """Structural + semantic checks for one case's step array."""

    ctx = f"line {line_no} ({case_id})"

    if status == "NOT_APPLICABLE":
        if steps:
            report.error(
                f"{ctx}: NOT_APPLICABLE must use steps_json=[], got {len(steps)} step(s)"
            )
        return

    if not steps:
        report.error(
            f"{ctx}: {status} case has an empty steps_json array; "
            "only NOT_APPLICABLE uses []"
        )
        return

    valid_steps: list[dict] = []
    structurally_valid = True
    seen_step_numbers: set[int] = set()

    for index, step in enumerate(steps):
        step_ctx = f"{ctx} step index {index}"
        if not isinstance(step, dict):
            report.error(f"{step_ctx}: step must be a JSON object, got {type(step).__name__}")
            structurally_valid = False
            continue

        keys = set(step)
        missing = [name for name in STEP_FIELDS if name not in keys]
        extra = sorted(keys - set(STEP_FIELDS))
        if missing:
            report.error(f"{step_ctx}: missing required field(s): {', '.join(missing)}")
            structurally_valid = False
        if extra:
            report.error(f"{step_ctx}: unexpected field(s): {', '.join(extra)}")
            structurally_valid = False

        step_no = step.get("step_no")
        if not isinstance(step_no, int) or isinstance(step_no, bool):
            report.error(f"{step_ctx}: step_no must be an integer, got {step_no!r}")
            structurally_valid = False
        else:
            step_ctx = f"{ctx} step_no={step_no}"
            if step_no != index + 1:
                report.error(
                    f"{step_ctx}: step_no must be consecutive from 1 "
                    f"(expected {index + 1})"
                )
                structurally_valid = False
            if step_no in seen_step_numbers:
                report.error(f"{step_ctx}: duplicate step_no")
                structurally_valid = False
            seen_step_numbers.add(step_no)
            if step_no <= 0:
                report.error(f"{step_ctx}: step_no must be positive")
                structurally_valid = False

        phase = step.get("phase")
        operation = step.get("operation")
        state = step.get("state")
        for name, value in (
            ("phase", phase),
            ("operation", operation),
            ("state", state),
            ("system_id", step.get("system_id")),
            ("resource_id", step.get("resource_id")),
        ):
            if not isinstance(value, str) or not value.strip():
                report.error(f"{step_ctx}: {name} must be a non-empty string, got {value!r}")
                structurally_valid = False

        if isinstance(phase, str) and isinstance(operation, str):
            expected_state = ALLOWED_STEPS.get((phase, operation))
            if expected_state is None:
                if phase in OPERATION_ORDER:
                    allowed = ", ".join(OPERATION_ORDER[phase])
                    report.error(
                        f"{step_ctx}: operation {operation!r} is not allowed in phase "
                        f"{phase!r}; allowed: {allowed}"
                    )
                else:
                    report.error(
                        f"{step_ctx}: unknown phase {phase!r}; "
                        "allowed: FINDING, REMEDIATION, VERIFICATION"
                    )
                structurally_valid = False
            elif isinstance(state, str) and state != expected_state:
                report.error(
                    f"{step_ctx}: {phase}/{operation} requires state "
                    f"{expected_state!r}, got {state!r}"
                )
                structurally_valid = False

        evidence_id = step.get("evidence_id")
        if not isinstance(evidence_id, str):
            report.error(
                f"{step_ctx}: evidence_id must be a string (use \"\" when absent), "
                f"got {evidence_id!r}"
            )
            structurally_valid = False
        elif evidence_id and not EVIDENCE_ID_RE.match(evidence_id):
            report.error(
                f"{step_ctx}: evidence_id {evidence_id!r} is not a dataset id; "
                "expected file_id/activity_id/audit_id such as evidence_54, "
                "activity_01 or audit_01"
            )
            structurally_valid = False

        if structurally_valid:
            valid_steps.append(step)

    if not structurally_valid:
        report.info(f"{ctx}: skipping semantic checks because structural errors were found")
        return

    phases = [step["phase"] for step in valid_steps]

    if status == "COMPLETE":
        non_verification = [
            step for step in valid_steps if step["phase"] != "VERIFICATION"
        ]
        if non_verification:
            report.error(
                f"{ctx}: COMPLETE cases may only contain VERIFICATION steps; "
                f"found {len(non_verification)} non-verification step(s) "
                f"(first: {non_verification[0]['phase']}/{non_verification[0]['operation']})"
            )
        for step in valid_steps:
            if not step["evidence_id"]:
                report.error(
                    f"{ctx}: COMPLETE verification step "
                    f"{step['step_no']} ({step['operation']} on "
                    f"{step['system_id']}/{step['resource_id']}) must cite the "
                    "evidence that proves the current state"
                )
        if not any(step["operation"] == "VERIFY_RECORD_ABSENT" for step in valid_steps):
            report.warn(
                f"{ctx}: COMPLETE case has no VERIFY_RECORD_ABSENT step; "
                "the canonical samples always verify the account/service baseline"
            )

    elif status == "INCOMPLETE":
        findings = [step for step in valid_steps if step["phase"] == "FINDING"]
        remediations = [step for step in valid_steps if step["phase"] == "REMEDIATION"]
        verifications = [step for step in valid_steps if step["phase"] == "VERIFICATION"]
        if not findings:
            report.error(f"{ctx}: INCOMPLETE case must contain at least one FINDING step")
        if not remediations:
            report.error(
                f"{ctx}: INCOMPLETE case must contain the complete REMEDIATION plan"
            )
        if not verifications:
            report.error(
                f"{ctx}: INCOMPLETE case must contain the VERIFICATION checks that "
                "must pass after remediation"
            )
        for step in findings:
            if not step["evidence_id"]:
                report.error(
                    f"{ctx}: FINDING step {step['step_no']} "
                    f"({step['operation']} on {step['system_id']}/{step['resource_id']}) "
                    "must cite participant-visible evidence"
                )
        for step in remediations + verifications:
            if step["evidence_id"]:
                report.error(
                    f"{ctx}: {step['phase']} step {step['step_no']} "
                    f"({step['operation']}) describes future work and must use an "
                    f"empty evidence_id, got {step['evidence_id']!r}"
                )
        validate_remediation_pairing(
            ctx, findings, remediations, verifications, report,
            lenient=lenient_pairing,
        )

    # Phase ordering.
    for index in range(1, len(valid_steps)):
        previous, current = valid_steps[index - 1], valid_steps[index]
        if PHASE_ORDER[current["phase"]] < PHASE_ORDER[previous["phase"]]:
            report.error(
                f"{ctx}: phase order must be FINDING -> REMEDIATION -> VERIFICATION; "
                f"step {current['step_no']} ({current['phase']}) follows "
                f"{previous['phase']}"
            )
            break

    # Within-phase ordering: operation order, then system_id, then resource_id.
    keys = [
        (
            PHASE_ORDER[step["phase"]],
            OPERATION_ORDER[step["phase"]][step["operation"]],
            step["system_id"],
            step["resource_id"],
        )
        for step in valid_steps
    ]
    for index in range(1, len(keys)):
        if keys[index] < keys[index - 1]:
            previous, current = valid_steps[index - 1], valid_steps[index]
            report.error(
                f"{ctx}: steps are out of order within phase {current['phase']}: "
                f"step {current['step_no']} "
                f"({current['operation']} {current['system_id']}/"
                f"{current['resource_id']}) must not come after step "
                f"{previous['step_no']} ({previous['operation']} "
                f"{previous['system_id']}/{previous['resource_id']}); order is "
                "operation order from the problem statement, then system_id, "
                "then resource_id ascending"
            )
            break

    # Exact duplicate steps are almost always a planner bug.
    duplicate_keys = Counter(
        (step["phase"], step["operation"], step["system_id"], step["resource_id"])
        for step in valid_steps
    )
    for key, count in duplicate_keys.items():
        if count > 1:
            report.warn(f"{ctx}: duplicate step {key} appears {count} times")


def validate_remediation_pairing(
    ctx: str,
    findings: list[dict],
    remediations: list[dict],
    verifications: list[dict],
    report: Report,
    *,
    lenient: bool,
) -> None:
    """Every finding needs its remediation; every remediation its future check."""

    def emit(message: str) -> None:
        if lenient:
            report.warn(message)
        else:
            report.error(message)

    finding_counts = Counter(
        (step["system_id"], step["resource_id"], step["operation"]) for step in findings
    )
    remediation_counts = Counter(
        (step["system_id"], step["resource_id"], step["operation"])
        for step in remediations
    )
    verification_counts = Counter(
        (step["system_id"], step["resource_id"], step["operation"])
        for step in verifications
    )

    for (system_id, resource_id, operation), count in sorted(finding_counts.items()):
        targets = FINDING_TO_REMEDIATION[operation]
        matched = sum(
            remediation_counts[(system_id, resource_id, target)] for target in targets
        )
        if matched < count:
            emit(
                f"{ctx}: finding {operation} on {system_id}/{resource_id} x{count} "
                f"needs matching remediation "
                f"{' or '.join(targets)} on the same system/resource (found {matched})"
            )

    for (system_id, resource_id, operation), count in sorted(
        remediation_counts.items()
    ):
        expected = REMEDIATION_TO_VERIFICATION[operation]
        matched = verification_counts[(system_id, resource_id, expected)]
        if matched < count:
            emit(
                f"{ctx}: remediation {operation} on {system_id}/{resource_id} x{count} "
                f"needs future verification {expected} on the same system/resource "
                f"(found {matched})"
            )


def validate_output_csv(
    path: Path,
    report: Report,
    *,
    expected_rows: int,
    expected_rows_source: str,
    dataset_index: "DatasetIndex | None",
    lenient_pairing: bool,
) -> list[dict] | None:
    """Validate the CSV contract; returns parsed rows or None on fatal errors."""

    report.section(f"output.csv contract ({path})")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        report.error(f"cannot read {path}: {exc}")
        return None

    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        records = list(reader)
    except csv.Error as exc:
        report.error(f"{path}: CSV parse error: {exc}")
        return None

    if not records:
        report.error(f"{path}: file is empty; expected header + {expected_rows} data rows")
        return None

    header = records[0]
    if header != EXPECTED_HEADER:
        report.error(
            f"{path}: header must be exactly {','.join(EXPECTED_HEADER)!r}, "
            f"got {','.join(header)!r}"
        )
        if len(header) != len(EXPECTED_HEADER):
            return None

    data_rows = records[1:]
    if len(data_rows) != expected_rows:
        report.error(
            f"{path}: expected exactly {expected_rows} data row(s) "
            f"({expected_rows_source}), found {len(data_rows)}"
        )

    rows: list[dict] = []
    raw_case_ids: list[tuple[str, int]] = []  # (case_id, csv_line_no) in file order
    seen_case_ids: dict[str, int] = {}
    for offset, row in enumerate(data_rows):
        line_no = offset + 2
        if not row:
            report.warn(f"line {line_no}: blank line ignored by the validator")
            continue
        if len(row) != len(EXPECTED_HEADER):
            report.error(
                f"line {line_no}: expected {len(EXPECTED_HEADER)} columns "
                f"({','.join(EXPECTED_HEADER)}), got {len(row)} "
                "(unquoted comma in steps_json or wrong CSV dialect?)"
            )
            continue
        case_id, status, raw_steps = row
        case_id = case_id.strip()
        status = status.strip()
        raw_case_ids.append((case_id, line_no))

        if not CASE_ID_RE.match(case_id):
            report.warn(
                f"line {line_no}: case_id {case_id!r} does not look like a dataset "
                "case id (case_<number>)"
            )
        if case_id in seen_case_ids:
            report.error(
                f"line {line_no}: duplicate case_id {case_id!r} "
                f"(first seen on line {seen_case_ids[case_id]})"
            )
        else:
            seen_case_ids[case_id] = line_no

        if status not in STATUSES:
            report.error(
                f"line {line_no} ({case_id}): deletion_status must be one of "
                f"{', '.join(STATUSES)}, got {status!r}"
            )
            continue

        steps = parse_steps(case_id, line_no, raw_steps, report)
        if steps is None:
            continue
        validate_case_steps(
            line_no,
            case_id,
            status,
            steps,
            report,
            lenient_pairing=lenient_pairing,
        )
        rows.append(
            {
                "case_id": case_id,
                "deletion_status": status,
                "steps": steps,
                "line_no": line_no,
            }
        )

    # Row ordering: canonical numeric case order, not lexicographic sorting.
    file_case_ids = [case_id for case_id, _ in raw_case_ids]
    order_keys = [case_sort_key(case_id) for case_id in file_case_ids]
    if order_keys != sorted(order_keys):
        first_bad = next(
            index
            for index in range(1, len(order_keys))
            if order_keys[index] < order_keys[index - 1]
        )
        previous_id, previous_line = raw_case_ids[first_bad - 1]
        current_id, current_line = raw_case_ids[first_bad]
        report.error(
            f"{path}: data rows must be sorted ascending by numeric case id "
            "(case_33, case_99, case_100, ... case_550), not lexicographically; "
            f"line {current_line} ({current_id}) follows "
            f"line {previous_line} ({previous_id})"
        )

    status_counts = Counter(row["deletion_status"] for row in rows)
    report.info(
        "status distribution: "
        + ", ".join(f"{status}={status_counts.get(status, 0)}" for status in STATUSES)
    )
    report.info(f"parsed {len(rows)} step-valid case row(s)")

    if dataset_index is not None and rows:
        validate_dataset_cross_checks(rows, dataset_index, report)
    return rows


# ---------------------------------------------------------------------------
# Dataset cross-checks (coverage, evidence ids, evidence timestamps).
# ---------------------------------------------------------------------------


@dataclass
class DatasetIndex:
    requests: dict[str, dict[str, str]]
    file_captured_at: dict[str, str]
    activity_time: dict[str, str]
    activity_systems: dict[str, set[str]]
    audit_time: dict[str, str]
    evidence_manifest_count: int


def load_dataset_index(dataset_dir: Path, report: Report) -> DatasetIndex | None:
    """Load the four participant-facing CSVs used for cross-checks."""

    report.section(f"dataset cross-checks ({dataset_dir})")
    required = {
        "deletion_requests.csv": None,
        "evidence_files.csv": None,
        "system_activity.csv": None,
        "user_audit_log.csv": None,
    }
    for name in required:
        candidate = dataset_dir / name
        if not candidate.is_file():
            report.error(
                f"missing dataset file {candidate}; cannot cross-check the output"
            )
            return None
        required[name] = candidate

    requests = {
        row["case_id"]: row
        for row in read_csv_rows(required["deletion_requests.csv"])
        if row.get("case_id")
    }
    file_captured_at = {
        row["file_id"]: row.get("captured_at", "")
        for row in read_csv_rows(required["evidence_files.csv"])
        if row.get("file_id")
    }
    activity_time: dict[str, str] = {}
    activity_systems: dict[str, set[str]] = {}
    for row in read_csv_rows(required["system_activity.csv"]):
        activity_id = row.get("activity_id", "")
        if not activity_id:
            continue
        activity_time[activity_id] = row.get("event_time", "")
        systems = {
            value
            for value in (row.get("source_system"), row.get("target_system"))
            if value
        }
        activity_systems[activity_id] = systems
    audit_time = {
        row["audit_id"]: row.get("event_time", "")
        for row in read_csv_rows(required["user_audit_log.csv"])
        if row.get("audit_id")
    }

    report.info(
        f"{len(requests)} requests, {len(file_captured_at)} evidence files, "
        f"{len(activity_time)} activities, {len(audit_time)} audit events"
    )
    return DatasetIndex(
        requests=requests,
        file_captured_at=file_captured_at,
        activity_time=activity_time,
        activity_systems=activity_systems,
        audit_time=audit_time,
        evidence_manifest_count=len(file_captured_at),
    )


def validate_dataset_cross_checks(
    rows: list[dict],
    index: DatasetIndex,
    report: Report,
) -> None:
    """Coverage, evidence-id existence and captured_at <= audit_at checks."""

    report.section("dataset cross-checks: case coverage + cited evidence")
    expected_ids = set(index.requests)
    actual_ids = {row["case_id"] for row in rows}
    missing = sorted(expected_ids - actual_ids)
    extra = sorted(actual_ids - expected_ids)
    if missing:
        preview = ", ".join(missing[:10])
        suffix = "" if len(missing) <= 10 else f" ... (+{len(missing) - 10} more)"
        report.error(
            f"{len(missing)} case(s) from deletion_requests.csv are missing in "
            f"output.csv: {preview}{suffix}"
        )
    if extra:
        preview = ", ".join(extra[:10])
        suffix = "" if len(extra) <= 10 else f" ... (+{len(extra) - 10} more)"
        report.error(
            f"{len(extra)} case_id(s) in output.csv are not in "
            f"deletion_requests.csv: {preview}{suffix}"
        )
    if not missing and not extra:
        report.info(f"case coverage OK: all {len(expected_ids)} cases present exactly once")

    unknown_ids: dict[str, set[str]] = {}
    late_ids: list[str] = []
    for row in rows:
        case_id = row["case_id"]
        request = index.requests.get(case_id)
        if request is None:
            continue
        audit_at = request.get("audit_at", "")
        for step in row["steps"]:
            evidence_id = step["evidence_id"]
            if not evidence_id:
                continue
            if evidence_id in index.file_captured_at:
                captured_at = index.file_captured_at[evidence_id]
            elif evidence_id in index.activity_time:
                captured_at = index.activity_time[evidence_id]
            elif evidence_id in index.audit_time:
                captured_at = index.audit_time[evidence_id]
            else:
                unknown_ids.setdefault(evidence_id, set()).add(case_id)
                continue
            ordered = timestamp_le(captured_at, audit_at)
            if ordered is None:
                report.warn(
                    f"{case_id}: step {step['step_no']} cites {evidence_id} but a "
                    f"timestamp could not be compared ({captured_at!r} vs audit_at "
                    f"{audit_at!r})"
                )
            elif not ordered:
                late_ids.append(
                    f"{case_id} step {step['step_no']} cites {evidence_id} "
                    f"(captured {captured_at}) after audit_at {audit_at}"
                )

    for evidence_id, case_ids in sorted(unknown_ids.items()):
        preview = ", ".join(sorted(case_ids)[:5])
        suffix = "" if len(case_ids) <= 5 else ", ..."
        report.error(
            f"evidence_id {evidence_id!r} is not a known file_id/activity_id/audit_id "
            f"(cited by {preview}{suffix})"
        )
    for message in late_ids[:20]:
        report.error(
            f"{message}; a case must only cite evidence captured at or before audit_at"
        )
    if len(late_ids) > 20:
        report.error(f"... and {len(late_ids) - 20} more late-evidence citation(s)")
    if not unknown_ids and not late_ids:
        report.info("all cited evidence ids resolve to the dataset and respect audit_at")


# ---------------------------------------------------------------------------
# code.zip validation.
# ---------------------------------------------------------------------------


def zip_entry_suffix(name: str) -> str:
    return Path(name).suffix.lower()


def validate_code_zip(path: Path, report: Report) -> None:
    report.section(f"code.zip contents ({path})")
    if not path.is_file():
        return
    if not zipfile.is_zipfile(path):
        report.error(f"{path}: not a readable zip archive")
        return

    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            names: list[str] = []
            size_by_name: dict[str, int] = {}
            duplicates: set[str] = set()
            forbidden_dirs: dict[str, list[str]] = {}
            nested_archives: list[str] = []
            forbidden_basenames: list[str] = []
            unsafe_paths: list[str] = []
            suspicious_entries: list[str] = []

            for info in infos:
                raw_name = info.filename
                name = raw_name.replace("\\", "/")
                if not name or name.endswith("/"):
                    report.warn(f"directory entry present (harmless): {raw_name}")
                    continue
                names.append(name)
                size_by_name[name] = info.file_size
                if name in duplicates:
                    report.error(f"duplicate archive entry: {name}")
                duplicates.add(name)

                parts = [part for part in name.split("/") if part]
                if name.startswith("/") or ".." in parts:
                    unsafe_paths.append(name)
                for part in parts[:-1]:
                    if part in FORBIDDEN_ZIP_DIR_NAMES:
                        forbidden_dirs.setdefault(part, []).append(name)
                if zip_entry_suffix(name) in NESTED_ARCHIVE_SUFFIXES:
                    nested_archives.append(name)
                if parts and parts[-1] in FORBIDDEN_ZIP_BASENAMES:
                    forbidden_basenames.append(name)
                if parts and parts[-1].startswith("api_key"):
                    suspicious_entries.append(name)

                # Secret scan for text-like entries.
                suffix = zip_entry_suffix(name)
                if suffix not in TEXT_SUFFIXES or info.file_size > 4 * 1024 * 1024:
                    continue
                try:
                    data = archive.read(info)
                except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                    report.error(f"cannot read archive entry {name}: {exc}")
                    continue
                text = decode_text(data)
                if text is not None:
                    scan_secrets(text, f"code.zip:{name}", report)
    except zipfile.BadZipFile as exc:
        report.error(f"{path}: corrupt zip archive: {exc}")
        return

    def preview(items: list[str], limit: int = 8) -> str:
        head = ", ".join(items[:limit])
        return head + (f", ... (+{len(items) - limit} more)" if len(items) > limit else "")

    if unsafe_paths:
        report.error(
            "unsafe archive entry path(s) (absolute or containing '..'): "
            + preview(unsafe_paths)
        )
    if forbidden_dirs:
        for directory, entries in sorted(forbidden_dirs.items()):
            report.error(
                f"code.zip must not contain '{directory}/': "
                f"{preview(entries)}"
            )
    if nested_archives:
        report.error(
            "nested archive(s) are rejected by the grader: " + preview(nested_archives)
        )
    if forbidden_basenames:
        report.error(
            "forbidden file(s) in code.zip (dataset template, secrets file or "
            "submission-only artifact): " + preview(forbidden_basenames)
        )
    if suspicious_entries:
        report.error(
            "possible credential file(s) in code.zip: " + preview(suspicious_entries)
        )

    name_set = set(names)
    for required in REQUIRED_ZIP_ENTRIES:
        if required not in name_set:
            report.error(f"required entry missing from code.zip: {required}")
    entry_point = [candidate for candidate in ZIP_ENTRY_POINT_CANDIDATES if candidate in name_set]
    if not entry_point:
        report.error(
            "code.zip has no runnable entry point; expected one of: "
            + ", ".join(ZIP_ENTRY_POINT_CANDIDATES)
            + " (README documents `uv run python -m ghost_audit run ...`)"
        )
    elif not any(
        candidate.startswith("src/ghost_audit/") for candidate in entry_point
    ):
        report.warn(
            "code.zip entry point is "
            + ", ".join(entry_point)
            + "; expected 'src/ghost_audit/__main__.py' for "
            "`uv run python -m ghost_audit`"
        )
    if "src/ghost_audit/__main__.py" not in name_set and any(
        candidate.startswith("code/") for candidate in entry_point
    ):
        report.warn(
            "src/ghost_audit/__main__.py missing; 'python -m ghost_audit' will fail"
        )

    for optional, label in (
        ("pyproject.toml", "pyproject.toml (needed for `uv sync`)"),
        ("uv.lock", "uv.lock (reproducible dependency resolution)"),
        (".python-version", ".python-version (Python version pin)"),
        ("INTERVIEW_GUIDE.md", "INTERVIEW_GUIDE.md"),
        ("src/ghost_audit/__main__.py", "src/ghost_audit/__main__.py"),
    ):
        if optional not in name_set:
            report.warn(f"recommended entry missing from code.zip: {label}")

    if "src/templete_project/" in " ".join(names):
        report.warn(
            "code.zip contains the stale template package 'src/templete_project/'; "
            "remove it from the repository before the final build"
        )

    for report_path in ("evaluation/evaluation_report.md", "evaluation/usage_report.md"):
        if report_path not in name_set:
            continue
        size = size_by_name.get(report_path, 0)
        if size < 200:
            report.warn(
                f"{report_path} is only {size} bytes; fill in the measured "
                "evaluation/token usage before submitting"
            )
            continue
        with zipfile.ZipFile(path) as archive:
            try:
                report_text = archive.read(report_path).decode("utf-8", errors="replace")
            except (OSError, KeyError):
                continue
        placeholders = report_text.count("TBD") + report_text.count("TODO")
        if placeholders:
            report.warn(
                f"{report_path} still contains {placeholders} TBD/TODO "
                "placeholder(s); replace them with the final measured values"
            )

    uncompressed = sum(info.file_size for info in infos if not info.is_dir())
    compressed = path.stat().st_size
    report.info(
        f"{len(name_set)} file entries, {uncompressed} bytes uncompressed, "
        f"{compressed} bytes on disk"
    )
    report.info("no forbidden paths found" if not (
        unsafe_paths
        or forbidden_dirs
        or nested_archives
        or forbidden_basenames
        or suspicious_entries
    ) else "forbidden path check failed")


# ---------------------------------------------------------------------------
# submission/ directory validation.
# ---------------------------------------------------------------------------


def validate_submission_dir(
    submission_dir: Path,
    report: Report,
    *,
    csv_path: Path,
    skip_missing: bool,
) -> list[str]:
    """Validate the submission/ deliverable; returns names of missing items."""

    report.section(f"submission directory ({submission_dir})")
    if not submission_dir.is_dir():
        message = (
            f"missing submission directory {submission_dir}; expected exactly "
            + ", ".join(SUBMISSION_ITEMS)
        )
        if skip_missing:
            report.warn(message + " (skipped: --skip-missing)")
            return []
        report.error(message)
        return list(SUBMISSION_ITEMS)

    present = {entry.name for entry in submission_dir.iterdir()}
    missing_items: list[str] = []
    for item in SUBMISSION_ITEMS:
        if item in present:
            continue
        if skip_missing:
            report.warn(
                f"missing required submission item (skipped: --skip-missing): "
                f"{submission_dir / item}"
            )
            continue
        missing_items.append(item)
        report.error(f"missing required submission item: {submission_dir / item}")

    extras = sorted(present - set(SUBMISSION_ITEMS))
    if extras:
        report.warn(
            "unexpected item(s) in submission/ (the deliverable is exactly "
            f"{', '.join(SUBMISSION_ITEMS)}): " + ", ".join(extras)
        )

    # chat transcript: the root log.txt is PART I; PART II holds the generated
    # conversation appendices written by scripts/build_transcript.py.
    log_path = submission_dir / "log.txt"
    if log_path.is_file():
        try:
            log_text = log_path.read_text(encoding="utf-8")
        except OSError as exc:
            report.error(f"cannot read {log_path}: {exc}")
        else:
            if not log_text.strip():
                report.error(f"{log_path}: chat transcript is empty")
            else:
                scan_secrets(log_text, "submission/log.txt", report)
                if 'tool=' not in log_text:
                    report.warn(
                        f"{log_path}: no `tool=` line found; AGENTS.md §5 requires "
                        "tool= and model= in every session entry"
                    )
                if 'model=' not in log_text:
                    report.warn(
                        f"{log_path}: no `model=` line found; AGENTS.md §5 requires "
                        "tool= and model= in every session entry"
                    )
                if not re.search(r"^## \[", log_text, re.MULTILINE):
                    report.warn(
                        f"{log_path}: no `## [timestamp]` section headings found; "
                        "does this look like the agent session transcript?"
                    )
                root_log = submission_dir.parent / "log.txt"
                if root_log.is_file():
                    try:
                        root_text = root_log.read_text(encoding="utf-8").rstrip("\n")
                    except OSError as exc:
                        report.error(f"cannot read {root_log}: {exc}")
                    else:
                        if root_text and not log_text.startswith(root_text):
                            report.warn(
                                f"{log_path}: does not start with the verbatim root "
                                "log.txt; regenerate it with "
                                "`python3 scripts/build_transcript.py`"
                            )
                if TRANSCRIPT_APPENDIX_MARKER not in log_text:
                    report.warn(
                        f"{log_path}: missing the generated PART II conversation "
                        "appendices; run `python3 scripts/build_transcript.py`"
                    )
                report.info(f"chat transcript: {len(log_text.splitlines())} lines")

    # interview guide must be substantive and final.
    for guide_name in INTERVIEW_GUIDES:
        guide_path = submission_dir / guide_name
        if not guide_path.is_file():
            continue  # missing items are reported above
        try:
            guide_text = guide_path.read_text(encoding="utf-8")
        except OSError as exc:
            report.error(f"cannot read {guide_path}: {exc}")
            continue
        if len(guide_text.strip()) < 500:
            report.warn(
                f"{guide_path}: only {len(guide_text)} characters; the AI "
                "interview guide should be substantive"
            )
        todo_count = guide_text.count("TODO")
        if todo_count:
            report.error(
                f"{guide_path}: still contains {todo_count} TODO placeholder(s); "
                "fill them from the final run before submitting"
            )
        report.info(f"interview guide {guide_name}: {len(guide_text.splitlines())} lines")

    # submission/output.csv should be the same artifact as the root output.csv.
    submission_csv = submission_dir / "output.csv"
    if submission_csv.is_file() and csv_path.is_file():
        try:
            if submission_csv.read_bytes() != csv_path.read_bytes():
                report.warn(
                    f"{submission_csv} differs from {csv_path}; copy the final "
                    "root-level output.csv into submission/ before submitting"
                )
        except OSError as exc:
            report.error(f"cannot compare output.csv copies: {exc}")

    return missing_items


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------


def resolve_dataset_dir(raw: str | None, repo_root: Path) -> Path | None:
    if raw is not None:
        return Path(raw).expanduser().resolve()
    default = (
        repo_root
        / "docs"
        / "refs"
        / "hackerrank-orchestrate-october26-main"
        / "dataset"
    )
    return default if default.is_dir() else None


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = Path(args.repo_root).expanduser().resolve()
    csv_path = repo_root / args.csv
    zip_path = repo_root / args.zip
    submission_dir = repo_root / args.submission_dir
    dataset_dir = resolve_dataset_dir(args.dataset, repo_root)

    report = Report()
    missing_count = 0

    # 1) output.csv.
    report.section("output.csv location")
    rows: list[dict] | None = None
    if not csv_path.is_file():
        if args.skip_missing:
            report.warn(
                f"missing artifact (skipped: --skip-missing): {csv_path}; "
                "run the pipeline first"
            )
        else:
            report.error(
                f"missing required artifact: {csv_path} "
                "(run `uv run python -m ghost_audit run ... --output output.csv`)"
            )
            missing_count += 1
    else:
        report.info(f"found {csv_path} ({csv_path.stat().st_size} bytes)")
        dataset_index: DatasetIndex | None = None
        expected_rows = args.expected_rows
        expected_rows_source = "--expected-rows"
        if dataset_dir is not None:
            dataset_index = load_dataset_index(dataset_dir, report)
            if dataset_index is not None:
                expected_rows = len(dataset_index.requests)
                expected_rows_source = "dataset/deletion_requests.csv"
        elif args.dataset is not None:
            report.error(
                f"dataset directory not found: {dataset_dir}; skipping dataset "
                "cross-checks"
            )
        else:
            report.info(
                "no --dataset given and the local reference dataset is absent; "
                "skipping coverage/evidence-id cross-checks"
            )
        rows = validate_output_csv(
            csv_path,
            report,
            expected_rows=expected_rows,
            expected_rows_source=expected_rows_source,
            dataset_index=dataset_index,
            lenient_pairing=args.lenient_pairing,
        )
    # 2) code.zip.
    report.section("code.zip location")
    if not zip_path.is_file():
        if args.skip_missing:
            report.warn(
                f"missing artifact (skipped: --skip-missing): {zip_path}; build it "
                "with `uv run python scripts/build_code_zip.py`"
            )
        else:
            report.error(
                f"missing required artifact: {zip_path} "
                "(run `uv run python scripts/build_code_zip.py`)"
            )
            missing_count += 1
    else:
        report.info(f"found {zip_path} ({zip_path.stat().st_size} bytes)")
    validate_code_zip(zip_path, report)

    # 3) submission/ directory.
    missing_submission_items = validate_submission_dir(
        submission_dir,
        report,
        csv_path=csv_path,
        skip_missing=args.skip_missing,
    )
    missing_count += len(missing_submission_items)

    # 4) Cross-artifact sanity: submission/code.zip is the same archive we validated.
    report.section("cross-artifact consistency")
    submission_zip = submission_dir / "code.zip"
    if submission_zip.is_file() and zip_path.is_file():
        try:
            if submission_zip.read_bytes() != zip_path.read_bytes():
                report.warn(
                    f"{submission_zip} differs from {zip_path}; rebuild or copy the "
                    "final archive into submission/ before submitting"
                )
        except OSError as exc:
            report.error(f"cannot compare code.zip copies: {exc}")
    if rows is not None:
        report.info(f"{len(rows)} validated case row(s) in {csv_path.name}")

    flush(report)
    report.print_report(verbose=args.verbose)

    if args.json_report:
        summary = {
            "errors": report.error_count,
            "warnings": report.warn_count,
            "missing_required_artifacts": missing_count,
            "sections": report.sections,
        }
        try:
            Path(args.json_report).write_text(
                json.dumps(summary, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            print(f"\nwrote JSON report: {args.json_report}")
        except OSError as exc:
            print(f"WARN could not write JSON report: {exc}")

    print()
    if missing_count:
        print(
            f"RESULT: MISSING ARTIFACTS ({missing_count} required file(s) absent; "
            "use --skip-missing to validate partial work)"
        )
        return 2
    if report.error_count:
        print(f"RESULT: FAIL ({report.error_count} error(s), {report.warn_count} warning(s))")
        return 1
    print(f"RESULT: PASS ({report.warn_count} warning(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
