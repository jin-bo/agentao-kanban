from __future__ import annotations

from typing import Protocol

from ..models import AgentResult, AgentRole, Card


class CardExecutor(Protocol):
    def run(self, role: AgentRole, card: Card) -> AgentResult:
        """Execute one role against one card and return the workflow outcome."""
