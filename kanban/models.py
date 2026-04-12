from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum, StrEnum
from typing import Any
from uuid import uuid4


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class CardStatus(StrEnum):
    INBOX = "inbox"
    READY = "ready"
    DOING = "doing"
    REVIEW = "review"
    VERIFY = "verify"
    DONE = "done"
    BLOCKED = "blocked"


class CardPriority(IntEnum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4


class AgentRole(StrEnum):
    PLANNER = "planner"
    WORKER = "worker"
    REVIEWER = "reviewer"
    VERIFIER = "verifier"


@dataclass(slots=True)
class Card:
    title: str
    goal: str
    acceptance_criteria: list[str] = field(default_factory=list)
    priority: CardPriority = CardPriority.MEDIUM
    status: CardStatus = CardStatus.INBOX
    id: str = field(default_factory=lambda: str(uuid4()))
    owner_role: AgentRole | None = None
    blocked_reason: str | None = None
    context_refs: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    outputs: dict[str, Any] = field(default_factory=dict)
    history: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def add_history(self, message: str) -> None:
        self.history.append(message)
        self.updated_at = utc_now()


@dataclass(slots=True)
class CardEvent:
    card_id: str
    message: str
    at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class AgentResult:
    role: AgentRole
    summary: str
    next_status: CardStatus
    updates: dict[str, Any] = field(default_factory=dict)
    prompt_version: str = ""
    duration_ms: int = 0
    attempt: int = 1
    raw_response: str | None = None
