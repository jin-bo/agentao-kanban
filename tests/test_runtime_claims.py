from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from kanban.models import (
    AgentResult,
    AgentRole,
    CardStatus,
    ClaimConflictError,
    ClaimMismatchError,
    ExecutionClaim,
    ExecutionResultEnvelope,
    ResourceUsage,
    WorkerPresence,
    utc_now,
)
from kanban.store import InMemoryBoardStore
from kanban.store_markdown import MarkdownBoardStore


def _make_claim(
    *,
    card_id: str = "card-1",
    claim_id: str = "clm-1",
    role: AgentRole = AgentRole.WORKER,
    lease_seconds: int = 60,
    attempt: int = 1,
) -> ExecutionClaim:
    now = utc_now()
    return ExecutionClaim(
        card_id=card_id,
        claim_id=claim_id,
        role=role,
        status_at_claim=CardStatus.READY,
        attempt=attempt,
        claimed_at=now,
        heartbeat_at=now,
        lease_expires_at=now + timedelta(seconds=lease_seconds),
        timeout_s=1800,
    )


# ---------- MarkdownBoardStore: claims ----------


def test_claim_round_trip_on_disk(tmp_path: Path):
    store = MarkdownBoardStore(tmp_path)
    claim = _make_claim()
    store.create_claim(claim)

    # New store instance reads from disk, proving persistence.
    reopened = MarkdownBoardStore(tmp_path)
    got = reopened.get_claim("card-1")
    assert got is not None
    assert got.claim_id == "clm-1"
    assert got.role == AgentRole.WORKER
    assert got.status_at_claim == CardStatus.READY
    assert got.attempt == 1
    assert got.timeout_s == 1800
    assert got.worker_id is None  # scheduler creates unassigned claims


def test_claim_files_are_under_runtime_dir(tmp_path: Path):
    store = MarkdownBoardStore(tmp_path)
    store.create_claim(_make_claim())
    path = tmp_path / "runtime" / "claims" / "card-1.json"
    assert path.is_file()


def test_duplicate_create_raises_conflict(tmp_path: Path):
    store = MarkdownBoardStore(tmp_path)
    store.create_claim(_make_claim())
    with pytest.raises(ClaimConflictError):
        store.create_claim(_make_claim(claim_id="clm-2"))


def test_renew_claim_updates_heartbeat_and_lease(tmp_path: Path):
    store = MarkdownBoardStore(tmp_path)
    claim = _make_claim()
    store.create_claim(claim)

    new_heartbeat = claim.heartbeat_at + timedelta(seconds=15)
    new_lease = claim.lease_expires_at + timedelta(seconds=60)
    renewed = store.renew_claim(
        "card-1",
        claim_id="clm-1",
        heartbeat_at=new_heartbeat,
        lease_expires_at=new_lease,
        worker_id="worker-a",
    )
    assert renewed.heartbeat_at == new_heartbeat
    assert renewed.lease_expires_at == new_lease
    assert renewed.worker_id == "worker-a"

    # On-disk state reflects the renewal.
    reopened = MarkdownBoardStore(tmp_path)
    got = reopened.get_claim("card-1")
    assert got is not None and got.worker_id == "worker-a"
    assert got.lease_expires_at == new_lease


def test_renew_claim_mismatched_id_raises(tmp_path: Path):
    store = MarkdownBoardStore(tmp_path)
    store.create_claim(_make_claim())
    with pytest.raises(ClaimMismatchError):
        store.renew_claim(
            "card-1",
            claim_id="wrong",
            heartbeat_at=utc_now(),
            lease_expires_at=utc_now() + timedelta(seconds=60),
        )


def test_renew_missing_claim_raises_keyerror(tmp_path: Path):
    store = MarkdownBoardStore(tmp_path)
    with pytest.raises(KeyError):
        store.renew_claim(
            "nope",
            claim_id="x",
            heartbeat_at=utc_now(),
            lease_expires_at=utc_now(),
        )


def test_clear_claim_removes_file(tmp_path: Path):
    store = MarkdownBoardStore(tmp_path)
    store.create_claim(_make_claim())
    store.clear_claim("card-1", claim_id="clm-1")
    assert store.get_claim("card-1") is None
    assert not (tmp_path / "runtime" / "claims" / "card-1.json").exists()


def test_clear_claim_mismatched_id_raises(tmp_path: Path):
    store = MarkdownBoardStore(tmp_path)
    store.create_claim(_make_claim())
    with pytest.raises(ClaimMismatchError):
        store.clear_claim("card-1", claim_id="wrong")
    # Original still intact.
    assert store.get_claim("card-1") is not None


def test_clear_claim_without_id_is_unconditional(tmp_path: Path):
    store = MarkdownBoardStore(tmp_path)
    store.create_claim(_make_claim())
    store.clear_claim("card-1")
    assert store.get_claim("card-1") is None


def test_clear_missing_claim_is_noop(tmp_path: Path):
    store = MarkdownBoardStore(tmp_path)
    store.clear_claim("nope")  # should not raise


def test_list_stale_claims_filters_by_lease_expiry(tmp_path: Path):
    store = MarkdownBoardStore(tmp_path)
    live = _make_claim(card_id="live", claim_id="l", lease_seconds=3600)
    stale = _make_claim(card_id="stale", claim_id="s", lease_seconds=-30)
    store.create_claim(live)
    store.create_claim(stale)

    all_claims = store.list_claims()
    assert {c.card_id for c in all_claims} == {"live", "stale"}

    stale_list = store.list_stale_claims(now=utc_now())
    assert [c.card_id for c in stale_list] == ["stale"]


