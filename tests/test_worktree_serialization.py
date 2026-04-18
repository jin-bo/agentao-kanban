"""Round-trip serialization tests for worktree fields."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from kanban import CardStatus, MarkdownBoardStore
from kanban.cli import _event_to_json, _format_event_line
from kanban.models import AgentRole, Card, CardEvent, ExecutionClaim
from kanban.store_markdown import _claim_from_json, _claim_to_json


def test_card_toml_round_trip_with_worktree(tmp_path: Path):
    store = MarkdownBoardStore(tmp_path)
    card = Card(
        title="Worktree Card",
        goal="Test worktree serialization",
        worktree_branch="kanban/abc123",
        worktree_base_commit="deadbeef" * 5,
    )
    store.add_card(card)

    reloaded = MarkdownBoardStore(tmp_path)
    got = reloaded.get_card(card.id)
    assert got.worktree_branch == "kanban/abc123"
    assert got.worktree_base_commit == "deadbeef" * 5


def test_card_toml_round_trip_without_worktree(tmp_path: Path):
    store = MarkdownBoardStore(tmp_path)
    card = Card(title="No WT", goal="Test")
    store.add_card(card)

    reloaded = MarkdownBoardStore(tmp_path)
    got = reloaded.get_card(card.id)
    assert got.worktree_branch is None
    assert got.worktree_base_commit is None


def test_claim_json_round_trip_with_worktree_path():
    now = datetime.now(timezone.utc)
    claim = ExecutionClaim(
        card_id="c1",
        claim_id="clm-abc",
        role=AgentRole.WORKER,
        status_at_claim=CardStatus.DOING,
        attempt=1,
        claimed_at=now,
        heartbeat_at=now,
        lease_expires_at=now,
        timeout_s=300,
        worktree_path="/workspace/worktrees/c1",
    )
    data = _claim_to_json(claim)
    assert data["worktree_path"] == "/workspace/worktrees/c1"

    restored = _claim_from_json(data)
    assert restored.worktree_path == "/workspace/worktrees/c1"


def test_claim_json_round_trip_without_worktree_path():
    now = datetime.now(timezone.utc)
    claim = ExecutionClaim(
        card_id="c2",
        claim_id="clm-def",
        role=AgentRole.REVIEWER,
        status_at_claim=CardStatus.REVIEW,
        attempt=1,
        claimed_at=now,
        heartbeat_at=now,
        lease_expires_at=now,
        timeout_s=300,
    )
    data = _claim_to_json(claim)
    assert "worktree_path" not in data

    restored = _claim_from_json(data)
    assert restored.worktree_path is None


def test_event_to_json_with_worktree_branch():
    event = CardEvent(
        card_id="c1",
        message="Worktree created",
        at=datetime.now(timezone.utc),
        event_type="worktree.created",
        worktree_branch="kanban/c1",
    )
    j = _event_to_json(event)
    assert j["worktree_branch"] == "kanban/c1"


def test_event_to_json_without_worktree_branch():
    event = CardEvent(
        card_id="c1",
        message="Normal event",
        at=datetime.now(timezone.utc),
    )
    j = _event_to_json(event)
    assert "worktree_branch" not in j


def test_format_event_line_with_worktree():
    event = CardEvent(
        card_id="c1234567890",
        message="Worktree detached",
        at=datetime(2026, 4, 15, 12, 0, 0, tzinfo=timezone.utc),
        event_type="worktree.detached",
        worktree_branch="kanban/c1234567890",
    )
    line = _format_event_line(event)
    assert "wt=kanban/c1234567890" in line
    assert "[worktree.detached]" in line


def test_runtime_event_with_worktree_branch(tmp_path: Path):
    store = MarkdownBoardStore(tmp_path)
    card = Card(title="WT Event", goal="Test")
    store.add_card(card)

    store.append_runtime_event(
        card.id,
        event_type="worktree.created",
        message="Worktree created: kanban/test",
        worktree_branch="kanban/test",
    )

    events = store.list_events()
    wt_events = [e for e in events if e.event_type == "worktree.created"]
    assert len(wt_events) == 1
    assert wt_events[0].worktree_branch == "kanban/test"
