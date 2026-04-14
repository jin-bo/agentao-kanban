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
    LeasePolicy,
    ResourceUsage,
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
    ) -> None:
        self.store = store
        self.executor = executor
        self.wip_policy = wip_policy or WipPolicy()
        self.lease_policy = lease_policy or LeasePolicy()

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

    def tick(self) -> Card | None:
        """Legacy serial path: select → claim → execute → apply in one process.

        Preserved for `kanban daemon --role legacy-serial` and the existing
        `kanban tick` / `kanban run` CLI commands. The v0.1.2 split uses
        :meth:`select_and_claim` + :meth:`apply_claim_result` instead.
        """
        claim = self.select_and_claim(worker_id=None)
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

        Returns None if no card is actionable or if creating a new claim
        would exceed `wip_policy.doing_limit`.
        """
        card = self._next_actionable_card()
        if card is None:
            return None
        # Skip any card that already has a live claim (scheduler-only writer
        # to the claim namespace, so this is sufficient without fcntl).
        if self.store.get_claim(card.id) is not None:
            return None

        role = self._role_for(card)

        # Transition ready → doing at claim time so scheduler owns all
        # workflow-status transitions into executable states. Post-execution
        # transitions stay in apply_claim_result (PR3 moves that into a
        # dedicated committer).
        if card.status == CardStatus.READY:
            self.store.move_card(
                card.id, CardStatus.DOING, "Dispatcher moved card to doing"
            )
            status_at_claim = CardStatus.DOING
        else:
            status_at_claim = card.status

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
        self.store.create_claim(claim)
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
        resource_usage: ResourceUsage | None = None,
    ) -> ExecutionResultEnvelope:
        """Worker step: persist the executor outcome as an envelope.

        The worker MUST NOT move the card or clear the claim after calling
        this; :meth:`commit_pending_results` (scheduler-side) is the sole
        writer of post-execution workflow status transitions.
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
            resource_usage=resource_usage,
        )
        self.store.write_result(envelope)
        outcome = (
            ExecutionEventType.FINISHED.value if ok else ExecutionEventType.FAILED.value
        )
        self.store.append_event(
            claim.card_id,
            f"[{outcome}] claim={claim.claim_id} worker={worker_id} "
            f"attempt={claim.attempt} duration_ms={duration_ms}"
            + (f" reason={failure_reason}" if failure_reason else ""),
        )
        return envelope

    def commit_pending_results(self) -> int:
        """Scheduler/committer: apply any pending result envelopes.

        For each envelope: verify it references the current live claim
        (``claim_id`` match). On match — apply the result and clear the
        claim. On mismatch or missing claim — quarantine as an orphan and
        emit ``execution.result_orphaned``. Returns the number committed.
        """
        committed = 0
        for env in self.store.read_results():
            claim = self.store.get_claim(env.card_id)
            if claim is None or claim.claim_id != env.claim_id:
                self._emit_orphan(env, claim)
                self.store.quarantine_result(env.card_id, env.attempt)
                continue
            if env.ok and env.agent_result is not None:
                self._apply_result(env.card_id, env.agent_result)
            else:
                reason = env.failure_reason or "executor reported failure"
                self.store.update_card(env.card_id, blocked_reason=reason)
                self.store.move_card(
                    env.card_id, CardStatus.BLOCKED, f"Blocked: {reason}"
                )
            self.store.clear_claim(env.card_id, claim_id=env.claim_id)
            self.store.delete_result(env.card_id, env.attempt)
            committed += 1
        return committed

    def recover_stale_claims(self, *, now=None) -> int:
        """Scheduler: move cards with expired leases to BLOCKED and clear claims.

        PR4 will add retries; here we deterministically block so the operator
        can requeue. Returns the number of claims recovered.
        """
        stale = self.store.list_stale_claims(now=now or utc_now())
        for claim in stale:
            reason = (
                f"runtime lease expired on attempt {claim.attempt} "
                f"(role={claim.role.value}, claim={claim.claim_id})"
            )
            self.store.update_card(claim.card_id, blocked_reason=reason)
            self.store.move_card(
                claim.card_id, CardStatus.BLOCKED, f"Blocked: {reason}"
            )
            self.store.clear_claim(claim.card_id, claim_id=claim.claim_id)
            self.store.append_event(
                claim.card_id,
                f"[{ExecutionEventType.CLAIM_RECOVERED.value}] "
                f"claim={claim.claim_id} role={claim.role.value} "
                f"attempt={claim.attempt}",
            )
        return len(stale)

    def _emit_orphan(
        self, env: ExecutionResultEnvelope, live_claim: ExecutionClaim | None
    ) -> None:
        live_id = live_claim.claim_id if live_claim is not None else "<none>"
        self.store.append_event(
            env.card_id,
            f"[{ExecutionEventType.RESULT_ORPHANED.value}] "
            f"envelope_claim={env.claim_id} live_claim={live_id} "
            f"attempt={env.attempt} worker={env.worker_id}",
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

    def _first_ready(self, status: CardStatus) -> Card | None:
        for card in self.store.list_by_status(status):
            if self._deps_satisfied(card):
                return card
        return None

    def _next_actionable_card(self) -> Card | None:
        # Work already in the pipeline first (finish what you started).
        for status in (CardStatus.VERIFY, CardStatus.REVIEW):
            card = self._first_ready(status)
            if card is not None:
                return card

        wip_count = self._wip_count()
        if wip_count < self.wip_policy.doing_limit:
            ready = self._first_ready(CardStatus.READY)
            if ready is not None:
                return ready

        # Planning (INBOX) does not count against WIP — only executing work does.
        return self._first_ready(CardStatus.INBOX)

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
