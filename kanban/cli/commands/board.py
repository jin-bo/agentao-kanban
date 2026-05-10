"""Board read + status-mutation commands.

Covers ``kanban list / show / result / move / block / unblock / events``
plus the registration of the top-level ``move`` / ``block`` / ``unblock``
parsers.

These commands form the everyday "look at and nudge cards" surface;
``runtime``/``daemon``/``card`` parsers are split into their own modules.
"""

from __future__ import annotations

import argparse
import json as _json
import sys

from ...models import AgentRole, CardStatus
from ...orchestrator import advance_inbox_dependents
from ..helpers import (
    _apply_limit,
    _detach_worktree_after_terminal_cli,
    _make_store,
    _non_negative_int,
    _require_card_writable,
    _resolve_card_id,
)
from ..rendering import (
    _event_to_json,
    _format_event_line,
    _format_result_block,
    _render_card,
    _show_extras,
    _summarize_card_result,
)


def cmd_list(args: argparse.Namespace) -> int:
    store = _make_store(args)
    snapshot = store.board_snapshot()
    if not snapshot:
        print("(empty board)")
        return 0
    for status in CardStatus:
        titles = snapshot.get(status.value, [])
        if not titles:
            continue
        print(f"{status.value}:")
        for card in store.list_by_status(status):
            print(f"  - {card.id[:8]}  {card.title}  (priority={card.priority.name})")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    store = _make_store(args)
    args.card_id = _resolve_card_id(store, args.card_id)
    try:
        card = store.get_card(args.card_id)
    except KeyError:
        print(f"No card with id {args.card_id}", file=sys.stderr)
        return 1
    extras = _show_extras(args, store, card)
    rendered = _render_card(card, as_json=getattr(args, "as_json", False), extras=extras)
    # yaml.safe_dump already ends with "\n"; json.dumps does not. Use
    # print's implicit newline for JSON, raw write for YAML so we don't
    # emit a blank trailing line.
    if getattr(args, "as_json", False):
        print(rendered)
    else:
        sys.stdout.write(rendered)
    return 0


def cmd_result(args: argparse.Namespace) -> int:
    store = _make_store(args)
    args.card_id = _resolve_card_id(store, args.card_id)
    try:
        card = store.get_card(args.card_id)
    except KeyError:
        print(f"No card with id {args.card_id}", file=sys.stderr)
        return 1
    summary = _summarize_card_result(args, store, card)
    if getattr(args, "as_json", False):
        print(_json.dumps(summary, ensure_ascii=False))
        return 0
    print(f"Card {card.id[:8]}  {card.title}")
    sys.stdout.write(_format_result_block(summary))
    return 0


def cmd_move(args: argparse.Namespace) -> int:
    store = _make_store(args)
    args.card_id = _resolve_card_id(store, args.card_id)
    _require_card_writable(args, args.card_id)
    try:
        previous_status = store.get_card(args.card_id).status
        card = store.move_card(args.card_id, CardStatus(args.status), "Manual move via CLI")
    except KeyError:
        print(f"No card with id {args.card_id}", file=sys.stderr)
        return 1
    if card.status == CardStatus.DONE and previous_status != CardStatus.DONE:
        advance_inbox_dependents(store, card.id)
    _detach_worktree_after_terminal_cli(args, store, card.id)
    print(f"Moved {card.id} to {card.status.value}")
    return 0


def cmd_block(args: argparse.Namespace) -> int:
    store = _make_store(args)
    args.card_id = _resolve_card_id(store, args.card_id)
    _require_card_writable(args, args.card_id)
    try:
        store.update_card(args.card_id, blocked_reason=args.reason)
        card = store.move_card(args.card_id, CardStatus.BLOCKED, f"Blocked: {args.reason}")
    except KeyError:
        print(f"No card with id {args.card_id}", file=sys.stderr)
        return 1
    _detach_worktree_after_terminal_cli(args, store, card.id)
    print(f"Blocked {card.id}: {args.reason}")
    return 0


def cmd_unblock(args: argparse.Namespace) -> int:
    store = _make_store(args)
    args.card_id = _resolve_card_id(store, args.card_id)
    _require_card_writable(args, args.card_id)
    try:
        target = CardStatus(args.target)
        previous_status = store.get_card(args.card_id).status
        store.update_card(args.card_id, blocked_reason=None)
        card = store.move_card(args.card_id, target, f"Unblocked to {target.value}")
    except KeyError:
        print(f"No card with id {args.card_id}", file=sys.stderr)
        return 1
    if card.status == CardStatus.DONE and previous_status != CardStatus.DONE:
        advance_inbox_dependents(store, card.id)
    # Unblocking to DONE is the only terminal target here; INBOX/READY/etc.
    # leave the worktree attached so the next worker dispatch can resume.
    _detach_worktree_after_terminal_cli(args, store, card.id)
    print(f"Unblocked {card.id} to {card.status.value}")
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    store = _make_store(args)
    if args.card_id is not None:
        args.card_id = _resolve_card_id(store, args.card_id)
    if args.role is not None:
        role = AgentRole(args.role)
        records = store.list_execution_events(
            card_id=args.card_id, role=role, limit=args.limit
        )
    elif args.card_id is not None:
        records = list(store.events_for_card(args.card_id))
        records = _apply_limit(records, args.limit)
    else:
        records = store.list_events(limit=args.limit)

    if args.as_json:
        for e in records:
            print(_json.dumps(_event_to_json(e), ensure_ascii=False))
    else:
        if not records:
            print("(no events)")
            return 0
        for e in records:
            print(_format_event_line(e))
    return 0


def register_board_commands(sub) -> None:
    """Register ``list / show / result / move / block / unblock / events``."""
    sub.add_parser("list", help="List cards grouped by status")

    show = sub.add_parser("show", help="Show a single card")
    show.add_argument("card_id")
    show.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit a single-line JSON object (for scripting).",
    )

    result = sub.add_parser(
        "result",
        help=(
            "Show a card's result: status, summary, branch, artifacts, transcripts. "
            "Use this instead of `worktree list` to find what a worker produced."
        ),
    )
    result.add_argument("card_id")
    result.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit a single-line JSON object (for scripting).",
    )

    move = sub.add_parser("move", help="Move a card to a status")
    move.add_argument("card_id")
    move.add_argument("status", choices=[s.value for s in CardStatus])

    block = sub.add_parser("block", help="Move a card to BLOCKED with a reason")
    block.add_argument("card_id")
    block.add_argument("reason")

    unblock = sub.add_parser("unblock", help="Move a blocked card back (default: inbox)")
    unblock.add_argument("card_id")
    unblock.add_argument(
        "--to",
        dest="target",
        choices=[s.value for s in CardStatus],
        default=CardStatus.INBOX.value,
    )

    events = sub.add_parser("events", help="Inspect events.log")
    events.add_argument("card_id", nargs="?", help="Filter to one card")
    events.add_argument(
        "--role",
        choices=[r.value for r in AgentRole],
        help="Filter to execution events for this role (hides plain events)",
    )
    events.add_argument(
        "--limit",
        type=_non_negative_int,
        default=50,
        help="Show the last N events (default 50; 0 = none)",
    )
    events.add_argument("--json", dest="as_json", action="store_true", help="Emit one JSON record per line")
