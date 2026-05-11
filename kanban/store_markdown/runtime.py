from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..models import (
    AgentResult,
    AgentRole,
    ExecutionClaim,
    ExecutionResultEnvelope,
    FailureCategory,
    ResourceUsage,
    RevisionRequest,
    WorkerPresence,
    coerce_card_status,
)


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Write JSON via tmp + os.replace so readers never see a torn file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse_iso(raw: Any) -> datetime:
    dt = datetime.fromisoformat(str(raw))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _claim_to_json(claim: ExecutionClaim) -> dict[str, Any]:
    data: dict[str, Any] = {
        "card_id": claim.card_id,
        "claim_id": claim.claim_id,
        "worker_id": claim.worker_id,
        "role": claim.role.value,
        "status_at_claim": claim.status_at_claim.value,
        "attempt": claim.attempt,
        "retry_count": claim.retry_count,
        "retry_of_claim_id": claim.retry_of_claim_id,
        "claimed_at": _iso(claim.claimed_at),
        "heartbeat_at": _iso(claim.heartbeat_at),
        "lease_expires_at": _iso(claim.lease_expires_at),
        "timeout_s": claim.timeout_s,
    }
    if claim.worktree_path is not None:
        data["worktree_path"] = claim.worktree_path
    return data


def _claim_from_json(data: dict[str, Any]) -> ExecutionClaim:
    return ExecutionClaim(
        card_id=str(data["card_id"]),
        claim_id=str(data["claim_id"]),
        role=AgentRole(data["role"]),
        status_at_claim=coerce_card_status(data["status_at_claim"]),
        attempt=int(data["attempt"]),
        claimed_at=_parse_iso(data["claimed_at"]),
        heartbeat_at=_parse_iso(data["heartbeat_at"]),
        lease_expires_at=_parse_iso(data["lease_expires_at"]),
        timeout_s=int(data["timeout_s"]),
        worker_id=data.get("worker_id"),
        retry_count=int(data.get("retry_count", 0)),
        retry_of_claim_id=data.get("retry_of_claim_id"),
        worktree_path=data.get("worktree_path"),
    )


def _resource_to_json(usage: ResourceUsage) -> dict[str, Any]:
    return {
        "pid": usage.pid,
        "rss_bytes": usage.rss_bytes,
        "cpu_seconds": usage.cpu_seconds,
        "workdir_size_bytes": usage.workdir_size_bytes,
    }


def _resource_from_json(data: dict[str, Any]) -> ResourceUsage:
    return ResourceUsage(
        pid=data.get("pid"),
        rss_bytes=data.get("rss_bytes"),
        cpu_seconds=data.get("cpu_seconds"),
        workdir_size_bytes=data.get("workdir_size_bytes"),
    )


def _agent_result_to_json(result: AgentResult) -> dict[str, Any]:
    # Normalized copy per open-questions decision — drop raw_response to keep
    # envelopes small; raw text still lives under workspace/raw/.
    data: dict[str, Any] = {
        "role": result.role.value,
        "summary": result.summary,
        "next_status": result.next_status.value,
        "updates": dict(result.updates),
        "prompt_version": result.prompt_version,
        "duration_ms": result.duration_ms,
        "attempt": result.attempt,
    }
    if result.revision_request is not None:
        data["revision_request"] = _revision_request_to_json(result.revision_request)
    return data


def _agent_result_from_json(data: dict[str, Any]) -> AgentResult:
    rr_raw = data.get("revision_request")
    revision = _revision_request_from_json(rr_raw) if rr_raw else None
    return AgentResult(
        role=AgentRole(data["role"]),
        summary=str(data["summary"]),
        next_status=coerce_card_status(data["next_status"]),
        updates=dict(data.get("updates", {})),
        prompt_version=str(data.get("prompt_version", "")),
        duration_ms=int(data.get("duration_ms", 0)),
        attempt=int(data.get("attempt", 1)),
        revision_request=revision,
    )


def _revision_request_to_json(r: RevisionRequest) -> dict[str, Any]:
    return {
        "at": _iso(r.at),
        "from_role": r.from_role.value,
        "iteration": int(r.iteration),
        "summary": r.summary,
        "hints": list(r.hints),
        "failing_criteria": list(r.failing_criteria),
    }


def _revision_request_from_json(data: dict[str, Any]) -> RevisionRequest:
    return RevisionRequest(
        at=_parse_iso(data["at"]),
        from_role=AgentRole(data["from_role"]),
        iteration=int(data.get("iteration", 0)),
        summary=str(data.get("summary", "")),
        hints=[str(h) for h in (data.get("hints") or [])],
        failing_criteria=[str(c) for c in (data.get("failing_criteria") or [])],
    )


def _result_to_json(envelope: ExecutionResultEnvelope) -> dict[str, Any]:
    out: dict[str, Any] = {
        "card_id": envelope.card_id,
        "claim_id": envelope.claim_id,
        "worker_id": envelope.worker_id,
        "role": envelope.role.value,
        "attempt": envelope.attempt,
        "started_at": _iso(envelope.started_at),
        "finished_at": _iso(envelope.finished_at),
        "duration_ms": envelope.duration_ms,
        "ok": envelope.ok,
        "failure_reason": envelope.failure_reason,
        "failure_category": (
            envelope.failure_category.value
            if envelope.failure_category is not None
            else None
        ),
    }
    if envelope.agent_result is not None:
        out["agent_result"] = _agent_result_to_json(envelope.agent_result)
    if envelope.resource_usage is not None:
        out["resource_usage"] = _resource_to_json(envelope.resource_usage)
    return out


def _result_from_json(data: dict[str, Any]) -> ExecutionResultEnvelope:
    agent_result = (
        _agent_result_from_json(data["agent_result"])
        if data.get("agent_result") is not None
        else None
    )
    resource_usage = (
        _resource_from_json(data["resource_usage"])
        if data.get("resource_usage") is not None
        else None
    )
    raw_cat = data.get("failure_category")
    failure_category = FailureCategory(raw_cat) if raw_cat else None
    return ExecutionResultEnvelope(
        card_id=str(data["card_id"]),
        claim_id=str(data["claim_id"]),
        role=AgentRole(data["role"]),
        attempt=int(data["attempt"]),
        started_at=_parse_iso(data["started_at"]),
        finished_at=_parse_iso(data["finished_at"]),
        duration_ms=int(data["duration_ms"]),
        ok=bool(data["ok"]),
        agent_result=agent_result,
        worker_id=data.get("worker_id"),
        failure_reason=data.get("failure_reason"),
        failure_category=failure_category,
        resource_usage=resource_usage,
    )


def _worker_to_json(presence: WorkerPresence) -> dict[str, Any]:
    return {
        "worker_id": presence.worker_id,
        "pid": presence.pid,
        "started_at": _iso(presence.started_at),
        "heartbeat_at": _iso(presence.heartbeat_at),
        "host": presence.host,
    }


def _worker_from_json(data: dict[str, Any]) -> WorkerPresence:
    return WorkerPresence(
        worker_id=str(data["worker_id"]),
        pid=int(data["pid"]),
        started_at=_parse_iso(data["started_at"]),
        heartbeat_at=_parse_iso(data["heartbeat_at"]),
        host=data.get("host"),
    )
