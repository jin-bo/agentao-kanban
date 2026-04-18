"""Unit tests for WorktreeManager."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from kanban.models import CardStatus
from kanban.worktree import WorktreeCreateError, WorktreeManager


def _init_repo(path: Path) -> Path:
    """Create a git repo with one commit and return the repo root."""
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=path, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=path, check=True, capture_output=True,
    )
    readme = path / "README.md"
    readme.write_text("init\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=path, check=True, capture_output=True,
    )
    return path


def _make_mgr(repo: Path) -> WorktreeManager:
    wt_root = repo / "workspace" / "worktrees"
    wt_root.mkdir(parents=True, exist_ok=True)
    return WorktreeManager(project_root=repo, worktrees_root=wt_root)


def test_create_and_get(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    mgr = _make_mgr(repo)

    info = mgr.create("card-001")
    assert info.card_id == "card-001"
    assert info.branch == "kanban/card-001"
    assert info.path is not None
    assert info.path.exists()
    assert len(info.base_commit) == 40
    assert info.head_commit == info.base_commit

    got = mgr.get("card-001", base_commit=info.base_commit)
    assert got is not None
    assert got.path is not None
    assert got.path.exists()
    assert got.branch == "kanban/card-001"


def test_create_duplicate_raises(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    mgr = _make_mgr(repo)
    mgr.create("card-dup")
    with pytest.raises(WorktreeCreateError):
        mgr.create("card-dup")


def test_detach_preserves_branch(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    mgr = _make_mgr(repo)
    info = mgr.create("card-det")
    assert info.path.exists()

    mgr.detach("card-det")
    assert not (mgr.worktrees_root / "card-det").exists()

    result = subprocess.run(
        ["git", "rev-parse", "--verify", "refs/heads/kanban/card-det"],
        cwd=repo, capture_output=True, text=True,
    )
    assert result.returncode == 0

    got = mgr.get("card-det", base_commit=info.base_commit)
    assert got is not None
    assert got.path is None


def test_prune_branch_merged(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    mgr = _make_mgr(repo)
    info = mgr.create("card-merge")

    # Make a commit in the worktree
    hello = info.path / "hello.txt"
    hello.write_text("hello\n")
    subprocess.run(["git", "add", "."], cwd=info.path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "add hello"],
        cwd=info.path, check=True, capture_output=True,
    )
    mgr.detach("card-merge")

    # Merge into main
    subprocess.run(
        ["git", "merge", "kanban/card-merge", "--no-edit"],
        cwd=repo, check=True, capture_output=True,
    )

    assert mgr.prune_branch("card-merge") is True
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "refs/heads/kanban/card-merge"],
        cwd=repo, capture_output=True, text=True,
    )
    assert result.returncode != 0


def test_prune_branch_unmerged_needs_force(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    mgr = _make_mgr(repo)
    info = mgr.create("card-unmerge")

    hello = info.path / "hello.txt"
    hello.write_text("hello\n")
    subprocess.run(["git", "add", "."], cwd=info.path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "add hello"],
        cwd=info.path, check=True, capture_output=True,
    )
    mgr.detach("card-unmerge")

    assert mgr.prune_branch("card-unmerge") is False
    assert mgr.prune_branch("card-unmerge", force=True) is True


def test_diff_summary_uses_base_commit(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    mgr = _make_mgr(repo)
    info = mgr.create("card-diff")

    hello = info.path / "hello.txt"
    hello.write_text("hello\n")
    subprocess.run(["git", "add", "."], cwd=info.path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "add hello"],
        cwd=info.path, check=True, capture_output=True,
    )

    diff = mgr.diff_summary("card-diff", info.base_commit)
    assert "hello.txt" in diff


def test_list_active(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    mgr = _make_mgr(repo)
    mgr.create("card-a")
    mgr.create("card-b")

    active = mgr.list_active()
    card_ids = {wt.card_id for wt in active}
    assert "card-a" in card_ids
    assert "card-b" in card_ids


def test_prune_stale(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    mgr = _make_mgr(repo)

    # Card 1: DONE + merged
    info1 = mgr.create("card-done")
    hello = info1.path / "hello.txt"
    hello.write_text("hello\n")
    subprocess.run(["git", "add", "."], cwd=info1.path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "add hello"],
        cwd=info1.path, check=True, capture_output=True,
    )
    mgr.detach("card-done")
    subprocess.run(
        ["git", "merge", "kanban/card-done", "--no-edit"],
        cwd=repo, check=True, capture_output=True,
    )

    # Card 2: BLOCKED + unmerged (use retention_days=0 to force prune)
    info2 = mgr.create("card-blocked")
    f2 = info2.path / "blocked.txt"
    f2.write_text("blocked\n")
    subprocess.run(["git", "add", "."], cwd=info2.path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "blocked work"],
        cwd=info2.path, check=True, capture_output=True,
    )
    mgr.detach("card-blocked")

    from datetime import datetime, timedelta, timezone

    statuses = {
        "card-done": CardStatus.DONE,
        "card-blocked": CardStatus.BLOCKED,
    }
    # Blocked a long time ago so retention window has expired.
    blocked_at = {
        "card-blocked": datetime.now(timezone.utc) - timedelta(days=30),
    }
    pruned = mgr.prune_stale(
        statuses, retention_days=0, card_blocked_at=blocked_at,
    )
    assert "card-done" in pruned
    assert "card-blocked" in pruned


def test_get_nonexistent(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    mgr = _make_mgr(repo)
    assert mgr.get("nonexistent") is None


def test_detach_auto_commits_uncommitted_changes(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    mgr = _make_mgr(repo)
    info = mgr.create("card-autocommit")

    # Write a file but do NOT commit
    (info.path / "wip.txt").write_text("work in progress\n")

    mgr.detach("card-autocommit")

    # Branch should contain the auto-committed file
    result = subprocess.run(
        ["git", "show", "kanban/card-autocommit:wip.txt"],
        cwd=repo, capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "work in progress" in result.stdout


def test_diff_summary_includes_uncommitted(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    mgr = _make_mgr(repo)
    info = mgr.create("card-uncommitted")

    # Uncommitted edit only (no git add/commit)
    (info.path / "draft.txt").write_text("draft\n")

    diff = mgr.diff_summary("card-uncommitted", info.base_commit)
    assert "draft.txt" in diff
    assert "Uncommitted" in diff


def test_create_without_preexisting_worktrees_dir(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    wt_root = repo / "workspace" / "worktrees"
    # Do NOT mkdir — let create() handle it
    mgr = WorktreeManager(project_root=repo, worktrees_root=wt_root)
    info = mgr.create("card-fresh")
    assert info.path.exists()
    assert wt_root.exists()


def test_recheckout_after_detach(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    mgr = _make_mgr(repo)
    info = mgr.create("card-rerun")

    (info.path / "work.txt").write_text("work\n")
    subprocess.run(["git", "add", "."], cwd=info.path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "work"], cwd=info.path, check=True, capture_output=True,
    )
    mgr.detach("card-rerun")
    assert not (mgr.worktrees_root / "card-rerun").exists()

    # Re-checkout the detached branch
    path = mgr.recheckout("card-rerun", "kanban/card-rerun")
    assert path is not None
    assert path.exists()
    assert (path / "work.txt").read_text() == "work\n"


def test_detach_preserved_when_autocommit_fails(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    mgr = _make_mgr(repo)
    info = mgr.create("card-hookfail")

    # Install a pre-commit hook in the main repo (worktrees share hooks)
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook = hooks_dir / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)

    (info.path / "wip.txt").write_text("uncommitted\n")

    ok = mgr.detach("card-hookfail")
    assert ok is False
    # Worktree should NOT be removed because auto-commit failed
    assert (mgr.worktrees_root / "card-hookfail").exists()
    assert (mgr.worktrees_root / "card-hookfail" / "wip.txt").exists()


def test_detach_returns_true_on_success(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    mgr = _make_mgr(repo)
    mgr.create("card-ok")
    assert mgr.detach("card-ok") is True


def test_diff_summary_raises_on_missing_branch(tmp_path: Path):
    from kanban.worktree import WorktreeDiffError

    repo = _init_repo(tmp_path / "repo")
    mgr = _make_mgr(repo)
    # Use a real base commit but a branch that was never created
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()
    with pytest.raises(WorktreeDiffError):
        mgr.diff_summary("nonexistent-card", base)


def test_diff_summary_raises_on_missing_base(tmp_path: Path):
    from kanban.worktree import WorktreeDiffError

    repo = _init_repo(tmp_path / "repo")
    mgr = _make_mgr(repo)
    info = mgr.create("card-diff-err")
    # Non-existent base commit
    with pytest.raises(WorktreeDiffError):
        mgr.diff_summary(info.card_id, "deadbeef" * 5)


def test_recheckout_after_external_directory_removal(tmp_path: Path):
    """If workspace/worktrees/<card> was rm -rf'd, recheckout must still work.

    `git worktree list` keeps a stale admin entry after external deletion,
    which would otherwise cause `git worktree add` to fail with
    "branch is already checked out".
    """
    import shutil

    repo = _init_repo(tmp_path / "repo")
    mgr = _make_mgr(repo)
    info = mgr.create("card-rmrf")

    (info.path / "work.txt").write_text("committed\n")
    subprocess.run(["git", "add", "."], cwd=info.path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=T",
         "commit", "-m", "work"],
        cwd=info.path, check=True, capture_output=True,
    )

    # Out-of-band deletion: stale admin entry remains in `git worktree list`
    shutil.rmtree(info.path)

    path = mgr.recheckout("card-rmrf", "kanban/card-rmrf")
    assert path is not None
    assert (path / "work.txt").read_text() == "committed\n"


def test_recheckout_replaces_stale_directory(tmp_path: Path):
    """If a non-worktree directory exists at the card path, remove and recreate."""
    repo = _init_repo(tmp_path / "repo")
    mgr = _make_mgr(repo)
    info = mgr.create("card-stale")

    (info.path / "work.txt").write_text("content\n")
    subprocess.run(["git", "add", "."], cwd=info.path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=T",
         "commit", "-m", "work"],
        cwd=info.path, check=True, capture_output=True,
    )
    mgr.detach("card-stale")

    # Plant a stale directory (not a worktree) at the expected path
    stale = mgr.worktrees_root / "card-stale"
    stale.mkdir(parents=True, exist_ok=True)
    (stale / "junk.txt").write_text("unrelated\n")

    path = mgr.recheckout("card-stale", "kanban/card-stale")
    assert path is not None
    # Stale junk should be gone; real branch content should be there
    assert not (path / "junk.txt").exists()
    assert (path / "work.txt").read_text() == "content\n"


def test_prune_stale_blocked_uses_card_blocked_at(tmp_path: Path):
    """BLOCKED cards must use card.blocked_at, not the branch tip commit date."""
    from datetime import datetime, timedelta, timezone

    repo = _init_repo(tmp_path / "repo")
    mgr = _make_mgr(repo)
    info = mgr.create("card-fresh-block")
    # Do NOT make a new commit — branch tip is the (possibly old) base commit
    mgr.detach("card-fresh-block")

    # Freshly blocked just now
    now = datetime.now(timezone.utc)
    statuses = {"card-fresh-block": CardStatus.BLOCKED}
    blocked_at = {"card-fresh-block": now}

    # retention_days=7, blocked just now → must NOT be pruned
    pruned = mgr.prune_stale(
        statuses, retention_days=7, card_blocked_at=blocked_at,
    )
    assert "card-fresh-block" not in pruned

    # Blocked 8 days ago → should be pruned
    blocked_at2 = {"card-fresh-block": now - timedelta(days=8)}
    pruned2 = mgr.prune_stale(
        statuses, retention_days=7, card_blocked_at=blocked_at2,
    )
    assert "card-fresh-block" in pruned2

    # No blocked_at recorded → conservatively skip
    pruned3 = mgr.prune_stale(statuses, retention_days=0, card_blocked_at={})
    assert "card-fresh-block" not in pruned3


def test_worktrees_root_added_to_git_exclude(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    wt_root = repo / "workspace" / "worktrees"
    wt_root.mkdir(parents=True, exist_ok=True)
    # Instantiating the manager should add the path to .git/info/exclude
    WorktreeManager(project_root=repo, worktrees_root=wt_root)
    exclude_text = (repo / ".git" / "info" / "exclude").read_text()
    assert "/workspace/worktrees/" in exclude_text

    # Creating a worktree should then not show up as untracked in main checkout
    mgr = WorktreeManager(project_root=repo, worktrees_root=wt_root)
    mgr.create("card-gitignore")
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo, capture_output=True, text=True, check=True,
    )
    assert "workspace/worktrees" not in status.stdout


def test_detach_without_user_config(tmp_path: Path):
    """Detach must work even without git user.name/user.email configured."""
    # Create repo using --bare-style init with NO user config
    repo = tmp_path / "noconfig"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    # Only set identity for initial commit via inline -c flags
    readme = repo / "README.md"
    readme.write_text("init\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git",
         "-c", "user.email=init@test",
         "-c", "user.name=Init",
         "commit", "-m", "init"],
        cwd=repo, check=True, capture_output=True,
    )

    mgr = WorktreeManager(
        project_root=repo, worktrees_root=repo / "workspace" / "worktrees",
    )
    info = mgr.create("card-nocfg")

    # Uncommitted edit — auto-commit must work without user config
    (info.path / "work.txt").write_text("work\n")

    assert mgr.detach("card-nocfg") is True
    assert not (mgr.worktrees_root / "card-nocfg").exists()

    # Branch has the auto-committed file
    result = subprocess.run(
        ["git", "show", "kanban/card-nocfg:work.txt"],
        cwd=repo, capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "work" in result.stdout


def test_get_ignores_branch_checked_out_in_main_repo(tmp_path: Path):
    """A kanban/<id> branch checked out in the main repo must not be mistaken
    for a managed worktree — otherwise workers would run in the shared
    checkout and detach()/prune_stale() would miss the active checkout.
    """
    repo = _init_repo(tmp_path / "repo")
    mgr = _make_mgr(repo)

    # Create the branch but check it out in the main repo, not the managed root.
    subprocess.run(
        ["git", "branch", "kanban/card-escape"],
        cwd=repo, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "checkout", "kanban/card-escape"],
        cwd=repo, check=True, capture_output=True,
    )

    info = mgr.get("card-escape", base_commit="deadbeef")
    assert info is not None, "branch exists so get() should return info"
    # But path must not be the main repo — the branch is effectively detached
    # from the manager's perspective.
    assert info.path is None

    # list_active must not surface the main-repo checkout either.
    active = mgr.list_active()
    assert all(wt.card_id != "card-escape" for wt in active)
