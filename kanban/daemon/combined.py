from __future__ import annotations

import logging
import os
import threading
from dataclasses import replace as _replace
from typing import Callable
from uuid import uuid4

from ..orchestrator import KanbanOrchestrator
from .config import DaemonConfig
from .role_base import _RoleDaemonBase, _gc_orphaned_runtime
from .scheduler import SchedulerDaemon
from .worker import WorkerDaemon

log = logging.getLogger("kanban.daemon")


class CombinedDaemon(_RoleDaemonBase):
    """Single-process real-parallel `all` daemon.

    Runs one scheduler loop plus ``max_claims`` worker loops — all in the
    same process but on independent threads — so ``--role all
    --max-claims N`` delivers N concurrent executions instead of the
    previous serial alternation.

    Each worker gets its own ``orchestrator`` (and therefore its own
    ``store`` / ``executor``). Sharing one ``MultiBackendExecutor``
    between threads would cross-contaminate ``working_directory``
    patches and router cache state; separate instances keep worktree
    isolation and router caching per-worker. The scheduler uses the
    ``orchestrator`` passed in.

    Stop is propagated via a shared ``threading.Event`` injected into
    every sub-daemon. Signal handlers are NOT installed on the sub-daemon
    objects — ``cmd_daemon`` (main thread) calls
    :meth:`install_signal_handlers` on this parent only. Sub-threads
    never call ``signal.signal(...)``.
    """

    def __init__(
        self,
        orchestrator: KanbanOrchestrator,
        config: DaemonConfig | None = None,
        *,
        orchestrator_factory: Callable[[], KanbanOrchestrator] | None = None,
    ) -> None:
        super().__init__(config)
        self.orchestrator = orchestrator

        # Without a factory every worker would share the same orchestrator,
        # causing cross-thread executor state corruption. Cap to 1 in that
        # case so the call site degrades gracefully instead of corrupting data.
        if orchestrator_factory is None and int(self.config.max_claims) > 1:
            import warnings

            warnings.warn(
                "CombinedDaemon: max_claims > 1 requires orchestrator_factory; "
                "capping worker count to 1 to avoid shared-state corruption.",
                stacklevel=2,
            )
        self._worker_count = (
            1
            if orchestrator_factory is None
            else max(1, int(self.config.max_claims))
        )

        self._worker_orchestrators: list[KanbanOrchestrator] = []
        for _ in range(self._worker_count):
            if orchestrator_factory is None:
                self._worker_orchestrators.append(orchestrator)
            else:
                self._worker_orchestrators.append(orchestrator_factory())

        self._scheduler = SchedulerDaemon(orchestrator, self.config)
        self._scheduler.attach_stop_event(self._stop)

        prefix = self._derive_worker_prefix()
        self._workers: list[WorkerDaemon] = []
        for i in range(self._worker_count):
            child_cfg = _replace(self.config, worker_id=f"{prefix}-{i + 1}")
            w = WorkerDaemon(self._worker_orchestrators[i], config=child_cfg)
            w.attach_stop_event(self._stop)
            self._workers.append(w)

    def _derive_worker_prefix(self) -> str:
        """Derive a worker_id prefix.

        - Explicit ``--worker-id`` wins: we use the operator's string as the
          prefix, even when it happens to match the ``worker-<8 hex>`` shape
          of the auto-generated default.
        - Otherwise we generate a random 6-hex base so children are compact
          ``worker-xxxxxx-1 / -2 / ...`` rather than doubling up UUIDs.
        """
        if getattr(self.config, "_worker_id_auto", False):
            return f"worker-{uuid4().hex[:6]}"
        return self.config.worker_id

    @property
    def workers(self) -> list[WorkerDaemon]:
        return list(self._workers)

    @property
    def scheduler(self) -> SchedulerDaemon:
        return self._scheduler

    def run_once(self) -> bool:
        """One scheduler pass + one acquire-execute-submit cycle per worker.

        Sequential for deterministic ``--once`` semantics: the scheduler
        commits/recovers/creates claims, then each worker is offered a
        single opportunity to pick up an unassigned claim and run it.

        ``WorkerDaemon.run_once`` heartbeats presence on every call but
        has no cleanup of its own (unlike ``WorkerDaemon.run``). The
        one-shot daemon exits as soon as this method returns, so we
        remove each worker's presence before returning — otherwise
        ``kanban workers`` and ``/api/board`` keep reporting workers
        that no longer exist.
        """
        scheduled = self._scheduler.run_once()
        any_worked = False
        try:
            for w in self._workers:
                worked = w.run_once()
                any_worked = any_worked or worked
        finally:
            for w in self._workers:
                try:
                    self.orchestrator.store.remove_worker(w.worker_id)
                except Exception:  # pragma: no cover - best-effort cleanup
                    log.exception(
                        "failed to remove worker presence after one-shot pass"
                    )
        did = scheduled or any_worked
        if did:
            self._ticks += 1
        return did

    def run(self) -> int:
        log.info(
            "CombinedDaemon started (pid=%d, workers=%d, max_claims=%d)",
            os.getpid(),
            len(self._workers),
            self.config.max_claims,
        )
        _gc_orphaned_runtime(self.orchestrator.store)

        # Sub-daemons run until the shared stop event fires. Overall
        # ``max_idle_cycles`` is enforced HERE so a quiet worker does not
        # drop out while the scheduler is still producing claims for its
        # peers. Without this override each sub-daemon counted its own
        # idle cycles and workers could exit before the first claim
        # landed — the CombinedDaemon would then join dead worker
        # threads mid-card and leave cards stranded in DOING.
        combined_max_idle = self.config.max_idle_cycles
        self._scheduler.config = _replace(
            self._scheduler.config, max_idle_cycles=None
        )
        for w in self._workers:
            w.config = _replace(w.config, max_idle_cycles=None)

        # Emit presence for every worker before the scheduler begins so a
        # `kanban workers` run during startup sees all N rows immediately.
        for w in self._workers:
            try:
                w._heartbeat()
            except Exception:  # pragma: no cover - best effort
                log.exception(
                    "worker %s failed initial heartbeat", w.worker_id
                )

        threads: list[threading.Thread] = []
        sched_thread = threading.Thread(
            target=self._scheduler.run,
            name="kanban-scheduler",
            daemon=False,
        )
        sched_thread.start()
        threads.append(sched_thread)
        worker_threads: list[tuple[WorkerDaemon, threading.Thread]] = []
        for idx, w in enumerate(self._workers, start=1):
            t = threading.Thread(
                target=w.run,
                name=f"kanban-worker-{idx}",
                daemon=False,
            )
            t.start()
            threads.append(t)
            worker_threads.append((w, t))

        def _total_ticks() -> int:
            return self._scheduler.ticks_processed + sum(
                w.ticks_processed for w in self._workers
            )

        def _has_active_claim() -> bool:
            # A worker mid-`executor.run()` does not advance any tick
            # counter until the call returns, so a long execution would
            # otherwise look identical to true idleness. An open claim
            # is the authoritative "work in flight" signal.
            #
            # Also treat uncommitted result envelopes as active work:
            # a worker clears its claim before write_result(), so there
            # is a window where list_claims() is empty but a result is
            # still waiting for the scheduler to commit it.
            try:
                store = self.orchestrator.store
                return bool(store.list_claims()) or bool(store.read_results())
            except Exception:  # pragma: no cover - defensive
                log.exception(
                    "CombinedDaemon failed to inspect claims; "
                    "treating as in-flight to avoid premature stop"
                )
                return True

        try:
            idle_polls = 0
            last_ticks = _total_ticks()
            poll = max(self.config.poll_interval, 0.05)
            while not self._stop.is_set():
                if not any(t.is_alive() for t in threads):
                    break
                self._stop.wait(timeout=poll)
                current = _total_ticks()
                if current > last_ticks:
                    idle_polls = 0
                    last_ticks = current
                    continue
                if _has_active_claim():
                    idle_polls = 0
                    continue
                idle_polls += 1
                if (
                    combined_max_idle is not None
                    and idle_polls >= combined_max_idle
                ):
                    log.info(
                        "CombinedDaemon idle for %d poll(s); stopping.",
                        idle_polls,
                    )
                    break
        finally:
            self._stop.set()
            for t in threads:
                t.join(timeout=10.0)
            # If any thread is still alive after the initial grace period,
            # keep waiting without a timeout. Returning from run() while a
            # worker thread is alive would release the board lock too early
            # and allow another process to start mutating the board while
            # the worker can still write result envelopes.
            for t in threads:
                if t.is_alive():
                    log.warning(
                        "thread %s still alive after 10s grace; "
                        "waiting for it to finish before releasing board lock",
                        t.name,
                    )
                    t.join()
            # Defense-in-depth: WorkerDaemon.run() already removes its
            # own presence in its finally. If a worker thread was killed
            # mid-start (before run() entered its try/finally) presence
            # may still be on disk, so clear it here.
            for w, t in worker_threads:
                try:
                    self.orchestrator.store.remove_worker(w.worker_id)
                except Exception:  # pragma: no cover
                    log.exception(
                        "failed to remove worker presence on shutdown"
                    )

        log.info(
            "CombinedDaemon stopped after %d scheduler+worker tick(s).",
            _total_ticks(),
        )
        return 0
