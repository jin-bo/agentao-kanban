from __future__ import annotations

import threading
from pathlib import Path

import pytest

from kanban import CardPriority, CardStatus, KanbanOrchestrator
from kanban.daemon import (
    CombinedDaemon,
    DaemonConfig,
    SchedulerDaemon,
    WorkerDaemon,
)
from kanban.executors import MockAgentaoExecutor
from kanban.models import AgentRole, Card
from kanban.store_markdown import MarkdownBoardStore


def _make(board_dir: Path) -> tuple[MarkdownBoardStore, KanbanOrchestrator]:
    store = MarkdownBoardStore(board_dir)
    return store, KanbanOrchestrator(store=store, executor=MockAgentaoExecutor())


# ---------- orchestrator split ----------


def test_select_and_claim_creates_unassigned_claim_and_moves_ready(tmp_path: Path):
    store, orch = _make(tmp_path)
    card = store.add_card(
        Card(title="t", goal="g", status=CardStatus.READY, owner_role=AgentRole.WORKER)
    )

    claim = orch.select_and_claim(worker_id=None)
    assert claim is not None
    assert claim.card_id == card.id
    assert claim.worker_id is None  # unassigned
    assert claim.role == AgentRole.WORKER
    assert store.get_card(card.id).status == CardStatus.DOING


def test_select_and_claim_skips_cards_with_live_claim(tmp_path: Path):
    store, orch = _make(tmp_path)
    store.add_card(
        Card(title="a", goal="g", status=CardStatus.READY, owner_role=AgentRole.WORKER)
    )
    first = orch.select_and_claim(worker_id=None)
    assert first is not None
    # Same scheduler tick with nothing else ready → None (already claimed).
    assert orch.select_and_claim(worker_id=None) is None


def test_select_and_claim_respects_wip(tmp_path: Path):
    # wip_policy.doing_limit = 2. Three READY cards → only two claims per scheduler
    # sweep (third waits until one clears).
    store, orch = _make(tmp_path)
    for i in range(3):
        store.add_card(
            Card(
                title=f"c{i}",
                goal="g",
                status=CardStatus.READY,
                owner_role=AgentRole.WORKER,
                priority=CardPriority.HIGH,
            )
        )
    first = orch.select_and_claim(worker_id=None)
    second = orch.select_and_claim(worker_id=None)
    third = orch.select_and_claim(worker_id=None)
    assert first is not None and second is not None and third is None


# ---------- CAS race ----------


def test_try_acquire_claim_is_exclusive_under_concurrent_workers(tmp_path: Path):
    """Two workers racing for a single unassigned claim: exactly one wins."""
    store, orch = _make(tmp_path)
    store.add_card(
        Card(title="solo", goal="g", status=CardStatus.READY, owner_role=AgentRole.WORKER)
    )
    claim = orch.select_and_claim(worker_id=None)
    assert claim is not None

    results: list[object] = [None, None]
    start = threading.Event()

    def attempt(idx: int, worker_id: str) -> None:
        start.wait()
        results[idx] = store.try_acquire_claim(claim.card_id, worker_id=worker_id)

    t1 = threading.Thread(target=attempt, args=(0, "worker-a"), daemon=True)
    t2 = threading.Thread(target=attempt, args=(1, "worker-b"), daemon=True)
    t1.start()
    t2.start()
    start.set()
    t1.join(timeout=5.0)
    t2.join(timeout=5.0)
    if t1.is_alive() or t2.is_alive():
        pytest.fail("concurrent claim acquisition threads did not finish")

    winners = [r for r in results if r is not None]
    losers = [r for r in results if r is None]
    assert len(winners) == 1
    assert len(losers) == 1
    winning_worker = winners[0].worker_id  # type: ignore[union-attr]
    assert winning_worker in {"worker-a", "worker-b"}
    # And the persisted claim reflects the winner.
    got = store.get_claim(claim.card_id)
    assert got is not None and got.worker_id == winning_worker


def test_try_acquire_claim_returns_none_when_already_owned(tmp_path: Path):
    store, orch = _make(tmp_path)
    store.add_card(
        Card(title="x", goal="g", status=CardStatus.READY, owner_role=AgentRole.WORKER)
    )
    claim = orch.select_and_claim(worker_id=None)
    assert claim is not None
    first = store.try_acquire_claim(claim.card_id, worker_id="w1")
    second = store.try_acquire_claim(claim.card_id, worker_id="w2")
    assert first is not None and second is None


# ---------- two workers concurrent on two cards (plan M1 exit criterion) ----------


