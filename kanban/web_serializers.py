"""Serialization helpers for the local web UI API."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def display_path(abs_path: str, *, board_dir: Path, git_root: Path | None) -> str:
    """Render an absolute result path relative to a useful local root."""
    p = Path(abs_path)
    roots = [board_dir.parent]
    if git_root is not None:
        roots.insert(0, git_root)
    for root in roots:
        try:
            return str(p.relative_to(root))
        except ValueError:
            continue
    return abs_path


def display_path_map(
    abs_paths: list[str], *, board_dir: Path, git_root: Path | None
) -> dict[str, str]:
    return {
        a: display_path(a, board_dir=board_dir, git_root=git_root)
        for a in abs_paths
    }


def card_summary(card_dict: dict[str, Any]) -> dict[str, Any]:
    """Slim board-column record."""
    keys = (
        "id",
        "title",
        "status",
        "priority",
        "owner_role",
        "blocked_reason",
        "updated_at",
        "created_at",
        "depends_on",
        "rework_iteration",
        "agent_profile",
    )
    return {k: card_dict.get(k) for k in keys}


def claim_to_dict(claim) -> dict[str, Any]:
    return {
        "card_id": claim.card_id,
        "claim_id": claim.claim_id,
        "role": claim.role.value,
        "status_at_claim": claim.status_at_claim.value,
        "attempt": claim.attempt,
        "worker_id": claim.worker_id,
        "claimed_at": claim.claimed_at.isoformat(),
        "lease_expires_at": claim.lease_expires_at.isoformat(),
        "heartbeat_at": claim.heartbeat_at.isoformat(),
    }


def worker_to_dict(worker) -> dict[str, Any]:
    return {
        "worker_id": worker.worker_id,
        "pid": worker.pid,
        "host": worker.host,
        "started_at": worker.started_at.isoformat(),
        "heartbeat_at": worker.heartbeat_at.isoformat(),
    }


def display_tag(event_dict: dict[str, Any]) -> str:
    """Short label for event coloring in the UI."""
    if event_dict.get("event_type"):
        return str(event_dict["event_type"])
    if event_dict.get("role"):
        return str(event_dict["role"])
    return "info"


def annotate_event(event_dict: dict[str, Any]) -> dict[str, Any]:
    event_dict["display_tag"] = display_tag(event_dict)
    return event_dict


PRIORITY_RANK = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1}


def priority_rank(name: str | None) -> int:
    if name is None:
        return 0
    return PRIORITY_RANK.get(name.upper(), 0)
