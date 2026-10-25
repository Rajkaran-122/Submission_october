"""Evidence registry: classify every evidence file and parse it.

The manifest (``evidence_files.csv``) does not say which format a file uses, so
the registry derives the family from the file path and the extracted text, then
routes the document to the family parser:

======================  ===================================================
family                  files
======================  ===================================================
log_plain_kv            ``service_events_*.log``
log_json_lines          ``structured_service_events_*.log``
log_syslog              ``syslog_archive_*.log``
log_application_trace   ``application_trace_*.log``
log_pipe_stream         ``operations_stream_*.log``
log_cloud_runtime       ``cloud_runtime_export_*.log``
log_record_lookup       ``search_index_diagnostic_*.log``,
                        ``notification_delivery_*.log``
log_readback_trace      ``resource_lookup_trace_*.log``
log_support_replica     ``support_replica.log``
json_service_export     ``service_resource_export_*.json``
json_snapshot_bundle    ``customer_data_snapshot_bundle_*.json``
json_record_manifest    ``*_manifest_*.json``,
                        ``connector_configurations_*.json``,
                        ``security_audit_ledger_*.json``
json_collection_records ``customer_analytics_report_*.json``,
                        ``customer_export_jobs_*.json``
sql_replica             replica / primary SQL snapshots
sql_inventory           ``customer_data_inventory_*.sql``
text_handover           ``customer_account_handover_*.txt``
pdf_*                   the four PDF documents
image_*                 the eleven screenshot consoles
======================  ===================================================

Every supplied document is parsed: there is no narrative-only evidence in this
dataset, and a file that yields neither observations nor coverage is reported
in :attr:`ParseResult.warnings` instead of being skipped silently.

The public entry point is :func:`parse_all`, which returns a
:class:`ParseResult` with the observations, per-file coverage, file families
and parse warnings:

.. code-block:: python

    result = parse_all(dataset.documents)
    result.observations        # list[Observation]
    result.coverage            # {evidence_id: {system_id: {record_type, ...}}}
    result.file_families       # {evidence_id: family}
    result.warnings            # {evidence_id: [message, ...]}
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from ..dataset import EvidenceDocument
from . import base, documents, handover, json_docs, logs, sql_dump
from .result import ParsedFile

#: Family used when no path/suffix rule matches.
FAMILY_UNKNOWN = "unknown"

#: Path marker -> (family, parser name).  Ordered from most specific to least.
_PATH_FAMILIES: tuple[tuple[str, str], ...] = (
    # documents (PDF / screenshots)
    ("account_history_report", "pdf_account_history_report"),
    ("legal_retention_register", "pdf_legal_retention_register"),
    ("security_retention_extract", "pdf_security_retention_extract"),
    ("delivery_correction_attachment", "pdf_support_attachment"),
    ("account_admin_console", "image_account_admin_console"),
    ("search_index_inspector", "image_search_index_inspector"),
    ("connector_management_console", "image_connector_management_console"),
    ("analytics_profile_view", "image_analytics_profile_view"),
    ("customer_export_console", "image_customer_export_console"),
    ("backup_restore_console", "image_backup_restore_console"),
    ("deleted_account_reference", "image_deleted_account_reference"),
    ("object_storage_browser", "image_object_storage_browser"),
    ("security_audit_explorer", "image_security_audit_explorer"),
    ("support_ticket_console", "image_support_ticket_console"),
    ("marketing_profile_console", "image_marketing_profile_console"),
    # JSON families
    ("service_resource_export", "service_export"),
    ("customer_data_snapshot_bundle", "snapshot_bundle"),
    ("connector_configurations", "record_manifest"),
    ("customer_export_manifest", "record_manifest"),
    ("security_audit_ledger", "record_manifest"),
    ("customer_analytics_report", "collection_records"),
    ("customer_export_jobs", "collection_records"),
    # SQL families
    ("customer_data_inventory", "sql_inventory"),
    ("customer_data_read_replica", "sql_replica"),
    ("customer_data_regional_replicas", "sql_replica"),
    ("customer_data_reporting_replica", "sql_replica"),
    ("customer_data_failover_replica", "sql_replica"),
    ("customer_accounts_failover_replica", "sql_replica"),
    ("customer_search_primary", "sql_replica"),
    # text handover
    ("customer_account_handover", "handover"),
    # log families
    ("search_index_diagnostic", "log_record_lookup"),
    ("notification_delivery", "log_record_lookup"),
    ("resource_lookup_trace", "log_readback_trace"),
    ("support_replica", "log_support_replica"),
    ("structured_service_events", "log_json_lines"),
    ("syslog_archive", "log_syslog"),
    ("application_trace", "log_application_trace"),
    ("operations_stream", "log_pipe_stream"),
    ("cloud_runtime_export", "log_cloud_runtime"),
    ("service_events", "log_plain_kv"),
)

_JSON_FAMILIES = {
    "service_export",
    "snapshot_bundle",
    "record_manifest",
    "collection_records",
}
_LOG_FAMILIES = {
    "log_plain_kv",
    "log_json_lines",
    "log_syslog",
    "log_application_trace",
    "log_pipe_stream",
    "log_cloud_runtime",
    "log_record_lookup",
    "log_readback_trace",
    "log_support_replica",
}
_SQL_FAMILIES = {"sql_replica", "sql_inventory"}
_DOCUMENT_FAMILIES = {
    "pdf_account_history_report",
    "pdf_legal_retention_register",
    "pdf_security_retention_extract",
    "pdf_support_attachment",
    "image_account_admin_console",
    "image_search_index_inspector",
    "image_connector_management_console",
    "image_analytics_profile_view",
    "image_customer_export_console",
    "image_backup_restore_console",
    "image_deleted_account_reference",
    "image_object_storage_browser",
    "image_security_audit_explorer",
    "image_support_ticket_console",
    "image_marketing_profile_console",
}


@dataclass
class ParseResult:
    """Result of parsing every document in the dataset."""

    observations: list[base.Observation] = field(default_factory=list)
    #: ``{evidence_id: {system_id: {record_type, ...}}}`` - categories the file
    #: rendered, even when it rendered no rows (an empty table proves absence).
    coverage: dict[str, dict[str, set[str]]] = field(default_factory=dict)
    file_families: dict[str, str] = field(default_factory=dict)
    warnings: dict[str, list[str]] = field(default_factory=dict)
    #: Family specific metadata (SQL topology/tables, JSON collection counts).
    metadata: dict[str, dict] = field(default_factory=dict)

    def __iter__(self):
        return iter(self.observations)

    def __len__(self) -> int:
        return len(self.observations)

    def as_dict(self) -> dict:
        """JSON-serializable view, used for the development observation dump."""

        return {
            "observations": [observation.to_dict() for observation in self.observations],
            "coverage": {
                evidence_id: {
                    system_id: sorted(record_types)
                    for system_id, record_types in sorted(by_system.items())
                }
                for evidence_id, by_system in sorted(self.coverage.items())
            },
            "file_families": dict(sorted(self.file_families.items())),
            "warnings": {key: list(value) for key, value in sorted(self.warnings.items())},
            "metadata": dict(sorted(self.metadata.items())),
        }


def classify_document(document: EvidenceDocument) -> str:
    """Return the format family of one evidence file."""

    return classify_path(document.file_path, document.kind)


def classify_path(file_path: str, kind: str | None = None) -> str:
    """Classify a file path; used by the registry and by tests."""

    name = Path(file_path).name
    for marker, family in _PATH_FAMILIES:
        if marker in name:
            return family
    suffix = Path(file_path).suffix.lower()
    if suffix in {".pdf", ".png"}:
        return f"{suffix[1:]}_unclassified"
    if suffix == ".json":
        return "json_unclassified"
    if suffix == ".sql":
        return "sql_unclassified"
    if suffix == ".log":
        return "log_unclassified"
    if suffix == ".txt":
        return "text_unclassified"
    return FAMILY_UNKNOWN


def parse_document(document: EvidenceDocument) -> ParsedFile:
    """Parse one evidence document according to its family."""

    family = classify_document(document)
    if family in _LOG_FAMILIES:
        return logs.parse_log_document(document)
    if family in _SQL_FAMILIES:
        return sql_dump.parse_sql_document(document)
    if family in _JSON_FAMILIES:
        return json_docs.parse_json_document(document)
    if family == "handover":
        return handover.parse_handover_document(document)
    if family in _DOCUMENT_FAMILIES:
        return documents.parse_document_text(document)
    parsed = ParsedFile()
    parsed.warnings.append(f"no parser for evidence family {family!r}")
    return parsed


def parse_all(documents: Iterable[EvidenceDocument]) -> ParseResult:
    """Parse every evidence document into a :class:`ParseResult`.

    Observations are ordered deterministically by evidence file id, resource id,
    record type and observation time.
    """

    result = ParseResult()
    for document in sorted(documents, key=lambda item: item.file_id):
        family = classify_document(document)
        result.file_families[document.file_id] = family
        try:
            parsed = parse_document(document)
        except Exception as exc:  # pragma: no cover - defensive, reported below
            parsed = ParsedFile()
            parsed.warnings.append(f"parser crashed: {exc!r}")
        result.observations.extend(parsed.observations)
        result.coverage[document.file_id] = {
            system_id: set(record_types) for system_id, record_types in parsed.coverage.items()
        }
        result.warnings[document.file_id] = list(parsed.warnings)
        if parsed.metadata:
            result.metadata[document.file_id] = parsed.metadata
        if not parsed.observations and not parsed.coverage and not parsed.warnings:
            result.warnings[document.file_id].append(
                "document yielded neither observations nor coverage"
            )
    result.observations.sort(
        key=lambda observation: (
            observation.evidence_id,
            observation.resource_id,
            observation.record_type,
            observation.observed_at or "",
        )
    )
    return result


def family_summary(result: ParseResult) -> dict[str, dict[str, int]]:
    """Aggregate files/observations per family (used by tests and reports)."""

    summary: dict[str, dict[str, int]] = {}
    for evidence_id, family in result.file_families.items():
        entry = summary.setdefault(family, {"files": 0, "observations": 0, "warnings": 0})
        entry["files"] += 1
        entry["warnings"] += len(result.warnings.get(evidence_id, ()))
    for observation in result.observations:
        family = result.file_families.get(observation.evidence_id, FAMILY_UNKNOWN)
        summary.setdefault(family, {"files": 0, "observations": 0, "warnings": 0})
        summary[family]["observations"] += 1
    return dict(sorted(summary.items()))


__all__ = [
    "ParseResult",
    "classify_document",
    "classify_path",
    "parse_document",
    "parse_all",
    "family_summary",
]
