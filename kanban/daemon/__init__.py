"""Local dispatcher daemon for a kanban board.

Foreground is the default: ``uv run kanban daemon`` runs a single-process
scheduling loop on the current terminal so an operator can Ctrl-C it.
``--detach`` forks once, reparents to init, and redirects logs to
``<board>/daemon.log``. Both modes share the same ``.daemon.lock`` guard
and graceful-shutdown path.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from pathlib import Path
from typing import Callable

from ..orchestrator import KanbanOrchestrator
from .combined import CombinedDaemon
from .config import DaemonConfig
from .lock import (  # noqa: F401
    LOCK_FILENAME,
    DaemonLockError,
    _pid_alive,
    assert_no_daemon,
    clear_stale_lock,
    daemon_lock,
    daemon_status,
    lock_path,
    read_lock,
)
from .role_base import _gc_orphaned_runtime, _refresh_store
from .scheduler import SchedulerDaemon
from .worker import WorkerDaemon

__all__ = [
    "CombinedDaemon",
    "DAEMON_LOG_FILENAME",
    "DaemonConfig",
    "DaemonLockError",
    "KanbanDaemon",
    "LOCK_FILENAME",
    "SchedulerDaemon",
    "WorkerDaemon",
    "assert_no_daemon",
    "clear_stale_lock",
    "daemon_lock",
    "daemon_status",
    "detach_to_background",
    "lock_path",
    "read_lock",
]

DAEMON_LOG_FILENAME = "daemon.log"

log = logging.getLogger("kanban.daemon")


class KanbanDaemon:
    """Legacy serial daemon: runs the full tick (select → execute → commit).

    Retained as the `--role legacy-serial` fallback while the scheduler /
    worker split matures. New code should use :class:`SchedulerDaemon` +
    :class:`WorkerDaemon` or :class:`CombinedDaemon`.
    """

    def __init__(
        self,
        orchestrator: KanbanOrchestrator,
        config: DaemonConfig | None = None,
    ) -> None:
        self.orchestrator = orchestrator
        self.config = config or DaemonConfig()
        self._stop = threading.Event()
        self._ticks = 0
        self._idle_cycles = 0
        self._force_exit_cleanups: list[Callable[[], None]] = []

    def add_force_exit_cleanup(self, fn: Callable[[], None]) -> None:
        """Register a callable to run before force-exiting on a second signal.

        Normal shutdown unwinds through context managers; the force-exit
        path uses ``os._exit`` to escape a blocked executor call and would
        otherwise skip them (notably the ``.daemon.lock`` cleanup).
        """
        self._force_exit_cleanups.append(fn)

    def _run_force_exit_cleanups(self) -> None:
        for fn in self._force_exit_cleanups:
            try:
                fn()
            except Exception:  # pragma: no cover - best effort
                log.exception("force-exit cleanup failed")

    def request_stop(self, signum: int | None = None, _frame=None) -> None:
        name = signal.Signals(signum).name if signum else "request"
        if self._stop.is_set():
            # Second signal — force exit immediately. The current tick may be
            # blocked in executor.run() which can hold for minutes on real
            # agents; operators hitting Ctrl-C a second time expect exit now.
            log.warning("Second %s received; forcing exit.", name)
            self._run_force_exit_cleanups()
            os._exit(130)
        log.info("Shutdown requested (%s); will stop after current tick. "
                 "Press Ctrl-C again to force exit.", name)
        self._stop.set()

    def install_signal_handlers(self) -> None:
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)

    @property
    def ticks_processed(self) -> int:
        return self._ticks

    def run_once(self) -> bool:
        """Run a single tick. Returns True if a card was processed."""
        _refresh_store(self.orchestrator.store)
        card = self.orchestrator.tick()
        if card is None:
            return False
        self._ticks += 1
        log.info("Tick %d: processed card %s → %s", self._ticks, card.id[:8], card.status.value)
        return True

    def run(self) -> int:
        log.info(
            "Daemon started (pid=%d, poll=%.2fs, executor=%s)",
            os.getpid(),
            self.config.poll_interval,
            type(self.orchestrator.executor).__name__,
        )
        _gc_orphaned_runtime(self.orchestrator.store)
        while not self._stop.is_set():
            did_work = self.run_once()
            if did_work:
                self._idle_cycles = 0
                continue
            self._idle_cycles += 1
            if (
                self.config.max_idle_cycles is not None
                and self._idle_cycles >= self.config.max_idle_cycles
            ):
                log.info("Idle for %d cycles; exiting.", self._idle_cycles)
                break
            self._sleep(self.config.poll_interval)
        log.info("Daemon stopped after %d tick(s).", self._ticks)
        return 0

    def _sleep(self, seconds: float) -> None:
        # Event-backed sleep returns immediately on request_stop.
        self._stop.wait(timeout=seconds)


def detach_to_background(board_dir: Path) -> None:
    """Double-fork to detach the current process from the terminal.

    Only the grandchild returns; parent and child exit. Stdout/stderr are
    redirected to ``<board_dir>/daemon.log`` (append).
    """
    # Parent → child
    pid = os.fork()
    if pid > 0:
        os._exit(0)
    os.setsid()
    # Child → grandchild
    pid = os.fork()
    if pid > 0:
        os._exit(0)

    board_dir.mkdir(parents=True, exist_ok=True)
    log_path = board_dir / DAEMON_LOG_FILENAME
    log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(log_fd, sys.stdout.fileno())
    os.dup2(log_fd, sys.stderr.fileno())
    os.close(log_fd)

    # Re-install a sink that goes to the redirected stderr.
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    logging.basicConfig(
        level=root.level or logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )
