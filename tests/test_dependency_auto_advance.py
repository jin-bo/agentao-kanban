"""Tests for the INBOX → READY auto-advance helper and its wiring.

The helper promotes INBOX cards whose ``depends_on`` becomes fully
satisfied the moment one of their parents transitions into DONE. It is
intentionally O(n) over the board per trigger; these tests pin the
behavior across the orchestrator commit path, CLI paths, and the MCP
tool path.
"""

from __future__ import annotations

from pathlib import Path

from kanban import CardStatus, InMemoryBoardStore, KanbanOrchestrator
from kanban.cli import main as cli_main
from kanban.executors import MockAgentaoExecutor
from kanban.mcp import ServerContext, tool_card_move, tool_card_unblock
from kanban.models import Card
from kanban.orchestrator import advance_inbox_dependents
from kanban.store_markdown import MarkdownBoardStore


# ---------- helper unit tests ----------


def _make_store() -> InMemoryBoardStore:
    return InMemoryBoardStore()


def test_advance_promotes_single_inbox_dependent():
    store = _make_store()
    parent = store.add_card(Card(title="parent", goal="p", status=CardStatus.DONE))
    child = store.add_card(
        Card(title="child", goal="c", status=CardStatus.INBOX, depends_on=[parent.id])
    )

    advanced = advance_inbox_dependents(store, parent.id)

    assert advanced == [child.id]
    assert store.get_card(child.id).status == CardStatus.READY


def test_advance_waits_for_all_dependencies():
    store = _make_store()
    p1 = store.add_card(Card(title="p1", goal="g", status=CardStatus.DONE))
    p2 = store.add_card(Card(title="p2", goal="g", status=CardStatus.DOING))
    child = store.add_card(
        Card(title="c", goal="g", status=CardStatus.INBOX, depends_on=[p1.id, p2.id])
    )

    # Only p1 is DONE — child must stay in INBOX.
    assert advance_inbox_dependents(store, p1.id) == []
    assert store.get_card(child.id).status == CardStatus.INBOX

    # Flip p2 to DONE — now a trigger from p2 should advance the child.
    store.move_card(p2.id, CardStatus.DONE, "manual done")
    advanced = advance_inbox_dependents(store, p2.id)
    assert advanced == [child.id]
    assert store.get_card(child.id).status == CardStatus.READY


def test_advance_leaves_non_inbox_dependents_alone():
    store = _make_store()
    parent = store.add_card(Card(title="p", goal="g", status=CardStatus.DONE))
    # READY / DOING / BLOCKED / DONE children must not be touched.
    ready = store.add_card(
        Card(title="ready", goal="g", status=CardStatus.READY, depends_on=[parent.id])
    )
    doing = store.add_card(
        Card(title="doing", goal="g", status=CardStatus.DOING, depends_on=[parent.id])
    )
    blocked = store.add_card(
        Card(title="blk", goal="g", status=CardStatus.BLOCKED, depends_on=[parent.id])
    )
    done = store.add_card(
        Card(title="done", goal="g", status=CardStatus.DONE, depends_on=[parent.id])
    )

    advance_inbox_dependents(store, parent.id)

    assert store.get_card(ready.id).status == CardStatus.READY
    assert store.get_card(doing.id).status == CardStatus.DOING
    assert store.get_card(blocked.id).status == CardStatus.BLOCKED
    assert store.get_card(done.id).status == CardStatus.DONE


def test_advance_is_non_recursive():
    """A promoted INBOX→READY child does not itself trigger advancement
    of its own dependents (grandchildren). Chains advance one parent at
    a time, as each parent actually reaches DONE."""
    store = _make_store()
    p = store.add_card(Card(title="p", goal="g", status=CardStatus.DONE))
    c = store.add_card(
        Card(title="c", goal="g", status=CardStatus.INBOX, depends_on=[p.id])
    )
    gc = store.add_card(
        Card(title="gc", goal="g", status=CardStatus.INBOX, depends_on=[c.id])
    )

    advance_inbox_dependents(store, p.id)

    assert store.get_card(c.id).status == CardStatus.READY
    # Grandchild still blocked: its parent is READY, not DONE.
    assert store.get_card(gc.id).status == CardStatus.INBOX


# ---------- orchestrator wiring ----------


def test_orchestrator_done_triggers_auto_advance(tmp_path: Path):
    store = MarkdownBoardStore(tmp_path)
    orch = KanbanOrchestrator(store=store, executor=MockAgentaoExecutor())
    parent = store.add_card(Card(title="p", goal="g", acceptance_criteria=["x"]))
    child_id = store.add_card(
        Card(
            title="c",
            goal="g",
            status=CardStatus.INBOX,
            depends_on=[parent.id],
            acceptance_criteria=["y"],
        )
    ).id

    orch.run_until_idle(max_steps=200)

    assert store.get_card(parent.id).status == CardStatus.DONE
    # Child also got picked up via the normal flow after auto-advance.
    assert store.get_card(child_id).status == CardStatus.DONE


