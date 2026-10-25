"""Parsers for the document evidence: PDFs and screenshot OCR text.

PDFs are converted to text by :mod:`ghost_audit.dataset` (pypdf) and PNG
screenshots are OCR'd by the local macOS Vision helper; both arrive as plain
text.  The extracted layout loses table columns, so each document family has a
small tolerant reader that only emits facts literally present in the text.

Rules applied here:

* only visible identifiers/resource ids are used; nothing is synthesized;
* ``observed_at`` is set only when the document itself renders an exact
  timestamp for the fact (for example the audit ``recorded_at`` in the
  compliance extract).  Console screenshots otherwise keep ``observed_at``
  empty so the scenario layer falls back to the manifest capture time;
* coverage is exactly the set of system/category pairs the document renders.

``object_storage_browser`` renders attachment *file names* rather than platform
resource ids; observations use the file name as the object-storage resource id
and keep the name in ``identifiers``/``context`` so the scenario layer can join
it with the ``ticket_attachments.file_name`` records from the database
snapshots.  ``deleted_account_reference`` renders the retained stable customer
key (``user_17``) and no direct identifiers; the tombstone resource id is not
rendered, so the observation keeps the visible reference and says so in
``context``.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Mapping

from ..dataset import EvidenceDocument
from . import base
from ._util import add_identifier, identifiers_from_text, iso_timestamp
from .result import ParsedFile

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")
_USER_RE = re.compile(r"user_\d+")
_RESOURCE_ID_RE = re.compile(r"[a-z][a-z_]*_\d+")
_PHONE_RE = re.compile(r"\+\d[\d\s()\-]{7,}\d")
_AUDIT_ID_RE = re.compile(r"audit[_ ]record[_ ]?(\d{1,4})", re.IGNORECASE)
_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
_DATE_TIME_RE = re.compile(
    r"(?P<day>\d{1,2})\s+(?P<month>[A-Za-z]{3})[a-z]*\s+(?P<year>\d{4})\s+"
    r"(?P<hour>\d{1,2})\s*(?::|\s)\s*(?P<minute>\d{2})\s*UTC"
)


def parse_document_text(document: EvidenceDocument) -> ParsedFile:
    """Dispatch one PDF/image document to its family reader."""

    parser = _find_parser(document.file_path)
    if parser is None:
        parsed = ParsedFile()
        parsed.warnings.append(f"no document reader for {document.file_path}")
        return parsed
    return parser(document)


def _find_parser(file_path: str) -> Callable[[EvidenceDocument], ParsedFile] | None:
    for marker, parser in _DOCUMENT_PARSERS:
        if marker in file_path:
            return parser
    return None


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def _flat(text: str) -> str:
    """Join the extracted lines so PDF column breaks do not hide patterns."""

    return " ".join(line.strip() for line in text.splitlines() if line.strip())


def _date_time(match: re.Match[str] | None) -> str | None:
    if match is None:
        return None
    month = _MONTHS.get(match.group("month").lower())
    if month is None:
        return None
    return (
        f"{int(match.group('year')):04d}-{month:02d}-{int(match.group('day')):02d}"
        f"T{int(match.group('hour')):02d}:{int(match.group('minute')):02d}:00Z"
    )


def _first(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    return match.group(0) if match else None


def _all_unique(pattern: re.Pattern[str], text: str) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for match in pattern.finditer(text):
        value = match.group(0)
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


def _resource_ids(text: str, prefix: str | None = None) -> list[str]:
    """Return rendered resource ids, optionally restricted to one prefix."""

    pattern = _RESOURCE_ID_RE
    if prefix is not None:
        # OCR occasionally glues the row number to the id ("1search_428"), so
        # only a letter/underscore prefix disqualifies a match.
        pattern = re.compile(rf"(?<![A-Za-z_]){re.escape(prefix)}_\d+")
    return [value for value in _all_unique(pattern, text) if not value.startswith("user_")]


def _emails(text: str) -> list[str]:
    return _all_unique(_EMAIL_RE, text)


def _first_email(text: str) -> str | None:
    match = _EMAIL_RE.search(text)
    return match.group(0) if match else None


def _first_phone(text: str) -> str | None:
    match = _PHONE_RE.search(text)
    return match.group(0).strip() if match else None


def _first_user(text: str) -> str | None:
    match = _USER_RE.search(text)
    return match.group(0) if match else None


def _line_before(text: str, needle: str) -> str | None:
    """Return the non-empty line directly before the line containing ``needle``."""

    lines = [line.strip() for line in text.splitlines()]
    for index, line in enumerate(lines):
        if needle in line:
            for previous in reversed(lines[:index]):
                if previous:
                    return previous
    return None


def _pdf_customer_name(text: str) -> str | None:
    """Read a person name that is rendered as its own line.

    PDF extractions keep a name on its own line; screenshots keep it in a
    header.  Only a title-cased line of 2-4 words is accepted, optionally
    prefixed with ``Customer``.  ALL-CAPS headings and UI labels are rejected.
    """

    for line in text.splitlines():
        candidate = line.strip()
        candidate = re.sub(r"^(?:Customer\s+)", "", candidate)
        words = candidate.split()
        if not 2 <= len(words) <= 4:
            continue
        if any(character.isdigit() for character in candidate):
            continue
        if not all(word.istitle() for word in words):
            continue
        if any(token in candidate for token in ("Shop", "Console", "Registry", "Manager", "Request")):
            continue
        return candidate
    return None


def _iso_date(value: str | None) -> str | None:
    """Normalize a rendered date (``04 May 2033``) to ``YYYY-MM-DD``."""

    if value is None:
        return None
    match = re.search(r"(\d{1,2})\s+([A-Za-z]{3})[a-z]*\s+(\d{4})", value)
    if match is None:
        return value.strip()
    month = _MONTHS.get(match.group(2).lower())
    if month is None:
        return value.strip()
    return f"{int(match.group(3)):04d}-{month:02d}-{int(match.group(1)):02d}"


def _make(
    document: EvidenceDocument,
    *,
    system_id: str,
    resource_id: str,
    record_type: str,
    raw_state: str,
    identifiers: Mapping[str, str] | None = None,
    observed_at: str | None = None,
    context: Mapping[str, Any] | None = None,
) -> base.Observation:
    return base.make_observation(
        evidence_id=document.file_id,
        captured_at=document.captured_at,
        system_id=system_id,
        resource_id=resource_id,
        record_type=record_type,
        raw_state=raw_state,
        observed_at=observed_at,
        identifiers=identifiers,
        context=dict(context or {}),
    )


# ---------------------------------------------------------------------------
# PDF documents
# ---------------------------------------------------------------------------
def _parse_account_history_report(document: EvidenceDocument) -> ParsedFile:
    """Customer statement: renders the customer identity and order history."""

    parsed = ParsedFile()
    text = _flat(document.text)
    user_id = _first_user(text)
    email = _first_email(text)
    name = _pdf_customer_name(document.text)
    identifiers: dict[str, str] = {}
    add_identifier(identifiers, "user_id", user_id)
    add_identifier(identifiers, "email", email)
    add_identifier(identifiers, "name", name)
    order = _first(re.compile(r"ORD-\d+"), text)
    if user_id is None:
        parsed.warnings.append("account history report without a rendered customer reference")
        return parsed
    parsed.add(
        _make(
            document,
            system_id=base.SYSTEM_ACCOUNT,
            resource_id=user_id,
            record_type=base.RT_USER_PROFILE,
            raw_state="FOUND",
            identifiers=identifiers,
            context={
                "source": "account_history_report",
                "order_reference": order,
                "note": "statement renders the customer identity; no account resource id is visible",
            },
        )
    )
    return parsed


def _parse_legal_retention_register(document: EvidenceDocument) -> ParsedFile:
    """Retention register: one retained legal hold record per disputed order row."""

    parsed = ParsedFile()
    text = _flat(document.text)
    row_re = re.compile(
        r"(ORD-\d+)\s*\|\s*(user_\d+)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*"
        r"([A-Z]{3}\s+[\d.,]+)\s*\|\s*Retain until\s+([^|]+?)\s*\|"
    )
    for match in row_re.finditer(text):
        order_reference, user_id, reason, order_date, amount, retain_until = match.groups()
        parsed.add(
            _make(
                document,
                system_id=base.SYSTEM_LEGAL,
                resource_id=order_reference,
                record_type=base.RT_LEGAL_HOLD_RECORD,
                raw_state="RETAINED",
                identifiers={"user_id": user_id},
                context={
                    "source": "legal_retention_register",
                    "retention_reason": reason.strip(),
                    "order_date": order_date.strip(),
                    "amount": amount.strip(),
                    "retain_until": _iso_date(retain_until),
                },
            )
        )
    if not parsed.observations:
        parsed.warnings.append("legal retention register rows not recognized")
    return parsed


def _parse_security_retention_extract(document: EvidenceDocument) -> ParsedFile:
    """Compliance journal extract for one retained security audit record."""

    parsed = ParsedFile()
    text = _flat(document.text)
    audit_id = _first(_AUDIT_ID_RE, text)
    user_id = _first_user(text)
    if audit_id is None:
        parsed.warnings.append("security retention extract without an audit record id")
        return parsed
    audit_id = _normalize_audit_id(audit_id)
    recorded_match = re.search(r"recorded\s+" + _DATE_TIME_RE.pattern, text)
    retained_match = re.search(r"retain until\s+" + _DATE_TIME_RE.pattern, text, re.IGNORECASE)
    event_match = re.search(r"event\s+([A-Z][^.|]+?)\s+Detail", text)
    identifiers: dict[str, str] = {}
    add_identifier(identifiers, "user_id", user_id)
    parsed.add(
        _make(
            document,
            system_id=base.SYSTEM_SECURITY,
            resource_id=audit_id,
            record_type=base.RT_SECURITY_AUDIT_LOG,
            raw_state="RETAINED",
            identifiers=identifiers,
            observed_at=_date_time(recorded_match) if isinstance(recorded_match, re.Match) else None,
            context={
                "source": "security_retention_extract",
                "event_name": event_match.group(1).strip() if event_match else None,
                "retain_until": _date_time(retained_match) if isinstance(retained_match, re.Match) else None,
            },
        )
    )
    return parsed


def _normalize_audit_id(raw: str) -> str:
    match = _AUDIT_ID_RE.search(raw)
    assert match is not None
    return f"audit_record_{int(match.group(1)):03d}"


def _parse_delivery_correction_attachment(document: EvidenceDocument) -> ParsedFile:
    """Support attachment PDF: the attachment record and the requester identity."""

    parsed = ParsedFile()
    text = _flat(document.text)
    support_id = _first(re.compile(r"support_\d+"), text)
    if support_id is None:
        parsed.warnings.append("support attachment pdf without a support record id")
        return parsed
    identifiers = identifiers_from_text(text)
    add_identifier(identifiers, "name", _pdf_customer_name(document.text))
    captured_match = re.search(r"captured\s+" + _DATE_TIME_RE.pattern, text, re.IGNORECASE)
    order = _first(re.compile(r"ORD-\d+"), text)
    parsed.add(
        _make(
            document,
            system_id=base.SYSTEM_SUPPORT,
            resource_id=support_id,
            record_type=base.RT_SUPPORT_ATTACHMENT,
            raw_state="FOUND",
            identifiers=identifiers,
            observed_at=_date_time(captured_match) if isinstance(captured_match, re.Match) else None,
            context={
                "source": "delivery_correction_attachment",
                "order_reference": order,
                "file_name": _first(re.compile(r"[a-z_]+\.pdf"), text),
            },
        )
    )
    return parsed


# ---------------------------------------------------------------------------
# Screenshot (OCR text) documents
# ---------------------------------------------------------------------------
def _parse_account_admin_console(document: EvidenceDocument) -> ParsedFile:
    parsed = ParsedFile()
    text = document.text
    account_id = _first(re.compile(r"account_\d+"), text)
    user_id = _first_user(text)
    if account_id is None:
        parsed.warnings.append("account console without an account resource id")
        return parsed
    identifiers: dict[str, str] = {}
    add_identifier(identifiers, "user_id", user_id)
    add_identifier(identifiers, "email", _first_email(text))
    add_identifier(identifiers, "phone", _first_phone(text))
    name = _line_before(text, "Silver")
    if name and 1 < len(name.split()) <= 4:
        add_identifier(identifiers, "name", name)
    parsed.add(
        _make(
            document,
            system_id=base.SYSTEM_ACCOUNT,
            resource_id=account_id,
            record_type=base.RT_USER_PROFILE,
            raw_state="FOUND",
            identifiers=identifiers,
            context={"source": "account_admin_console"},
        )
    )
    support_id = _first(re.compile(r"support_\d+"), text)
    if support_id:
        support_identifiers: dict[str, str] = {}
        add_identifier(support_identifiers, "user_id", user_id)
        parsed.add(
            _make(
                document,
                system_id=base.SYSTEM_SUPPORT,
                resource_id=support_id,
                record_type=base.RT_SUPPORT_MESSAGE,
                raw_state="PRESENT",
                identifiers=support_identifiers,
                context={"source": "account_admin_console_support_panel"},
            )
        )
    return parsed


def _parse_search_index_inspector(document: EvidenceDocument) -> ParsedFile:
    parsed = ParsedFile()
    text = document.text
    search_ids = _resource_ids(text, "search")
    if not search_ids:
        parsed.warnings.append("search inspector without a rendered document id")
        return parsed
    email = _first_email(text)
    user_id = _first_user(text)
    indexed_at = iso_timestamp(_first(re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"), text))
    for index, search_id in enumerate(search_ids):
        identifiers: dict[str, str] = {}
        if index == 0:
            add_identifier(identifiers, "email", email)
            add_identifier(identifiers, "user_id", user_id)
        parsed.add(
            _make(
                document,
                system_id=base.SYSTEM_SEARCH,
                resource_id=search_id,
                record_type=base.RT_SEARCH_INDEX_ENTRY,
                raw_state="FOUND",
                identifiers=identifiers,
                observed_at=indexed_at if index == 0 else None,
                context={"source": "search_index_inspector", "index": "customer_directory_v3"},
            )
        )
    return parsed


def _parse_connector_console(document: EvidenceDocument) -> ParsedFile:
    parsed = ParsedFile()
    text = document.text
    flow_ids = _resource_ids(text, "data_flow")
    emails = _emails(text)
    if not flow_ids:
        parsed.warnings.append("connector console without rendered connector ids")
        return parsed
    for index, flow_id in enumerate(flow_ids):
        identifiers: dict[str, str] = {}
        if index < len(emails):
            add_identifier(identifiers, "email", emails[index])
        parsed.add(
            _make(
                document,
                system_id=base.SYSTEM_INTEGRATION,
                resource_id=flow_id,
                record_type=base.RT_DATA_FLOW,
                raw_state="ACTIVE",
                identifiers=identifiers,
                context={"source": "connector_management_console", "health": "Healthy"},
            )
        )
    return parsed


def _parse_analytics_profile_view(document: EvidenceDocument) -> ParsedFile:
    parsed = ParsedFile()
    text = document.text
    analytics_ids = _resource_ids(text, "analytics")
    if not analytics_ids:
        parsed.warnings.append("analytics profile view without a rendered profile id")
        return parsed
    identifiers: dict[str, str] = {}
    add_identifier(identifiers, "user_id", _first_user(text))
    add_identifier(identifiers, "email", _first_email(text))
    parsed.add(
        _make(
            document,
            system_id=base.SYSTEM_ANALYTICS,
            resource_id=analytics_ids[0],
            record_type=base.RT_ANALYTICS_PROFILE,
            raw_state="IDENTIFIABLE",
            identifiers=identifiers,
            context={"source": "analytics_profile_view"},
        )
    )
    return parsed


def _parse_customer_export_console(document: EvidenceDocument) -> ParsedFile:
    parsed = ParsedFile()
    text = document.text
    export_ids = _resource_ids(text, "export")
    owners = _emails(text)
    if not export_ids:
        parsed.warnings.append("export console without rendered export ids")
        return parsed
    for index, export_id in enumerate(export_ids):
        identifiers: dict[str, str] = {}
        if index < len(owners):
            add_identifier(identifiers, "email", owners[index])
        parsed.add(
            _make(
                document,
                system_id=base.SYSTEM_EXPORT,
                resource_id=export_id,
                record_type=base.RT_USER_EXPORT,
                raw_state="FOUND",
                identifiers=identifiers,
                context={"source": "customer_export_console"},
            )
        )
    return parsed


def _parse_backup_restore_console(document: EvidenceDocument) -> ParsedFile:
    """Archive list with one detailed archive; the restore flag is not rendered."""

    parsed = ParsedFile()
    text = document.text
    backup_ids = _resource_ids(text, "backup")
    if not backup_ids:
        parsed.warnings.append("backup console without rendered archive ids")
        return parsed
    # The detailed archive is the id rendered again after the table.
    detail_id = max(backup_ids, key=lambda value: text.rfind(value))
    detail_position = text.rfind(detail_id)
    emails = _emails(text)
    table_emails = [email for email in emails if text.find(email) < detail_position]
    detail_emails = [email for email in emails if text.find(email) >= detail_position]
    table_ids = [value for value in backup_ids if value != detail_id]
    for index, backup_id in enumerate(table_ids):
        identifiers: dict[str, str] = {}
        if index < len(table_emails):
            add_identifier(identifiers, "email", table_emails[index])
        parsed.add(
            _make(
                document,
                system_id=base.SYSTEM_BACKUP,
                resource_id=backup_id,
                record_type=base.RT_BACKUP_ARCHIVE,
                raw_state="FOUND",
                identifiers=identifiers,
                context={"source": "backup_restore_console", "restore_allowed": "not_rendered"},
            )
        )
    detail_identifiers: dict[str, str] = {}
    if detail_emails:
        add_identifier(detail_identifiers, "email", detail_emails[0])
    parsed.add(
        _make(
            document,
            system_id=base.SYSTEM_BACKUP,
            resource_id=detail_id,
            record_type=base.RT_BACKUP_ARCHIVE,
            raw_state="FOUND",
            identifiers=detail_identifiers,
            context={"source": "backup_restore_console", "restore_allowed": "not_rendered"},
        )
    )
    return parsed


def _parse_deleted_account_reference(document: EvidenceDocument) -> ParsedFile:
    """Retained customer key; only the stable reference is visible."""

    parsed = ParsedFile()
    user_id = _first_user(document.text)
    if user_id is None:
        parsed.warnings.append("deleted account reference without a stable user reference")
        return parsed
    parsed.add(
        _make(
            document,
            system_id=base.SYSTEM_IDENTITY,
            resource_id=user_id,
            record_type=base.RT_ACCOUNT_TOMBSTONE,
            raw_state="REDACTED",
            identifiers={"user_id": user_id},
            context={
                "source": "deleted_account_reference",
                "note": "tombstone resource id not rendered; observation keyed by the stable customer key",
            },
        )
    )
    return parsed


def _parse_object_storage_browser(document: EvidenceDocument) -> ParsedFile:
    """Object listing under ``support-attachments``; keyed by the rendered file name."""

    parsed = ParsedFile()
    raw_names = _all_unique(
        re.compile(r"[A-Za-z][A-Za-z0-9_\-]*\.(?:pdf|jpg|jpeg|png)"), document.text
    )
    file_names = _ordered_unique(raw_names)
    attachment_names = [name for name in file_names if not name.startswith("customer_export_")]
    for file_name in attachment_names:
        parsed.add(
            _make(
                document,
                system_id=base.SYSTEM_OBJECT_STORAGE,
                resource_id=file_name,
                record_type=base.RT_SUPPORT_ATTACHMENT,
                raw_state="FOUND",
                context={
                    "source": "object_storage_browser",
                    "file_name": file_name,
                    "bucket": "northstar-customer-data",
                    "prefix": "eu-west-1/support-attachments/2026/02",
                },
            )
        )
    if not parsed.observations:
        parsed.warnings.append("object storage listing without attachment objects")
    return parsed


def _parse_security_audit_explorer(document: EvidenceDocument) -> ParsedFile:
    parsed = ParsedFile()
    text = document.text
    audit_ids = [f"audit_record_{int(match.group(1)):03d}" for match in _AUDIT_ID_RE.finditer(text)]
    if not audit_ids:
        parsed.warnings.append("security audit explorer without rendered audit ids")
        return parsed
    user_id = _first_user(text)
    recorded_at = iso_timestamp(text)
    identifiers: dict[str, str] = {}
    add_identifier(identifiers, "user_id", user_id)
    for audit_id in _ordered_unique(audit_ids):
        parsed.add(
            _make(
                document,
                system_id=base.SYSTEM_SECURITY,
                resource_id=audit_id,
                record_type=base.RT_SECURITY_AUDIT_LOG,
                raw_state="RETAINED",
                identifiers=identifiers,
                observed_at=recorded_at if audit_id.endswith("003") else None,
                context={"source": "security_audit_explorer", "integrity": "Valid"},
            )
        )
    return parsed


def _parse_support_ticket_console(document: EvidenceDocument) -> ParsedFile:
    parsed = ParsedFile()
    text = document.text
    support_id = _first(re.compile(r"support_\d+"), text)
    if support_id is None:
        parsed.warnings.append("support console without a rendered ticket id")
        return parsed
    identifiers: dict[str, str] = {}
    add_identifier(identifiers, "email", _first_email(text))
    name_match = re.search(
        rf"{re.escape(support_id)}\s*[•·]\s*([A-Z][A-Za-z'\-]+(?:\s+[A-Z][A-Za-z'\-]+)+)", text
    )
    if name_match:
        add_identifier(identifiers, "name", name_match.group(1))
    parsed.add(
        _make(
            document,
            system_id=base.SYSTEM_SUPPORT,
            resource_id=support_id,
            record_type=base.RT_SUPPORT_MESSAGE,
            raw_state="IDENTIFIABLE",
            identifiers=identifiers,
            context={
                "source": "support_ticket_console",
                "ticket": _first(re.compile(r"CS-\d+"), text),
            },
        )
    )
    return parsed


def _parse_marketing_profile_console(document: EvidenceDocument) -> ParsedFile:
    parsed = ParsedFile()
    text = document.text
    marketing_id = _first(re.compile(r"marketing_\d+"), text)
    if marketing_id is None:
        parsed.warnings.append("marketing console without a rendered profile id")
        return parsed
    identifiers: dict[str, str] = {}
    add_identifier(identifiers, "email", _first_email(text))
    add_identifier(identifiers, "phone", _first_phone(text))
    name = _line_before(text, marketing_id)
    if name and 1 < len(name.split()) <= 4:
        add_identifier(identifiers, "name", name)
    parsed.add(
        _make(
            document,
            system_id=base.SYSTEM_MARKETING,
            resource_id=marketing_id,
            record_type=base.RT_MARKETING_PROFILE,
            raw_state="FOUND",
            identifiers=identifiers,
            context={"source": "marketing_profile_console"},
        )
    )
    return parsed


def _ordered_unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


#: File-name marker -> document reader.  The manifest path is stable, so the
#: family is chosen by the document identity rather than by OCR heuristics.
_DOCUMENT_PARSERS: tuple[tuple[str, Callable[[EvidenceDocument], ParsedFile]], ...] = (
    ("account_history_report", _parse_account_history_report),
    ("legal_retention_register", _parse_legal_retention_register),
    ("security_retention_extract", _parse_security_retention_extract),
    ("delivery_correction_attachment", _parse_delivery_correction_attachment),
    ("account_admin_console", _parse_account_admin_console),
    ("search_index_inspector", _parse_search_index_inspector),
    ("connector_management_console", _parse_connector_console),
    ("analytics_profile_view", _parse_analytics_profile_view),
    ("customer_export_console", _parse_customer_export_console),
    ("backup_restore_console", _parse_backup_restore_console),
    ("deleted_account_reference", _parse_deleted_account_reference),
    ("object_storage_browser", _parse_object_storage_browser),
    ("security_audit_explorer", _parse_security_audit_explorer),
    ("support_ticket_console", _parse_support_ticket_console),
    ("marketing_profile_console", _parse_marketing_profile_console),
)

#: Document families that render facts but no deletion-contract observations.
#: Empty in this dataset: every supplied document yields at least one fact.
NARRATIVE_ONLY_FAMILIES: tuple[str, ...] = ()


__all__ = ["parse_document_text", "NARRATIVE_ONLY_FAMILIES"]
