from __future__ import annotations

from pathlib import Path

from kanban import CardPriority, CardStatus, KanbanOrchestrator, MarkdownBoardStore
from kanban.executors import MockAgentaoExecutor
from kanban.models import AgentRole, Card, ContextRef


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


def test_structured_context_refs_roundtrip(tmp_path: Path):
    store = MarkdownBoardStore(tmp_path)
    card = Card(
        title="ctx",
        goal="g",
        context_refs=[
            ContextRef(path="workspace/data/raw.jsonl", kind="required", note="源数据"),
            ContextRef(path="docs/api.md", kind="optional", note=""),
        ],
    )
    store.add_card(card)

    reloaded = MarkdownBoardStore(tmp_path)
    got = reloaded.get_card(card.id)
    assert got.context_refs == [
        ContextRef(path="workspace/data/raw.jsonl", kind="required", note="源数据"),
        ContextRef(path="docs/api.md", kind="optional", note=""),
    ]


def test_legacy_string_context_refs_upgrade(tmp_path: Path):
    # Simulate an old card file with flat string refs.
    (tmp_path / "cards").mkdir()
    (tmp_path / "cards" / "legacy.md").write_text(
        '+++\n'
        'id = "legacy"\n'
        'title = "L"\n'
        'status = "inbox"\n'
        'priority = 2\n'
        'goal = "g"\n'
        'acceptance_criteria = []\n'
        'context_refs = ["docs/a.md", "docs/b.md"]\n'
        'depends_on = []\n'
        'history = []\n'
        'created_at = 2026-01-01T00:00:00+00:00\n'
        'updated_at = 2026-01-01T00:00:00+00:00\n'
        '+++\n\n# L\n',
        encoding="utf-8",
    )
    store = MarkdownBoardStore(tmp_path)
    card = store.get_card("legacy")
    assert card.context_refs == [
        ContextRef(path="docs/a.md", kind="optional", note=""),
        ContextRef(path="docs/b.md", kind="optional", note=""),
    ]


def test_history_entries_carry_role_prefix(tmp_path: Path):
    store = MarkdownBoardStore(tmp_path)
    orch = KanbanOrchestrator(store=store, executor=MockAgentaoExecutor())
    card = orch.create_card(title="H", goal="g")
    orch.run_until_idle(max_steps=20)

    reloaded = MarkdownBoardStore(tmp_path)
    got = reloaded.get_card(card.id)
    assert got.history, "expected some history"
    # Every line must be prefixed with a role or system tag.
    for entry in got.history:
        assert entry.startswith("["), entry
        assert "]" in entry, entry


def test_inline_table_with_multiline_string_is_single_line(tmp_path: Path):
    # Regression for codex review finding P1: agents return structured
    # outputs whose string fields may contain newlines. TOML 1.0 requires
    # inline tables to be single-line, so multiline values inside them
    # must be escaped ("\\n"), not triple-quoted. CPython's tomllib is
    # lenient, but stricter parsers (tomli_w, other languages) are not.
    store = MarkdownBoardStore(tmp_path)
    card = Card(title="m", goal="g")
    store.add_card(card)
    store.update_card(
        card.id,
        outputs={
            "review": {
                "status": "approved",
                "notes": "line1\nline2\nline3",
            }
        },
    )

    text = (tmp_path / "cards" / f"{card.id}.md").read_text(encoding="utf-8")
    inline_line = next(line for line in text.splitlines() if line.startswith("review ="))
    # The whole inline table must fit on one physical line.
    assert "\n" not in inline_line
    assert '"""' not in inline_line
    assert "\\n" in inline_line  # newline was escaped, not preserved

    reloaded = MarkdownBoardStore(tmp_path)
    got = reloaded.get_card(card.id)
    assert got.outputs["review"]["notes"] == "line1\nline2\nline3"
    assert got.outputs["review"]["status"] == "approved"


