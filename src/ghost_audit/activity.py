"""Index of the global operational activity stream.

``system_activity.csv`` is a global stream that mixes unrelated people, noise
events and the deletion workflow under audit. It is used for two jobs:

1. **Link a case to its resources.** ``reported_at`` is (approximately) the
   moment the deletion workflow reported its final result, so the workflow's
   events for one case cluster around it. The event stream therefore gives the
   resource ids (account, search entry, analytics profile, support record,
   export, connector, backup entry) that belong to the case even when the
   evidence packet renders a resource without direct identifiers.

2. **Link a case to its data flows / restore paths / classifications** via
   ``CHECK_DATA_FLOW``, ``CHECK_RESTORE_ACCESS``, ``CLASSIFY_REQUEST``,
   ``LINK_RESOURCE_IDENTITY`` and ``RESTORE_BACKUP`` events.

Claims are not proof: ``reported_result`` is never trusted as the final state.
The activity stream only contributes *resource ids and event times*; the state
comes from evidence observations.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .dataset import Activity, Dataset, parse_time

#: Event types that belong to a deletion workflow and therefore identify the
#: case's resources. Deliberately excludes noise (index/cache/update/email).
WORKFLOW_EVENTS = {
    "DELETE_RECORD",
    "ANONYMIZE_RECORD",
    "REDACT_RECORD",
    "DISABLE_DATA_FLOW",
    "BLOCK_RESTORE",
    "CHECK_DATA_FLOW",
    "CHECK_RESTORE_ACCESS",
    "RETAIN_RECORD",
    "SYNC_RECORD",
}

#: Events that explicitly link a case (through time) to a resource even though
#: the resource is not deleted.
LINK_EVENTS = {"CLASSIFY_REQUEST", "LINK_RESOURCE_IDENTITY", "RESTORE_BACKUP"}

#: A workflow's events may finish a little after the workflow claims completion.
WINDOW_BEFORE_HOURS = 96
WINDOW_AFTER_HOURS = 16


@dataclass
class ResourceActivity:
    resource_id: str
    event_types: set[str] = field(default_factory=set)
    events: list[Activity] = field(default_factory=list)
    min_distance_seconds: float = float("inf")


@dataclass
class CaseActivity:
    case_id: str
    resources: dict[str, ResourceActivity] = field(default_factory=dict)
    classification_events: list[Activity] = field(default_factory=list)
    link_events: list[Activity] = field(default_factory=list)
    restore_events: list[Activity] = field(default_factory=list)


class ActivityIndex:
    def __init__(self, dataset: Dataset):
        self.activities = dataset.activities
        self.by_resource: dict[str, list[Activity]] = defaultdict(list)
        for activity in self.activities:
            self.by_resource[activity.resource_id].append(activity)
        for items in self.by_resource.values():
            items.sort(key=lambda a: a.event_time)

    # -- helpers ------------------------------------------------------------
    def events_for(self, resource_id: str) -> list[Activity]:
        return self.by_resource.get(resource_id, [])

    def case_activity(self, case_id: str, requested_at: str, reported_at: str) -> CaseActivity:
        """Collect workflow events plausibly belonging to one case.

        The window is anchored on the workflow claim time and widened towards
        the request time; events are ranked by distance from ``reported_at``.
        The result is a *candidate* set; final assignment is refined in
        :mod:`ghost_audit.scenario` using evidence anchors and uniqueness.
        """

        request_dt = parse_time(requested_at)
        report_dt = parse_time(reported_at)
        result = CaseActivity(case_id=case_id)
        for activity in self.activities:
            event_dt = activity.event_dt
            if activity.event_type in LINK_EVENTS:
                distance = abs((event_dt - report_dt).total_seconds())
                if distance <= WINDOW_AFTER_HOURS * 3600 and event_dt >= request_dt:
                    result.link_events.append(activity)
                    if activity.event_type == "CLASSIFY_REQUEST":
                        result.classification_events.append(activity)
                    if activity.event_type == "RESTORE_BACKUP":
                        result.restore_events.append(activity)
                continue
            if activity.event_type not in WORKFLOW_EVENTS:
                continue
            if event_dt < request_dt or event_dt > report_dt.replace(microsecond=0):
                # Allow a small tolerance after the claim: datasets show the
                # final operation result landing seconds after the claim.
                if not (
                    event_dt > report_dt
                    and (event_dt - report_dt).total_seconds() <= WINDOW_AFTER_HOURS * 3600
                ):
                    continue
            if (report_dt - event_dt).total_seconds() > WINDOW_BEFORE_HOURS * 3600:
                continue
            if activity.source_system not in (
                "deletion_worker",
                "security_service",
                "backup_service",
                "integration_service",
                "privacy_intake",
            ):
                # Noise from account/profile/notification services is not part
                # of the deletion workflow.
                continue
            distance = abs((event_dt - report_dt).total_seconds())
            bundle = result.resources.setdefault(activity.resource_id, ResourceActivity(activity.resource_id))
            bundle.event_types.add(activity.event_type)
            bundle.events.append(activity)
            bundle.min_distance_seconds = min(bundle.min_distance_seconds, distance)
        for bundle in result.resources.values():
            bundle.events.sort(key=lambda a: a.event_time)
        return result

    def security_audit_records_for(self, user_id: str) -> list[Activity]:
        """RETAIN_RECORD events never name the subject; kept for completeness."""

        return [a for a in self.activities if a.event_type == "RETAIN_RECORD" and a.resource_id.startswith("audit_record_")]
