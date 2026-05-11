from __future__ import annotations

from ..models import (
    AgentRole,
    ExecutionClaim,
    ExecutionEventType,
    FailureCategory,
    utc_now,
)
from .claims import build_execution_claim
from .component import OrchestratorComponent
from .helpers import WorktreeMissingError
from .terminal import block_card


class RetryHandler(OrchestratorComponent):
    def recover_stale_claims(self, *, now=None) -> int:
        """Scheduler: handle claims whose lease has expired (plan §Stuck Task
        Recovery). Categorized as ``lease_expiry``; consults the retry matrix
        to either create a linked replacement claim or block the card.

        The actual claim-clear + replacement runs under the transactional
        guarantees of :meth:`_retry_or_block`: the stale claim is only
        dropped once the replacement state (retry claim OR BLOCKED) is
        durably established, so a mid-recovery failure can't strand the
        card in an execution status without a claim.
        """
        stale = self.store.list_stale_claims(now=now or utc_now())
        for claim in stale:
            # Drop stale claim if its card was deleted externally.
            try:
                self.store.get_card(claim.card_id)
            except KeyError:
                try:
                    self.store.clear_claim(claim.card_id, claim_id=claim.claim_id)
                except Exception:  # noqa: BLE001
                    pass
                continue
            reason = (
                f"runtime lease expired on attempt {claim.attempt} "
                f"(role={claim.role.value}, claim={claim.claim_id})"
            )
            self.store.append_runtime_event(
                claim.card_id,
                event_type=ExecutionEventType.CLAIM_RECOVERED.value,
                message=reason,
                role=claim.role,
                claim_id=claim.claim_id,
                worker_id=claim.worker_id,
                attempt=claim.attempt,
                failure_category=FailureCategory.LEASE_EXPIRY.value,
                failure_reason=reason,
            )
            self.retry_or_block(claim, FailureCategory.LEASE_EXPIRY, reason)
        return len(stale)

    def retry_claim(
        self,
        previous: ExecutionClaim,
        *,
        reason: str,
        category: FailureCategory,
    ) -> ExecutionClaim:
        """Create a linked replacement claim with ``attempt+1``.

        Transactional rollback: the old claim is cleared first so the
        store-level uniqueness check accepts the new claim. If
        ``create_claim`` then raises, we restore the original claim so the
        card never ends up in an execution status without a live claim.
        Emits ``execution.retried`` on success.
        """
        card = self.store.get_card(previous.card_id)
        new_claim = build_execution_claim(
            card=card,
            role=previous.role,
            attempt=previous.attempt + 1,
            lease_seconds=self.lease_policy.lease_seconds,
            timeout_s=previous.timeout_s,
            retry_count=previous.retry_count + 1,
            retry_of_claim_id=previous.claim_id,
            worktree_path=previous.worktree_path,
            status_at_claim=previous.status_at_claim,
        )

        # Validate the worktree still exists. The previous claim may have
        # had a worktree_path, but the branch could have been deleted (via
        # prune or external git branch -D) between executions. If so,
        # either recreate it (WORKER) or raise so caller can BLOCK
        # (REVIEWER/VERIFIER).
        if (
            self.worktree_mgr is not None
            and previous.role != AgentRole.PLANNER
        ):
            if card.worktree_branch is not None:
                wt_info = self.worktree_mgr.get(
                    card.id, base_commit=card.worktree_base_commit or "",
                )
                if wt_info is not None and wt_info.path is not None:
                    new_claim.worktree_path = str(wt_info.path)
                elif wt_info is not None and wt_info.path is None:
                    recheckout = self.worktree_mgr.recheckout(
                        card.id, card.worktree_branch,
                    )
                    if recheckout is not None:
                        new_claim.worktree_path = str(recheckout)
                    else:
                        raise WorktreeMissingError(
                            f"cannot recheckout {card.worktree_branch}; "
                            f"cannot retry {previous.role.value} role"
                        )
                elif wt_info is None and previous.role == AgentRole.WORKER:
                    from ..worktree import WorktreeCreateError

                    try:
                        wt_info_new = self.worktree_mgr.create(card.id)
                    except WorktreeCreateError as exc:
                        raise WorktreeMissingError(
                            f"cannot recreate worktree for retry: {exc}"
                        ) from exc
                    self.store.update_card(
                        card.id,
                        worktree_branch=wt_info_new.branch,
                        worktree_base_commit=wt_info_new.base_commit,
                    )
                    new_claim.worktree_path = str(wt_info_new.path)
                elif wt_info is None:
                    new_claim.worktree_path = None
                    raise WorktreeMissingError(
                        f"worktree branch {card.worktree_branch} missing; "
                        f"cannot retry {previous.role.value} role"
                    )

        self.store.clear_claim(previous.card_id, claim_id=previous.claim_id)
        try:
            self.store.create_claim(new_claim)
        except Exception:
            # Rollback: restore the old claim so the card isn't stranded.
            try:
                self.store.create_claim(previous)
            except Exception:  # noqa: BLE001 — best-effort rollback
                pass
            raise

        self.store.append_runtime_event(
            new_claim.card_id,
            event_type=ExecutionEventType.RETRIED.value,
            message=f"retry attempt {new_claim.attempt} after {category.value}: {reason}",
            role=new_claim.role,
            claim_id=new_claim.claim_id,
            attempt=new_claim.attempt,
            failure_category=category.value,
            failure_reason=reason,
            retry_of_claim_id=previous.claim_id,
        )
        return new_claim

    def retry_or_block(
        self,
        failed_claim: ExecutionClaim,
        category: FailureCategory,
        reason: str,
    ) -> None:
        """Apply retry matrix: either link a new claim or BLOCK the card.

        Invariant: a card is never left in an execution status (DOING /
        REVIEW) without either a live claim or a completed
        terminal transition to BLOCKED. Both paths below enforce this:

        - Retry: :meth:`retry_claim` clears → creates with rollback on
          failure (old claim restored).
        - Block: move the card to BLOCKED *first*, then clear the claim.
          If ``update_card`` or ``move_card`` raises, the claim is still
          live and the next scheduler tick can redo the recovery.
        """
        budget = self.retry_policy.budget_for(category)
        retries_used = failed_claim.retry_count
        if retries_used < budget:
            try:
                self.retry_claim(failed_claim, reason=reason, category=category)
                return
            except WorktreeMissingError as exc:
                # Branch was deleted and cannot be recovered — fall through
                # to BLOCKED instead of running in the main checkout.
                reason = f"{reason} ({exc})"

        # Exhausted — terminal BLOCKED transition FIRST (card status moves
        # out of any execution state), THEN clear the claim. If the move
        # fails, the old claim is preserved so recovery can retry.
        block_reason = (
            f"{reason} [category={category.value} attempts={failed_claim.attempt}]"
        )
        block_card(self.store, self.worktree_mgr, failed_claim.card_id, block_reason)
        self.store.clear_claim(
            failed_claim.card_id, claim_id=failed_claim.claim_id
        )
