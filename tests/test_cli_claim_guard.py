"""Regression tests for the Codex adversarial-review finding that the CLI
documented `.daemon.lock` as a universal writer guard while workers
actually run without it.

Before the fix, an operator could run ``kanban --role worker`` (no board
lock), then issue ``move`` / ``requeue`` / ``card edit`` against a card
the worker held a claim on, and the edit would race the worker's next
envelope. The fix adds a per-card live-claim guard on top of the board
lock: every per-card mutating command refuses when ``get_claim(card_id)``
returns a live claim, unless ``--force`` is set.
"""
from __future__ import annotations

import pytest
from pathlib import Path

from kanban.cli import main
from kanban.executors import MockAgentaoExecutor
from kanban.models import AgentRole, Card, CardStatus
from kanban.orchestrator import KanbanOrchestrator
from kanban.store_markdown import MarkdownBoardStore


def _card_with_claim(board: Path) -> tuple[MarkdownBoardStore, str]:
    store = MarkdownBoardStore(board)
    card = store.add_card(
        Card(
            title="t",
            goal="g",
            status=CardStatus.READY,
            owner_role=AgentRole.WORKER,
            acceptance_criteria=["x"],
        )
    )
    orch = KanbanOrchestrator(store=store, executor=MockAgentaoExecutor())
    claim = orch.select_and_claim(worker_id=None)
    assert claim is not None
    # Simulate a worker acquiring the claim.
    store.try_acquire_claim(card.id, worker_id="worker-1")
    return store, card.id


GUARDED = [
    lambda cid: ["card", "edit", cid, "--title", "New"],
    lambda cid: ["card", "context", "add", cid, "--path", "p"],
    lambda cid: ["card", "context", "rm", cid, "--path", "p"],
    lambda cid: ["card", "acceptance", "add", cid, "--item", "x"],
    lambda cid: ["card", "acceptance", "rm", cid, "--index", "1"],
    lambda cid: ["card", "acceptance", "clear", cid],
    lambda cid: ["move", cid, "ready"],
    lambda cid: ["block", cid, "reason"],
    lambda cid: ["unblock", cid],
    lambda cid: ["requeue", cid],
]


@pytest.mark.parametrize("argv_for", GUARDED)
def test_mutating_command_refuses_when_live_claim_exists(
    argv_for, tmp_path: Path, capsys
):
    board = tmp_path / "b"
    store, cid = _card_with_claim(board)
    with pytest.raises(SystemExit) as exc:
        main(["--board", str(board), *argv_for(cid)])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "live execution claim" in err
    # Live claim still intact.
    assert store.get_claim(cid) is not None


def test_force_bypasses_live_claim_guard(tmp_path: Path, capsys):
    """--force is the documented escape hatch; mutation must go through."""
    board = tmp_path / "b"
    _, cid = _card_with_claim(board)
    rc = main(
        [
            "--board",
            str(board),
            "--force",
            "card",
            "edit",
            cid,
            "--title",
            "Forced",
        ]
    )
    assert rc == 0
    assert MarkdownBoardStore(board).get_card(cid).title == "Forced"


def test_mutation_allowed_when_no_claim_present(tmp_path: Path):
    """Without a live claim the guard is a no-op."""
    board = tmp_path / "b"
    store = MarkdownBoardStore(board)
    card = store.add_card(Card(title="t", goal="g"))
    rc = main(
        ["--board", str(board), "card", "edit", card.id, "--title", "Quiet"]
    )
    assert rc == 0
    assert MarkdownBoardStore(board).get_card(card.id).title == "Quiet"


def test_guard_message_names_worker_id(tmp_path: Path, capsys):
    board = tmp_path / "b"
    store, cid = _card_with_claim(board)
    with pytest.raises(SystemExit):
        main(["--board", str(board), "move", cid, "ready"])
    err = capsys.readouterr().err
    assert "worker=worker-1" in err
    assert "`kanban claims" in err  # guidance to run claims


def test_card_add_still_uses_board_lock_only(tmp_path: Path):
    """`card add` has no card_id to guard per-card; verify it's untouched
    by the claim check (board-lock-only remains the right scope)."""
    board = tmp_path / "b"
    # Create a claim on a DIFFERENT card. `card add` should not be blocked
    # by it.
    _card_with_claim(board)
    rc = main(
        [
            "--board",
            str(board),
            "card",
            "add",
            "--title",
            "new",
            "--goal",
            "g",
        ]
    )
    assert rc == 0


def test_guard_still_fires_with_unassigned_claim(tmp_path: Path, capsys):
    """A claim can be live but not yet acquired (worker_id is None). That
    still means work is in-flight — the guard must refuse."""
    board = tmp_path / "b"
    store = MarkdownBoardStore(board)
    card = store.add_card(
        Card(
            title="t",
            goal="g",
            status=CardStatus.READY,
            owner_role=AgentRole.WORKER,
        )
    )
    orch = KanbanOrchestrator(store=store, executor=MockAgentaoExecutor())
    claim = orch.select_and_claim(worker_id=None)
    assert claim is not None and claim.worker_id is None

    with pytest.raises(SystemExit) as exc:
        main(["--board", str(board), "move", card.id, "ready"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "unassigned" in err
