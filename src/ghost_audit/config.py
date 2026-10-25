"""Shared configuration for the Ghost Data Still Exists audit pipeline.

Secrets are never stored in this package. The DeepSeek API key is read from the
``DEEPSEEK_API_KEY`` environment variable, or (for local development only) from
an untracked file next to the code (``config/api_key.txt``). The untracked file
is excluded from every git commit and from ``code.zip``.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Repository root (the directory that contains ``pyproject.toml``).
PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: Default location of the participant-facing dataset in this working copy.
DEFAULT_DATASET_DIR = PROJECT_ROOT / "docs" / "refs" / "hackerrank-orchestrate-october26-main" / "dataset"

#: Local, untracked development credential files (never committed / packaged).
#: The key file location is confirmed from the environment; the second path is
#: the pre-existing template location in this working copy.
LOCAL_API_KEY_FILES = (
    Path(__file__).resolve().parent / "config" / "api_key.txt",
    Path(__file__).resolve().parents[1] / "templete_project" / "config" / "api_key.txt",
)

#: DeepSeek chat-completions endpoint and supported model identifier.
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")

#: Model pricing (USD per 1M tokens) used by the usage report. Source: DeepSeek
#: public pricing page https://api-docs.deepseek.com/quick_start/pricing ,
#: recorded on 2026-10-25 for model ``deepseek-flash``: off-peak input
#: USD 0.15 / output USD 0.60 per 1M tokens (peak rates are double; the final
#: run happened on a Sunday, i.e. off-peak). The usage report cites this source.
PRICE_INPUT_USD_PER_MTOK = 0.15
PRICE_OUTPUT_USD_PER_MTOK = 0.60


def read_api_key() -> str | None:
    """Return the DeepSeek API key from env, then from the local untracked file.

    The key is never logged, printed, committed or written into any artifact.
    """

    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if key:
        return key
    for candidates in LOCAL_API_KEY_FILES:
        try:
            text = candidates.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            if "API_KEY" in line and '"' in line:
                parts = line.split('"')
                if len(parts) >= 2 and parts[1].strip():
                    return parts[1].strip()
    return None
