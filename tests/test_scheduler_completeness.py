"""Regression tests for the third round of Codex adversarial findings:

1. ``select_and_claim`` used to return None on the first actionable card
   that already had a live claim — so one claimed front card could starve
   the scheduler's remaining ``max_claims`` capacity. The fix iterates
   every actionable card in priority order and skips the already-claimed
   ones.
2. Recovery paths (stale-claim recovery + failed-envelope handling) used
   to ``clear_claim`` *before* establishing the replacement state. A
   failure in ``create_claim`` / ``update_card`` / ``move_card`` then
   stranded the card in an execution status without a live claim — the
   scheduler never picks those up again. The fix is transactional:
   retry_claim rolls back to the old claim on create failure, and the
   BLOCKED path moves status first so the claim is cleared only after
   the terminal transition is durable.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from kanban import CardPriority, CardStatus, KanbanOrchestrator
from kanban.daemon import DaemonConfig, SchedulerDaemon
from kanban.executors import MockAgentaoExecutor
from kanban.models import (
    AgentRole,
    Card,
    FailureCategory,
    utc_now,
)
from kanban.orchestrator import WipPolicy
from kanban.store_markdown import MarkdownBoardStore


def _make(
    board: Path, *, doing_limit: int = 2
) -> tuple[MarkdownBoardStore, KanbanOrchestrator]:
    s = MarkdownBoardStore(board)
    orch = KanbanOrchestrator(
        store=s,
        executor=MockAgentaoExecutor(),
        wip_policy=WipPolicy(doing_limit=doing_limit),
    )
    return s, orch


# ---------- Fix 1: scheduler scans past already-claimed cards ----------


def test_scheduler_fills_capacity_behind_claimed_card(tmp_path: Path):
    """Card A is in VERIFY with a live claim. Cards B and C are in READY
    and runnable. Before the fix, the scheduler would return None on A
    and stop. After the fix, it skips A and claims B (and C)."""
    store, orch = _make(tmp_path, doing_limit=10)
    a = store.add_card(
        Card(
            title="A",
            goal="g",
            status=CardStatus.VERIFY,
            owner_role=AgentRole.VERIFIER,
            priority=CardPriority.HIGH,
        )
    )
    b = store.add_card(
        Card(
            title="B",
            goal="g",
            status=CardStatus.READY,
            owner_role=AgentRole.WORKER,
            priority=CardPriority.HIGH,
        )
    )
    c = store.add_card(
        Card(
            title="C",
            goal="g",
            status=CardStatus.READY,
            owner_role=AgentRole.WORKER,
            priority=CardPriority.HIGH,
        )
    )

    # Simulate: A is already claimed (e.g. by a worker that hasn't finished).
    first = orch.select_and_claim(worker_id=None)
    assert first is not None and first.card_id == a.id

    # Remaining calls should go to B, then C — not bail because A is claimed.
    second = orch.select_and_claim(worker_id=None)
    third = orch.select_and_claim(worker_id=None)
    assert second is not None and second.card_id == b.id
    assert third is not None and third.card_id == c.id
    assert orch.select_and_claim(worker_id=None) is None  # now exhausted


def test_scheduler_daemon_max_claims_not_starved_by_claimed_front_card(
    tmp_path: Path,
):
    """End-to-end: SchedulerDaemon with max_claims=3 must fill all three
    slots even when the highest-priority actionable card is already
    claimed. Uses a relaxed WIP so the READY→DOING path isn't throttled."""
    store, orch = _make(tmp_path, doing_limit=10)
    a = store.add_card(
        Card(
            title="A",
            goal="g",
            status=CardStatus.REVIEW,
            owner_role=AgentRole.REVIEWER,
            priority=CardPriority.CRITICAL,
        )
    )
    for i in range(3):
        store.add_card(
            Card(
                title=f"R{i}",
                goal="g",
                status=CardStatus.READY,
                owner_role=AgentRole.WORKER,
                priority=CardPriority.MEDIUM,
            )
        )
    # Pre-claim A to simulate an in-flight reviewer run.
    pre = orch.select_and_claim(worker_id=None)
    assert pre is not None and pre.card_id == a.id

    sched = SchedulerDaemon(orch, config=DaemonConfig(max_claims=3, max_idle_cycles=1))
    sched.run_once()

    live = store.list_claims()
    # Should have 3 live claims total (including the pre-existing A claim).
    assert len(live) == 3
    claimed_cards = {c.card_id for c in live}
    assert a.id in claimed_cards  # pre-existing claim preserved
    # And at least 2 of the READY cards got claimed.
    assert len(claimed_cards - {a.id}) == 2


# ---------- Fix 2: transactional retry rollback ----------


