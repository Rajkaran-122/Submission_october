"""Tests for ``scripts/build_transcript.py`` (conversation transcript builder).

The builder is stdlib-only tooling that turns the local ``.conversations/``
harness exports into the sanitized appendices of the uploaded ``submission/
log.txt``.  These tests use synthetic exports so they do not need the real,
machine-specific folder.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "build_transcript.py"


def load_builder():
    spec = importlib.util.spec_from_file_location("build_transcript", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = load_builder()

BASE_LOG = "# base session log\n\n## [2026-10-25T00:00:00Z] SESSION START\n\ntool=DeepSeek Harness\nmodel=deepseek-flash\n"

# Credential-shaped fixtures are assembled from fragments on purpose: the
# submission validator rejects any code.zip text entry that spells out a secret
# pattern, so these tests must not contain literal key-shaped strings.
FAKE_API_KEY = "sk-" + "0123456789abcdef" * 2
FAKE_ASSIGNMENT = 'API_KEY = "' + "should-be-redacted-123456" + '"'
FAKE_BEARER = "Authorization: Bearer " + "abcdef.ghijkl.mnopqrstuvwxyz0123"
FAKE_PRIVATE_KEY_HEADER = "-----BEGIN " + "RSA PRIVATE KEY" + "-----"


def write_jsonl(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def make_conversations(root: Path) -> Path:
    """Create a small but structurally faithful .conversations export."""

    gpt = root / "GPT"
    gpt.mkdir(parents=True)
    (gpt / "analyze problem.md").write_text(
        "# analyze the core problem\n\nUser asked about the core. "
        f"The stored {FAKE_ASSIGNMENT} must never leak.\n",
        encoding="utf-8",
    )

    main = root / "DeepSeek" / "dsh-session-session-test"
    write_jsonl(
        main / "session.v4.jsonl",
        [
            {"type": "session", "version": 4, "id": "session-test",
             "createdAt": 1792898385555,
             "cwd": "/tmp/worktree"},
            {"type": "session/title", "data": {"title": "test session"},
             "time": 1792898385555},
            {"type": "user/message", "time": 1792898453840,
             "data": {"content": [{"type": "text", "text": "Please audit the cases."}]}},
            {"type": "user/message", "time": 1792898453900,
             "data": {"content": [{"type": "text",
                                   "text": "<system-reminder>injected rules</system-reminder>"}]}},
            {"type": "user/message", "time": 1792898454000,
             "data": {"content": [{"type": "image", "attachment": {
                 "attachmentId": "sha256:abc", "mediaType": "image/png",
                 "width": 10, "height": 20, "name": "shot.png"}}]}},
            {"type": "assistant/message", "time": 1792898455000,
             "data": {"turn": 1, "step": 1, "message": {"role": "assistant",
              "content": [
                  {"type": "reasoning", "text": "internal chain of thought"},
                  {"type": "text", "text": "I will inspect the dataset first."},
                  {"type": "tool-call", "id": "call_1", "name": "bash",
                   "arguments": "{}"},
              ]}}},
            {"type": "tool/call", "time": 1792898455000,
             "data": {"callId": "call_1", "name": "bash",
              "arguments": json.dumps({"command": "ls -la", "description": "List files"})}},
            {"type": "compaction/summary", "time": 1792898456000,
             "data": {"summary": "earlier turns condensed"}},
        ],
    )

    sub = main / "subagents" / "sub-a"
    write_jsonl(
        sub / "session.v4.jsonl",
        [
            {"type": "session", "id": "sub-a", "createdAt": 1792898456100,
             "cwd": "/tmp/worktree"},
            {"type": "user/message", "time": 1792898456200,
             "data": {"content": [{"type": "text", "text": "You are docs-packaging."}]}},
            {"type": "assistant/message", "time": 1792898456300,
             "data": {"turn": 1, "step": 1, "message": {"role": "assistant",
              "content": [{"type": "text", "text": "Packaging done."}]}}},
        ],
    )
    return root


def write_base_log(path: Path) -> Path:
    path.write_text(BASE_LOG, encoding="utf-8")
    return path


def test_sanitize_redacts_common_secret_shapes() -> None:
    text = "\n".join([
        "key " + FAKE_API_KEY,
        FAKE_ASSIGNMENT,
        FAKE_BEARER,
        FAKE_PRIVATE_KEY_HEADER,
    ])
    cleaned = builder.sanitize_text(text)
    assert FAKE_API_KEY not in cleaned
    assert "should-be-redacted" not in cleaned
    assert "abcdef.ghijkl" not in cleaned
    assert cleaned.count("[REDACTED") >= 3


def test_build_transcript_assembles_and_sanitizes(tmp_path: Path) -> None:
    conversations = make_conversations(tmp_path / ".conversations")
    base = write_base_log(tmp_path / "log.txt")

    transcript, stats = builder.build_transcript(base, conversations)

    # PART I is preserved byte-for-byte as a prefix.
    assert transcript.startswith(BASE_LOG.rstrip("\n"))
    assert builder.APPENDIX_MARKER in transcript
    # Every discovered source is represented.
    assert "## Appendix A — Codex session exports" in transcript
    assert "## Appendix B1 — DeepSeek Harness main session" in transcript
    assert "## Appendix C — DeepSeek Harness sub-agent sessions" in transcript
    assert "I will inspect the dataset first." in transcript
    assert "Packaging done." in transcript
    assert "> tool: `bash` — List files" in transcript
    assert "[image attachment: shot.png (image/png, 10x20)]" in transcript
    assert "context compacted" in transcript
    # Internal reasoning and synthetic reminders are not part of the transcript.
    assert "internal chain of thought" not in transcript
    assert "injected rules" not in transcript
    # Secrets in the source exports are redacted before writing.
    assert "should-be-redacted" not in transcript
    assert FAKE_API_KEY not in transcript
    assert stats["codex_exports"] == 1
    assert stats["dsh_main_sessions"] == 1
    assert stats["dsh_subagent_sessions"] == 1
    assert stats["skipped_reminders"] == 1


def test_build_transcript_is_deterministic(tmp_path: Path) -> None:
    conversations = make_conversations(tmp_path / "conv")
    base = write_base_log(tmp_path / "log.txt")

    first, _ = builder.build_transcript(base, conversations)
    second, _ = builder.build_transcript(base, conversations)

    assert first == second


def test_build_transcript_rejects_base_log_with_appendix(tmp_path: Path) -> None:
    conversations = make_conversations(tmp_path / "conv")
    base = tmp_path / "log.txt"
    base.write_text(BASE_LOG + "\n# " + builder.APPENDIX_MARKER + "\n", encoding="utf-8")

    with pytest.raises(ValueError):
        builder.build_transcript(base, conversations)


def test_build_transcript_requires_exports(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    base = write_base_log(tmp_path / "log.txt")

    with pytest.raises(FileNotFoundError):
        builder.build_appendix(empty)
