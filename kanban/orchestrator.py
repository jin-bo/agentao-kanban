from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from .executors.base import CardExecutor
from .models import (
    AgentResult,
    AgentRole,
    Card,
    CardPriority,
    CardStatus,
    ExecutionClaim,
    ExecutionEventType,
    ExecutionResultEnvelope,
    FailureCategory,
    LeasePolicy,
    ResourceUsage,
    RetryPolicy,
    utc_now,
)
from .store import BoardStore


@dataclass(slots=True)
class WipPolicy:
    doing_limit: int = 2


_WIP_STATUSES = (CardStatus.DOING, CardStatus.REVIEW, CardStatus.VERIFY)


class KanbanOrchestrator:
    def __init__(
        self,
        store: BoardStore,
        executor: CardExecutor,
        wip_policy: WipPolicy | None = None,
        lease_policy: LeasePolicy | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self.store = store
        self.executor = executor
        self.wip_policy = wip_policy or WipPolicy()
        self.lease_policy = lease_policy or LeasePolicy()
        self.retry_policy = retry_policy or RetryPolicy()

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
        self.store.update_card(card_id, blocked_reason=reason)
        return self.store.move_card(card_id, CardStatus.BLOCKED, f"Blocked: {reason}")

    def unblock(self, card_id: str, target: CardStatus = CardStatus.INBOX) -> Card:
        self.store.update_card(card_id, blocked_reason=None)
        return self.store.move_card(card_id, target, f"Unblocked to {target.value}")

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
        result = self.executor.run(claim.role, card)
        self.apply_claim_result(claim, result)
        return self.store.get_card(claim.card_id)

    # ---------- v0.1.2 scheduler / worker split ----------

    def select_and_claim(self, worker_id: str | None = None) -> ExecutionClaim | None:
        """Scheduler step: pick the next actionable card and create a claim.

        If `worker_id` is None the claim is created unassigned (the open-
        questions decision). A worker daemon later calls
        :meth:`BoardStore.try_acquire_claim` to take ownership. The legacy
        serial path passes `worker_id="local"` so the returned claim is
        immediately owned.

        Walks actionable cards in priority order and skips any that
        already have a live claim — one claimed front card must not
        prevent the scheduler from filling remaining ``max_claims``
        capacity behind it. Returns ``None`` only when *every* actionable
        card is already claimed or there is no actionable card at all.
        """
        card: Card | None = None
        for candidate in self._iter_actionable_cards():
            if self.store.get_claim(candidate.id) is None:
                card = candidate
                break
        if card is None:
            return None

        role = self._role_for(card)

        # Status-at-claim reflects where the worker will see the card.
        # READY → DOING at claim time so scheduler owns all workflow-status
        # transitions into executable states.
        status_at_claim = (
            CardStatus.DOING if card.status == CardStatus.READY else card.status
        )

        now = utc_now()
        lease_expires = now + timedelta(seconds=self.lease_policy.lease_seconds)
        claim = ExecutionClaim(
            card_id=card.id,
            claim_id=f"clm-{uuid4().hex[:12]}",
            role=role,
            status_at_claim=status_at_claim,
            attempt=1,
            claimed_at=now,
            heartbeat_at=now,
            lease_expires_at=lease_expires,
            timeout_s=self.lease_policy.timeout_for(role),
            worker_id=worker_id,
        )

        # Persist the claim BEFORE moving status. If create_claim raises
        # (stale sentinel, fs error, duplicate), the card stays in READY —
        # the scheduler will simply retry on the next tick. Only after the
        # claim is safely on disk do we advance the card, so we can never
        # leave a DOING card without a live claim.
        self.store.create_claim(claim)
        if card.status == CardStatus.READY:
            try:
                self.store.move_card(
                    card.id, CardStatus.DOING, "Dispatcher moved card to doing"
                )
            except Exception:
                # Roll back the claim so the next tick can retry cleanly.
                try:
                    self.store.clear_claim(card.id, claim_id=claim.claim_id)
                except Exception:  # noqa: BLE001 — best-effort rollback
                    pass
                raise
        return claim

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

    # ---------- v0.1.2 commit path (PR3/M2) ----------

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
            self._retry_or_block(claim, FailureCategory.LEASE_EXPIRY, reason)
        return len(stale)

    # ---------- retry matrix ----------

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
        from uuid import uuid4

        now = utc_now()
        lease_expires = now + timedelta(seconds=self.lease_policy.lease_seconds)
        new_claim = ExecutionClaim(
            card_id=previous.card_id,
            claim_id=f"clm-{uuid4().hex[:12]}",
            role=previous.role,
            status_at_claim=previous.status_at_claim,
            attempt=previous.attempt + 1,
            claimed_at=now,
            heartbeat_at=now,
            lease_expires_at=lease_expires,
            timeout_s=previous.timeout_s,
            worker_id=None,
            retry_count=previous.retry_count + 1,
            retry_of_claim_id=previous.claim_id,
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

    def _handle_failed_envelope(
        self, env: ExecutionResultEnvelope, claim: ExecutionClaim
    ) -> None:
        category = env.failure_category or FailureCategory.INFRASTRUCTURE
        reason = env.failure_reason or "executor reported failure"
        # ``_retry_or_block`` owns the clear-old-claim step; it keeps the
        # claim alive until the replacement state is durable.
        self._retry_or_block(claim, category, reason)

    def _retry_or_block(
        self,
        failed_claim: ExecutionClaim,
        category: FailureCategory,
        reason: str,
    ) -> None:
        """Apply retry matrix: either link a new claim or BLOCK the card.

        Invariant: a card is never left in an execution status (DOING /
        REVIEW / VERIFY) without either a live claim or a completed
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
            self.retry_claim(failed_claim, reason=reason, category=category)
            return

        # Exhausted — terminal BLOCKED transition FIRST (card status moves
        # out of any execution state), THEN clear the claim. If the move
        # fails, the old claim is preserved so recovery can retry.
        block_reason = (
            f"{reason} [category={category.value} attempts={failed_claim.attempt}]"
        )
        self.store.update_card(failed_claim.card_id, blocked_reason=block_reason)
        self.store.move_card(
            failed_claim.card_id, CardStatus.BLOCKED, f"Blocked: {block_reason}"
        )
        self.store.clear_claim(
            failed_claim.card_id, claim_id=failed_claim.claim_id
        )

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

    def run_until_idle(self, max_steps: int = 20) -> list[Card]:
        processed: list[Card] = []
        for _ in range(max_steps):
            card = self.tick()
            if card is None:
                break
            processed.append(card)
        return processed

    def _wip_count(self) -> int:
        return sum(len(self.store.list_by_status(s)) for s in _WIP_STATUSES)

    def _deps_satisfied(self, card: Card) -> bool:
        for dep_id in card.depends_on:
            try:
                dep = self.store.get_card(dep_id)
            except KeyError:
                return False
            if dep.status != CardStatus.DONE:
                return False
        return True

    def _ready_cards(self, status: CardStatus):
        """Yield all dependency-satisfied cards in ``status`` (priority order)."""
        for card in self.store.list_by_status(status):
            if self._deps_satisfied(card):
                yield card

    def _first_ready(self, status: CardStatus) -> Card | None:
        return next(iter(self._ready_cards(status)), None)

    def _next_actionable_card(self) -> Card | None:
        return next(iter(self._iter_actionable_cards()), None)

    def _iter_actionable_cards(self):
        """Yield every actionable card in scheduler priority order.

        Order:
          1. Finish-what-you-started: VERIFY, then REVIEW.
          2. If WIP budget allows, READY (pulled into DOING on claim).
          3. Planning: INBOX (does not count against WIP).

        ``select_and_claim`` iterates this generator and skips candidates
        that already have a live claim, so one claimed front card cannot
        starve the scheduler's remaining capacity.
        """
        for status in (CardStatus.VERIFY, CardStatus.REVIEW):
            yield from self._ready_cards(status)

        if self._wip_count() < self.wip_policy.doing_limit:
            yield from self._ready_cards(CardStatus.READY)

        yield from self._ready_cards(CardStatus.INBOX)

    def _role_for(self, card: Card) -> AgentRole:
        mapping = {
            CardStatus.INBOX: AgentRole.PLANNER,
            CardStatus.READY: AgentRole.WORKER,
            CardStatus.REVIEW: AgentRole.REVIEWER,
            CardStatus.VERIFY: AgentRole.VERIFIER,
        }
        return mapping[card.status]

    def _apply_result(self, card_id: str, result: AgentResult) -> None:
        card = self.store.update_card(card_id, **result.updates)
        card.add_history(result.summary, role=result.role)
        self.store.append_execution_event(card_id, result)
        self.store.move_card(
            card_id,
            result.next_status,
            f"Status changed to {result.next_status.value}",
        )
