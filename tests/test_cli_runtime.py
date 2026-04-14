from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from kanban.cli import main
from kanban.executors import MockAgentaoExecutor
from kanban.models import (
    AgentRole,
    Card,
    CardStatus,
    ExecutionClaim,
    FailureCategory,
    WorkerPresence,
    utc_now,
)
from kanban.orchestrator import KanbanOrchestrator
from kanban.store_markdown import MarkdownBoardStore


def _ready_card(board: Path, title: str = "t") -> tuple[MarkdownBoardStore, Card]:
    store = MarkdownBoardStore(board)
    card = store.add_card(
        Card(
            title=title,
            goal="g",
            status=CardStatus.READY,
            owner_role=AgentRole.WORKER,
            acceptance_criteria=["x"],
        )
    )
    return store, card


# ---------- kanban claims ----------


def test_claims_empty_board_prints_placeholder(tmp_path: Path, capsys):
    board = tmp_path / "b"
    board.mkdir()
    rc = main(["--board", str(board), "claims"])
    assert rc == 0
    assert "(no active claims)" in capsys.readouterr().out


def test_claims_lists_live_claims(tmp_path: Path, capsys):
    board = tmp_path / "b"
    store, card = _ready_card(board)
    orch = KanbanOrchestrator(store=store, executor=MockAgentaoExecutor())
    claim = orch.select_and_claim(worker_id=None)
    assert claim is not None

    rc = main(["--board", str(board), "claims"])
    assert rc == 0
    out = capsys.readouterr().out
    assert card.id[:8] in out
    assert claim.claim_id in out
    assert "worker" in out  # role
    assert "attempt" in out  # header


