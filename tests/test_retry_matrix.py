from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from kanban import CardStatus, KanbanOrchestrator
from kanban.executors import MockAgentaoExecutor
from kanban.models import (
    AgentRole,
    Card,
    FailureCategory,
    RetryPolicy,
    utc_now,
)
from kanban.store_markdown import MarkdownBoardStore


def _make(board: Path, **kwargs) -> tuple[MarkdownBoardStore, KanbanOrchestrator]:
    s = MarkdownBoardStore(board)
    return s, KanbanOrchestrator(store=s, executor=MockAgentaoExecutor(), **kwargs)


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


def _submit_failure(orch, claim, category: FailureCategory, reason: str = "boom"):
    """Submit under the claim's owning worker_id so the commit path accepts
    the envelope (worker_id mismatch would be quarantined as an orphan)."""
    assert claim.worker_id is not None, (
        "test must acquire the claim before submitting a result"
    )
    orch.submit_result(
        claim,
        None,
        worker_id=claim.worker_id,
        started_at=utc_now(),
        ok=False,
        failure_reason=reason,
        failure_category=category,
    )


# ---------- infrastructure: 2 retries then BLOCK ----------


def test_infrastructure_failure_retries_twice_then_blocks(tmp_path: Path):
    store, orch = _make(tmp_path)
    card = _ready(store)
    # attempt 1
    claim1 = orch.select_and_claim(worker_id=None)
    assert claim1 is not None and claim1.attempt == 1
    store.try_acquire_claim(claim1.card_id, worker_id="w1")
    live1 = store.get_claim(card.id)
    _submit_failure(orch, live1, FailureCategory.INFRASTRUCTURE)
    orch.commit_pending_results()

    # retry → attempt 2, new claim with retry_of_claim_id linkage
    claim2 = store.get_claim(card.id)
    assert claim2 is not None
    assert claim2.attempt == 2
    assert claim2.retry_count == 1
    assert claim2.retry_of_claim_id == claim1.claim_id
    assert claim2.worker_id is None  # unassigned again

    store.try_acquire_claim(claim2.card_id, worker_id="w2")
    live2 = store.get_claim(card.id)
    _submit_failure(orch, live2, FailureCategory.INFRASTRUCTURE)
    orch.commit_pending_results()

    # retry → attempt 3
    claim3 = store.get_claim(card.id)
    assert claim3 is not None
    assert claim3.attempt == 3
    assert claim3.retry_count == 2

    store.try_acquire_claim(claim3.card_id, worker_id="w3")
    live3 = store.get_claim(card.id)
    _submit_failure(orch, live3, FailureCategory.INFRASTRUCTURE)
    orch.commit_pending_results()

    # budget exhausted (2 retries used) → BLOCKED
    assert store.get_claim(card.id) is None
    got = store.get_card(card.id)
    assert got.status == CardStatus.BLOCKED
    assert "infrastructure" in (got.blocked_reason or "")


# ---------- functional rejection: immediate BLOCK ----------


def test_functional_rejection_blocks_immediately(tmp_path: Path):
    store, orch = _make(tmp_path)
    card = _ready(store)
    claim = orch.select_and_claim(worker_id=None)
    assert claim is not None
    store.try_acquire_claim(claim.card_id, worker_id="w1")
    live = store.get_claim(card.id)
    _submit_failure(
        orch, live, FailureCategory.FUNCTIONAL, reason="reviewer rejected"
    )
    orch.commit_pending_results()

    assert store.get_claim(card.id) is None
    assert store.get_card(card.id).status == CardStatus.BLOCKED
    assert "functional" in (store.get_card(card.id).blocked_reason or "")


# ---------- malformed: immediate BLOCK ----------


def test_malformed_agent_response_blocks_immediately(tmp_path: Path):
    store, orch = _make(tmp_path)
    card = _ready(store)
    claim = orch.select_and_claim(worker_id=None)
    assert claim is not None
    store.try_acquire_claim(claim.card_id, worker_id="w1")
    live = store.get_claim(card.id)
    _submit_failure(orch, live, FailureCategory.MALFORMED, reason="bad json")
    orch.commit_pending_results()

    assert store.get_card(card.id).status == CardStatus.BLOCKED


