"""Parsers for the JSON evidence family.

Five JSON layouts are supplied:

* ``service_resource_export_*.json`` - per-service snapshots of profiles,
  connectors, backups, exports, search queries, audit records and tickets;
* ``customer_data_snapshot_bundle_*.json`` - named data stores and collections;
* ``*_manifest_*.json`` / ``connector_configurations_*.json`` /
  ``security_audit_ledger_*.json`` - flat ``records[]`` of explicit state
  lookups;
* single-collection captures (``customer_analytics_report_*.json``,
  ``customer_export_jobs_*.json``) that mirror one bundle snapshot.

All of them render per-record state with an in-file timestamp
(``observed_at``/``recorded_at``/snapshot ``captured_at``/``exported_at``).
That timestamp becomes ``Observation.observed_at``; the manifest capture time
stays in ``Observation.captured_at`` so the scenario layer can apply the
``observed_at <= audit_at`` cutoff per fact.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from ..dataset import EvidenceDocument
from . import base, records
from ._util import clean_optional, identifiers_from_mapping, identifiers_from_text, iso_timestamp
from .result import ParsedFile


def parse_json_document(document: EvidenceDocument) -> ParsedFile:
    """Parse one JSON evidence file into observations."""

    parsed = ParsedFile()
    try:
        payload = json.loads(document.text)
    except json.JSONDecodeError as exc:
        parsed.warnings.append(f"invalid JSON: {exc}")
        return parsed
    if not isinstance(payload, Mapping):
        parsed.warnings.append("JSON root is not an object")
        return parsed

    if "snapshots" in payload:
        _parse_snapshot_bundle(document, payload, parsed)
    elif "service_exports" in payload:
        _parse_service_exports(document, payload, parsed)
    elif "records" in payload and "collection" in payload:
        _parse_single_collection(document, payload, parsed)
    elif "records" in payload:
        _parse_record_manifest(document, payload, parsed)
    else:
        parsed.warnings.append(f"unrecognized JSON layout: {sorted(payload)}")
    if not parsed.observations and not parsed.warnings:
        parsed.warnings.append("no observations derived from JSON document")
    return parsed


# ---------------------------------------------------------------------------
# Snapshot bundles
# ---------------------------------------------------------------------------
def _parse_snapshot_bundle(
    document: EvidenceDocument,
    payload: Mapping[str, Any],
    parsed: ParsedFile,
) -> None:
    exported_at = iso_timestamp(payload.get("exported_at")) or document.captured_at
    collections: dict[str, int] = {}
    for snapshot in payload.get("snapshots", []):
        if not isinstance(snapshot, Mapping):
            continue
        data_store = clean_optional(snapshot.get("data_store"))
        collection = clean_optional(snapshot.get("collection"))
        if collection is None:
            parsed.warnings.append("snapshot without collection")
            continue
        observed_at = iso_timestamp(snapshot.get("captured_at")) or exported_at
        rows = snapshot.get("records") or []
        collections[f"{data_store}.{collection}"] = len(rows)
        for raw_record in rows:
            if not isinstance(raw_record, Mapping):
                continue
            parsed.add(
                records.observation_from_collection_record(
                    evidence_id=document.file_id,
                    captured_at=document.captured_at,
                    data_store=data_store,
                    collection=collection,
                    record=raw_record,
                    observed_at=observed_at,
                    warnings=parsed.warnings,
                )
            )
        # An empty collection still proves the snapshot scanned that category.
        system_id = records.COLLECTION_TO_SYSTEM.get(collection)
        record_type = records.COLLECTION_TO_RECORD_TYPE.get(collection)
        if system_id and record_type and not rows:
            parsed.cover(system_id, record_type)
    parsed.metadata["collections"] = collections
    parsed.metadata["exported_at"] = exported_at


# ---------------------------------------------------------------------------
# Single-collection captures
# ---------------------------------------------------------------------------
def _parse_single_collection(
    document: EvidenceDocument,
    payload: Mapping[str, Any],
    parsed: ParsedFile,
) -> None:
    collection = str(payload.get("collection"))
    observed_at = (
        iso_timestamp(payload.get("captured_at"))
        or iso_timestamp(payload.get("exported_at"))
        or document.captured_at
    )
    rows = payload.get("records") or []
    for raw_record in rows:
        if not isinstance(raw_record, Mapping):
            continue
        parsed.add(
            records.observation_from_collection_record(
                evidence_id=document.file_id,
                captured_at=document.captured_at,
                data_store=clean_optional(payload.get("source")),
                collection=collection,
                record=raw_record,
                observed_at=observed_at,
                warnings=parsed.warnings,
            )
        )
    system_id = records.COLLECTION_TO_SYSTEM.get(collection)
    record_type = records.COLLECTION_TO_RECORD_TYPE.get(collection)
    if system_id and record_type:
        parsed.cover(system_id, record_type)
    parsed.metadata["collections"] = {collection: len(rows)}


# ---------------------------------------------------------------------------
# Flat ``records[]`` manifests
# ---------------------------------------------------------------------------
def _parse_record_manifest(
    document: EvidenceDocument,
    payload: Mapping[str, Any],
    parsed: ParsedFile,
) -> None:
    exported_at = iso_timestamp(payload.get("exported_at")) or document.captured_at
    record_types: set[str] = set()
    for raw_record in payload.get("records", []):
        if not isinstance(raw_record, Mapping):
            continue
        record_type = clean_optional(raw_record.get("record_type"))
        if record_type:
            record_types.add(record_type)
        parsed.add(
            _record_lookup(
                document=document,
                record=raw_record,
                default_observed_at=exported_at,
                warnings=parsed.warnings,
            )
        )
    parsed.metadata["record_types"] = sorted(record_types)
    parsed.metadata["record_count"] = len(payload.get("records") or [])


def _record_lookup(
    *,
    document: EvidenceDocument,
    record: Mapping[str, Any],
    default_observed_at: str,
    warnings: list[str],
) -> base.Observation | None:
    system_id = clean_optional(record.get("system"))
    resource_id = clean_optional(record.get("resource_id"))
    record_type = clean_optional(record.get("record_type"))
    result = clean_optional(record.get("result"))
    if not (system_id and resource_id and record_type and result):
        warnings.append("incomplete records[] entry")
        return None
    observed_at = iso_timestamp(record.get("recorded_at")) or default_observed_at
    return records.observation_from_lookup(
        evidence_id=document.file_id,
        captured_at=document.captured_at,
        system_id=system_id,
        resource_id=resource_id,
        record_type=record_type,
        result=result,
        subject_reference=clean_optional(record.get("subject_reference")),
        observed_at=observed_at,
        context={"source": "records_manifest"},
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Service exports
# ---------------------------------------------------------------------------
def _parse_service_exports(
    document: EvidenceDocument,
    payload: Mapping[str, Any],
    parsed: ParsedFile,
) -> None:
    exported_at = iso_timestamp(payload.get("exported_at")) or document.captured_at
    region = clean_optional(payload.get("region"))
    counts: dict[str, int] = {}
    service_exports = payload.get("service_exports") or {}
    for system_id, sections in service_exports.items():
        if not isinstance(sections, Mapping):
            continue
        for section, rows in sections.items():
            handler = _SERVICE_SECTION_HANDLERS.get((system_id, section))
            row_list = rows or []
            counts[f"{system_id}.{section}"] = len(row_list)
            if handler is None:
                if row_list:
                    parsed.warnings.append(f"unmapped service section {system_id}.{section}")
                continue
            for row in row_list:
                if not isinstance(row, Mapping):
                    continue
                parsed.add(
                    handler(
                        document=document,
                        row=row,
                        default_observed_at=exported_at,
                        region=region,
                    )
                )
            record_type = _SERVICE_SECTION_RECORD_TYPES.get((system_id, section))
            if record_type:
                parsed.cover(system_id, record_type)
    parsed.metadata["sections"] = counts
    parsed.metadata["region"] = region
    parsed.metadata["exported_at"] = exported_at


def _service_make(
    *,
    document: EvidenceDocument,
    row: Mapping[str, Any],
    default_observed_at: str,
    region: str | None,
    system_id: str,
    record_type: str,
    raw_state: str,
    identifiers: Mapping[str, str] | None = None,
    context: Mapping[str, Any] | None = None,
) -> base.Observation | None:
    resource_id = clean_optional(row.get("resource_id"))
    if resource_id is None:
        return None
    merged_identifiers = identifiers_from_mapping(row)
    for key, value in (identifiers or {}).items():
        if clean_optional(value):
            merged_identifiers.setdefault(key, str(value))
    observed_at = iso_timestamp(row.get("observed_at")) or default_observed_at
    merged_context: dict[str, Any] = {"source": "service_resource_export"}
    if region:
        merged_context["region"] = region
    if context:
        merged_context.update(context)
    return base.make_observation(
        evidence_id=document.file_id,
        captured_at=document.captured_at,
        system_id=system_id,
        resource_id=resource_id,
        record_type=record_type,
        raw_state=raw_state,
        observed_at=observed_at,
        identifiers=merged_identifiers,
        context=merged_context,
    )


def _handle_profile_lookup(**kwargs: Any) -> base.Observation | None:
    row = kwargs["row"]
    status_code = clean_optional(row.get("status_code"))
    profile = row.get("profile")
    if status_code == "404" or profile is None:
        state = "NOT_FOUND"
        identifiers: dict[str, str] = {}
    else:
        state = "FOUND"
        identifiers = identifiers_from_mapping(profile)
    return _service_make(
        **kwargs,
        system_id=base.SYSTEM_ACCOUNT,
        record_type=base.RT_USER_PROFILE,
        raw_state=state,
        identifiers=identifiers,
        context={"status_code": status_code} if status_code else None,
    )


def _handle_analytics_profile(**kwargs: Any) -> base.Observation | None:
    row = kwargs["row"]
    identifiers = identifiers_from_mapping(row)
    state = "IDENTIFIABLE" if identifiers else "ANONYMIZED"
    return _service_make(
        **kwargs,
        system_id=base.SYSTEM_ANALYTICS,
        record_type=base.RT_ANALYTICS_PROFILE,
        raw_state=state,
        identifiers=identifiers,
    )


def _handle_restore_entry(**kwargs: Any) -> base.Observation | None:
    row = kwargs["row"]
    state = "ACTIVE" if row.get("restore_allowed") else "BLOCKED"
    return _service_make(
        **kwargs,
        system_id=base.SYSTEM_BACKUP,
        record_type=base.RT_BACKUP_ARCHIVE,
        raw_state=state,
    )


def _handle_export(**kwargs: Any) -> base.Observation | None:
    return _service_make(
        **kwargs,
        system_id=base.SYSTEM_EXPORT,
        record_type=base.RT_USER_EXPORT,
        raw_state="FOUND",
    )


def _handle_connector(**kwargs: Any) -> base.Observation | None:
    row = kwargs["row"]
    state = "ACTIVE" if row.get("enabled") else "DISABLED"
    return _service_make(
        **kwargs,
        system_id=base.SYSTEM_INTEGRATION,
        record_type=base.RT_DATA_FLOW,
        raw_state=state,
    )


def _handle_index_query(**kwargs: Any) -> base.Observation | None:
    row = kwargs["row"]
    hits = row.get("hits") or {}
    documents = hits.get("documents") or []
    total = hits.get("total")
    total_value = clean_optional(total)
    if total_value is not None:
        state = "FOUND" if total_value != "0" else "NOT_FOUND"
    else:
        state = "FOUND" if documents else "NOT_FOUND"
    identifiers: dict[str, str] = {}
    query = clean_optional(row.get("query"))
    if query:
        identifiers.setdefault("email", query) if "@" in query else identifiers.setdefault("customer_reference", query)
    for document in documents:
        if isinstance(document, Mapping):
            for key, value in identifiers_from_mapping(document).items():
                identifiers.setdefault(key, value)
    return _service_make(
        **kwargs,
        system_id=base.SYSTEM_SEARCH,
        record_type=base.RT_SEARCH_INDEX_ENTRY,
        raw_state=state,
        identifiers=identifiers,
        context={"total_hits": total_value},
    )


def _handle_audit_record(**kwargs: Any) -> base.Observation | None:
    return _service_make(
        **kwargs,
        system_id=base.SYSTEM_SECURITY,
        record_type=base.RT_SECURITY_AUDIT_LOG,
        raw_state="RETAINED",
    )


def _handle_support_ticket(**kwargs: Any) -> base.Observation | None:
    row = kwargs["row"]
    identifiers = identifiers_from_mapping(row)
    message = str(row.get("message_excerpt") or "")
    message_identifiers = identifiers_from_text(message)
    for key, value in message_identifiers.items():
        identifiers.setdefault(key, value)
    # A stable subject reference alone is allowed; only direct identifiers
    # (requester name/email/phone or contact details in the message) expose PII.
    exposes_pii = bool(records.direct_identifiers(row)) or bool(message_identifiers)
    if "[REDACTED]" in message.upper() or not exposes_pii:
        state = "REDACTED"
    else:
        state = "IDENTIFIABLE"
    return _service_make(
        **kwargs,
        system_id=base.SYSTEM_SUPPORT,
        record_type=base.RT_SUPPORT_MESSAGE,
        raw_state=state,
        identifiers=identifiers,
    )


_SERVICE_SECTION_HANDLERS = {
    ("account_service", "profile_lookups"): _handle_profile_lookup,
    ("analytics_platform", "profiles"): _handle_analytics_profile,
    ("backup_service", "restore_entries"): _handle_restore_entry,
    ("export_service", "exports"): _handle_export,
    ("integration_service", "connectors"): _handle_connector,
    ("search_service", "index_queries"): _handle_index_query,
    ("security_service", "audit_records"): _handle_audit_record,
    ("support_platform", "tickets"): _handle_support_ticket,
}

_SERVICE_SECTION_RECORD_TYPES = {
    ("account_service", "profile_lookups"): base.RT_USER_PROFILE,
    ("analytics_platform", "profiles"): base.RT_ANALYTICS_PROFILE,
    ("backup_service", "restore_entries"): base.RT_BACKUP_ARCHIVE,
    ("export_service", "exports"): base.RT_USER_EXPORT,
    ("integration_service", "connectors"): base.RT_DATA_FLOW,
    ("search_service", "index_queries"): base.RT_SEARCH_INDEX_ENTRY,
    ("security_service", "audit_records"): base.RT_SECURITY_AUDIT_LOG,
    ("support_platform", "tickets"): base.RT_SUPPORT_MESSAGE,
}
