from __future__ import annotations

import logging
import os
import signal
import threading
from typing import Callable

from .config import DaemonConfig

log = logging.getLogger("kanban.daemon")


def _refresh_store(store) -> None:
    """Best-effort board refresh so daemon loops see external CLI edits."""
    refresh = getattr(store, "refresh", None)
    if callable(refresh):
        refresh()


def _gc_orphaned_runtime(store) -> None:
    """Clean runtime artifacts whose card file was deleted externally.

    Without this, the first ``commit_pending_results`` / ``recover_stale``
    tick would call ``update_card()`` on a missing id and raise KeyError,
    crashing the daemon loop.
    """
    gc = getattr(store, "gc_orphaned_runtime", None)
    if callable(gc):
        try:
            removed = gc()
        except Exception:  # noqa: BLE001 — best-effort cleanup
            log.exception("orphan-runtime GC failed; continuing")
            return
        if removed:
            log.warning("GC: removed %d orphan runtime file(s) at startup", removed)


class _RoleDaemonBase:
    """Shared loop plumbing for scheduler/worker daemons.

    ``_stop`` is a ``threading.Event`` so a parent (``CombinedDaemon``)
    can broadcast stop to all sub-daemon threads by injecting a shared
    event via :meth:`attach_stop_event`. Keeps plain single-process
    daemons compatible: they simply own their own event.
    """

    def __init__(self, config: DaemonConfig | None = None) -> None:
        self.config = config or DaemonConfig()
        self._stop = threading.Event()
        self._ticks = 0
        self._idle_cycles = 0
        self._force_exit_cleanups: list[Callable[[], None]] = []

    def attach_stop_event(self, event: threading.Event) -> None:
        """Replace this daemon's stop event with a shared one.

        Used by ``CombinedDaemon`` so a single SIGINT/SIGTERM on the main
        thread can halt every scheduler/worker thread at once — no
        cross-thread bool juggling, no per-thread signal handlers.
        """
        self._stop = event

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
            # Second signal — force exit. The current tick may be blocked in
            # executor.run() on a real agent for minutes; Ctrl-C twice means
            # exit now.
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

    def _sleep(self, seconds: float) -> None:
        # Using Event.wait gives us a responsive stop without polling the
        # bool every 0.25s. Returns immediately on stop.set().
        self._stop.wait(timeout=seconds)

    def run_once(self) -> bool:  # pragma: no cover - overridden
        raise NotImplementedError

    def run(self) -> int:
        log.info(
            "%s started (pid=%d, poll=%.2fs)",
            type(self).__name__,
            os.getpid(),
            self.config.poll_interval,
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
        log.info(
            "%s stopped after %d tick(s).", type(self).__name__, self._ticks
        )
        return 0