def test_two_workers_process_two_cards_concurrently(tmp_path: Path):
    """Two cards ready; two worker daemons pick one each, neither collides."""
    store, orch = _make(tmp_path)
    a = store.add_card(
        Card(title="A", goal="g", status=CardStatus.READY, owner_role=AgentRole.WORKER)
    )
    b = store.add_card(
        Card(title="B", goal="g", status=CardStatus.READY, owner_role=AgentRole.WORKER)
    )
    # Scheduler seeds two unassigned claims.
    c1 = orch.select_and_claim(worker_id=None)
    c2 = orch.select_and_claim(worker_id=None)
    assert c1 is not None and c2 is not None
    assert {c1.card_id, c2.card_id} == {a.id, b.id}

    store1 = MarkdownBoardStore(tmp_path)
    orch1 = KanbanOrchestrator(store=store1, executor=MockAgentaoExecutor())
    w1 = WorkerDaemon(
        orch1, config=DaemonConfig(max_idle_cycles=1, worker_id="worker-1")
    )

    store2 = MarkdownBoardStore(tmp_path)
    orch2 = KanbanOrchestrator(store=store2, executor=MockAgentaoExecutor())
    w2 = WorkerDaemon(
        orch2, config=DaemonConfig(max_idle_cycles=1, worker_id="worker-2")
    )

    # Each worker runs exactly one tick → each writes a result envelope.
    r1 = w1.run_once()
    r2 = w2.run_once()
    assert r1 and r2

    # Envelopes are pending commit; claims are still live (assigned to each worker).
    pending = MarkdownBoardStore(tmp_path).read_results()
    assert {e.card_id for e in pending} == {a.id, b.id}
    live_claims = MarkdownBoardStore(tmp_path).list_claims()
    assert {c.worker_id for c in live_claims} == {"worker-1", "worker-2"}

    # Scheduler commits both envelopes.
    committer_store = MarkdownBoardStore(tmp_path)
    committer = KanbanOrchestrator(
        store=committer_store, executor=MockAgentaoExecutor()
    )
    assert committer.commit_pending_results() == 2

    # Now claims and envelopes are cleared.
    fresh = MarkdownBoardStore(tmp_path)
    assert fresh.list_claims() == []
    assert fresh.read_results() == []
    # And both workers left presence records.
    worker_ids = {p.worker_id for p in fresh.list_workers()}
    assert worker_ids == {"worker-1", "worker-2"}


# ---------- combined + scheduler daemons ----------


def test_scheduler_daemon_fills_up_to_max_claims(tmp_path: Path):
    store, orch = _make(tmp_path)
    for i in range(4):
        store.add_card(
            Card(
                title=f"t{i}",
                goal="g",
                status=CardStatus.READY,
                owner_role=AgentRole.WORKER,
            )
        )
    sched = SchedulerDaemon(orch, config=DaemonConfig(max_claims=2, max_idle_cycles=1))
    sched.run_once()
    assert len(store.list_claims()) == 2


def test_scheduler_daemon_refreshes_external_unblock(tmp_path: Path):
    store, orch = _make(tmp_path)
    card = store.add_card(Card(title="t", goal="g"))
    store.update_card(card.id, blocked_reason="waiting")
    store.move_card(card.id, CardStatus.BLOCKED, "Blocked: waiting")

    sched = SchedulerDaemon(orch, config=DaemonConfig(max_claims=1, max_idle_cycles=1))

    external = MarkdownBoardStore(tmp_path)
    external.update_card(card.id, blocked_reason=None)
    external.move_card(card.id, CardStatus.READY, "Unblocked to ready")

    assert sched.run_once() is True

    fresh = MarkdownBoardStore(tmp_path)
    claim = fresh.get_claim(card.id)
    assert claim is not None
    assert fresh.get_card(card.id).status == CardStatus.DOING


def test_combined_daemon_runs_cards_to_completion(tmp_path: Path):
    store, orch = _make(tmp_path)
    card = store.add_card(
        Card(title="t", goal="g", acceptance_criteria=["x"])
    )
    daemon = CombinedDaemon(
        orch,
        config=DaemonConfig(max_idle_cycles=2, poll_interval=0.01, max_claims=2),
        orchestrator_factory=lambda: _make(tmp_path)[1],
    )
    daemon.run()
    final = store.get_card(card.id)
    assert final.status == CardStatus.DONE
    # No lingering claims after the board is idle.
    assert store.list_claims() == []


def test_worker_daemon_skips_when_no_unassigned_claim(tmp_path: Path):
    store, orch = _make(tmp_path)
    w = WorkerDaemon(orch, config=DaemonConfig(max_idle_cycles=1))
    # No cards, no claims → worker has nothing to do but still heartbeats.
    assert w.run_once() is False
    assert len(store.list_workers()) == 1


# ---------- legacy serial path unchanged ----------


def test_legacy_tick_still_ends_with_no_claim(tmp_path: Path):
    """The pre-v0.1.2 tick() path now creates and clears a claim each step;
    callers that only inspect card state should see the same final outcome."""
    store, orch = _make(tmp_path)
    card = store.add_card(Card(title="t", goal="g", acceptance_criteria=["x"]))
    processed = orch.run_until_idle(max_steps=20)
    assert processed, "legacy tick produced no steps"
    assert store.get_card(card.id).status == CardStatus.DONE
    assert store.list_claims() == []
