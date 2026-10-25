"""Unit tests for the multilingual request-intent classifier."""

from __future__ import annotations

from pathlib import Path

import pytest

from ghost_audit.intent import DELETION, NON_DELETION, IntentCache, classify_request_text

DATASET = Path("docs/refs/hackerrank-orchestrate-october26-main/dataset")

DELETION_TEXTS = [
    "Please permanently delete the account associated with this email address.",
    "Je demande la suppression définitive du compte associé à cette adresse e-mail.",
    "Solicito la eliminación permanente de la cuenta asociada a este correo.",
    "Solicito a exclusão permanente da conta associada a este e-mail.",
    "このメールアドレスに関連するアカウントの完全な削除を依頼します。",
    "이 이메일 주소에 연결된 계정의 영구 삭제를 요청합니다.",
    "I deleted this account and received a confirmation, but the same email address is still recognized.",
    "I would like the account associated with this address to be erased.",
    "Please apply the correct treatment to records covered by the request.",
]

NON_DELETION_TEXTS = [
    "I'd like to download my account information in a portable format. Please keep the account as it is.",
    "I can't access my profile and need help resetting the login. Do not close the account.",
    "My surname is misspelled on the account. Can you correct it without removing any of my records?",
    "Please remove me from marketing lists only. I still want receipts and service messages for this account.",
    "Can you tell me what personal information you hold about me? This is an information request, not a request to erase it.",
    "Please stop the promotional emails. I still need the account and my order history, so I am not asking for deletion.",
]


def test_deletion_texts_in_several_languages():
    for text in DELETION_TEXTS:
        intent = classify_request_text(text)
        assert intent.label == DELETION, (text, intent)
        assert intent.authorizes_deletion


def test_non_deletion_texts():
    for text in NON_DELETION_TEXTS:
        intent = classify_request_text(text)
        assert intent.label == NON_DELETION, (text, intent)
        assert not intent.authorizes_deletion


def test_empty_text_is_uncertain_not_authorized():
    intent = classify_request_text("")
    assert intent.label not in (DELETION,)
    assert classify_request_text("   ").label != DELETION


@pytest.mark.skipif(not (DATASET / "deletion_requests.csv").exists(), reason="official dataset not present")
def test_official_dataset_intent_distribution():
    import csv

    rows = list(csv.DictReader((DATASET / "deletion_requests.csv").open(encoding="utf-8")))
    non_deletion = [row["case_id"] for row in rows if classify_request_text(row["request_text"]).label == NON_DELETION]
    assert len(non_deletion) == 10
    assert non_deletion == ["case_533", "case_534", "case_535", "case_536", "case_537", "case_538", "case_539", "case_540", "case_541", "case_542"]
    # The workflow classification stream independently confirms the same 10 cases.
    activity = list(csv.DictReader((DATASET / "system_activity.csv").open(encoding="utf-8")))
    classified = [row for row in activity if row["event_type"] == "CLASSIFY_REQUEST"]
    assert len(classified) == 10


def test_intent_cache_is_content_addressed(tmp_path):
    cache = IntentCache(tmp_path / "cache.json")
    assert cache.get("delete my account", "deepseek-chat") is None
    cache.put("delete my account", "deepseek-chat", "DELETION", "review")
    assert cache.get("delete my account", "deepseek-chat") == "DELETION"
    # The stored file must not contain the request text itself.
    stored = (tmp_path / "cache.json").read_text(encoding="utf-8")
    assert "delete my account" not in stored
