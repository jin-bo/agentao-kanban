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


def test_gc_orphaned_runtime_removes_claims_and_results(tmp_path: Path):
    """Deleting a card file but leaving claims/results must be GC-able."""
    from kanban.models import Card

    store = MarkdownBoardStore(tmp_path)
    card = Card(title="Doomed", goal="x")
    store.add_card(card)
    claim = _make_claim(card_id=card.id, claim_id="clm-orphan")
    store.create_claim(claim)
    envelope = ExecutionResultEnvelope(
        card_id=card.id,
        claim_id="clm-orphan",
        worker_id="w1",
        role=AgentRole.WORKER,
        attempt=1,
        started_at=utc_now(),
        finished_at=utc_now(),
        duration_ms=10,
        ok=True,
    )
    store.write_result(envelope)

    # Simulate external card deletion.
    (tmp_path / "cards" / f"{card.id}.md").unlink()

    # Reopen store — load itself must not raise even with orphaned runtime.
    reopened = MarkdownBoardStore(tmp_path)
    # Explicit GC removes the orphans.
    removed = reopened.gc_orphaned_runtime()
    assert removed >= 2
    assert reopened.get_claim(card.id) is None
    assert list(reopened.read_results()) == []


def test_gc_preserves_runtime_for_unparseable_card(tmp_path: Path):
    """Unparseable card file must not trigger runtime GC — a TOML typo
    or merge-conflict marker would otherwise destroy in-flight state."""
    from kanban.models import Card

    store = MarkdownBoardStore(tmp_path)
    card = Card(title="Flaky", goal="x")
    store.add_card(card)
    claim = _make_claim(card_id=card.id, claim_id="clm-keep")
    store.create_claim(claim)
    envelope = ExecutionResultEnvelope(
        card_id=card.id,
        claim_id="clm-keep",
        worker_id="w1",
        role=AgentRole.WORKER,
        attempt=1,
        started_at=utc_now(),
        finished_at=utc_now(),
        duration_ms=10,
        ok=True,
    )
    store.write_result(envelope)

    # Corrupt the card file so _load() skips it as unparseable.
    card_path = tmp_path / "cards" / f"{card.id}.md"
    card_path.write_text("+++\nnot = valid = toml\n+++\n", encoding="utf-8")

    reopened = MarkdownBoardStore(tmp_path)
    assert card.id not in {c.id for c in reopened.list_cards()}
    assert reopened.unparseable_cards(), "card should be reported unparseable"

    removed = reopened.gc_orphaned_runtime()
    assert removed == 0
    assert any(reopened.claims_dir.glob("*.json")), "claim file must survive"
    assert any(reopened.results_dir.glob("*.json")), "result file must survive"


def test_commit_tolerates_deleted_card(tmp_path: Path):
    """commit_pending_results must not crash when card file vanished mid-run."""
    from kanban.models import Card
    from kanban.executors import MockAgentaoExecutor
    from kanban import KanbanOrchestrator

    store = MarkdownBoardStore(tmp_path)
    card = Card(title="Doomed", goal="x")
    store.add_card(card)
    claim = _make_claim(card_id=card.id, claim_id="clm-raceo")
    claim.worker_id = "w1"
    store.create_claim(claim)
    envelope = ExecutionResultEnvelope(
        card_id=card.id,
        claim_id="clm-raceo",
        worker_id="w1",
        role=AgentRole.WORKER,
        attempt=1,
        started_at=utc_now(),
        finished_at=utc_now(),
        duration_ms=10,
        ok=True,
    )
    store.write_result(envelope)
    (tmp_path / "cards" / f"{card.id}.md").unlink()
    store.refresh()

    orch = KanbanOrchestrator(store=store, executor=MockAgentaoExecutor())
    # Must not raise KeyError
    orch.commit_pending_results()
    orch.recover_stale_claims()


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


def test_write_result_is_write_once_per_claim(tmp_path: Path):
    """A second write for the same claim must fail (FileExistsError) so a
    forger with the right claim_id still can't replace a pending envelope."""
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

    with pytest.raises(FileExistsError):
        store.write_result(replace(base, ok=True, failure_reason=None))
    # Original pending envelope still intact.
    results = store.read_results(card_id="c")
    assert len(results) == 1 and results[0].ok is False


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
    store.delete_result("c", "clm")
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
