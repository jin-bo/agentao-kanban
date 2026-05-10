"""Miscellaneous one-off commands: doctor, demo, web, mcp install.

These don't share a clean theme — they're commands that don't fit into
the card / board / runtime / daemon / worktree / profiles groups.
"""

from __future__ import annotations

import argparse
import json as _json
import subprocess
import sys
from pathlib import Path

from ...executors import MockAgentaoExecutor
from ...init import MARKER_DIR
from ...models import CardStatus
from ...orchestrator import KanbanOrchestrator
from ..helpers import (
    _make_store,
    _project_root_for,
    _require_writable,
    _resolve_worktree_mgr,
)


def _doctor_project_root(board: Path) -> Path:
    """Project root for environment checks.

    Prefer a ``.kanban/`` ancestor of ``cwd`` so a user running ``doctor``
    from inside their workspace gets diagnostics about *their* setup, not
    a marker that happens to sit above ``--board``. Fall back to the
    board-derived root, then cwd as last resort.
    """
    cwd = Path.cwd().resolve()
    for candidate in (cwd, *cwd.parents):
        if (candidate / MARKER_DIR).is_dir():
            return candidate
    derived = _project_root_for(board)
    if (derived / MARKER_DIR).is_dir():
        return derived
    return cwd


def cmd_doctor(args: argparse.Namespace) -> int:
    from ... import doctor as _doctor

    project_root = _doctor_project_root(args.board)
    board_path = Path(args.board).resolve()

    env_checks = _doctor.run_environment(project_root, board_path)
    # Card-level checks need a readable board. Skip them when the board
    # directory is missing — `_make_store` would create it as a side
    # effect of the first read, masking the env finding.
    card_checks: list = []
    if board_path.is_dir():
        store = _make_store(args)
        card_checks = list(_doctor.run(store).checks)

    report = _doctor.DoctorReport(checks=list(env_checks) + card_checks)

    fixes_applied: list[tuple[str, str]] = []
    if args.fix:
        remaining: list = []
        for check in report.checks:
            if check.fix is None:
                remaining.append(check)
                continue
            try:
                description = check.fix()
            except Exception as exc:  # noqa: BLE001 — surface to operator, keep going
                description = f"fix raised {type(exc).__name__}: {exc}"
                remaining.append(check)
            else:
                fixes_applied.append((check.rule, description))
        report = _doctor.DoctorReport(checks=remaining)

    if args.as_json:
        payload = {
            "checks": [
                {
                    "severity": c.severity,
                    "rule": c.rule,
                    "card_id": c.card_id,
                    "message": c.message,
                    "fixable": c.fix is not None,
                }
                for c in report.checks
            ],
            "fixes_applied": [
                {"rule": rule, "description": desc} for rule, desc in fixes_applied
            ],
        }
        print(_json.dumps(payload, ensure_ascii=False))
    else:
        if fixes_applied:
            print("Applied fixes:")
            for rule, desc in fixes_applied:
                print(f"  - {rule}: {desc}")
        if not report.checks:
            if not fixes_applied:
                print("Board is healthy.")
            else:
                print("Remaining issues: none.")
        else:
            for c in report.checks:
                hint = " (fixable: rerun with --fix)" if c.fix is not None and not args.fix else ""
                # Environment findings have no card id — drop the column so
                # the output reads cleanly instead of "[warning] rule   message".
                cid = f"  {c.card_id[:8]}" if c.card_id else ""
                print(f"[{c.severity}] {c.rule}{cid}  {c.message}{hint}")
    return report.exit_code()


def _mcp_install_args(name: str, board: Path) -> tuple[list[str], list[str]]:
    """Render the (program, argv) pair to register kanban-mcp with a client.

    Picks the launcher based on whether we're running from a source
    checkout (parent contains ``pyproject.toml``) or an installed
    package. Source checkouts use ``uv run --project <repo>`` so the
    installed agentao etc. resolve against the repo's lockfile;
    installed packages use ``uvx --from agentao-kanban kanban-mcp``,
    which works regardless of how the user's shell PATH is set up.

    Always emits an absolute board path: MCP clients launch the server
    later from their own cwd, so a relative path captured here would
    silently point at a different (often empty) board on the client side.
    """
    board_abs = Path(board).resolve()
    # Walk up from this file to the directory above the ``kanban`` package
    # (where pyproject.toml lives in a source checkout).
    project = Path(__file__).resolve().parents[3]
    if (project / "pyproject.toml").is_file():
        server = [
            "uv", "run", "--project", str(project),
            "kanban-mcp", "--board", str(board_abs),
        ]
    else:
        server = [
            "uvx", "--from", "agentao-kanban",
            "kanban-mcp", "--board", str(board_abs),
        ]
    claude = ["claude", "mcp", "add", name, "--"] + server
    codex = ["codex", "mcp", "add", name, "--"] + server
    return claude, codex


def cmd_mcp_install(args: argparse.Namespace) -> int:
    """Print or run the MCP registration command for a chosen client."""
    import shlex

    claude_argv, codex_argv = _mcp_install_args(args.name, args.board)
    targets = {
        "claude": claude_argv,
        "codex": codex_argv,
    }

    if args.client == "print":
        if args.run:
            print(
                "--run requires --client claude or --client codex.",
                file=sys.stderr,
            )
            return 2
        print("# Claude Code")
        print(shlex.join(claude_argv))
        print()
        print("# Codex CLI")
        print(shlex.join(codex_argv))
        return 0

    argv = targets[args.client]
    if not args.run:
        print(shlex.join(argv))
        return 0

    try:
        proc = subprocess.run(argv, check=False)
    except FileNotFoundError:
        print(
            f"Could not find the `{argv[0]}` CLI on PATH. Install it first, "
            f"or rerun without --run to copy the command yourself.",
            file=sys.stderr,
        )
        return 1
    return proc.returncode


