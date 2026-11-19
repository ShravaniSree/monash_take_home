"""Load a local .env file without overriding variables already set."""

from __future__ import annotations

import os
from pathlib import Path


def load_local_env(root: Path) -> None:
    """Read ``root/.env`` into the process environment.

    Args:
        root: Project directory. A missing file leaves the environment unchanged.

    Existing variables win, so a shell export is kept and tests that point
    at a temporary directory do not pick up this checkout's key.
    """
    path = root / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
