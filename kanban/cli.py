from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .daemon import (
    DaemonConfig,
    DaemonLockError,
    KanbanDaemon,
    assert_no_daemon,
    daemon_lock,
    detach_to_background,
)
import json as _json

from .executors import CardExecutor, MockAgentaoExecutor
from .models import (
    CONTEXT_REF_KINDS,
    AgentRole,
    Card,
    CardEvent,
    CardPriority,
    CardStatus,
    ContextRef,
)
from .orchestrator import KanbanOrchestrator
from .store_markdown import MarkdownBoardStore

DEFAULT_BOARD = Path("workspace/board")


def _apply_limit(items: list, limit: int | None) -> list:
    """Mirror the store's tail semantics: None=all, <=0=none."""
    if limit is None:
        return items
    if limit <= 0:
        return []
    return items[-limit:]


def _non_negative_int(value: str) -> int:
    n = int(value)
    if n < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0, got {n}")
    return n

# Statuses an operator may force via `card edit --set-status`. doing/review/verify
# are excluded because they have an expected owner_role and would desync the
# orchestrator — use `requeue` for recovery paths.
_OPERATOR_STATUSES = (
    CardStatus.INBOX,
    CardStatus.READY,
    CardStatus.BLOCKED,
    CardStatus.DONE,
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kanban", description="Kanban board CLI")
    p.add_argument(
        "--board",
        type=Path,
        default=DEFAULT_BOARD,
        help=f"Board directory (default: {DEFAULT_BOARD})",
    )
    p.add_argument(
        "--executor",
        choices=["mock", "agentao"],
        default="mock",
        help="Executor backend (default: mock). `agentao` requires the agentao package.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    card = sub.add_parser("card", help="Card operations")
    card_sub = card.add_subparsers(dest="card_command", required=True)

    add = card_sub.add_parser("add", help="Create a new card")
    add.add_argument("--title", required=True)
    add.add_argument("--goal", required=True)
    add.add_argument(
        "--priority",
        choices=[p.name for p in CardPriority],
        default=CardPriority.MEDIUM.name,
    )
    add.add_argument("--acceptance", action="append", default=[], help="Acceptance criterion (repeatable)")
    add.add_argument("--depends", action="append", default=[], help="Card id this card depends on (repeatable)")

    edit = card_sub.add_parser("edit", help="Edit an existing card")
    edit.add_argument("card_id")
    edit.add_argument("--title")
    edit.add_argument("--goal")
    edit.add_argument(
        "--priority",
        choices=[p.name for p in CardPriority],
        help="New priority",
    )
    edit.add_argument(
        "--set-status",
        dest="set_status",
        choices=[s.value for s in _OPERATOR_STATUSES],
        help="Operator override; disallowed for doing/review/verify (use requeue instead).",
    )
    blocked_group = edit.add_mutually_exclusive_group()
    blocked_group.add_argument(
        "--blocked-reason",
        dest="blocked_reason",
        help="Set or update the blocked_reason field.",
    )
    blocked_group.add_argument(
        "--clear-blocked-reason",
        dest="clear_blocked_reason",
        action="store_true",
        help="Clear blocked_reason.",
    )

    context = card_sub.add_parser("context", help="Manage card context_refs")
    context_sub = context.add_subparsers(dest="context_command", required=True)

    ctx_list = context_sub.add_parser("list", help="List context refs on a card")
    ctx_list.add_argument("card_id")

    ctx_add = context_sub.add_parser("add", help="Add or upsert a context ref by path")
    ctx_add.add_argument("card_id")
    ctx_add.add_argument("--path", required=True)
    ctx_add.add_argument(
        "--kind", choices=list(CONTEXT_REF_KINDS), default="optional"
    )
    ctx_add.add_argument("--note", default="")

    ctx_rm = context_sub.add_parser("rm", help="Remove a context ref by path")
    ctx_rm.add_argument("card_id")
    ctx_rm.add_argument("--path", required=True)

    acc = card_sub.add_parser("acceptance", help="Manage acceptance_criteria")
    acc_sub = acc.add_subparsers(dest="acceptance_command", required=True)

    acc_list = acc_sub.add_parser("list", help="List acceptance criteria")
    acc_list.add_argument("card_id")

    acc_add = acc_sub.add_parser("add", help="Append an acceptance criterion")
    acc_add.add_argument("card_id")
    acc_add.add_argument("--item", required=True)

    acc_rm = acc_sub.add_parser("rm", help="Remove a criterion by 1-based index")
    acc_rm.add_argument("card_id")
    acc_rm.add_argument("--index", type=int, required=True)

    acc_clear = acc_sub.add_parser("clear", help="Clear all criteria")
    acc_clear.add_argument("card_id")

    sub.add_parser("list", help="List cards grouped by status")

    show = sub.add_parser("show", help="Show a single card")
    show.add_argument("card_id")

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

    doctor = sub.add_parser("doctor", help="Run board integrity checks")
    doctor.add_argument("--json", dest="as_json", action="store_true", help="Emit machine-readable records")

    traces = sub.add_parser("traces", help="List retained raw agent transcripts")
    traces.add_argument("card_id")
    traces.add_argument(
        "--role",
        choices=[r.value for r in AgentRole],
        help="Only transcripts from this role",
    )
    traces.add_argument("--latest", action="store_true", help="Only the most recent transcript")

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

    requeue = sub.add_parser("requeue", help="Return a (usually blocked) card back to flow")
    requeue.add_argument("card_id")
    requeue.add_argument(
        "--to",
        dest="target",
        choices=["inbox", "ready"],
        default="inbox",
        help="Target status (default: inbox)",
    )
    requeue.add_argument("--note", default="", help="Recovery note appended to history")

    sub.add_parser("tick", help="Run a single orchestrator step")
    run = sub.add_parser("run", help="Run orchestrator until idle")
    run.add_argument("--max-steps", type=int, default=100)

    daemon = sub.add_parser("daemon", help="Run the dispatcher loop (foreground by default)")
    daemon.add_argument("--detach", action="store_true", help="Fork into the background")
    daemon.add_argument("--once", action="store_true", help="Run a single tick and exit")
    daemon.add_argument(
        "--poll-interval", type=float, default=2.0, help="Idle sleep in seconds (default 2.0)"
    )
    daemon.add_argument("--verbose", action="store_true", help="Enable DEBUG logging")

    p.add_argument(
        "--force",
        action="store_true",
        help="Mutate the board even if a daemon holds the lock (for recovery only).",
    )

    return p


def _build_executor(name: str) -> CardExecutor:
    if name == "mock":
        return MockAgentaoExecutor()
    if name == "agentao":
        try:
            from .executors.agentao_multi import AgentaoMultiAgentExecutor
        except ImportError as exc:
            raise SystemExit(
                "agentao package is not installed. Run `uv add --editable ../agentao` first."
            ) from exc
        return AgentaoMultiAgentExecutor()
    raise ValueError(f"Unknown executor: {name}")


def _make_store(args: argparse.Namespace) -> MarkdownBoardStore:
    """Open the board store without constructing an executor.

    Use this for read-only commands (`list`, `show`, `events`, `traces`,
    `doctor`) and for write commands that do not need an agent runner
    (`card add/edit/context/acceptance`, `block`, `unblock`, `requeue`,
    `move`). Building the executor can import the optional `agentao`
    package, which these commands don't need.
    """
    return MarkdownBoardStore(args.board)


def _make_orchestrator(args: argparse.Namespace) -> tuple[MarkdownBoardStore, KanbanOrchestrator]:
    store = _make_store(args)
    orchestrator = KanbanOrchestrator(store=store, executor=_build_executor(args.executor))
    return store, orchestrator


def _require_writable(args: argparse.Namespace) -> None:
    """Refuse to mutate the board while a live daemon holds the lock."""
    if getattr(args, "force", False):
        return
    try:
        assert_no_daemon(args.board)
    except DaemonLockError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)


def _print_card(card: Card) -> None:
    print(f"{card.id}  [{card.status.value}]  {card.title}  (priority={card.priority.name})")
    if card.owner_role is not None:
        print(f"  owner: {card.owner_role.value}")
    if card.blocked_reason:
        print(f"  blocked: {card.blocked_reason}")
    print(f"  goal: {card.goal}")
    if card.depends_on:
        print("  depends_on:")
        for dep in card.depends_on:
            print(f"    - {dep}")
    if card.acceptance_criteria:
        print("  acceptance_criteria:")
        for item in card.acceptance_criteria:
            print(f"    - {item}")
    if card.outputs:
        print("  outputs:")
        for key, value in card.outputs.items():
            print(f"    {key}: {value}")
    if card.history:
        print("  history:")
        for item in card.history:
            print(f"    - {item}")


def cmd_card_edit(args: argparse.Namespace) -> int:
    _require_writable(args)
    store = _make_store(args)
    try:
        card = store.get_card(args.card_id)
    except KeyError:
        print(f"No card with id {args.card_id}", file=sys.stderr)
        return 1

    new_status: CardStatus | None = None
    if args.set_status is not None:
        new_status = CardStatus(args.set_status)
        if new_status == CardStatus.BLOCKED and not args.blocked_reason:
            print(
                "--set-status blocked requires --blocked-reason in the same call.",
                file=sys.stderr,
            )
            return 2

    # --blocked-reason is only valid when the card actually is (or in this
    # same call becomes) BLOCKED. Writing a live reason on a non-blocked
    # card leaves contradictory state: `show` reports a block reason while
    # the dispatcher keeps processing the card.
    if args.blocked_reason is not None:
        effective_status = new_status if new_status is not None else card.status
        if effective_status != CardStatus.BLOCKED:
            print(
                "--blocked-reason is only valid when the card is or is being moved to blocked "
                f"(current={card.status.value}"
                + (f", --set-status {new_status.value}" if new_status is not None else "")
                + ").",
                file=sys.stderr,
            )
            return 2

    scalar_updates: dict[str, object] = {}
    if args.title is not None:
        scalar_updates["title"] = args.title
    if args.goal is not None:
        scalar_updates["goal"] = args.goal
    if args.priority is not None:
        scalar_updates["priority"] = CardPriority[args.priority]

    blocked_changed = False
    if args.blocked_reason is not None:
        scalar_updates["blocked_reason"] = args.blocked_reason
        blocked_changed = True
    elif args.clear_blocked_reason:
        scalar_updates["blocked_reason"] = None
        blocked_changed = True

    if not scalar_updates and new_status is None:
        print("Nothing to edit. Pass at least one flag.", file=sys.stderr)
        return 2

    if scalar_updates:
        store.update_card(card.id, **scalar_updates)
        fresh = store.get_card(card.id)
        notes: list[str] = []
        if args.title or args.goal or args.priority:
            notes.append("Manual edit via CLI")
        if blocked_changed:
            notes.append(
                "Blocked reason cleared via CLI"
                if args.clear_blocked_reason
                else "Blocked reason updated via CLI"
            )
        for note in notes:
            fresh.add_history(note, role="system")
            store.append_event(fresh.id, note)
        if notes and new_status is None:
            # Flush the in-memory history mutation to disk. The move_card
            # path below would do it, but we may not be taking it.
            store.update_card(fresh.id)

    if new_status is not None:
        # --set-status always resets owner_role; operator-forced statuses
        # never carry an implicit agent expectation. It also clears any
        # stale blocked_reason when moving AWAY from BLOCKED — an operator
        # forcing the card back into flow has no business leaving the old
        # block note behind (which would make `show` contradict itself).
        forced_updates: dict[str, object] = {"owner_role": None}
        current = store.get_card(card.id)
        if (
            new_status != CardStatus.BLOCKED
            and current.blocked_reason is not None
            and not blocked_changed
        ):
            forced_updates["blocked_reason"] = None
        store.update_card(card.id, **forced_updates)
        store.move_card(
            card.id,
            new_status,
            f"Status manually set to {new_status.value} via CLI",
        )

    print(f"Edited {card.id}")
    return 0


def cmd_card_context_list(args: argparse.Namespace) -> int:
    store = _make_store(args)
    try:
        card = store.get_card(args.card_id)
    except KeyError:
        print(f"No card with id {args.card_id}", file=sys.stderr)
        return 1
    if not card.context_refs:
        print("(no context refs)")
        return 0
    for ref in card.context_refs:
        suffix = f"  — {ref.note}" if ref.note else ""
        print(f"[{ref.kind}] {ref.path}{suffix}")
    return 0


def cmd_card_context_add(args: argparse.Namespace) -> int:
    _require_writable(args)
    store = _make_store(args)
    try:
        card = store.get_card(args.card_id)
    except KeyError:
        print(f"No card with id {args.card_id}", file=sys.stderr)
        return 1
    try:
        new_ref = ContextRef.coerce({"path": args.path, "kind": args.kind, "note": args.note})
    except (KeyError, TypeError, ValueError) as exc:
        print(f"Invalid context ref: {exc}", file=sys.stderr)
        return 2

    refs = list(card.context_refs)
    existing_idx = next(
        (i for i, r in enumerate(refs) if r.path == new_ref.path), None
    )
    if existing_idx is not None:
        refs[existing_idx] = new_ref
        note = f"Context updated: {new_ref.path} [{new_ref.kind}]"
    else:
        refs.append(new_ref)
        note = f"Context added: {new_ref.path} [{new_ref.kind}]"

    store.update_card(card.id, context_refs=refs)
    fresh = store.get_card(card.id)
    fresh.add_history(note, role="system")
    store.update_card(fresh.id)
    store.append_event(fresh.id, note)
    print(note)
    return 0


def cmd_card_context_rm(args: argparse.Namespace) -> int:
    _require_writable(args)
    store = _make_store(args)
    try:
        card = store.get_card(args.card_id)
    except KeyError:
        print(f"No card with id {args.card_id}", file=sys.stderr)
        return 1

    refs = [r for r in card.context_refs if r.path != args.path]
    if len(refs) == len(card.context_refs):
        print(f"No context ref with path {args.path}", file=sys.stderr)
        return 1

    store.update_card(card.id, context_refs=refs)
    fresh = store.get_card(card.id)
    note = f"Context removed: {args.path}"
    fresh.add_history(note, role="system")
    store.update_card(fresh.id)
    store.append_event(fresh.id, note)
    print(note)
    return 0


def cmd_card_acceptance_list(args: argparse.Namespace) -> int:
    store = _make_store(args)
    try:
        card = store.get_card(args.card_id)
    except KeyError:
        print(f"No card with id {args.card_id}", file=sys.stderr)
        return 1
    if not card.acceptance_criteria:
        print("(no acceptance criteria)")
        return 0
    for i, item in enumerate(card.acceptance_criteria, start=1):
        print(f"{i}. {item}")
    return 0


def cmd_card_acceptance_add(args: argparse.Namespace) -> int:
    _require_writable(args)
    store = _make_store(args)
    try:
        card = store.get_card(args.card_id)
    except KeyError:
        print(f"No card with id {args.card_id}", file=sys.stderr)
        return 1
    criteria = list(card.acceptance_criteria) + [args.item]
    store.update_card(card.id, acceptance_criteria=criteria)
    fresh = store.get_card(card.id)
    note = f"Acceptance criterion added: {args.item}"
    fresh.add_history(note, role="system")
    store.update_card(fresh.id)
    store.append_event(fresh.id, note)
    print(note)
    return 0


def cmd_card_acceptance_rm(args: argparse.Namespace) -> int:
    _require_writable(args)
    store = _make_store(args)
    try:
        card = store.get_card(args.card_id)
    except KeyError:
        print(f"No card with id {args.card_id}", file=sys.stderr)
        return 1
    idx = args.index
    if idx < 1 or idx > len(card.acceptance_criteria):
        print(
            f"Invalid index {idx}; card has {len(card.acceptance_criteria)} criteria.",
            file=sys.stderr,
        )
        return 2
    criteria = list(card.acceptance_criteria)
    removed = criteria.pop(idx - 1)
    store.update_card(card.id, acceptance_criteria=criteria)
    fresh = store.get_card(card.id)
    note = f"Acceptance criterion removed at index {idx}"
    fresh.add_history(note, role="system")
    store.update_card(fresh.id)
    store.append_event(fresh.id, note)
    print(f"{note}: {removed}")
    return 0


def cmd_card_acceptance_clear(args: argparse.Namespace) -> int:
    _require_writable(args)
    store = _make_store(args)
    try:
        card = store.get_card(args.card_id)
    except KeyError:
        print(f"No card with id {args.card_id}", file=sys.stderr)
        return 1
    if not card.acceptance_criteria:
        print("(already empty)")
        return 0
    store.update_card(card.id, acceptance_criteria=[])
    fresh = store.get_card(card.id)
    note = "Acceptance criteria cleared via CLI"
    fresh.add_history(note, role="system")
    store.update_card(fresh.id)
    store.append_event(fresh.id, note)
    print("Cleared acceptance criteria")
    return 0


def cmd_card_add(args: argparse.Namespace) -> int:
    _require_writable(args)
    store = _make_store(args)
    card = store.add_card(
        Card(
            title=args.title,
            goal=args.goal,
            priority=CardPriority[args.priority],
            acceptance_criteria=list(args.acceptance),
            depends_on=list(args.depends),
        )
    )
    print(f"Created card {card.id}")
    return 0


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
    try:
        card = store.get_card(args.card_id)
    except KeyError:
        print(f"No card with id {args.card_id}", file=sys.stderr)
        return 1
    _print_card(card)
    return 0


def cmd_move(args: argparse.Namespace) -> int:
    _require_writable(args)
    store = _make_store(args)
    try:
        card = store.move_card(args.card_id, CardStatus(args.status), "Manual move via CLI")
    except KeyError:
        print(f"No card with id {args.card_id}", file=sys.stderr)
        return 1
    print(f"Moved {card.id} to {card.status.value}")
    return 0


def cmd_block(args: argparse.Namespace) -> int:
    _require_writable(args)
    store = _make_store(args)
    try:
        store.update_card(args.card_id, blocked_reason=args.reason)
        card = store.move_card(args.card_id, CardStatus.BLOCKED, f"Blocked: {args.reason}")
    except KeyError:
        print(f"No card with id {args.card_id}", file=sys.stderr)
        return 1
    print(f"Blocked {card.id}: {args.reason}")
    return 0


def cmd_unblock(args: argparse.Namespace) -> int:
    _require_writable(args)
    store = _make_store(args)
    try:
        target = CardStatus(args.target)
        store.update_card(args.card_id, blocked_reason=None)
        card = store.move_card(args.card_id, target, f"Unblocked to {target.value}")
    except KeyError:
        print(f"No card with id {args.card_id}", file=sys.stderr)
        return 1
    print(f"Unblocked {card.id} to {card.status.value}")
    return 0


def _event_to_json(e: CardEvent) -> dict[str, object]:
    record: dict[str, object] = {
        "at": e.at.isoformat(),
        "card_id": e.card_id,
        "message": e.message,
    }
    if e.is_execution:
        record["role"] = e.role.value if e.role else None
        record["prompt_version"] = e.prompt_version
        record["duration_ms"] = e.duration_ms
        record["attempt"] = e.attempt
        if e.raw_path is not None:
            record["raw_path"] = e.raw_path
    return record


def _format_event_line(e: CardEvent) -> str:
    stamp = e.at.strftime("%Y-%m-%dT%H:%M:%SZ") if e.at.tzinfo else e.at.isoformat()
    role_tag = f"[{e.role.value}]" if e.role else "[system]"
    return f"{stamp}  {e.card_id[:8]}  {role_tag}  {e.message}"


def cmd_events(args: argparse.Namespace) -> int:
    store = _make_store(args)
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


def cmd_doctor(args: argparse.Namespace) -> int:
    from . import doctor as _doctor

    store = _make_store(args)
    report = _doctor.run(store)

    if args.as_json:
        payload = {
            "checks": [
                {
                    "severity": c.severity,
                    "rule": c.rule,
                    "card_id": c.card_id,
                    "message": c.message,
                }
                for c in report.checks
            ]
        }
        print(_json.dumps(payload, ensure_ascii=False))
    else:
        if not report.checks:
            print("Board is healthy.")
        else:
            for c in report.checks:
                print(f"[{c.severity}] {c.rule}  {c.card_id[:8]}  {c.message}")
    return report.exit_code()


def cmd_traces(args: argparse.Namespace) -> int:
    store = _make_store(args)
    try:
        store.get_card(args.card_id)
    except KeyError:
        print(f"No card with id {args.card_id}", file=sys.stderr)
        return 1
    role = AgentRole(args.role) if args.role else None
    traces = store.list_traces(args.card_id, role=role, latest=args.latest)
    if not traces:
        print(f"no traces retained for {args.card_id}")
        return 0
    for t in traces:
        stamp = t.at.strftime("%Y-%m-%dT%H:%M:%SZ")
        print(f"{stamp}  [{t.role.value}]  {t.size:>8}  {t.path}")
    return 0


def cmd_requeue(args: argparse.Namespace) -> int:
    _require_writable(args)
    store = _make_store(args)
    try:
        card = store.get_card(args.card_id)
    except KeyError:
        print(f"No card with id {args.card_id}", file=sys.stderr)
        return 1

    previous_status = card.status
    target = CardStatus(args.target)

    # Clear blocked_reason and reset owner_role — both target statuses
    # (INBOX, READY) expect no pending owner.
    store.update_card(card.id, blocked_reason=None, owner_role=None)

    suffix = f": {args.note}" if args.note else ""
    history_note = (
        f"Requeued from {previous_status.value} to {target.value}{suffix}"
    )
    store.move_card(card.id, target, history_note)
    print(history_note)
    return 0


def cmd_tick(args: argparse.Namespace) -> int:
    _require_writable(args)
    _, orchestrator = _make_orchestrator(args)
    card = orchestrator.tick()
    if card is None:
        print("Board is idle.")
    else:
        print(f"Processed {card.id[:8]}: now {card.status.value}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    _require_writable(args)
    _, orchestrator = _make_orchestrator(args)
    processed = orchestrator.run_until_idle(max_steps=args.max_steps)
    print(f"Processed {len(processed)} step(s); board idle.")
    return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.detach and args.once:
        print("--detach and --once are mutually exclusive", file=sys.stderr)
        return 2

    board_dir = args.board
    if args.detach:
        detach_to_background(board_dir)

    try:
        with daemon_lock(board_dir):
            _, orchestrator = _make_orchestrator(args)
            config = DaemonConfig(
                poll_interval=args.poll_interval,
                max_idle_cycles=1 if args.once else None,
            )
            daemon = KanbanDaemon(orchestrator, config=config)
            daemon.install_signal_handlers()
            if args.once:
                did = daemon.run_once()
                if not did:
                    print("Board is idle.")
                return 0
            return daemon.run()
    except DaemonLockError as exc:
        print(str(exc), file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "card":
        if args.card_command == "add":
            return cmd_card_add(args)
        if args.card_command == "edit":
            return cmd_card_edit(args)
        if args.card_command == "context":
            if args.context_command == "list":
                return cmd_card_context_list(args)
            if args.context_command == "add":
                return cmd_card_context_add(args)
            if args.context_command == "rm":
                return cmd_card_context_rm(args)
            parser.error(f"Unknown context subcommand: {args.context_command}")
        if args.card_command == "acceptance":
            handler = {
                "list": cmd_card_acceptance_list,
                "add": cmd_card_acceptance_add,
                "rm": cmd_card_acceptance_rm,
                "clear": cmd_card_acceptance_clear,
            }.get(args.acceptance_command)
            if handler is None:
                parser.error(f"Unknown acceptance subcommand: {args.acceptance_command}")
            return handler(args)
        parser.error(f"Unknown card subcommand: {args.card_command}")
    dispatch = {
        "list": cmd_list,
        "show": cmd_show,
        "move": cmd_move,
        "block": cmd_block,
        "unblock": cmd_unblock,
        "requeue": cmd_requeue,
        "events": cmd_events,
        "traces": cmd_traces,
        "doctor": cmd_doctor,
        "tick": cmd_tick,
        "run": cmd_run,
        "daemon": cmd_daemon,
    }
    return dispatch[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
