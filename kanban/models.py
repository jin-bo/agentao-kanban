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


CONTEXT_REF_KINDS = ("required", "optional")


@dataclass(slots=True)
class ContextRef:
    """Structured pointer to an external file the agent should read.

    ``kind`` is "required" or "optional". ``note`` is a one-line human hint.
    Plain strings are accepted on construction for backward-compat with the
    flat ``context_refs: list[str]`` format.

    ``__post_init__`` validates ``kind`` so *every* construction path (direct
    instantiation, ``coerce``, dataclass replace) fails fast on typos. Use
    :meth:`from_stored` to materialize a record read from disk whose ``kind``
    may have been corrupted — that path stays lenient so ``doctor`` can see
    and flag it.
    """

    path: str
    kind: str = "optional"
    note: str = ""

    def __post_init__(self) -> None:
        if self.kind not in CONTEXT_REF_KINDS:
            raise ValueError(
                f"ContextRef.kind must be one of {CONTEXT_REF_KINDS!r}, got {self.kind!r}"
            )

    @classmethod
    def from_stored(cls, *, path: str, kind: str, note: str) -> "ContextRef":
        """Load-path factory that bypasses `kind` validation.

        A persisted card file may carry a legacy or hand-edited ``kind``
        outside the allowed set; dropping the ref at load would hide it
        from ``doctor``. We keep the record intact and let the checks
        surface the problem.
        """
        obj = cls.__new__(cls)
        obj.path = path
        obj.kind = kind
        obj.note = note
        return obj

    @classmethod
    def coerce(cls, value: "ContextRef | str | dict[str, Any]") -> "ContextRef":
        """Strict: raises for unrecognizable shapes and invalid kinds.

        Use on write paths (CLI ingress, `Card()` construction, store
        updates). Already-validated ``ContextRef`` instances pass through.
        """
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            if not value:
                raise ValueError("ContextRef path must be non-empty")
            return cls(path=value)
        if isinstance(value, dict):
            if "path" not in value:
                raise KeyError("ContextRef dict requires 'path'")
            path = str(value["path"])
            if not path:
                raise ValueError("ContextRef path must be non-empty")
            return cls(
                path=path,
                kind=str(value.get("kind", "optional")),
                note=str(value.get("note", "")),
            )
        raise TypeError(f"Cannot coerce {type(value).__name__} to ContextRef")

    @classmethod
    def try_coerce(cls, value: Any) -> "ContextRef | None":
        """Lenient: returns None for structurally bad shapes; preserves a
        record with an unknown ``kind`` string so ``doctor`` can flag it.
        Use on load paths only.
        """
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            return cls.from_stored(path=value, kind="optional", note="") if value else None
        if isinstance(value, dict):
            raw_path = value.get("path")
            if not raw_path:
                return None
            path = str(raw_path)
            if not path:
                return None
            return cls.from_stored(
                path=path,
                kind=str(value.get("kind", "optional")),
                note=str(value.get("note", "")),
            )
        return None


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
    context_refs: list[ContextRef] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    outputs: dict[str, Any] = field(default_factory=dict)
    history: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        self.context_refs = [ContextRef.coerce(r) for r in self.context_refs]

    def add_history(self, message: str, role: AgentRole | str | None = None) -> None:
        tag = role.value if isinstance(role, AgentRole) else (role or "system")
        self.history.append(f"[{tag}] {message}")
        self.updated_at = utc_now()


@dataclass(slots=True)
class CardEvent:
    card_id: str
    message: str
    at: datetime = field(default_factory=utc_now)
    # Populated only for execution events (agent runs). Plain events
    # (status changes, manual edits, locks) leave these as None.
    role: AgentRole | None = None
    prompt_version: str | None = None
    duration_ms: int | None = None
    attempt: int | None = None
    raw_path: str | None = None

    @property
    def is_execution(self) -> bool:
        return self.role is not None


@dataclass(slots=True)
class TraceInfo:
    """Metadata for one retained raw agent transcript."""

    card_id: str
    role: AgentRole
    at: datetime
    path: str
    size: int


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
