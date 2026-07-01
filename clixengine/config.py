"""Configuration & secret loading.

The Anthropic API key lives in a ``.env`` file at the project root. Because we
work inside a git *worktree* nested under the main checkout, we walk up parent
directories until we find a ``.env`` — this transparently finds the main repo's
``.env`` without copying the secret into the worktree.
"""

from __future__ import annotations

import os
from pathlib import Path


def _parse_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def find_dotenv(start: Path | None = None) -> Path | None:
    """Walk up from ``start`` (default cwd, then this file's dir) to find .env."""
    candidates = []
    if start is not None:
        candidates.append(start)
    candidates.append(Path.cwd())
    candidates.append(Path(__file__).resolve().parent)
    seen: set[Path] = set()
    for base in candidates:
        cur = base.resolve()
        while cur not in seen:
            seen.add(cur)
            env_path = cur / ".env"
            if env_path.is_file():
                return env_path
            if cur.parent == cur:
                break
            cur = cur.parent
    return None


def load_dotenv() -> dict[str, str]:
    """Load .env into a dict and set any missing keys into os.environ."""
    path = find_dotenv()
    if path is None:
        return {}
    env = _parse_env_file(path)
    for k, v in env.items():
        os.environ.setdefault(k, v)
    return env


def get_api_key() -> str | None:
    """Return the Anthropic API key from the environment or .env, or None."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    env = load_dotenv()
    return env.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