# ---------- lease expiry: 1 retry then BLOCK ----------


def test_lease_expiry_retries_once_then_blocks(tmp_path: Path):
    store, orch = _make(tmp_path)
    card = _ready(store)
    claim = orch.select_and_claim(worker_id=None)
    assert claim is not None

    # Expire the lease → scheduler recovers and creates retry claim.
    store.clear_claim(card.id)
    store.create_claim(
        replace(
            claim,
            lease_expires_at=utc_now() - timedelta(seconds=30),
            worker_id="w-dead",
        )
    )
    orch.recover_stale_claims()
    retry = store.get_claim(card.id)
    assert retry is not None
    assert retry.attempt == 2
    assert retry.retry_count == 1
    assert retry.retry_of_claim_id == claim.claim_id

    # Expire the retry too → budget exhausted → BLOCKED.
    store.clear_claim(card.id)
    store.create_claim(
        replace(
            retry,
            lease_expires_at=utc_now() - timedelta(seconds=30),
            worker_id="w-dead-2",
        )
    )
    orch.recover_stale_claims()
    assert store.get_claim(card.id) is None
    assert store.get_card(card.id).status == CardStatus.BLOCKED


# ---------- custom retry policy ----------


def test_custom_retry_policy_zero_infra_blocks_first_failure(tmp_path: Path):
    store, orch = _make(
        tmp_path, retry_policy=RetryPolicy(infrastructure=0)
    )
    card = _ready(store)
    claim = orch.select_and_claim(worker_id=None)
    assert claim is not None
    store.try_acquire_claim(claim.card_id, worker_id="w1")
    live = store.get_claim(card.id)
    _submit_failure(orch, live, FailureCategory.INFRASTRUCTURE)
    orch.commit_pending_results()

    assert store.get_card(card.id).status == CardStatus.BLOCKED


# ---------- worker acquires with short lease (heartbeat renews) ----------


def test_worker_acquire_uses_short_lease_not_timeout(tmp_path: Path):
    """Acquire should set a short lease (= lease_seconds). The heartbeat
    thread in ``_heartbeat_claim`` pushes it forward; the role timeout is
    enforced by total elapsed, not the initial lease window."""
    from kanban.daemon import DaemonConfig, WorkerDaemon

    store, orch = _make(tmp_path)
    _ready(store)
    claim = orch.select_and_claim(worker_id=None)
    assert claim is not None
    worker_timeout = orch.lease_policy.timeout_for(AgentRole.WORKER)
    assert claim.timeout_s == worker_timeout  # stored for heartbeat logic

    worker = WorkerDaemon(orch, config=DaemonConfig(worker_id="wx"))
    acquired = worker._acquire_any_claim()
    assert acquired is not None
    lease_delta = (acquired.lease_expires_at - acquired.heartbeat_at).total_seconds()
    assert abs(lease_delta - orch.lease_policy.lease_seconds) < 5
    assert lease_delta < worker_timeout  # must be much shorter than timeout


# ---------- structured runtime events ----------


def test_retry_emits_linked_runtime_events(tmp_path: Path):
    store, orch = _make(tmp_path)
    card = _ready(store)
    claim1 = orch.select_and_claim(worker_id=None)
    assert claim1 is not None
    store.try_acquire_claim(claim1.card_id, worker_id="w1")
    live1 = store.get_claim(card.id)
    _submit_failure(orch, live1, FailureCategory.INFRASTRUCTURE, reason="500")
    orch.commit_pending_results()

    # Reopen so events are freshly decoded from disk (proves round-trip).
    fresh = MarkdownBoardStore(tmp_path)
    events = [e for e in fresh.events_for_card(card.id) if e.is_runtime]
    event_types = [e.event_type for e in events]
    assert "execution.failed" in event_types
    assert "execution.retried" in event_types
    # The retried event carries retry_of_claim_id pointing back to attempt 1.
    retried = next(e for e in events if e.event_type == "execution.retried")
    assert retried.retry_of_claim_id == claim1.claim_id
    assert retried.attempt == 2
    assert retried.failure_category == "infrastructure"
