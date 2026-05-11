from __future__ import annotations

from ..models import AgentRole, Card, CardStatus, ExecutionClaim
from .claims import build_execution_claim
from .component import OrchestratorComponent
from .helpers import _WIP_STATUSES


class Scheduler(OrchestratorComponent):
    def select_and_claim(self, worker_id: str | None = None) -> ExecutionClaim | None:
        """Scheduler step: pick the next actionable card and create a claim.

        If `worker_id` is None the claim is created unassigned (the open-
        questions decision). A worker daemon later calls
        :meth:`BoardStore.try_acquire_claim` to take ownership. The legacy
        serial path passes `worker_id="local-serial"` so the returned claim
        is immediately owned.

        Walks actionable cards in priority order and skips any that
        already have a live claim — one claimed front card must not
        prevent the scheduler from filling remaining ``max_claims``
        capacity behind it. Cards that fail worktree setup are moved to
        BLOCKED and the scan continues to the next candidate, so a single
        bad card cannot stall lower-priority work in the same tick.
        Returns ``None`` only when *every* actionable card is already
        claimed, blocked, or there is no actionable card at all.
        """
        for candidate in self._iter_actionable_cards():
            if self.store.get_claim(candidate.id) is not None:
                continue

            role = self._role_for(candidate)

            claim = build_execution_claim(
                card=candidate,
                role=role,
                lease_seconds=self.lease_policy.lease_seconds,
                timeout_s=self.lease_policy.timeout_for(role),
                worker_id=worker_id,
            )

            if not self._setup_worktree_for_claim(candidate, role, claim):
                # Card was blocked by worktree setup; try the next candidate
                # so one broken card does not stall the rest of the queue.
                continue

            # Persist the claim BEFORE moving status. If create_claim raises
            # (stale sentinel, fs error, duplicate), the card stays in READY —
            # the scheduler will simply retry on the next tick. Only after the
            # claim is safely on disk do we advance the card, so we can never
            # leave a DOING card without a live claim.
            self.store.create_claim(claim)
            if candidate.status == CardStatus.READY:
                try:
                    self.store.move_card(
                        candidate.id, CardStatus.DOING,
                        "Dispatcher moved card to doing",
                    )
                except Exception:
                    # Roll back the claim so the next tick can retry cleanly.
                    try:
                        self.store.clear_claim(
                            candidate.id, claim_id=claim.claim_id,
                        )
                    except Exception:  # noqa: BLE001 — best-effort rollback
                        pass
                    raise
            return claim
        return None

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
          1. Finish-what-you-started: REVIEW.
          2. If WIP budget allows, READY (pulled into DOING on claim).
          3. Planning: INBOX (does not count against WIP).

        ``select_and_claim`` iterates this generator and skips candidates
        that already have a live claim, so one claimed front card cannot
        starve the scheduler's remaining capacity.
        """
        yield from self._ready_cards(CardStatus.REVIEW)

        if self._wip_count() < self.wip_policy.doing_limit:
            yield from self._ready_cards(CardStatus.READY)

        yield from self._ready_cards(CardStatus.INBOX)

    def _role_for(self, card: Card) -> AgentRole:
        mapping = {
            CardStatus.INBOX: AgentRole.PLANNER,
            CardStatus.READY: AgentRole.WORKER,
            CardStatus.REVIEW: AgentRole.REVIEWER,
        }
        return mapping[card.status]
