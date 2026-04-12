from __future__ import annotations

from dataclasses import dataclass

from .executors.base import CardExecutor
from .models import AgentResult, AgentRole, Card, CardPriority, CardStatus
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
    ) -> None:
        self.store = store
        self.executor = executor
        self.wip_policy = wip_policy or WipPolicy()

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
        card = self._next_actionable_card()
        if card is None:
            return None

        role = self._role_for(card)
        if card.status == CardStatus.READY:
            self.store.move_card(card.id, CardStatus.DOING, "Dispatcher moved card to doing")

        result = self.executor.run(role, self.store.get_card(card.id))
        self._apply_result(card.id, result)
        return self.store.get_card(card.id)

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
        card.add_history(f"{result.role.value}: {result.summary}")
        self.store.append_execution_event(card_id, result)
        self.store.move_card(
            card_id,
            result.next_status,
            f"Status changed to {result.next_status.value}",
        )
