"""Local dispatcher daemon for a kanban board.

Foreground is the default: ``uv run kanban daemon`` runs a single-process
scheduling loop on the current terminal so an operator can Ctrl-C it.
``--detach`` forks once, reparents to init, and redirects logs to
``<board>/daemon.log``. Both modes share the same ``.daemon.lock`` guard
and graceful-shutdown path.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Callable, Iterator
from uuid import uuid4

from .models import ExecutionClaim, FailureCategory, WorkerPresence, utc_now
from .orchestrator import KanbanOrchestrator, _patch_executor_cwd


LOCK_FILENAME = ".daemon.lock"
DAEMON_LOG_FILENAME = "daemon.log"

log = logging.getLogger("kanban.daemon")


# ---------- lock ----------


class DaemonLockError(RuntimeError):
    """Raised when another live process already holds the board lock."""


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


def lock_path(board_dir: Path) -> Path:
    return Path(board_dir) / LOCK_FILENAME


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but belongs to another user — treat as alive.
        return True
    return True


def read_lock(board_dir: Path) -> dict | None:
    path = lock_path(board_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def clear_stale_lock(board_dir: Path) -> bool:
    """Remove the lock if its recorded pid is no longer alive. Returns True if cleared."""
    data = read_lock(board_dir)
    if data is None:
        return False
    pid = int(data.get("pid", 0))
    if _pid_alive(pid):
        return False
    try:
        lock_path(board_dir).unlink(missing_ok=True)
    except OSError:
        return False
    return True


@contextmanager
def daemon_lock(board_dir: Path) -> Iterator[Path]:
    board_dir.mkdir(parents=True, exist_ok=True)
    path = lock_path(board_dir)
    clear_stale_lock(board_dir)

    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        data = read_lock(board_dir) or {}
        raise DaemonLockError(
            f"Another kanban daemon is running on this board "
            f"(pid={data.get('pid', '?')}, started={data.get('started_at', '?')})."
        )

    try:
        payload = json.dumps(
            {"pid": os.getpid(), "started_at": time.time()}, ensure_ascii=False
        )
        os.write(fd, payload.encode("utf-8"))
    finally:
        os.close(fd)

    try:
        yield path
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def assert_no_daemon(board_dir: Path) -> None:
    """Raise DaemonLockError if a live daemon currently holds the board."""
    clear_stale_lock(board_dir)
    data = read_lock(board_dir)
    if data is None:
        return
    pid = int(data.get("pid", 0))
    if _pid_alive(pid):
        raise DaemonLockError(
            f"Daemon (pid={pid}) is running on this board; refuse to mutate "
            f"while a dispatcher holds the lock. Stop the daemon or pass --force."
        )


# ---------- daemon loop ----------


@dataclass
class DaemonConfig:
    poll_interval: float = 2.0
    max_idle_cycles: int | None = None  # None = run forever
    # v0.1.2 role split knobs. Legacy (KanbanDaemon) ignores these.
    max_claims: int = 2
    # ``None`` (the default) means "auto-derive" — ``__post_init__`` fills in
    # a random ``worker-<8-hex>`` value and records that the id was generated.
    # That flag lets ``CombinedDaemon._derive_worker_prefix`` distinguish the
    # auto-id from an operator who explicitly passed a string that happens to
    # match the default shape (e.g. ``--worker-id worker-deadbeef``).
    worker_id: str | None = None

    def __post_init__(self) -> None:
        if self.worker_id is None:
            self._worker_id_auto = True
            self.worker_id = f"worker-{uuid4().hex[:8]}"
        else:
            self._worker_id_auto = False


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

    # signal handling
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


# ---------- v0.1.2 scheduler / worker split ----------


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


class SchedulerDaemon(_RoleDaemonBase):
    """Claim-creation loop. Holds the board ``.daemon.lock``; no execution.

    Each tick scans the board, skips cards with a live claim, and creates
    up to ``max_claims`` unassigned claims. Workers pick those up via
    :meth:`BoardStore.try_acquire_claim`. The scheduler never runs the
    executor or mutates card state after execution.
    """

    def __init__(
        self, orchestrator: KanbanOrchestrator, config: DaemonConfig | None = None
    ) -> None:
        super().__init__(config)
        self.orchestrator = orchestrator

    def run_once(self) -> bool:
        _refresh_store(self.orchestrator.store)
        # Commit any envelopes workers have submitted since last tick, then
        # recover any leases that expired during that window — both must run
        # before creating new claims so stale cards don't block new work.
        committed = self.orchestrator.commit_pending_results()
        recovered = self.orchestrator.recover_stale_claims()

        store = self.orchestrator.store
        live = store.list_claims()
        if len(live) >= self.config.max_claims:
            return bool(committed or recovered)

        created = False
        budget = self.config.max_claims - len(live)
        for _ in range(budget):
            claim = self.orchestrator.select_and_claim(worker_id=None)
            if claim is None:
                break
            self._ticks += 1
            created = True
            log.info(
                "scheduler claimed %s → %s (role=%s, attempt=%d)",
                claim.card_id[:8],
                claim.status_at_claim.value,
                claim.role.value,
                claim.attempt,
            )

        if not (created or committed or recovered):
            wt_mgr = getattr(self.orchestrator, "worktree_mgr", None)
            if wt_mgr is not None:
                all_cards = store.list_cards()
                card_statuses = {c.id: c.status for c in all_cards}
                card_blocked_at = {
                    c.id: c.blocked_at for c in all_cards if c.blocked_at is not None
                }
                pruned = wt_mgr.prune_stale(
                    card_statuses, card_blocked_at=card_blocked_at,
                )
                for cid in pruned:
                    # Clear stale worktree metadata on the card so any later
                    # unblock/requeue recreates isolation from scratch.
                    # Tolerate races where an operator deleted the card file
                    # between list_cards() and this loop — match the same
                    # external-delete handling in commit_pending_results()
                    # and recover_stale_claims().
                    try:
                        self.orchestrator.store.update_card(
                            cid,
                            worktree_branch=None,
                            worktree_base_commit=None,
                        )
                        self.orchestrator.store.append_runtime_event(
                            cid,
                            event_type="worktree.pruned",
                            message=f"Worktree branch pruned: kanban/{cid}",
                            worktree_branch=f"kanban/{cid}",
                        )
                    except KeyError:
                        log.info(
                            "card %s vanished before worktree prune metadata "
                            "could be recorded; skipping",
                            cid[:8],
                        )

        return bool(created or committed or recovered)


class WorkerDaemon(_RoleDaemonBase):
    """Execution loop. Takes no board lock; heartbeats as a WorkerPresence.

    Each tick: (1) refresh own presence; (2) try to acquire any unassigned
    claim via the store's CAS; (3) run the executor; (4) apply the result.
    Claim acquisition failures are no-ops so many workers can share one board.
    """

    def __init__(
        self, orchestrator: KanbanOrchestrator, config: DaemonConfig | None = None
    ) -> None:
        super().__init__(config)
        self.orchestrator = orchestrator
        self._started_at = utc_now()
        self._host = socket.gethostname()

    @property
    def worker_id(self) -> str:
        return self.config.worker_id

    def _heartbeat(self) -> None:
        now = utc_now()
        self.orchestrator.store.heartbeat_worker(
            WorkerPresence(
                worker_id=self.worker_id,
                pid=os.getpid(),
                started_at=self._started_at,
                heartbeat_at=now,
                host=self._host,
            )
        )

    def _acquire_any_claim(self) -> ExecutionClaim | None:
        store = self.orchestrator.store
        lease = self.orchestrator.lease_policy
        now = utc_now()
        for claim in store.list_claims():
            if claim.worker_id is not None:
                continue
            # Short lease (~lease_seconds). A background heartbeat thread
            # renews it while executor.run() is running; see
            # ``_heartbeat_claim``. If the worker crashes or hangs, renewal
            # stops and the scheduler's stale recovery fires within
            # lease_seconds — not ``timeout_s`` minutes later.
            lease_expires = now + timedelta(seconds=lease.lease_seconds)
            acquired = store.try_acquire_claim(
                claim.card_id,
                worker_id=self.worker_id,
                heartbeat_at=now,
                lease_expires_at=lease_expires,
            )
            if acquired is not None:
                return acquired
        return None

    @contextmanager
    def _heartbeat_claim(self, claim: ExecutionClaim) -> Iterator[None]:
        """Keep the claim's lease fresh while the body runs.

        A daemon thread calls ``store.renew_claim`` every
        ``lease_policy.heartbeat_seconds`` and pushes ``lease_expires_at``
        forward by ``lease_seconds``. It stops renewing once elapsed time
        exceeds ``claim.timeout_s`` — that is the runtime timeout, and the
        scheduler's stale recovery path handles it as ``TIMEOUT``. It also
        stops on any renew_claim failure (claim was cleared externally).

        On exit we signal the thread, wait briefly for it to drain, and
        leave. We don't bubble heartbeat errors — the worst case is a
        premature stale recovery, which the runtime already handles.
        """
        lease = self.orchestrator.lease_policy
        stop = threading.Event()
        started = utc_now()

        def loop() -> None:
            while not stop.wait(lease.heartbeat_seconds):
                now = utc_now()
                elapsed = (now - started).total_seconds()
                if elapsed >= claim.timeout_s:
                    log.warning(
                        "worker %s: claim %s exceeded timeout_s=%d; "
                        "stopping heartbeat so scheduler can recover",
                        self.worker_id,
                        claim.claim_id,
                        claim.timeout_s,
                    )
                    return
                try:
                    self.orchestrator.store.renew_claim(
                        claim.card_id,
                        claim_id=claim.claim_id,
                        heartbeat_at=now,
                        lease_expires_at=now
                        + timedelta(seconds=lease.lease_seconds),
                        worker_id=self.worker_id,
                    )
                except Exception:  # noqa: BLE001 — claim gone or fs error
                    log.debug(
                        "worker %s: heartbeat for %s failed; stopping",
                        self.worker_id,
                        claim.claim_id,
                    )
                    return

        thread = threading.Thread(
            target=loop,
            name=f"kanban-heartbeat-{claim.claim_id}",
            daemon=True,
        )
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=2.0)

    def run_once(self) -> bool:
        _refresh_store(self.orchestrator.store)
        self._heartbeat()
        claim = self._acquire_any_claim()
        if claim is None:
            return False

        card = self.orchestrator.store.get_card(claim.card_id)
        log.info(
            "worker %s running %s (role=%s)",
            self.worker_id,
            claim.card_id[:8],
            claim.role.value,
        )
        started_at = utc_now()
        # Run executor under a heartbeat-renewed lease so a legitimate
        # long run is not declared stale mid-flight.
        _restore_cwd = None
        if claim.worktree_path is not None:
            _restore_cwd = _patch_executor_cwd(
                self.orchestrator.executor, Path(claim.worktree_path)
            )
        with self._heartbeat_claim(claim):
            try:
                result = self.orchestrator.executor.run(claim.role, card)
            except Exception as exc:  # noqa: BLE001 — worker must never crash loop
                log.exception(
                    "worker %s executor raised on %s",
                    self.worker_id,
                    claim.card_id[:8],
                )
                self._submit_safe(
                    claim,
                    None,
                    started_at=started_at,
                    ok=False,
                    failure_reason=f"executor raised {type(exc).__name__}: {exc}",
                    failure_category=FailureCategory.INFRASTRUCTURE,
                )
            else:
                self._submit_safe(
                    claim,
                    result,
                    started_at=started_at,
                    ok=True,
                )
            finally:
                if _restore_cwd is not None:
                    _restore_cwd()
        self._ticks += 1
        return True

    def _submit_safe(
        self,
        claim: ExecutionClaim,
        result,
        *,
        started_at,
        ok: bool = True,
        failure_reason: str | None = None,
        failure_category: FailureCategory | None = None,
    ) -> None:
        """Persist the envelope, but never let a persistence error crash
        the worker loop.

        If ``write_result`` raises (``FileExistsError`` for a duplicate
        submission, disk full, corrupt runtime dir, …), we log loudly and
        return. The claim is still live; its lease will expire, the
        scheduler will recover it via the stale-claim path, and the
        retry matrix handles the rest. The alternative — crashing — would
        leave the claim holding a slot with nothing coming, until lease
        expiry anyway, with the added damage of killing the worker.
        """
        try:
            self.orchestrator.submit_result(
                claim,
                result,
                worker_id=self.worker_id,
                started_at=started_at,
                ok=ok,
                failure_reason=failure_reason,
                failure_category=failure_category,
            )
        except Exception:  # noqa: BLE001 — worker must never crash loop
            log.exception(
                "worker %s failed to persist result envelope for claim %s "
                "(card %s). Claim kept live; scheduler will recover on "
                "lease expiry.",
                self.worker_id,
                claim.claim_id,
                claim.card_id[:8],
            )

    def run(self) -> int:
        self._heartbeat()
        try:
            return super().run()
        finally:
            try:
                self.orchestrator.store.remove_worker(self.worker_id)
            except Exception:  # pragma: no cover - best-effort cleanup
                log.exception("failed to remove worker presence on shutdown")


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
            from dataclasses import replace as _replace

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
    def workers(self) -> list["WorkerDaemon"]:
        return list(self._workers)

    @property
    def scheduler(self) -> "SchedulerDaemon":
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
        from dataclasses import replace as _replace

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


# ---------- detach ----------


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
