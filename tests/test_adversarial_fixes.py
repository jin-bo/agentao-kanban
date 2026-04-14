"""Regression tests for the two high-severity findings in the second
Codex adversarial review (design-level concerns).

1. A live executor run must not be falsely declared stale mid-flight.
   Before the fix, ``WorkerDaemon`` set the lease once at acquire and
   never renewed it, so any task running past the lease window would be
   recovered while the worker was still computing. The fix runs a
   background heartbeat thread that renews the lease on an interval
   and only stops when (a) the worker finishes, or (b) total elapsed
   exceeds the role timeout.

2. ``select_and_claim`` must not leave a card in DOING without a live
   claim. Before the fix the orchestrator moved READY→DOING and THEN
   tried to create the claim — a ``create_claim`` failure (stale
   sentinel, fs error) stranded the card until a human intervened. The
   fix persists the claim first and rolls it back on move_card failure.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from kanban import CardStatus, KanbanOrchestrator
from kanban.daemon import DaemonConfig, WorkerDaemon
from kanban.executors import MockAgentaoExecutor
from kanban.models import (
    AgentRole,
    Card,
    LeasePolicy,
    utc_now,
)
from kanban.store_markdown import MarkdownBoardStore


def _ready(store: MarkdownBoardStore, title: str = "t") -> Card:
    return store.add_card(
        Card(
            title=title,
            goal="g",
            status=CardStatus.READY,
            owner_role=AgentRole.WORKER,
            acceptance_criteria=["x"],
        )
    )


# ---------- Fix 1: heartbeat keeps long runs alive ----------


def test_heartbeat_renews_lease_across_long_executor_run(tmp_path: Path):
    """Worker runs an executor that takes longer than the initial lease.
    Without the heartbeat thread, the scheduler would see a stale claim
    mid-flight. With it, the lease is renewed and recovery does NOT fire."""

    # Short lease and heartbeat so the test runs in seconds.
    lease_policy = LeasePolicy(
        lease_seconds=1, heartbeat_seconds=0.2, timeout_by_role={"worker": 30}
    )

    class SlowExecutor:
        def __init__(self) -> None:
            self.inner = MockAgentaoExecutor()

        def run(self, role, card):
            # Sleep past two full lease windows to prove renewal is happening.
            time.sleep(2.5)
            return self.inner.run(role, card)

    store = MarkdownBoardStore(tmp_path)
    orch = KanbanOrchestrator(
        store=store, executor=SlowExecutor(), lease_policy=lease_policy
    )
    card = _ready(store)
    claim = orch.select_and_claim(worker_id=None)
    assert claim is not None

    worker = WorkerDaemon(orch, config=DaemonConfig(worker_id="slow-worker"))
    acquired = worker._acquire_any_claim()
    assert acquired is not None
    assert acquired.worker_id == "slow-worker"

    # Sample the claim periodically while the executor runs on a thread.
    ran: dict[str, Any] = {}

    def go() -> None:
        ran["card"] = orch.store.get_card(card.id)
        started_at = utc_now()
        with worker._heartbeat_claim(acquired):
            result = orch.executor.run(acquired.role, ran["card"])
        orch.submit_result(
            acquired,
            result,
            worker_id=worker.worker_id,
            started_at=started_at,
            ok=True,
        )

    t = threading.Thread(target=go)
    t.start()

    # Poll mid-flight: stale-claim recovery must see no stale claim.
    deadline = time.monotonic() + 2.0
    saw_live_claim = False
    while time.monotonic() < deadline:
        fresh = MarkdownBoardStore(tmp_path)
        stale = fresh.list_stale_claims()
        assert stale == [], f"claim reported stale mid-run: {stale}"
        live = fresh.get_claim(card.id)
        if live is not None and live.worker_id == "slow-worker":
            saw_live_claim = True
        time.sleep(0.3)
    assert saw_live_claim, "claim was never observed live during the run"
    t.join(timeout=5)

    # Envelope committed cleanly.
    orch.commit_pending_results()
    assert store.get_card(card.id).status != CardStatus.BLOCKED


def test_heartbeat_stops_renewing_once_timeout_exceeded(tmp_path: Path):
    """Past the role timeout, the heartbeat thread must stop renewing so
    the scheduler can recover a runaway task as TIMEOUT / LEASE_EXPIRY."""

    lease_policy = LeasePolicy(
        lease_seconds=1, heartbeat_seconds=0.1, timeout_by_role={"worker": 1}
    )
    store = MarkdownBoardStore(tmp_path)
    orch = KanbanOrchestrator(
        store=store, executor=MockAgentaoExecutor(), lease_policy=lease_policy
    )
    card = _ready(store)
    claim = orch.select_and_claim(worker_id=None)
    assert claim is not None
    worker = WorkerDaemon(orch, config=DaemonConfig(worker_id="runaway"))
    acquired = worker._acquire_any_claim()
    assert acquired is not None

    # Enter the heartbeat context without running the executor, then wait
    # past timeout_s. The heartbeat thread should stop renewing, so the
    # lease naturally expires.
    with worker._heartbeat_claim(acquired):
        time.sleep(2.5)
        # After stop, the lease should be stale.
        fresh = MarkdownBoardStore(tmp_path)
        stale = fresh.list_stale_claims()
        assert len(stale) == 1 and stale[0].card_id == card.id


# ---------- Fix 2: claim-then-status is atomic ----------


def test_create_claim_failure_leaves_card_in_ready(tmp_path: Path, monkeypatch):
    """If create_claim raises, the card must NOT be advanced to DOING —
    otherwise the next scheduler tick won't see a claimable/actionable card
    and the card is stranded."""
    store = MarkdownBoardStore(tmp_path)
    orch = KanbanOrchestrator(store=store, executor=MockAgentaoExecutor())
    card = _ready(store)

    original = store.create_claim
    calls = {"n": 0}

    def flaky(claim):
        calls["n"] += 1
        raise RuntimeError("simulated fs error")

    monkeypatch.setattr(store, "create_claim", flaky)

    import pytest as _p

    with _p.raises(RuntimeError):
        orch.select_and_claim(worker_id=None)

    # Card stayed in READY, no claim got written.
    assert store.get_card(card.id).status == CardStatus.READY
    assert store.get_claim(card.id) is None
    # Restore so a retry works normally.
    monkeypatch.setattr(store, "create_claim", original)
    claim = orch.select_and_claim(worker_id=None)
    assert claim is not None
    assert store.get_card(card.id).status == CardStatus.DOING


def test_move_card_failure_rolls_back_claim(tmp_path: Path, monkeypatch):
    """If create_claim succeeds but move_card fails, the orchestrator must
    clear the claim so the next tick retries cleanly — otherwise we're left
    with a live claim for a READY card (workers would still execute it, but
    the doing-state semantics on the claim are wrong)."""
    store = MarkdownBoardStore(tmp_path)
    orch = KanbanOrchestrator(store=store, executor=MockAgentaoExecutor())
    card = _ready(store)

    original_move = store.move_card

    def flaky_move(card_id, status, note):
        if card_id == card.id and status == CardStatus.DOING:
            raise RuntimeError("simulated fs error")
        return original_move(card_id, status, note)

    monkeypatch.setattr(store, "move_card", flaky_move)

    import pytest as _p

    with _p.raises(RuntimeError):
        orch.select_and_claim(worker_id=None)

    # Claim rolled back; card still in READY.
    assert store.get_claim(card.id) is None
    assert store.get_card(card.id).status == CardStatus.READY
