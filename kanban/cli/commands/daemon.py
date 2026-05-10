"""Daemon lifecycle commands: status / stop / logs / run.

Drives the dispatcher loop and provides the operator-facing recovery
surface (``daemon stop`` with PID-reuse guards, malformed-lock cleanup).
The ``daemon`` parser exposes per-role flags (``scheduler`` /
``worker`` / ``legacy-serial`` / ``all``) plus a sub-subcommand layer
for status / logs / stop without breaking the no-arg ``kanban daemon``
form.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from ...daemon import (
    DAEMON_LOG_FILENAME,
    CombinedDaemon,
    DaemonConfig,
    DaemonLockError,
    KanbanDaemon,
    SchedulerDaemon,
    WorkerDaemon,
    clear_stale_lock,
    daemon_lock,
    daemon_status,
    detach_to_background,
    lock_path,
)
from ...orchestrator import KanbanOrchestrator
from ..helpers import _make_orchestrator


def cmd_daemon_status(args: argparse.Namespace) -> int:
    """Print the lock-file state in the same shape as ``GET /api/daemon``."""
    import json as _json

    info = daemon_status(args.board)
    if getattr(args, "as_json", False):
        print(_json.dumps(info, ensure_ascii=False))
        return 0

    status = info.get("status", "unknown")
    pid = info.get("pid")
    started = info.get("started_at")
    started_str = "-"
    if started is not None:
        try:
            started_str = datetime.fromtimestamp(float(started), tz=timezone.utc).isoformat()
        except (TypeError, ValueError):
            started_str = str(started)

    print(f"status:     {status}")
    print(f"pid:        {pid if pid is not None else '-'}")
    print(f"started_at: {started_str}")
    print(f"lock_path:  {info.get('lock_path', '-')}")
    log_path = Path(args.board) / DAEMON_LOG_FILENAME
    if log_path.is_file():
        print(f"log:        {log_path}")
    return 0


def _pid_command(pid: int) -> str | None:
    """Return the COMMAND column for ``pid`` from ``ps``, or None.

    Used to defend against PID reuse: a stale ``.daemon.lock`` whose pid
    has been recycled by an unrelated process would otherwise let
    ``daemon stop`` SIGTERM that process. ``ps`` is POSIX and present on
    macOS + every common Linux. Errors → None so callers can decide.
    """
    try:
        rv = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True, text=True, check=False,
        )
    except OSError:
        return None
    if rv.returncode != 0:
        return None
    out = rv.stdout.strip()
    return out or None


def _looks_like_kanban_daemon(pid: int) -> bool:
    """Best-effort check that ``pid`` is a kanban daemon, not a recycled pid.

    A bare substring match on "kanban" was too loose — `grep kanban`,
    `vim kanban/cli.py`, and a developer's editor all matched. Instead
    look for a kanban *launcher*: argv[0]'s basename is one of the
    project's entry points, or argv[0] is a python/uv runner with a
    later token that names the package.
    """
    import shlex

    # Resolved via ``kanban.cli`` so test monkeypatches on the package
    # namespace reach the call site.
    from kanban import cli as _cli
    cmd = _cli._pid_command(pid)
    if not cmd:
        return False
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        tokens = cmd.split()
    if not tokens:
        return False

    def _basename(tok: str) -> str:
        return tok.rsplit("/", 1)[-1]

    entry_points = ("kanban", "kanban-mcp")
    if _basename(tokens[0]) in entry_points:
        return True

    launcher = _basename(tokens[0])
    if launcher == "uv" or "python" in launcher:
        for tok in tokens[1:]:
            base = _basename(tok)
            if base in entry_points:
                return True
            if tok == "kanban" or tok.startswith("kanban."):
                return True
            if "/kanban/" in tok or tok.endswith("/kanban"):
                return True
    return False


def _force_remove_lock(path: Path, *, label: str) -> int:
    """Delete a (possibly malformed) lock file. Returns shell-style rc."""
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        print(f"Failed to remove {label} at {path}: {exc}", file=sys.stderr)
        return 1
    print(f"Removed {label} at {path}.")
    return 0


def cmd_daemon_stop(args: argparse.Namespace) -> int:
    """Send SIGTERM (or SIGKILL with --force) to the daemon and wait for the lock to clear."""
    import signal as _signal

    info = daemon_status(args.board)
    status = info.get("status")
    path = lock_path(args.board)
    if status == "stopped":
        # daemon_status() returns "stopped" both when the lock is absent
        # AND when it exists but JSON-decode fails. Cover the malformed
        # case here so `daemon stop` is the documented recovery path.
        if path.exists():
            return _force_remove_lock(path, label="malformed lock")
        print("No daemon is running on this board.", file=sys.stderr)
        return 1
    if status == "stale":
        try:
            cleared = clear_stale_lock(args.board)
        except (TypeError, ValueError):
            # Non-numeric pid in the lock file would otherwise crash
            # clear_stale_lock's int() coercion. Unlink directly.
            return _force_remove_lock(path, label="malformed stale lock")
        if not cleared:
            print(f"Failed to remove stale lock at {path}", file=sys.stderr)
            return 1
        print(f"Cleared stale lock at {path}.")
        return 0

    pid = info.get("pid")
    if not pid:
        print("Daemon lock has no recorded pid; refusing to signal.", file=sys.stderr)
        return 1

    # Guard against PID reuse: a stale lock whose pid was later recycled
    # by an unrelated process would otherwise be SIGTERM'd here. Refuse
    # unless the recorded pid still looks like a kanban process.
    from kanban import cli as _cli
    if not _cli._looks_like_kanban_daemon(int(pid)):
        print(
            f"Refusing to signal pid {pid}: process command does not look "
            f"like a kanban daemon (likely a stale lock with a reused pid). "
            f"Inspect manually, then `rm {lock_path(args.board)}` if safe.",
            file=sys.stderr,
        )
        return 1

    sig = _signal.SIGKILL if getattr(args, "stop_force", False) else _signal.SIGTERM
    try:
        os.kill(int(pid), sig)
    except ProcessLookupError:
        print(f"pid {pid} is gone; clearing the stale lock.")
        clear_stale_lock(args.board)
        return 0
    except PermissionError as exc:
        print(f"Cannot signal pid {pid}: {exc}", file=sys.stderr)
        return 1

    timeout = float(getattr(args, "stop_timeout", 5.0))
    deadline = time.monotonic() + timeout
    path = lock_path(args.board)
    while time.monotonic() < deadline:
        if not path.exists():
            print(f"Daemon (pid {pid}) stopped.")
            return 0
        # SIGKILL bypasses the daemon's own lock cleanup, so once the
        # pid is reaped we have to unlink the file ourselves.
        # clear_stale_lock is a no-op while the pid is still alive, which
        # also handles slow SIGTERM shutdown gracefully.
        if clear_stale_lock(args.board):
            print(f"Daemon (pid {pid}) stopped; cleared lock at {path}.")
            return 0
        time.sleep(0.1)

    print(
        f"Daemon (pid {pid}) did not release the lock within "
        f"{timeout:.1f}s. Re-run with --force to SIGKILL.",
        file=sys.stderr,
    )
    return 1


def cmd_daemon_logs(args: argparse.Namespace) -> int:
    """Print the tail of ``<board>/daemon.log``, optionally following."""
    from collections import deque

    log_path = Path(args.board) / DAEMON_LOG_FILENAME
    try:
        fh = log_path.open("r", encoding="utf-8", errors="replace")
    except FileNotFoundError:
        print(
            f"No daemon log at {log_path}. Run `kanban daemon --detach` "
            f"to create one.",
            file=sys.stderr,
        )
        return 1
    except OSError as exc:
        print(f"Failed to read {log_path}: {exc}", file=sys.stderr)
        return 1

    with fh:
        # deque keeps only the last N lines as we stream — bounds memory
        # for arbitrarily large logs while still honoring -n. -n 0 means
        # "no backlog" (useful with -f to watch only new entries); a
        # negative value falls back to "all" for parity with `tail`.
        if args.lines > 0:
            tail: list[str] | deque[str] = deque(fh, maxlen=args.lines)
        elif args.lines == 0:
            tail = []
            fh.read()  # advance to EOF for a clean -f start
        else:
            tail = fh.readlines()
        for line in tail:
            sys.stdout.write(line)

        if not args.follow:
            return 0

        sys.stdout.flush()
        try:
            while True:
                chunk = fh.read()
                if chunk:
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
                else:
                    time.sleep(0.25)
        except KeyboardInterrupt:
            return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    # Sub-subcommand dispatch. When `kanban daemon stop` is invoked,
    # argparse fills in `daemon_command` and we route to a one-shot
    # handler instead of starting the loop.
    daemon_command = getattr(args, "daemon_command", None)
    if daemon_command == "stop":
        return cmd_daemon_stop(args)
    if daemon_command == "status":
        return cmd_daemon_status(args)
    if daemon_command == "logs":
        return cmd_daemon_logs(args)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.detach and args.once:
        print("--detach and --once are mutually exclusive", file=sys.stderr)
        return 2

    board_dir = args.board
    role = args.role
    # `--detach` MUST fork before any scheduler/worker thread is created.
    # CombinedDaemon spawns threads inside run(); forking after threads
    # exist would leave orphan threads in the parent and break signal
    # handling in the child.
    if args.detach:
        detach_to_background(board_dir)

    def _build_config() -> DaemonConfig:
        cfg_kwargs: dict[str, object] = {
            "poll_interval": args.poll_interval,
            "max_idle_cycles": 1 if args.once else None,
            "max_claims": args.max_claims,
        }
        if args.worker_id:
            cfg_kwargs["worker_id"] = args.worker_id
        return DaemonConfig(**cfg_kwargs)

    # Workers do not hold the board lock — only scheduler/legacy/all do.
    needs_board_lock = role in ("scheduler", "legacy-serial", "all")

    def _run_daemon(lock_file: Path | None = None) -> int:
        _, orchestrator = _make_orchestrator(args)
        config = _build_config()
        if role == "scheduler":
            daemon = SchedulerDaemon(orchestrator, config=config)
        elif role == "worker":
            daemon = WorkerDaemon(orchestrator, config=config)
        elif role == "legacy-serial":
            daemon = KanbanDaemon(orchestrator, config=config)
        else:  # all — real N-way parallel; workers need their own orchestrators
            def _fresh_orchestrator() -> KanbanOrchestrator:
                _, o = _make_orchestrator(args)
                return o

            daemon = CombinedDaemon(
                orchestrator,
                config=config,
                orchestrator_factory=_fresh_orchestrator,
            )
        if lock_file is not None:
            daemon.add_force_exit_cleanup(
                lambda p=lock_file: p.unlink(missing_ok=True)
            )
        # Signal handlers live ONLY on the main thread. CombinedDaemon
        # relies on its shared stop event to propagate into child threads;
        # no sub-daemon calls signal.signal(...) itself.
        daemon.install_signal_handlers()
        if args.once:
            did = daemon.run_once()
            if not did:
                print("Board is idle.")
            return 0
        return daemon.run()

    try:
        if needs_board_lock:
            with daemon_lock(board_dir) as lock_file:
                return _run_daemon(lock_file)
        return _run_daemon()
    except DaemonLockError as exc:
        print(str(exc), file=sys.stderr)
        return 2


def register_daemon_commands(sub) -> None:
    """Register the ``kanban daemon`` parser (with stop/status/logs sub-subs)."""
    daemon = sub.add_parser("daemon", help="Run the dispatcher loop (foreground by default)")
    daemon.add_argument("--detach", action="store_true", help="Fork into the background")
    daemon.add_argument("--once", action="store_true", help="Run a single tick and exit")
    daemon.add_argument(
        "--poll-interval", type=float, default=2.0, help="Idle sleep in seconds (default 2.0)"
    )
    daemon.add_argument("--verbose", action="store_true", help="Enable DEBUG logging")
    daemon.add_argument(
        "--role",
        choices=["all", "scheduler", "worker", "legacy-serial"],
        default="all",
        help=(
            "Daemon role (default: all = scheduler+worker in one process). "
            "`scheduler` creates claims and holds the board lock; `worker` "
            "executes claimed cards and takes no board lock. `legacy-serial` "
            "runs the pre-v0.1.2 tick path."
        ),
    )
    daemon.add_argument(
        "--worker-id",
        dest="worker_id",
        help="Stable worker identifier for `--role worker` (default: random).",
    )
    daemon.add_argument(
        "--max-claims",
        dest="max_claims",
        type=int,
        default=2,
        help="Scheduler concurrency budget (default 2).",
    )

    # Optional sub-subcommands. Adding them AFTER the run-mode flags keeps
    # `kanban daemon --once` (no subcommand) parsing the same as before; a
    # subcommand wins when present, e.g. `kanban daemon stop`.
    daemon_sub = daemon.add_subparsers(dest="daemon_command", required=False)

    d_stop = daemon_sub.add_parser(
        "stop",
        help="Send SIGTERM to the daemon recorded in .daemon.lock.",
    )
    d_stop.add_argument(
        "--force",
        dest="stop_force",
        action="store_true",
        help="Use SIGKILL instead of SIGTERM. Last resort.",
    )
    d_stop.add_argument(
        "--timeout",
        dest="stop_timeout",
        type=float,
        default=5.0,
        help="Seconds to wait for the lock file to disappear after SIGTERM (default 5).",
    )

    d_status = daemon_sub.add_parser(
        "status",
        help="Print daemon lock state (running / stale / stopped).",
    )
    d_status.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit a single JSON object instead of human-readable lines.",
    )

    d_logs = daemon_sub.add_parser(
        "logs",
        help="Tail the daemon log file at <board>/daemon.log.",
    )
    d_logs.add_argument(
        "-f",
        "--follow",
        action="store_true",
        help="Stream new lines as the daemon writes them.",
    )
    d_logs.add_argument(
        "-n",
        "--lines",
        type=int,
        default=50,
        help="Number of trailing lines to print before following (default 50).",
    )
