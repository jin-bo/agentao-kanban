"""Tests for ``GET /api/cards/{card_id}/diff`` — the Web equivalent of
``kanban worktree diff <card-id>``.

Key invariants:

- States that can't produce a diff (``none`` / ``not-git`` / ``missing``)
  return HTTP 200 with a human-readable ``message`` and ``diff == None`` —
  the route must never 500 just because a card has no worktree.
- ``active`` / ``detached`` branches return the ``git diff --stat`` text.
- A missing ``base_commit`` surfaces as a message, not a crash.
- Read-only; no ``--enable-writes`` needed, and probing must not mutate
  the repo (no ``.git/info/exclude`` write).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from kanban.models import Card, CardStatus
from kanban.store_markdown import MarkdownBoardStore
from kanban.web import create_app
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
        ["git", "commit", "-m", "initial"], cwd=path, check=True, capture_output=True
    )
    return path


def _mgr(repo: Path) -> WorktreeManager:
    return WorktreeManager.for_project(repo)


def test_diff_unknown_card_404(tmp_path: Path) -> None:
    board = tmp_path / "board"
    MarkdownBoardStore(board).add_card(Card(title="x", goal="g"))
    r = TestClient(create_app(board)).get("/api/cards/nope/diff")
    assert r.status_code == 404


def test_diff_none_state(tmp_path: Path) -> None:
    board = tmp_path / "board"
    store = MarkdownBoardStore(board)
    card = store.add_card(Card(title="fresh", goal="g"))
    r = TestClient(create_app(board)).get(f"/api/cards/{card.id}/diff")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "none"
    assert body["diff"] is None
    assert body["branch"] is None
    assert body["message"]  # explains there's nothing to diff


def test_diff_not_git_board(tmp_path: Path) -> None:
    # Board not inside a Git repo, but the card carries worktree metadata.
    board = tmp_path / "board"
    store = MarkdownBoardStore(board)
    card_id = "11111111-0000-0000-0000-000000000000"
    store.add_card(
        Card(
            id=card_id,
            title="not-git",
            goal="g",
            worktree_branch=f"kanban/{card_id}",
            worktree_base_commit="abc123",
        )
    )
    r = TestClient(create_app(board)).get(f"/api/cards/{card_id}/diff")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "not-git"
    assert body["diff"] is None
    assert "Git repository" in body["message"]


def test_diff_missing_branch(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    board = repo / "workspace" / "board"
    store = MarkdownBoardStore(board)
    card_id = "22222222-0000-0000-0000-000000000000"
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    # Worktree metadata recorded, but the kanban/<id> branch never existed
    # (or was force-deleted) — state should be "missing", not a crash.
    store.add_card(
        Card(
            id=card_id,
            title="ghost branch",
            goal="g",
            status=CardStatus.DONE,
            worktree_branch=f"kanban/{card_id}",
            worktree_base_commit=head,
        )
    )
    r = TestClient(create_app(board)).get(f"/api/cards/{card_id}/diff")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "missing"
    assert body["diff"] is None
    assert "prune" in body["message"]


def test_diff_detached_branch_with_committed_change(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    board = repo / "workspace" / "board"
    store = MarkdownBoardStore(board)
    card_id = "33333333-0000-0000-0000-000000000000"
    mgr = _mgr(repo)
    info = mgr.create(card_id)
    (info.path / "feature.txt").write_text("new feature\n")
    subprocess.run(["git", "add", "."], cwd=info.path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "add feature"], cwd=info.path, check=True, capture_output=True
    )
    # Detach: drop the worktree dir, keep the branch.
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(info.path)],
        cwd=repo, check=True, capture_output=True,
    )
    store.add_card(
        Card(
            id=card_id,
            title="detached work",
            goal="g",
            status=CardStatus.DONE,
            worktree_branch=info.branch,
            worktree_base_commit=info.base_commit,
        )
    )
    r = TestClient(create_app(board)).get(f"/api/cards/{card_id}/diff")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "detached"
    assert body["truncated"] is False
    assert "feature.txt" in (body["diff"] or "")


def test_diff_active_branch_with_untracked_file(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    board = repo / "workspace" / "board"
    store = MarkdownBoardStore(board)
    card_id = "44444444-0000-0000-0000-000000000000"
    mgr = _mgr(repo)
    info = mgr.create(card_id)
    (info.path / "scratch.txt").write_text("wip\n")  # untracked, uncommitted
    store.add_card(
        Card(
            id=card_id,
            title="active work",
            goal="g",
            worktree_branch=info.branch,
            worktree_base_commit=info.base_commit,
        )
    )
    r = TestClient(create_app(board)).get(f"/api/cards/{card_id}/diff")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "active"
    assert "scratch.txt" in (body["diff"] or "")


def test_diff_active_branch_missing_base_commit_message(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    board = repo / "workspace" / "board"
    store = MarkdownBoardStore(board)
    card_id = "55555555-0000-0000-0000-000000000000"
    mgr = _mgr(repo)
    info = mgr.create(card_id)
    # Branch + active worktree exist, but the card recorded no base commit.
    store.add_card(
        Card(
            id=card_id,
            title="no base",
            goal="g",
            worktree_branch=info.branch,
            worktree_base_commit=None,
        )
    )
    r = TestClient(create_app(board)).get(f"/api/cards/{card_id}/diff")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "active"
    assert body["diff"] is None
    assert "base_commit" in body["message"]


def test_diff_endpoint_is_side_effect_free(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    exclude = repo / ".git" / "info" / "exclude"
    if exclude.exists():
        exclude.unlink()
    board = repo / "workspace" / "board"
    store = MarkdownBoardStore(board)
    card_id = "66666666-0000-0000-0000-000000000000"
    store.add_card(
        Card(
            id=card_id,
            title="probe me",
            goal="g",
            worktree_branch=f"kanban/{card_id}",
            worktree_base_commit="deadbeef",
        )
    )
    r = TestClient(create_app(board)).get(f"/api/cards/{card_id}/diff")
    assert r.status_code == 200
    assert not exclude.exists()
