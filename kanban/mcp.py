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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .daemon import DaemonLockError, assert_no_daemon
from .models import (
    AgentRole,
    Card,
    CardEvent,
    CardPriority,
    CardStatus,
    ContextRef,
    RevisionRequest,
)
from .orchestrator import KanbanOrchestrator
from .store_markdown import MarkdownBoardStore


_VALID_PRIORITIES = tuple(p.name for p in CardPriority)
_VALID_STATUSES = tuple(s.value for s in CardStatus)
_VALID_ROLES = tuple(r.value for r in AgentRole)


# ---------- serialization ----------


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _ref_to_dict(r: ContextRef) -> dict[str, Any]:
    out: dict[str, Any] = {"path": r.path, "kind": r.kind}
    if r.note:
        out["note"] = r.note
    return out


def _revision_to_dict(rev: RevisionRequest) -> dict[str, Any]:
    out: dict[str, Any] = {
        "iteration": rev.iteration,
        "from_role": rev.from_role.value,
        "at": _iso(rev.at),
        "summary": rev.summary,
    }
    if rev.hints:
        out["hints"] = list(rev.hints)
    if rev.failing_criteria:
        out["failing_criteria"] = list(rev.failing_criteria)
    return out


def card_to_dict(card: Card) -> dict[str, Any]:
    """JSON-serializable Card for MCP tool/resource responses."""
    return {
        "id": card.id,
        "title": card.title,
        "goal": card.goal,
        "status": card.status.value,
        "priority": card.priority.name,
        "owner_role": card.owner_role.value if card.owner_role else None,
        "blocked_reason": card.blocked_reason,
        "blocked_at": _iso(card.blocked_at),
        "created_at": _iso(card.created_at),
        "updated_at": _iso(card.updated_at),
        "depends_on": list(card.depends_on),
        "acceptance_criteria": list(card.acceptance_criteria),
        "context_refs": [_ref_to_dict(r) for r in card.context_refs],
        "outputs": dict(card.outputs),
        "history": list(card.history),
        "agent_profile": card.agent_profile,
        "agent_profile_source": card.agent_profile_source,
        "worktree_branch": card.worktree_branch,
        "worktree_base_commit": card.worktree_base_commit,
        "rework_iteration": card.rework_iteration,
        # Reviewer/verifier feedback the worker is supposed to address on
        # the next pass. Without this, MCP clients see a bumped
        # ``rework_iteration`` but cannot read the actual rework asks.
        "revision_requests": [
            _revision_to_dict(r) for r in card.revision_requests
        ],
    }


def event_to_dict(e: CardEvent) -> dict[str, Any]:
    """JSON-serializable CardEvent for events_tail and events resource."""
    rec: dict[str, Any] = {
        "at": _iso(e.at),
        "card_id": e.card_id,
        "message": e.message,
    }
    if e.is_execution:
        rec["role"] = e.role.value if e.role else None
        rec["prompt_version"] = e.prompt_version
        rec["duration_ms"] = e.duration_ms
        rec["attempt"] = e.attempt
        if e.raw_path is not None:
            rec["raw_path"] = e.raw_path
    for key, value in (
        ("event_type", e.event_type),
        ("claim_id", e.claim_id),
        ("worker_id", e.worker_id),
        ("failure_reason", e.failure_reason),
        ("failure_category", e.failure_category),
        ("retry_of_claim_id", e.retry_of_claim_id),
        ("worktree_branch", e.worktree_branch),
        ("rework_iteration", e.rework_iteration),
    ):
        if value is not None:
            rec[key] = value
    return rec


# ---------- server context ----------


class CardClaimedError(RuntimeError):
    """MCP write refused because the target card has a live execution claim.

    Mirrors the CLI's ``_require_card_writable`` refusal so split-topology
    workers (which don't hold ``.daemon.lock``) still prevent operators
    from racing an in-flight execution.
    """


