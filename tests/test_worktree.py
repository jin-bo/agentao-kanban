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


def test_diff_summary_missing_base_commit(tmp_path: Path):
    from kanban.worktree import WorktreeDiffError

    repo = _init_repo(tmp_path / "repo")
    mgr = _make_mgr(repo)
    mgr.create("card-nb")
    with pytest.raises(WorktreeDiffError) as ei:
        mgr.diff_summary("card-nb", "")
    assert "base_commit" in str(ei.value)


def test_diff_summary_passes_timeout_to_git(tmp_path: Path, monkeypatch):
    """Every ``git`` subprocess ``diff_summary`` spawns must carry the
    configured timeout — a hung repo would otherwise pin the caller (the
    web ``/diff`` route runs on a threadpool)."""
    import kanban.worktree as wt_mod

    repo = _init_repo(tmp_path / "repo")
    mgr = _make_mgr(repo)
    info = mgr.create("card-to")
    # An active worktree so the uncommitted-changes branch (the direct
    # subprocess.run calls) runs too, not just the self._git() calls.
    (info.path / "draft.txt").write_text("draft\n")

    seen_timeouts: list = []
    real_run = wt_mod.subprocess.run

    def spy(*args, **kwargs):
        seen_timeouts.append(kwargs.get("timeout"))
        return real_run(*args, **kwargs)

    monkeypatch.setattr(wt_mod.subprocess, "run", spy)
    mgr.diff_summary("card-to", info.base_commit)
    assert seen_timeouts, "expected diff_summary to shell out to git"
    assert all(t == mgr.git_diff_timeout_s for t in seen_timeouts)


def test_diff_summary_wraps_timeout_as_diff_error(tmp_path: Path, monkeypatch):
    import kanban.worktree as wt_mod
    from kanban.worktree import WorktreeDiffError

    repo = _init_repo(tmp_path / "repo")
    mgr = _make_mgr(repo)
    info = mgr.create("card-hang")

    def boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd=args[0] if args else "git", timeout=kwargs.get("timeout") or 0.0
        )

    monkeypatch.setattr(wt_mod.subprocess, "run", boom)
    with pytest.raises(WorktreeDiffError) as ei:
        mgr.diff_summary("card-hang", info.base_commit)
    assert "timed out" in str(ei.value)


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
    assert ok.removed is False
    assert bool(ok) is False
    # Worktree should NOT be removed because auto-commit failed
    assert (mgr.worktrees_root / "card-hookfail").exists()
    assert (mgr.worktrees_root / "card-hookfail" / "wip.txt").exists()


def test_detach_returns_true_on_success(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    mgr = _make_mgr(repo)
    mgr.create("card-ok")
    result = mgr.detach("card-ok")
    assert result.removed is True
    assert bool(result) is True


# ---------- artifact rescue (gitignored deliverables) ----------


def _mgr_with_artifacts(repo: Path) -> WorktreeManager:
    """WorktreeManager configured to snapshot ignored content on detach."""
    wt_root = repo / "workspace" / "worktrees"
    wt_root.mkdir(parents=True, exist_ok=True)
    return WorktreeManager(
        project_root=repo,
        worktrees_root=wt_root,
        artifacts_root=repo / "workspace" / "raw",
    )


def _add_gitignore(repo: Path, pattern: str) -> None:
    gi = repo / ".gitignore"
    existing = gi.read_text() if gi.exists() else ""
    gi.write_text(existing + pattern + "\n")
    subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "ignore"],
        cwd=repo, check=True, capture_output=True,
    )


def test_detach_rescues_gitignored_deliverable(tmp_path: Path):
    """The aed6a19e regression: a worker writes to workspace/reports/...,
    that path is gitignored, _auto_commit can't see it, and the file
    must survive detach via the artifacts snapshot.
    """
    repo = _init_repo(tmp_path / "repo")
    _add_gitignore(repo, "workspace/")
    mgr = _mgr_with_artifacts(repo)
    info = mgr.create("card-deliv")

    deliverable = info.path / "workspace" / "reports" / "ai-news.md"
    deliverable.parent.mkdir(parents=True, exist_ok=True)
    deliverable.write_text("# AI news\n\n- item 1\n- item 2\n", encoding="utf-8")

    result = mgr.detach("card-deliv")
    assert result.removed is True
    assert result.artifacts_path is not None
    saved = result.artifacts_path / "workspace" / "reports" / "ai-news.md"
    assert saved.exists(), f"deliverable not snapshotted at {saved}"
    assert "AI news" in saved.read_text(encoding="utf-8")
    # Worktree dir is gone, but the snapshot persists outside it.
    assert not (mgr.worktrees_root / "card-deliv").exists()


