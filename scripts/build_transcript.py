#!/usr/bin/env python3
"""Build the uploaded chat transcript (``submission/log.txt``).

The official contract (reference ``AGENTS.md`` §2/§5) says the repository-root
``log.txt`` is the append-only coding-agent session log.  This script keeps that
file untouched and produces the *submission* transcript used on HackerRank:

    submission/log.txt  =  log.txt (root, verbatim prefix)
                         +  PART II: full conversation transcripts

The second part is generated from the local ``.conversations/`` exports the user
provided (Codex session export + DeepSeek Harness ``session.v4.jsonl`` files,
including the sub-agent sessions).  Those raw exports are machine-specific,
large and may contain transient data, so they are gitignored and never shipped
in ``code.zip``; only this sanitized, human-readable derivation is delivered.

Design rules
============

* **Stdlib only** — runs with the system Python before/without ``uv sync``.
* **Deterministic** — same input exports produce byte-identical output: no
  wall-clock timestamps, fixed ordering, source-derived timestamps only.
* **Base log is verbatim** — ``submission/log.txt`` must start with the exact
  bytes of the root ``log.txt``; a mismatch aborts the build.
* **Sanitized** — API keys, credential assignments, bearer tokens and private
  key blocks are redacted before anything is written.
* **Readable** — user messages and assistant-visible text are kept; tool calls
  are reduced to one ``> tool: name — summary`` line (no tool outputs), so the
  transcript shows *what was asked, answered and done* without megabytes of
  raw payloads.

Usage
=====

    python3 scripts/build_transcript.py                    # write submission/log.txt
    python3 scripts/build_transcript.py --check            # verify it is up to date
    python3 scripts/build_transcript.py --dry-run          # print the summary only
    python3 scripts/build_transcript.py --conversations DIR --base-log FILE

Exit codes: 0 success / up to date, 1 stale or invalid artifact, 2 missing input.

Regenerate after adding new exports or new root-log entries.  The generated
PART II section is *not* append-only: it is a build product rebuilt from the
source exports (the PART I session log inside it remains append-only).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT_DEFAULT = Path(__file__).resolve().parents[1]
DEFAULT_BASE_LOG = "log.txt"
DEFAULT_OUTPUT = "submission/log.txt"
DEFAULT_CONVERSATIONS = ".conversations"

#: Marks the start of the generated part; never present in the root session log.
APPENDIX_MARKER = "# PART II — FULL CONVERSATION TRANSCRIPTS"

#: Redaction patterns (mirrors scripts/validate_submission.py's intent).
SECRET_PATTERNS = (
    ("private key block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("sk- style API key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")),
    (
        "credential assignment",
        re.compile(
            r"""(?ix)
            \b(?:api[_-]?key|secret(?:[_-]?key)?|access[_-]?token|auth[_-]?token|
                 client[_-]?secret|password|passwd)\b
            \s*[:=]\s*
            (["']?)[^\s"']{8,}\1
            """
        ),
    ),
    ("bearer token", re.compile(r"\bBearer\s+[A-Za-z0-9._-]{20,}\b")),
)

#: Content-item keys worth showing when a tool call has no description.
_TOOL_SUMMARY_KEYS = (
    "description",
    "file_path",
    "path",
    "pattern",
    "query",
    "url",
    "command",
    "prompt",
)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sanitize_text(text: str) -> str:
    """Normalize line endings and redact secrets."""

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    for label, pattern in SECRET_PATTERNS:
        text = pattern.sub(lambda _m, _l=label: f"[REDACTED {label.upper()}]", text)
    return text


def _iso_utc(ms: int | None) -> str:
    if not isinstance(ms, (int, float)):
        return "unknown"
    return (
        datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
        .isoformat(timespec="seconds")
    )


def _one_line(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _iter_events(path: Path):
    """Yield JSON events, silently skipping malformed lines (never files)."""

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                yield event


def _content_text(content: list) -> str:
    """Render text/image content items; ignore other content types."""

    chunks: list[str] = []
    for item in content or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text" and isinstance(item.get("text"), str):
            chunks.append(item["text"])
        elif item.get("type") == "image":
            attachment = item.get("attachment") or {}
            name = attachment.get("name") or attachment.get("attachmentId") or "image"
            media = attachment.get("mediaType") or "image"
            width, height = attachment.get("width"), attachment.get("height")
            size = ""
            if isinstance(width, int) and isinstance(height, int):
                size = f", {width}x{height}"
            chunks.append(f"[image attachment: {name} ({media}{size})]")
    return "\n\n".join(chunk.strip() for chunk in chunks if chunk.strip())


def _tool_summary(arguments: str) -> str:
    try:
        args = json.loads(arguments)
    except (TypeError, json.JSONDecodeError):
        args = None
    if isinstance(args, dict):
        for key in _TOOL_SUMMARY_KEYS:
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                return _one_line(value, 160)
    return _one_line(arguments or "", 160)


# ---------------------------------------------------------------------------
# DeepSeek Harness session rendering
# ---------------------------------------------------------------------------


def _session_meta(path: Path) -> dict:
    meta: dict = {"cwd": "", "createdAt": "", "title": "", "first_time": None, "last_time": None}
    for event in _iter_events(path):
        kind = event.get("type")
        if kind == "session":
            meta["cwd"] = event.get("cwd") or ""
            meta["createdAt"] = event.get("createdAt") or ""
        elif kind == "session/title":
            data = event.get("data") or {}
            if isinstance(data, dict) and isinstance(data.get("title"), str):
                meta["title"] = data["title"]
        time = event.get("time")
        if isinstance(time, (int, float)):
            meta["first_time"] = time if meta["first_time"] is None else min(meta["first_time"], time)
            meta["last_time"] = time if meta["last_time"] is None else max(meta["last_time"], time)
    return meta


def render_dsh_session(path: Path, heading: str) -> str:
    """Render one ``session.v4.jsonl`` as a readable transcript section."""

    meta = _session_meta(path)
    skipped = {"system_reminders": 0}
    lines: list[str] = [
        heading,
        "",
        f"Source: `{path.name}` (sha256 `{sha256_file(path)[:16]}…`, "
        f"events {_iso_utc(meta['first_time'])} → {_iso_utc(meta['last_time'])})",
    ]
    if meta["cwd"]:
        lines.append(f"Working directory: `{meta['cwd']}`")
    if meta["title"]:
        lines.append(f"Session title: {sanitize_text(meta['title'])}")
    lines.append("")

    for event in _iter_events(path):
        kind = event.get("type")
        time = _iso_utc(event.get("time"))
        data = event.get("data") or {}

        if kind == "user/message":
            text = _content_text(data.get("content") or [])
            # The harness injects workspace instructions as synthetic user
            # messages; they are not conversation turns, so skip them.
            if text.lstrip().startswith("<system-reminder>"):
                skipped["system_reminders"] += 1
                continue
            if text:
                lines += [f"### [{time}] USER", "", sanitize_text(text), ""]
        elif kind == "assistant/message":
            message = data.get("message") or {}
            text = _content_text(message.get("content") or [])
            if text:
                lines += [f"### [{time}] ASSISTANT", "", sanitize_text(text), ""]
        elif kind == "tool/call":
            name = data.get("name") or "tool"
            summary = _tool_summary(data.get("arguments") or "")
            lines.append(f"> tool: `{name}` — {sanitize_text(summary)}")
        elif kind == "compaction/summary":
            lines.append(
                "> [context compacted: earlier turns were condensed; the "
                "session-log entries in PART I cover that span]"
            )
    lines.append("")
    return "\n".join(lines), skipped["system_reminders"]


# ---------------------------------------------------------------------------
# Conversation discovery and assembly
# ---------------------------------------------------------------------------


def _deepseek_exports(conversations: Path) -> tuple[list[Path], list[tuple[Path, Path]]]:
    """Return (main sessions, [(session_dir, subagent_path)])."""

    mains: list[Path] = []
    subs: list[tuple[Path, Path]] = []
    for session_dir in sorted(conversations.glob("DeepSeek/dsh-session-*")):
        main = session_dir / "session.v4.jsonl"
        if main.is_file():
            mains.append(main)
        for sub_dir in sorted((session_dir / "subagents").glob("*/session.v4.jsonl")):
            subs.append((session_dir, sub_dir))
    return mains, subs


def _codex_exports(conversations: Path) -> list[Path]:
    return sorted(
        path
        for path in conversations.glob("GPT/*")
        if path.is_file() and path.suffix.lower() == ".md" and path.name != "AGENTS.md"
    )


def build_appendix(conversations: Path) -> tuple[str, dict]:
    """Return the generated PART II text and summary statistics."""

    mains, subs = _deepseek_exports(conversations)
    codex = _codex_exports(conversations)
    if not mains and not subs and not codex:
        raise FileNotFoundError(
            f"no conversation exports found under {conversations} "
            "(expected GPT/*.md and DeepSeek/dsh-session-*/session.v4.jsonl)"
        )

    newest = None
    for main in mains:
        meta = _session_meta(main)
        if isinstance(meta["last_time"], (int, float)):
            newest = meta["last_time"] if newest is None else max(newest, meta["last_time"])

    parts: list[str] = [
        "",
        "",
        "# " + APPENDIX_MARKER,
        "# " + "=" * 75,
        "# Generated by scripts/build_transcript.py from the local export folder",
        "# `.conversations/` (machine-specific; gitignored, never shipped in code.zip).",
        "# PART I above is the append-only root log.txt, preserved verbatim.",
        "# PART II is a clean rebuild: secrets redacted, tool outputs replaced by",
        "# one-line tool-call summaries, and synthetic <system-reminder> user",
        "# messages injected by the harness are omitted (they are not turns).",
        f"# Newest source event: {_iso_utc(newest)}",
        "# " + "=" * 75,
        "",
    ]

    if codex:
        parts += ["## Appendix A — Codex session exports (verbatim markdown)", ""]
        for index, path in enumerate(codex, start=1):
            raw = path.read_text(encoding="utf-8", errors="replace")
            parts += [
                f"### A.{index} — {path.stem}",
                "",
                f"Source: `{path.name}` ({len(raw.encode('utf-8'))} bytes, "
                f"sha256 `{sha256_file(path)[:16]}…`)",
                "",
                sanitize_text(raw),
                "",
            ]

    section = "B" if codex else "A"
    skipped_reminders = 0
    for index, path in enumerate(mains, start=1):
        rendered, skipped = render_dsh_session(
            path,
            f"## Appendix {section}{index} — DeepSeek Harness main session "
            f"({path.parent.name})",
        )
        parts.append(rendered)
        skipped_reminders += skipped
    main_section = section
    section = "C" if main_section == "B" else "B"

    if subs:
        parts += [
            f"## Appendix {section} — DeepSeek Harness sub-agent sessions "
            f"({len(subs)} session(s))",
            "",
            "Sub-agents are teammates spawned by the Lead during this delivery; "
            "they use the same shared filesystem and are part of the project "
            "history.",
            "",
        ]
        for index, (session_dir, path) in enumerate(subs, start=1):
            rendered, skipped = render_dsh_session(
                path,
                f"### {section}.{index} — sub-agent {path.parent.name} "
                f"(parent session {session_dir.name})",
            )
            parts.append(rendered)
            skipped_reminders += skipped

    if skipped_reminders:
        parts.append(
            f"({skipped_reminders} synthetic system-reminder message(s) were "
            "omitted across the appended sessions.)"
        )
        parts.append("")
    stats = {
        "codex_exports": len(codex),
        "dsh_main_sessions": len(mains),
        "dsh_subagent_sessions": len(subs),
        "skipped_reminders": skipped_reminders,
        "newest_event": _iso_utc(newest),
    }
    return "".join(part if part.endswith("\n") else part + "\n" for part in parts), stats


def build_transcript(base_log: Path, conversations: Path) -> tuple[str, dict]:
    """Assemble the full submission transcript."""

    base = base_log.read_text(encoding="utf-8")
    appendix, stats = build_appendix(conversations)
    if APPENDIX_MARKER in base:
        raise ValueError(
            f"{base_log} already contains generated PART II content; the base "
            "log must stay append-only"
        )
    transcript = base.rstrip("\n") + "\n" + appendix
    stats.update(
        {
            "base_lines": len(base.splitlines()),
            "total_lines": len(transcript.splitlines()),
            "total_bytes": len(transcript.encode("utf-8")),
        }
    )
    return transcript, stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _summary_text(stats: dict) -> str:
    return (
        f"codex exports: {stats['codex_exports']}, "
        f"DSH main sessions: {stats['dsh_main_sessions']}, "
        f"DSH sub-agent sessions: {stats['dsh_subagent_sessions']} "
        f"(skipped {stats['skipped_reminders']} system-reminder injections); "
        f"total {stats['total_lines']} lines / {stats['total_bytes']} bytes "
        f"(base log {stats['base_lines']} lines); "
        f"newest source event: {stats['newest_event']}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--conversations", default=DEFAULT_CONVERSATIONS,
                        help="folder with the raw conversation exports (default: .conversations)")
    parser.add_argument("--base-log", default=DEFAULT_BASE_LOG,
                        help="append-only root session log used as PART I (default: log.txt)")
    parser.add_argument("--output", default=DEFAULT_OUTPUT,
                        help="transcript to write/check (default: submission/log.txt)")
    parser.add_argument("--check", action="store_true",
                        help="verify the output is current instead of writing it")
    parser.add_argument("--dry-run", action="store_true",
                        help="build in memory and print a summary, write nothing")
    args = parser.parse_args(argv)

    repo_root = REPO_ROOT_DEFAULT
    base_log = (repo_root / args.base_log).resolve()
    conversations = (repo_root / args.conversations).resolve()
    output = (repo_root / args.output).resolve()

    if not base_log.is_file():
        print(f"ERROR: base log not found: {base_log}", file=sys.stderr)
        return 2
    if not conversations.is_dir():
        print(
            f"ERROR: conversation export folder not found: {conversations}\n"
            "The raw exports are machine-specific and gitignored; on a fresh "
            "clone only the committed transcript in submission/log.txt exists.",
            file=sys.stderr,
        )
        return 2

    try:
        transcript, stats = build_transcript(base_log, conversations)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.check:
        if not output.is_file():
            print(f"FAIL: missing transcript {output}", file=sys.stderr)
            return 1
        current = output.read_text(encoding="utf-8")
        if current == transcript:
            print(f"PASS: transcript is current ({_summary_text(stats)})")
            return 0
        print(
            "FAIL: submission transcript is stale; run "
            "`python3 scripts/build_transcript.py` and commit the result.",
            file=sys.stderr,
        )
        current_lines, expected_lines = current.splitlines(), transcript.splitlines()
        for index, (got, want) in enumerate(zip(current_lines, expected_lines), start=1):
            if got != want:
                print(f"  first difference at line {index}:", file=sys.stderr)
                print(f"    current: {got[:120]!r}", file=sys.stderr)
                print(f"    rebuilt: {want[:120]!r}", file=sys.stderr)
                break
        else:
            print(
                f"  line counts differ: current {len(current_lines)}, "
                f"rebuilt {len(expected_lines)}",
                file=sys.stderr,
            )
        return 1

    print(_summary_text(stats))
    if args.dry_run:
        print("dry run: nothing written")
        return 0

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(transcript, encoding="utf-8")
    print(f"wrote {output} ({output.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
