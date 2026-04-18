"""Tri-state resolution tests for the ``--worktree`` CLI flag.

The CLI flag is tri-state:

- ``None`` (default, no flag): auto — enable inside a Git repo, else warn+off.
- ``True`` (``--worktree``): hard-require a Git repo, SystemExit if none.
- ``False`` (``--no-worktree``): off.

These tests exercise the pure resolver so they don't depend on argparse.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import pytest

from kanban.cli import _resolve_worktree_mgr
from kanban.worktree import WorktreeManager


def _init_repo(path: Path) -> Path:
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=path, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=path, check=True, capture_output=True,
    )
    (path / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=path, check=True, capture_output=True,
    )
    return path


def _args(board: Path, worktree: bool | None) -> argparse.Namespace:
    return argparse.Namespace(board=board, worktree=worktree)


def test_auto_enabled_inside_git_repo(tmp_path: Path, capsys):
    repo = _init_repo(tmp_path / "repo")
    board = repo / "workspace" / "board"
    mgr = _resolve_worktree_mgr(_args(board, None))
    assert isinstance(mgr, WorktreeManager)
    assert mgr.project_root == repo
    assert mgr.worktrees_root == repo / "workspace" / "worktrees"
    # Auto success path must be silent.
    assert capsys.readouterr().err == ""


def test_auto_disabled_outside_git_repo(tmp_path: Path, capsys):
    board = tmp_path / "not-a-repo" / "board"
    mgr = _resolve_worktree_mgr(_args(board, None))
    assert mgr is None
    err = capsys.readouterr().err
    assert "worktree isolation disabled" in err
    assert "Git repo" in err


def test_explicit_on_hard_requires_git_repo(tmp_path: Path):
    board = tmp_path / "not-a-repo" / "board"
    with pytest.raises(SystemExit) as excinfo:
        _resolve_worktree_mgr(_args(board, True))
    assert "requires a Git repository" in str(excinfo.value)


def test_explicit_off_returns_none_even_in_git_repo(tmp_path: Path, capsys):
    repo = _init_repo(tmp_path / "repo")
    board = repo / "workspace" / "board"
    mgr = _resolve_worktree_mgr(_args(board, False))
    assert mgr is None
    # Explicit off must never warn.
    assert capsys.readouterr().err == ""


def test_explicit_on_inside_git_repo_returns_manager(tmp_path: Path, capsys):
    repo = _init_repo(tmp_path / "repo")
    board = repo / "workspace" / "board"
    mgr = _resolve_worktree_mgr(_args(board, True))
    assert isinstance(mgr, WorktreeManager)
    assert mgr.project_root == repo
    assert capsys.readouterr().err == ""
