from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from kanban import CardStatus, KanbanOrchestrator
from kanban.daemon import DaemonConfig, SchedulerDaemon, WorkerDaemon
from kanban.executors import MockAgentaoExecutor
from kanban.models import (
    AgentResult,
    AgentRole,
    Card,
    ExecutionClaim,
    ExecutionResultEnvelope,
    FailureCategory,
    utc_now,
)
from kanban.store_markdown import MarkdownBoardStore


def _make(board: Path) -> tuple[MarkdownBoardStore, KanbanOrchestrator]:
    s = MarkdownBoardStore(board)
    return s, KanbanOrchestrator(store=s, executor=MockAgentaoExecutor())


def _ready_card(store: MarkdownBoardStore, title: str = "t") -> Card:
    return store.add_card(
        Card(
            title=title,
            goal="g",
            status=CardStatus.READY,
            owner_role=AgentRole.WORKER,
            acceptance_criteria=["x"],
        )
    )


# ---------- submit_result + commit_pending_results ----------


def test_worker_submit_then_scheduler_commit_applies_result(tmp_path: Path):
    store, orch = _make(tmp_path)
    card = _ready_card(store)

    # Scheduler creates claim; worker executes + submits envelope.
    claim = orch.select_and_claim(worker_id=None)
    assert claim is not None
    # Simulate worker side without going through WorkerDaemon.
    store.try_acquire_claim(claim.card_id, worker_id="w1")
    updated_claim = store.get_claim(claim.card_id)
    assert updated_claim is not None
    result = orch.executor.run(updated_claim.role, store.get_card(card.id))
    env = orch.submit_result(
        updated_claim, result, worker_id="w1", started_at=utc_now()
    )
    assert env.ok is True

    # Pre-commit: envelope exists, claim still live.
    assert len(store.read_results()) == 1
    assert store.get_claim(card.id) is not None

    # Committer applies and clears.
    committed = orch.commit_pending_results()
    assert committed == 1
    assert store.read_results() == []
    assert store.get_claim(card.id) is None
    # Card advanced per the mock executor (ready→doing→review).
    assert store.get_card(card.id).status == CardStatus.REVIEW


def test_worker_failed_envelope_blocks_card_when_unretryable(tmp_path: Path):
    """Functional rejection has zero retry budget → BLOCKED on first failure."""
    store, orch = _make(tmp_path)
    card = _ready_card(store)
    claim = orch.select_and_claim(worker_id=None)
    assert claim is not None
    store.try_acquire_claim(claim.card_id, worker_id="w1")
    live = store.get_claim(claim.card_id)
    assert live is not None

    orch.submit_result(
        live,
        None,
        worker_id="w1",
        started_at=utc_now(),
        ok=False,
        failure_reason="rejected",
        failure_category=FailureCategory.FUNCTIONAL,
    )
    orch.commit_pending_results()

    got = store.get_card(card.id)
    assert got.status == CardStatus.BLOCKED
    assert got.blocked_reason is not None and "rejected" in got.blocked_reason
    assert "functional" in got.blocked_reason


def test_commit_survives_scheduler_restart(tmp_path: Path):
    """Envelope persisted to disk; a brand-new orchestrator instance can commit it."""
    store, orch = _make(tmp_path)
    card = _ready_card(store)
    claim = orch.select_and_claim(worker_id=None)
    assert claim is not None
    store.try_acquire_claim(claim.card_id, worker_id="w1")
    live = store.get_claim(claim.card_id)
    assert live is not None
    result = orch.executor.run(live.role, store.get_card(card.id))
    orch.submit_result(live, result, worker_id="w1", started_at=utc_now())

    # Brand-new process reopens the board and commits.
    store2 = MarkdownBoardStore(tmp_path)
    orch2 = KanbanOrchestrator(store=store2, executor=MockAgentaoExecutor())
    assert orch2.commit_pending_results() == 1
    assert store2.read_results() == []
    assert store2.get_claim(card.id) is None


# ---------- orphan results ----------


def test_envelope_with_wrong_claim_id_is_quarantined(tmp_path: Path):
    store, orch = _make(tmp_path)
    card = _ready_card(store)
    old_claim = orch.select_and_claim(worker_id=None)
    assert old_claim is not None

    # Simulate a stale-recovery cycle: replace the live claim with a fresh
    # one for the same card (different claim_id). A late envelope from the
    # old worker then arrives.
    from dataclasses import replace

    store.clear_claim(card.id)
    new_claim = replace(old_claim, claim_id="clm-new", worker_id=None)
    store.create_claim(new_claim)

    stale_env = ExecutionResultEnvelope(
        card_id=card.id,
        claim_id=old_claim.claim_id,
        role=AgentRole.WORKER,
        attempt=1,
        started_at=utc_now(),
        finished_at=utc_now(),
        duration_ms=10,
        ok=True,
        agent_result=AgentResult(
            role=AgentRole.WORKER,
            summary="stale",
            next_status=CardStatus.REVIEW,
            updates={},
        ),
        worker_id="w-old",
    )
    store.write_result(stale_env)

    # Committer sees mismatch → orphans the envelope, does NOT apply it.
    orch.commit_pending_results()
    assert store.read_results() == []
    orphans = store.list_orphan_results()
    assert len(orphans) == 1 and orphans[0].claim_id == old_claim.claim_id
    # Current live claim untouched; card still in DOING (not advanced to REVIEW).
    live = store.get_claim(card.id)
    assert live is not None and live.claim_id == "clm-new"
    assert store.get_card(card.id).status == CardStatus.DOING