def test_update_card_coerces_legacy_context_refs(tmp_path: Path):
    store = MarkdownBoardStore(tmp_path)
    card = Card(title="u", goal="g")
    store.add_card(card)
    store.update_card(
        card.id,
        context_refs=["a.md", {"path": "b.md", "kind": "required", "note": "n"}],
    )

    got = store.get_card(card.id)
    assert all(isinstance(r, ContextRef) for r in got.context_refs)
    assert got.context_refs == [
        ContextRef(path="a.md", kind="optional", note=""),
        ContextRef(path="b.md", kind="required", note="n"),
    ]

    # Also survives reload from disk.
    reloaded = MarkdownBoardStore(tmp_path).get_card(card.id)
    assert reloaded.context_refs == got.context_refs


def test_dict_keys_with_dots_and_unicode_roundtrip(tmp_path: Path):
    # Regression: real agents returned dict-shaped outputs keyed by file
    # names containing dots and CJK characters (e.g. "test韩1_报告.xlsx").
    # These must be quoted, not emitted as bare keys.
    store = MarkdownBoardStore(tmp_path)
    card = Card(title="k", goal="g")
    store.add_card(card)
    payload = {
        "test韩1_检验报告.xlsx": "/path/to/xlsx",
        "SHA256SUM.txt": "/path/to/sum",
        "普通 key with space": "v",
        "status": "ready_for_review",
    }
    store.update_card(card.id, outputs={"implementation": payload})

    reloaded = MarkdownBoardStore(tmp_path).get_card(card.id)
    assert reloaded.outputs["implementation"] == payload


def test_unparseable_card_does_not_kill_load(tmp_path: Path, caplog):
    (tmp_path / "cards").mkdir()
    (tmp_path / "cards" / "bad.md").write_text(
        "+++\nthis is not valid toml {{{\n+++\n",
        encoding="utf-8",
    )
    good = Card(title="good", goal="g")
    MarkdownBoardStore(tmp_path).add_card(good)

    # Re-open and confirm the good card still loads while bad one is skipped.
    import logging

    with caplog.at_level(logging.WARNING):
        reloaded = MarkdownBoardStore(tmp_path)
    assert reloaded.get_card(good.id).title == "good"
    assert any("bad.md" in rec.message for rec in caplog.records)


def test_malformed_context_refs_are_dropped_on_load(tmp_path: Path, caplog):
    # A card written with a mixed-quality context_refs list must still load:
    # bad entries are dropped + warned, good ones survive.
    (tmp_path / "cards").mkdir()
    (tmp_path / "cards" / "mixed.md").write_text(
        '+++\n'
        'id = "mixed"\n'
        'title = "T"\n'
        'status = "inbox"\n'
        'priority = 2\n'
        'goal = "g"\n'
        'acceptance_criteria = []\n'
        # legal dict, legal string, malformed (missing path), malformed (int)
        'context_refs = ['
        '{ path = "docs/good.md", kind = "required", note = "" }, '
        '"docs/legacy.md", '
        '{ kind = "required", note = "no path" }, '
        '42'
        ']\n'
        'depends_on = []\n'
        'history = []\n'
        'created_at = 2026-01-01T00:00:00+00:00\n'
        'updated_at = 2026-01-01T00:00:00+00:00\n'
        '+++\n\n',
        encoding="utf-8",
    )

    import logging

    with caplog.at_level(logging.WARNING):
        store = MarkdownBoardStore(tmp_path)
    card = store.get_card("mixed")
    assert [r.path for r in card.context_refs] == ["docs/good.md", "docs/legacy.md"]
    warn_messages = [rec.message for rec in caplog.records if "malformed context_ref" in rec.message]
    assert len(warn_messages) == 2


def test_context_ref_coerce_rejects_bad_shapes():
    import pytest as _pt
    from kanban.models import ContextRef as _CR

    with _pt.raises((KeyError, TypeError, ValueError)):
        _CR.coerce({"kind": "required"})  # missing path
    with _pt.raises((KeyError, TypeError, ValueError)):
        _CR.coerce(42)  # wrong type
    with _pt.raises((KeyError, TypeError, ValueError)):
        _CR.coerce("")  # empty path
    assert _CR.try_coerce({"kind": "x"}) is None
    assert _CR.try_coerce(42) is None


