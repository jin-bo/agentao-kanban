"""Regression tests for the fourth round of Codex adversarial findings:

1. Workers used to write ``execution.finished`` / ``execution.failed``
   events directly from ``submit_result``, before the committer had
   verified claim ownership. A forged envelope (wrong worker_id) would
   be quarantined — but its success/failure event was already in the
   audit trail. The fix moves lifecycle emission into the committer,
   after ownership checks pass, and leaves ``execution.result_orphaned``
   as the authoritative rejection event.
2. If envelope persistence raised (FileExistsError, disk error),
   ``submit_result`` escaped ``run_once`` and crashed the worker. The
   fix wraps the call in a worker-side safe helper that logs and
   returns; the claim is left live for scheduler recovery.
"""
from __future__ import annotations

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


# ---------- Fix 1: event emission is committer-authoritative ----------


def test_submit_result_alone_emits_no_runtime_event(tmp_path: Path):
    """Worker writing an envelope must NOT leave a lifecycle event behind.
    Only the committer may emit execution.finished / execution.failed."""
    store, orch = _make(tmp_path)
    card = _ready(store)
    claim = orch.select_and_claim(worker_id=None)
    assert claim is not None
    store.try_acquire_claim(card.id, worker_id="w1")
    live = store.get_claim(card.id)

    orch.submit_result(
        live,
        None,
        worker_id=live.worker_id,
        started_at=utc_now(),
        ok=False,
        failure_reason="boom",
        failure_category=FailureCategory.INFRASTRUCTURE,
    )

    # Envelope exists, but no finished/failed event has been emitted yet.
    fresh = MarkdownBoardStore(tmp_path)
    events = [e for e in fresh.events_for_card(card.id) if e.is_runtime]
    etypes = [e.event_type for e in events]
    assert "execution.finished" not in etypes
    assert "execution.failed" not in etypes


def test_forged_envelope_produces_no_finished_event(tmp_path: Path):
    """Adversarial: an envelope forged under the wrong worker_id must be
    orphaned without leaving any execution.finished/failed trace. Only
    execution.result_orphaned should appear."""
    store, orch = _make(tmp_path)
    card = _ready(store)
    claim = orch.select_and_claim(worker_id=None)
    assert claim is not None
    store.try_acquire_claim(card.id, worker_id="legit")
    live = store.get_claim(card.id)

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
            summary="forged",
            next_status=CardStatus.DONE,
            updates={},
        ),
    )
    store.write_result(forged)
    orch.commit_pending_results()

    fresh = MarkdownBoardStore(tmp_path)
    runtime = [e for e in fresh.events_for_card(card.id) if e.is_runtime]
    etypes = [e.event_type for e in runtime]
    # Authoritative rejection was emitted.
    assert "execution.result_orphaned" in etypes
    # But NO finished/failed event — those are ownership-verified only.
    assert "execution.finished" not in etypes
    assert "execution.failed" not in etypes


def test_committer_emits_finished_on_successful_commit(tmp_path: Path):
    """Positive case: when ownership checks pass, commit_pending_results
    emits execution.finished with the full runtime record."""
    store, orch = _make(tmp_path)
    card = _ready(store)
    claim = orch.select_and_claim(worker_id=None)
    assert claim is not None
    store.try_acquire_claim(card.id, worker_id="legit")
    live = store.get_claim(card.id)
    result = orch.executor.run(live.role, store.get_card(card.id))
    orch.submit_result(live, result, worker_id="legit", started_at=utc_now())

    # Before commit: no finished event.
    pre = [e for e in store.events_for_card(card.id) if e.is_runtime]
    assert not any(e.event_type == "execution.finished" for e in pre)

    orch.commit_pending_results()

    post = [e for e in MarkdownBoardStore(tmp_path).events_for_card(card.id)
            if e.is_runtime]
    finished = [e for e in post if e.event_type == "execution.finished"]
    assert len(finished) == 1
    assert finished[0].worker_id == "legit"
    assert finished[0].claim_id == live.claim_id


def test_committer_emits_failed_on_unretryable_failure(tmp_path: Path):
    store, orch = _make(tmp_path)
    card = _ready(store)
    claim = orch.select_and_claim(worker_id=None)
    assert claim is not None
    store.try_acquire_claim(card.id, worker_id="w1")
    live = store.get_claim(card.id)
    orch.submit_result(
        live,
        None,
        worker_id="w1",
        started_at=utc_now(),
        ok=False,
        failure_reason="rejected",
        failure_category=FailureCategory.FUNCTIONAL,  # no retry → BLOCKED
    )
    orch.commit_pending_results()

    events = [e for e in MarkdownBoardStore(tmp_path).events_for_card(card.id)
              if e.is_runtime]
    failed = [e for e in events if e.event_type == "execution.failed"]
    assert len(failed) == 1
    assert failed[0].failure_category == "functional"
    assert failed[0].failure_reason == "rejected"


# ---------- Fix 2: worker survives submit_result failure ----------


def test_worker_survives_envelope_persistence_error(
    tmp_path: Path, monkeypatch, caplog
):
    """If ``write_result`` raises, the worker must NOT crash. The claim
    stays live (scheduler recovery handles it) and the worker can try the
    next tick."""
    import logging

    store, orch = _make(tmp_path)
    card = _ready(store)
    claim = orch.select_and_claim(worker_id=None)
    assert claim is not None

    # Inject a persistent write failure.
    def broken_write(envelope):
        raise FileExistsError("simulated duplicate submission")

    monkeypatch.setattr(store, "write_result", broken_write)

    worker = WorkerDaemon(
        orch, config=DaemonConfig(worker_id="w-flaky", max_idle_cycles=1)
    )

    caplog.set_level(logging.ERROR, logger="kanban.daemon")
    # Must NOT raise even though write_result always fails.
    did = worker.run_once()
    assert did is True  # the worker did acquire and attempted to submit

    # Claim still live; scheduler would recover on lease expiry.
    assert store.get_claim(card.id) is not None
    # Loud failure logged.
    assert any(
        "failed to persist result envelope" in rec.message for rec in caplog.records
    )


def test_worker_loop_continues_after_persistence_error(
    tmp_path: Path, monkeypatch
):
    """End-to-end: a second tick runs cleanly after a first tick failed
    to persist the envelope. This is the real operational concern — the
    worker process stays up."""
    store, orch = _make(tmp_path)
    _ready(store, "a")
    _ready(store, "b")
    claim = orch.select_and_claim(worker_id=None)
    assert claim is not None

    real_write = store.write_result
    calls = {"n": 0}

    def sometimes_broken(envelope):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated disk blip")
        return real_write(envelope)

    monkeypatch.setattr(store, "write_result", sometimes_broken)

    worker = WorkerDaemon(
        orch, config=DaemonConfig(worker_id="w1", max_idle_cycles=1)
    )
    # First tick hits the error but must not propagate.
    assert worker.run_once() is True
    # Second tick sees the next card and persists cleanly.
    # (Need to create another claim first, since scheduler isn't running.)
    orch.select_and_claim(worker_id=None)
    assert worker.run_once() is True
    # Second attempt wrote a real envelope.
    assert len(store.read_results()) >= 1