def test_detach_no_artifacts_when_only_tracked_changes(tmp_path: Path):
    """Untracked-not-ignored changes are caught by _auto_commit; we
    intentionally don't duplicate them in the snapshot."""
    repo = _init_repo(tmp_path / "repo")
    mgr = _mgr_with_artifacts(repo)
    info = mgr.create("card-tracked")
    (info.path / "src.py").write_text("print('hi')\n")  # untracked, not ignored

    result = mgr.detach("card-tracked")
    assert result.removed is True
    assert result.artifacts_path is None
    assert result.artifacts_skipped_reason == "no-artifacts"


def test_detach_partial_snapshot_under_size_cap(tmp_path: Path):
    """Per-file accounting: keep what fits, skip the oversized rest.

    Replaces the old all-or-nothing behavior. The whole point is that
    a giant deliverable shouldn't crowd out smaller real outputs sitting
    next to it.
    """
    repo = _init_repo(tmp_path / "repo")
    _add_gitignore(repo, "workspace/")
    wt_root = repo / "workspace" / "worktrees"
    wt_root.mkdir(parents=True, exist_ok=True)
    # Use a temp directory outside `workspace/` since the gitignore
    # would otherwise let `workspace/data/` files be re-ignored when
    # the worktree is created. Place gitignore on the literal path.
    mgr = WorktreeManager(
        project_root=repo,
        worktrees_root=wt_root,
        artifacts_root=repo / "workspace" / "raw",
        artifacts_max_bytes=300,  # fits ~300 bytes, not 1 KiB
        artifacts_denylist=(),    # disable denylist so size cap is the only filter
    )
    info = mgr.create("card-mixed")
    small = info.path / "workspace" / "report.txt"
    small.parent.mkdir(parents=True, exist_ok=True)
    small.write_bytes(b"keepme\n")  # 7 bytes
    big = info.path / "workspace" / "huge.bin"
    big.write_bytes(b"x" * 1024)    # 1024 bytes — over remaining budget

    result = mgr.detach("card-mixed")
    assert result.removed is True
    assert result.artifacts_path is not None  # partial save still wins
    assert (result.artifacts_path / "workspace" / "report.txt").exists()
    assert not (result.artifacts_path / "workspace" / "huge.bin").exists()


def test_detach_size_cap_exceeded_when_nothing_fits(tmp_path: Path):
    """If even the smallest file exceeds the cap, the snapshot is empty
    and the reason is reported (regression for the original 'all-or-
    nothing' contract on hopelessly small caps)."""
    repo = _init_repo(tmp_path / "repo")
    _add_gitignore(repo, "workspace/")
    wt_root = repo / "workspace" / "worktrees"
    wt_root.mkdir(parents=True, exist_ok=True)
    mgr = WorktreeManager(
        project_root=repo,
        worktrees_root=wt_root,
        artifacts_root=repo / "workspace" / "raw",
        artifacts_max_bytes=8,  # smaller than any realistic file
        artifacts_denylist=(),
    )
    info = mgr.create("card-tiny")
    big = info.path / "workspace" / "out.bin"
    big.parent.mkdir(parents=True, exist_ok=True)
    big.write_bytes(b"x" * 256)

    result = mgr.detach("card-tiny")
    assert result.removed is True
    assert result.artifacts_path is None
    assert result.artifacts_skipped_reason == "size-cap-exceeded"


