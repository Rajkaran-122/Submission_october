"""Current and historical subject identity resolution.

The dataset contains two complementary sources:

* ``user_identities.csv`` — the current identity snapshot per customer;
* ``user_audit_log.csv`` — historical changes of ``email``, ``email_hash`` and
  ``phone``.

A deletion request may cite a *historical* email, and older evidence may still
carry historical emails/phone numbers/hashes. Every identifier observed in the
evidence must therefore be translated through the union of current and
historical values before it can be linked to a subject.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .dataset import AuditChange, Dataset, Request, UserIdentity


def _norm(value: str) -> str:
    return (value or "").strip().lower()


@dataclass
class UserTokens:
    """Every identifier ever associated with one user."""

    user_id: str
    current: UserIdentity
    emails: set[str] = field(default_factory=set)
    phones: set[str] = field(default_factory=set)
    hashes: set[str] = field(default_factory=set)
    names: set[str] = field(default_factory=set)

    def add(self, value: str) -> None:
        text = (value or "").strip()
        if not text:
            return
        lowered = text.lower()
        if "@" in text:
            self.emails.add(lowered)
        elif text.lower().startswith("+") or any(ch.isdigit() for ch in text):
            # Phone numbers and hex hashes both may contain digits; keep both
            # buckets so either form can resolve evidence identifiers.
            self.phones.add(lowered)
            if len(text) >= 32 and all(ch in "0123456789abcdef" for ch in lowered):
                self.hashes.add(lowered)
        elif len(text) == 64 and all(ch in "0123456789abcdef" for ch in lowered):
            self.hashes.add(lowered)
        else:
            self.names.add(lowered)


class IdentityIndex:
    """Resolve identifiers (current or historical) to one stable user id."""

    def __init__(self, dataset: Dataset):
        self.users: dict[str, UserIdentity] = {}
        self.tokens: dict[str, UserTokens] = {}
        self.alias_to_users: dict[str, set[str]] = {}
        for identity in dataset.users:
            self.users[identity.user_id] = identity
            token = UserTokens(user_id=identity.user_id, current=identity)
            token.add(identity.email)
            token.add(identity.phone)
            token.add(identity.email_hash)
            token.add(identity.name)
            self.tokens[identity.user_id] = token
        for change in dataset.audit_log:
            bundle = self.tokens.setdefault(
                change.user_id,
                UserTokens(user_id=change.user_id, current=self.users[change.user_id]),
            )
            for value in (change.old_value, change.new_value):
                bundle.add(value)
        for user_id, bundle in self.tokens.items():
            for value in bundle.emails | bundle.phones | bundle.hashes:
                self.alias_to_users.setdefault(value, set()).add(user_id)
            for value in bundle.names:
                self.alias_to_users.setdefault(value, set()).add(user_id)
        self._case_to_user: dict[str, str] | None = None

    # -- lookups ------------------------------------------------------------
    def resolve(self, value: str) -> str | None:
        """Return the unique user id owning an identifier, if unambiguous."""

        candidates = self.alias_to_users.get(_norm(value))
        if candidates and len(candidates) == 1:
            return next(iter(candidates))
        return None

    def resolve_many(self, values) -> set[str]:
        found: set[str] = set()
        for value in values:
            user_id = self.resolve(value)
            if user_id:
                found.add(user_id)
        return found

    def tokens_for(self, user_id: str) -> set[str]:
        bundle = self.tokens[user_id]
        return set(bundle.emails) | set(bundle.phones) | set(bundle.hashes) | set(bundle.names)

    def case_to_user(self, cases: list[Request]) -> dict[str, str]:
        """Map every case id to its subject user id.

        Requests must resolve through current OR historical email. A request
        that cannot be resolved is a dataset problem and raises loudly instead
        of being guessed.
        """

        mapping: dict[str, str] = {}
        unresolved: list[str] = []
        for case in cases:
            user_id = self.resolve(case.email)
            if user_id is None:
                unresolved.append(case.case_id)
                continue
            mapping[case.case_id] = user_id
        if unresolved:
            raise ValueError(f"could not resolve identities for cases: {unresolved[:10]}")
        self._case_to_user = mapping
        return mapping

    def user_for_case(self, case_id: str) -> str:
        if self._case_to_user is None:
            raise RuntimeError("call case_to_user() first")
        return self._case_to_user[case_id]

    def historical_emails(self, user_id: str) -> set[str]:
        """Return emails that are no longer current (history-only)."""

        current = _norm(self.users[user_id].email)
        return {email for email in self.tokens[user_id].emails if email != current}
