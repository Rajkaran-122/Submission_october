"""Small shared helpers used by the evidence parsers.

Everything here is deliberately plain text handling: evidence parsers receive
already-extracted text (PDF text, OCR text, raw log/SQL/JSON text) and must not
depend on the file suffix or on the manifest.  The helpers only normalize
values; they never invent facts.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

#: Placeholder words that mean "no value" in the exported evidence.
_NA_WORDS = {"", "-", "--", "none", "null", "n/a", "na", "no direct identifier"}

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")
_EMAIL_HASH_RE = re.compile(r"[0-9a-fA-F]{64}")
_USER_REF_RE = re.compile(r"user_\d+")
_RESOURCE_ID_RE = re.compile(r"[a-z][a-z_]*_\d+")
_TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.\d+)?Z?")
_PHONE_RE = re.compile(r"\+?[0-9][0-9\s().\-]{6,}[0-9]")

_FIELD_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)=("(?:[^"\\]|\\.)*"|\S+)')


def clean_optional(value: Any) -> str | None:
    """Return a stripped value, or ``None`` for placeholder/empty values."""

    if value is None:
        return None
    text = str(value).strip()
    if text.lower() in _NA_WORDS:
        return None
    return text


def iso_timestamp(value: Any) -> str | None:
    """Normalize a timestamp to ``YYYY-MM-DDTHH:MM:SSZ``.

    The evidence uses UTC timestamps only.  ``None`` is returned when the text
    does not contain a recognisable timestamp, so callers can distinguish
    "not rendered" from a made-up value.
    """

    if value is None:
        return None
    match = _TS_RE.search(str(value))
    if not match:
        return None
    return f"{match.group(1)}T{match.group(2)}Z"


def looks_like_email(value: str) -> bool:
    return bool(_EMAIL_RE.fullmatch(value.strip()))


def looks_like_email_hash(value: str) -> bool:
    return bool(_EMAIL_HASH_RE.fullmatch(value.strip()))


def looks_like_user_ref(value: str) -> bool:
    return bool(_USER_REF_RE.fullmatch(value.strip()))


def looks_like_phone(value: str) -> bool:
    text = value.strip()
    return bool(_PHONE_RE.fullmatch(text)) and sum(ch.isdigit() for ch in text) >= 7


def truthy(value: Any) -> bool | None:
    """Interpret the evidence's boolean spellings; ``None`` when unclear."""

    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "enabled", "active", "allowed", "restore_allowed"}:
        return True
    if text in {"false", "0", "no", "disabled", "blocked", "stopped", "denied", "deny"}:
        return False
    return None


def flag_word(value: Any, *, when_true: str, when_false: str) -> str | None:
    """Map a boolean-ish evidence value onto a raw state word."""

    state = truthy(value)
    if state is None:
        return None
    return when_true if state else when_false


def canonical_identifier_target(key: str, value: str) -> str:
    """Choose the canonical identifier key for ``key=value``.

    The evidence renders the same identifier under many field names
    (``lookup_key``, ``correlation_key``, ``owner_email`` ...).  The value shape
    decides the canonical key first, the exact field name only as a fallback.
    """

    text = value.strip()
    if looks_like_email(text):
        return "email"
    if looks_like_email_hash(text):
        return "email_hash"
    if looks_like_user_ref(text):
        return "user_id"
    if looks_like_phone(text):
        return "phone"
    lowered = key.lower()
    if lowered in {"name", "full_name", "display_name", "requester_name"}:
        return "name"
    if lowered in {"phone", "owner_phone", "requester_phone"}:
        return "phone"
    if lowered in {"email_hash"}:
        return "email_hash"
    if lowered in {"customer_reference", "subject_reference", "subject_ref", "user_id"}:
        return "customer_reference"
    return lowered


#: Field names that carry a subject identifier even when the value is not
#: recognisable by shape (for example a display name).
_IDENTIFIER_FIELD_HINTS = {
    "name",
    "full_name",
    "display_name",
    "requester_name",
    "email",
    "owner_email",
    "customer_email",
    "requester_email",
    "phone",
    "owner_phone",
    "requester_phone",
    "email_hash",
    "customer_reference",
    "subject_reference",
    "subject_ref",
    "user_id",
    "lookup_key",
    "match_key",
    "subject_key",
    "scan_key",
    "owner_key",
    "correlation_key",
    "lookup_value",
    "matched_value",
}


def add_identifier(target: dict[str, str], key: str, value: Any) -> None:
    """Add one identifier to ``target`` without overwriting an existing value.

    A field is kept when its value is recognisably an identifier (email, email
    hash, user reference, phone number) or when the field name is a known
    identifier field.  Ordinary rendered attributes (``audience_cohort``,
    ``message_excerpt``, ``connector_name`` ...) are ignored so ``identifiers``
    stays linkable and does not make a record look identifiable.
    """

    text = clean_optional(value)
    if text is None:
        return
    canonical = canonical_identifier_target(key, text)
    if canonical not in {"email", "email_hash", "user_id", "phone", "name", "customer_reference"}:
        if key.lower() not in _IDENTIFIER_FIELD_HINTS:
            return
    target.setdefault(canonical, text)


def identifiers_from_mapping(mapping: Mapping[str, Any]) -> dict[str, str]:
    """Extract canonical identifiers from one evidence record."""

    result: dict[str, str] = {}
    for key, value in mapping.items():
        if isinstance(value, (dict, list, tuple)):
            continue
        add_identifier(result, key, value)
    return result


def identifiers_from_text(text: str, *, users: bool = True) -> dict[str, str]:
    """Extract the identifiers visible in a free-text document.

    Used for PDF statements and screenshot OCR where the layout is already
    broken into lines: only values literally present in the text are returned.
    """

    result: dict[str, str] = {}
    for match in _EMAIL_RE.finditer(text):
        add_identifier(result, "email", match.group(0))
    if users:
        for match in _USER_REF_RE.finditer(text):
            add_identifier(result, "user_id", match.group(0))
    for match in _PHONE_RE.finditer(text):
        add_identifier(result, "phone", match.group(0))
    return result


def parse_kv_pairs(text: str) -> dict[str, str]:
    """Parse ``key=value`` pairs from a log line fragment.

    Handles quoted values (including escaped quotes), bare values and
    ``key="a b"``.  Unknown fragments are ignored instead of raising.
    """

    fields: dict[str, str] = {}
    for match in _FIELD_RE.finditer(text):
        key = match.group(1)
        raw = match.group(2)
        if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
            value = raw[1:-1].replace('\\"', '"')
        else:
            value = raw
        fields[key] = value
    return fields


