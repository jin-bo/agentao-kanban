from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Protocol, runtime_checkable

from .models import (
    AgentResult,
    AgentRole,
    Card,
    CardEvent,
    CardStatus,
    ContextRef,
    ExecutionClaim,
    ExecutionResultEnvelope,
    TraceInfo,
    WorkerPresence,
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
    def refresh(self) -> None: ...
    def add_card(self, card: Card) -> Card: ...
    def get_card(self, card_id: str) -> Card: ...
    def list_cards(self) -> list[Card]: ...
    def list_by_status(self, status: CardStatus) -> list[Card]: ...
    def move_card(self, card_id: str, status: CardStatus, note: str) -> Card: ...
    def update_card(self, card_id: str, **updates: object) -> Card: ...
    def append_event(self, card_id: str, message: str) -> None: ...
    def append_execution_event(self, card_id: str, result: AgentResult) -> None: ...
    def append_runtime_event(
        self,
        card_id: str,
        *,
        event_type: str,
        message: str,
        role: AgentRole | None = ...,
        claim_id: str | None = ...,
        worker_id: str | None = ...,
        attempt: int | None = ...,
        duration_ms: int | None = ...,
        failure_reason: str | None = ...,
        failure_category: str | None = ...,
        retry_of_claim_id: str | None = ...,
    ) -> None: ...
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

    # ---------- v0.1.2 runtime surface ----------
    #
    # Optional: stores that do not persist runtime state (InMemoryBoardStore)
    # may leave these as no-ops or raise NotImplementedError. The markdown
    # store implements them over workspace/board/runtime/.

    def create_claim(self, claim: ExecutionClaim) -> ExecutionClaim: ...
    def get_claim(self, card_id: str) -> ExecutionClaim | None: ...
    def renew_claim(
        self,
        card_id: str,
        *,
        claim_id: str,
        heartbeat_at: datetime,
        lease_expires_at: datetime,
        worker_id: str | None = ...,
    ) -> ExecutionClaim: ...
    def clear_claim(self, card_id: str, *, claim_id: str | None = ...) -> None: ...
    def list_claims(self) -> list[ExecutionClaim]: ...
    def list_stale_claims(self, *, now: datetime | None = ...) -> list[ExecutionClaim]: ...
    def try_acquire_claim(
        self,
        card_id: str,
        *,
        worker_id: str,
        heartbeat_at: datetime | None = ...,
        lease_expires_at: datetime | None = ...,
    ) -> ExecutionClaim | None: ...

    def write_result(self, result: ExecutionResultEnvelope) -> None: ...
    def read_results(
        self, *, card_id: str | None = ...
    ) -> list[ExecutionResultEnvelope]: ...
    def delete_result(self, card_id: str, claim_id: str) -> None: ...
    def quarantine_result(self, card_id: str, claim_id: str) -> None: ...
    def list_orphan_results(self) -> list[ExecutionResultEnvelope]: ...

    def heartbeat_worker(self, presence: WorkerPresence) -> WorkerPresence: ...
    def list_workers(self) -> list[WorkerPresence]: ...
    def remove_worker(self, worker_id: str) -> None: ...


class InMemoryBoardStore:
    def __init__(self) -> None:
        self._cards: dict[str, Card] = {}
        self._events: list[CardEvent] = []
        self._claims: dict[str, ExecutionClaim] = {}
        self._results: list[ExecutionResultEnvelope] = []
        self._orphans: list[ExecutionResultEnvelope] = []
        self._workers: dict[str, WorkerPresence] = {}

    def refresh(self) -> None:
        return None

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
            elif key == "owner_role" and isinstance(value, str):
                value = AgentRole(value)
            elif key == "status" and isinstance(value, str):
                value = CardStatus(value)
            setattr(card, key, value)
        card.updated_at = utc_now()
        return card

    def append_event(self, card_id: str, message: str) -> None:
        self._events.append(CardEvent(card_id=card_id, message=message))

    def append_runtime_event(
        self,
        card_id: str,
        *,
        event_type: str,
        message: str,
        role: "AgentRole | None" = None,
        claim_id: str | None = None,
        worker_id: str | None = None,
        attempt: int | None = None,
        duration_ms: int | None = None,
        failure_reason: str | None = None,
        failure_category: str | None = None,
        retry_of_claim_id: str | None = None,
    ) -> None:
        self._events.append(
            CardEvent(
                card_id=card_id,
                message=message,
                role=role,
                attempt=attempt,
                duration_ms=duration_ms,
                event_type=event_type,
                claim_id=claim_id,
                worker_id=worker_id,
                failure_reason=failure_reason,
                failure_category=failure_category,
                retry_of_claim_id=retry_of_claim_id,
            )
        )

    def append_execution_event(self, card_id: str, result: AgentResult) -> None:
        self._events.append(
            CardEvent(
                card_id=card_id,
                message=result.summary,
                role=result.role,
                prompt_version=result.prompt_version,
                duration_ms=result.duration_ms,
                attempt=result.attempt,
                agent_profile=result.agent_profile,
                backend_type=result.backend_type,
                backend_target=result.backend_target,
                routing_source=result.routing_source,
                routing_reason=result.routing_reason,
                fallback_from_profile=result.fallback_from_profile,
                session_id=result.session_id,
                router_prompt_version=result.router_prompt_version,
                backend_metadata=dict(result.backend_metadata),
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

    # ---------- runtime surface (in-memory) ----------

    def create_claim(self, claim: ExecutionClaim) -> ExecutionClaim:
        from .models import ClaimConflictError

        if claim.card_id in self._claims:
            raise ClaimConflictError(
                f"claim already exists for card {claim.card_id}"
            )
        self._claims[claim.card_id] = claim
        return claim

    def get_claim(self, card_id: str) -> ExecutionClaim | None:
        return self._claims.get(card_id)

    def renew_claim(
        self,
        card_id: str,
        *,
        claim_id: str,
        heartbeat_at: datetime,
        lease_expires_at: datetime,
        worker_id: str | None = None,
    ) -> ExecutionClaim:
        from .models import ClaimMismatchError
        from dataclasses import replace

        current = self._claims.get(card_id)
        if current is None:
            raise KeyError(f"no claim for card {card_id}")
        if current.claim_id != claim_id:
            raise ClaimMismatchError(
                f"claim_id mismatch for {card_id}: "
                f"expected {current.claim_id}, got {claim_id}"
            )
        updated = replace(
            current,
            heartbeat_at=heartbeat_at,
            lease_expires_at=lease_expires_at,
            worker_id=worker_id if worker_id is not None else current.worker_id,
        )
        self._claims[card_id] = updated
        return updated

    def clear_claim(self, card_id: str, *, claim_id: str | None = None) -> None:
        from .models import ClaimMismatchError

        current = self._claims.get(card_id)
        if current is None:
            return
        if claim_id is not None and current.claim_id != claim_id:
            raise ClaimMismatchError(
                f"claim_id mismatch for {card_id}: "
                f"expected {current.claim_id}, got {claim_id}"
            )
        del self._claims[card_id]

    def list_claims(self) -> list[ExecutionClaim]:
        return list(self._claims.values())

    def list_stale_claims(
        self, *, now: datetime | None = None
    ) -> list[ExecutionClaim]:
        cutoff = now or utc_now()
        return [c for c in self._claims.values() if c.lease_expires_at < cutoff]

    def try_acquire_claim(
        self,
        card_id: str,
        *,
        worker_id: str,
        heartbeat_at: datetime | None = None,
        lease_expires_at: datetime | None = None,
    ) -> ExecutionClaim | None:
        from dataclasses import replace

        current = self._claims.get(card_id)
        if current is None or current.worker_id is not None:
            return None
        updated = replace(
            current,
            worker_id=worker_id,
            heartbeat_at=heartbeat_at or utc_now(),
            lease_expires_at=lease_expires_at or current.lease_expires_at,
        )
        self._claims[card_id] = updated
        return updated

    def write_result(self, result: ExecutionResultEnvelope) -> None:
        # Write-once per claim: refuse to overwrite a pending envelope for
        # the same claim_id (a forger with the wrong claim_id simply creates
        # a separate record that will be orphaned at commit time).
        for r in self._results:
            if r.card_id == result.card_id and r.claim_id == result.claim_id:
                raise FileExistsError(
                    f"result envelope for claim {result.claim_id} already exists"
                )
        self._results.append(result)

    def read_results(
        self, *, card_id: str | None = None
    ) -> list[ExecutionResultEnvelope]:
        if card_id is None:
            return list(self._results)
        return [r for r in self._results if r.card_id == card_id]

    def delete_result(self, card_id: str, claim_id: str) -> None:
        self._results = [
            r
            for r in self._results
            if not (r.card_id == card_id and r.claim_id == claim_id)
        ]

    def quarantine_result(self, card_id: str, claim_id: str) -> None:
        for r in list(self._results):
            if r.card_id == card_id and r.claim_id == claim_id:
                self._results.remove(r)
                self._orphans.append(r)
                return

    def list_orphan_results(self) -> list[ExecutionResultEnvelope]:
        return list(self._orphans)

    def heartbeat_worker(self, presence: WorkerPresence) -> WorkerPresence:
        self._workers[presence.worker_id] = presence
        return presence

    def list_workers(self) -> list[WorkerPresence]:
        return list(self._workers.values())

    def remove_worker(self, worker_id: str) -> None:
        self._workers.pop(worker_id, None)