@dataclass
class ServerContext:
    """Runtime config bound to one MCP server process."""

    board_dir: Path
    force: bool = False
    executor_name: str = "mock"
    # Worktree isolation tri-state, matching the CLI's --worktree flag:
    # None = auto (enable in a Git repo, warn-and-disable otherwise),
    # True = hard-require a Git repo, False = always disabled.
    worktree_mode: bool | None = None

    def store(self) -> MarkdownBoardStore:
        # New store per call: cheap, and any out-of-band daemon write is
        # picked up on the next read without an explicit refresh().
        return MarkdownBoardStore(self.board_dir)

    def guard_write(self) -> None:
        """Refuse writes while a live daemon holds .daemon.lock.

        Mirrors the CLI's ``_require_writable`` semantics. ``--force`` on
        server startup disables the guard for recovery sessions.
        """
        if self.force:
            return
        assert_no_daemon(self.board_dir)

    def guard_card_write(self, card_id: str) -> None:
        """Board-lock + per-card live-claim guard (v0.1.2 split topology).

        Workers don't hold ``.daemon.lock`` but do hold live claims on
        specific cards while executing. Mutating a card with a live
        claim races the worker's next envelope — the worker's result can
        overwrite the operator's change, or the change can invalidate
        the assumptions of the in-flight execution. Refuse unless the
        server was started with ``--force``.
        """
        self.guard_write()
        if self.force:
            return
        claim = self.store().get_claim(card_id)
        if claim is None:
            return
        worker_tag = (
            f"worker={claim.worker_id}" if claim.worker_id else "unassigned"
        )
        raise CardClaimedError(
            f"Card {card_id[:8]} has a live execution claim "
            f"{claim.claim_id} ({worker_tag}); refuse to mutate. Stop "
            f"the claimed worker (or wait for it to finish), then retry. "
            f"Restart kanban-mcp with --force to override (may race with "
            f"in-flight execution)."
        )


def _detach_worktree_after_terminal(
    ctx: ServerContext, store: MarkdownBoardStore, card_id: str
) -> None:
    """MCP counterpart to ``cli._detach_worktree_after_terminal_cli``.

    Called after every MCP write that can land a card in DONE/BLOCKED
    (block, unblock-to-done, move-to-terminal). Without this, a card
    transitioned to a terminal state via MCP keeps its
    ``workspace/worktrees/<card-id>`` directory attached forever, and
    ``worktree prune`` skips the branch because the directory still
    exists.
    """
    if ctx.worktree_mode is False:
        return
    try:
        card = store.get_card(card_id)
    except KeyError:
        return
    if card.worktree_branch is None:
        return
    if card.status not in (CardStatus.DONE, CardStatus.BLOCKED):
        return
    from .cli import _find_git_root_optional

    project_root = _find_git_root_optional(ctx.board_dir)
    if project_root is None:
        return
    from .orchestrator import detach_worktree_on_terminal
    from .worktree import WorktreeManager

    wt_mgr = WorktreeManager(
        project_root=project_root,
        worktrees_root=project_root / "workspace" / "worktrees",
    )
    detach_worktree_on_terminal(store, wt_mgr, card_id, card.status)


def _resolve_worktree_mgr(ctx: ServerContext):
    """Resolve the WorktreeManager for this MCP server's tick/run path.

    Mirrors ``cli._resolve_worktree_mgr``: tri-state on ``worktree_mode``,
    same auto/required/disabled semantics, same warning text. Without
    this, MCP-driven ``tick``/``run`` would execute worker/reviewer/
    verifier in the shared checkout instead of the per-card worktree the
    daemon and CLI use.
    """
    if ctx.worktree_mode is False:
        return None
    from .cli import _find_git_root_optional

    project_root = _find_git_root_optional(ctx.board_dir)
    if project_root is None:
        if ctx.worktree_mode is True:
            # Hard-require mode: surface a tool-level error rather than
            # SystemExit, which would kill the stdio server mid-request.
            # Startup validation in ``main()`` rejects this combination
            # up front; this branch is defense-in-depth for programmatic
            # ``ServerContext`` construction.
            raise RuntimeError(
                f"--worktree requires a Git repository (no repo found for "
                f"{ctx.board_dir}); restart kanban-mcp with --no-worktree "
                f"or move the board inside a repo."
            )
        print(
            "kanban-mcp: worktree isolation disabled (board not in a Git "
            "repo). Pass --no-worktree to silence, or --worktree to "
            "hard-require a repo.",
            file=sys.stderr,
        )
        return None
    from .worktree import WorktreeManager

    return WorktreeManager(
        project_root=project_root,
        worktrees_root=project_root / "workspace" / "worktrees",
    )


