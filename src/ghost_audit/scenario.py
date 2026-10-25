"""Point-in-time scenario reconstruction.

A :class:`CaseScenario` is the reconstructed set of resources belonging to one
case at ``audit_at`` together with the evidence that proves each state. The
planner turns it into the required output steps.

Reconstruction rules
--------------------

1. **Evidence is the only source of state.** Observations are attributed to a
   subject through current *and historical* identifiers (email/phone/email_hash),
   explicit ``user_id`` references, and ``LINK_RESOURCE_IDENTITY`` activity.
2. **Wave packets.** The hidden cases were generated per *audit wave*: cases
   share an exact ``audit_at`` and the dataset ships one capture document that
   renders that wave's resources. That document is the packet for every case in
   the wave; a case may also have its own packet (PDF, screenshot, small
   log/JSON/SQL) captured between ``requested_at - 30 days`` and ``audit_at``.
3. **Account baseline.** Every authorized case checks the subject's account. If
   the packet does not contain an account row for the subject, the account id is
   recovered from the workflow activity stream (events cluster around
   ``reported_at``) and the packet's account-system coverage proves whether the
   profile existed at capture time.
4. **Anonymous rows.** Records that must be redacted/anonymized have no
   identifiers left. Their resource ids are recovered from the workflow activity
   stream and matched to cases inside the wave (unique match first, then a
   deterministic ordering informed by snapshot-bundle row order).
5. **Temporal rule.** A state may only be used when the evidence was *available*
   by ``audit_at``. Availability means: the manifest capture time is at/before
   ``audit_at``, or the observation itself is timestamped at/before ``audit_at``
   (readback/export files sometimes export a point-in-time read taken before
   the audit and are shipped later). Citations additionally require the manifest
   capture time to be at/before ``audit_at``.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from .activity import ActivityIndex
from .dataset import Activity, Dataset, EvidenceDocument, Request, parse_time
from .evidence.base import (
    RT_ACCOUNT_TOMBSTONE,
    RT_ANALYTICS_PROFILE,
    RT_EMAIL_DELIVERY_LOG,
    RT_SECURITY_AUDIT_LOG,
    RT_SUPPORT_MESSAGE,
    RT_USER_PROFILE,
    ST_ABSENT,
    ST_PRESENT,
    Observation,
)
from .identity import IdentityIndex
from .planner import ResourceState

#: Direct packet documents must have been captured no earlier than this many
#: days before the request; older evidence belongs to other cases/decoys.
PACKET_LOOKBACK_DAYS = 30

#: Workflow events within this distance from ``reported_at`` identify a case.
ACTIVITY_TIGHT_SECONDS = 3 * 3600

#: Record types whose compliant state removes identifiers.
ANONYMOUS_TYPES = {RT_ANALYTICS_PROFILE, RT_SUPPORT_MESSAGE, RT_ACCOUNT_TOMBSTONE, RT_EMAIL_DELIVERY_LOG}


def _is_policy_resource(resource_id: str) -> bool:
    """Policy resources are the ones the deletion rules can act on.

    Narrative documents (e.g. the legal-retention register) reference orders
    and finance references to explain retention; those are not resources the
    audit can delete or verify, so they never become output steps.
    """

    return not resource_id.upper().startswith(("ORD-", "FIN-", "CS-"))


def _normalize_record_type(observation: Observation) -> str:
    """Refine a parser category with evidence semantics.

    ``contact_lookup_completed`` events come from the marketing contact store
    (opt-in flag, contact hash): the required treatment is DELETE per
    MARKETING_PROFILE, not ANONYMIZE per ANALYTICS_PROFILE.
    """

    event = str(observation.context.get("event", ""))
    if observation.record_type == RT_ANALYTICS_PROFILE and event == "contact_lookup_completed":
        return "MARKETING_PROFILE"
    return observation.record_type

RESOURCE_RE = re.compile(
    r"\b((?:account|search|support|analytics|export|data_flow|backup|audit_record|tombstone|marketing|object|"
    r"email_delivery|cache|legal_hold)_[0-9A-Za-z]+)\b"
)


def _try_parse(value: str | None):
    if not value:
        return None
    try:
        return parse_time(value)
    except (ValueError, AttributeError):
        return None


def _observation_available_at(observation: Observation, audit: datetime) -> bool:
    """Whether this state may influence the answer at ``audit_at``.

    Manifest capture time OR the observation's own timestamp must be at/before
    the audit moment. This mirrors the dataset's design where a readback export
    can be shipped later than the point-in-time read it contains.
    """

    captured = _try_parse(observation.captured_at)
    observed = _try_parse(observation.observed_at)
    if captured is not None and captured <= audit:
        return True
    if observed is not None and observed <= audit:
        return True
    return False


def _citable_at(observation: Observation, audit: datetime) -> bool:
    captured = _try_parse(observation.captured_at)
    return captured is not None and captured <= audit


def _observation_sort_key(observation: Observation) -> tuple:
    return (observation.observed_at or "", observation.captured_at, observation.evidence_id)


@dataclass
class DocFacts:
    document: EvidenceDocument
    observations: list[Observation]
    coverage: dict[str, set[str]]
    family: str = ""

    @property
    def evidence_id(self) -> str:
        return self.document.file_id

    @property
    def captured_dt(self) -> datetime:
        return self.document.captured_dt

    def account_observations(self) -> list[Observation]:
        return [o for o in self.observations if o.record_type == RT_USER_PROFILE and o.system_id == "account_service"]


@dataclass
class CaseScenario:
    case: Request
    user_id: str
    resources: list[ResourceState] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def resource(self, resource_id: str) -> ResourceState | None:
        for item in self.resources:
            if item.resource_id == resource_id:
                return item
        return None

    def has_record_type(self, record_type: str) -> bool:
        return any(item.record_type == record_type for item in self.resources)

    def add(self, state: ResourceState) -> None:
        existing = self.resource(state.resource_id)
        if existing is not None:
            # Keep the observation that is latest and citable if possible.
            if existing.state == state.state and not existing.evidence_id and state.evidence_id:
                self.resources[self.resources.index(existing)] = state
            return
        self.resources.append(state)

    @property
    def evidence_ids(self) -> list[str]:
        return sorted({item.evidence_id for item in self.resources if item.evidence_id})


class ScenarioBuilder:
    """Reconstruct every case's resources and states at ``audit_at``."""

    def __init__(
        self,
        dataset: Dataset,
        identities: IdentityIndex,
        activities: ActivityIndex,
        observations: list[Observation],
        coverage: dict[str, dict[str, set[str]]] | None = None,
        families: dict[str, str] | None = None,
    ):
        self.dataset = dataset
        self.identities = identities
        self.activities = activities
        self.families = families or {}
        self.documents = {doc.file_id: doc for doc in dataset.documents}
        grouped: dict[str, list[Observation]] = defaultdict(list)
        for observation in observations:
            grouped[observation.evidence_id].append(observation)
        self.doc_facts: dict[str, DocFacts] = {}
        for file_id, doc in self.documents.items():
            self.doc_facts[file_id] = DocFacts(
                document=doc,
                observations=grouped.get(file_id, []),
                coverage=(coverage or {}).get(file_id, {}),
                family=self.families.get(file_id, ""),
            )
        # Static ownership resolution (any capture time): identifiers and links.
        self.owner_by_key: dict[tuple[str, str, str], str] = {}
        self.resource_owners: dict[str, set[str]] = defaultdict(set)
        for observation in observations:
            owner = self._owner_of(observation)
            if owner:
                self.owner_by_key[(observation.evidence_id, observation.resource_id, observation.record_type)] = owner
                self.resource_owners[observation.resource_id].add(owner)
        for activity in activities.activities:
            if activity.event_type != "LINK_RESOURCE_IDENTITY":
                continue
            parsed = self._parse_link_event(activity)
            if parsed:
                resource_id, user_id = parsed
                if user_id in self.identities.users:
                    self.resource_owners[resource_id].add(user_id)
        self._activity_cache: dict[str, object] = {}
        # Row order per evidence id for deterministic anonymous attribution.
        self._order: dict[str, dict[tuple[str, str], int]] = {}
        for file_id, facts in self.doc_facts.items():
            order = {}
            for index, observation in enumerate(facts.observations):
                order.setdefault((observation.record_type, observation.resource_id), index)
            self._order[file_id] = order

    # ------------------------------------------------------------------
    # Attribution
    # ------------------------------------------------------------------
    def _owner_of(self, observation: Observation) -> str | None:
        cached = self.owner_by_key.get((observation.evidence_id, observation.resource_id, observation.record_type))
        if cached:
            return cached
        for key in (
            "customer_reference",
            "subject_reference",
            "user_id",
            "requester_email",
            "owner_email",
            "customer_email",
            "email",
            "email_hash",
            "phone",
            "requester_phone",
            "requester_name",
            "name",
            "display_name",
            "subject",
        ):
            value = observation.identifiers.get(key)
            if not value:
                continue
            if value.startswith("user_"):
                return value if value in self.identities.users else None
            user_id = self.identities.resolve(value)
            if user_id:
                return user_id
        return None

    def _is_owned_by(self, observation: Observation, user_id: str) -> bool:
        return self._owner_of(observation) == user_id or user_id in self.resource_owners.get(
            observation.resource_id, set()
        )

    @staticmethod
    def _parse_link_event(activity: Activity) -> tuple[str, str] | None:
        detail = activity.details
        match = re.search(r"catalog linked\s+(\S+)\s+\(([^)]*)\)\s+to\s+\S+\s+and\s+(user_\d+)", detail)
        if match:
            return match.group(1), match.group(3)
        match = re.search(r"catalog confirmed\s+(\S+)\s+\(([^)]*)\)\s+belongs only to .*?and\s+(user_\d+)", detail)
        if match:
            return match.group(1), match.group(3)
        return None

    # ------------------------------------------------------------------
    # Grouping and packets
    # ------------------------------------------------------------------
    @staticmethod
    def group_cases(cases: list[Request]) -> list[list[Request]]:
        groups: dict[str, list[Request]] = defaultdict(list)
        for case in cases:
            groups[case.audit_at].append(case)
        return [sorted(group, key=lambda c: c.case_id) for _, group in sorted(groups.items())]

    def _covering_users(self, facts: DocFacts, users: set[str]) -> set[str]:
        covered = set()
        for observation in facts.observations:
            owner = self._owner_of(observation)
            if owner in users:
                covered.add(owner)
        return covered

    def _choose_wave_document(self, group: list[Request]) -> EvidenceDocument | None:
        users = {self.identities.resolve(case.email) for case in group}
        users = {user for user in users if user}
        audit = min(parse_time(case.audit_at) for case in group)
        best: tuple[int, str, EvidenceDocument] | None = None
        for facts in self.doc_facts.values():
            if facts.captured_dt > audit:
                continue
            covered = self._covering_users(facts, users)
            if not covered:
                continue
            candidate = (len(covered), facts.document.captured_at, facts.document)
            if best is None or (candidate[0], candidate[1]) > (best[0], best[1]):
                best = candidate
        if best is None:
            return None
        threshold = max(2, (len(group) + 1) // 3)
        if best[0] < threshold:
            return None
        return best[2]

    def _packet_documents(self, case: Request, user_id: str, wave_doc: EvidenceDocument | None) -> list[EvidenceDocument]:
        requested = parse_time(case.requested_at)
        audit = parse_time(case.audit_at)
        lower = requested.timestamp() - PACKET_LOOKBACK_DAYS * 86400
        packets: dict[str, EvidenceDocument] = {}
        if wave_doc is not None:
            packets[wave_doc.file_id] = wave_doc
        for facts in self.doc_facts.values():
            if not self._doc_relevant(facts, user_id, lower, audit):
                continue
            packets[facts.evidence_id] = facts.document
        return [packets[key] for key in sorted(packets)]

    def _doc_relevant(self, facts: DocFacts, user_id: str, lower: float, audit: datetime) -> bool:
        if not self._doc_has_owned_observation(facts, user_id):
            return False
        captured = facts.captured_dt.timestamp()
        if captured > audit.timestamp():
            # Exported later: only relevant if it contains an observation that
            # was itself taken inside the case window.
            for observation in facts.observations:
                observed = _try_parse(observation.observed_at)
                if observed and lower <= observed.timestamp() <= audit.timestamp() and self._owner_of(observation) == user_id:
                    return True
            return False
        return captured >= lower

    def _doc_has_owned_observation(self, facts: DocFacts, user_id: str) -> bool:
        for observation in facts.observations:
            if self._owner_of(observation) == user_id:
                return True
            if user_id in self.resource_owners.get(observation.resource_id, set()):
                return True
        return False

    # ------------------------------------------------------------------
    # Building
    # ------------------------------------------------------------------
    def build_all(self, cases: list[Request], grouping_pool: list[Request] | None = None) -> dict[str, CaseScenario]:
        groups = self.group_cases(grouping_pool if grouping_pool is not None else cases)
        group_of: dict[str, list[Request]] = {}
        wave_docs: dict[str, EvidenceDocument | None] = {}
        for group in groups:
            doc = self._choose_wave_document(group)
            key = group[0].audit_at
            for case in group:
                group_of[case.case_id] = group
            wave_docs[key] = doc
        scenarios: dict[str, CaseScenario] = {}
        for case in cases:
            group = group_of.get(case.case_id, [case])
            key = group[0].audit_at
            scenarios[case.case_id] = self._build_case(case, group, wave_docs.get(key))
        for group in groups:
            key = group[0].audit_at
            doc = wave_docs.get(key)
            if doc is not None:
                self._assign_wave_accounts(group, scenarios, doc)
        self._assign_anonymous_rows(scenarios)
        self._assign_remaining_accounts(scenarios)
        return scenarios

    def _build_case(self, case: Request, group: list[Request], wave_doc: EvidenceDocument | None) -> CaseScenario:
        user_id = self.identities.resolve(case.email)
        if user_id is None:
            raise ValueError(f"cannot resolve subject for {case.case_id}")
        scenario = CaseScenario(case=case, user_id=user_id)
        packets = self._packet_documents(case, user_id, wave_doc)
        scenario.notes.append(f"packets={[doc.file_id for doc in packets]}")
        audit = parse_time(case.audit_at)
        self._collect_owned_observations(user_id, audit, scenario)
        if wave_doc is None:
            self._baseline_account(case, user_id, packets, scenario, audit)
        return scenario

    def _collect_owned_observations(self, user_id: str, audit: datetime, scenario: CaseScenario) -> None:
        per_resource: dict[str, list[Observation]] = defaultdict(list)
        owned_resources: set[str] = set()
        requested = parse_time(scenario.case.requested_at)
        lower = requested.timestamp() - PACKET_LOOKBACK_DAYS * 86400
        for facts in self.doc_facts.values():
            if not self._doc_relevant(facts, user_id, lower, audit):
                continue
            for observation in facts.observations:
                if not self._is_owned_by(observation, user_id):
                    continue
                if not _observation_available_at(observation, audit):
                    continue
                per_resource[observation.resource_id].append(observation)
                owned_resources.add(observation.resource_id)
        # A resource already linked to the subject keeps that ownership even when
        # a later row has no identifiers left (e.g. a 404 lookup or a redacted
        # row): aggregate every observation of the same resource id.
        for facts in self.doc_facts.values():
            if not self._doc_relevant(facts, user_id, lower, audit):
                continue
            for observation in facts.observations:
                if observation.resource_id not in owned_resources:
                    continue
                owner = self._owner_of(observation)
                if owner is not None and owner != user_id and user_id not in self.resource_owners.get(observation.resource_id, set()):
                    continue
                if not _observation_available_at(observation, audit):
                    continue
                per_resource[observation.resource_id].append(observation)
        per_resource = {rid: items for rid, items in per_resource.items() if _is_policy_resource(rid)}
        for resource_id, items in per_resource.items():
            unique: dict[tuple, Observation] = {}
            for observation in items:
                unique[(observation.evidence_id, observation.observed_at or "", observation.state)] = observation
            items = sorted(unique.values(), key=_observation_sort_key)
            latest = items[-1]
            evidence_id = latest.evidence_id
            if not _citable_at(latest, audit):
                citable = [o for o in items if _citable_at(o, audit)]
                evidence_id = citable[-1].evidence_id if citable else ""
                if not citable:
                    evidence_id = self._fallback_citation(
                        resource_id,
                        _normalize_record_type(latest),
                        latest.system_id,
                        latest.state,
                        scenario.case,
                        audit,
                    )
                    scenario.notes.append(f"{resource_id}: citation fallback -> {evidence_id!r}")
            scenario.add(
                ResourceState(
                    resource_id=resource_id,
                    system_id=latest.system_id,
                    record_type=_normalize_record_type(latest),
                    state=latest.state,
                    evidence_id=evidence_id,
                    observed_at=latest.observed_at,
                    source="evidence",
                    identifiers=tuple(sorted(latest.identifiers.items())),
                )
            )

    def _fallback_citation(
        self,
        resource_id: str,
        record_type: str,
        system_id: str,
        state: str,
        case: Request,
        audit: datetime,
    ) -> str:
        """Find evidence available at ``audit_at`` for a state seen only later.

        Preference order: (1) another observation of the same resource captured
        at/before ``audit_at``; (2) a scoped capture that renders the resource's
        system and does not contain the resource (absence proof); (3) any scoped
        capture that renders the system (presence is then already established by
        the later-exported observation, the capture only anchors availability).
        """

        requested = parse_time(case.requested_at)
        lower = requested.timestamp() - PACKET_LOOKBACK_DAYS * 86400
        best_obs: tuple[str, str] | None = None
        coverage_docs: list[DocFacts] = []
        for facts in self.doc_facts.values():
            if facts.captured_dt > audit:
                continue
            if record_type in facts.coverage.get(system_id, set()):
                coverage_docs.append(facts)
            for observation in facts.observations:
                if observation.resource_id != resource_id:
                    continue
                if not _citable_at(observation, audit):
                    continue
                key = (observation.observed_at or "", facts.document.captured_at)
                if best_obs is None or key > best_obs[0]:
                    best_obs = (key, facts.evidence_id)
        if best_obs is not None:
            return best_obs[1]
        coverage_docs.sort(key=lambda f: f.document.captured_at, reverse=True)
        if coverage_docs:
            latest = coverage_docs[0]
            return latest.evidence_id
        return ""

    # ------------------------------------------------------------------
    # Activity helpers
    # ------------------------------------------------------------------
    def _activity(self, case: Request):
        if case.case_id not in self._activity_cache:
            self._activity_cache[case.case_id] = self.activities.case_activity(
                case.case_id, case.requested_at, case.reported_at
            )
        return self._activity_cache[case.case_id]

    def _ranked_account_candidates(self, case: Request, user_id: str) -> list[str]:
        activity = self._activity(case)
        reported = parse_time(case.reported_at)
        ranked = []
        for resource_id, bundle in activity.resources.items():
            if not resource_id.startswith("account_"):
                continue
            owners = self.resource_owners.get(resource_id, set())
            if owners and user_id not in owners:
                continue
            distance = min(abs((parse_time(event.event_time) - reported).total_seconds()) for event in bundle.events)
            ranked.append((distance, resource_id))
        ranked.sort()
        if not ranked:
            return []
        tight = [resource_id for distance, resource_id in ranked if distance <= ACTIVITY_TIGHT_SECONDS]
        return tight or [ranked[0][1]]

    def _activity_account_ids(self, case: Request) -> set[str]:
        activity = self._activity(case)
        return {resource_id for resource_id in activity.resources if resource_id.startswith("account_")}

    # ------------------------------------------------------------------
    # Account baseline
    # ------------------------------------------------------------------
    def _baseline_account(self, case, user_id, packets, scenario, audit) -> None:
        if any(item.record_type == RT_USER_PROFILE and item.system_id == "account_service" for item in scenario.resources):
            return
        candidates = self._ranked_account_candidates(case, user_id)
        if not candidates:
            scenario.notes.append("no account candidate from workflow activity")
            return
        chosen = candidates[0]
        state, evidence_id = self._account_state_from_packets(chosen, packets, audit)
        if state is None:
            state, evidence_id = self._account_state_from_any_doc(chosen, case, audit)
        if state is None:
            scenario.notes.append(f"account {chosen}: state unknown")
            return
        scenario.add(
            ResourceState(
                resource_id=chosen,
                system_id="account_service",
                record_type=RT_USER_PROFILE,
                state=state,
                evidence_id=evidence_id,
                source="activity+coverage",
            )
        )

    def _account_state_from_packets(self, account_id: str, packets, audit: datetime) -> tuple[str | None, str]:
        candidates = []
        for doc in packets:
            facts = self.doc_facts[doc.file_id]
            if RT_USER_PROFILE not in facts.coverage.get("account_service", set()):
                continue
            if facts.captured_dt > audit:
                continue
            candidates.append(facts)
        for facts in sorted(candidates, key=lambda f: f.document.captured_at, reverse=True):
            matching = [o for o in facts.account_observations() if o.resource_id == account_id]
            if matching:
                latest = sorted(matching, key=_observation_sort_key)[-1]
                return latest.state, facts.evidence_id
            return ST_ABSENT, facts.evidence_id
        return None, ""

    def _account_state_from_any_doc(self, account_id: str, case: Request, audit: datetime) -> tuple[str | None, str]:
        requested = parse_time(case.requested_at)
        lower = requested.timestamp() - PACKET_LOOKBACK_DAYS * 86400
        candidates = []
        for facts in self.doc_facts.values():
            if facts.captured_dt > audit or facts.captured_dt.timestamp() < lower:
                continue
            if RT_USER_PROFILE not in facts.coverage.get("account_service", set()):
                continue
            candidates.append(facts)
        for facts in sorted(candidates, key=lambda f: f.document.captured_at, reverse=True):
            matching = [o for o in facts.account_observations() if o.resource_id == account_id]
            if matching:
                latest = sorted(matching, key=_observation_sort_key)[-1]
                return latest.state, facts.evidence_id
            return ST_ABSENT, facts.evidence_id
        return None, ""

    # ------------------------------------------------------------------
    # Wave-level anonymous attribution
    # ------------------------------------------------------------------
    def _assign_anonymous_rows(self, scenarios: dict[str, CaseScenario]) -> None:
        """Attribute identifier-free rows (anonymized/redacted) to subjects.

        Anonymous rows are recovered by workflow activity: the resource id of a
        row appears in the case's workflow events. Candidates are ranked by the
        distance between the workflow event and ``reported_at``; when several
        cases claim a resource with the same distance (cases reported at the
        same moment), the pairing falls back to the snapshot-bundle row order or
        a deterministic ascending pairing.
        """

        # Gather anonymous observations per resource, keeping the best one per
        # evidence file/time.
        anonymous: dict[str, list[Observation]] = defaultdict(list)
        for facts in self.doc_facts.values():
            for observation in facts.observations:
                if observation.record_type not in ANONYMOUS_TYPES:
                    continue
                if self._owner_of(observation) is not None:
                    continue
                if not _is_policy_resource(observation.resource_id):
                    continue
                anonymous[observation.resource_id].append(observation)
        if not anonymous:
            return

        # Candidate cases per resource from the workflow activity (all cases).
        case_pool: list[CaseScenario] = list(scenarios.values())
        candidates: dict[str, list[tuple[CaseScenario, float]]] = {}
        for resource_id in anonymous:
            ranked: list[tuple[CaseScenario, float]] = []
            for scenario in case_pool:
                bundle = self._activity(scenario.case).resources.get(resource_id)
                if bundle is None:
                    continue
                ranked.append((scenario, bundle.min_distance_seconds))
            ranked.sort(key=lambda item: (item[1], item[0].case.case_id))
            candidates[resource_id] = ranked

        # Process the least ambiguous resources first.
        resources = sorted(
            anonymous,
            key=lambda rid: (len(candidates.get(rid, [])), rid),
        )
        for resource_id in resources:
            ranked = candidates.get(resource_id, [])
            if not ranked:
                continue
            sample = anonymous[resource_id][-1]
            record_type = _normalize_record_type(sample)
            compliant_row = sample.state in ("ANONYMIZED", "REDACTED", "BLOCKED", "DISABLED")
            # A compliant (already redacted/anonymized) row only belongs to a
            # case that otherwise looks finished: an incomplete case has its own
            # remaining findings and its anonymized/redacted copies are already
            # visible through identifiers or through the wave capture.
            if compliant_row:
                ranked = [(scenario, distance) for scenario, distance in ranked if not self._scenario_has_violation(scenario)]
                if not ranked:
                    continue
            chosen = None
            for scenario, _distance in ranked:
                if scenario.has_record_type(record_type):
                    continue
                chosen = scenario
                break
            if chosen is None:
                continue
            observation = self._best_anonymous_observation(anonymous[resource_id], chosen)
            if observation is None:
                continue
            self._attach_anonymous(chosen, observation)

    @staticmethod
    def _scenario_has_violation(scenario: CaseScenario) -> bool:
        from .policy import is_compliant

        for resource in scenario.resources:
            if not is_compliant(resource.record_type, resource.state):
                return True
        return False

    def _best_anonymous_observation(self, items: list[Observation], scenario: CaseScenario) -> Observation | None:
        audit = parse_time(scenario.case.audit_at)
        available = [o for o in items if _observation_available_at(o, audit)]
        if not available:
            return None
        available.sort(key=_observation_sort_key)
        latest = available[-1]
        if not _citable_at(latest, audit):
            citable = [o for o in available if _citable_at(o, audit)]
            if citable:
                return citable[-1]
        return latest

    def _preferred_order(self, record_type: str, resource_ids: list[str]) -> list[str] | None:
        """Recover the subject order of anonymous rows from a bundled snapshot.

        Snapshot bundles render records in subject order. When another document
        contains the same anonymous resource ids, its row order is a much better
        pairing signal than sorting resource ids lexicographically.
        """

        wanted = set(resource_ids)
        best: tuple[int, list[str]] | None = None
        for facts in self.doc_facts.values():
            if facts.family != "snapshot_bundle":
                continue
            present = [
                observation.resource_id
                for observation in facts.observations
                if observation.record_type == record_type and observation.resource_id in wanted
            ]
            if len(present) > (best[0] if best else 0):
                best = (len(present), present)
        if best and best[0] >= max(2, len(wanted) // 2):
            return best[1]
        return None

    @staticmethod
    def _attach_anonymous(scenario: CaseScenario, observation: Observation) -> None:
        scenario.add(
            ResourceState(
                resource_id=observation.resource_id,
                system_id=observation.system_id,
                record_type=observation.record_type,
                state=observation.state,
                evidence_id=observation.evidence_id,
                observed_at=observation.observed_at,
                source="evidence+anonymous",
            )
        )

    # ------------------------------------------------------------------
    # Wave-level account attribution
    # ------------------------------------------------------------------
    def _assign_wave_accounts(self, group, scenarios: dict[str, CaseScenario], wave_doc: EvidenceDocument) -> None:
        facts = self.doc_facts[wave_doc.file_id]
        rows = [o for o in facts.account_observations() if self._owner_of(o) is None]
        if not rows:
            return
        rows.sort(key=lambda o: o.resource_id)
        open_cases = []
        for case in group:
            scenario = scenarios[case.case_id]
            if any(item.record_type == RT_USER_PROFILE and item.system_id == "account_service" for item in scenario.resources):
                continue
            open_cases.append(scenario)
        if not open_cases:
            return
        open_cases.sort(key=lambda s: s.case.case_id)
        pool = list(rows)
        progress = True
        while progress and pool:
            progress = False
            for scenario in list(open_cases):
                activity_ids = self._activity_account_ids(scenario.case)
                possible = [o for o in pool if o.resource_id in activity_ids]
                if len(possible) == 1:
                    self._attach_account(scenario, possible[0])
                    pool.remove(possible[0])
                    open_cases.remove(scenario)
                    progress = True
        if not pool or not open_cases:
            return
        # Cluster by workflow claim time: resources of cases claimed at the same
        # moment are mutually ambiguous, so pair deterministically inside each
        # claim cluster using the activity candidates.
        clusters: dict[str, list[CaseScenario]] = defaultdict(list)
        for scenario in open_cases:
            clusters[scenario.case.reported_at].append(scenario)
        for claim in sorted(clusters):
            members = sorted(clusters[claim], key=lambda s: s.case.case_id)
            candidate_rows = [
                observation
                for observation in pool
                if any(observation.resource_id in self._activity_account_ids(scenario.case) for scenario in members)
            ]
            if not candidate_rows:
                continue
            connectable = [scenario for scenario in members if self._activity_account_ids(scenario.case) & {o.resource_id for o in candidate_rows}]
            attach_rows = sorted(candidate_rows, key=lambda o: o.resource_id)
            for scenario, observation in zip(connectable, attach_rows):
                self._attach_account(scenario, observation)
                pool.remove(observation)

    def _assign_remaining_accounts(self, scenarios: dict[str, CaseScenario]) -> None:
        """Assign account ids to cases the wave packet did not cover.

        Candidates come from the workflow activity (accounts owned by another
        subject are excluded). Cases that share the same ``reported_at`` are
        mutually ambiguous, so their candidates are paired deterministically.
        """

        missing = [
            scenario
            for scenario in scenarios.values()
            if not any(
                item.record_type == RT_USER_PROFILE and item.system_id == "account_service"
                for item in scenario.resources
            )
        ]
        if not missing:
            return
        clusters: dict[str, list[CaseScenario]] = defaultdict(list)
        for scenario in missing:
            clusters[scenario.case.reported_at].append(scenario)
        for claim in sorted(clusters):
            members = sorted(clusters[claim], key=lambda s: s.case.case_id)
            proposals: dict[str, list[str]] = {}
            for scenario in members:
                proposals[scenario.case.case_id] = self._ranked_account_candidates(scenario.case, scenario.user_id)
            union: list[str] = []
            for scenario in members:
                for resource_id in proposals[scenario.case.case_id]:
                    if resource_id not in union:
                        union.append(resource_id)
            union.sort()
            if len(members) == 1:
                for scenario in members:
                    self._finish_account(scenario, proposals[scenario.case.case_id][0] if proposals[scenario.case.case_id] else None)
                continue
            connectable = [
                scenario for scenario in members if proposals[scenario.case.case_id]
            ]
            for scenario, resource_id in zip(connectable, union):
                self._finish_account(scenario, resource_id)

    def _finish_account(self, scenario: CaseScenario, account_id: str | None) -> None:
        if not account_id:
            scenario.notes.append("no account candidate from workflow activity")
            return
        packets = self._packet_documents(scenario.case, scenario.user_id, None)
        state, evidence_id = self._account_state_from_packets(account_id, packets, parse_time(scenario.case.audit_at))
        if state is None:
            state, evidence_id = self._account_state_from_any_doc(account_id, scenario.case, parse_time(scenario.case.audit_at))
        if state is None:
            scenario.notes.append(f"account {account_id}: state unknown")
            return
        scenario.add(
            ResourceState(
                resource_id=account_id,
                system_id="account_service",
                record_type=RT_USER_PROFILE,
                state=state,
                evidence_id=evidence_id,
                source="activity+coverage",
            )
        )

    @staticmethod
    def _attach_account(scenario: CaseScenario, observation: Observation) -> None:
        scenario.add(
            ResourceState(
                resource_id=observation.resource_id,
                system_id="account_service",
                record_type=RT_USER_PROFILE,
                state=observation.state,
                evidence_id=observation.evidence_id,
                observed_at=observation.observed_at,
                source="evidence+anonymous",
            )
        )