def test_list_events_mixed_shapes_and_limit(tmp_path: Path):
    store = MarkdownBoardStore(tmp_path)
    orch = KanbanOrchestrator(store=store, executor=MockAgentaoExecutor())
    c = orch.create_card(title="E", goal="g")
    orch.run_until_idle(max_steps=20)

    reloaded = MarkdownBoardStore(tmp_path)
    all_events = reloaded.list_events()
    assert len(all_events) > 5
    # Mixed shapes: some have role (execution events), some don't (plain events).
    assert any(e.is_execution for e in all_events)
    assert any(not e.is_execution for e in all_events)

    # limit returns the tail.
    last_three = reloaded.list_events(limit=3)
    assert len(last_three) == 3
    assert last_three == all_events[-3:]


def test_list_execution_events_filters_by_card_and_role(tmp_path: Path):
    store = MarkdownBoardStore(tmp_path)
    orch = KanbanOrchestrator(store=store, executor=MockAgentaoExecutor())
    c1 = orch.create_card(title="A", goal="g")
    c2 = orch.create_card(title="B", goal="g")
    orch.run_until_idle(max_steps=40)

    reloaded = MarkdownBoardStore(tmp_path)
    exec_events = reloaded.list_execution_events()
    assert all(e.is_execution for e in exec_events)

    only_c1 = reloaded.list_execution_events(card_id=c1.id)
    assert {e.card_id for e in only_c1} == {c1.id}

    only_worker = reloaded.list_execution_events(role=AgentRole.WORKER)
    assert only_worker, "should have worker events"
    assert all(e.role == AgentRole.WORKER for e in only_worker)


def test_blocked_reason_roundtrips(tmp_path: Path):
    store = MarkdownBoardStore(tmp_path)
    orch = KanbanOrchestrator(store=store, executor=MockAgentaoExecutor())
    c = orch.create_card(title="B", goal="b")
    orch.block(c.id, "needs input")

    reloaded = MarkdownBoardStore(tmp_path)
    got = reloaded.get_card(c.id)
    assert got.status == CardStatus.BLOCKED
    assert got.blocked_reason == "needs input"


def test_agent_profile_round_trips(tmp_path: Path):
    store = MarkdownBoardStore(tmp_path)
    card = Card(
        title="P",
        goal="g",
        agent_profile="gemini-worker",
        agent_profile_source="manual",
    )
    store.add_card(card)

    reloaded = MarkdownBoardStore(tmp_path)
    got = reloaded.get_card(card.id)
    assert got.agent_profile == "gemini-worker"
    assert got.agent_profile_source == "manual"


def test_agent_profile_absent_by_default(tmp_path: Path):
    store = MarkdownBoardStore(tmp_path)
    card = Card(title="P", goal="g")
    store.add_card(card)

    on_disk = (tmp_path / "cards" / f"{card.id}.md").read_text(encoding="utf-8")
    assert "agent_profile" not in on_disk
    assert "agent_profile_source" not in on_disk

    reloaded = MarkdownBoardStore(tmp_path)
    got = reloaded.get_card(card.id)
    assert got.agent_profile is None
    assert got.agent_profile_source is None


def test_legacy_card_without_agent_profile_loads(tmp_path: Path):
    (tmp_path / "cards").mkdir()
    (tmp_path / "cards" / "legacy.md").write_text(
        '+++\n'
        'id = "legacy"\n'
        'title = "L"\n'
        'status = "inbox"\n'
        'priority = 2\n'
        'goal = "g"\n'
        'acceptance_criteria = []\n'
        'context_refs = []\n'
        'depends_on = []\n'
        'history = []\n'
        'created_at = 2026-01-01T00:00:00+00:00\n'
        'updated_at = 2026-01-01T00:00:00+00:00\n'
        '+++\n\n# L\n',
        encoding="utf-8",
    )
    store = MarkdownBoardStore(tmp_path)
    card = store.get_card("legacy")
    assert card.agent_profile is None
    assert card.agent_profile_source is None
