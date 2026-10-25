"""Parser for the SQLite-compatible SQL snapshot evidence.

The supplied SQL files are point-in-time database captures.  Two layouts occur:

* dedicated-column tables (``customer_profiles``, ``customer_search_documents``,
  ``support_records``, ``connector_subscriptions``, ``backup_restore_entries``,
  ``security_audit_records``, ...), used by the inventory and failover captures;
* a generic ``customer_data_*`` table whose rows carry
  ``origin_system``/``collection_name``/``record_json`` columns, used by the
  read / regional / reporting replica captures.

The file is executed in an in-memory SQLite database, so quoting, ``NULL`` and
JSON payloads are handled by SQLite itself instead of ad-hoc string splitting.

Absence semantics: this parser never invents ABSENT rows.  It emits one
observation per rendered row and reports *coverage* (which system/category
pairs the snapshot rendered) plus per-table row counts and resource ids.  The
scenario layer combines coverage with the known resource ids to infer absence
from a whole-table capture; explicit ``NOT_FOUND``/404 rows are emitted only
when the evidence text asserts them.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Mapping

from ..dataset import EvidenceDocument
from . import base, records
from ._util import clean_optional, iso_timestamp
from .result import ParsedFile

_TOPOLOGY_RE = re.compile(r"^--\s*Database topology:\s*(\S+)", re.MULTILINE)
_CAPTURED_AT_RE = re.compile(r"^--\s*Captured at:\s*([0-9TZ:.\-]+)", re.MULTILINE)

#: Dedicated-column tables rendered by the inventory / failover captures.  The
#: value is the collection name used by the shared record mapping.
_DEDICATED_TABLES: tuple[tuple[str, str], ...] = (
    ("customer_profiles", "customer_profiles"),
    ("search_documents", "customer_search_documents"),
    ("customer_search_documents", "customer_search_documents"),
    ("customer_analytics_profiles", "customer_analytics_reports"),
    ("support_records", "support_conversations"),
    ("customer_exports", "customer_export_jobs"),
    ("connector_subscriptions", "connector_configurations"),
    ("backup_restore_entries", "backup_catalog"),
    ("security_audit_records", "security_audit_events"),
    ("deleted_customer_references", "deleted_customer_references"),
)

#: Tables that only carry snapshot metadata.
_METADATA_TABLES = {"snapshot_scope"}


def parse_sql_document(document: EvidenceDocument) -> ParsedFile:
    """Execute the snapshot and convert every rendered table row."""

    parsed = ParsedFile()
    connection = _execute_snapshot(document.text)
    if connection is None:
        parsed.warnings.append("could not execute SQL snapshot")
        return parsed
    try:
        topology = _search(_TOPOLOGY_RE, document.text) or _topology_from_path(document.file_path)
        captured_at = (
            iso_timestamp(_search(_CAPTURED_AT_RE, document.text))
            or document.captured_at
        )
        scopes = _read_snapshot_scope(connection)
        default_observed_at = captured_at if not scopes else max(scopes.values())
        table_names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
        table_summary: dict[str, dict[str, Any]] = {}
        for table_name in table_names:
            columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{table_name}")')]
            table_summary[table_name] = _parse_table(
                document=document,
                parsed=parsed,
                connection=connection,
                table_name=table_name,
                columns=columns,
                scopes=scopes,
                default_observed_at=default_observed_at,
            )
    finally:
        connection.close()
    parsed.metadata["topology"] = topology
    parsed.metadata["captured_at"] = captured_at
    parsed.metadata["snapshot_scope"] = scopes
    parsed.metadata["tables"] = table_summary
    return parsed


def sql_table_summary(document: EvidenceDocument) -> dict[str, Any]:
    """Return the SQL snapshot table metadata without building observations.

    This is the module-level helper the scenario layer can call for a specific
    evidence file: topology, the in-file capture time, and per-table row counts
    with the resource ids each table renders.
    """

    parsed = parse_sql_document(document)
    return {
        "topology": parsed.metadata.get("topology"),
        "captured_at": parsed.metadata.get("captured_at"),
        "tables": parsed.metadata.get("tables", {}),
        "warnings": list(parsed.warnings),
    }


# ---------------------------------------------------------------------------
# SQL execution helpers
# ---------------------------------------------------------------------------
def _execute_snapshot(text: str) -> sqlite3.Connection | None:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(text)
        return connection
    except sqlite3.Error:
        connection.close()
    # Some exports wrap the statements in an explicit transaction that SQLite
    # rejects when replayed; retry without the transaction markers.
    cleaned = "\n".join(
        line
        for line in text.splitlines()
        if not re.match(r"^\s*(BEGIN TRANSACTION|COMMIT)\s*;?\s*$", line, re.IGNORECASE)
    )
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(cleaned)
        return connection
    except sqlite3.Error:
        connection.close()
        return None


def _search(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    return clean_optional(match.group(1)) if match else None


#: File-name hints for captures whose SQL header does not state the topology.
_TOPOLOGY_BY_PATH: tuple[tuple[str, str], ...] = (
    ("customer_accounts_failover_replica", "account_failover_replica"),
    ("customer_data_read_replica", "read_replica"),
    ("customer_data_regional_replicas", "regional_replicas"),
    ("customer_data_reporting_replica", "reporting_replica"),
    ("customer_data_failover_replica", "failover_replica"),
    ("customer_data_inventory", "inventory"),
    ("customer_search_primary", "primary_only"),
)


def _topology_from_path(file_path: str) -> str | None:
    for marker, topology in _TOPOLOGY_BY_PATH:
        if marker in file_path:
            return topology
    return None


def _read_snapshot_scope(connection: sqlite3.Connection) -> dict[str, str]:
    try:
        rows = connection.execute("SELECT origin_system, captured_at FROM snapshot_scope").fetchall()
    except sqlite3.Error:
        return {}
    scopes: dict[str, str] = {}
    for origin_system, captured_at in rows:
        value = iso_timestamp(captured_at)
        if origin_system and value:
            scopes[str(origin_system)] = value
    return scopes


# ---------------------------------------------------------------------------
# Per-table conversion
# ---------------------------------------------------------------------------
def _parse_table(
    *,
    document: EvidenceDocument,
    parsed: ParsedFile,
    connection: sqlite3.Connection,
    table_name: str,
    columns: list[str],
    scopes: Mapping[str, str],
    default_observed_at: str,
) -> dict[str, Any]:
    rows = [
        dict(zip(columns, values))
        for values in connection.execute(f'SELECT * FROM "{table_name}"')
    ]
    resource_ids = sorted(
        {
            str(row["resource_id"])
            for row in rows
            if isinstance(row, Mapping) and row.get("resource_id") is not None
        }
    )
    summary: dict[str, Any] = {
        "row_count": len(rows),
        "columns": columns,
        "resource_ids": resource_ids,
        "systems": {},
    }
    if table_name in _METADATA_TABLES:
        summary["role"] = "metadata"
        return summary
    if "record_json" in columns:
        summary["layout"] = "record_json"
        start = len(parsed.observations)
        for row in rows:
            _parse_record_json_row(
                document=document,
                parsed=parsed,
                table_name=table_name,
                row=row,
                scopes=scopes,
                default_observed_at=default_observed_at,
            )
        summary["systems"] = _summarize_coverage(parsed.observations[start:])
        return summary
    collection = _dedicated_collection(table_name)
    if collection is None:
        parsed.warnings.append(f"unmapped SQL table {table_name!r}")
        summary["layout"] = "unmapped"
        return summary
    summary["layout"] = "dedicated_columns"
    summary["collection"] = collection
    system_id = records.COLLECTION_TO_SYSTEM[collection]
    record_type = records.COLLECTION_TO_RECORD_TYPE[collection]
    observed_at = scopes.get(system_id, default_observed_at)
    for row in rows:
        row["_collection"] = collection
        parsed.add(
            records.observation_from_collection_record(
                evidence_id=document.file_id,
                captured_at=document.captured_at,
                data_store=system_id,
                collection=collection,
                record=row,
                observed_at=observed_at,
                warnings=parsed.warnings,
                extra_context={"table": table_name},
            )
        )
    parsed.cover(system_id, record_type)
    summary["systems"] = {system_id: [record_type]}
    return summary


def _dedicated_collection(table_name: str) -> str | None:
    lowered = table_name.lower()
    for prefix, collection in _DEDICATED_TABLES:
        if lowered.startswith(prefix):
            return collection
    return None


def _parse_record_json_row(
    *,
    document: EvidenceDocument,
    parsed: ParsedFile,
    table_name: str,
    row: Mapping[str, Any],
    scopes: Mapping[str, str],
    default_observed_at: str,
) -> None:
    origin_system = clean_optional(row.get("origin_system"))
    collection = clean_optional(row.get("collection_name"))
    payload = clean_optional(row.get("record_json"))
    if payload is None or collection is None:
        parsed.warnings.append("record_json row without collection or payload")
        return
    try:
        record = json.loads(payload)
    except json.JSONDecodeError:
        parsed.warnings.append("record_json row is not valid JSON")
        return
    if not isinstance(record, Mapping):
        parsed.warnings.append("record_json row is not a JSON object")
        return
    observed_at = scopes.get(origin_system or "", default_observed_at)
    parsed.add(
        records.observation_from_collection_record(
            evidence_id=document.file_id,
            captured_at=document.captured_at,
            data_store=origin_system,
            collection=collection,
            record=record,
            observed_at=observed_at,
            warnings=parsed.warnings,
            extra_context={"updated_at": clean_optional(row.get("updated_at")), "table": table_name},
        )
    )


def _summarize_coverage(observations: list[base.Observation]) -> dict[str, list[str]]:
    """Describe which system/category pairs one table rendered (for metadata)."""

    summary: dict[str, list[str]] = {}
    for observation in observations:
        types = summary.setdefault(observation.system_id, [])
        if observation.record_type not in types:
            types.append(observation.record_type)
    return summary
