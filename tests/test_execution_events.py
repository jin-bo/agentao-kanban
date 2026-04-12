from __future__ import annotations

import json
from pathlib import Path

from kanban import CardStatus, KanbanOrchestrator
from kanban.executors import MockAgentaoExecutor
from kanban.models import AgentResult, AgentRole, Card, CardStatus as CS
from kanban.store_markdown import MarkdownBoardStore


def _read_events(store: MarkdownBoardStore) -> list[dict]:
    lines = store.events_path.read_text(encoding="utf-8").strip().splitlines()
    return [json.loads(line) for line in lines if line]


def test_execution_event_records_role_and_timing(tmp_path: Path):
    store = MarkdownBoardStore(tmp_path / "board")
    orch = KanbanOrchestrator(store=store, executor=MockAgentaoExecutor())
    card = orch.create_card(title="t", goal="g")
    orch.run_until_idle(max_steps=10)

    events = _read_events(store)
    role_events = [e for e in events if e.get("role")]
    # At least one event per role
    roles = {e["role"] for e in role_events}
    assert {"planner", "worker", "reviewer", "verifier"} <= roles

    for e in role_events:
        assert e["card_id"] == card.id
        assert "prompt_version" in e
        assert "duration_ms" in e
        assert "attempt" in e


def test_raw_transcript_written_and_retained(tmp_path: Path):
    store = MarkdownBoardStore(tmp_path / "board", raw_retention=3)
    card = Card(title="t", goal="g")
    store.add_card(card)

    # Write 5 results for the same role; expect only the last 3 files to remain.
    for i in range(5):
        result = AgentResult(
            role=AgentRole.WORKER,
            summary=f"attempt {i}",
            next_status=CS.REVIEW,
            prompt_version="1",
            duration_ms=10 * i,
            raw_response=f"transcript {i}",
        )
        store.append_execution_event(card.id, result)

    raw_dir = store.raw_root / card.id
    files = sorted(raw_dir.glob("worker-*.md"))
    assert len(files) == 3
    assert files[-1].read_text(encoding="utf-8") == "transcript 4"


def test_raw_transcript_disabled_when_retention_zero(tmp_path: Path):
    store = MarkdownBoardStore(tmp_path / "board", raw_retention=0)
    card = Card(title="t", goal="g")
    store.add_card(card)

    result = AgentResult(
        role=AgentRole.WORKER,
        summary="x",
        next_status=CS.REVIEW,
        prompt_version="1",
        raw_response="should-not-be-written",
    )
    store.append_execution_event(card.id, result)

    assert not store.raw_root.exists() or not any(store.raw_root.rglob("*.md"))
    events = _read_events(store)
    role_events = [e for e in events if e.get("role") == "worker"]
    assert role_events and "raw_path" not in role_events[0]


def test_raw_path_is_relative_to_workspace(tmp_path: Path):
    store = MarkdownBoardStore(tmp_path / "board")
    card = Card(title="t", goal="g")
    store.add_card(card)

    result = AgentResult(
        role=AgentRole.PLANNER,
        summary="planned",
        next_status=CS.READY,
        prompt_version="1",
        raw_response="full transcript here",
    )
    store.append_execution_event(card.id, result)

    events = _read_events(store)
    role_events = [e for e in events if e.get("role") == "planner"]
    assert role_events
    raw_path = role_events[0]["raw_path"]
    # Path should be relative to workspace root, starting with "raw/".
    assert raw_path.startswith("raw/")


def test_append_event_legacy_still_works(tmp_path: Path):
    store = MarkdownBoardStore(tmp_path / "board")
    card = Card(title="t", goal="g")
    store.add_card(card)
    store.append_event(card.id, "manual note")

    events = _read_events(store)
    assert any(e.get("message") == "manual note" and "role" not in e for e in events)
