"""Conversions from structured evidence records into normalized observations.

Three evidence families share the same shape: a JSON/SQL snapshot renders a
collection of records (customer profiles, search documents, support
conversations, ...).  This module owns the single mapping from a rendered
record to a :class:`~ghost_audit.evidence.base.Observation` so every format
family applies the same policy.

The mapping is conservative:

* identifiers are copied only from fields the evidence actually renders;
* the state word is derived from the rendered fields (``enabled``,
  ``restore_allowed``, ``message_text``, direct identifiers) and the raw word is
  kept in ``context['raw_state']``;
* a record with no usable ``resource_id`` is dropped and reported as a warning
  by the caller.
"""

from __future__ import annotations

from typing import Any, Mapping

from . import base
from ._util import (
    add_identifier,
    clean_optional,
    flag_word,
    identifiers_from_mapping,
    identifiers_from_text,
    iso_timestamp,
)

#: ``collection`` / ``record_type`` values rendered by the records evidence.
COLLECTION_TO_SYSTEM: dict[str, str] = {
    "customer_profiles": base.SYSTEM_ACCOUNT,
    "customer_search_documents": base.SYSTEM_SEARCH,
    "customer_analytics_reports": base.SYSTEM_ANALYTICS,
    "support_conversations": base.SYSTEM_SUPPORT,
    "ticket_attachments": base.SYSTEM_SUPPORT,
    "connector_configurations": base.SYSTEM_INTEGRATION,
    "backup_catalog": base.SYSTEM_BACKUP,
    "deleted_customer_references": base.SYSTEM_IDENTITY,
    "security_audit_events": base.SYSTEM_SECURITY,
    "customer_export_jobs": base.SYSTEM_EXPORT,
    "retained_order_records": base.SYSTEM_LEGAL,
}

COLLECTION_TO_RECORD_TYPE: dict[str, str] = {
    "customer_profiles": base.RT_USER_PROFILE,
    "customer_search_documents": base.RT_SEARCH_INDEX_ENTRY,
    "customer_analytics_reports": base.RT_ANALYTICS_PROFILE,
    "support_conversations": base.RT_SUPPORT_MESSAGE,
    "ticket_attachments": base.RT_SUPPORT_ATTACHMENT,
    "connector_configurations": base.RT_DATA_FLOW,
    "backup_catalog": base.RT_BACKUP_ARCHIVE,
    "deleted_customer_references": base.RT_ACCOUNT_TOMBSTONE,
    "security_audit_events": base.RT_SECURITY_AUDIT_LOG,
    "customer_export_jobs": base.RT_USER_EXPORT,
    "retained_order_records": base.RT_LEGAL_HOLD_RECORD,
    # Only rendered by the platform event log / record-lookup evidence.
    "marketing_profiles": base.RT_MARKETING_PROFILE,
    "email_deliveries": base.RT_EMAIL_DELIVERY_LOG,
    "product_telemetry": base.RT_PRODUCT_TELEMETRY,
    "cache_entries": base.RT_CACHE_ENTRY,
    "secondary_identifiers": base.RT_SECONDARY_IDENTIFIER,
    "legal_hold_records": base.RT_LEGAL_HOLD_RECORD,
}

#: Fields that carry a *direct* identifier (name/email/phone/email hash).
_DIRECT_IDENTIFIER_FIELDS = (
    "full_name",
    "display_name",
    "name",
    "requester_name",
    "email",
    "phone",
    "email_hash",
    "requester_email",
    "requester_phone",
)

#: Collections whose records may nest the identifier fields one level down.
_NESTED_RECORD_FIELDS: dict[str, tuple[str, ...]] = {
    "customer_search_documents": ("document",),
}


def direct_identifiers(record: Mapping[str, Any]) -> dict[str, str]:
    """Return the direct identifiers rendered by ``record`` (nested included)."""

    result: dict[str, str] = {}
    for field in _DIRECT_IDENTIFIER_FIELDS:
        add_identifier(result, field, record.get(field))
    for nested_key in _NESTED_RECORD_FIELDS.get(str(record.get("_collection", "")), ()):
        nested = record.get(nested_key)
        if isinstance(nested, Mapping):
            for field in _DIRECT_IDENTIFIER_FIELDS:
                add_identifier(result, field, nested.get(field))
    return result


