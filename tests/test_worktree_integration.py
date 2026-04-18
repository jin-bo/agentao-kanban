"""Integration tests for worktree isolation with the full kanban stack."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from kanban import CardStatus, KanbanOrchestrator, MarkdownBoardStore
from kanban.executors import MockAgentaoExecutor
from kanban.models import AgentRole, CardPriority
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
    readme = path / "README.md"
    readme.write_text("init\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=path, check=True, capture_output=True,
    )
    return path


def _make_stack(repo: Path):
    board_dir = repo / "workspace" / "board"
    board_dir.mkdir(parents=True, exist_ok=True)
    store = MarkdownBoardStore(board_dir)
    executor = MockAgentaoExecutor()
    wt_mgr = WorktreeManager(
        project_root=repo,
        worktrees_root=repo / "workspace" / "worktrees",
    )
    (repo / "workspace" / "worktrees").mkdir(parents=True, exist_ok=True)
    orch = KanbanOrchestrator(store=store, executor=executor, worktree_mgr=wt_mgr)
    return store, orch, wt_mgr


def test_full_lifecycle(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    store, orch, wt_mgr = _make_stack(repo)

    card = orch.create_card("Test Card", "Write hello.py", priority=CardPriority.HIGH)

    # Tick 1: planner (INBOX → READY) — no worktree
    result_card = orch.tick()
    assert result_card is not None
    card = store.get_card(card.id)
    assert card.status == CardStatus.READY
    assert card.worktree_branch is None

    # Tick 2: worker — worktree created
    result_card = orch.tick()
    assert result_card is not None
    card = store.get_card(card.id)
    assert card.status == CardStatus.REVIEW
    assert card.worktree_branch == f"kanban/{card.id}"
    assert card.worktree_base_commit is not None
    assert len(card.worktree_base_commit) == 40

    # Tick 3: reviewer — worktree reused
    result_card = orch.tick()
    assert result_card is not None
    card = store.get_card(card.id)
    assert card.status == CardStatus.VERIFY

    # Tick 4: verifier → DONE → worktree detached
    result_card = orch.tick()
    assert result_card is not None
    card = store.get_card(card.id)
    assert card.status == CardStatus.DONE

    wt_path = repo / "workspace" / "worktrees" / card.id
    assert not wt_path.exists()

    # Branch should still exist
    result = subprocess.run(
        ["git", "rev-parse", "--verify", f"refs/heads/kanban/{card.id}"],
        cwd=repo, capture_output=True, text=True,
    )
    assert result.returncode == 0


def test_worktree_disabled_by_default(tmp_path: Path):
    board_dir = tmp_path / "board"
    board_dir.mkdir(parents=True)
    store = MarkdownBoardStore(board_dir)
    executor = MockAgentaoExecutor()
    orch = KanbanOrchestrator(store=store, executor=executor)

    card = orch.create_card("No WT", "test")
    orch.tick()  # planner
    orch.tick()  # worker

    card = store.get_card(card.id)
    assert card.worktree_branch is None
    assert card.worktree_base_commit is None


def test_retry_claim_preserves_worktree_path(tmp_path: Path):
    from kanban.models import FailureCategory

    repo = _init_repo(tmp_path / "repo")
    store, orch, wt_mgr = _make_stack(repo)

    card = orch.create_card("Retry Test", "test retry preserves worktree")
    orch.tick()  # planner → READY

    # Worker claim with worktree
    claim = orch.select_and_claim(worker_id="w1")
    assert claim is not None
    assert claim.role == AgentRole.WORKER
    assert claim.worktree_path is not None
    worktree_path = claim.worktree_path

    # Simulate infrastructure failure → retry
    new_claim = orch.retry_claim(
        claim, reason="transient net error", category=FailureCategory.INFRASTRUCTURE,
    )
    assert new_claim.worktree_path == worktree_path


def test_recreate_worktree_after_external_prune(tmp_path: Path):
    """Card metadata persists but branch is gone — select_and_claim must recover."""
    import subprocess

    repo = _init_repo(tmp_path / "repo")
    store, orch, wt_mgr = _make_stack(repo)

    card = orch.create_card("Prune Recovery", "test")
    orch.tick()  # planner
    orch.tick()  # worker — creates worktree

    card = store.get_card(card.id)
    original_branch = card.worktree_branch
    assert original_branch is not None

    # Simulate external prune: kill worktree directory AND branch while card
    # metadata still references them.
    wt_mgr.detach(card.id)
    subprocess.run(
        ["git", "branch", "-D", original_branch],
        cwd=repo, check=True, capture_output=True,
    )

    # Reset card to READY so worker runs again
    store.update_card(card.id, owner_role=None)
    store.move_card(card.id, CardStatus.READY, "manual requeue for test")

    # Next worker claim must rebuild isolation from scratch
    claim = orch.select_and_claim(worker_id="w1")
    assert claim is not None
    assert claim.role == AgentRole.WORKER
    assert claim.worktree_path is not None
    card = store.get_card(card.id)
    # Metadata got refreshed to point at the new branch
    assert card.worktree_branch is not None


def test_in_memory_store_accepts_worktree_events():
    """InMemoryBoardStore must accept worktree_branch kwarg on runtime events."""
    from kanban.store import InMemoryBoardStore

    store = InMemoryBoardStore()
    # Should not raise TypeError
    from kanban.models import Card
    card = Card(title="T", goal="g")
    store.add_card(card)
    store.append_runtime_event(
        card.id,
        event_type="worktree.created",
        message="test",
        worktree_branch="kanban/x",
    )
    events = store.events_for_card(card.id)
    assert any(e.worktree_branch == "kanban/x" for e in events)


def test_retry_with_missing_reviewer_branch_blocks(tmp_path: Path):
    """retry_claim must block when reviewer/verifier branch was deleted."""
    import subprocess
    from kanban.models import FailureCategory

    repo = _init_repo(tmp_path / "repo")
    store, orch, wt_mgr = _make_stack(repo)

    card = orch.create_card("Review Retry", "test")
    orch.tick()  # planner
    orch.tick()  # worker → REVIEW with worktree
    card = store.get_card(card.id)
    assert card.status == CardStatus.REVIEW

    # Claim for reviewer
    claim = orch.select_and_claim(worker_id="w1")
    assert claim is not None
    assert claim.role == AgentRole.REVIEWER

    # Destroy branch + worktree
    wt_mgr.detach(card.id)
    subprocess.run(
        ["git", "branch", "-D", card.worktree_branch],
        cwd=repo, check=True, capture_output=True,
    )

    # Try to retry — should block the card, not silently run in main checkout
    orch._retry_or_block(claim, FailureCategory.INFRASTRUCTURE, "transient fail")
    card = store.get_card(card.id)
    assert card.status == CardStatus.BLOCKED
    assert "missing" in (card.blocked_reason or "").lower()


def test_migrated_review_card_without_worktree_blocks(tmp_path: Path):
    """A pre-worktree board with cards already in REVIEW must not leak to main checkout."""
    repo = _init_repo(tmp_path / "repo")
    store, orch, wt_mgr = _make_stack(repo)

    # Create a card and advance it to REVIEW without any worktree metadata
    # (simulates pre-worktree board upgraded to --worktree mode)
    card = orch.create_card("Legacy Review", "test")
    store.move_card(card.id, CardStatus.READY, "manual")
    store.move_card(card.id, CardStatus.DOING, "manual")
    store.move_card(card.id, CardStatus.REVIEW, "manual")

    card = store.get_card(card.id)
    assert card.worktree_branch is None  # pre-worktree card

    # Reviewer claim — must BLOCK rather than run in main checkout
    claim = orch.select_and_claim(worker_id="w1")
    assert claim is None
    card = store.get_card(card.id)
    assert card.status == CardStatus.BLOCKED
    assert "worktree" in (card.blocked_reason or "").lower()


def test_reviewer_blocks_when_branch_missing(tmp_path: Path):
    """If the worker branch was deleted, REVIEWER must not fall back to main checkout."""
    import subprocess

    repo = _init_repo(tmp_path / "repo")
    store, orch, wt_mgr = _make_stack(repo)

    card = orch.create_card("Reviewer Recovery", "test")
    orch.tick()  # planner
    orch.tick()  # worker → REVIEW, worktree detached at detach stage? No — worktree preserved through REVIEW/VERIFY.

    card = store.get_card(card.id)
    assert card.status == CardStatus.REVIEW
    branch = card.worktree_branch
    assert branch is not None

    # Simulate external destruction of the branch AND worktree
    wt_mgr.detach(card.id)
    subprocess.run(
        ["git", "branch", "-D", branch],
        cwd=repo, check=True, capture_output=True,
    )

    # Reviewer claim — should BLOCK, not run in main checkout
    claim = orch.select_and_claim(worker_id="w1")
    assert claim is None
    card = store.get_card(card.id)
    assert card.status == CardStatus.BLOCKED
    assert "missing" in (card.blocked_reason or "").lower()


def test_select_and_claim_skips_blocked_card_continues_to_next(tmp_path: Path):
    """A high-priority card with invalid worktree must not stall the queue.

    Regression: select_and_claim() previously returned None after blocking
    the bad card, so SchedulerDaemon.run_once() treated that as "queue
    drained" and stopped scheduling for the remainder of the tick.
    """
    repo = _init_repo(tmp_path / "repo")
    store, orch, wt_mgr = _make_stack(repo)

    # High-priority card pre-staged into REVIEW with no worktree → will block.
    bad = orch.create_card(
        "Migrated Bad", "review without worktree", priority=CardPriority.HIGH,
    )
    store.move_card(bad.id, CardStatus.READY, "manual")
    store.move_card(bad.id, CardStatus.DOING, "manual")
    store.move_card(bad.id, CardStatus.REVIEW, "manual")

    # Lower-priority card that should still get claimed in the same tick.
    good = orch.create_card(
        "Healthy Inbox", "normal flow", priority=CardPriority.LOW,
    )

    claim = orch.select_and_claim(worker_id="w1")
    assert claim is not None, "scheduler must continue past the blocked card"
    assert claim.card_id == good.id

    bad_after = store.get_card(bad.id)
    assert bad_after.status == CardStatus.BLOCKED


def test_detach_on_blocked_transition(tmp_path: Path):
    """A reviewer/verifier result that moves the card to BLOCKED must
    detach the worktree, otherwise workspace/worktrees/<card> stays
    attached forever — prune_stale() skips cards whose directory still
    exists, so blocked branches would accumulate until manual cleanup.
    """
    from kanban.models import AgentResult

    repo = _init_repo(tmp_path / "repo")
    store, orch, wt_mgr = _make_stack(repo)

    card = orch.create_card("Blocked Flow", "test blocked detach")
    orch.tick()  # planner → READY
    orch.tick()  # worker → REVIEW, worktree created

    card = store.get_card(card.id)
    assert card.status == CardStatus.REVIEW
    assert card.worktree_branch is not None
    wt_path = repo / "workspace" / "worktrees" / card.id
    assert wt_path.exists()

    # Simulate a reviewer/verifier rejection that routes the card to BLOCKED.
    rejection = AgentResult(
        role=AgentRole.REVIEWER,
        summary="rejected: missing tests",
        next_status=CardStatus.BLOCKED,
        updates={"blocked_reason": "rejected: missing tests"},
    )
    orch._apply_result(card.id, rejection)

    card = store.get_card(card.id)
    assert card.status == CardStatus.BLOCKED
    assert not wt_path.exists(), "worktree dir must be cleaned up on BLOCKED"

    # Branch preserved so diff_summary / manual recovery remains possible.
    result = subprocess.run(
        ["git", "rev-parse", "--verify", f"refs/heads/{card.worktree_branch}"],
        cwd=repo, capture_output=True, text=True,
    )
    assert result.returncode == 0


def test_working_directory_injection(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    store, orch, wt_mgr = _make_stack(repo)

    card = orch.create_card("WD Test", "test working dir injection")
    orch.tick()  # planner

    # Before worker tick, check executor has no working_directory
    original_wd = getattr(orch.executor, "working_directory", None)

    orch.tick()  # worker

    # After tick, working_directory should be restored
    after_wd = getattr(orch.executor, "working_directory", None)
    assert after_wd == original_wd


class _WDNoneExecutor(MockAgentaoExecutor):
    """MultiBackendExecutor-style stand-in: declares ``working_directory``
    as an attribute whose default is ``None``. Used to pin the regression
    where the cleanup path used ``None`` as the "no prior value" sentinel
    and incorrectly deleted the attribute after the first worktree-backed
    run — breaking every subsequent run with ``AttributeError``.
    """

    working_directory: Path | None = None

    def __init__(self) -> None:
        self.working_directory = None


def test_working_directory_preserved_when_default_is_none(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    board_dir = repo / "workspace" / "board"
    board_dir.mkdir(parents=True, exist_ok=True)
    store = MarkdownBoardStore(board_dir)
    executor = _WDNoneExecutor()
    wt_mgr = WorktreeManager(
        project_root=repo,
        worktrees_root=repo / "workspace" / "worktrees",
    )
    (repo / "workspace" / "worktrees").mkdir(parents=True, exist_ok=True)
    orch = KanbanOrchestrator(store=store, executor=executor, worktree_mgr=wt_mgr)

    orch.create_card("WD None 1", "first card")
    orch.tick()  # planner
    orch.tick()  # worker — first worktree-backed run

    assert hasattr(executor, "working_directory"), (
        "cleanup must not delete working_directory when its default is None"
    )
    assert executor.working_directory is None

    orch.create_card("WD None 2", "second card")
    # Planner + worker for the second card must succeed without AttributeError.
    for _ in range(4):
        orch.tick()

    assert executor.working_directory is None


def test_cli_prune_emits_worktree_pruned_event(tmp_path: Path, capsys):
    """`kanban worktree prune` must append the worktree.pruned runtime
    event so the manual path is visible in events.log alongside the
    scheduler's idle-prune path.
    """
    from argparse import Namespace

    from kanban.cli import cmd_worktree_prune
    from kanban.models import Card

    repo = _init_repo(tmp_path / "repo")
    board_dir = repo / "workspace" / "board"
    board_dir.mkdir(parents=True, exist_ok=True)
    store = MarkdownBoardStore(board_dir)
    wt_mgr = WorktreeManager(
        project_root=repo,
        worktrees_root=repo / "workspace" / "worktrees",
    )
    (repo / "workspace" / "worktrees").mkdir(parents=True, exist_ok=True)

    # DONE card with a branch but no active worktree → eligible for prune.
    card = store.add_card(Card(title="Prune Me", goal="test prune event"))
    wt_mgr.create(card.id)
    wt_mgr.detach(card.id)
    store.update_card(
        card.id,
        worktree_branch=f"kanban/{card.id}",
        worktree_base_commit="0" * 40,
    )
    store.move_card(card.id, CardStatus.DONE, "mock lifecycle")

    args = Namespace(board=board_dir, retention_days=0)
    rc = cmd_worktree_prune(args)
    assert rc == 0
    assert f"Pruned kanban/{card.id}" in capsys.readouterr().out

    events_path = board_dir / "events.log"
    assert events_path.exists()
    matched = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    pruned_events = [
        ev for ev in matched
        if ev.get("event_type") == "worktree.pruned" and ev.get("card_id") == card.id
    ]
    assert len(pruned_events) == 1, (
        f"expected one worktree.pruned event, got {pruned_events}"
    )
    assert pruned_events[0].get("worktree_branch") == f"kanban/{card.id}"

    # Re-open the store to pick up mutations made by cmd_worktree_prune's
    # own MarkdownBoardStore instance (the test's store has a stale cache).
    refreshed = MarkdownBoardStore(board_dir).get_card(card.id)
    assert refreshed.worktree_branch is None
    assert refreshed.worktree_base_commit is None
