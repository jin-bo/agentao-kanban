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
    agent_profile: str | None = None
    agent_profile_source: str | None = None

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
    # v0.1.2 runtime lifecycle fields (PR4/M3). Present on events emitted
    # by the runtime layer (claimed, finished, failed, timed_out, retried,
    # claim_recovered, result_orphaned). None on legacy plain events.
    event_type: str | None = None
    claim_id: str | None = None
    worker_id: str | None = None
    failure_reason: str | None = None
    failure_category: str | None = None
    retry_of_claim_id: str | None = None
    # v0.2.0 profile routing diagnostics (mirror of AgentResult extras).
    agent_profile: str | None = None
    backend_type: str | None = None
    backend_target: str | None = None
    routing_source: str | None = None
    routing_reason: str | None = None
    fallback_from_profile: str | None = None
    session_id: str | None = None
    router_prompt_version: str | None = None
    # Free-form backend diagnostics (stop_reason, effective_cwd, ...).
    # Persisted alongside the scalar fields so ACP postmortems survive
    # a board reload rather than only living on the in-memory AgentResult.
    backend_metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_execution(self) -> bool:
        return self.role is not None

    @property
    def is_runtime(self) -> bool:
        return self.event_type is not None


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
    # v0.2.0 profile routing: which implementation actually ran this card,
    # how it was selected, and backend-level diagnostics. All optional so
    # legacy executors (mock, agentao_multi) keep working unchanged.
    agent_profile: str | None = None
    backend_type: str | None = None
    backend_target: str | None = None
    routing_source: str | None = None  # "card" | "planner" | "policy" | "default"
    routing_reason: str | None = None
    fallback_from_profile: str | None = None
    session_id: str | None = None
    # Version string of the router agent spec, populated ONLY when the
    # router was actually invoked for this resolution (not on disabled,
    # spec-missing, or single-candidate short-circuit paths). Resolves
    # agent-router-design Open Question #2.
    router_prompt_version: str | None = None
    backend_metadata: dict[str, Any] = field(default_factory=dict)


# ---------- v0.1.2 runtime concurrency kernel ----------
#
# Workflow status (Card.status) answers "where is this card in delivery?"
# Claim / lease state answers "is there an in-flight execution right now?"
# The two are intentionally separate: runtime state lives beside the board
# under workspace/board/runtime/ and is advisory; card files remain the
# source of truth for workflow transitions.


class ExecutionEventType(StrEnum):
    """Runtime lifecycle event names (plan §Event Model Upgrade)."""

    CLAIMED = "execution.claimed"
    STARTED = "execution.started"
    HEARTBEAT = "execution.heartbeat"
    FINISHED = "execution.finished"
    FAILED = "execution.failed"
    TIMED_OUT = "execution.timed_out"
    RETRIED = "execution.retried"
    CLAIM_RECOVERED = "execution.claim_recovered"
    RESULT_ORPHANED = "execution.result_orphaned"
    WORKER_STARTED = "worker.started"
    WORKER_STOPPED = "worker.stopped"


@dataclass(slots=True)
class ResourceUsage:
    """Lightweight local resource metrics. All fields optional."""

    pid: int | None = None
    rss_bytes: int | None = None
    cpu_seconds: float | None = None
    workdir_size_bytes: int | None = None


@dataclass(slots=True)
class ExecutionClaim:
    """One live execution lease for a card.

    `worker_id` is None when the scheduler creates an unassigned claim
    (per open-questions decision). A worker sets it on first heartbeat
    to record ownership.
    """

    card_id: str
    claim_id: str
    role: AgentRole
    status_at_claim: CardStatus
    attempt: int
    claimed_at: datetime
    lease_expires_at: datetime
    heartbeat_at: datetime
    timeout_s: int
    worker_id: str | None = None
    retry_count: int = 0
    retry_of_claim_id: str | None = None

    def is_expired(self, *, now: datetime | None = None) -> bool:
        return self.lease_expires_at < (now or utc_now())


class FailureCategory(StrEnum):
    """Why an executor attempt failed. Drives the retry matrix in PR4/M3.

    - ``infrastructure``: executor raised or transport layer failed (LLM 5xx,
      network, disk). Retryable up to ``RetryPolicy.infrastructure`` times.
    - ``malformed``: agent returned but the response could not be parsed.
      Not retried — the task-quality issue will recur.
    - ``functional``: agent ran successfully and declined the card
      (reviewer/verifier rejection). Not retried; goes straight to BLOCKED.
    - ``lease_expiry``: scheduler detected an expired lease without an
      envelope. Retryable once.
    - ``timeout``: worker exceeded its role-specific timeout. Same retry
      budget as ``lease_expiry`` in PR4.
    """

    INFRASTRUCTURE = "infrastructure"
    MALFORMED = "malformed"
    FUNCTIONAL = "functional"
    LEASE_EXPIRY = "lease_expiry"
    TIMEOUT = "timeout"


@dataclass(slots=True)
class RetryPolicy:
    """Per-category retry budgets (plan §Retry Policy)."""

    infrastructure: int = 2
    lease_expiry: int = 1
    timeout: int = 1
    malformed: int = 0
    functional: int = 0

    def budget_for(self, category: "FailureCategory | None") -> int:
        if category is None:
            return 0
        return int(getattr(self, category.value, 0))


@dataclass(slots=True)
class ExecutionResultEnvelope:
    """A worker's submitted outcome for one claim attempt."""

    card_id: str
    claim_id: str
    role: AgentRole
    attempt: int
    started_at: datetime
    finished_at: datetime
    duration_ms: int
    ok: bool
    agent_result: AgentResult | None = None
    worker_id: str | None = None
    failure_reason: str | None = None
    failure_category: FailureCategory | None = None
    resource_usage: ResourceUsage | None = None


@dataclass(slots=True)
class WorkerPresence:
    """Heartbeat record for an active worker process. Observability only."""

    worker_id: str
    pid: int
    started_at: datetime
    heartbeat_at: datetime
    host: str | None = None


class ClaimConflictError(RuntimeError):
    """Raised when `create_claim` is called for a card that already has one."""


class ClaimMismatchError(RuntimeError):
    """Raised when `renew_claim` / `clear_claim` is called with the wrong claim_id."""


@dataclass(slots=True)
class LeasePolicy:
    """Runtime lease parameters. Plan §Lease Semantics recommends these defaults."""

    lease_seconds: int = 60
    heartbeat_seconds: int = 15
    # Role-specific timeouts (plan §Timeout Policy). Unused in PR2 but
    # declared here so later PRs don't need a breaking change.
    timeout_by_role: dict[str, int] = field(
        default_factory=lambda: {
            "planner": 120,
            "worker": 1800,
            "reviewer": 300,
            "verifier": 300,
        }
    )

    def timeout_for(self, role: "AgentRole") -> int:
        return int(self.timeout_by_role.get(role.value, 1800))
