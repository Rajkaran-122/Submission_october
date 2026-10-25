#!/usr/bin/env python3
"""Build the submission ``code.zip`` deterministically.

The archive contains the runnable solution and the required evaluation reports,
but never the official dataset, virtual environments, caches, credentials or
nested archives. The submission contract (problem_statement.md / AGENTS.md
§6.8-6.9) requires:

  * ``README.md`` with clear setup and run instructions,
  * ``evaluation/evaluation_report.md``,
  * ``evaluation/usage_report.md``,
  * the runnable code (here: ``src/ghost_audit/__main__.py``, invoked as
    ``uv run python -m ghost_audit run --dataset ... --output ...``).

``evaluation/intent_cache.json`` (LLM review cache: hashes and labels only, no
request text or secrets) is packaged so ``--llm-intent`` reruns are reproducible
offline. Local telemetry ``evaluation/llm_usage.jsonl`` and scratch
``evaluation/.tmp/**`` are excluded.

Usage
=====

    export UV_CACHE_DIR="$PWD/.uv-cache"
    uv run python scripts/build_code_zip.py                 # write submission/code.zip
    uv run python scripts/build_code_zip.py --dry-run       # list + checks, write nothing
    uv run python scripts/build_code_zip.py --dry-run --allow-missing
    uv run python scripts/build_code_zip.py --list          # also print every entry

``--allow-missing`` relaxes the required-file check while the pipeline or the
evaluation reports are still being written; the final packaging run must not
use it.

Determinism
===========

Entries are sorted by archive path, every entry gets the fixed timestamp
1980-01-01T00:00:00 and fixed permissions, and the archive is built twice and
byte-compared. Two builds from the same repository state therefore produce
identical bytes and the same SHA-256 digest.

Exit codes: 0 success, 1 validation error (missing required file, forbidden
path, non-deterministic build), 2 argparse usage error.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import sys
import zipfile
from pathlib import Path

REPO_ROOT_DEFAULT = Path(__file__).resolve().parents[1]

#: Root-level files that must be packaged when present. README.md is mandatory.
ROOT_FILES = (
    "README.md",
    "INTERVIEW_GUIDE.md",
    "pyproject.toml",
    "uv.lock",
    ".python-version",
)

#: Directories packaged recursively (sorted, filtered by the rules below).
INCLUDE_DIRS = ("src", "tests", "evaluation", "scripts")

#: Required archive entries (the grader / AI interview expects these).
REQUIRED_ENTRIES = (
    "README.md",
    "evaluation/evaluation_report.md",
    "evaluation/usage_report.md",
)

#: The documented invocation is ``uv run python -m ghost_audit ...``.
ENTRY_POINT_CANDIDATES = (
    "src/ghost_audit/__main__.py",
    "src/ghost_audit/cli.py",
)

#: Directory names never packaged, wherever they appear.
EXCLUDED_DIR_NAMES = {
    ".git",
    ".github",
    ".venv",
    "venv",
    "node_modules",
    ".uv-cache",
    ".ocr-cache",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "work",
    "submission",
    "docs",
    ".conversations",
    "dataset",
}

#: Repository-relative prefixes never packaged (official dataset, refs, notes).
EXCLUDED_PREFIXES = (
    "docs/refs/",
    "docs/",
    "dataset/",
    "submission/",
    "work/",
    "evaluation/.tmp/",  # ephemeral evaluation scratch
    "src/ghost_audit/config/",  # local untracked API key file
    "src/templete_project/config/",  # stale template key file
)

#: Nested archives are rejected by the grader; never re-package them.
EXCLUDED_SUFFIXES = {
    ".zip",
    ".tar",
    ".gz",
    ".tgz",
    ".bz2",
    ".xz",
    ".7z",
    ".rar",
    ".whl",
}

#: File names that are cache/secret/submission artifacts rather than code.
EXCLUDED_NAMES = {
    ".DS_Store",
    ".env",
    "api_key.txt",
    "output.csv",
    "log.txt",
    "code.zip",
    "llm_usage.jsonl",  # local telemetry; only the clean report is packaged
}

#: Team-authored mini-dataset fixtures legitimately reuse the participant file
#: names; they are small, hand-written cases and safe to package.
FIXTURE_DIR_PREFIXES = ("evaluation/cases/", "tests/fixtures/")

#: Basenames of participant-facing dataset files: never bundle the dataset.
OFFICIAL_DATASET_BASENAMES = {
    "deletion_requests.csv",
    "sample_deletion_requests.csv",
    "sample_output.csv",
    "user_identities.csv",
    "user_audit_log.csv",
    "system_activity.csv",
    "deletion_rules.csv",
    "evidence_files.csv",
}

FIXED_DATE_TIME = (1980, 1, 1, 0, 0, 0)
FIXED_FILE_MODE = 0o644


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build a deterministic submission/code.zip with the runnable "
            "solution, README and evaluation reports, excluding the official "
            "dataset, caches, virtualenvs and secrets."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  %(prog)s\n"
            "  %(prog)s --dry-run --list\n"
            "  %(prog)s --output /tmp/code.zip\n"
        ),
    )
    parser.add_argument(
        "--repo-root",
        default=str(REPO_ROOT_DEFAULT),
        help="repository root (default: parent of scripts/)",
    )
    parser.add_argument(
        "--output",
        default="submission/code.zip",
        help="archive path, relative to the repo root (default: submission/code.zip)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="collect and validate entries, print the summary, but write nothing",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="print every archive entry",
    )
    parser.add_argument(
        "--allow-missing",
        action="store_true",
        help=(
            "downgrade missing required entries to warnings (development only; "
            "the final packaging run must not use it)"
        ),
    )
    return parser.parse_args(argv)


def excluded_by_policy(rel_path: str) -> str | None:
    """Return a human-readable reason when a repo-relative path must not ship."""

    normalized = rel_path.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part]
    if not parts:
        return "empty path"
    if normalized in ROOT_FILES:
        return None
    for prefix in EXCLUDED_PREFIXES:
        if normalized == prefix.rstrip("/") or normalized.startswith(prefix):
            return f"excluded prefix {prefix!r}"
    for part in parts[:-1]:
        if part in EXCLUDED_DIR_NAMES:
            return f"excluded directory {part!r}"
    name = parts[-1]
    if name in EXCLUDED_NAMES:
        return f"excluded file name {name!r}"
    if name.startswith(".env"):
        return "environment file"
    if name.startswith("api_key") or name.startswith("secret"):
        return "possible credential file"
    if name.startswith("."):
        return "hidden file"
    if Path(name).suffix.lower() in EXCLUDED_SUFFIXES:
        return "nested archive"
    return None


def collect_entries(repo_root: Path) -> tuple[dict[str, Path], list[str]]:
    """Return ({archive_path: absolute_path}, policy_warnings)."""

    entries: dict[str, Path] = {}
    warnings: list[str] = []

    for name in ROOT_FILES:
        path = repo_root / name
        if not path.is_file():
            continue
        reason = excluded_by_policy(name)
        if reason:  # defensive: ROOT_FILES entries are allow-listed above
            warnings.append(f"skipped root file {name}: {reason}")
            continue
        entries[name] = path

    for directory in INCLUDE_DIRS:
        base = repo_root / directory
        if not base.is_dir():
            warnings.append(f"optional source directory missing: {directory}/")
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            rel_path = path.relative_to(repo_root).as_posix()
            reason = excluded_by_policy(rel_path)
            if reason:
                continue
            if path.name in OFFICIAL_DATASET_BASENAMES and not rel_path.startswith(
                FIXTURE_DIR_PREFIXES
            ):
                warnings.append(
                    f"{rel_path}: looks like a participant-facing dataset file; "
                    "the official dataset must not be bundled (confirm it is "
                    "team-authored fixture data, not a copy of dataset/ files)"
                )
            entries[rel_path] = path

    stale_template = [
        rel_path for rel_path in entries if rel_path.startswith("src/templete_project/")
    ]
    if stale_template:
        warnings.append(
            "stale template package 'src/templete_project/' is packaged "
            f"({len(stale_template)} file(s)); remove it from the repository "
            "before the final build"
        )

    # Case-insensitive collisions break extraction on macOS/Windows.
    seen_lower: dict[str, str] = {}
    for rel_path in sorted(entries):
        lowered = rel_path.lower()
        if lowered in seen_lower:
            warnings.append(
                f"case-insensitive path collision: {rel_path!r} vs "
                f"{seen_lower[lowered]!r}"
            )
        else:
            seen_lower[lowered] = rel_path

    return entries, warnings


def guard_forbidden_entries(entries: dict[str, Path]) -> list[str]:
    """Safety net: assert no forbidden path slipped through selection."""

    errors: list[str] = []
    for rel_path in sorted(entries):
        reason = excluded_by_policy(rel_path)
        if reason:
            errors.append(f"forbidden entry selected: {rel_path} ({reason})")
        if rel_path.startswith("/") or ".." in rel_path.split("/"):
            errors.append(f"unsafe archive path: {rel_path}")
    return errors


def build_archive(entries: dict[str, Path]) -> bytes:
    """Serialize entries into a deterministic zip byte string."""

    buffer = io.BytesIO()
    with zipfile.ZipFile(
        buffer, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for arcname in sorted(entries):
            data = entries[arcname].read_bytes()
            info = zipfile.ZipInfo(filename=arcname, date_time=FIXED_DATE_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3  # Unix, so permissions are stable
            info.external_attr = (FIXED_FILE_MODE & 0xFFFF) << 16
            archive.writestr(info, data, compresslevel=9)
    return buffer.getvalue()


def format_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KiB"
    return f"{size / (1024 * 1024):.2f} MiB"


def summarize(
    entries: dict[str, Path],
    warnings: list[str],
    *,
    allow_missing: bool,
    list_entries: bool,
) -> tuple[list[str], dict[str, dict[str, int]]]:
    """Return (errors, per-top-level breakdown) and print the report."""

    errors: list[str] = []
    entry_names = sorted(entries)

    print(f"== code.zip contents: {len(entry_names)} file(s) ==")
    breakdown: dict[str, dict[str, int]] = {}
    for rel_path in entry_names:
        top = rel_path.split("/", 1)[0] if "/" in rel_path else "(root)"
        size = entries[rel_path].stat().st_size
        bucket = breakdown.setdefault(top, {"files": 0, "bytes": 0})
        bucket["files"] += 1
        bucket["bytes"] += size
    for top in sorted(breakdown):
        bucket = breakdown[top]
        print(
            f"  {top + '/':<28} {bucket['files']:>3} file(s)  "
            f"{format_bytes(bucket['bytes']):>9}"
        )
    total_bytes = sum(path.stat().st_size for path in entries.values())
    print(f"  {'TOTAL':<28} {len(entry_names):>3} file(s)  {format_bytes(total_bytes):>9}")

    print("\n== required entries ==")
    for required in REQUIRED_ENTRIES:
        if required in entries:
            print(f"  OK       {required}")
        elif allow_missing:
            print(f"  WARN     missing required entry: {required} (--allow-missing)")
        else:
            print(f"  MISSING  missing required entry: {required}")
            errors.append(f"missing required entry: {required}")
    entry_point = [c for c in ENTRY_POINT_CANDIDATES if c in entries]
    if entry_point:
        print(f"  OK       runnable entry point: {entry_point[0]}")
    else:
        message = (
            "missing runnable entry point "
            f"({', '.join(ENTRY_POINT_CANDIDATES)}); README documents "
            "`uv run python -m ghost_audit run ...`"
        )
        if allow_missing:
            print(f"  WARN     {message} (--allow-missing)")
        else:
            print(f"  MISSING  {message}")
            errors.append(message)
    for recommended in (
        "pyproject.toml",
        "uv.lock",
        ".python-version",
        "evaluation/intent_cache.json",
    ):
        if recommended not in entries:
            print(f"  WARN     recommended entry missing: {recommended}")

    if warnings:
        print("\n== warnings ==")
        for message in warnings:
            print(f"  WARN     {message}")

    if list_entries:
        print("\n== entries ==")
        for rel_path in entry_names:
            print(f"  {rel_path}")

    return errors, breakdown


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = Path(args.repo_root).expanduser().resolve()
    output_path = Path(args.output).expanduser()
    if not output_path.is_absolute():
        output_path = repo_root / output_path

    if not repo_root.is_dir():
        print(f"ERROR repository root not found: {repo_root}")
        return 1
    if not (repo_root / "pyproject.toml").is_file():
        print(f"ERROR {repo_root} does not look like the project root (no pyproject.toml)")
        return 1

    entries, warnings = collect_entries(repo_root)
    forbidden = guard_forbidden_entries(entries)
    errors, _ = summarize(
        entries,
        warnings,
        allow_missing=args.allow_missing,
        list_entries=args.list,
    )
    errors.extend(forbidden)

    if not entries:
        errors.append("no files selected; refusing to write an empty archive")

    print("\n== excluded by policy ==")
    print(
        "  dataset/, docs/ (including the read-only reference checkout), "
        "submission/, work/, .venv, .git, .uv-cache, .ocr-cache, __pycache__, "
        ".pytest_cache, evaluation/.tmp/, evaluation/llm_usage.jsonl, "
        "*.zip/*.tar/*.gz, .env/.DS_Store/api_key.txt, src/*/config/ credentials"
    )

    if errors:
        print("\n== RESULT: FAIL ==")
        for message in errors:
            print(f"  FAIL {message}")
        return 1

    # Build twice and byte-compare to prove determinism.
    try:
        first = build_archive(entries)
        second = build_archive(entries)
    except OSError as exc:
        print(f"\n== RESULT: FAIL ==\n  FAIL cannot read an input file: {exc}")
        return 1
    if first != second:
        print("\n== RESULT: FAIL ==")
        print("  FAIL archive serialization is not deterministic between two builds")
        return 1
    digest = hashlib.sha256(first).hexdigest()

    print("\n== archive ==")
    print(f"  deterministic: yes (two builds byte-identical)")
    print(f"  sha256:        {digest}")
    print(f"  compressed:    {format_bytes(len(first))}")

    if args.dry_run:
        print(f"\n== RESULT: PASS (dry run; nothing written to {output_path}) ==")
        return 0

    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(first)
    except OSError as exc:
        print(f"\n== RESULT: FAIL ==\n  FAIL cannot write {output_path}: {exc}")
        return 1

    # Re-open the written archive: a corrupt archive must never be reported as
    # a successful build.
    try:
        with zipfile.ZipFile(output_path) as archive:
            corrupt_entry = archive.testzip()
            written_names = {info.filename for info in archive.infolist() if not info.is_dir()}
    except (OSError, zipfile.BadZipFile) as exc:
        print(f"\n== RESULT: FAIL ==\n  FAIL written archive is unreadable: {exc}")
        return 1
    if corrupt_entry is not None:
        print(f"\n== RESULT: FAIL ==\n  FAIL corrupt archive entry: {corrupt_entry}")
        return 1
    if written_names != set(entries):
        print(
            "\n== RESULT: FAIL ==\n  FAIL archive file list changed after writing "
            f"({len(written_names)} vs {len(entries)} entries)"
        )
        return 1
    missing_required = [name for name in REQUIRED_ENTRIES if name not in written_names]
    if missing_required and not args.allow_missing:
        print("\n== RESULT: FAIL ==")
        for name in missing_required:
            print(f"  FAIL required entry missing after writing: {name}")
        return 1

    print(f"  written:       {output_path}")
    print(f"  verified:      archive re-opened, {len(written_names)} entries readable")
    print("\n== RESULT: PASS ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