# ---------- MarkdownBoardStore: results ----------


def test_result_envelope_round_trip(tmp_path: Path):
    store = MarkdownBoardStore(tmp_path)
    now = utc_now()
    agent_result = AgentResult(
        role=AgentRole.WORKER,
        summary="did the thing",
        next_status=CardStatus.REVIEW,
        updates={"outputs": {"impl": "x"}},
        prompt_version="1.0",
        duration_ms=1234,
        attempt=2,
    )
    envelope = ExecutionResultEnvelope(
        card_id="card-1",
        claim_id="clm-1",
        role=AgentRole.WORKER,
        attempt=2,
        started_at=now,
        finished_at=now + timedelta(milliseconds=1234),
        duration_ms=1234,
        ok=True,
        agent_result=agent_result,
        worker_id="worker-a",
        resource_usage=ResourceUsage(pid=99, rss_bytes=1024, cpu_seconds=0.5),
    )
    store.write_result(envelope)

    reopened = MarkdownBoardStore(tmp_path)
    got = reopened.read_results(card_id="card-1")
    assert len(got) == 1
    r = got[0]
    assert r.claim_id == "clm-1"
    assert r.attempt == 2
    assert r.ok is True
    assert r.agent_result is not None
    assert r.agent_result.summary == "did the thing"
    assert r.agent_result.next_status == CardStatus.REVIEW
    assert r.resource_usage is not None and r.resource_usage.pid == 99


def test_multiple_results_per_card_coexist(tmp_path: Path):
    store = MarkdownBoardStore(tmp_path)
    now = utc_now()
    for attempt in (1, 2, 3):
        store.write_result(
            ExecutionResultEnvelope(
                card_id="card-1",
                claim_id=f"clm-{attempt}",
                role=AgentRole.WORKER,
                attempt=attempt,
                started_at=now,
                finished_at=now,
                duration_ms=1,
                ok=(attempt == 3),
                failure_reason=None if attempt == 3 else "retry",
            )
        )
    results = store.read_results(card_id="card-1")
    assert sorted(r.attempt for r in results) == [1, 2, 3]


def test_write_result_overwrites_same_attempt(tmp_path: Path):
    store = MarkdownBoardStore(tmp_path)
    now = utc_now()
    base = ExecutionResultEnvelope(
        card_id="c",
        claim_id="clm",
        role=AgentRole.WORKER,
        attempt=1,
        started_at=now,
        finished_at=now,
        duration_ms=1,
        ok=False,
        failure_reason="first",
    )
    store.write_result(base)
    from dataclasses import replace

    store.write_result(replace(base, ok=True, failure_reason=None))
    results = store.read_results(card_id="c")
    assert len(results) == 1 and results[0].ok is True


def test_delete_result_removes_file(tmp_path: Path):
    store = MarkdownBoardStore(tmp_path)
    now = utc_now()
    store.write_result(
        ExecutionResultEnvelope(
            card_id="c",
            claim_id="clm",
            role=AgentRole.WORKER,
            attempt=1,
            started_at=now,
            finished_at=now,
            duration_ms=1,
            ok=True,
        )
    )
    store.delete_result("c", 1)
    assert store.read_results(card_id="c") == []


# ---------- MarkdownBoardStore: worker presence ----------


def test_worker_presence_heartbeat_round_trip(tmp_path: Path):
    store = MarkdownBoardStore(tmp_path)
    now = utc_now()
    store.heartbeat_worker(
        WorkerPresence(
            worker_id="worker-a",
            pid=12345,
            started_at=now,
            heartbeat_at=now,
            host="localhost",
        )
    )
    later = now + timedelta(seconds=15)
    store.heartbeat_worker(
        WorkerPresence(
            worker_id="worker-a",
            pid=12345,
            started_at=now,
            heartbeat_at=later,
            host="localhost",
        )
    )
    workers = MarkdownBoardStore(tmp_path).list_workers()
    assert len(workers) == 1
    assert workers[0].heartbeat_at == later


def test_remove_worker_clears_file(tmp_path: Path):
    store = MarkdownBoardStore(tmp_path)
    now = utc_now()
    store.heartbeat_worker(
        WorkerPresence(worker_id="w1", pid=1, started_at=now, heartbeat_at=now)
    )
    store.remove_worker("w1")
    assert store.list_workers() == []


# ---------- InMemoryBoardStore parity ----------


def test_in_memory_store_conflict_matches_markdown():
    store = InMemoryBoardStore()
    store.create_claim(_make_claim())
    with pytest.raises(ClaimConflictError):
        store.create_claim(_make_claim(claim_id="other"))


def test_in_memory_renew_and_clear_parity():
    store = InMemoryBoardStore()
    claim = _make_claim()
    store.create_claim(claim)
    updated = store.renew_claim(
        "card-1",
        claim_id="clm-1",
        heartbeat_at=utc_now(),
        lease_expires_at=claim.lease_expires_at + timedelta(seconds=60),
        worker_id="w",
    )
    assert updated.worker_id == "w"

    with pytest.raises(ClaimMismatchError):
        store.clear_claim("card-1", claim_id="wrong")
    store.clear_claim("card-1", claim_id="clm-1")
    assert store.get_claim("card-1") is None


# ---------- atomic write ----------


def test_create_claim_leaves_no_tmp_file(tmp_path: Path):
    store = MarkdownBoardStore(tmp_path)
    store.create_claim(_make_claim())
    leftover = list((tmp_path / "runtime" / "claims").glob("*.tmp"))
    assert leftover == []