def test_done_to_done_does_not_reemit_auto_advance(tmp_path: Path):
    """Replaying a DONE transition (e.g. a redundant manual move) must
    not emit a second ``dependencies.satisfied`` event."""
    store = MarkdownBoardStore(tmp_path)
    parent = store.add_card(Card(title="p", goal="g", status=CardStatus.DONE))
    child = store.add_card(
        Card(
            title="c",
            goal="g",
            status=CardStatus.INBOX,
            depends_on=[parent.id],
        )
    )

    # First transition already happened before the child was ever inboxed,
    # so the child is still INBOX. Calling the helper simulates the first
    # "parent → DONE" trigger.
    advance_inbox_dependents(store, parent.id)
    assert store.get_card(child.id).status == CardStatus.READY
    first_events = [
        e for e in store.events_for_card(child.id)
        if e.event_type == "dependencies.satisfied"
    ]
    assert len(first_events) == 1

    # Replaying the helper on an already-promoted child must not re-emit.
    # (The child is now READY, so the helper correctly skips it.)
    advance_inbox_dependents(store, parent.id)
    second_events = [
        e for e in store.events_for_card(child.id)
        if e.event_type == "dependencies.satisfied"
    ]
    assert len(second_events) == 1


# ---------- CLI wiring ----------


def test_cli_move_to_done_triggers_auto_advance(tmp_path: Path):
    board = tmp_path / "board"
    assert (
        cli_main(["--board", str(board), "card", "add", "--title", "p", "--goal", "g"])
        == 0
    )
    parent_id = MarkdownBoardStore(board).list_cards()[0].id

    # Child depends on parent and starts in INBOX.
    assert (
        cli_main(
            [
                "--board", str(board),
                "card", "add",
                "--title", "c", "--goal", "g",
                "--depends", parent_id,
            ]
        )
        == 0
    )
    child_id = [c.id for c in MarkdownBoardStore(board).list_cards() if c.id != parent_id][0]

    # Manually move parent → done via CLI. Child must auto-advance to READY.
    assert cli_main(["--board", str(board), "move", parent_id, "done"]) == 0

    store = MarkdownBoardStore(board)
    assert store.get_card(parent_id).status == CardStatus.DONE
    assert store.get_card(child_id).status == CardStatus.READY


def test_cli_card_edit_set_status_done_triggers_auto_advance(tmp_path: Path):
    board = tmp_path / "board"
    assert (
        cli_main(["--board", str(board), "card", "add", "--title", "p", "--goal", "g"])
        == 0
    )
    parent_id = MarkdownBoardStore(board).list_cards()[0].id
    assert (
        cli_main(
            [
                "--board", str(board),
                "card", "add",
                "--title", "c", "--goal", "g",
                "--depends", parent_id,
            ]
        )
        == 0
    )
    child_id = [c.id for c in MarkdownBoardStore(board).list_cards() if c.id != parent_id][0]

    # `card edit --set-status done` must fan out to dependents just like `move`.
    assert (
        cli_main(
            ["--board", str(board), "card", "edit", parent_id, "--set-status", "done"]
        )
        == 0
    )

    store = MarkdownBoardStore(board)
    assert store.get_card(parent_id).status == CardStatus.DONE
    assert store.get_card(child_id).status == CardStatus.READY


def test_cli_unblock_to_done_triggers_auto_advance(tmp_path: Path):
    board = tmp_path / "board"
    assert (
        cli_main(["--board", str(board), "card", "add", "--title", "p", "--goal", "g"])
        == 0
    )
    parent_id = MarkdownBoardStore(board).list_cards()[0].id
    assert (
        cli_main(
            [
                "--board", str(board),
                "card", "add",
                "--title", "c", "--goal", "g",
                "--depends", parent_id,
            ]
        )
        == 0
    )
    child_id = [c.id for c in MarkdownBoardStore(board).list_cards() if c.id != parent_id][0]

    # Block the parent, then unblock directly to DONE.
    assert cli_main(["--board", str(board), "block", parent_id, "wait"]) == 0
    assert cli_main(["--board", str(board), "unblock", parent_id, "--to", "done"]) == 0

    store = MarkdownBoardStore(board)
    assert store.get_card(parent_id).status == CardStatus.DONE
    assert store.get_card(child_id).status == CardStatus.READY


# ---------- MCP wiring ----------


def _mcp_ctx(board: Path) -> ServerContext:
    return ServerContext(
        board_dir=board,
        executor_name="mock",
        force=False,
        worktree_mode=False,
    )


def test_mcp_card_move_to_done_triggers_auto_advance(tmp_path: Path):
    board = tmp_path / "board"
    store = MarkdownBoardStore(board)
    parent = store.add_card(Card(title="p", goal="g"))
    child = store.add_card(
        Card(title="c", goal="g", status=CardStatus.INBOX, depends_on=[parent.id])
    )

    ctx = _mcp_ctx(board)
    tool_card_move(ctx, parent.id, "done")

    fresh = MarkdownBoardStore(board)
    assert fresh.get_card(parent.id).status == CardStatus.DONE
    assert fresh.get_card(child.id).status == CardStatus.READY


def test_mcp_card_unblock_to_done_triggers_auto_advance(tmp_path: Path):
    board = tmp_path / "board"
    store = MarkdownBoardStore(board)
    parent = store.add_card(
        Card(title="p", goal="g", status=CardStatus.BLOCKED, blocked_reason="wait")
    )
    child = store.add_card(
        Card(title="c", goal="g", status=CardStatus.INBOX, depends_on=[parent.id])
    )

    ctx = _mcp_ctx(board)
    tool_card_unblock(ctx, parent.id, "done")

    fresh = MarkdownBoardStore(board)
    assert fresh.get_card(parent.id).status == CardStatus.DONE
    assert fresh.get_card(child.id).status == CardStatus.READY
