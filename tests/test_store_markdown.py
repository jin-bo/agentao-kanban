from __future__ import annotations

from pathlib import Path

from kanban import CardPriority, CardStatus, KanbanOrchestrator, MarkdownBoardStore
from kanban.executors import MockAgentaoExecutor
from kanban.models import AgentRole, Card


def test_round_trip_preserves_all_fields(tmp_path: Path):
    store = MarkdownBoardStore(tmp_path)
    card = Card(
        title="Complex\nTitle",
        goal="Goal with\nmultiple lines and \"quotes\"",
        priority=CardPriority.CRITICAL,
        acceptance_criteria=["a", "b with \"quote\""],
        depends_on=["dep-1", "dep-2"],
        context_refs=["ref"],
    )
    store.add_card(card)
    store.update_card(
        card.id,
        outputs={"implementation": "done", "notes": "line1\nline2"},
        owner_role=AgentRole.REVIEWER,
    )
    store.move_card(card.id, CardStatus.REVIEW, "moved")

    reloaded = MarkdownBoardStore(tmp_path)
    got = reloaded.get_card(card.id)
    assert got.title == card.title
    assert got.goal == card.goal
    assert got.priority == CardPriority.CRITICAL
    assert got.status == CardStatus.REVIEW
    assert got.owner_role == AgentRole.REVIEWER
    assert got.acceptance_criteria == ["a", "b with \"quote\""]
    assert got.depends_on == ["dep-1", "dep-2"]
    assert got.outputs == {"implementation": "done", "notes": "line1\nline2"}


def test_full_pipeline_persists_across_reload(tmp_path: Path):
    store = MarkdownBoardStore(tmp_path)
    orch = KanbanOrchestrator(store=store, executor=MockAgentaoExecutor())
    card = orch.create_card(title="E2E", goal="run it", priority=CardPriority.HIGH)
    orch.run_until_idle(max_steps=20)
    assert store.get_card(card.id).status == CardStatus.DONE

    # Reload from disk and confirm state.
    reloaded = MarkdownBoardStore(tmp_path)
    got = reloaded.get_card(card.id)
    assert got.status == CardStatus.DONE
    assert "implementation" in got.outputs
    assert "review" in got.outputs
    assert "verification" in got.outputs
    # Events were persisted line-by-line.
    events = reloaded.events_for_card(card.id)
    assert any("Status changed to done" in e.message for e in events)


def test_event_with_newlines_roundtrips(tmp_path: Path):
    store = MarkdownBoardStore(tmp_path)
    orch = KanbanOrchestrator(store=store, executor=MockAgentaoExecutor())
    c = orch.create_card(title="N", goal="g")
    store.append_event(c.id, "multi\nline\tmessage with\nbreaks")

    reloaded = MarkdownBoardStore(tmp_path)
    events = reloaded.events_for_card(c.id)
    messages = [e.message for e in events]
    assert "multi\nline\tmessage with\nbreaks" in messages


def test_legacy_tsv_events_still_parse(tmp_path: Path):
    # Pre-seed an events.log in the old TSV format; ensure reload doesn't break.
    (tmp_path / "cards").mkdir()
    (tmp_path / "events.log").write_text(
        "2026-04-12T10:00:00+00:00\tcard-1\tlegacy message\n",
        encoding="utf-8",
    )
    store = MarkdownBoardStore(tmp_path)
    events = store.events_for_card("card-1")
    assert len(events) == 1
    assert events[0].message == "legacy message"


def test_blocked_reason_roundtrips(tmp_path: Path):
    store = MarkdownBoardStore(tmp_path)
    orch = KanbanOrchestrator(store=store, executor=MockAgentaoExecutor())
    c = orch.create_card(title="B", goal="b")
    orch.block(c.id, "needs input")

    reloaded = MarkdownBoardStore(tmp_path)
    got = reloaded.get_card(c.id)
    assert got.status == CardStatus.BLOCKED
    assert got.blocked_reason == "needs input"
