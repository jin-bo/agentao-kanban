"""Regression test for the Codex review finding that
``kanban worktree prune`` mutated card metadata and appended runtime
events without ever calling ``_require_writable``. With a scheduler /
legacy / all daemon holding ``.daemon.lock``, the manual prune raced
the daemon's own worktree bookkeeping. The fix wires the same guard
every other mutating CLI command uses.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from kanban.cli import main
from kanban.daemon import LOCK_FILENAME


def _git_init(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    for key, value in (("user.email", "t@t.com"), ("user.name", "T")):
        subprocess.run(
            ["git", "config", key, value],
            cwd=path, check=True, capture_output=True,
        )
    (path / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=path, check=True, capture_output=True,
    )
    return path


def _write_live_lock(board: Path) -> None:
    board.mkdir(parents=True, exist_ok=True)
    (board / LOCK_FILENAME).write_text(
        json.dumps({"pid": os.getpid(), "started_at": time.time()}),
        encoding="utf-8",
    )


def test_worktree_prune_refuses_while_daemon_lock_held(tmp_path: Path):
    repo = _git_init(tmp_path / "repo")
    board = repo / "workspace" / "board"
    _write_live_lock(board)

    with pytest.raises(SystemExit) as excinfo:
        main(["--board", str(board), "worktree", "prune"])
    # `_require_writable` exits with code 2 (matches every other
    # daemon-locked write path in cli.py).
    assert excinfo.value.code == 2


def test_worktree_prune_runs_with_force_despite_lock(tmp_path: Path):
    repo = _git_init(tmp_path / "repo")
    board = repo / "workspace" / "board"
    _write_live_lock(board)

    # --force bypasses the guard for recovery; with no stale branches the
    # command exits cleanly.
    rc = main(["--force", "--board", str(board), "worktree", "prune"])
    assert rc == 0