def test_retry_claim_rolls_back_old_claim_if_create_fails(
    tmp_path: Path, monkeypatch
):
    """If ``create_claim`` fails when building the retry claim, the old
    claim must be restored — otherwise the card sits in DOING with no
    claim and the scheduler never sees it again."""
    store, orch = _make(tmp_path)
    card = store.add_card(
        Card(
            title="t",
            goal="g",
            status=CardStatus.READY,
            owner_role=AgentRole.WORKER,
            acceptance_criteria=["x"],
        )
    )
    original = orch.select_and_claim(worker_id=None)
    assert original is not None

    real_create = store.create_claim
    calls = {"n": 0}

    def flaky(claim):
        calls["n"] += 1
        # Fail on the RETRY create (the one with attempt>1), but allow the
        # rollback create to succeed so we can observe the invariant.
        if claim.attempt > 1:
            raise RuntimeError("simulated fs error on retry create")
        return real_create(claim)

    monkeypatch.setattr(store, "create_claim", flaky)

    with pytest.raises(RuntimeError):
        orch.retry_claim(
            original, reason="test", category=FailureCategory.INFRASTRUCTURE
        )

    # Invariant: card still has a live claim, same claim_id as before.
    live = store.get_claim(card.id)
    assert live is not None
    assert live.claim_id == original.claim_id
    assert store.get_card(card.id).status == CardStatus.DOING


def test_block_path_moves_status_before_clearing_claim(
    tmp_path: Path, monkeypatch
):
    """When retry budget is exhausted, move_card(BLOCKED) must happen
    before clear_claim. A move_card failure must leave the claim intact
    so the next scheduler tick can redo the recovery."""
    store, orch = _make(tmp_path)
    card = store.add_card(
        Card(
            title="t",
            goal="g",
            status=CardStatus.READY,
            owner_role=AgentRole.WORKER,
        )
    )
    claim = orch.select_and_claim(worker_id=None)
    assert claim is not None
    # Fake-exhaust the budget by stamping retry_count high.
    store.clear_claim(card.id)
    exhausted = replace(claim, retry_count=5, attempt=6)
    store.create_claim(exhausted)

    real_move = store.move_card

    def flaky_move(card_id, status, note):
        if card_id == card.id and status == CardStatus.BLOCKED:
            raise RuntimeError("simulated fs error on BLOCKED move")
        return real_move(card_id, status, note)

    monkeypatch.setattr(store, "move_card", flaky_move)

    # Trigger the block path by submitting a final-attempt infra failure.
    store.try_acquire_claim(card.id, worker_id="w")
    live = store.get_claim(card.id)
    with pytest.raises(RuntimeError):
        orch._retry_or_block(live, FailureCategory.INFRASTRUCTURE, "boom")

    # Invariant: claim still live (cleared only AFTER terminal transition).
    # Card status not yet BLOCKED (move_card failed).
    assert store.get_claim(card.id) is not None
    assert store.get_card(card.id).status != CardStatus.BLOCKED


def test_stale_recovery_never_strands_card_on_create_failure(
    tmp_path: Path, monkeypatch
):
    """End-to-end: a stale claim becomes runnable again even if the retry
    create fails once. Next scheduler pass should see the original claim
    (restored by rollback) and try recovery again."""
    store, orch = _make(tmp_path)
    card = store.add_card(
        Card(
            title="t",
            goal="g",
            status=CardStatus.READY,
            owner_role=AgentRole.WORKER,
        )
    )
    claim = orch.select_and_claim(worker_id=None)
    assert claim is not None
    # Force the lease to be expired.
    store.clear_claim(card.id)
    store.create_claim(
        replace(
            claim,
            lease_expires_at=utc_now() - timedelta(seconds=30),
            worker_id="dead",
        )
    )

    real_create = store.create_claim
    attempts = {"n": 0}

    def flaky(c):
        attempts["n"] += 1
        # Fail the FIRST retry create; allow rollback restore and later retries.
        if attempts["n"] == 1:
            raise RuntimeError("simulated fs error")
        return real_create(c)

    # Patch only for one recovery pass.
    monkeypatch.setattr(store, "create_claim", flaky)
    with pytest.raises(RuntimeError):
        orch.recover_stale_claims()

    # Invariant: card still has a live claim (the original, restored).
    live = store.get_claim(card.id)
    assert live is not None
    assert live.claim_id == claim.claim_id
    assert store.get_card(card.id).status == CardStatus.DOING

    # Restore the real create_claim; a second recovery pass succeeds.
    monkeypatch.setattr(store, "create_claim", real_create)
    orch.recover_stale_claims()
    retry = store.get_claim(card.id)
    assert retry is not None and retry.attempt == 2