def cmd_demo(args: argparse.Namespace) -> int:
    """Seed the board with example cards and (by default) run them.

    Refuses on a non-empty board *unless* the existing cards are exactly
    the demo set (e.g. left over from ``kanban init --demo``), so the
    documented init→demo flow round-trips cleanly without a second
    ``rm -rf``.
    """
    _require_writable(args)
    from ...demo import is_demo_only, seed_demo_board

    store = _make_store(args)
    existing = store.list_cards()
    if existing:
        if not is_demo_only(existing):
            print(
                f"Board already has {len(existing)} non-demo card(s); refusing to seed. "
                f"Use a different --board or remove the existing cards first.",
                file=sys.stderr,
            )
            return 2
        print(
            f"Board already seeded ({len(existing)} demo card(s)); "
            f"skipping seed and advancing what's there."
        )
    else:
        result = seed_demo_board(store)
        print(f"Seeded {result.created} demo cards onto {args.board}.")

    if args.no_run:
        print("Run `kanban run` to advance them, or `kanban web` to browse.")
        return 0

    # Use the mock executor regardless of --executor — demo is supposed
    # to run offline. Drive a bounded number of ticks rather than the
    # default cap so the output stays predictable for first-time users.
    orchestrator = KanbanOrchestrator(
        store=store,
        executor=MockAgentaoExecutor(),
        worktree_mgr=_resolve_worktree_mgr(args),
    )
    orchestrator.run_until_idle(max_steps=args.max_steps)

    snapshot = store.board_snapshot()
    print()
    print("Final state:")
    for status in CardStatus:
        titles = snapshot.get(status.value, [])
        if titles:
            print(f"  {status.value:<8} {len(titles)} card(s)")
    print()
    print("Try next:")
    print(f"  uv run kanban --board {args.board} list")
    print(f"  uv run kanban --board {args.board} web         # browse the result")
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    # Read-only endpoint: no ``_require_writable`` check so the daemon can
    # hold ``.daemon.lock`` while the UI observes it. The server mounts a
    # fresh store per request, which picks up daemon/CLI writes on the
    # next poll.
    from ...web import main as web_main

    return web_main(
        args.board,
        host=args.host,
        port=args.port,
        poll_interval_ms=args.poll_interval_ms,
        enable_writes=args.enable_writes,
        allow_remote_writes=args.allow_remote_writes,
    )


def register_doctor_command(sub) -> None:
    doctor = sub.add_parser("doctor", help="Run board + environment checks")
    doctor.add_argument("--json", dest="as_json", action="store_true", help="Emit machine-readable records")
    doctor.add_argument(
        "--fix",
        action="store_true",
        help=(
            "Apply remediation for fixable environment issues "
            "(stale/malformed `.daemon.lock`, missing board dir, missing "
            "or malformed `.kanban/config.yaml`). Card-level findings are "
            "never auto-fixed."
        ),
    )


def register_demo_command(sub) -> None:
    demo_p = sub.add_parser(
        "demo",
        help="Seed the board with example cards and run a few orchestrator steps.",
        description=(
            "Seed 4 example cards on the file-backed board and (by default) run "
            "the mock orchestrator until idle so you can see the full pipeline. "
            "Refuses to seed when the board already has cards."
        ),
    )
    demo_p.add_argument(
        "--no-run",
        action="store_true",
        dest="no_run",
        help="Just seed the cards; don't run the orchestrator.",
    )
    demo_p.add_argument(
        "--max-steps",
        type=int,
        default=20,
        dest="max_steps",
        help="Cap orchestrator iterations when seeding into an empty board (default 20).",
    )


def register_mcp_command(sub) -> None:
    mcp_p = sub.add_parser(
        "mcp",
        help="Helpers for registering kanban-mcp with MCP clients.",
        description=(
            "The kanban MCP server itself is the separate `kanban-mcp` entry "
            "point. This subcommand only emits / executes the registration "
            "command line for popular clients."
        ),
    )
    mcp_sub = mcp_p.add_subparsers(dest="mcp_command", required=True)

    mi = mcp_sub.add_parser(
        "install",
        help="Print (or run) the registration command for a client.",
    )
    mi.add_argument(
        "--client",
        choices=["claude", "codex", "print"],
        default="print",
        help=(
            "Which client to target. `print` (default) emits a copy/paste "
            "snippet for both. `claude` and `codex` emit a single command "
            "line; combine with --run to execute it."
        ),
    )
    mi.add_argument(
        "--name",
        default="kanban",
        help="Server name registered with the client (default: kanban).",
    )
    mi.add_argument(
        "--run",
        action="store_true",
        help="Execute the rendered command instead of just printing it.",
    )


def register_web_command(sub) -> None:
    web_p = sub.add_parser(
        "web",
        help="Run the read-only web board (no writes; polls the board dir).",
    )
    web_p.add_argument("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1)")
    web_p.add_argument("--port", type=int, default=8000, help="Bind port (default 8000)")
    web_p.add_argument(
        "--poll-interval-ms",
        type=int,
        default=5000,
        dest="poll_interval_ms",
        help="Frontend poll interval in ms (default 5000)",
    )
    web_p.add_argument(
        "--enable-writes",
        action="store_true",
        dest="enable_writes",
        help=(
            "Expose POST /api/cards so the browser can create new INBOX "
            "cards. Off by default; the rest of the surface stays read-only."
        ),
    )
    web_p.add_argument(
        "--allow-remote-writes",
        action="store_true",
        dest="allow_remote_writes",
        help=(
            "Permit --enable-writes on non-loopback bind hosts. Required "
            "when fronting the server with a reverse proxy or firewall."
        ),
    )
