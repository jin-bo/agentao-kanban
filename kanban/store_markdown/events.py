from __future__ import annotations

import json
from datetime import datetime

from ..models import AgentRole, CardEvent


def _decode_event_line(line: str) -> CardEvent | None:
    if line.startswith("{"):
        try:
            data = json.loads(line)
            role_str = data.get("role")
            role = AgentRole(role_str) if role_str else None
            return CardEvent(
                card_id=str(data["card_id"]),
                message=str(data["message"]),
                at=datetime.fromisoformat(str(data["at"])),
                role=role,
                prompt_version=data.get("prompt_version"),
                duration_ms=data.get("duration_ms"),
                attempt=data.get("attempt"),
                raw_path=data.get("raw_path"),
                event_type=data.get("event_type"),
                claim_id=data.get("claim_id"),
                worker_id=data.get("worker_id"),
                failure_reason=data.get("failure_reason"),
                failure_category=data.get("failure_category"),
                retry_of_claim_id=data.get("retry_of_claim_id"),
                agent_profile=data.get("agent_profile"),
                backend_type=data.get("backend_type"),
                backend_target=data.get("backend_target"),
                routing_source=data.get("routing_source"),
                routing_reason=data.get("routing_reason"),
                fallback_from_profile=data.get("fallback_from_profile"),
                session_id=data.get("session_id"),
                router_prompt_version=data.get("router_prompt_version"),
                backend_metadata=(
                    dict(data["backend_metadata"])
                    if isinstance(data.get("backend_metadata"), dict)
                    else {}
                ),
                worktree_branch=data.get("worktree_branch"),
                rework_iteration=data.get("rework_iteration"),
            )
        except (json.JSONDecodeError, KeyError, ValueError):
            return None
    # Backward compat: legacy TSV lines `<iso>\t<card_id>\t<message>`.
    parts = line.split("\t", 2)
    if len(parts) != 3:
        return None
    ts, card_id, message = parts
    try:
        at = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return CardEvent(card_id=card_id, message=message, at=at)
