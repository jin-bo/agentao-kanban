from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from ..models import AgentRole, Card, CardStatus, ExecutionClaim, utc_now


def build_execution_claim(
    *,
    card: Card,
    role: AgentRole,
    lease_seconds: int,
    timeout_s: int,
    attempt: int = 1,
    worker_id: str | None = None,
    retry_count: int = 0,
    retry_of_claim_id: str | None = None,
    worktree_path: str | None = None,
    status_at_claim: CardStatus | None = None,
) -> ExecutionClaim:
    now = utc_now()
    if status_at_claim is None:
        status_at_claim = (
            CardStatus.DOING if card.status == CardStatus.READY else card.status
        )
    return ExecutionClaim(
        card_id=card.id,
        claim_id=f"clm-{uuid4().hex[:12]}",
        role=role,
        status_at_claim=status_at_claim,
        attempt=attempt,
        claimed_at=now,
        heartbeat_at=now,
        lease_expires_at=now + timedelta(seconds=lease_seconds),
        timeout_s=timeout_s,
        worker_id=worker_id,
        retry_count=retry_count,
        retry_of_claim_id=retry_of_claim_id,
        worktree_path=worktree_path,
    )
