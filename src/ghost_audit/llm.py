"""Minimal DeepSeek client for the optional intent-review layer.

Only the standard library is used. The API key is read from the environment or
from the untracked local development file; it is never logged, stored in
artifacts, or included in the submission. Every call is appended to a local
usage log (JSONL) so the final usage report can quote measured token counts.

The model is used for one narrow semantic task (multilingual request-intent
review). All factual evidence processing is deterministic and offline.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import (
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL,
    PROJECT_ROOT,
    read_api_key,
)

USAGE_LOG = PROJECT_ROOT / "evaluation" / "llm_usage.jsonl"

SYSTEM_PROMPT = (
    "You are a privacy-request classifier for an e-commerce platform. "
    "Decide whether the customer message authorizes deletion of their account/personal data. "
    "Answer with exactly one word: DELETION if the message asks for deletion/erasure/removal of the "
    "account or personal data (in any language), or NON_DELETION if it asks for something else "
    "(access, correction, export, marketing opt-out, login help) or explicitly says not to delete. "
    "Do not explain."
)


@dataclass
class Usage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = DEEPSEEK_MODEL
    records: list[dict] = field(default_factory=list)

    def add(self, prompt_tokens: int, completion_tokens: int, tag: str) -> None:
        self.calls += 1
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        self.records.append(
            {
                "model": self.model,
                "tag": tag,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            }
        )


class DeepSeekClient:
    def __init__(self) -> None:
        self.api_key = read_api_key()
        if not self.api_key:
            raise RuntimeError("no DeepSeek API key configured (set DEEPSEEK_API_KEY or provide the untracked local key file)")
        self.model = DEEPSEEK_MODEL
        self.base_url = DEEPSEEK_BASE_URL.rstrip("/")
        self.usage = Usage()

    def _post(self, payload: dict) -> dict:
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - trusted endpoint
            return json.loads(response.read().decode("utf-8"))

    def classify_intent(self, text: str) -> str:
        payload = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
        }
        try:
            data = self._post(payload)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"DeepSeek request failed: {type(exc).__name__}") from exc
        usage = data.get("usage", {})
        self.usage.add(
            int(usage.get("prompt_tokens", 0)),
            int(usage.get("completion_tokens", 0)),
            tag="intent_review",
        )
        self._append_usage(usage)
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        label = "NON_DELETION" if "NON_DELETION" in content.upper() or "NON-DELETION" in content.upper() else "DELETION"
        return label

    @staticmethod
    def _append_usage(usage: dict) -> None:
        USAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "model": DEEPSEEK_MODEL,
            "utc_time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            **usage,
        }
        with USAGE_LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