def test_claims_json_has_structured_fields(tmp_path: Path, capsys):
    board = tmp_path / "b"
    store, card = _ready_card(board)
    orch = KanbanOrchestrator(store=store, executor=MockAgentaoExecutor())
    orch.select_and_claim(worker_id=None)

    rc = main(["--board", str(board), "claims", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data) == 1
    rec = data[0]
    assert rec["card_id"] == card.id
    assert rec["role"] == "worker"
    assert "lease_remaining_s" in rec
    assert "heartbeat_age_s" in rec
    assert rec["expired"] is False


def test_claims_single_card_filter(tmp_path: Path, capsys):
    board = tmp_path / "b"
    store, a = _ready_card(board, "A")
    b = store.add_card(
        Card(title="B", goal="g", status=CardStatus.READY, owner_role=AgentRole.WORKER)
    )
    orch = KanbanOrchestrator(store=store, executor=MockAgentaoExecutor())
    orch.select_and_claim(worker_id=None)
    orch.select_and_claim(worker_id=None)

    rc = main(["--board", str(board), "claims", a.id])
    assert rc == 0
    out = capsys.readouterr().out
    assert a.id[:8] in out
    assert b.id[:8] not in out


def test_claims_expired_tag_shown(tmp_path: Path, capsys):
    board = tmp_path / "b"
    store, card = _ready_card(board)
    orch = KanbanOrchestrator(store=store, executor=MockAgentaoExecutor())
    claim = orch.select_and_claim(worker_id=None)
    assert claim is not None
    # Rewrite the claim with an expired lease.
    store.clear_claim(card.id)
    store.create_claim(
        replace(claim, lease_expires_at=utc_now() - timedelta(seconds=30))
    )

    rc = main(["--board", str(board), "claims"])
    assert rc == 0
    assert "*EXPIRED*" in capsys.readouterr().out


# ---------- kanban workers ----------


def test_workers_empty_placeholder(tmp_path: Path, capsys):
    board = tmp_path / "b"
    board.mkdir()
    rc = main(["--board", str(board), "workers"])
    assert rc == 0
    assert "(no live workers)" in capsys.readouterr().out


def test_workers_lists_presence(tmp_path: Path, capsys):
    board = tmp_path / "b"
    store = MarkdownBoardStore(board)
    now = utc_now()
    store.heartbeat_worker(
        WorkerPresence(
            worker_id="worker-cli",
            pid=12345,
            started_at=now - timedelta(seconds=90),
            heartbeat_at=now - timedelta(seconds=5),
            host="localhost",
        )
    )
    rc = main(["--board", str(board), "workers"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "worker-cli" in out
    assert "12345" in out


def test_workers_json(tmp_path: Path, capsys):
    board = tmp_path / "b"
    store = MarkdownBoardStore(board)
    now = utc_now()
    store.heartbeat_worker(
        WorkerPresence(
            worker_id="w1", pid=100, started_at=now, heartbeat_at=now
        )
    )
    rc = main(["--board", str(board), "workers", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data) == 1
    assert data[0]["worker_id"] == "w1"
    assert data[0]["pid"] == 100
    assert "heartbeat_age_s" in data[0]


# ---------- kanban recover --stale ----------


def test_recover_requires_stale_flag(tmp_path: Path, capsys):
    board = tmp_path / "b"
    board.mkdir()
    rc = main(["--board", str(board), "recover"])
    assert rc == 2
    assert "requires --stale" in capsys.readouterr().err


def test_recover_stale_retries_first_time(tmp_path: Path, capsys):
    board = tmp_path / "b"
    store, card = _ready_card(board)
    orch = KanbanOrchestrator(store=store, executor=MockAgentaoExecutor())
    claim = orch.select_and_claim(worker_id=None)
    assert claim is not None
    # Force expiry.
    store.clear_claim(card.id)
    store.create_claim(
        replace(claim, lease_expires_at=utc_now() - timedelta(seconds=30))
    )

    rc = main(["--board", str(board), "recover", "--stale"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "retried" in out
    assert "recovered 1" in out
    # A linked retry claim now exists (attempt 2).
    fresh_claim = MarkdownBoardStore(board).get_claim(card.id)
    assert fresh_claim is not None and fresh_claim.attempt == 2


def test_recover_stale_blocks_when_budget_exhausted(tmp_path: Path, capsys):
    board = tmp_path / "b"
    store, card = _ready_card(board)
    orch = KanbanOrchestrator(store=store, executor=MockAgentaoExecutor())
    claim = orch.select_and_claim(worker_id=None)
    assert claim is not None
    # Pre-exhaust lease_expiry retry budget (=1).
    store.clear_claim(card.id)
    store.create_claim(
        replace(
            claim,
            lease_expires_at=utc_now() - timedelta(seconds=30),
            retry_count=1,
            attempt=2,
        )
    )

    rc = main(["--board", str(board), "recover", "--stale"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "blocked" in out
    fresh = MarkdownBoardStore(board).get_card(card.id)
    assert fresh.status == CardStatus.BLOCKED


def test_recover_stale_json(tmp_path: Path, capsys):
    board = tmp_path / "b"
    store, card = _ready_card(board)
    orch = KanbanOrchestrator(store=store, executor=MockAgentaoExecutor())
    claim = orch.select_and_claim(worker_id=None)
    assert claim is not None
    store.clear_claim(card.id)
    store.create_claim(
        replace(claim, lease_expires_at=utc_now() - timedelta(seconds=30))
    )

    rc = main(["--board", str(board), "recover", "--stale", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["recovered"] == 1
    assert len(data["cards"]) == 1
    assert data["cards"][0]["card_id"] == card.id


# ---------- kanban events surfaces runtime fields ----------


def test_events_text_shows_runtime_tag_and_extras(tmp_path: Path, capsys):
    board = tmp_path / "b"
    store, card = _ready_card(board)
    orch = KanbanOrchestrator(store=store, executor=MockAgentaoExecutor())
    claim = orch.select_and_claim(worker_id=None)
    assert claim is not None
    store.try_acquire_claim(claim.card_id, worker_id="w-cli")
    live = store.get_claim(card.id)
    assert live is not None
    orch.submit_result(
        live,
        None,
        worker_id="w-cli",
        started_at=utc_now(),
        ok=False,
        failure_reason="boom",
        failure_category=FailureCategory.INFRASTRUCTURE,
    )
    orch.commit_pending_results()

    rc = main(["--board", str(board), "events", card.id, "--limit", "20"])
    assert rc == 0
    out = capsys.readouterr().out
    # Runtime tags
    assert "[execution.failed]" in out
    assert "[execution.retried]" in out
    # Extras on the line
    assert "worker=w-cli" in out
    assert "attempt=" in out


def test_events_json_includes_runtime_fields(tmp_path: Path, capsys):
    board = tmp_path / "b"
    store, card = _ready_card(board)
    orch = KanbanOrchestrator(store=store, executor=MockAgentaoExecutor())
    claim = orch.select_and_claim(worker_id=None)
    assert claim is not None
    store.try_acquire_claim(claim.card_id, worker_id="w1")
    live = store.get_claim(card.id)
    orch.submit_result(
        live,
        None,
        worker_id="w1",
        started_at=utc_now(),
        ok=False,
        failure_reason="boom",
        failure_category=FailureCategory.INFRASTRUCTURE,
    )
    orch.commit_pending_results()

    rc = main(
        ["--board", str(board), "events", card.id, "--limit", "20", "--json"]
    )
    assert rc == 0
    lines = [
        json.loads(l) for l in capsys.readouterr().out.strip().splitlines() if l
    ]
    failed = [e for e in lines if e.get("event_type") == "execution.failed"]
    retried = [e for e in lines if e.get("event_type") == "execution.retried"]
    assert failed and failed[0]["failure_category"] == "infrastructure"
    assert retried and retried[0]["retry_of_claim_id"] == claim.claim_id
