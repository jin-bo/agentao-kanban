from __future__ import annotations

from collections import defaultdict
from typing import Protocol, runtime_checkable

from .models import AgentResult, Card, CardEvent, CardStatus, utc_now


@runtime_checkable
class BoardStore(Protocol):
    def add_card(self, card: Card) -> Card: ...
    def get_card(self, card_id: str) -> Card: ...
    def list_cards(self) -> list[Card]: ...
    def list_by_status(self, status: CardStatus) -> list[Card]: ...
    def move_card(self, card_id: str, status: CardStatus, note: str) -> Card: ...
    def update_card(self, card_id: str, **updates: object) -> Card: ...
    def append_event(self, card_id: str, message: str) -> None: ...
    def append_execution_event(self, card_id: str, result: AgentResult) -> None: ...
    def events_for_card(self, card_id: str) -> list[CardEvent]: ...
    def board_snapshot(self) -> dict[str, list[str]]: ...


class InMemoryBoardStore:
    def __init__(self) -> None:
        self._cards: dict[str, Card] = {}
        self._events: list[CardEvent] = []

    def add_card(self, card: Card) -> Card:
        self._cards[card.id] = card
        self.append_event(card.id, f"Card created in {card.status.value}")
        return card

    def get_card(self, card_id: str) -> Card:
        return self._cards[card_id]

    def list_cards(self) -> list[Card]:
        return list(self._cards.values())

    def list_by_status(self, status: CardStatus) -> list[Card]:
        cards = [card for card in self._cards.values() if card.status == status]
        return sorted(cards, key=lambda card: (-int(card.priority), card.created_at))

    def move_card(self, card_id: str, status: CardStatus, note: str) -> Card:
        card = self.get_card(card_id)
        card.status = status
        card.add_history(note)
        self.append_event(card_id, note)
        return card

    def update_card(self, card_id: str, **updates: object) -> Card:
        card = self.get_card(card_id)
        for key, value in updates.items():
            setattr(card, key, value)
        card.updated_at = utc_now()
        return card

    def append_event(self, card_id: str, message: str) -> None:
        self._events.append(CardEvent(card_id=card_id, message=message))

    def append_execution_event(self, card_id: str, result: AgentResult) -> None:
        self.append_event(card_id, f"{result.role.value}: {result.summary}")

    def events_for_card(self, card_id: str) -> list[CardEvent]:
        return [event for event in self._events if event.card_id == card_id]

    def board_snapshot(self) -> dict[str, list[str]]:
        grouped: dict[str, list[str]] = defaultdict(list)
        for card in self.list_cards():
            grouped[card.status.value].append(card.title)
        return dict(grouped)
