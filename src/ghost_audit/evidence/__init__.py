"""Evidence parsing layer for the Ghost Data Still Exists audit.

Every participant-facing evidence file is converted into normalized
:class:`~ghost_audit.evidence.base.Observation` facts.  The layer is
deterministic and needs no model calls: it reads the manifest file id, the
manifest capture time, and the file text (PDF text / OCR text included), then
applies one shared mapping per format family.

Public API
----------

``parse_all(documents) -> ParseResult``
    Parse ``Dataset.documents`` in one call.  The result carries the
    observations, per-file coverage (which system/category pairs a file
    renders, so absence can be inferred from a whole-table capture), file
    families, warnings and family specific metadata.

``parse_document(document) -> ParsedFile``
    Parse a single document (observations + coverage + warnings + metadata).

``classify_document(document) / classify_path(path) -> str``
    Evidence format family of a file, without parsing it.

``family_summary(result) -> dict``
    Files/observations/warnings per family (reports and tests).

``sql_table_summary(document) -> dict``
    SQL snapshot topology, in-file capture time and per-table row counts /
    resource ids (used by the scenario layer for absence reasoning).

The :class:`Observation` fields and the canonical state vocabulary live in
:mod:`ghost_audit.evidence.base`; ``observed_at`` comes from inside the
evidence file when the file renders a timestamp and ``captured_at`` is always
the manifest capture time.
"""

from __future__ import annotations

from .base import (
    CANONICAL_STATES,
    RECORD_TYPES,
    Observation,
    canonical_state,
    make_observation,
)
from .registry import (
    ParseResult,
    classify_document,
    classify_path,
    family_summary,
    parse_all,
    parse_document,
)
from .result import ParsedFile
from .sql_dump import sql_table_summary

__all__ = [
    "CANONICAL_STATES",
    "RECORD_TYPES",
    "Observation",
    "ParseResult",
    "ParsedFile",
    "canonical_state",
    "classify_document",
    "classify_path",
    "family_summary",
    "make_observation",
    "parse_all",
    "parse_document",
    "sql_table_summary",
]
