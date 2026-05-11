from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ..executors.base import CardExecutor
from ..models import (
    Card,
    CardPriority,
    CardStatus,
    LeasePolicy,
    RetryPolicy,
)
from ..store import BoardStore
from .helpers import (
    WipPolicy,
    _patch_executor_cwd,
)
from .results import ResultCommitter
from .retry import RetryHandler
from .scheduling import Scheduler
from .terminal import block_card, detach_worktree_on_terminal
from .transitions import ResultTransitioner
from .worktree_flow import WorktreeClaimPreparer

if TYPE_CHECKING:
    from ..worktree import WorktreeManager


class KanbanOrchestrator:
    def __init__(
        self,
        store: BoardStore,
        executor: CardExecutor,
        wip_policy: WipPolicy | None = None,
        lease_policy: LeasePolicy | None = None,
        retry_policy: RetryPolicy | None = None,
        worktree_mgr: WorktreeManager | None = None,
    ) -> None:
        self.store = store
        self.executor = executor
        self.wip_policy = wip_policy or WipPolicy()
        self.lease_policy = lease_policy or LeasePolicy()
        self.retry_policy = retry_policy or RetryPolicy()
        self.worktree_mgr = worktree_mgr
        self.scheduler = Scheduler(self)
        self.worktree_claims = WorktreeClaimPreparer(self)
        self.result_committer = ResultCommitter(self)
        self.retry_handler = RetryHandler(self)
        self.result_transitions = ResultTransitioner(self)

    def create_card(
        self,
        title: str,
        goal: str,
        priority: CardPriority = CardPriority.MEDIUM,
        acceptance_criteria: list[str] | None = None,
        depends_on: list[str] | None = None,
    ) -> Card:
        card = Card(
            title=title,
            goal=goal,
            priority=priority,
            acceptance_criteria=acceptance_criteria or [],
            depends_on=depends_on or [],
        )
        return self.store.add_card(card)

    def block(self, card_id: str, reason: str) -> Card:
        return block_card(self.store, self.worktree_mgr, card_id, reason)

    def unblock(self, card_id: str, target: CardStatus = CardStatus.INBOX) -> Card:
        self.store.update_card(card_id, blocked_reason=None)
        card = self.store.move_card(card_id, target, f"Unblocked to {target.value}")
        detach_worktree_on_terminal(
            self.store, self.worktree_mgr, card_id, target
        )
        return card

    def select_and_claim(self, worker_id: str | None = None):
        return self.scheduler.select_and_claim(worker_id=worker_id)

    def run_until_idle(self, max_steps: int = 20) -> list[Card]:
        return self.scheduler.run_until_idle(max_steps=max_steps)

    def apply_claim_result(self, claim, result) -> Card:
        return self.result_committer.apply_claim_result(claim, result)

    def submit_result(self, claim, result, **kwargs):
        return self.result_committer.submit_result(claim, result, **kwargs)

    def commit_pending_results(self) -> int:
        return self.result_committer.commit_pending_results()

    def recover_stale_claims(self, *, now=None) -> int:
        return self.retry_handler.recover_stale_claims(now=now)

    def retry_claim(self, previous, *, reason: str, category):
        return self.retry_handler.retry_claim(
            previous, reason=reason, category=category
        )

    def _setup_worktree_for_claim(self, card, role, claim) -> bool:
        return self.worktree_claims.setup_for_claim(card, role, claim)

    def _apply_result(self, card_id: str, result) -> None:
        self.result_transitions.apply_result(card_id, result)

    def _retry_or_block(self, failed_claim, category, reason: str) -> None:
        self.retry_handler.retry_or_block(failed_claim, category, reason)

    LEGACY_SERIAL_WORKER_ID = "local-serial"

    def tick(self) -> Card | None:
        """Legacy serial path: select → claim → execute → apply in one process.

        Preserved for `kanban daemon --role legacy-serial` and the existing
        `kanban tick` / `kanban run` CLI commands. The claim is created with
        an owner (``LEGACY_SERIAL_WORKER_ID``) so a parallel worker daemon
        cannot steal it between claim and execute — only a split-topology
        scheduler publishes unassigned claims.
        """
        claim = self.select_and_claim(worker_id=self.LEGACY_SERIAL_WORKER_ID)
        if claim is None:
            return None

        card = self.store.get_card(claim.card_id)
        _restore_cwd = None
        if claim.worktree_path is not None:
            _restore_cwd = _patch_executor_cwd(
                self.executor, Path(claim.worktree_path)
            )
        try:
            result = self.executor.run(claim.role, card)
        finally:
            if _restore_cwd is not None:
                _restore_cwd()
        self.apply_claim_result(claim, result)
        return self.store.get_card(claim.card_id)
