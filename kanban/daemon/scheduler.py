from __future__ import annotations

import logging

from ..orchestrator import KanbanOrchestrator
from .config import DaemonConfig
from .role_base import _RoleDaemonBase, _refresh_store

log = logging.getLogger("kanban.daemon")


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