def record_identifiers(collection: str, record: Mapping[str, Any]) -> dict[str, str]:
    """Collect canonical identifiers for one rendered record.

    Support messages also carry identifiers inside the message text (the
    customer writes their email/phone in the message); those are extracted so
    the record can be linked to its subject.
    """

    result = identifiers_from_mapping(record)
    for nested_key in _NESTED_RECORD_FIELDS.get(collection, ()):
        nested = record.get(nested_key)
        if isinstance(nested, Mapping):
            for key, value in nested.items():
                add_identifier(result, key, value)
    if COLLECTION_TO_RECORD_TYPE.get(collection) == base.RT_SUPPORT_MESSAGE:
        for key, value in identifiers_from_text(_message_text(record)).items():
            result.setdefault(key, value)
    return result


def _message_text(record: Mapping[str, Any]) -> str:
    return " ".join(str(record.get(key) or "") for key in ("message_text", "message_excerpt", "message", "body"))


def state_for_rendered_record(record_type: str, record: Mapping[str, Any]) -> str:
    """Derive the raw state word for a rendered record.

    The word is one of the spellings documented in
    :mod:`ghost_audit.evidence.base` so :func:`base.make_observation` can
    normalize it.  Unknown categories fall back to the record's own
    ``account_status``/``result``/``status`` field and finally to ``FOUND``.
    """

    # Record-lookup style rows assert their own result word (FOUND, NOT_FOUND,
    # REDACTED, ACTIVE, ...); keep it instead of re-deriving a state.
    explicit_result = clean_optional(record.get("result"))
    if explicit_result is not None and clean_optional(record.get("record_type")) is not None:
        return explicit_result

    if record_type == base.RT_USER_PROFILE:
        status = clean_optional(record.get("account_status"))
        return status or "FOUND"

    if record_type in (base.RT_SEARCH_INDEX_ENTRY, base.RT_USER_EXPORT, base.RT_SUPPORT_ATTACHMENT):
        return "FOUND"

    if record_type == base.RT_ANALYTICS_PROFILE:
        return "IDENTIFIABLE" if direct_identifiers(record) else "ANONYMIZED"

    if record_type == base.RT_SUPPORT_MESSAGE:
        message = _message_text(record)
        if "[REDACTED]" in message.upper():
            return "REDACTED"
        if direct_identifiers(record) or identifiers_from_text(message):
            return "IDENTIFIABLE"
        # Only the allowed stable subject reference remains.
        return "REDACTED"

    if record_type == base.RT_ACCOUNT_TOMBSTONE:
        return "PRESENT" if direct_identifiers(record) else "REDACTED"

    if record_type == base.RT_DATA_FLOW:
        return flag_word(record.get("enabled"), when_true="ACTIVE", when_false="DISABLED") or "FOUND"

    if record_type == base.RT_BACKUP_ARCHIVE:
        return flag_word(record.get("restore_allowed"), when_true="ACTIVE", when_false="BLOCKED") or "FOUND"

    if record_type in (base.RT_SECURITY_AUDIT_LOG, base.RT_LEGAL_HOLD_RECORD):
        return "RETAINED"

    for key in ("account_status", "result", "status", "state"):
        value = clean_optional(record.get(key))
        if value is not None:
            return value
    return "FOUND"


