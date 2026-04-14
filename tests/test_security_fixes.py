"""Regression tests for the two high-severity findings in the Codex review.

1. Legacy serial ``tick()`` must never publish an unassigned claim —
   otherwise a parallel ``WorkerDaemon`` could steal the claim between
   select and execute and the card would run twice.
2. ``commit_pending_results`` must reject envelopes whose ``worker_id``
   does not match the live claim's owner, and the result store must be
   write-once per claim — otherwise a forging worker that knows the
   claim_id can replace a pending envelope.
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from kanban import CardStatus, KanbanOrchestrator
from kanban.daemon import DaemonConfig, WorkerDaemon
from kanban.executors import MockAgentaoExecutor
from kanban.models import (
    AgentResult,
    AgentRole,
    Card,
    ExecutionResultEnvelope,
    FailureCategory,
    utc_now,
)
from kanban.store_markdown import MarkdownBoardStore


def _make(board: Path) -> tuple[MarkdownBoardStore, KanbanOrchestrator]:
    s = MarkdownBoardStore(board)
    return s, KanbanOrchestrator(store=s, executor=MockAgentaoExecutor())


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


# ---------- Fix 1: legacy tick owns its claim ----------


def test_legacy_tick_publishes_claim_with_owner(tmp_path: Path):
    """tick() must NOT leave the claim unassigned during execution — a
    concurrent WorkerDaemon scanning list_claims() would otherwise steal it."""
    store, orch = _make(tmp_path)
    _ready(store)

    # Intercept executor.run() so we can inspect live runtime state mid-tick.
    seen_worker_ids: list[str | None] = []
    original_run = orch.executor.run

    def spy(role, card):
        live = store.get_claim(card.id)
        seen_worker_ids.append(live.worker_id if live else None)
        return original_run(role, card)

    orch.executor.run = spy  # type: ignore[method-assign]

    orch.tick()
    assert seen_worker_ids, "executor was never invoked"
    assert seen_worker_ids[0] is not None, (
        "legacy tick left the claim unassigned; a parallel worker daemon "
        "could have stolen it"
    )
    assert seen_worker_ids[0] == KanbanOrchestrator.LEGACY_SERIAL_WORKER_ID


def test_worker_daemon_cannot_steal_legacy_tick_claim(tmp_path: Path):
    """End-to-end: with a legacy tick claim live, a WorkerDaemon running
    against the same board sees no acquirable claim."""
    store, orch = _make(tmp_path)
    card = _ready(store)

    # Create a legacy-owned claim but do NOT run the executor yet.
    claim = orch.select_and_claim(
        worker_id=KanbanOrchestrator.LEGACY_SERIAL_WORKER_ID
    )
    assert claim is not None and claim.worker_id == "local-serial"

    # A second process's WorkerDaemon should refuse to acquire it.
    worker_store = MarkdownBoardStore(tmp_path)
    worker_orch = KanbanOrchestrator(
        store=worker_store, executor=MockAgentaoExecutor()
    )
    worker = WorkerDaemon(
        worker_orch, config=DaemonConfig(worker_id="intruder", max_idle_cycles=1)
    )
    assert worker._acquire_any_claim() is None
    # Card status, claim ownership unchanged.
    assert store.get_claim(card.id).worker_id == "local-serial"  # type: ignore[union-attr]


# ---------- Fix 2a: result store is write-once per claim ----------


def test_write_result_refuses_second_envelope_for_same_claim(tmp_path: Path):
    store = MarkdownBoardStore(tmp_path)
    now = utc_now()
    envelope = ExecutionResultEnvelope(
        card_id="card-1",
        claim_id="clm-1",
        role=AgentRole.WORKER,
        attempt=1,
        started_at=now,
        finished_at=now,
        duration_ms=1,
        ok=True,
        worker_id="w-real",
    )
    store.write_result(envelope)

    # A forger that knows the claim_id tries to submit a contradicting result.
    from dataclasses import replace

    forged = replace(envelope, worker_id="w-forger", ok=False, failure_reason="X")
    with pytest.raises(FileExistsError):
        store.write_result(forged)

    # Only the original envelope persists.
    results = store.read_results()
    assert len(results) == 1 and results[0].worker_id == "w-real"


def test_retry_attempts_for_same_card_get_distinct_envelope_files(tmp_path: Path):
    """Write-once is keyed by claim_id, so different attempts (different
    claims) for the same card still coexist."""
    store, orch = _make(tmp_path)
    card = _ready(store)
    claim1 = orch.select_and_claim(worker_id=None)
    assert claim1 is not None
    store.try_acquire_claim(claim1.card_id, worker_id="w1")
    live1 = store.get_claim(card.id)
    orch.submit_result(
        live1,
        None,
        worker_id=live1.worker_id,
        started_at=utc_now(),
        ok=False,
        failure_reason="boom",
        failure_category=FailureCategory.INFRASTRUCTURE,
    )
    orch.commit_pending_results()  # produces retry claim

    claim2 = store.get_claim(card.id)
    assert claim2 is not None and claim2.attempt == 2
    store.try_acquire_claim(claim2.card_id, worker_id="w2")
    live2 = store.get_claim(card.id)
    # Second envelope for the same card but different claim is allowed.
    orch.submit_result(
        live2,
        None,
        worker_id=live2.worker_id,
        started_at=utc_now(),
        ok=False,
        failure_reason="boom2",
        failure_category=FailureCategory.INFRASTRUCTURE,
    )
    assert len(store.read_results(card_id=card.id)) == 1  # only the new one pending


# ---------- Fix 2b: committer verifies worker_id ----------


def test_forged_worker_id_envelope_is_quarantined(tmp_path: Path):
    """An envelope whose worker_id does not match the live claim owner is
    quarantined as an orphan and the card state is NOT mutated."""
    store, orch = _make(tmp_path)
    card = _ready(store)
    claim = orch.select_and_claim(worker_id=None)
    assert claim is not None
    # Legit worker acquires.
    store.try_acquire_claim(claim.card_id, worker_id="legit")
    live = store.get_claim(card.id)
    assert live is not None and live.worker_id == "legit"

    # A second process directly writes an envelope under a different
    # worker_id (bypassing try_acquire_claim). This is the forging scenario
    # Codex flagged.
    forged = ExecutionResultEnvelope(
        card_id=card.id,
        claim_id=live.claim_id,
        role=live.role,
        attempt=live.attempt,
        started_at=utc_now(),
        finished_at=utc_now(),
        duration_ms=1,
        ok=True,
        worker_id="forger",
        agent_result=AgentResult(
            role=AgentRole.WORKER,
            summary="forged win",
            next_status=CardStatus.DONE,  # would advance board if applied
            updates={},
        ),
    )
    store.write_result(forged)

    processed = orch.commit_pending_results()
    assert processed == 1
    # Forged envelope quarantined, card status untouched (still DOING).
    assert store.read_results() == []
    orphans = store.list_orphan_results()
    assert len(orphans) == 1 and orphans[0].worker_id == "forger"
    assert store.get_card(card.id).status == CardStatus.DOING
    # Live claim still belongs to the legit worker.
    still = store.get_claim(card.id)
    assert still is not None and still.worker_id == "legit"


def test_envelope_on_unowned_claim_is_quarantined(tmp_path: Path):
    """A claim that was never acquired (worker_id=None) should never accept
    an envelope — only an acquiring worker can produce a committable result."""
    store, orch = _make(tmp_path)
    card = _ready(store)
    claim = orch.select_and_claim(worker_id=None)
    assert claim is not None and claim.worker_id is None

    rogue = ExecutionResultEnvelope(
        card_id=card.id,
        claim_id=claim.claim_id,
        role=claim.role,
        attempt=claim.attempt,
        started_at=utc_now(),
        finished_at=utc_now(),
        duration_ms=1,
        ok=True,
        worker_id="rogue",
    )
    store.write_result(rogue)

    orch.commit_pending_results()
    assert store.read_results() == []
    assert len(store.list_orphan_results()) == 1
    assert store.get_card(card.id).status == CardStatus.DOING


def test_orphan_event_records_reason(tmp_path: Path):
    store, orch = _make(tmp_path)
    card = _ready(store)
    claim = orch.select_and_claim(worker_id=None)
    assert claim is not None
    store.try_acquire_claim(claim.card_id, worker_id="legit")
    live = store.get_claim(card.id)

    store.write_result(
        ExecutionResultEnvelope(
            card_id=card.id,
            claim_id=live.claim_id,
            role=live.role,
            attempt=live.attempt,
            started_at=utc_now(),
            finished_at=utc_now(),
            duration_ms=1,
            ok=True,
            worker_id="forger",
        )
    )
    orch.commit_pending_results()

    fresh = MarkdownBoardStore(tmp_path)
    events = [
        e
        for e in fresh.events_for_card(card.id)
        if e.event_type == "execution.result_orphaned"
    ]
    assert events, "result_orphaned event not emitted"
    assert "worker_id mismatch" in events[0].failure_reason  # type: ignore[operator]
