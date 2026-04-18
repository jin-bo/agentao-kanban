"""Regression tests for the Codex review finding that manual CLI/MCP
transitions to a terminal status (``BLOCKED`` / ``DONE``) did not
detach the card's worktree directory. Once the directory was left
attached, ``kanban worktree prune`` skipped the branch (because the
directory still existed), so manually-blocked cards accumulated stale
attached worktrees indefinitely. The fix mirrors the orchestrator's
``_apply_normal_result`` detach step on every manual terminal
transition path.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from kanban.cli import main as cli_main
from kanban.mcp import (
    ServerContext,
    tool_card_add,
    tool_card_block,
    tool_card_move,
    tool_card_unblock,
)
from kanban.models import AgentRole, Card, CardStatus
from kanban.orchestrator import KanbanOrchestrator
from kanban.executors import MockAgentaoExecutor
from kanban.store_markdown import MarkdownBoardStore
from kanban.worktree import WorktreeManager


# ---------- helpers ----------


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    for key, value in (("user.email", "t@t.com"), ("user.name", "T")):
        subprocess.run(
            ["git", "config", key, value],
            cwd=path, check=True, capture_output=True,
        )
    (path / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=path, check=True, capture_output=True,
    )
    return path


def _attach_card_with_worktree(repo: Path) -> tuple[
    MarkdownBoardStore, WorktreeManager, str
]:
    """Add a card and run the orchestrator far enough that the worker
    creates an attached worktree. Returns the store, the worktree
    manager, and the card id with ``card.worktree_branch`` set and the
    on-disk directory present."""
    board = repo / "workspace" / "board"
    board.mkdir(parents=True, exist_ok=True)
    store = MarkdownBoardStore(board)
    wt_mgr = WorktreeManager(
        project_root=repo,
        worktrees_root=repo / "workspace" / "worktrees",
    )
    (repo / "workspace" / "worktrees").mkdir(parents=True, exist_ok=True)
    orch = KanbanOrchestrator(
        store=store,
        executor=MockAgentaoExecutor(),
        worktree_mgr=wt_mgr,
    )
    card = orch.create_card("t", "g")
    # planner ticks promotes inbox→ready; worker tick attaches a worktree.
    orch.tick()
    orch.tick()
    fresh = store.get_card(card.id)
    assert fresh.worktree_branch is not None, (
        "test setup: card should have a worktree branch after worker tick"
    )
    wt_path = wt_mgr.worktrees_root / card.id
    assert wt_path.exists(), "test setup: worktree directory must be on disk"
    return store, wt_mgr, card.id


def _fresh_store(repo: Path) -> MarkdownBoardStore:
    """Reload the store from disk so we see writes the CLI made in its
    own ``MarkdownBoardStore`` instance."""
    return MarkdownBoardStore(repo / "workspace" / "board")


# ---------- CLI: cmd_block ----------


class TestCmdBlockDetachesWorktree:
    def test_cmd_block_releases_worktree_directory(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo")
        _, wt_mgr, cid = _attach_card_with_worktree(repo)
        wt_path = wt_mgr.worktrees_root / cid

        rc = cli_main([
            "--board", str(repo / "workspace" / "board"),
            "block", cid, "stuck on auth",
        ])
        assert rc == 0

        # The Codex finding: directory must be gone, branch retained.
        assert not wt_path.exists()
        fresh = _fresh_store(repo).get_card(cid)
        assert fresh.status == CardStatus.BLOCKED
        # Branch metadata stays so unblock can re-checkout later.
        assert fresh.worktree_branch is not None

    def test_cmd_block_emits_worktree_detached_event(
        self, tmp_path: Path
    ) -> None:
        repo = _init_repo(tmp_path / "repo")
        _, _, cid = _attach_card_with_worktree(repo)

        cli_main([
            "--board", str(repo / "workspace" / "board"),
            "block", cid, "stuck",
        ])

        events = _fresh_store(repo).events_for_card(cid)
        detached = [e for e in events if e.event_type == "worktree.detached"]
        assert len(detached) == 1
        assert "Worktree detached" in detached[0].message

    def test_cmd_block_on_card_without_worktree_is_noop(
        self, tmp_path: Path
    ) -> None:
        # No worktree means nothing to detach; must not raise.
        board = tmp_path / "board"
        board.mkdir(parents=True, exist_ok=True)
        store = MarkdownBoardStore(board)
        card = store.add_card(Card(title="t", goal="g"))
        assert card.worktree_branch is None

        rc = cli_main([
            "--board", str(board), "block", card.id, "x",
        ])
        assert rc == 0
        fresh = MarkdownBoardStore(board).get_card(card.id)
        assert fresh.status == CardStatus.BLOCKED


# ---------- CLI: cmd_move ----------


class TestCmdMoveDetachesOnTerminal:
    def test_cmd_move_to_done_releases_directory(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo")
        _, wt_mgr, cid = _attach_card_with_worktree(repo)
        wt_path = wt_mgr.worktrees_root / cid

        rc = cli_main([
            "--board", str(repo / "workspace" / "board"),
            "move", cid, "done",
        ])
        assert rc == 0
        assert not wt_path.exists()
        assert _fresh_store(repo).get_card(cid).status == CardStatus.DONE

    def test_cmd_move_to_non_terminal_keeps_directory(
        self, tmp_path: Path
    ) -> None:
        repo = _init_repo(tmp_path / "repo")
        _, wt_mgr, cid = _attach_card_with_worktree(repo)
        wt_path = wt_mgr.worktrees_root / cid

        # review is non-terminal — worktree stays attached.
        cli_main([
            "--board", str(repo / "workspace" / "board"),
            "move", cid, "review",
        ])
        assert wt_path.exists()


# ---------- CLI: cmd_card_edit --set-status ----------


class TestCmdCardEditTerminalDetaches:
    def test_set_status_blocked_via_edit_releases_directory(
        self, tmp_path: Path
    ) -> None:
        repo = _init_repo(tmp_path / "repo")
        store, wt_mgr, cid = _attach_card_with_worktree(repo)
        wt_path = wt_mgr.worktrees_root / cid

        rc = cli_main([
            "--board", str(repo / "workspace" / "board"),
            "card", "edit", cid,
            "--set-status", "blocked", "--blocked-reason", "stuck",
        ])
        assert rc == 0
        assert not wt_path.exists()


# ---------- CLI: cmd_unblock to terminal ----------


class TestCmdUnblockToTerminal:
    def test_unblock_to_done_releases_directory(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo")
        store, wt_mgr, cid = _attach_card_with_worktree(repo)
        wt_path = wt_mgr.worktrees_root / cid

        # Park the card in BLOCKED via direct store writes (bypassing
        # the CLI's block command which already detaches), so the
        # directory stays attached. This simulates the on-disk state of
        # a card blocked by an older version that did not release.
        store.update_card(cid, blocked_reason="stale")
        store.move_card(cid, CardStatus.BLOCKED, "Blocked: stale")
        assert wt_path.exists()

        rc = cli_main([
            "--board", str(repo / "workspace" / "board"),
            "unblock", cid, "--to", "done",
        ])
        assert rc == 0
        # Directory must be released by the unblock-to-terminal path.
        assert not wt_path.exists()


# ---------- MCP: tool_card_block / tool_card_move / tool_card_unblock ----------


class TestMcpWritesDetachOnTerminal:
    def test_tool_card_block_releases_directory(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo")
        _, wt_mgr, cid = _attach_card_with_worktree(repo)
        wt_path = wt_mgr.worktrees_root / cid

        ctx = ServerContext(board_dir=repo / "workspace" / "board")
        tool_card_block(ctx, card_id=cid, reason="x")
        assert not wt_path.exists()

    def test_tool_card_move_to_done_releases_directory(
        self, tmp_path: Path
    ) -> None:
        repo = _init_repo(tmp_path / "repo")
        _, wt_mgr, cid = _attach_card_with_worktree(repo)
        wt_path = wt_mgr.worktrees_root / cid

        ctx = ServerContext(board_dir=repo / "workspace" / "board")
        tool_card_move(ctx, card_id=cid, status="done")
        assert not wt_path.exists()

    def test_tool_card_move_to_review_keeps_directory(
        self, tmp_path: Path
    ) -> None:
        repo = _init_repo(tmp_path / "repo")
        _, wt_mgr, cid = _attach_card_with_worktree(repo)
        wt_path = wt_mgr.worktrees_root / cid

        ctx = ServerContext(board_dir=repo / "workspace" / "board")
        tool_card_move(ctx, card_id=cid, status="review")
        assert wt_path.exists()

    def test_tool_card_block_with_disabled_worktree_mode_is_noop(
        self, tmp_path: Path
    ) -> None:
        # ``worktree_mode=False`` short-circuits the cleanup: a server
        # explicitly run with --no-worktree should not touch any
        # worktree state, even on a card that already has one.
        repo = _init_repo(tmp_path / "repo")
        _, wt_mgr, cid = _attach_card_with_worktree(repo)
        wt_path = wt_mgr.worktrees_root / cid

        ctx = ServerContext(
            board_dir=repo / "workspace" / "board",
            worktree_mode=False,
        )
        tool_card_block(ctx, card_id=cid, reason="x")
        assert wt_path.exists()


# ---------- helper unit test ----------


class TestDetachWorktreeOnTerminal:
    def test_helper_no_op_when_mgr_none(self, tmp_path: Path) -> None:
        from kanban.orchestrator import detach_worktree_on_terminal

        board = tmp_path / "board"
        board.mkdir(parents=True, exist_ok=True)
        store = MarkdownBoardStore(board)
        card = store.add_card(Card(title="t", goal="g"))
        # mgr=None — must not raise.
        detach_worktree_on_terminal(store, None, card.id, CardStatus.BLOCKED)
        assert store.get_card(card.id).status == CardStatus.INBOX

    def test_helper_no_op_when_status_not_terminal(
        self, tmp_path: Path
    ) -> None:
        repo = _init_repo(tmp_path / "repo")
        store, wt_mgr, cid = _attach_card_with_worktree(repo)
        wt_path = wt_mgr.worktrees_root / cid

        from kanban.orchestrator import detach_worktree_on_terminal

        # READY is not terminal — directory must stay.
        detach_worktree_on_terminal(store, wt_mgr, cid, CardStatus.READY)
        assert wt_path.exists()

    def test_helper_emits_detached_event(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo")
        store, wt_mgr, cid = _attach_card_with_worktree(repo)

        from kanban.orchestrator import detach_worktree_on_terminal

        detach_worktree_on_terminal(store, wt_mgr, cid, CardStatus.DONE)
        events = store.events_for_card(cid)
        types = [e.event_type for e in events if e.event_type]
        assert "worktree.detached" in types