def test_envelope_with_no_live_claim_is_quarantined(tmp_path: Path):
    store, orch = _make(tmp_path)
    card = _ready_card(store)
    claim = orch.select_and_claim(worker_id=None)
    assert claim is not None
    store.try_acquire_claim(claim.card_id, worker_id="w1")
    live = store.get_claim(claim.card_id)
    assert live is not None
    result = orch.executor.run(live.role, store.get_card(card.id))
    orch.submit_result(live, result, worker_id="w1", started_at=utc_now())

    # Claim cleared before the committer runs.
    store.clear_claim(card.id, claim_id=live.claim_id)
    orch.commit_pending_results()
    assert store.read_results() == []
    assert len(store.list_orphan_results()) == 1


# ---------- stale claim recovery ----------


def test_stale_claim_on_final_retry_moves_card_to_blocked(tmp_path: Path):
    """Lease expiry has retry budget 1; a claim whose retry_count already
    equals the budget blocks the card on the next stale recovery pass."""
    store, orch = _make(tmp_path)
    card = _ready_card(store)
    claim = orch.select_and_claim(worker_id=None)
    assert claim is not None
    from dataclasses import replace

    exhausted = replace(
        claim,
        lease_expires_at=utc_now() - timedelta(seconds=30),
        worker_id="w-dead",
        retry_count=1,  # budget used up
        attempt=2,
    )
    store.clear_claim(card.id)
    store.create_claim(exhausted)

    recovered = orch.recover_stale_claims()
    assert recovered == 1
    got = store.get_card(card.id)
    assert got.status == CardStatus.BLOCKED
    assert got.blocked_reason is not None
    assert "lease_expiry" in got.blocked_reason
    assert store.get_claim(card.id) is None


def test_scheduler_daemon_tick_commits_and_recovers(tmp_path: Path):
    store, orch = _make(tmp_path)
    good = _ready_card(store, "good")
    stuck = _ready_card(store, "stuck")

    # Two claims: one will get a normal envelope, one will go stale.
    claim_good = orch.select_and_claim(worker_id=None)
    claim_stuck = orch.select_and_claim(worker_id=None)
    assert claim_good is not None and claim_stuck is not None

    # Simulate a worker finishing good.
    store.try_acquire_claim(claim_good.card_id, worker_id="w1")
    live_good = store.get_claim(claim_good.card_id)
    assert live_good is not None
    result = orch.executor.run(live_good.role, store.get_card(good.id))
    orch.submit_result(live_good, result, worker_id="w1", started_at=utc_now())

    # Expire the stuck one with retry budget already exhausted so it blocks.
    from dataclasses import replace

    store.clear_claim(stuck.id)
    store.create_claim(
        replace(
            claim_stuck,
            lease_expires_at=utc_now() - timedelta(seconds=30),
            worker_id="w-dead",
            retry_count=1,
            attempt=2,
        )
    )

    sched = SchedulerDaemon(orch, config=DaemonConfig(max_claims=2, max_idle_cycles=1))
    assert sched.run_once() is True

    # Envelope applied; stuck card blocked.
    assert store.read_results() == []
    assert store.get_card(good.id).status == CardStatus.REVIEW
    assert store.get_card(stuck.id).status == CardStatus.BLOCKED
    # The original stuck claim is gone; any live claim for good belongs to
    # the new REVIEW phase, not the old WORKER attempt.
    for claim in store.list_claims():
        assert claim.card_id != stuck.id
        if claim.card_id == good.id:
            assert claim.role == AgentRole.REVIEWER


# ---------- end-to-end through the split topology ----------


def test_split_topology_runs_card_end_to_end(tmp_path: Path):
    """Scheduler and worker daemons alternating cycles complete a card."""
    store, orch = _make(tmp_path)
    card = _ready_card(store)

    sched = SchedulerDaemon(orch, config=DaemonConfig(max_claims=1))
    worker_store = MarkdownBoardStore(tmp_path)
    worker_orch = KanbanOrchestrator(
        store=worker_store, executor=MockAgentaoExecutor()
    )
    worker = WorkerDaemon(
        worker_orch, config=DaemonConfig(worker_id="w1", max_claims=1)
    )

    for _ in range(20):
        sched.run_once()
        worker.run_once()
        fresh = MarkdownBoardStore(tmp_path)
        if fresh.get_card(card.id).status == CardStatus.DONE:
            break
    fresh = MarkdownBoardStore(tmp_path)
    assert fresh.get_card(card.id).status == CardStatus.DONE
    assert fresh.list_claims() == []
    assert fresh.read_results() == []


# ---------- legacy tick still works ----------


def test_legacy_tick_unaffected_by_envelope_path(tmp_path: Path):
    store, orch = _make(tmp_path)
    card = _ready_card(store)
    processed = orch.run_until_idle(max_steps=20)
    assert processed and store.get_card(card.id).status == CardStatus.DONE
    # Legacy path does not produce envelopes or orphans.
    assert store.read_results() == []
    assert store.list_orphan_results() == []
    assert store.list_claims() == []
