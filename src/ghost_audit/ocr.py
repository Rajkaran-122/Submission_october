"""OCR support for screenshot evidence.

The supplied evidence contains 11 PNG console screenshots. The challenge asks
for multimodal interpretation; on macOS the local Vision framework provides
high-quality OCR, so we call a tiny Swift helper. On machines without
``swift`` (for example the Linux CI runner) the OCR step raises a clear error
and dataset-dependent tests are skipped instead of silently producing wrong
answers. Every extracted text block is cached in memory for the run.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OCR_SCRIPT = PROJECT_ROOT / "scripts" / "ocr_images.swift"
CACHE_DIR = PROJECT_ROOT / ".ocr-cache"


class OCRUnavailable(RuntimeError):
    """Raised when a PNG needs OCR but the local toolchain cannot provide it."""


def ocr_available() -> bool:
    return shutil.which("swift") is not None and OCR_SCRIPT.exists()


def ocr_images(paths: list[Path]) -> dict[Path, str]:
    """Return ``{path: extracted_text}`` for the given screenshots."""

    if not paths:
        return {}
    if not ocr_available():
        raise OCRUnavailable(
            "PNG evidence requires OCR. Install the Swift toolchain (macOS Vision) "
            f"and keep {OCR_SCRIPT} available; alternatively pass --skip-images."
        )

    CACHE_DIR.mkdir(exist_ok=True)
    cache_file = CACHE_DIR / "ocr_cache.json"
    cache: dict[str, str] = {}
    if cache_file.exists():
        try:
            cache = json.loads(cache_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cache = {}

    results: dict[Path, str] = {}
    pending: list[Path] = []
    for path in paths:
        key = hashlib.sha256(path.read_bytes()).hexdigest()
        if key in cache:
            results[path] = cache[key]
        else:
            pending.append(path)

    if pending:
        module_cache = CACHE_DIR / "swift-module-cache"
        tmp_dir = CACHE_DIR / "tmp"
        module_cache.mkdir(parents=True, exist_ok=True)
        tmp_dir.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env["TMPDIR"] = str(tmp_dir)
        env["CLANG_MODULE_CACHE_PATH"] = str(module_cache)
        env["SWIFT_MODULECACHE_PATH"] = str(module_cache)
        command = [
            "swift",
            "-module-cache-path",
            str(module_cache),
            str(OCR_SCRIPT),
            *[str(p) for p in pending],
        ]
        completed = subprocess.run(command, capture_output=True, text=True, env=env, check=False)
        if completed.returncode != 0:
            raise OCRUnavailable(f"swift OCR failed: {completed.stderr.strip()[:800]}")
        current = None
        chunks: dict[str, list[str]] = {}
        for line in completed.stdout.splitlines():
            if line.startswith("=== FILE: "):
                current = line[len("=== FILE: ") :].strip()
                chunks[current] = []
            elif current is not None:
                chunks[current].append(line)
        for path in pending:
            text = "\n".join(chunks.get(str(path), []))
            results[path] = text
            cache[hashlib.sha256(path.read_bytes()).hexdigest()] = text
        cache_file.write_text(json.dumps(cache, indent=1, sort_keys=True), encoding="utf-8")

    return results
