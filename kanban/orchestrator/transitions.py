from __future__ import annotations

from ..models import AgentResult, AgentRole, CardStatus, RevisionRequest
from .component import OrchestratorComponent
from .helpers import advance_inbox_dependents
from .terminal import detach_worktree_on_terminal


class ResultTransitioner(OrchestratorComponent):
    def apply_result(self, card_id: str, result: AgentResult) -> None:
        # Reviewer/verifier rework takes a dedicated path so the worktree
        # stays attached and ``rework_iteration`` / ``revision_requests``
        # stay internally consistent. A terminal rework exhaustion produces
        # a synthetic BLOCKED result and delegates back to the normal path.
        if result.revision_request is not None:
            self._apply_rework(card_id, result)
            return
        self._apply_normal_result(card_id, result)

    def _apply_normal_result(self, card_id: str, result: AgentResult) -> None:
        previous_status = self.store.get_card(card_id).status
        card = self.store.update_card(card_id, **result.updates)
        card.add_history(result.summary, role=result.role)
        self.store.append_execution_event(card_id, result)
        self.store.move_card(
            card_id,
            result.next_status,
            f"Status changed to {result.next_status.value}",
        )
        # First-time transition to DONE fans out to any INBOX card whose
        # depends_on is now fully satisfied. Guard on the pre-update status
        # so a DONE card replayed into DONE (idempotent commit) does not
        # re-emit dependency-advance events.
        if (
            result.next_status == CardStatus.DONE
            and previous_status != CardStatus.DONE
        ):
            advance_inbox_dependents(self.store, card_id)
        # Detach on any terminal transition so a reviewer/verifier rejection
        # (next_status=BLOCKED) doesn't leave workspace/worktrees/<card>
        # attached forever — prune_stale() skips cards whose directory
        # still exists, so those branches would otherwise accumulate.
        detach_worktree_on_terminal(
            self.store, self.worktree_mgr, card_id, result.next_status,
        )

    def _apply_rework(self, card_id: str, result: AgentResult) -> None:
        """Handle a reviewer/verifier revision request.

        Accepts up to ``retry_policy.rework`` reworks per card. Each accepted
        rework appends to ``card.revision_requests``, bumps
        ``card.rework_iteration``, and moves the card REVIEW → READY
        so the worker is re-dispatched on the next scheduler tick. The
        worktree stays attached — the worker picks up where it left off.

        Budget exhaustion synthesizes a BLOCKED ``AgentResult`` and delegates
        to :meth:`_apply_normal_result` so the standard detach + event path
        still runs.
        """
        assert result.revision_request is not None  # dispatcher guarantees this
        req = result.revision_request
        card = self.store.get_card(card_id)
        next_iter = card.rework_iteration + 1
        budget = int(self.retry_policy.rework)

        if next_iter > budget:
            # Budget exhausted — block the card. Still record this last
            # revision request for postmortem so the operator sees the
            # final ask that tipped it over.
            stamped = RevisionRequest(
                at=req.at,
                from_role=req.from_role,
                iteration=next_iter,
                summary=req.summary,
                hints=list(req.hints),
                failing_criteria=list(req.failing_criteria),
            )
            new_requests = list(card.revision_requests) + [stamped]
            self.store.update_card(
                card_id,
                revision_requests=new_requests,
            )
            reason = (
                f"rework budget exhausted ({budget} iterations). "
                f"Last ask from {req.from_role.value}: {req.summary}"
            )
            blocked = AgentResult(
                role=result.role,
                summary=(
                    f"{result.role.value} exhausted rework budget "
                    f"({budget} iterations)"
                ),
                next_status=CardStatus.BLOCKED,
                updates={"blocked_reason": reason, "owner_role": None},
                prompt_version=result.prompt_version,
                duration_ms=result.duration_ms,
                attempt=result.attempt,
                raw_response=result.raw_response,
                agent_profile=result.agent_profile,
                backend_type=result.backend_type,
                backend_target=result.backend_target,
                routing_source=result.routing_source,
                routing_reason=result.routing_reason,
                fallback_from_profile=result.fallback_from_profile,
                session_id=result.session_id,
                router_prompt_version=result.router_prompt_version,
                backend_metadata=dict(result.backend_metadata),
            )
            self._apply_normal_result(card_id, blocked)
            return

        # Accept rework: stamp iteration, append, bump counter, rewind to READY.
        stamped = RevisionRequest(
            at=req.at,
            from_role=req.from_role,
            iteration=next_iter,
            summary=req.summary,
            hints=list(req.hints),
            failing_criteria=list(req.failing_criteria),
        )
        new_requests = list(card.revision_requests) + [stamped]
        updates = dict(result.updates)
        updates["revision_requests"] = new_requests
        updates["rework_iteration"] = next_iter
        updates["owner_role"] = AgentRole.WORKER
        card = self.store.update_card(card_id, **updates)
        card.add_history(
            f"rework requested (iteration {next_iter}/{budget}): {req.summary}",
            role=result.role,
        )
        self.store.append_execution_event(card_id, result)
        self.store.append_runtime_event(
            card_id,
            event_type="rework.requested",
            message=(
                f"iteration {next_iter}/{budget} by {req.from_role.value}: "
                f"{req.summary}"
            ),
            role=req.from_role,
            rework_iteration=next_iter,
            worktree_branch=card.worktree_branch,
        )
        self.store.move_card(
            card_id,
            CardStatus.READY,
            (
                f"Rework iteration {next_iter}/{budget} requested by "
                f"{req.from_role.value}"
            ),
        )
        # The router cache key is built from card fields the router sees
        # (title/goal/acceptance/context_refs/...) and ignores rework
        # state, so the next worker dispatch would otherwise reuse the
        # pre-rework profile. Bust the entry so the new revision_requests
        # / rework_iteration trigger a fresh routing decision.
        self._invalidate_router_cache(card_id)

    def _invalidate_router_cache(self, card_id: str) -> None:
        """Best-effort: drop cached router decisions for ``card_id``.

        Decoupled via duck-typing on ``executor.policy.invalidate_card``
        so executors without a router policy (mock, agentao_multi,
        custom) need no extra surface.
        """
        policy = getattr(self.executor, "policy", None)
        if policy is None:
            return
        invalidate = getattr(policy, "invalidate_card", None)
        if callable(invalidate):
            invalidate(card_id)
