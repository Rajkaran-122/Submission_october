"""Tests for the evidence parsing layer.

Two groups of tests:

* dataset tests - parse all 94 official evidence files through
  :class:`ghost_audit.dataset.Dataset` and assert known facts.  They are
  skipped when the read-only dataset checkout is absent (or when PNG OCR is
  unavailable on the machine);
* format tests - small synthetic documents that pin each parser syntax without
  needing the dataset.
"""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path

import pytest

from ghost_audit.dataset import Dataset, EvidenceDocument
from ghost_audit.ocr import OCRUnavailable
from ghost_audit.evidence import (
    CANONICAL_STATES,
    RECORD_TYPES,
    classify_path,
    family_summary,
    parse_all,
    parse_document,
    sql_table_summary,
)
from ghost_audit.evidence import base as evidence_base_module
from ghost_audit.evidence.registry import ParseResult

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = (
    PROJECT_ROOT / "docs" / "refs" / "hackerrank-orchestrate-october26-main" / "dataset"
)
DATASET_DIR = Path(os.environ.get("GHOST_AUDIT_DATASET", DEFAULT_DATASET))

requires_dataset = pytest.mark.skipif(
    not DATASET_DIR.is_dir(), reason=f"official dataset not available at {DATASET_DIR}"
)

KNOWN_SYSTEMS = {
    getattr(evidence_base_module, name)
    for name in dir(evidence_base_module)
    if name.startswith("SYSTEM_")
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def dataset() -> Dataset:
    return Dataset(DATASET_DIR)


@pytest.fixture(scope="module")
def documents(dataset: Dataset) -> list[EvidenceDocument]:
    try:
        return dataset.documents
    except OCRUnavailable as exc:  # pragma: no cover - Linux CI without macOS Vision
        pytest.skip(f"PNG OCR unavailable: {exc}")


@pytest.fixture(scope="module")
def parsed(documents: list[EvidenceDocument]) -> ParseResult:
    return parse_all(documents)


def _doc(file_id: str, file_path: str, text: str, captured_at: str = "2026-09-01T00:00:00Z") -> EvidenceDocument:
    suffix = Path(file_path).suffix.lower()
    kind = {".png": "png", ".pdf": "pdf", ".sql": "sql", ".json": "json", ".log": "log"}.get(
        suffix, "text"
    )
    return EvidenceDocument(
        file_id=file_id,
        file_path=file_path,
        origin="test",
        captured_at=captured_at,
        kind=kind,
        text=text,
    )


def _find(
    parsed: ParseResult,
    evidence_id: str,
    resource_id: str | None = None,
    record_type: str | None = None,
) -> list:
    return [
        observation
        for observation in parsed.observations
        if observation.evidence_id == evidence_id
        and (resource_id is None or observation.resource_id == resource_id)
        and (record_type is None or observation.record_type == record_type)
    ]


# ---------------------------------------------------------------------------
# Format tests (no dataset required)
# ---------------------------------------------------------------------------
def test_classify_every_family() -> None:
    cases = {
        "evidence/logs/service_events_ap_south_20260915.log": "log_plain_kv",
        "evidence/logs/structured_service_events_eu_west_20260918.log": "log_json_lines",
        "evidence/logs/syslog_archive_us_east_20260824.log": "log_syslog",
        "evidence/logs/application_trace_eu_north_20260302.log": "log_application_trace",
        "evidence/logs/operations_stream_eu_central_20260705.log": "log_pipe_stream",
        "evidence/logs/cloud_runtime_export_us_west_20260608.log": "log_cloud_runtime",
        "evidence/logs/search_index_diagnostic_20260906.log": "log_record_lookup",
        "evidence/logs/notification_delivery_20260315.log": "log_record_lookup",
        "evidence/logs/resource_lookup_trace_20260218_083911.log": "log_readback_trace",
        "evidence/logs/support_replica.log": "log_support_replica",
        "evidence/records/service_resource_export_20260129_114459.json": "service_export",
        "evidence/records/customer_data_snapshot_bundle_20260917.json": "snapshot_bundle",
        "evidence/records/security_audit_ledger_20261001.json": "record_manifest",
        "evidence/records/customer_export_jobs_20260914.json": "collection_records",
        "evidence/databases/customer_data_inventory_20260213.sql": "sql_inventory",
        "evidence/databases/customer_data_read_replica_20260921.sql": "sql_replica",
        "evidence/databases/customer_accounts_failover_replica_20260902.sql": "sql_replica",
        "evidence/records/customer_account_handover_20261001.txt": "handover",
        "evidence/documents/account_history_report_20260718.pdf": "pdf_account_history_report",
        "evidence/documents/legal_retention_register_20260529.pdf": "pdf_legal_retention_register",
        "evidence/documents/security_retention_extract.pdf": "pdf_security_retention_extract",
        "evidence/documents/delivery_correction_attachment.pdf": "pdf_support_attachment",
        "evidence/images/search_index_inspector_20260901.png": "image_search_index_inspector",
        "evidence/images/backup_restore_console_20260429.png": "image_backup_restore_console",
        "evidence/images/deleted_account_reference_20260310.png": "image_deleted_account_reference",
        "evidence/images/object_storage_browser_20260213.png": "image_object_storage_browser",
    }
    for path, expected in cases.items():
        assert classify_path(path) == expected, path
    assert classify_path("evidence/unknown/thing.bin") == "unknown"


def test_log_line_syntaxes_all_parse() -> None:
    """Each renderer must yield the same observation for the same fact."""

    lines = [
        '2026-09-15T08:51:57Z level=INFO service=account_service trace_id=tr-1 '
        'resource_id=account_1 event=profile_lookup_completed rows=1 http_status=200 lookup_key="a@example.com"',
        '2026-09-15T08:51:57Z {"severity":"INFO","service":"account_service","trace_id":"tr-1",'
        '"resource_id":"account_1","event":"profile_lookup_completed","rows":"1","http_status":"200","lookup_key":"a@example.com"}',
        '2026-09-15T08:51:57Z <15>1 node-1 account_service tr-1 - - level=INFO resource_id="account_1" '
        'event="profile_lookup_completed" rows="1" http_status="200" lookup_key="a@example.com"',
        '2026-09-15T08:51:57Z [INFO] [account_service] [tr-1] profile_lookup_completed: '
        'resource_id="account_1" rows="1" http_status="200" lookup_key="a@example.com"',
        '2026-09-15T08:51:57Z | INFO | account_service | tr-1 | profile_lookup_completed | '
        'resource_id="account_1" rows="1" http_status="200" lookup_key="a@example.com"',
        '2026-09-15T08:51:57Z requestId=tr-1 level=INFO function=account_service '
        'message="resource_id=\\"account_1\\" event=\\"profile_lookup_completed\\" rows=\\"1\\" '
        'http_status=\\"200\\" lookup_key=\\"a@example.com\\""',
    ]
    for index, line in enumerate(lines):
        parsed = parse_document(
            _doc(f"evidence_{index:02d}", "evidence/logs/service_events_x.log", line)
        )
        assert len(parsed.observations) == 1, (index, parsed.warnings)
        observation = parsed.observations[0]
        assert observation.system_id == "account_service"
        assert observation.resource_id == "account_1"
        assert observation.record_type == "USER_PROFILE"
        assert observation.state == "PRESENT"
        assert observation.observed_at == "2026-09-15T08:51:57Z"
        assert observation.identifiers["email"] == "a@example.com"


def test_record_lookup_line_keeps_explicit_result() -> None:
    text = (
        '2026-09-06T07:00:00Z INFO service=search_index_service event=record_lookup seq=8 '
        'system="account_service" resource_id="account_8473" record_type="USER_PROFILE" '
        'result="NOT_FOUND" subject_reference="marek@example.com"'
    )
    parsed = parse_document(_doc("evidence_08", "evidence/logs/search_index_diagnostic.log", text))
    assert len(parsed.observations) == 1
    observation = parsed.observations[0]
    assert observation.state == "ABSENT"
    assert observation.identifiers["email"] == "marek@example.com"
    assert parsed.coverage["account_service"] == {"USER_PROFILE"}


def test_trace_pairing_carries_lookup_key() -> None:
    text = "\n".join(
        [
            '2026-02-18T05:47:23Z level=DEBUG service=analytics_platform trace_id=rb-1 '
            'event=analytics_profile_read_started resource_id=analytics_1 lookup_key=e@example.com',
            '2026-02-18T05:47:23Z level=INFO service=analytics_platform trace_id=rb-1 '
            'event=analytics_profile_read_completed resource_id=analytics_1 email=e@example.com phone=null',
        ]
    )
    parsed = parse_document(
        _doc("evidence_64", "evidence/logs/resource_lookup_trace_20260218.log", text)
    )
    assert len(parsed.observations) == 1
    assert parsed.observations[0].identifiers["email"] == "e@example.com"
    assert parsed.observations[0].state == "PRESENT"


def test_handover_rows() -> None:
    text = "\n".join(
        [
            "CUSTOMER PLATFORM - SHIFT HANDOVER",
            "- 2026-10-01T12:00:00Z | account_service | account_1 | USER_PROFILE | NOT_FOUND | a@example.com",
            "- 2026-10-01T12:00:00Z | security_service | audit_record_1 | SECURITY_AUDIT_LOG | FOUND | user_01",
            "- 2026-10-01T12:00:00Z | support_platform | support_1 | SUPPORT_MESSAGE | REDACTED | no direct identifier",
        ]
    )
    parsed = parse_document(
        _doc("evidence_03", "evidence/records/customer_account_handover_20261001.txt", text)
    )
    states = {(observation.resource_id, observation.state) for observation in parsed.observations}
    assert states == {
        ("account_1", "ABSENT"),
        ("audit_record_1", "RETAINED"),
        ("support_1", "REDACTED"),
    }


def test_sql_empty_table_proves_coverage() -> None:
    text = (
        "PRAGMA foreign_keys=ON;\n"
        "BEGIN TRANSACTION;\n"
        "CREATE TABLE snapshot_scope (origin_system TEXT PRIMARY KEY, captured_at TEXT NOT NULL);\n"
        "CREATE TABLE customer_profiles (resource_id TEXT PRIMARY KEY, customer_reference TEXT, "
        "full_name TEXT, email TEXT, phone TEXT, email_hash TEXT, account_status TEXT NOT NULL, "
        "updated_at TEXT NOT NULL);\n"
        "INSERT INTO snapshot_scope VALUES ('account_service', '2026-09-05T07:18:01Z');\n"
        "COMMIT;\n"
    )
    parsed = parse_document(_doc("evidence_90", "evidence/databases/customer_data_inventory_x.sql", text))
    assert parsed.observations == []
    assert parsed.coverage["account_service"] == {"USER_PROFILE"}


def test_sql_rows_and_absence_are_not_invented() -> None:
    text = (
        "BEGIN TRANSACTION;\n"
        "CREATE TABLE customer_profiles_primary (resource_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, "
        "full_name TEXT NOT NULL, email TEXT NOT NULL, phone TEXT NOT NULL, account_status TEXT NOT NULL, "
        "updated_at TEXT NOT NULL);\n"
        "INSERT INTO \"customer_profiles_primary\" VALUES('account_1','user_01','A B',"
        "'a@example.com','+1 555 0100','ACTIVE','2026-09-02T22:45:00Z');\n"
        "COMMIT;\n"
    )
    parsed = parse_document(
        _doc("evidence_54", "evidence/databases/customer_accounts_failover_replica_x.sql", text)
    )
    assert len(parsed.observations) == 1
    observation = parsed.observations[0]
    assert observation.resource_id == "account_1"
    assert observation.state == "PRESENT"
    # Absence of unknown resources is never synthesized.
    assert {item.resource_id for item in parsed.observations} == {"account_1"}


def test_service_export_minimal() -> None:
    payload = {
        "exported_at": "2026-09-15T11:44:55Z",
        "region": "global",
        "service_exports": {
            "account_service": {
                "profile_lookups": [
                    {
                        "observed_at": "2026-09-15T09:08:49Z",
                        "profile": {"email": "a@example.com"},
                        "resource_id": "account_1",
                        "status_code": 200,
                    },
                    {
                        "observed_at": "2026-09-15T11:44:14Z",
                        "profile": None,
                        "resource_id": "account_2",
                        "status_code": 404,
                    },
                ]
            },
            "support_platform": {
                "tickets": [
                    {
                        "observed_at": "2026-09-15T09:00:00Z",
                        "resource_id": "support_1",
                        "customer_reference": "user_1",
                        "requester_email": None,
                        "requester_name": None,
                        "requester_phone": None,
                        "message_excerpt": "Customer followed up about an order.",
                    },
                    {
                        "observed_at": "2026-09-15T09:30:00Z",
                        "resource_id": "support_2",
                        "customer_reference": None,
                        "requester_email": "b@example.com",
                        "requester_name": None,
                        "requester_phone": None,
                        "message_excerpt": "Please update my address.",
                    },
                ]
            },
        },
    }
    parsed = parse_document(
        _doc("evidence_92", "evidence/records/service_resource_export_x.json", json.dumps(payload))
    )
    by_resource = {observation.resource_id: observation for observation in parsed.observations}
    assert by_resource["account_1"].state == "PRESENT"
    assert by_resource["account_2"].state == "ABSENT"
    assert by_resource["support_1"].state == "REDACTED"
    assert by_resource["support_2"].state == "PRESENT"
    assert by_resource["support_1"].observed_at == "2026-09-15T09:00:00Z"


def test_record_manifest_keeps_redacted_result() -> None:
    payload = {
        "exported_at": "2026-04-09T12:00:00Z",
        "records": [
            {
                "recorded_at": "2026-04-09T12:00:00Z",
                "system": "support_platform",
                "resource_id": "support_9",
                "record_type": "SUPPORT_MESSAGE",
                "result": "REDACTED",
                "subject_reference": "user_9",
            }
        ],
    }
    parsed = parse_document(
        _doc("evidence_36", "evidence/records/customer_export_manifest_x.json", json.dumps(payload))
    )
    assert len(parsed.observations) == 1
    assert parsed.observations[0].state == "REDACTED"


# ---------------------------------------------------------------------------
# Dataset tests
# ---------------------------------------------------------------------------
@requires_dataset
def test_all_94_documents_are_classified_and_parsed(
    documents: list[EvidenceDocument], parsed: ParseResult
) -> None:
    assert len(documents) == 94
    assert len(parsed.file_families) == 94
    assert set(parsed.file_families) == {document.file_id for document in documents}
    for evidence_id, family in parsed.file_families.items():
        assert not family.endswith("unclassified"), (evidence_id, family)
        assert family != "unknown", (evidence_id, family)
        assert parsed.coverage.get(evidence_id), f"{evidence_id} has no coverage"
    # A targeted empty snapshot renders no rows on purpose; it is still
    # classified, covered, and documented, which is the only allowed no-row file.
    without_observations = [
        evidence_id for evidence_id in parsed.file_families if not _find(parsed, evidence_id)
    ]
    assert without_observations == ["evidence_90"]
    assert {key: value for key, value in parsed.warnings.items() if value} == {}


@requires_dataset
def test_manifest_ids_and_capture_times_are_exact(
    dataset: Dataset, parsed: ParseResult
) -> None:
    manifest = {item.file_id: item for item in dataset.evidence_manifest}
    assert {observation.evidence_id for observation in parsed.observations} <= set(manifest)
    for observation in parsed.observations:
        assert observation.captured_at == manifest[observation.evidence_id].captured_at


@requires_dataset
def test_states_and_record_types_are_canonical(parsed: ParseResult) -> None:
    assert {observation.state for observation in parsed.observations} <= CANONICAL_STATES
    assert {observation.record_type for observation in parsed.observations} <= RECORD_TYPES
    assert {observation.system_id for observation in parsed.observations} <= KNOWN_SYSTEMS


@requires_dataset
def test_observation_ordering_is_deterministic(
    documents: list[EvidenceDocument], parsed: ParseResult
) -> None:
    keys = [
        (observation.evidence_id, observation.resource_id, observation.record_type, observation.observed_at or "")
        for observation in parsed.observations
    ]
    assert keys == sorted(keys)
    assert [item.to_dict() for item in parse_all(documents).observations] == [
        item.to_dict() for item in parsed.observations
    ]


@requires_dataset
def test_family_structure(parsed: ParseResult) -> None:
    summary = family_summary(parsed)
    assert sum(entry["files"] for entry in summary.values()) == 94
    assert sum(entry["observations"] for entry in summary.values()) == len(parsed.observations)
    assert len(parsed.observations) >= 2000
    expected_files = {
        "log_plain_kv": 2,
        "log_json_lines": 2,
        "log_syslog": 2,
        "log_application_trace": 2,
        "log_pipe_stream": 2,
        "log_cloud_runtime": 2,
        "log_record_lookup": 2,
        "log_readback_trace": 11,
        "log_support_replica": 1,
        "service_export": 11,
        "snapshot_bundle": 12,
        "record_manifest": 3,
        "collection_records": 2,
        "sql_inventory": 11,
        "sql_replica": 13,
        "handover": 1,
        "pdf_account_history_report": 1,
        "pdf_legal_retention_register": 1,
        "pdf_security_retention_extract": 1,
        "pdf_support_attachment": 1,
    }
    for family, expected in expected_files.items():
        assert summary[family]["files"] == expected, family
    image_files = sum(entry["files"] for family, entry in summary.items() if family.startswith("image_"))
    assert image_files == 11


@requires_dataset
def test_known_explicit_facts(parsed: ParseResult) -> None:
    cases = [
        ("evidence_51", "tombstone_9178", "ACCOUNT_TOMBSTONE", "PRESENT"),
        ("evidence_51", "tombstone_440", "ACCOUNT_TOMBSTONE", "REDACTED"),
        ("evidence_54", "account_005", "USER_PROFILE", "PRESENT"),
        ("evidence_94", "account_7566", "USER_PROFILE", "ABSENT"),
        ("evidence_94", "analytics_9406", "ANALYTICS_PROFILE", "ANONYMIZED"),
        ("evidence_58", "support_001", "SUPPORT_ATTACHMENT", "PRESENT"),
        ("evidence_55", "audit_record_003", "SECURITY_AUDIT_LOG", "RETAINED"),
        ("evidence_56", "analytics_901", "ANALYTICS_PROFILE", "ANONYMIZED"),
        ("evidence_56", "analytics_824", "ANALYTICS_PROFILE", "PRESENT"),
        ("evidence_57", "search_001", "SEARCH_INDEX_ENTRY", "PRESENT"),
        ("evidence_59", "export_001", "USER_EXPORT", "PRESENT"),
        ("evidence_61", "support_002", "SUPPORT_MESSAGE", "PRESENT"),
        ("evidence_62", "account_470", "USER_PROFILE", "ABSENT"),
        ("evidence_62", "backup_6344", "BACKUP_ARCHIVE", "BLOCKED"),
        ("evidence_62", "data_flow_6987", "DATA_FLOW", "DISABLED"),
        ("evidence_50", "account_1733", "USER_PROFILE", "PRESENT"),
        ("evidence_50", "audit_record_1414", "SECURITY_AUDIT_LOG", "RETAINED"),
        ("evidence_93", "backup_4978", "BACKUP_ARCHIVE", "ACTIVE"),
        ("evidence_03", "account_146", "USER_PROFILE", "ABSENT"),
        ("evidence_03", "data_flow_2528", "DATA_FLOW", "ACTIVE"),
        ("evidence_09", "search_428", "SEARCH_INDEX_ENTRY", "PRESENT"),
        ("evidence_04", "account_5762", "USER_PROFILE", "PRESENT"),
        ("evidence_14", "data_flow_1121", "DATA_FLOW", "ACTIVE"),
        ("evidence_19", "analytics_7438", "ANALYTICS_PROFILE", "PRESENT"),
        ("evidence_60", "marketing_001", "MARKETING_PROFILE", "PRESENT"),
        ("evidence_37", "audit_record_003", "SECURITY_AUDIT_LOG", "RETAINED"),
    ]
    for evidence_id, resource_id, record_type, state in cases:
        matches = _find(parsed, evidence_id, resource_id, record_type)
        assert matches, f"missing observation {evidence_id}/{resource_id}/{record_type}"
        assert state in {observation.state for observation in matches}, (
            evidence_id,
            resource_id,
            [(observation.state, observation.context.get("raw_state")) for observation in matches],
        )


@requires_dataset
def test_identity_identifiers_are_attached(parsed: ParseResult) -> None:
    def identifiers_of(evidence_id: str, resource_id: str) -> dict:
        matches = _find(parsed, evidence_id, resource_id)
        assert matches, (evidence_id, resource_id)
        return dict(matches[0].identifiers)

    assert identifiers_of("evidence_58", "support_001")["email"] == "priya.raman@gmail.com"
    assert identifiers_of("evidence_94", "search_4262")["email"] == "akosua.gyasi@gmail.com"
    assert identifiers_of("evidence_94", "account_7566")["email"] == "l.fontaine46@yahoo.com"
    assert identifiers_of("evidence_51", "tombstone_9178")["email"] == "addo.kofi@gmail.com"
    assert identifiers_of("evidence_03", "account_146")["email"] == "e.lindberg18@yahoo.com"


@requires_dataset
def test_absence_is_covered_by_snapshots(parsed: ParseResult) -> None:
    # Whole-table captures render the category even when no row matches.
    assert parsed.coverage["evidence_54"]["account_service"] == {"USER_PROFILE"}
    assert parsed.coverage["evidence_51"]["account_service"] == {"USER_PROFILE"}
    assert parsed.coverage["evidence_08"]["account_service"] == {"USER_PROFILE"}
    # The empty inventory capture renders eight categories and no rows.
    empty_snapshot = parsed.coverage["evidence_90"]
    assert set(empty_snapshot) == {
        "account_service",
        "analytics_platform",
        "backup_service",
        "export_service",
        "integration_service",
        "search_service",
        "security_service",
        "support_platform",
    }
    assert _find(parsed, "evidence_90") == []


@requires_dataset
def test_sql_table_summary(dataset: Dataset) -> None:
    summary = sql_table_summary(dataset.document("evidence_54"))
    assert summary["topology"] == "failover_replica"
    assert summary["captured_at"] == "2026-09-02T23:00:00Z"
    tables = summary["tables"]
    assert tables["customer_profiles_failover_replica"]["row_count"] == 4
    assert "account_005" in tables["customer_profiles_failover_replica"]["resource_ids"]
    # Replication drift: the failover copy still has account_005, the primary does not.
    assert "account_005" not in tables["customer_profiles_primary"]["resource_ids"]
    inventory = sql_table_summary(dataset.document("evidence_63"))
    assert inventory["topology"] == "inventory"
    assert inventory["captured_at"] == "2026-02-13T07:43:27Z"
    assert inventory["tables"]["snapshot_scope"]["role"] == "metadata"


@requires_dataset
def test_grid_evidence_observed_at_comes_from_inside_the_file(parsed: ParseResult) -> None:
    # Log lines carry their own timestamps.
    log_observation = _find(parsed, "evidence_94", "search_4262")[0]
    assert log_observation.observed_at == "2026-09-26T07:57:13Z"
    # Snapshot bundles keep the per-snapshot capture time, not the manifest time.
    assert _find(parsed, "evidence_02", "account_5684")[0].observed_at == "2026-09-09T12:00:00Z"
    assert _find(parsed, "evidence_02", "account_3866")[0].observed_at == "2026-09-17T12:00:00Z"
    bundle_observations = _find(parsed, "evidence_02")
    assert {observation.captured_at for observation in bundle_observations} == {
        "2026-09-17T12:00:00Z"
    }
    # SQL inventory rows use the in-file snapshot_scope timestamp.
    assert {observation.observed_at for observation in _find(parsed, "evidence_63")} == {
        "2026-02-13T07:43:27Z"
    }


@requires_dataset
def test_sample_output_findings_are_supported_by_parser(
    dataset: Dataset, parsed: ParseResult
) -> None:
    """Every citable sample step must map to a parsed observation.

    Explicit absence verification steps are allowed to rely on coverage instead
    of an emitted ABSENT row (that is the scenario layer's job), so they are
    excluded here.
    """

    by_document: dict[str, list] = {}
    for observation in parsed.observations:
        by_document.setdefault(observation.evidence_id, []).append(observation)
    expected_state = {"RETAINED": "PRESENT"}  # retained records are PRESENT in the output contract
    checked = 0
    missing = []
    sample_output = dataset.root / "sample_output.csv"
    assert sample_output.exists()
    with sample_output.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            for step in json.loads(row["steps_json"]):
                evidence_id = step.get("evidence_id") or ""
                if not evidence_id or step["operation"] == "VERIFY_RECORD_ABSENT":
                    continue
                checked += 1
                wanted = expected_state.get(step["state"], step["state"])
                candidates = [
                    observation
                    for observation in by_document.get(evidence_id, [])
                    if observation.resource_id == step["resource_id"]
                ]
                states = {expected_state.get(observation.state, observation.state) for observation in candidates}
                if wanted not in states:
                    missing.append((row["case_id"], evidence_id, step["resource_id"], step["state"]))
    assert checked >= 60
    assert not missing, f"sample steps without parser support: {missing[:10]}"
