"""Tests for kanban.mcp — MCP server façade over the BoardStore.

We exercise the underlying tool functions (which take a ``ServerContext``)
directly. They are pure: no FastMCP / async machinery in the way. The
in-process FastMCP wiring is verified once via ``build_server`` to make
sure every tool/resource is registered.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from kanban.daemon import LOCK_FILENAME
from kanban.mcp import (
    ServerContext,
    _build_orchestrator,
    _resolve_worktree_mgr,
    build_server,
    card_to_dict,
    tool_card_add,
    tool_card_block,
    tool_card_list,
    tool_card_move,
    tool_card_show,
    tool_card_unblock,
    tool_events_tail,
    tool_run,
    tool_tick,
)
from kanban.models import (
    AgentRole,
    Card,
    CardStatus,
    RevisionRequest,
)
from kanban.store_markdown import MarkdownBoardStore


# ---------- helpers ----------


@pytest.fixture
def ctx(tmp_path: Path) -> ServerContext:
    board = tmp_path / "board"
    board.mkdir(parents=True, exist_ok=True)
    return ServerContext(board_dir=board)


def _add(ctx: ServerContext, title: str = "T", goal: str = "G", **kw) -> dict:
    return tool_card_add(ctx, title=title, goal=goal, **kw)


# ---------- card lifecycle ----------


class TestCardLifecycle:
    def test_add_then_show_returns_same_card(self, ctx: ServerContext) -> None:
        created = _add(ctx, title="hello", goal="world", priority="HIGH")
        assert created["title"] == "hello"
        assert created["status"] == "inbox"
        assert created["priority"] == "HIGH"

        shown = tool_card_show(ctx, card_id=created["id"])
        assert shown["id"] == created["id"]
        assert shown["title"] == "hello"

    def test_list_filters_by_status(self, ctx: ServerContext) -> None:
        a = _add(ctx, title="A")
        b = _add(ctx, title="B")
        tool_card_move(ctx, card_id=b["id"], status="ready")

        all_cards = tool_card_list(ctx)
        assert {c["title"] for c in all_cards} == {"A", "B"}

        ready = tool_card_list(ctx, status="ready")
        assert [c["id"] for c in ready] == [b["id"]]

        inbox = tool_card_list(ctx, status="inbox")
        assert [c["id"] for c in inbox] == [a["id"]]

    def test_block_records_reason_and_status(self, ctx: ServerContext) -> None:
        c = _add(ctx)
        blocked = tool_card_block(ctx, card_id=c["id"], reason="missing dep")
        assert blocked["status"] == "blocked"
        assert blocked["blocked_reason"] == "missing dep"
        assert blocked["blocked_at"] is not None

    def test_unblock_clears_reason_and_targets_doing(
        self, ctx: ServerContext
    ) -> None:
        c = _add(ctx)
        tool_card_block(ctx, card_id=c["id"], reason="x")
        unblocked = tool_card_unblock(ctx, card_id=c["id"], to="doing")
        assert unblocked["status"] == "doing"
        assert unblocked["blocked_reason"] is None
        assert unblocked["blocked_at"] is None

    def test_show_unknown_card_raises_value_error(
        self, ctx: ServerContext
    ) -> None:
        with pytest.raises(ValueError, match="not found"):
            tool_card_show(ctx, card_id="does-not-exist")

    def test_move_unknown_card_raises_value_error(
        self, ctx: ServerContext
    ) -> None:
        with pytest.raises(ValueError, match="not found"):
            tool_card_move(ctx, card_id="missing", status="ready")

    def test_invalid_status_raises_value_error(
        self, ctx: ServerContext
    ) -> None:
        c = _add(ctx)
        with pytest.raises(ValueError, match="status must be one of"):
            tool_card_move(ctx, card_id=c["id"], status="bogus")

    def test_invalid_priority_raises_value_error(
        self, ctx: ServerContext
    ) -> None:
        with pytest.raises(ValueError, match="priority must be one of"):
            _add(ctx, priority="EXTREME")


# ---------- events ----------


class TestEventsTail:
    def test_tail_returns_recent_events_in_chronological_order(
        self, ctx: ServerContext
    ) -> None:
        c = _add(ctx, title="a")
        tool_card_move(ctx, card_id=c["id"], status="ready")
        tool_card_move(ctx, card_id=c["id"], status="doing")

        events = tool_events_tail(ctx, limit=10)
        # add_card writes "Card created in inbox", then two move events
        # (each carrying the move note, "Manual move via MCP").
        assert len(events) >= 3
        last_three = events[-3:]
        assert all(e["card_id"] == c["id"] for e in last_three)
        assert "inbox" in last_three[0]["message"]
        assert last_three[1]["message"] == "Manual move via MCP"
        assert last_three[2]["message"] == "Manual move via MCP"
        # Strict chronological order on append.
        timestamps = [e["at"] for e in last_three]
        assert timestamps == sorted(timestamps)

    def test_tail_filters_by_card_id(self, ctx: ServerContext) -> None:
        a = _add(ctx, title="a")
        b = _add(ctx, title="b")
        tool_card_move(ctx, card_id=b["id"], status="ready")

        only_b = tool_events_tail(ctx, limit=50, card_id=b["id"])
        assert {e["card_id"] for e in only_b} == {b["id"]}
        # 1 create event + 1 move event for b.
        assert len(only_b) == 2
        assert all(e["card_id"] != a["id"] for e in only_b)

    def test_execution_only_excludes_plain_events(
        self, ctx: ServerContext
    ) -> None:
        # No agent has run yet → no execution events recorded.
        _add(ctx)
        execs = tool_events_tail(ctx, limit=50, execution_only=True)
        assert execs == []

    def test_limit_caps_results(self, ctx: ServerContext) -> None:
        c = _add(ctx)
        for status in ("ready", "doing", "review", "verify", "done"):
            tool_card_move(ctx, card_id=c["id"], status=status)
        few = tool_events_tail(ctx, limit=2)
        assert len(few) == 2


# ---------- daemon-lock guard ----------


def _write_fake_lock(board: Path, pid: int) -> None:
    """Create a .daemon.lock file pointing at a known-live pid."""
    payload = {"pid": pid, "started_at": time.time()}
    (board / LOCK_FILENAME).write_text(json.dumps(payload), encoding="utf-8")


class TestDaemonLockGuard:
    def test_write_refused_while_live_lock_present(
        self, ctx: ServerContext
    ) -> None:
        from kanban.daemon import DaemonLockError

        # Use this test process's pid — guaranteed alive.
        _write_fake_lock(ctx.board_dir, os.getpid())
        with pytest.raises(DaemonLockError, match="Daemon"):
            _add(ctx)

    def test_read_unaffected_by_lock(self, ctx: ServerContext) -> None:
        # Add before locking, then verify reads still work.
        c = _add(ctx)
        _write_fake_lock(ctx.board_dir, os.getpid())
        # Both list and show are read paths — must not raise.
        listed = tool_card_list(ctx)
        assert [x["id"] for x in listed] == [c["id"]]
        shown = tool_card_show(ctx, card_id=c["id"])
        assert shown["id"] == c["id"]

    def test_force_bypasses_lock(self, ctx: ServerContext) -> None:
        ctx.force = True
        _write_fake_lock(ctx.board_dir, os.getpid())
        created = _add(ctx, title="forced")
        assert created["status"] == "inbox"

    def test_stale_lock_is_cleared_so_writes_succeed(
        self, ctx: ServerContext
    ) -> None:
        # PID 1 belongs to init/launchd — alive on every Unix-like system,
        # so we use a deliberately impossible pid (2**31 - 1) instead.
        _write_fake_lock(ctx.board_dir, 2_147_483_647)
        # assert_no_daemon clears stale locks before raising.
        created = _add(ctx, title="stale-cleared")
        assert created["title"] == "stale-cleared"


# ---------- orchestrator triggers ----------


class TestTickAndRun:
    def test_tick_on_idle_board_returns_idle(self, ctx: ServerContext) -> None:
        result = tool_tick(ctx)
        assert result == {"idle": True}

    def test_run_on_idle_board_returns_zero_steps(
        self, ctx: ServerContext
    ) -> None:
        result = tool_run(ctx, max_steps=5)
        assert result == {"steps": 0}

    def test_run_drives_card_to_done_with_mock_executor(
        self, ctx: ServerContext
    ) -> None:
        c = _add(ctx)
        result = tool_run(ctx, max_steps=20)
        assert result["steps"] > 0
        final = tool_card_show(ctx, card_id=c["id"])
        assert final["status"] in {
            CardStatus.DONE.value,
            CardStatus.BLOCKED.value,
        }


# ---------- FastMCP wiring ----------


class TestFastMCPWiring:
    def test_all_tools_registered(self, ctx: ServerContext) -> None:
        server = build_server(ctx)
        names = sorted(t.name for t in asyncio.run(server.list_tools()))
        assert names == [
            "card_add",
            "card_block",
            "card_list",
            "card_move",
            "card_show",
            "card_unblock",
            "events_tail",
            "run",
            "tick",
        ]

    def test_static_resources_registered(self, ctx: ServerContext) -> None:
        server = build_server(ctx)
        uris = sorted(
            r.uri.unicode_string() for r in asyncio.run(server.list_resources())
        )
        assert uris == [
            "kanban://board/snapshot",
            "kanban://events/recent",
        ]

    def test_card_resource_template_registered(
        self, ctx: ServerContext
    ) -> None:
        server = build_server(ctx)
        templates = [
            t.uriTemplate
            for t in asyncio.run(server.list_resource_templates())
        ]
        assert "kanban://card/{card_id}" in templates

    def test_call_card_add_through_fastmcp(self, ctx: ServerContext) -> None:
        server = build_server(ctx)
        result = asyncio.run(
            server.call_tool(
                "card_add",
                {"title": "via-fastmcp", "goal": "g"},
            )
        )
        # FastMCP returns either a Sequence[ContentBlock] or a structured
        # dict. Both call paths surface our return value — check the dict
        # form when present, fall back to parsing JSON text content.
        if isinstance(result, dict):
            payload = result
        else:
            content_blocks, structured = result
            payload = structured if structured is not None else json.loads(
                content_blocks[0].text
            )
        assert payload["title"] == "via-fastmcp"
        assert payload["status"] == "inbox"

    def test_read_board_snapshot_resource(self, ctx: ServerContext) -> None:
        _add(ctx, title="snap")
        server = build_server(ctx)
        contents = list(
            asyncio.run(server.read_resource("kanban://board/snapshot"))
        )
        snapshot = json.loads(contents[0].content)
        assert snapshot.get("inbox") == ["snap"]


# ---------- revision_requests serialization (Codex P2) ----------


class TestRevisionRequestSerialization:
    def test_card_to_dict_emits_revision_requests(self) -> None:
        card = Card(
            title="t",
            goal="g",
            revision_requests=[
                RevisionRequest(
                    at=datetime(2026, 4, 16, 12, 0, tzinfo=timezone.utc),
                    from_role=AgentRole.REVIEWER,
                    iteration=1,
                    summary="tighten error path",
                    hints=["check 5xx mapping"],
                    failing_criteria=["criterion-2"],
                ),
            ],
            rework_iteration=1,
        )
        d = card_to_dict(card)
        assert d["rework_iteration"] == 1
        assert d["revision_requests"] == [
            {
                "iteration": 1,
                "from_role": "reviewer",
                "at": "2026-04-16T12:00:00+00:00",
                "summary": "tighten error path",
                "hints": ["check 5xx mapping"],
                "failing_criteria": ["criterion-2"],
            }
        ]

    def test_card_show_round_trips_revision_request(
        self, ctx: ServerContext
    ) -> None:
        c = _add(ctx)
        store = ctx.store()
        store.update_card(
            c["id"],
            revision_requests=[
                RevisionRequest(
                    at=datetime(2026, 4, 16, 12, 0, tzinfo=timezone.utc),
                    from_role=AgentRole.VERIFIER,
                    iteration=2,
                    summary="missing acceptance proof",
                ),
            ],
            rework_iteration=2,
        )
        shown = tool_card_show(ctx, card_id=c["id"])
        assert shown["rework_iteration"] == 2
        assert len(shown["revision_requests"]) == 1
        rr = shown["revision_requests"][0]
        assert rr["from_role"] == "verifier"
        assert rr["iteration"] == 2
        assert rr["summary"] == "missing acceptance proof"
        # Optional fields omitted when empty.
        assert "hints" not in rr
        assert "failing_criteria" not in rr

    def test_empty_revision_requests_serialized_as_empty_list(
        self, ctx: ServerContext
    ) -> None:
        c = _add(ctx)
        shown = tool_card_show(ctx, card_id=c["id"])
        assert shown["revision_requests"] == []


# ---------- worktree wiring on tick/run (Codex P1) ----------


def _git_init(path: Path) -> Path:
    """Create a minimal git repo with one commit (mirrors test_cli_worktree_flag)."""
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


class TestWorktreeWiring:
    def test_resolver_returns_none_when_disabled(
        self, ctx: ServerContext
    ) -> None:
        ctx.worktree_mode = False
        assert _resolve_worktree_mgr(ctx) is None

    def test_resolver_returns_none_in_auto_outside_repo(
        self, ctx: ServerContext, capsys
    ) -> None:
        ctx.worktree_mode = None
        assert _resolve_worktree_mgr(ctx) is None
        err = capsys.readouterr().err
        assert "worktree isolation disabled" in err

    def test_resolver_raises_when_required_outside_repo(
        self, ctx: ServerContext
    ) -> None:
        # Must be a normal exception (not SystemExit) so the mid-request
        # path surfaces a tool error instead of killing the stdio server.
        ctx.worktree_mode = True
        with pytest.raises(RuntimeError, match="requires a Git repository"):
            _resolve_worktree_mgr(ctx)

    def test_resolver_returns_manager_inside_git_repo(
        self, tmp_path: Path
    ) -> None:
        repo = _git_init(tmp_path / "repo")
        board = repo / "workspace" / "board"
        board.mkdir(parents=True, exist_ok=True)
        ctx = ServerContext(board_dir=board, worktree_mode=None)
        from kanban.worktree import WorktreeManager

        mgr = _resolve_worktree_mgr(ctx)
        assert isinstance(mgr, WorktreeManager)

    def test_build_orchestrator_passes_worktree_mgr(
        self, tmp_path: Path
    ) -> None:
        repo = _git_init(tmp_path / "repo")
        board = repo / "workspace" / "board"
        board.mkdir(parents=True, exist_ok=True)
        ctx = ServerContext(board_dir=board, worktree_mode=None)
        orch = _build_orchestrator(ctx)
        # Without the fix this is None; with the fix it's a WorktreeManager.
        assert orch.worktree_mgr is not None

    def test_build_orchestrator_omits_worktree_mgr_when_disabled(
        self, ctx: ServerContext
    ) -> None:
        ctx.worktree_mode = False
        orch = _build_orchestrator(ctx)
        assert orch.worktree_mgr is None
