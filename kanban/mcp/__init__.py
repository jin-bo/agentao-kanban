"""MCP server exposing the kanban board to MCP clients (Claude Code, Codex, ...).

Wraps the same ``BoardStore`` API the CLI uses; does not shell out. Writes
respect ``.daemon.lock`` (refused while a live daemon holds the board) unless
the server was started with ``--force`` (recovery only).

Run::

    uv run kanban-mcp --board workspace/board

Register with Claude Code::

    claude mcp add kanban -- uv run --directory <repo> kanban-mcp \
        --board workspace/board
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .context import (  # noqa: F401
    CardClaimedError,
    ServerContext,
    _detach_worktree_after_terminal,
    _resolve_worktree_mgr,
)
from .serializers import card_to_dict, event_to_dict  # noqa: F401
from .tools import (  # noqa: F401
    _build_executor,
    _build_orchestrator,
    _coerce_priority,
    _coerce_role,
    _coerce_status,
    _resolve_card_id_mcp,
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

__all__ = [
    "CardClaimedError",
    "ServerContext",
    "build_server",
    "card_to_dict",
    "event_to_dict",
    "main",
    "tool_card_add",
    "tool_card_block",
    "tool_card_list",
    "tool_card_move",
    "tool_card_show",
    "tool_card_unblock",
    "tool_events_tail",
    "tool_run",
    "tool_tick",
]


def build_server(ctx: ServerContext) -> FastMCP:
    """Build a FastMCP instance with all kanban tools/resources bound to ctx."""
    mcp = FastMCP(
        "kanban",
        instructions=(
            "Kanban board over MCP. Card CRUD, events tail, orchestrator "
            "tick/run, and board snapshot resources. Writes are refused "
            "while a kanban daemon holds the board lock."
        ),
    )

    @mcp.tool(description="Create a new kanban card. Returns the created card.")
    def card_add(
        title: str,
        goal: str,
        priority: str = "MEDIUM",
        acceptance: list[str] | None = None,
        depends: list[str] | None = None,
    ) -> dict[str, Any]:
        return tool_card_add(ctx, title, goal, priority, acceptance, depends)

    @mcp.tool(
        description=(
            "List cards, optionally filtered by status "
            "(inbox/ready/doing/review/done/blocked)."
        )
    )
    def card_list(status: str | None = None) -> list[dict[str, Any]]:
        return tool_card_list(ctx, status)

    @mcp.tool(description="Show a single card by id (full fields).")
    def card_show(card_id: str) -> dict[str, Any]:
        return tool_card_show(ctx, card_id)

    @mcp.tool(description="Move a card to a target status.")
    def card_move(card_id: str, status: str) -> dict[str, Any]:
        return tool_card_move(ctx, card_id, status)

    @mcp.tool(description="Block a card with a reason and move it to BLOCKED.")
    def card_block(card_id: str, reason: str) -> dict[str, Any]:
        return tool_card_block(ctx, card_id, reason)

    @mcp.tool(
        description=(
            "Clear a card's blocked_reason and move it to a target status "
            "(default inbox)."
        )
    )
    def card_unblock(card_id: str, to: str = "inbox") -> dict[str, Any]:
        return tool_card_unblock(ctx, card_id, to)

    @mcp.tool(
        description=(
            "Tail the board event log. Filters: card_id, role "
            "(planner/worker/reviewer/verifier), execution_only."
        )
    )
    def events_tail(
        limit: int = 50,
        card_id: str | None = None,
        role: str | None = None,
        execution_only: bool = False,
    ) -> list[dict[str, Any]]:
        return tool_events_tail(ctx, limit, card_id, role, execution_only)

    @mcp.tool(
        description=(
            "Run a single orchestrator tick. Refused while a kanban daemon "
            "holds the board lock."
        )
    )
    def tick() -> dict[str, Any]:
        return tool_tick(ctx)

    @mcp.tool(
        description=(
            "Run the orchestrator until idle or max_steps reached. Refused "
            "while a kanban daemon holds the board lock."
        )
    )
    def run(max_steps: int = 100) -> dict[str, Any]:
        return tool_run(ctx, max_steps)

    @mcp.resource(
        "kanban://board/snapshot",
        mime_type="application/json",
        description="Board snapshot: {status: [card titles]}.",
    )
    def board_snapshot_resource() -> str:
        return json.dumps(ctx.store().board_snapshot(), ensure_ascii=False)

    @mcp.resource(
        "kanban://card/{card_id}",
        mime_type="application/json",
        description="Full card record (JSON) by id.",
    )
    def card_resource(card_id: str) -> str:
        try:
            card = ctx.store().get_card(card_id)
        except KeyError as exc:
            raise ValueError(f"card {card_id} not found") from exc
        return json.dumps(card_to_dict(card), ensure_ascii=False)

    @mcp.resource(
        "kanban://events/recent",
        mime_type="application/json",
        description="Most recent 50 board events as a JSON array.",
    )
    def events_recent_resource() -> str:
        events = ctx.store().list_events(limit=50)
        return json.dumps([event_to_dict(e) for e in events], ensure_ascii=False)

    return mcp


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="kanban-mcp",
        description=(
            "MCP server exposing the kanban board over stdio. Register with "
            "Claude Code via "
            "`claude mcp add kanban -- uv run kanban-mcp --board <dir>`."
        ),
    )
    parser.add_argument(
        "--board",
        type=Path,
        default=Path(os.environ.get("KANBAN_BOARD", "workspace/board")),
        help=(
            "Path to the board directory. Default: $KANBAN_BOARD or "
            "workspace/board (relative to the server's CWD)."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Bypass the daemon-lock guard on writes (recovery only).",
    )
    parser.add_argument(
        "--executor",
        choices=("mock", "agentao", "multi-backend"),
        default="mock",
        help="Executor used by tick/run tools. Default: mock.",
    )
    # Tri-state worktree isolation, matching the CLI: default auto, opt
    # in to hard-require with --worktree, opt out with --no-worktree.
    wt = parser.add_mutually_exclusive_group()
    wt.add_argument(
        "--worktree",
        dest="worktree",
        action="store_true",
        default=None,
        help="Hard-require a Git repo for tick/run worktree isolation.",
    )
    wt.add_argument(
        "--no-worktree",
        dest="worktree",
        action="store_false",
        help="Disable per-card Git worktree isolation for tick/run.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    args.board.mkdir(parents=True, exist_ok=True)
    board_dir = args.board.resolve()
    # Fail fast on ``--worktree`` (hard-require) against a non-Git board,
    # rather than letting the first tick/run raise mid-request and drop
    # the stdio client.
    if args.worktree is True:
        from ..cli import _find_git_root_optional

        if _find_git_root_optional(board_dir) is None:
            print(
                f"kanban-mcp: --worktree requires a Git repository (no "
                f"repo found for {args.board}). Restart with --no-worktree "
                f"or move the board inside a repo.",
                file=sys.stderr,
            )
            return 2
    ctx = ServerContext(
        board_dir=board_dir,
        force=args.force,
        executor_name=args.executor,
        worktree_mode=args.worktree,
    )
    server = build_server(ctx)
    server.run("stdio")
    return 0


if __name__ == "__main__":  # pragma: no cover - manual smoke
    sys.exit(main())
