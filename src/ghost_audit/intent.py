"""Decide whether a request actually authorizes deletion.

The supplied request texts are almost all deletion requests in several
languages (English, Spanish, French, Portuguese, German, Japanese, Korean,
...). A small number are access / correction / export / marketing
opt-out / "do-not-delete" requests and must yield ``NOT_APPLICABLE``.

Decision policy: a request is treated as a deletion authorization **unless** it
contains explicit non-deletion instructions. This is both safer (a missed
deletion authorization would be a serious privacy failure) and more robust than
keyword-positive matching on multilingual templates. The optional LLM layer
only re-checks the deterministic decision; it never silently overrides an
explicit negative instruction.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

DELETION = "DELETION"
NON_DELETION = "NON_DELETION"
UNCERTAIN = "UNCERTAIN"

#: Strong phrasings that mean "this is not a deletion request" or that
#: explicitly keep the account. These override generic deletion vocabulary
#: because they are the user's most precise instruction.
_NON_DELETION_PATTERNS = [
    # explicit negation
    r"\bdo not (?:delete|close|remove|erase)\b",
    r"\bdon'?t (?:delete|close|remove|erase)\b",
    r"\bnot (?:asking|requesting) (?:for )?(?:deletion|erasure|removal)\b",
    r"\bnot a (?:request|deletion request) to (?:delete|erase|remove)\b",
    r"\bnot requesting (?:deletion|erasure)\b",
    r"\bno deletion (?:is )?(?:authorized|requested|needed)\b",
    r"\bnot (?:close|delete) the account\b",
    # account must stay / still needed
    r"\bleave the account (?:active|open|as is|intact)\b",
    r"\bleave (?:my )?(?:profile|account) (?:and [\w ]+ )?in place\b",
    r"\bkeep the account\b",
    r"\bstill (?:need|use|want) the account\b",
    r"\bstill (?:need|want) (?:receipts|service messages|order history)\b",
    r"\bcontinue using (?:the|this) (?:service|account)\b",
    r"\baccount (?:should|must) (?:stay|remain) (?:active|open)\b",
    # marketing-only request
    r"\bremove me from (?:the |your )?marketing\b",
    r"\bstop the promotional emails\b",
    r"\bmarketing (?:lists?|preferences|emails?) only\b",
    r"\bunsubscribe\b",
    r"\bopt[- ]?out\b",
    # access / correction / export requests
    r"\bcorrection\b",
    r"\bcorrect (?:it|my|the (?:name|surname|record|email|address))\b",
    r"\bmisspelled\b",
    r"\bmisspelled\b",
    r"\btypo\b",
    r"\bchange the (?:email|address|name|phone|surname)\b",
    r"\breplace (?:it|the (?:email|number|phone|address|name))\b",
    r"\bupdate (?:my|the) (?:email|address|name|phone|number|profile|details)\b",
    r"\bdownload my account information\b",
    r"\bcopy of (?:the|my) (?:information|records|data|invoices|last year)\b",
    r"\bsend me a copy\b",
    r"\bportable format\b",
    r"\baccess my (?:profile|account|data)\b",
    r"\bcan'?t access\b",
    r"\breset(?:ting)? (?:the|my) (?:login|password)\b",
    r"\binformation request\b",
    r"\bwhat personal information\b",
    r"\bwhat data (?:do )?you (?:hold|have)\b",
]

_NON_DELETION_RE = [re.compile(pattern, re.IGNORECASE) for pattern in _NON_DELETION_PATTERNS]

#: Positive deletion vocabulary, used only for the explanation/reason string.
_DELETION_PATTERNS = [
    r"\bdelet(?:e|ed|ion)\b",
    r"\beras(?:e|ed|ure)\b",
    r"\bremov(?:e|ed|al)\b",
    r"\bclos(?:e|ed|ure)\b",
    r"\bwip(?:e|ed)\b",
    r"\bsupprim(?:er|ez|é)\b",
    r"\bsuppression\b",
    r"\beffac(?:er|ement)\b",
    r"\belimin(?:ar|aci[oó]n)\b",
    r"\bborrar\b",
    r"\bexclu(?:ir|s[aã]o)\b",
    r"\bapagar\b",
    r"\bremover\b",
    r"\bl[oö]sch(?:en|ung)\b",
    r"削除",
    r"解約",
    r"삭제",
    r"탈퇴",
]
_DELETION_RE = [re.compile(pattern, re.IGNORECASE) for pattern in _DELETION_PATTERNS]


@dataclass(frozen=True)
class Intent:
    label: str
    reason: str
    matched: tuple[str, ...] = ()

    @property
    def authorizes_deletion(self) -> bool:
        return self.label != NON_DELETION


def classify_request_text(text: str) -> Intent:
    """Classify one request text deterministically.

    Returns ``NON_DELETION`` only when an explicit non-deletion instruction is
    present. Everything else is a deletion authorization (with the reason
    recording the matched vocabulary, or "default: no non-deletion instruction"
    for templated multilingual requests without a matched positive phrase).
    """

    normalized = " ".join((text or "").split())
    if not normalized:
        return Intent(UNCERTAIN, "empty request text")
    negatives = tuple(found.group(0) for pattern in _NON_DELETION_RE if (found := pattern.search(normalized)))
    if negatives:
        return Intent(NON_DELETION, "explicit non-deletion instruction", negatives)
    positives = tuple(found.group(0) for pattern in _DELETION_RE if (found := pattern.search(normalized)))
    if positives:
        return Intent(DELETION, "deletion vocabulary", positives)
    return Intent(DELETION, "default: no explicit non-deletion instruction")


class IntentCache:
    """Deterministic on-disk cache for optional LLM intent reviews.

    The cache stores only a prompt hash, the model label and the model name, so
    it contains no request text and no personal data. It makes a run with
    ``--llm-intent`` reproducible after the first execution.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.entries: dict[str, dict] = {}
        if self.path.exists():
            self.entries = json.loads(self.path.read_text(encoding="utf-8"))

    @staticmethod
    def key(text: str, model: str) -> str:
        return hashlib.sha256(f"{model}\x00{text}".encode("utf-8")).hexdigest()

    def get(self, text: str, model: str) -> str | None:
        entry = self.entries.get(self.key(text, model))
        return entry.get("label") if entry else None

    def put(self, text: str, model: str, label: str, reason: str) -> None:
        self.entries[self.key(text, model)] = {"label": label, "reason": reason, "model": model}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.entries, indent=1, sort_keys=True), encoding="utf-8")