def test_detach_denylist_skips_cache_dirs(tmp_path: Path):
    """node_modules / __pycache__ / build caches must never count
    against the size budget — they aren't deliverables."""
    repo = _init_repo(tmp_path / "repo")
    _add_gitignore(repo, "workspace/")
    mgr = _mgr_with_artifacts(repo)
    info = mgr.create("card-junk")

    junk = info.path / "workspace" / "node_modules" / "lodash" / "index.js"
    junk.parent.mkdir(parents=True, exist_ok=True)
    junk.write_bytes(b"junk\n")
    pyc = info.path / "workspace" / "src" / "__pycache__" / "mod.cpython-312.pyc"
    pyc.parent.mkdir(parents=True, exist_ok=True)
    pyc.write_bytes(b"\x00" * 64)
    real = info.path / "workspace" / "report.md"
    real.write_bytes(b"# real\n")

    result = mgr.detach("card-junk")
    assert result.artifacts_path is not None
    saved = result.artifacts_path
    assert (saved / "workspace" / "report.md").exists()
    assert not (saved / "workspace" / "node_modules").exists()
    assert not (saved / "workspace" / "src" / "__pycache__").exists()


def test_artifacts_max_bytes_env_override(tmp_path: Path, monkeypatch):
    """KANBAN_ARTIFACTS_MAX_BYTES overrides the dataclass default at
    __post_init__ time, without touching call sites."""
    monkeypatch.setenv("KANBAN_ARTIFACTS_MAX_BYTES", "1024")
    repo = _init_repo(tmp_path / "repo")
    mgr = _mgr_with_artifacts(repo)
    assert mgr.artifacts_max_bytes == 1024


def test_artifacts_max_bytes_env_invalid_ignored(
    tmp_path: Path, monkeypatch, caplog
):
    monkeypatch.setenv("KANBAN_ARTIFACTS_MAX_BYTES", "not-a-number")
    repo = _init_repo(tmp_path / "repo")
    with caplog.at_level("WARNING", logger="kanban.worktree"):
        mgr = _mgr_with_artifacts(repo)
    # Falls back to the dataclass default rather than crashing.
    from kanban.worktree import DEFAULT_ARTIFACTS_MAX_BYTES
    assert mgr.artifacts_max_bytes == DEFAULT_ARTIFACTS_MAX_BYTES
    assert any("KANBAN_ARTIFACTS_MAX_BYTES" in r.message for r in caplog.records)


def test_detach_disabled_artifacts_root(tmp_path: Path):
    """Without an artifacts_root, behavior matches the pre-rescue era:
    no snapshot is created, the dataclass reports None silently."""
    repo = _init_repo(tmp_path / "repo")
    _add_gitignore(repo, "workspace/")
    mgr = _make_mgr(repo)  # artifacts_root=None
    info = mgr.create("card-noartroot")
    deliverable = info.path / "workspace" / "out.txt"
    deliverable.parent.mkdir(parents=True, exist_ok=True)
    deliverable.write_text("doomed\n")

    result = mgr.detach("card-noartroot")
    assert result.removed is True
    assert result.artifacts_path is None
    assert result.artifacts_skipped_reason is None  # disabled, not skipped


def test_detach_artifacts_retention(tmp_path: Path):
    """Multiple terminal cycles for the same card keep at most N snapshots."""
    repo = _init_repo(tmp_path / "repo")
    _add_gitignore(repo, "workspace/")
    wt_root = repo / "workspace" / "worktrees"
    wt_root.mkdir(parents=True, exist_ok=True)
    mgr = WorktreeManager(
        project_root=repo,
        worktrees_root=wt_root,
        artifacts_root=repo / "workspace" / "raw",
        artifacts_retention=2,
    )
    for i in range(4):
        info = mgr.create("card-rotate")
        (info.path / "workspace" / f"file-{i}.txt").parent.mkdir(
            parents=True, exist_ok=True,
        )
        (info.path / "workspace" / f"file-{i}.txt").write_text(f"v{i}\n")
        result = mgr.detach("card-rotate")
        assert result.artifacts_path is not None
        # Branch must be deleted between cycles so create() can reuse the id.
        mgr.prune_branch("card-rotate", force=True)

    snapshots = sorted(
        (repo / "workspace" / "raw" / "card-rotate").glob("artifacts-*")
    )
    assert len(snapshots) == 2, [s.name for s in snapshots]


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

    assert mgr.detach("card-nocfg").removed is True
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
