from __future__ import annotations

from ..models import AgentRole, Card, ExecutionClaim
from .component import OrchestratorComponent
from .terminal import block_card


class WorktreeClaimPreparer(OrchestratorComponent):
    def setup_for_claim(
        self, card: Card, role: AgentRole, claim: ExecutionClaim,
    ) -> bool:
        """Resolve the card's worktree and stamp ``claim.worktree_path``.

        Returns ``True`` when the claim is ready to persist. Returns
        ``False`` after blocking the card on a worktree error so the
        caller can move on to the next actionable candidate.
        """
        if self.worktree_mgr is None or role == AgentRole.PLANNER:
            return True

        from ..worktree import WorktreeCreateError

        if card.worktree_branch is None and role in (
            AgentRole.REVIEWER, AgentRole.VERIFIER,
        ):
            # Card reached REVIEW without a worktree — either the
            # board pre-dates the worktree feature, or metadata was
            # cleared. Block rather than run unisolated against the
            # worker's (presumably modified) main checkout.
            reason = (
                f"card has no worktree branch; cannot run "
                f"{role.value} in isolation"
            )
            block_card(
                self.store,
                self.worktree_mgr,
                card.id,
                reason,
                event_type="worktree.missing",
            )
            return False

        if card.worktree_branch is None and role == AgentRole.WORKER:
            try:
                wt_info = self.worktree_mgr.create(card.id)
            except WorktreeCreateError as exc:
                block_card(self.store, self.worktree_mgr, card.id, str(exc))
                return False
            self.store.update_card(
                card.id,
                worktree_branch=wt_info.branch,
                worktree_base_commit=wt_info.base_commit,
            )
            claim.worktree_path = str(wt_info.path)
            self.store.append_runtime_event(
                card.id,
                event_type="worktree.created",
                message=(
                    f"Worktree created: {wt_info.branch}"
                    f" from {wt_info.base_commit[:12]}"
                ),
                worktree_branch=wt_info.branch,
            )
            return True

        if card.worktree_branch is not None:
            wt_info = self.worktree_mgr.get(
                card.id, base_commit=card.worktree_base_commit or "",
            )
            if wt_info is not None and wt_info.path is not None:
                claim.worktree_path = str(wt_info.path)
                return True
            if wt_info is not None and wt_info.path is None:
                # Worktree was detached (card unblocked/requeued) — re-checkout
                recheckout = self.worktree_mgr.recheckout(
                    card.id, card.worktree_branch,
                )
                if recheckout is not None:
                    claim.worktree_path = str(recheckout)
                    return True
                reason = (
                    f"failed to recheckout worktree {card.worktree_branch};"
                    " cannot run without isolation"
                )
                block_card(
                    self.store,
                    self.worktree_mgr,
                    card.id,
                    reason,
                    event_type="worktree.missing",
                    worktree_branch=card.worktree_branch,
                )
                return False
            if wt_info is None and role == AgentRole.WORKER:
                # Branch was pruned but card metadata is stale. Clear it
                # and create a fresh worktree so isolation is restored.
                try:
                    wt_info_new = self.worktree_mgr.create(card.id)
                except WorktreeCreateError as exc:
                    block_card(self.store, self.worktree_mgr, card.id, str(exc))
                    return False
                self.store.update_card(
                    card.id,
                    worktree_branch=wt_info_new.branch,
                    worktree_base_commit=wt_info_new.base_commit,
                )
                claim.worktree_path = str(wt_info_new.path)
                self.store.append_runtime_event(
                    card.id,
                    event_type="worktree.created",
                    message=(
                        f"Worktree recreated after prune: {wt_info_new.branch}"
                        f" from {wt_info_new.base_commit[:12]}"
                    ),
                    worktree_branch=wt_info_new.branch,
                )
                return True
            if wt_info is None and role in (
                AgentRole.REVIEWER, AgentRole.VERIFIER,
            ):
                # Reviewer/verifier cannot run without the worker's branch;
                # block the card rather than silently running in the main
                # checkout.
                reason = (
                    f"worktree branch {card.worktree_branch} missing; "
                    "cannot review deleted work"
                )
                block_card(
                    self.store,
                    self.worktree_mgr,
                    card.id,
                    reason,
                    event_type="worktree.missing",
                    worktree_branch=card.worktree_branch,
                )
                return False

        return True
