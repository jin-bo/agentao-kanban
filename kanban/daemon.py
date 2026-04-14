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
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from .models import ExecutionClaim, WorkerPresence, utc_now
from .orchestrator import KanbanOrchestrator


LOCK_FILENAME = ".daemon.lock"
DAEMON_LOG_FILENAME = "daemon.log"

log = logging.getLogger("kanban.daemon")


# ---------- lock ----------


class DaemonLockError(RuntimeError):
    """Raised when another live process already holds the board lock."""


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
    worker_id: str = field(default_factory=lambda: f"worker-{uuid4().hex[:8]}")


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
        self._stop = False
        self._ticks = 0
        self._idle_cycles = 0

    # signal handling
    def request_stop(self, signum: int | None = None, _frame=None) -> None:
        name = signal.Signals(signum).name if signum else "request"
        log.info("Shutdown requested (%s); will stop after current tick.", name)
        self._stop = True

    def install_signal_handlers(self) -> None:
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)

    @property
    def ticks_processed(self) -> int:
        return self._ticks

    def run_once(self) -> bool:
        """Run a single tick. Returns True if a card was processed."""
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
        while not self._stop:
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
        # Interruptible sleep so SIGINT lands promptly.
        deadline = time.monotonic() + seconds
        while not self._stop:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 0.25))


# ---------- v0.1.2 scheduler / worker split ----------


class _RoleDaemonBase:
    """Shared loop plumbing for scheduler/worker daemons."""

    def __init__(self, config: DaemonConfig | None = None) -> None:
        self.config = config or DaemonConfig()
        self._stop = False
        self._ticks = 0
        self._idle_cycles = 0

    def request_stop(self, signum: int | None = None, _frame=None) -> None:
        name = signal.Signals(signum).name if signum else "request"
        log.info("Shutdown requested (%s); will stop after current tick.", name)
        self._stop = True

    def install_signal_handlers(self) -> None:
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)

    @property
    def ticks_processed(self) -> int:
        return self._ticks

    def _sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not self._stop:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 0.25))

    def run_once(self) -> bool:  # pragma: no cover - overridden
        raise NotImplementedError

    def run(self) -> int:
        log.info(
            "%s started (pid=%d, poll=%.2fs)",
            type(self).__name__,
            os.getpid(),
            self.config.poll_interval,
        )
        while not self._stop:
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
        lease_expires = now + timedelta(seconds=lease.lease_seconds)
        for claim in store.list_claims():
            if claim.worker_id is not None:
                continue
            acquired = store.try_acquire_claim(
                claim.card_id,
                worker_id=self.worker_id,
                heartbeat_at=now,
                lease_expires_at=lease_expires,
            )
            if acquired is not None:
                return acquired
        return None

    def run_once(self) -> bool:
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
        try:
            result = self.orchestrator.executor.run(claim.role, card)
        except Exception as exc:  # noqa: BLE001 — worker must never crash the loop
            log.exception(
                "worker %s executor raised on %s", self.worker_id, claim.card_id[:8]
            )
            self.orchestrator.submit_result(
                claim,
                None,
                worker_id=self.worker_id,
                started_at=started_at,
                ok=False,
                failure_reason=f"executor raised {type(exc).__name__}: {exc}",
            )
        else:
            self.orchestrator.submit_result(
                claim,
                result,
                worker_id=self.worker_id,
                started_at=started_at,
                ok=True,
            )
        self._ticks += 1
        return True

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
    """One-process convenience: alternate scheduler + worker ticks.

    Not a true concurrent topology — the two loops share a thread and the
    scheduler simply refills claims between worker runs. Use for local dev;
    use separate `SchedulerDaemon` + `WorkerDaemon` processes for real
    parallelism.
    """

    def __init__(
        self, orchestrator: KanbanOrchestrator, config: DaemonConfig | None = None
    ) -> None:
        super().__init__(config)
        self.orchestrator = orchestrator
        self._scheduler = SchedulerDaemon(orchestrator, self.config)
        self._worker = WorkerDaemon(orchestrator, self.config)

    def run_once(self) -> bool:
        scheduled = self._scheduler.run_once()
        worked = self._worker.run_once()
        did = scheduled or worked
        if did:
            self._ticks += 1
        return did

    def run(self) -> int:
        self._worker._heartbeat()
        try:
            return super().run()
        finally:
            try:
                self.orchestrator.store.remove_worker(self._worker.worker_id)
            except Exception:  # pragma: no cover
                log.exception("failed to remove worker presence on shutdown")


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
