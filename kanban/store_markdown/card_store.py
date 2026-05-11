from __future__ import annotations

import os
import tomllib
from collections import defaultdict
from pathlib import Path

from ..models import (
    AgentRole,
    Card,
    CardEvent,
    CardStatus,
    ContextRef,
    coerce_card_status,
    utc_now,
)
from .cards import _read_card, _render_card
from .component import StoreComponent
from .events import _decode_event_line
from .store_utils import _LOG


class CardStore(StoreComponent):
    def refresh(self) -> None:
        """Reload cards/events so long-running daemons observe external edits."""
        self._cards = {}
        self._events = []
        self._unparseable = []
        self._load()

    def add_card(self, card: Card) -> Card:
        self._cards[card.id] = card
        self._write_card(card)
        self.append_event(card.id, f"Card created in {card.status.value}")
        return card

    def get_card(self, card_id: str) -> Card:
        return self._cards[card_id]

    def list_cards(self) -> list[Card]:
        return list(self._cards.values())

    def list_by_status(self, status: CardStatus) -> list[Card]:
        cards = [c for c in self._cards.values() if c.status == status]
        return sorted(cards, key=lambda c: (-int(c.priority), c.created_at))

    def move_card(self, card_id: str, status: CardStatus, note: str) -> Card:
        card = self.get_card(card_id)
        previous = card.status
        card.status = status
        if status == CardStatus.BLOCKED and previous != CardStatus.BLOCKED:
            card.blocked_at = utc_now()
        elif status != CardStatus.BLOCKED:
            card.blocked_at = None
        card.add_history(note, role="system")
        self._write_card(card)
        self.append_event(card_id, note)
        return card

    def update_card(self, card_id: str, **updates: object) -> Card:
        card = self.get_card(card_id)
        for key, value in updates.items():
            if key == "context_refs":
                value = [ContextRef.coerce(v) for v in value]  # type: ignore[arg-type]
            elif key == "owner_role" and isinstance(value, str):
                value = AgentRole(value)
            elif key == "status" and isinstance(value, str):
                value = coerce_card_status(value)
            setattr(card, key, value)
        card.updated_at = utc_now()
        self._write_card(card)
        return card

    def events_for_card(self, card_id: str) -> list[CardEvent]:
        return [e for e in self._events if e.card_id == card_id]

    def board_snapshot(self) -> dict[str, list[str]]:
        grouped: dict[str, list[str]] = defaultdict(list)
        for card in self.list_cards():
            grouped[card.status.value].append(card.title)
        return dict(grouped)

    def _card_path(self, card_id: str) -> Path:
        return self.cards_dir / f"{card_id}.md"

    def _load(self) -> None:
        for path in sorted(self.cards_dir.glob("*.md")):
            # Valid TOML can still miss fields the Card constructor requires
            # (TypeError) or reject a value type (KeyError/ValueError). Treat
            # any failure in this reader path as an unparseable card rather
            # than letting a single bad file break the whole board.
            try:
                card = _read_card(path)
            except (tomllib.TOMLDecodeError, TypeError, KeyError, ValueError) as exc:
                _LOG.warning("Skipping unparseable card %s: %s", path.name, exc)
                self._unparseable.append(path.name)
                continue
            self._cards[card.id] = card
        if self.events_path.exists():
            self._load_events()

    def unparseable_cards(self) -> list[str]:
        return list(self._unparseable)

    def _load_events(self) -> None:
        with self.events_path.open("r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                event = _decode_event_line(line)
                if event is not None:
                    self._events.append(event)

    def _write_card(self, card: Card) -> None:
        self.cards_dir.mkdir(parents=True, exist_ok=True)
        path = self._card_path(card.id)
        tmp = path.with_suffix(".md.tmp")
        content = _render_card(card)
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)
