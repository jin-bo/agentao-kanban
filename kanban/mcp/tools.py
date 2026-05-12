from __future__ import annotations

from pathlib import Path
from typing import Any

from ..models import AgentRole, Card, CardPriority, CardStatus, coerce_card_status
from ..operations import (
    TransitionResult,
    transition_block,
    transition_move,
    transition_unblock,
)
from ..orchestrator import KanbanOrchestrator
from ..store_markdown import MarkdownBoardStore
from .context import (
    ServerContext,
    _resolve_worktree_mgr,
    _terminal_worktree_mgr,
)
from .serializers import card_to_dict, event_to_dict

_VALID_PRIORITIES = tuple(p.name for p in CardPriority)
_VALID_STATUSES = tuple(s.value for s in CardStatus)
_VALID_ROLES = tuple(r.value for r in AgentRole)


def _coerce_priority(value: str) -> CardPriority:
    try:
        return CardPriority[value.upper()]
    except KeyError as exc:
        raise ValueError(
            f"priority must be one of {_VALID_PRIORITIES}, got {value!r}"
        ) from exc


def _coerce_status(value: str) -> CardStatus:
    try:
        return coerce_card_status(value)
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
        from ..executors import MockAgentaoExecutor

        return MockAgentaoExecutor()
    if name in {"agentao", "multi-backend"}:
        # Reuse the CLI factory so project-root inference, `.agentao/`
        # resolution, and config loading all stay identical to the
        # command-line path.
        from ..cli import _build_executor as _cli_build

        return _cli_build(name, board=board_dir)
    raise ValueError(f"Unknown executor: {name}")


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


def _transition_response(result: TransitionResult) -> dict[str, Any]:
    """Serialize a card transition, attaching ``warnings`` only when present.

    Keeps the historical "tool returns the card dict" shape; the optional
    ``warnings`` key is additive and non-breaking for existing clients.
    """
    payload = card_to_dict(result.card)
    if result.warnings:
        payload["warnings"] = list(result.warnings)
    return payload


def tool_card_move(
    ctx: ServerContext, card_id: str, status: str
) -> dict[str, Any]:
    ctx.guard_card_write(card_id)
    store = ctx.store()
    try:
        result = transition_move(
            store,
            _terminal_worktree_mgr(ctx),
            card_id,
            status,
            note="Manual move via MCP",
        )
    except KeyError as exc:
        raise ValueError(f"card {card_id} not found") from exc
    return _transition_response(result)


def tool_card_block(
    ctx: ServerContext, card_id: str, reason: str
) -> dict[str, Any]:
    ctx.guard_card_write(card_id)
    store = ctx.store()
    try:
        result = transition_block(
            store, _terminal_worktree_mgr(ctx), card_id, reason
        )
    except KeyError as exc:
        raise ValueError(f"card {card_id} not found") from exc
    return _transition_response(result)


def tool_card_unblock(
    ctx: ServerContext, card_id: str, to: str = "inbox"
) -> dict[str, Any]:
    ctx.guard_card_write(card_id)
    store = ctx.store()
    try:
        result = transition_unblock(
            store, _terminal_worktree_mgr(ctx), card_id, to
        )
    except KeyError as exc:
        raise ValueError(f"card {card_id} not found") from exc
    return _transition_response(result)


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
