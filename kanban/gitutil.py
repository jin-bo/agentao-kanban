"""Tiny Git-discovery helpers shared by the CLI, the web server, and the
result aggregator. Kept dependency-free (stdlib only) so any layer can
import it without dragging in ``kanban.cli``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def find_git_root_optional(path: Path) -> Path | None:
    """Return the Git toplevel for ``path``, or ``None`` if there is none.

    Uses ``git rev-parse --show-toplevel`` which works for regular repos,
    linked worktrees (``.git`` is a file), and paths nested inside repos.
    When ``path`` does not yet exist, walks up to the first existing
    ancestor so we still bind to the correct repo on fresh boards. Returns
    ``None`` (rather than raising) when no existing ancestor or no repo can
    be found, or when the ``git`` binary itself is unavailable — callers
    decide whether that's a hard failure.
    """
    try:
        start = path.resolve(strict=False)
    except OSError:
        start = path
    probe = start
    while not probe.exists():
        parent = probe.parent
        if parent == probe:
            return None
        probe = parent
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=probe,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip())
