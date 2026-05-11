from __future__ import annotations

from ..models import (
    AgentResult,
    Card,
    ExecutionClaim,
    ExecutionEventType,
    ExecutionResultEnvelope,
    FailureCategory,
    ResourceUsage,
    utc_now,
)
from .component import OrchestratorComponent


class ResultCommitter(OrchestratorComponent):
    def apply_claim_result(
        self, claim: ExecutionClaim, result: AgentResult
    ) -> Card:
        """Legacy single-process commit path (used by ``tick()`` and
        ``--role legacy-serial``).

        In the PR2/PR3 split topology, workers call :meth:`submit_result`
        instead and the scheduler/committer picks the envelope up via
        :meth:`commit_pending_results`. This method remains so the serial
        path does not incur the envelope round-trip.
        """
        self._apply_result(claim.card_id, result)
        self.store.clear_claim(claim.card_id, claim_id=claim.claim_id)
        return self.store.get_card(claim.card_id)

    def submit_result(
        self,
        claim: ExecutionClaim,
        result: AgentResult | None,
        *,
        worker_id: str,
        started_at,
        finished_at=None,
        ok: bool = True,
        failure_reason: str | None = None,
        failure_category: FailureCategory | None = None,
        resource_usage: ResourceUsage | None = None,
    ) -> ExecutionResultEnvelope:
        """Worker step: persist the executor outcome as an envelope.

        Emits **no** runtime events. An envelope's authenticity cannot be
        verified until :meth:`commit_pending_results` (scheduler-side)
        validates the claim_id + worker_id match. Authoritative
        ``execution.finished`` / ``execution.failed`` events are emitted
        there, after ownership checks pass. A forged envelope is
        quarantined and logged as ``execution.result_orphaned`` — it
        cannot leave a success/failure entry in the trusted audit trail.

        The worker MUST NOT move the card or clear the claim after calling
        this; the committer is the sole writer of post-execution
        workflow status transitions.
        """
        finished = finished_at or utc_now()
        duration_ms = int((finished - started_at).total_seconds() * 1000)
        envelope = ExecutionResultEnvelope(
            card_id=claim.card_id,
            claim_id=claim.claim_id,
            role=claim.role,
            attempt=claim.attempt,
            started_at=started_at,
            finished_at=finished,
            duration_ms=duration_ms,
            ok=ok,
            agent_result=result,
            worker_id=worker_id,
            failure_reason=failure_reason,
            failure_category=failure_category,
            resource_usage=resource_usage,
        )
        self.store.write_result(envelope)
        return envelope

    def commit_pending_results(self) -> int:
        """Scheduler/committer: apply any pending result envelopes.

        An envelope is accepted only when all three ownership checks pass:

        1. the envelope's card has a live claim;
        2. the live claim's ``claim_id`` matches the envelope;
        3. the live claim has been acquired (``worker_id is not None``) AND
           the envelope's ``worker_id`` equals the claim's owner.

        Any failure quarantines the envelope as an orphan — a second worker
        that forged a submission under the wrong identity cannot coerce the
        committer into writing its result into the board. Returns the number
        of envelopes processed (committed + quarantined).
        """
        processed = 0
        for env in self.store.read_results():
            # Defense-in-depth: if the card was deleted externally, drop the
            # envelope + any live claim rather than crashing in _apply_result.
            try:
                self.store.get_card(env.card_id)
            except KeyError:
                try:
                    self.store.delete_result(env.card_id, env.claim_id)
                    existing = self.store.get_claim(env.card_id)
                    if existing is not None:
                        self.store.clear_claim(env.card_id, claim_id=existing.claim_id)
                except Exception:  # noqa: BLE001
                    pass
                processed += 1
                continue
            claim = self.store.get_claim(env.card_id)
            if claim is None or claim.claim_id != env.claim_id:
                self._emit_orphan(env, claim, reason="claim_id mismatch")
                self.store.quarantine_result(env.card_id, env.claim_id)
                processed += 1
                continue
            if claim.worker_id is None or claim.worker_id != env.worker_id:
                self._emit_orphan(
                    env,
                    claim,
                    reason=(
                        f"worker_id mismatch (envelope={env.worker_id!r}, "
                        f"claim={claim.worker_id!r})"
                    ),
                )
                self.store.quarantine_result(env.card_id, env.claim_id)
                processed += 1
                continue
            # Ownership verified — emit the authoritative lifecycle event
            # BEFORE applying side effects so the audit trail reflects the
            # trusted outcome even if apply/block transitions fail later.
            event_type = (
                ExecutionEventType.FINISHED.value
                if env.ok
                else ExecutionEventType.FAILED.value
            )
            self.store.append_runtime_event(
                env.card_id,
                event_type=event_type,
                message=(
                    env.failure_reason
                    if env.failure_reason
                    else (
                        env.agent_result.summary
                        if env.agent_result is not None
                        else "executor finished"
                    )
                ),
                role=env.role,
                claim_id=env.claim_id,
                worker_id=env.worker_id,
                attempt=env.attempt,
                duration_ms=env.duration_ms,
                failure_reason=env.failure_reason,
                failure_category=(
                    env.failure_category.value
                    if env.failure_category is not None
                    else None
                ),
            )
            if env.ok and env.agent_result is not None:
                self._apply_result(env.card_id, env.agent_result)
                self.store.clear_claim(env.card_id, claim_id=env.claim_id)
            else:
                self._handle_failed_envelope(env, claim)
            self.store.delete_result(env.card_id, env.claim_id)
            processed += 1
        return processed

    def _handle_failed_envelope(
        self, env: ExecutionResultEnvelope, claim: ExecutionClaim
    ) -> None:
        category = env.failure_category or FailureCategory.INFRASTRUCTURE
        reason = env.failure_reason or "executor reported failure"
        # ``_retry_or_block`` owns the clear-old-claim step; it keeps the
        # claim alive until the replacement state is durable.
        self._retry_or_block(claim, category, reason)

    def _emit_orphan(
        self,
        env: ExecutionResultEnvelope,
        live_claim: ExecutionClaim | None,
        *,
        reason: str,
    ) -> None:
        live_id = live_claim.claim_id if live_claim is not None else None
        self.store.append_runtime_event(
            env.card_id,
            event_type=ExecutionEventType.RESULT_ORPHANED.value,
            message=(
                f"envelope for claim={env.claim_id} rejected "
                f"({reason}); live_claim={live_id or '<none>'}"
            ),
            role=env.role,
            claim_id=env.claim_id,
            worker_id=env.worker_id,
            attempt=env.attempt,
            failure_reason=reason,
        )