# ---------- coercion ----------


def _coerce_priority(value: str) -> CardPriority:
    try:
        return CardPriority[value.upper()]
    except KeyError as exc:
        raise ValueError(
            f"priority must be one of {_VALID_PRIORITIES}, got {value!r}"
        ) from exc


def _coerce_status(value: str) -> CardStatus:
    try:
        return CardStatus(value.lower())
    except ValueError as exc:
        raise ValueError(
            f"status must be one of {_VALID_STATUSES}, got {value!r}"
        ) from exc


def _coerce_role(value: str | None) -> AgentRole | None:
    if value is None:
        return None
    try:
        return AgentRole(value.lower())
    except ValueError as exc:
        raise ValueError(
            f"role must be one of {_VALID_ROLES}, got {value!r}"
        ) from exc


def _resolve_card_id_mcp(store: MarkdownBoardStore, given: str) -> str:
    """MCP counterpart to the CLI's ``_resolve_card_id``.

    Expands a unique full/prefix card id to its canonical full id so
    dependency lists persisted from MCP stay resolvable by
    ``_deps_satisfied``. Raises ``ValueError`` on ambiguity instead of
    the CLI's ``SystemExit`` — MCP surfaces this as a tool error to the
    client rather than killing the stdio server.
    """
    try:
        store.get_card(given)
        return given
    except KeyError:
        pass
    matches = [c.id for c in store.list_cards() if c.id.startswith(given)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        shown = ", ".join(m[:12] for m in matches[:5])
        more = f" (+{len(matches) - 5} more)" if len(matches) > 5 else ""
        raise ValueError(
            f"Ambiguous card id prefix {given!r} matches {len(matches)} "
            f"cards: {shown}{more}. Provide more characters."
        )
    return given


def _build_executor(name: str, board_dir: Path):
    if name == "mock":
        from .executors import MockAgentaoExecutor

        return MockAgentaoExecutor()
    if name == "agentao":
        from .executors.agentao_multi import AgentaoMultiAgentExecutor

        return AgentaoMultiAgentExecutor()
    if name == "multi-backend":
        # Reuse the CLI factory so config resolution matches exactly.
        from .cli import _build_executor as _cli_build

        return _cli_build(name, board=board_dir)
    raise ValueError(f"Unknown executor: {name}")


# ---------- tool implementations (pure, take ServerContext) ----------


def tool_card_add(
    ctx: ServerContext,
    title: str,
    goal: str,
    priority: str = "MEDIUM",
    acceptance: list[str] | None = None,
    depends: list[str] | None = None,
) -> dict[str, Any]:
    ctx.guard_write()
    store = ctx.store()
    resolved_depends = [_resolve_card_id_mcp(store, d) for d in (depends or [])]
    card = store.add_card(
        Card(
            title=title,
            goal=goal,
            priority=_coerce_priority(priority),
            acceptance_criteria=list(acceptance or []),
            depends_on=resolved_depends,
        )
    )
    return card_to_dict(card)


def tool_card_list(
    ctx: ServerContext, status: str | None = None
) -> list[dict[str, Any]]:
    store = ctx.store()
    if status is None:
        cards = store.list_cards()
    else:
        cards = store.list_by_status(_coerce_status(status))
    return [card_to_dict(c) for c in cards]


def tool_card_show(ctx: ServerContext, card_id: str) -> dict[str, Any]:
    store = ctx.store()
    try:
        card = store.get_card(card_id)
    except KeyError as exc:
        raise ValueError(f"card {card_id} not found") from exc
    return card_to_dict(card)


def tool_card_move(
    ctx: ServerContext, card_id: str, status: str
) -> dict[str, Any]:
    ctx.guard_card_write(card_id)
    store = ctx.store()
    try:
        previous_status = store.get_card(card_id).status
        card = store.move_card(
            card_id, _coerce_status(status), "Manual move via MCP"
        )
    except KeyError as exc:
        raise ValueError(f"card {card_id} not found") from exc
    if card.status == CardStatus.DONE and previous_status != CardStatus.DONE:
        from .orchestrator import advance_inbox_dependents

        advance_inbox_dependents(store, card.id)
    _detach_worktree_after_terminal(ctx, store, card.id)
    return card_to_dict(card)


def tool_card_block(
    ctx: ServerContext, card_id: str, reason: str
) -> dict[str, Any]:
    ctx.guard_card_write(card_id)
    store = ctx.store()
    try:
        store.update_card(card_id, blocked_reason=reason)
        card = store.move_card(
            card_id, CardStatus.BLOCKED, f"Blocked: {reason}"
        )
    except KeyError as exc:
        raise ValueError(f"card {card_id} not found") from exc
    _detach_worktree_after_terminal(ctx, store, card.id)
    return card_to_dict(card)


def tool_card_unblock(
    ctx: ServerContext, card_id: str, to: str = "inbox"
) -> dict[str, Any]:
    ctx.guard_card_write(card_id)
    store = ctx.store()
    target = _coerce_status(to)
    try:
        previous_status = store.get_card(card_id).status
        store.update_card(card_id, blocked_reason=None)
        card = store.move_card(
            card_id, target, f"Unblocked to {target.value}"
        )
    except KeyError as exc:
        raise ValueError(f"card {card_id} not found") from exc
    if card.status == CardStatus.DONE and previous_status != CardStatus.DONE:
        from .orchestrator import advance_inbox_dependents

        advance_inbox_dependents(store, card.id)
    _detach_worktree_after_terminal(ctx, store, card.id)
    return card_to_dict(card)


def tool_events_tail(
    ctx: ServerContext,
    limit: int = 50,
    card_id: str | None = None,
    role: str | None = None,
    execution_only: bool = False,
) -> list[dict[str, Any]]:
    store = ctx.store()
    role_enum = _coerce_role(role)
    if execution_only or role_enum is not None:
        events = store.list_execution_events(
            card_id=card_id, role=role_enum, limit=limit
        )
    elif card_id is not None:
        events = list(store.events_for_card(card_id))
        # Match ``list_events`` / ``_tail``: ``<=0`` → empty, positive → tail.
        if limit is not None:
            events = events[-limit:] if limit > 0 else []
    else:
        events = store.list_events(limit=limit)
    return [event_to_dict(e) for e in events]


def _build_orchestrator(ctx: ServerContext) -> KanbanOrchestrator:
    return KanbanOrchestrator(
        store=ctx.store(),
        executor=_build_executor(ctx.executor_name, ctx.board_dir),
        worktree_mgr=_resolve_worktree_mgr(ctx),
    )


def tool_tick(ctx: ServerContext) -> dict[str, Any]:
    ctx.guard_write()
    orch = _build_orchestrator(ctx)
    card = orch.tick()
    if card is None:
        return {"idle": True}
    return {"idle": False, "card": card_to_dict(card)}


def tool_run(ctx: ServerContext, max_steps: int = 100) -> dict[str, Any]:
    ctx.guard_write()
    orch = _build_orchestrator(ctx)
    processed = orch.run_until_idle(max_steps=max_steps)
    return {"steps": len(processed)}


# ---------- FastMCP wiring ----------


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
            "(inbox/ready/doing/review/verify/done/blocked)."
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


# ---------- argv ----------


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
        from .cli import _find_git_root_optional

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
