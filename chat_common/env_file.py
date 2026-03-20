"""Load repo-root `.env` into the process environment (no python-dotenv dependency)."""

from __future__ import annotations

import os
from pathlib import Path


def load_repo_env(repo_root: Path | None = None) -> None:
    """Merge `.env` into ``os.environ``; never overrides existing variables."""
    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent
    path = repo_root / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ[key] = value
