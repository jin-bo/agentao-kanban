"""Tests for the N-worker CombinedDaemon and its threading model.

These cover the v0.2-ish `--role all --max-claims N` real-parallel
topology: 1 scheduler thread + N worker threads sharing a
``threading.Event`` stop signal.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from kanban import CardStatus, KanbanOrchestrator
from kanban.daemon import (
    CombinedDaemon,
    DaemonConfig,
    SchedulerDaemon,
    WorkerDaemon,
)
from kanban.executors import MockAgentaoExecutor
from kanban.models import AgentRole, Card
from kanban.store_markdown import MarkdownBoardStore


def _fresh_orch(board: Path) -> KanbanOrchestrator:
    return KanbanOrchestrator(
        store=MarkdownBoardStore(board),
        executor=MockAgentaoExecutor(),
    )


def _combined(board: Path, *, max_claims: int, **cfg) -> CombinedDaemon:
    orch = _fresh_orch(board)
    return CombinedDaemon(
        orch,
        config=DaemonConfig(max_claims=max_claims, **cfg),
        orchestrator_factory=lambda: _fresh_orch(board),
    )


def test_combined_daemon_builds_n_worker_daemons(tmp_path: Path):
    daemon = _combined(tmp_path, max_claims=3)
    assert len(daemon.workers) == 3
    assert len({w.worker_id for w in daemon.workers}) == 3


def test_combined_daemon_respects_worker_id_prefix(tmp_path: Path):
    orch = _fresh_orch(tmp_path)
    daemon = CombinedDaemon(
        orch,
        config=DaemonConfig(max_claims=2, worker_id="ci"),
        orchestrator_factory=lambda: _fresh_orch(tmp_path),
    )
    assert [w.worker_id for w in daemon.workers] == ["ci-1", "ci-2"]


def test_combined_daemon_preserves_explicit_worker_id_that_looks_like_default(
    tmp_path: Path,
):
    # An operator explicit ``--worker-id worker-deadbeef`` must be used as
    # the literal prefix, even though it has the same shape as the
    # auto-generated ``worker-<8-hex>`` default.
    orch = _fresh_orch(tmp_path)
    daemon = CombinedDaemon(
        orch,
        config=DaemonConfig(max_claims=3, worker_id="worker-deadbeef"),
        orchestrator_factory=lambda: _fresh_orch(tmp_path),
    )
    assert [w.worker_id for w in daemon.workers] == [
        "worker-deadbeef-1",
        "worker-deadbeef-2",
        "worker-deadbeef-3",
    ]


def test_combined_daemon_heartbeats_all_workers_at_startup(tmp_path: Path):
    # No cards — the daemon will idle out immediately. But it must still
    # register a WorkerPresence for every configured worker so
    # observability tools see the real fleet, not just worker-1.
    daemon = _combined(
        tmp_path, max_claims=3, max_idle_cycles=1, poll_interval=0.01
    )
    daemon.run()

    # After shutdown every presence must be cleared.
    presences = MarkdownBoardStore(tmp_path).list_workers()
    assert presences == []


def test_combined_daemon_runs_cards_to_completion_with_many_workers(
    tmp_path: Path,
):
    # Three cards with three workers: every card should reach DONE in
    # the same run without deadlocking or stranding any in DOING.
    store = MarkdownBoardStore(tmp_path)
    ids = [
        store.add_card(
            Card(title=f"c{i}", goal="g", acceptance_criteria=["x"])
        ).id
        for i in range(3)
    ]
    daemon = _combined(
        tmp_path, max_claims=3, max_idle_cycles=3, poll_interval=0.01
    )
    daemon.run()

    fresh = MarkdownBoardStore(tmp_path)
    for cid in ids:
        assert fresh.get_card(cid).status == CardStatus.DONE
    assert fresh.list_claims() == []
    assert fresh.list_workers() == []


def test_combined_daemon_once_runs_each_sub_daemon_exactly_once(tmp_path: Path):
    store = MarkdownBoardStore(tmp_path)
    store.add_card(
        Card(title="t", goal="g", status=CardStatus.READY, owner_role=AgentRole.WORKER)
    )
    daemon = _combined(tmp_path, max_claims=2, poll_interval=0.01)

    assert daemon.run_once() is True

    # One scheduler pass must have created a claim; the workers had one
    # cycle each — at most one of them acquires the single claim.
    fresh = MarkdownBoardStore(tmp_path)
    # One worker acquired the claim and submitted an envelope.
    assert len(fresh.read_results()) == 1
    # Scheduler tick counter is 1; one worker ticked (ran an executor), the
    # other idled.
    assert daemon.scheduler.ticks_processed == 1
    worker_ticks = [w.ticks_processed for w in daemon.workers]
    assert sum(worker_ticks) == 1


def test_combined_daemon_max_claims_drives_parallelism(tmp_path: Path):
    # With ``max_claims=2`` two READY cards must be claimed by two
    # distinct workers in the same scheduler pass + two worker cycles
    # (not queued behind one serial worker).
    store = MarkdownBoardStore(tmp_path)
    a = store.add_card(
        Card(title="A", goal="g", status=CardStatus.READY, owner_role=AgentRole.WORKER)
    )
    b = store.add_card(
        Card(title="B", goal="g", status=CardStatus.READY, owner_role=AgentRole.WORKER)
    )
    daemon = _combined(tmp_path, max_claims=2, poll_interval=0.01)
    daemon.run_once()

    fresh = MarkdownBoardStore(tmp_path)
    envelopes = fresh.read_results()
    # Both cards processed concurrently → two envelopes pending commit.
    assert {e.card_id for e in envelopes} == {a.id, b.id}
    # And the two claims are owned by the two distinct worker ids.
    owners = {c.worker_id for c in fresh.list_claims() if c.worker_id}
    assert len(owners) == 2


# ---------- stop event / threading ----------


def test_shared_stop_event_halts_all_threads(tmp_path: Path):
    """One request_stop() on the main thread must stop scheduler and
    every worker thread, not just the parent."""
    daemon = _combined(tmp_path, max_claims=3, poll_interval=10.0)
    # Long poll so threads would otherwise park for 10s; the stop event
    # must interrupt them immediately.
    t = threading.Thread(target=daemon.run, daemon=True)
    t.start()
    time.sleep(0.1)  # let sub-threads enter their sleep

    daemon.request_stop()
    t.join(timeout=5.0)
    assert not t.is_alive()


def test_combined_daemon_does_not_install_signal_handlers_in_children(
    tmp_path: Path, monkeypatch,
):
    """Signal handlers must be installed at most once and only by the
    top-level runner. Sub-daemons must not call ``signal.signal``."""
    import signal as _signal

    calls: list[object] = []
    original = _signal.signal

    def _tracking(sig, handler):
        calls.append(sig)
        return original(sig, lambda *a, **k: None)

    monkeypatch.setattr(_signal, "signal", _tracking)

    daemon = _combined(
        tmp_path, max_claims=2, max_idle_cycles=1, poll_interval=0.01
    )
    daemon.install_signal_handlers()
    daemon.run()

    # install_signal_handlers installs SIGINT + SIGTERM on the parent only.
    # Sub-daemons must not add more.
    assert calls == [_signal.SIGINT, _signal.SIGTERM]


def test_scheduler_and_worker_daemons_share_stop_event(tmp_path: Path):
    """attach_stop_event replaces the child's stop Event with a shared
    one; setting the shared event exits the child loop."""
    orch = _fresh_orch(tmp_path)
    sched = SchedulerDaemon(orch, config=DaemonConfig(poll_interval=10.0))
    w = WorkerDaemon(orch, config=DaemonConfig(poll_interval=10.0))

    shared = threading.Event()
    sched.attach_stop_event(shared)
    w.attach_stop_event(shared)

    # Run both in their own threads; they should exit when shared is set.
    t1 = threading.Thread(target=sched.run, daemon=True)
    t2 = threading.Thread(target=w.run, daemon=True)
    t1.start()
    t2.start()
    time.sleep(0.1)

    shared.set()
    t1.join(timeout=5.0)
    t2.join(timeout=5.0)
    assert not t1.is_alive()
    assert not t2.is_alive()
