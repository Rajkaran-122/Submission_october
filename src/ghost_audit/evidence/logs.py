"""Parsers for the log evidence family.

The supplied log files use five different line syntaxes, all carrying the same
kind of facts:

* plain ``key=value`` lines (``service_events_*.log``, readback traces);
* explicit ``event=record_lookup`` lines that assert
  ``system/resource/type/result``;
* JSON-lines (``structured_service_events_*.log``);
* syslog with a positional ``service trace - -`` prefix;
* application traces (``[LEVEL] [service] [trace] event: key=value``);
* pipe-separated operations streams (``LEVEL | service | trace | event | ...``);
* cloud runtime exports whose facts are nested inside a ``message="..."``
  wrapper.

Every renderer ultimately produces one of the same query/completion event
names, so the log parser normalizes the line to a field mapping first and then
maps the event to an :class:`Observation`.  ``*_started`` lines carry the
subject lookup key; they are paired with their terminal line through the trace
id so the observation keeps the identifiers of the query.  Operational noise
(export markers, retries, heartbeats, queue metrics) yields no observation.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

from . import base
from ..dataset import EvidenceDocument
from ._util import add_identifier, clean_optional, parse_kv_pairs, truthy
from .result import ParsedFile

_TS_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(?:\.\d+)?Z\s+(.*)$"
)
_APPLICATION_TRACE_RE = re.compile(
    r"^\[([A-Za-z]+)\]\s*\[([^\]]*)\]\s*\[([^\]]*)\]\s+([A-Za-z0-9_]+):\s*(.*)$"
)
_SYSLOG_RE = re.compile(r"^<\d+>\d+\s+\S+\s+(\S+)\s+(\S+)\s+-\s+-\s+(.*)$")

#: Fields that may carry the subject's identifiers on a log line.
_IDENTIFIER_FIELDS = (
    "lookup_key",
    "match_key",
    "subject_key",
    "scan_key",
    "owner_key",
    "correlation_key",
    "email",
    "phone",
    "email_hash",
    "customer_email",
    "owner_email",
    "owner_phone",
    "requester_email",
    "requester_name",
    "requester_phone",
    "customer_reference",
    "subject_reference",
    "subject_ref",
    "lookup_value",
    "display_name",
    "full_name",
    "matched_value",
)


def parse_log_document(document: EvidenceDocument) -> ParsedFile:
    """Parse one log evidence file into observations."""

    parsed = ParsedFile()
    started: dict[str, dict[str, str]] = {}
    for line in document.text.splitlines():
        extracted = _parse_log_line(line)
        if extracted is None:
            continue
        timestamp, fields = extracted
        event = clean_optional(fields.get("event"))
        if event is None:
            # record_lookup lines may not have an ``event`` field in some
            # renderers; the presence of record_type/result is enough.
            if fields.get("record_type") and fields.get("result"):
                event = "record_lookup"
            else:
                continue
        trace_id = clean_optional(fields.get("trace_id"))
        if event.endswith("_started"):
            if trace_id:
                started[trace_id] = fields
            continue
        merged: dict[str, str] = dict(started.get(trace_id, {})) if trace_id else {}
        merged.update(fields)
        if event == "record_lookup":
            parsed.add(_record_lookup_observation(document, timestamp, merged, parsed.warnings))
            continue
        observation = _event_observation(document, timestamp, event, merged)
        parsed.add(observation)
    if not parsed.observations:
        parsed.warnings.append("no observations derived from log lines")
    return parsed


def _parse_log_line(line: str) -> tuple[str, dict[str, str]] | None:
    """Split one physical log line into ``(timestamp, field mapping)``."""

    match = _TS_LINE_RE.match(line.strip())
    if not match:
        return None
    timestamp = f"{match.group(1)}T{match.group(2)}Z"
    rest = match.group(3).strip()
    fields: dict[str, str] = {}
    if rest.startswith("{"):
        try:
            payload = json.loads(rest)
        except json.JSONDecodeError:
            return None
        fields = {str(key): "" if value is None else str(value) for key, value in payload.items()}
    elif rest.startswith("["):
        trace_match = _APPLICATION_TRACE_RE.match(rest)
        if trace_match:
            fields = {
                "level": trace_match.group(1),
                "service": trace_match.group(2),
                "trace_id": trace_match.group(3),
                "event": trace_match.group(4),
            }
            fields.update(parse_kv_pairs(trace_match.group(5)))
        else:
            fields = parse_kv_pairs(rest)
    elif " | " in rest:
        fields = _parse_pipe_line(rest) or parse_kv_pairs(rest)
    else:
        syslog_match = _SYSLOG_RE.match(rest)
        if syslog_match:
            fields = {
                "service": syslog_match.group(1),
                "trace_id": syslog_match.group(2),
                "event": "",
            }
            fields.update(parse_kv_pairs(syslog_match.group(3)))
        else:
            fields = parse_kv_pairs(rest)
    if "message" in fields and "event" not in fields:
        fields = _unwrap_message(fields)
    if "requestId" in fields:
        fields.setdefault("trace_id", fields["requestId"])
    if "function" in fields:
        fields.setdefault("service", fields.pop("function"))
    return timestamp, fields


def _parse_pipe_line(rest: str) -> dict[str, str] | None:
    parts = [part.strip() for part in rest.split("|")]
    while parts and not parts[0]:
        parts.pop(0)
    if len(parts) < 5:
        return None
    fields = {
        "level": parts[0],
        "service": parts[1],
        "trace_id": parts[2],
        "event": parts[3],
    }
    fields.update(parse_kv_pairs(" ".join(parts[4:])))
    # The pipe renderer prefixes the payload with a backtick in some exports.
    fields["event"] = fields["event"].strip("`")
    return fields


def _unwrap_message(fields: Mapping[str, str]) -> dict[str, str]:
    """Unwrap the escaped ``message="..."`` payload of cloud runtime exports."""

    outer = dict(fields)
    inner = parse_kv_pairs(str(outer.pop("message")))
    inner.update({key: value for key, value in outer.items() if value != ""})
    return inner


# ---------------------------------------------------------------------------
# Event mappings
# ---------------------------------------------------------------------------
def _record_lookup_observation(
    document: EvidenceDocument,
    timestamp: str,
    fields: Mapping[str, str],
    warnings: list[str],
) -> base.Observation | None:
    system_id = clean_optional(fields.get("system"))
    resource_id = clean_optional(fields.get("resource_id"))
    record_type = clean_optional(fields.get("record_type"))
    result = clean_optional(fields.get("result"))
    if not (system_id and resource_id and record_type and result):
        warnings.append("incomplete record_lookup line")
        return None
    identifiers: dict[str, str] = {}
    add_identifier(identifiers, "subject_reference", fields.get("subject_reference"))
    for key in _IDENTIFIER_FIELDS:
        if key in fields:
            add_identifier(identifiers, key, fields[key])
    context: dict[str, Any] = {"source": "log_record_lookup"}
    if clean_optional(fields.get("seq")):
        context["seq"] = fields["seq"]
    return base.make_observation(
        evidence_id=document.file_id,
        captured_at=document.captured_at,
        system_id=system_id,
        resource_id=resource_id,
        record_type=record_type,
        raw_state=result,
        observed_at=timestamp,
        identifiers=identifiers,
        context=context,
    )


def _event_observation(
    document: EvidenceDocument,
    timestamp: str,
    event: str,
    fields: Mapping[str, str],
) -> base.Observation | None:
    handler = _EVENT_HANDLERS.get(event)
    if handler is None:
        return None
    return handler(document, timestamp, fields)


def _make(
    document: EvidenceDocument,
    timestamp: str,
    fields: Mapping[str, str],
    *,
    system_id: str,
    record_type: str,
    raw_state: str,
    resource_id: str | None = None,
    extra_identifiers: Mapping[str, str] | None = None,
    extra_context: Mapping[str, Any] | None = None,
) -> base.Observation | None:
    resource = clean_optional(resource_id) or clean_optional(fields.get("resource_id"))
    if resource is None:
        return None
    identifiers: dict[str, str] = {}
    for key in _IDENTIFIER_FIELDS:
        value = fields.get(key)
        if value is not None:
            add_identifier(identifiers, key, value)
    for key, value in (extra_identifiers or {}).items():
        add_identifier(identifiers, key, value)
    event_name = clean_optional(fields.get("event")) or "unknown"
    context: dict[str, Any] = {"source": "log_event", "event": event_name}
    for key in ("rows", "http_status", "result", "enabled", "restore_allowed", "decision", "event_code"):
        value = clean_optional(fields.get(key))
        if value is not None:
            context[key] = value
    if extra_context:
        context.update(extra_context)
    return base.make_observation(
        evidence_id=document.file_id,
        captured_at=document.captured_at,
        system_id=system_id,
        resource_id=resource,
        record_type=record_type,
        raw_state=raw_state,
        observed_at=timestamp,
        identifiers=identifiers,
        context=context,
    )


def _row_state(fields: Mapping[str, str]) -> str:
    """Derive existence from the ``rows``/``http_status`` pair."""

    rows = clean_optional(fields.get("rows"))
    status = clean_optional(fields.get("http_status"))
    if rows == "0" or status == "404":
        return "NOT_FOUND"
    return "FOUND"


def _handle_profile_lookup(document, timestamp, fields):
    return _make(
        document,
        timestamp,
        fields,
        system_id=base.SYSTEM_ACCOUNT,
        record_type=base.RT_USER_PROFILE,
        raw_state=_row_state(fields),
    )


def _handle_index_query(document, timestamp, fields):
    result = (clean_optional(fields.get("result")) or "").lower()
    if result in {"hit", "found"}:
        state = "FOUND"
    elif result in {"miss", "not_found", "no_hit"}:
        state = "NOT_FOUND"
    elif clean_optional(fields.get("total_hits")) in {"0"}:
        state = "NOT_FOUND"
    else:
        state = "FOUND"
    return _make(
        document,
        timestamp,
        fields,
        system_id=base.SYSTEM_SEARCH,
        record_type=base.RT_SEARCH_INDEX_ENTRY,
        raw_state=state,
    )


def _handle_connector_read(document, timestamp, fields):
    state = _connector_state(fields)
    return _make(
        document,
        timestamp,
        fields,
        system_id=base.SYSTEM_INTEGRATION,
        record_type=base.RT_DATA_FLOW,
        raw_state=state,
    )


def _connector_state(fields: Mapping[str, str]) -> str:
    enabled = truthy(fields.get("enabled"))
    if enabled is not None:
        return "ACTIVE" if enabled else "DISABLED"
    delivery = (clean_optional(fields.get("last_delivery_status")) or "").lower()
    return "DISABLED" if delivery in {"stopped", "disabled"} else "ACTIVE"


def _handle_restore_policy_read(document, timestamp, fields):
    archive_present = truthy(fields.get("archive_present"))
    if archive_present is False:
        state = "ABSENT"
    else:
        restore_allowed = truthy(fields.get("restore_allowed"))
        state = "BLOCKED" if restore_allowed is False else "ACTIVE"
    return _make(
        document,
        timestamp,
        fields,
        system_id=base.SYSTEM_BACKUP,
        record_type=base.RT_BACKUP_ARCHIVE,
        raw_state=state,
    )


def _handle_restore_authorization(document, timestamp, fields):
    decision = (clean_optional(fields.get("decision")) or "").lower()
    state = "BLOCKED" if decision in {"deny", "denied", "blocked"} else "ACTIVE"
    return _make(
        document,
        timestamp,
        fields,
        system_id=base.SYSTEM_BACKUP,
        record_type=base.RT_BACKUP_ARCHIVE,
        raw_state=state,
    )


def _handle_restore_candidate(document, timestamp, fields):
    deletion_block = truthy(fields.get("deletion_block"))
    eligible = truthy(fields.get("eligible"))
    if deletion_block is True:
        state = "BLOCKED"
    elif eligible is False:
        state = "BLOCKED"
    else:
        state = "ACTIVE"
    return _make(
        document,
        timestamp,
        fields,
        system_id=base.SYSTEM_BACKUP,
        record_type=base.RT_BACKUP_ARCHIVE,
        raw_state=state,
    )


def _handle_support_ticket_read(document, timestamp, fields):
    has_identifier = any(
        clean_optional(fields.get(key)) for key in ("requester_name", "requester_email", "requester_phone")
    )
    state = "IDENTIFIABLE" if has_identifier else "REDACTED"
    return _make(
        document,
        timestamp,
        fields,
        system_id=base.SYSTEM_SUPPORT,
        record_type=base.RT_SUPPORT_MESSAGE,
        raw_state=state,
    )


def _handle_support_contact_query(document, timestamp, fields):
    return _make(
        document,
        timestamp,
        fields,
        system_id=base.SYSTEM_SUPPORT,
        record_type=base.RT_SUPPORT_MESSAGE,
        raw_state=_row_state(fields),
    )


def _handle_support_marker(document, timestamp, fields):
    marker_found = truthy(fields.get("marker_found"))
    state = "ABSENT" if marker_found else "PRESENT"
    return _make(
        document,
        timestamp,
        fields,
        system_id=base.SYSTEM_SUPPORT,
        record_type=base.RT_SUPPORT_MESSAGE,
        raw_state=state,
    )


def _handle_support_message_read(document, timestamp, fields):
    message = clean_optional(fields.get("message_text")) or ""
    state = "REDACTED" if "[REDACTED]" in message.upper() else "PRESENT"
    return _make(
        document,
        timestamp,
        fields,
        system_id=base.SYSTEM_SUPPORT,
        record_type=base.RT_SUPPORT_MESSAGE,
        raw_state=state,
    )


def _handle_support_message_scan(document, timestamp, fields):
    return _make(
        document,
        timestamp,
        fields,
        system_id=base.SYSTEM_SUPPORT,
        record_type=base.RT_SUPPORT_MESSAGE,
        raw_state="IDENTIFIABLE",
    )


def _handle_attachment_scan(document, timestamp, fields):
    return _make(
        document,
        timestamp,
        fields,
        system_id=base.SYSTEM_SUPPORT,
        record_type=base.RT_SUPPORT_ATTACHMENT,
        raw_state="FOUND",
    )


def _handle_analytics_read(document, timestamp, fields):
    has_identifier = any(
        clean_optional(fields.get(key)) for key in ("email", "phone", "email_hash", "customer_reference")
    )
    state = "IDENTIFIABLE" if has_identifier else "ANONYMIZED"
    return _make(
        document,
        timestamp,
        fields,
        system_id=base.SYSTEM_ANALYTICS,
        record_type=base.RT_ANALYTICS_PROFILE,
        raw_state=state,
    )


def _handle_analytics_scan(document, timestamp, fields):
    count = clean_optional(fields.get("direct_identifiers"))
    has_identifier = bool(clean_optional(fields.get("matched_value")))
    if count is not None:
        has_identifier = has_identifier or count not in {"0", ""}
    state = "IDENTIFIABLE" if has_identifier else "ANONYMIZED"
    return _make(
        document,
        timestamp,
        fields,
        system_id=base.SYSTEM_ANALYTICS,
        record_type=base.RT_ANALYTICS_PROFILE,
        raw_state=state,
    )


def _handle_analytics_contact(document, timestamp, fields):
    rows = clean_optional(fields.get("rows"))
    state = "IDENTIFIABLE" if rows != "0" else "ANONYMIZED"
    return _make(
        document,
        timestamp,
        fields,
        system_id=base.SYSTEM_ANALYTICS,
        record_type=base.RT_ANALYTICS_PROFILE,
        raw_state=state,
    )


def _handle_audit_read(document, timestamp, fields):
    state = "RETAINED" if _row_state(fields) == "FOUND" else "ABSENT"
    return _make(
        document,
        timestamp,
        fields,
        system_id=base.SYSTEM_SECURITY,
        record_type=base.RT_SECURITY_AUDIT_LOG,
        raw_state=state,
    )


def _handle_legal_hold(document, timestamp, fields):
    return _make(
        document,
        timestamp,
        fields,
        system_id=base.SYSTEM_LEGAL,
        record_type=base.RT_LEGAL_HOLD_RECORD,
        raw_state="RETAINED",
    )


def _handle_export_metadata(document, timestamp, fields):
    return _make(
        document,
        timestamp,
        fields,
        system_id=base.SYSTEM_EXPORT,
        record_type=base.RT_USER_EXPORT,
        raw_state="FOUND",
    )


#: Terminal log events that render a resource state.  Started events and
#: operational noise are intentionally absent.
_EVENT_HANDLERS = {
    "profile_lookup_completed": _handle_profile_lookup,
    "index_query_completed": _handle_index_query,
    "connector_config_read_completed": _handle_connector_read,
    "connector_heartbeat": _handle_connector_read,
    "restore_policy_read_completed": _handle_restore_policy_read,
    "restore_authorization_completed": _handle_restore_authorization,
    "restore_candidate_evaluated": _handle_restore_candidate,
    "support_ticket_read_completed": _handle_support_ticket_read,
    "contact_query_completed": _handle_support_contact_query,
    "deletion_marker_check": _handle_support_marker,
    "message_read": _handle_support_message_read,
    "message_pii_scan": _handle_support_message_scan,
    "attachment_pii_scan": _handle_attachment_scan,
    "analytics_profile_read_completed": _handle_analytics_read,
    "analytics_profile_read": _handle_analytics_read,
    "analytics_identity_scan": _handle_analytics_scan,
    "contact_lookup_completed": _handle_analytics_contact,
    "audit_record_read_completed": _handle_audit_read,
    "audit_record_query_completed": _handle_audit_read,
    "retained_order_loaded": _handle_legal_hold,
    "export_metadata_read_completed": _handle_export_metadata,
    "export_download_check_completed": _handle_export_metadata,
}


# ---------------------------------------------------------------------------
# Readback traces
# ---------------------------------------------------------------------------
READBACK_STARTED_EVENTS = {
    "profile_lookup_started",
    "index_query_started",
    "connector_config_read_started",
    "restore_policy_read_started",
    "support_ticket_read_started",
    "analytics_profile_read_started",
    "audit_record_read_started",
    "audit_record_query_started",
    "export_metadata_read_started",
    "export_download_check_started",
    "message_scan_started",
    "attachment_scan_started",
    "analytics_profile_scan_started",
    "restore_policy_check_started",
    "retained_order_query_started",
    "contact_query_started",
}

__all__ = ["parse_log_document", "READBACK_STARTED_EVENTS"]
