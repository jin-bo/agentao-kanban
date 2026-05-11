from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

BRANCH_PREFIX = "kanban/"

DEFAULT_ARTIFACTS_RETENTION = 5
DEFAULT_ARTIFACTS_MAX_BYTES = 500 * 1024 * 1024  # 500 MiB
ARTIFACTS_MAX_BYTES_ENV = "KANBAN_ARTIFACTS_MAX_BYTES"

# Pattern for snapshot directory names emitted by ``_save_artifacts``
# (``f"artifacts-{strftime('%Y%m%dT%H%M%S%fZ')}"``). Lives next to the
# writer so the format stays a single source of truth — readers (e.g.
# ``kanban.web``) import this rather than re-deriving the pattern.
ARTIFACT_DIR_NAME_RE = re.compile(r"^artifacts-\d{8}T\d{6}\d+Z$")
# Path components or path suffixes that almost always represent build
# caches / dependency stores rather than worker deliverables. Skipping
# them keeps the snapshot focused on real outputs and (importantly)
# stops a single ``node_modules/`` from blowing the size cap. Operators
# who actually need one of these for a card can override
# ``WorktreeManager.artifacts_denylist`` at construction.
DEFAULT_ARTIFACTS_DENYLIST: tuple[str, ...] = (
    "__pycache__/",
    "node_modules/",
    ".venv/",
    "venv/",
    ".tox/",
    ".cache/",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    ".next/",
    ".turbo/",
    "target/",        # Rust / sbt
    "dist/",
    "build/",
    ".gradle/",
    ".pyc",           # extension match
    ".pyo",
)


def _is_denylisted(rel_path: str, patterns: tuple[str, ...]) -> bool:
    """Match POSIX-style ``rel_path`` against the denylist.

    A pattern ending in ``/`` matches when its stripped name appears as
    *any* path component (so ``node_modules/`` hits both
    ``node_modules/foo.js`` and ``packages/x/node_modules/y.js``).
    Otherwise it's treated as a path-suffix match — useful for file
    extensions like ``.pyc``.
    """
    parts = rel_path.split("/")
    for p in patterns:
        if p.endswith("/"):
            if p[:-1] in parts:
                return True
        elif rel_path.endswith(p):
            return True
    return False


class WorktreeCreateError(RuntimeError):
    """Raised when ``git worktree add`` fails."""


class WorktreeDiffError(RuntimeError):
    """Raised when ``diff_summary`` cannot resolve refs or run git diff."""


@dataclass
class WorktreeInfo:
    card_id: str
    path: Path | None
    branch: str
    base_commit: str
    head_commit: str


@dataclass
class DetachResult:
    """Outcome of :meth:`WorktreeManager.detach`.

    ``removed`` mirrors the historical bool return (True = worktree
    directory gone or never existed; False = removal aborted to preserve
    uncommitted work).

    ``artifacts_path`` points at the per-card snapshot dir if any
    gitignored content was rescued before the worktree was deleted.
    None when no artifacts existed, the snapshot was skipped (size cap
    or disabled), or the worktree was never on disk.
    """

    removed: bool
    artifacts_path: Path | None = None
    artifacts_skipped_reason: str | None = None

    def __bool__(self) -> bool:  # back-compat: ``if mgr.detach(...):``
        return self.removed
