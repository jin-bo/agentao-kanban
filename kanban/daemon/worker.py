from __future__ import annotations

import logging
import os
import socket
import threading
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Iterator

from ..models import ExecutionClaim, FailureCategory, WorkerPresence, utc_now
from ..orchestrator import KanbanOrchestrator, _patch_executor_cwd
from .config import DaemonConfig
from .role_base import _RoleDaemonBase, _refresh_store

log = logging.getLogger("kanban.daemon")


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