def observation_from_collection_record(
    *,
    evidence_id: str,
    captured_at: str,
    data_store: str | None,
    collection: str,
    record: Mapping[str, Any],
    observed_at: str | None,
    extra_context: Mapping[str, Any] | None = None,
    warnings: list[str] | None = None,
) -> base.Observation | None:
    """Convert one rendered collection record into an observation.

    ``collection`` decides the category and system, unless the record carries
    its own ``system``/``record_type`` pair (the ``operational_record_history``
    collection does).  Returns ``None`` when the record has no resource id.
    """

    flattened = dict(record)
    flattened["_collection"] = collection
    record_type = clean_optional(record.get("record_type"))
    system_id = clean_optional(record.get("system"))
    if record_type is None or system_id is None:
        record_type = COLLECTION_TO_RECORD_TYPE.get(collection)
        system_id = COLLECTION_TO_SYSTEM.get(collection) or data_store
    if record_type is None or system_id is None:
        if warnings is not None:
            warnings.append(f"unmapped collection {collection!r}")
        return None

    resource_id = clean_optional(record.get("resource_id"))
    if resource_id is None:
        if warnings is not None:
            warnings.append(f"record without resource_id in {collection!r}")
        return None

    identifiers = record_identifiers(collection, flattened)
    if not identifiers:
        fallback = clean_optional(record.get("subject_reference")) or clean_optional(record.get("lookup_value"))
        add_identifier(identifiers, "customer_reference", fallback)

    raw_state = state_for_rendered_record(record_type, flattened)
    context: dict[str, Any] = {
        "collection": collection,
        "source": "rendered_record",
    }
    if data_store:
        context["data_store"] = data_store
    for key in (
        "event_code",
        "event_name",
        "retention_until",
        "retained_until",
        "order_reference",
        "file_name",
        "storage_class",
        "index_name",
        "archive_key",
        "archive_path",
        "connector_name",
        "destination",
        "mode",
        "channel",
        "ticket_id",
        "uploaded_by",
        "created_at",
        "expires_at",
        "download_available",
        "updated_at",
    ):
        value = clean_optional(record.get(key))
        if value is not None:
            context[key] = value
    if extra_context:
        context.update(extra_context)
    return base.make_observation(
        evidence_id=evidence_id,
        captured_at=captured_at,
        system_id=system_id,
        resource_id=resource_id,
        record_type=record_type,
        raw_state=raw_state,
        observed_at=observed_at,
        identifiers=identifiers,
        context=context,
    )


def observation_from_state_row(
    *,
    evidence_id: str,
    captured_at: str,
    system_id: str,
    resource_id: str,
    record_type: str,
    raw_state: str,
    identifiers: Mapping[str, str] | None = None,
    observed_at: str | None = None,
    context: Mapping[str, Any] | None = None,
) -> base.Observation:
    """Small wrapper so parsers do not duplicate keyword arguments."""

    return base.make_observation(
        evidence_id=evidence_id,
        captured_at=captured_at,
        system_id=system_id,
        resource_id=resource_id,
        record_type=record_type,
        raw_state=raw_state,
        observed_at=observed_at or iso_timestamp(captured_at),
        identifiers=identifiers,
        context=context,
    )


def observation_from_lookup(
    *,
    evidence_id: str,
    captured_at: str,
    system_id: str,
    resource_id: str,
    record_type: str,
    result: str,
    subject_reference: str | None,
    observed_at: str | None,
    context: Mapping[str, Any] | None = None,
    warnings: list[str] | None = None,
) -> base.Observation | None:
    """Convert one explicit ``record_lookup`` style fact into an observation.

    This covers log lines, readback traces and ``records[]`` manifests, which
    all assert ``system/resource/type/result`` directly.  Unknown record types
    are skipped with a warning instead of being coerced.
    """

    if record_type not in base.RECORD_TYPES:
        if warnings is not None:
            warnings.append(f"unknown record_type {record_type!r}")
        return None
    identifiers: dict[str, str] = {}
    add_identifier(identifiers, "subject_reference", subject_reference)
    observation_context: dict[str, Any] = {"source": "record_lookup"}
    if context:
        observation_context.update(context)
    return base.make_observation(
        evidence_id=evidence_id,
        captured_at=captured_at,
        system_id=system_id,
        resource_id=resource_id,
        record_type=record_type,
        raw_state=result,
        observed_at=observed_at,
        identifiers=identifiers,
        context=observation_context,
    )
