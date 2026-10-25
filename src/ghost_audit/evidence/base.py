"""Normalized evidence facts shared by every evidence parser.

Every evidence file is converted into a list of :class:`Observation` values.
An observation is one *state claim* about one resource at one point in time, as
rendered by a participant-facing evidence file.

The pipeline is deterministic from this point on: identity resolution,
point-in-time reconstruction and step planning all operate on observations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

# ---------------------------------------------------------------------------
# Systems that appear as ``system_id`` in the required output. These identifiers
# are taken verbatim from the participant-facing evidence and sample answers.
# ---------------------------------------------------------------------------
SYSTEM_ACCOUNT = "account_service"
SYSTEM_IDENTITY = "identity_service"
SYSTEM_SEARCH = "search_service"
SYSTEM_ANALYTICS = "analytics_platform"
SYSTEM_SUPPORT = "support_platform"
SYSTEM_EXPORT = "export_service"
SYSTEM_INTEGRATION = "integration_service"
SYSTEM_BACKUP = "backup_service"
SYSTEM_SECURITY = "security_service"
SYSTEM_CACHE = "cache_service"
SYSTEM_NOTIFICATION = "notification_service"
SYSTEM_MARKETING = "marketing_platform"
SYSTEM_LEGAL = "legal_service"
SYSTEM_OBJECT_STORAGE = "object_storage"

# ---------------------------------------------------------------------------
# Data categories (``record_type``). The first block is present in the supplied
# deletion policy; the rest are categories that only appear in evidence/activity
# and are mapped to the closest policy rule.
# ---------------------------------------------------------------------------
RT_USER_PROFILE = "USER_PROFILE"
RT_SECONDARY_IDENTIFIER = "SECONDARY_IDENTIFIER"
RT_SEARCH_INDEX_ENTRY = "SEARCH_INDEX_ENTRY"
RT_CACHE_ENTRY = "CACHE_ENTRY"
RT_SUPPORT_MESSAGE = "SUPPORT_MESSAGE"
RT_SUPPORT_ATTACHMENT = "SUPPORT_ATTACHMENT"
RT_ANALYTICS_PROFILE = "ANALYTICS_PROFILE"
RT_PRODUCT_TELEMETRY = "PRODUCT_TELEMETRY"
RT_USER_EXPORT = "USER_EXPORT"
RT_MARKETING_PROFILE = "MARKETING_PROFILE"
RT_EMAIL_DELIVERY_LOG = "EMAIL_DELIVERY_LOG"
RT_SECURITY_AUDIT_LOG = "SECURITY_AUDIT_LOG"
RT_LEGAL_HOLD_RECORD = "LEGAL_HOLD_RECORD"
RT_BACKUP_ARCHIVE = "BACKUP_ARCHIVE"
RT_ACCOUNT_TOMBSTONE = "ACCOUNT_TOMBSTONE"
RT_DATA_FLOW = "DATA_FLOW"

RECORD_TYPES = {
    RT_USER_PROFILE,
    RT_SECONDARY_IDENTIFIER,
    RT_SEARCH_INDEX_ENTRY,
    RT_CACHE_ENTRY,
    RT_SUPPORT_MESSAGE,
    RT_SUPPORT_ATTACHMENT,
    RT_ANALYTICS_PROFILE,
    RT_PRODUCT_TELEMETRY,
    RT_USER_EXPORT,
    RT_MARKETING_PROFILE,
    RT_EMAIL_DELIVERY_LOG,
    RT_SECURITY_AUDIT_LOG,
    RT_LEGAL_HOLD_RECORD,
    RT_BACKUP_ARCHIVE,
    RT_ACCOUNT_TOMBSTONE,
    RT_DATA_FLOW,
}

# ---------------------------------------------------------------------------
# Canonical resource states (the vocabulary used by the required operations).
# ``PRESENT`` means "the record still exists" for a record that should be gone,
# or "still carries direct identifiers" for a record that must be redacted or
# anonymized. Parsers keep the raw evidence words in ``context['raw_state']``.
# ---------------------------------------------------------------------------
ST_PRESENT = "PRESENT"
ST_ABSENT = "ABSENT"
ST_ANONYMIZED = "ANONYMIZED"
ST_REDACTED = "REDACTED"
ST_ACTIVE = "ACTIVE"
ST_DISABLED = "DISABLED"
ST_BLOCKED = "BLOCKED"
ST_RETAINED = "RETAINED"
ST_UNKNOWN = "UNKNOWN"

CANONICAL_STATES = {
    ST_PRESENT,
    ST_ABSENT,
    ST_ANONYMIZED,
    ST_REDACTED,
    ST_ACTIVE,
    ST_DISABLED,
    ST_BLOCKED,
    ST_RETAINED,
    ST_UNKNOWN,
}

#: Raw evidence words that mean "record still exists".
_RAW_PRESENT = {
    "FOUND",
    "PRESENT",
    "EXISTS",
    "IDENTIFIABLE",
    "DELETION_PENDING",
    "ACTIVE",
    "ENABLED",
    "true",
    "1",
    "200",
}
#: Raw evidence words that mean "record no longer exists".
_RAW_ABSENT = {"NOT_FOUND", "ABSENT", "MISSING", "DELETED", "ROWS=0", "0", "404", "false"}
#: Raw evidence words for compliant transformed states.
_RAW_TRANSFORMED = {
    "ANONYMIZED": ST_ANONYMIZED,
    "REDACTED": ST_REDACTED,
    "DISABLED": ST_DISABLED,
    "BLOCKED": ST_BLOCKED,
    "RETAINED": ST_RETAINED,
}
#: Raw evidence words that mean "the resource is active" (data flow / restore).
_RAW_ACTIVE_RESOURCE = {"ACTIVE", "ENABLED", "RESTORE_ALLOWED", "true", "1"}


def canonical_state(record_type: str, raw_state: str, context: Mapping[str, Any] | None = None) -> str:
    """Translate a parser-specific state word into the canonical vocabulary.

    The mapping is intentionally category aware: ``ACTIVE`` means "violation"
    for a data flow or a backup archive, but for a user profile it means the
    record is still present. Unknown words stay ``UNKNOWN`` so the planner can
    surface them instead of silently guessing.
    """

    raw = (raw_state or "").strip()
    upper = raw.upper()
    ctx = context or {}

    if record_type in (RT_DATA_FLOW,):
        if upper in _RAW_TRANSFORMED:
            return _RAW_TRANSFORMED[upper]
        if upper in _RAW_ACTIVE_RESOURCE:
            return ST_ACTIVE
        return ST_UNKNOWN
    if record_type == RT_BACKUP_ARCHIVE:
        if upper in _RAW_TRANSFORMED:
            return _RAW_TRANSFORMED[upper]
        if upper in _RAW_ACTIVE_RESOURCE:
            return ST_ACTIVE
        return ST_UNKNOWN
    if record_type in (RT_ANALYTICS_PROFILE, RT_SUPPORT_MESSAGE, RT_ACCOUNT_TOMBSTONE, RT_EMAIL_DELIVERY_LOG):
        if upper in _RAW_TRANSFORMED:
            return _RAW_TRANSFORMED[upper]
        if upper in _RAW_PRESENT:
            return ST_PRESENT
        return ST_UNKNOWN
    if record_type in (RT_SECURITY_AUDIT_LOG, RT_LEGAL_HOLD_RECORD):
        if upper in _RAW_TRANSFORMED:
            return _RAW_TRANSFORMED[upper]
        if upper in _RAW_PRESENT:
            return ST_RETAINED
        return ST_UNKNOWN
    if upper in _RAW_TRANSFORMED:
        return _RAW_TRANSFORMED[upper]
    if upper in _RAW_PRESENT:
        return ST_PRESENT
    if upper in _RAW_ABSENT:
        return ST_ABSENT
    return ST_UNKNOWN


@dataclass(frozen=True)
class Observation:
    """One state claim about one resource, rendered by one evidence file."""

    evidence_id: str
    captured_at: str
    system_id: str
    resource_id: str
    record_type: str
    state: str
    observed_at: str | None = None
    identifiers: Mapping[str, str] = field(default_factory=dict)
    context: Mapping[str, Any] = field(default_factory=dict)
    #: Filled by :mod:`ghost_audit.scenario` when the resource is linked to a subject.
    owner_user_id: str | None = None

    def with_owner(self, user_id: str | None) -> "Observation":
        return Observation(
            evidence_id=self.evidence_id,
            captured_at=self.captured_at,
            system_id=self.system_id,
            resource_id=self.resource_id,
            record_type=self.record_type,
            state=self.state,
            observed_at=self.observed_at,
            identifiers=self.identifiers,
            context=self.context,
            owner_user_id=user_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "captured_at": self.captured_at,
            "observed_at": self.observed_at,
            "system_id": self.system_id,
            "resource_id": self.resource_id,
            "record_type": self.record_type,
            "state": self.state,
            "identifiers": dict(self.identifiers),
            "context": dict(self.context),
            "owner_user_id": self.owner_user_id,
        }

    def sort_key(self) -> tuple:
        return (
            self.captured_at,
            self.observed_at or "",
            self.system_id,
            self.resource_id,
            self.record_type,
        )


def make_observation(
    *,
    evidence_id: str,
    captured_at: str,
    system_id: str,
    resource_id: str,
    record_type: str,
    raw_state: str,
    observed_at: str | None = None,
    identifiers: Mapping[str, str] | None = None,
    context: Mapping[str, Any] | None = None,
) -> Observation:
    """Convenience constructor used by every parser.

    It normalizes the raw evidence word into :data:`CANONICAL_STATES` and keeps
    the raw value plus parser extras in ``context`` for auditability.
    """

    cleaned_identifiers = {
        key: str(value).strip()
        for key, value in (identifiers or {}).items()
        if value is not None and str(value).strip() and str(value).strip().lower() not in {"none", "null", "-"}
    }
    merged_context: dict[str, Any] = dict(context or {})
    merged_context.setdefault("raw_state", raw_state)
    return Observation(
        evidence_id=evidence_id,
        captured_at=captured_at,
        system_id=system_id,
        resource_id=resource_id,
        record_type=record_type,
        state=canonical_state(record_type, raw_state, merged_context),
        observed_at=observed_at,
        identifiers=cleaned_identifiers,
        context=merged_context,
    )
