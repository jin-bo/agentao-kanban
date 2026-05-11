from __future__ import annotations

from datetime import datetime
from typing import Any

from ..models import Card, CardEvent, ContextRef, RevisionRequest


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _ref_to_dict(r: ContextRef) -> dict[str, Any]:
    out: dict[str, Any] = {"path": r.path, "kind": r.kind}
    if r.note:
        out["note"] = r.note
    return out


def _revision_to_dict(rev: RevisionRequest) -> dict[str, Any]:
    out: dict[str, Any] = {
        "iteration": rev.iteration,
        "from_role": rev.from_role.value,
        "at": _iso(rev.at),
        "summary": rev.summary,
    }
    if rev.hints:
        out["hints"] = list(rev.hints)
    if rev.failing_criteria:
        out["failing_criteria"] = list(rev.failing_criteria)
    return out


def card_to_dict(card: Card) -> dict[str, Any]:
    """JSON-serializable Card for MCP tool/resource responses."""
    return {
        "id": card.id,
        "title": card.title,
        "goal": card.goal,
        "status": card.status.value,
        "priority": card.priority.name,
        "owner_role": card.owner_role.value if card.owner_role else None,
        "blocked_reason": card.blocked_reason,
        "blocked_at": _iso(card.blocked_at),
        "created_at": _iso(card.created_at),
        "updated_at": _iso(card.updated_at),
        "depends_on": list(card.depends_on),
        "acceptance_criteria": list(card.acceptance_criteria),
        "context_refs": [_ref_to_dict(r) for r in card.context_refs],
        "outputs": dict(card.outputs),
        "history": list(card.history),
        "agent_profile": card.agent_profile,
        "agent_profile_source": card.agent_profile_source,
        "worktree_branch": card.worktree_branch,
        "worktree_base_commit": card.worktree_base_commit,
        "rework_iteration": card.rework_iteration,
        # Reviewer/verifier feedback the worker is supposed to address on
        # the next pass. Without this, MCP clients see a bumped
        # ``rework_iteration`` but cannot read the actual rework asks.
        "revision_requests": [
            _revision_to_dict(r) for r in card.revision_requests
        ],
    }


def event_to_dict(e: CardEvent) -> dict[str, Any]:
    """JSON-serializable CardEvent for events_tail and events resource."""
    rec: dict[str, Any] = {
        "at": _iso(e.at),
        "card_id": e.card_id,
        "message": e.message,
    }
    if e.is_execution:
        rec["role"] = e.role.value if e.role else None
        rec["prompt_version"] = e.prompt_version
        rec["duration_ms"] = e.duration_ms
        rec["attempt"] = e.attempt
        if e.raw_path is not None:
            rec["raw_path"] = e.raw_path
    for key, value in (
        ("event_type", e.event_type),
        ("claim_id", e.claim_id),
        ("worker_id", e.worker_id),
        ("failure_reason", e.failure_reason),
        ("failure_category", e.failure_category),
        ("retry_of_claim_id", e.retry_of_claim_id),
        ("worktree_branch", e.worktree_branch),
        ("rework_iteration", e.rework_iteration),
    ):
        if value is not None:
            rec[key] = value
    return rec
