"""Container types shared by the evidence parsers and the registry."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .base import Observation


@dataclass
class ParsedFile:
    """Everything one evidence file yields: observations plus absence coverage.

    ``coverage`` records which system/record-type pairs the file *renders*.
    When a later stage knows that a resource belongs to one of those
    system/category pairs and the resource is not among the file's rendered
    records (and no explicit NOT_FOUND row was emitted), the file can be used
    as absence evidence for that resource.  A file with no rendered records for
    a category (an empty snapshot table) still covers it, which is why coverage
    is tracked separately from observations.
    """

    observations: list[Observation] = field(default_factory=list)
    coverage: dict[str, set[str]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def cover(self, system_id: str, record_type: str) -> None:
        """Mark one system/category pair as rendered by this file."""

        self.coverage.setdefault(system_id, set()).add(record_type)

    def add(self, observation: Observation | None) -> None:
        if observation is None:
            return
        self.observations.append(observation)
        self.cover(observation.system_id, observation.record_type)
