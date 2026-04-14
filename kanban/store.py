from __future__ import annotations

from collections import defaultdict
from typing import Protocol, runtime_checkable

from .models import (
    AgentResult,
    AgentRole,
    Card,
    CardEvent,
    CardStatus,
    ContextRef,
    TraceInfo,
    utc_now,
)


def _tail(items: list, limit: int | None) -> list:
    """Return the last `limit` items. `None` → all; `<=0` → none."""
    if limit is None:
        return items
    if limit <= 0:
        return []
    return items[-limit:]


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
    def list_events(self, *, limit: int | None = ...) -> list[CardEvent]: ...
    def list_execution_events(
        self,
        *,
        card_id: str | None = ...,
        role: AgentRole | None = ...,
        limit: int | None = ...,
    ) -> list[CardEvent]: ...
    def list_traces(
        self,
        card_id: str,
        *,
        role: AgentRole | None = ...,
        latest: bool = ...,
    ) -> list[TraceInfo]: ...
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
        card.add_history(note, role="system")
        self.append_event(card_id, note)
        return card

    def update_card(self, card_id: str, **updates: object) -> Card:
        card = self.get_card(card_id)
        for key, value in updates.items():
            if key == "context_refs":
                value = [ContextRef.coerce(v) for v in value]  # type: ignore[arg-type]
            setattr(card, key, value)
        card.updated_at = utc_now()
        return card

    def append_event(self, card_id: str, message: str) -> None:
        self._events.append(CardEvent(card_id=card_id, message=message))

    def append_execution_event(self, card_id: str, result: AgentResult) -> None:
        self._events.append(
            CardEvent(
                card_id=card_id,
                message=result.summary,
                role=result.role,
                prompt_version=result.prompt_version,
                duration_ms=result.duration_ms,
                attempt=result.attempt,
            )
        )

    def events_for_card(self, card_id: str) -> list[CardEvent]:
        return [event for event in self._events if event.card_id == card_id]

    def list_events(self, *, limit: int | None = None) -> list[CardEvent]:
        return _tail(list(self._events), limit)

    def list_execution_events(
        self,
        *,
        card_id: str | None = None,
        role: AgentRole | None = None,
        limit: int | None = None,
    ) -> list[CardEvent]:
        events = [e for e in self._events if e.is_execution]
        if card_id is not None:
            events = [e for e in events if e.card_id == card_id]
        if role is not None:
            events = [e for e in events if e.role == role]
        return _tail(events, limit)

    def list_traces(
        self,
        card_id: str,
        *,
        role: AgentRole | None = None,
        latest: bool = False,
    ) -> list[TraceInfo]:
        # In-memory store has no raw transcripts.
        return []

    def board_snapshot(self) -> dict[str, list[str]]:
        grouped: dict[str, list[str]] = defaultdict(list)
        for card in self.list_cards():
            grouped[card.status.value].append(card.title)
        return dict(grouped)
