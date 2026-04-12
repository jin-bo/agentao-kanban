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
from .executors import CardExecutor, MockAgentaoExecutor
from .models import Card, CardPriority, CardStatus
from .orchestrator import KanbanOrchestrator
from .store_markdown import MarkdownBoardStore

DEFAULT_BOARD = Path("workspace/board")


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


def _make_orchestrator(args: argparse.Namespace) -> tuple[MarkdownBoardStore, KanbanOrchestrator]:
    store = MarkdownBoardStore(args.board)
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


def cmd_card_add(args: argparse.Namespace) -> int:
    _require_writable(args)
    store, orchestrator = _make_orchestrator(args)
    card = orchestrator.create_card(
        title=args.title,
        goal=args.goal,
        priority=CardPriority[args.priority],
        acceptance_criteria=list(args.acceptance),
        depends_on=list(args.depends),
    )
    print(f"Created card {card.id}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    store, _ = _make_orchestrator(args)
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
    store, _ = _make_orchestrator(args)
    try:
        card = store.get_card(args.card_id)
    except KeyError:
        print(f"No card with id {args.card_id}", file=sys.stderr)
        return 1
    _print_card(card)
    return 0


def cmd_move(args: argparse.Namespace) -> int:
    _require_writable(args)
    store, _ = _make_orchestrator(args)
    try:
        card = store.move_card(args.card_id, CardStatus(args.status), "Manual move via CLI")
    except KeyError:
        print(f"No card with id {args.card_id}", file=sys.stderr)
        return 1
    print(f"Moved {card.id} to {card.status.value}")
    return 0


def cmd_block(args: argparse.Namespace) -> int:
    _require_writable(args)
    _, orchestrator = _make_orchestrator(args)
    try:
        card = orchestrator.block(args.card_id, args.reason)
    except KeyError:
        print(f"No card with id {args.card_id}", file=sys.stderr)
        return 1
    print(f"Blocked {card.id}: {args.reason}")
    return 0


def cmd_unblock(args: argparse.Namespace) -> int:
    _require_writable(args)
    _, orchestrator = _make_orchestrator(args)
    try:
        card = orchestrator.unblock(args.card_id, CardStatus(args.target))
    except KeyError:
        print(f"No card with id {args.card_id}", file=sys.stderr)
        return 1
    print(f"Unblocked {card.id} to {card.status.value}")
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
        parser.error(f"Unknown card subcommand: {args.card_command}")
    dispatch = {
        "list": cmd_list,
        "show": cmd_show,
        "move": cmd_move,
        "block": cmd_block,
        "unblock": cmd_unblock,
        "tick": cmd_tick,
        "run": cmd_run,
        "daemon": cmd_daemon,
    }
    return dispatch[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
