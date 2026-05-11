from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..models import AgentResult, AgentRole, CardEvent, TraceInfo, utc_now
from .component import StoreComponent
from .store_utils import _tail


class EventStore(StoreComponent):
    def append_event(self, card_id: str, message: str) -> None:
        event = CardEvent(card_id=card_id, message=message)
        self._events.append(event)
        record = {"at": event.at.isoformat(), "card_id": card_id, "message": message}
        self._write_event_line(record)

    def append_runtime_event(
        self,
        card_id: str,
        *,
        event_type: str,
        message: str,
        role: AgentRole | None = None,
        claim_id: str | None = None,
        worker_id: str | None = None,
        attempt: int | None = None,
        duration_ms: int | None = None,
        failure_reason: str | None = None,
        failure_category: str | None = None,
        retry_of_claim_id: str | None = None,
        worktree_branch: str | None = None,
        rework_iteration: int | None = None,
    ) -> None:
        """Emit a structured runtime lifecycle event (plan §Event Model Upgrade).

        Separate from ``append_event`` so readers (``doctor``, ``events``
        CLI) can filter by ``event_type`` without scraping messages.
        """
        at = utc_now()
        event = CardEvent(
            card_id=card_id,
            message=message,
            at=at,
            role=role,
            attempt=attempt,
            duration_ms=duration_ms,
            event_type=event_type,
            claim_id=claim_id,
            worker_id=worker_id,
            failure_reason=failure_reason,
            failure_category=failure_category,
            retry_of_claim_id=retry_of_claim_id,
            worktree_branch=worktree_branch,
            rework_iteration=rework_iteration,
        )
        self._events.append(event)
        record: dict[str, Any] = {
            "at": at.isoformat(),
            "card_id": card_id,
            "message": message,
            "event_type": event_type,
        }
        if role is not None:
            record["role"] = role.value
        for key, value in (
            ("claim_id", claim_id),
            ("worker_id", worker_id),
            ("attempt", attempt),
            ("duration_ms", duration_ms),
            ("failure_reason", failure_reason),
            ("failure_category", failure_category),
            ("retry_of_claim_id", retry_of_claim_id),
            ("worktree_branch", worktree_branch),
            ("rework_iteration", rework_iteration),
        ):
            if value is not None:
                record[key] = value
        self._write_event_line(record)

    def append_execution_event(self, card_id: str, result: AgentResult) -> None:
        at = utc_now()
        raw_path_str: str | None = None
        if result.raw_response is not None:
            raw_path = self._write_raw_transcript(
                card_id, result.role, at, result.raw_response
            )
            if raw_path is not None:
                try:
                    raw_path_str = str(raw_path.relative_to(self.root.parent))
                except ValueError:
                    raw_path_str = str(raw_path)
        event = CardEvent(
            card_id=card_id,
            message=result.summary,
            at=at,
            role=result.role,
            prompt_version=result.prompt_version,
            duration_ms=result.duration_ms,
            attempt=result.attempt,
            raw_path=raw_path_str,
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
        self._events.append(event)
        record: dict[str, Any] = {
            "at": at.isoformat(),
            "card_id": card_id,
            "role": result.role.value,
            "prompt_version": result.prompt_version,
            "duration_ms": result.duration_ms,
            "attempt": result.attempt,
            "message": result.summary,
        }
        if raw_path_str is not None:
            record["raw_path"] = raw_path_str
        for field_name in (
            "agent_profile",
            "backend_type",
            "backend_target",
            "routing_source",
            "routing_reason",
            "fallback_from_profile",
            "session_id",
            "router_prompt_version",
        ):
            value = getattr(result, field_name)
            if value is not None:
                record[field_name] = value
        if result.backend_metadata:
            record["backend_metadata"] = dict(result.backend_metadata)
        self._write_event_line(record)

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
        card_dir = self.raw_root / card_id
        if not card_dir.exists():
            return []
        traces: list[TraceInfo] = []
        for path in sorted(card_dir.glob("*.md")):
            role_str, sep, stamp_with_ext = path.name.partition("-")
            if not sep or not stamp_with_ext.endswith(".md"):
                continue
            try:
                this_role = AgentRole(role_str)
            except ValueError:
                continue
            if role is not None and this_role != role:
                continue
            stamp_str = stamp_with_ext[:-3]
            try:
                at = datetime.strptime(stamp_str, "%Y%m%dT%H%M%S%fZ").replace(
                    tzinfo=UTC
                )
            except ValueError:
                at = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            traces.append(
                TraceInfo(
                    card_id=card_id,
                    role=this_role,
                    at=at,
                    path=str(path),
                    size=path.stat().st_size,
                )
            )
        if latest and traces:
            return [max(traces, key=lambda t: t.at)]
        return traces

    def _write_event_line(self, record: dict[str, Any]) -> None:
        # O_APPEND writes are atomic for < PIPE_BUF on POSIX, so concurrent
        # CLI + daemon writers don't interleave whole lines.
        self.root.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
        fd = os.open(self.events_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)

    def _write_raw_transcript(
        self,
        card_id: str,
        role: AgentRole,
        at: datetime,
        raw_response: str,
    ) -> Path | None:
        if self.raw_retention <= 0:
            return None
        card_dir = self.raw_root / card_id
        card_dir.mkdir(parents=True, exist_ok=True)
        stamp = at.strftime("%Y%m%dT%H%M%S%fZ")
        path = card_dir / f"{role.value}-{stamp}.md"
        path.write_text(raw_response, encoding="utf-8")
        # Retention: keep the most recent N per (card, role).
        role_files = sorted(card_dir.glob(f"{role.value}-*.md"))
        for stale in role_files[: -self.raw_retention]:
            try:
                stale.unlink()
            except OSError:
                pass
        return path
