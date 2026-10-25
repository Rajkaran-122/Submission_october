"""Parser for the shift-handover plain-text evidence.

The handover file is a table of completed checks, one per line::

    - 2026-10-01T12:00:00Z | account_service | account_146 | USER_PROFILE | NOT_FOUND | e.lindberg18@yahoo.com

The sixth column is the subject: an email, phone, stable ``user_`` reference,
or the literal ``no direct identifier``.  The parser reads only the rendered
columns and keeps the raw state word in the observation context.
"""

from __future__ import annotations

import re

from ..dataset import EvidenceDocument
from . import base
from ._util import add_identifier, iso_timestamp
from .result import ParsedFile

#: One handover row: ``- <timestamp> | <system> | <resource> | <type> | <state> | <subject>``
_HANDOVER_ROW_RE = re.compile(
    r"^-\s*(?P<timestamp>[0-9T:\-]+Z)\s*\|\s*(?P<system>[a-z_]+)\s*\|\s*"
    r"(?P<resource>[A-Za-z0-9_\-]+)\s*\|\s*(?P<record_type>[A-Z_]+)\s*\|\s*"
    r"(?P<state>[A-Z_]+)\s*\|\s*(?P<subject>.+?)\s*$"
)

#: Multi-word / ambiguous record types rendered by the handover.
_RECORD_TYPE_ALIASES = {
    "SEARCH_INDEX_ENTRY": base.RT_SEARCH_INDEX_ENTRY,
    "SECURITY_AUDIT_LOG": base.RT_SECURITY_AUDIT_LOG,
    "SUPPORT_ATTACHMENT": base.RT_SUPPORT_ATTACHMENT,
    "SUPPORT_MESSAGE": base.RT_SUPPORT_MESSAGE,
    "MARKETING_PROFILE": base.RT_MARKETING_PROFILE,
    "USER_PROFILE": base.RT_USER_PROFILE,
    "USER_EXPORT": base.RT_USER_EXPORT,
    "BACKUP_ARCHIVE": base.RT_BACKUP_ARCHIVE,
    "DATA_FLOW": base.RT_DATA_FLOW,
    "ANALYTICS_PROFILE": base.RT_ANALYTICS_PROFILE,
    "LEGAL_HOLD_RECORD": base.RT_LEGAL_HOLD_RECORD,
    "ACCOUNT_TOMBSTONE": base.RT_ACCOUNT_TOMBSTONE,
    "EMAIL_DELIVERY_LOG": base.RT_EMAIL_DELIVERY_LOG,
    "CACHE_ENTRY": base.RT_CACHE_ENTRY,
    "PRODUCT_TELEMETRY": base.RT_PRODUCT_TELEMETRY,
    "SECONDARY_IDENTIFIER": base.RT_SECONDARY_IDENTIFIER,
}


def parse_handover_document(document: EvidenceDocument) -> ParsedFile:
    """Parse every handover row into one observation."""

    parsed = ParsedFile()
    for line in document.text.splitlines():
        match = _HANDOVER_ROW_RE.match(line.strip())
        if match is None:
            continue
        raw_type = match.group("record_type")
        record_type = _RECORD_TYPE_ALIASES.get(raw_type)
        system_id = match.group("system")
        resource_id = match.group("resource")
        if record_type is None:
            parsed.warnings.append(f"unknown handover record type {raw_type!r}")
            continue
        identifiers: dict[str, str] = {}
        subject = match.group("subject").strip()
        add_identifier(identifiers, "subject_reference", subject)
        observation = base.make_observation(
            evidence_id=document.file_id,
            captured_at=document.captured_at,
            system_id=system_id,
            resource_id=resource_id,
            record_type=record_type,
            raw_state=match.group("state"),
            observed_at=iso_timestamp(match.group("timestamp")) or document.captured_at,
            identifiers=identifiers,
            context={"source": "handover", "subject": subject},
        )
        parsed.add(observation)
    if not parsed.observations:
        parsed.warnings.append("no handover rows parsed")
    return parsed


__all__ = ["parse_handover_document"]
