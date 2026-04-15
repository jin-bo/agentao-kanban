"""ACP backend adapter.

Delegates execution to a project-local ACP server via
``agentao.acp_client.ACPManager.prompt_once``. The backend keeps no
long-lived session state — each call runs one prompt turn, and the manager
handles server lifecycle and the streaming inbox.

Phase 4 scope: happy-path integration plus `SERVER_NOT_FOUND` validation.
The broader AcpErrorCode → kanban-failure mapping (Phase 5) and richer
event payload (Phase 6) are layered on later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .base import BackendRequest, BackendResponse


ManagerFactory = Callable[[Path | None], Any]


def _default_manager_factory(project_root: Path | None) -> Any:
    from agentao.acp_client import ACPManager

    return ACPManager.from_project(project_root)


@dataclass
class AcpBackend:
    """Run a resolved profile through an ACP server via `prompt_once`.

    The backend is intentionally thin: it validates the target name, drains
    the manager inbox before the turn (so stale messages from earlier calls
    don't leak in), runs one non-interactive prompt, and collects the
    assistant's RESPONSE text for this server/session. Failure mapping is
    left to the top-level executor in Phase 5.
    """

    project_root: Path | None = None
    timeout: float | None = None
    manager_factory: ManagerFactory = field(default=_default_manager_factory)
    _manager: Any = field(default=None, init=False, repr=False)

    backend_type: str = field(default="acp", init=False)

    @property
    def manager(self) -> Any:
        if self._manager is None:
            self._manager = self.manager_factory(self.project_root)
        return self._manager

    def invoke(self, request: BackendRequest) -> BackendResponse:
        from agentao.acp_client import AcpClientError, AcpErrorCode

        target = request.profile.backend.target
        manager = self.manager

        if manager.get_handle(target) is None:
            raise AcpClientError(
                f"ACP server {target!r} is not defined in .agentao/acp.json",
                code=AcpErrorCode.SERVER_NOT_FOUND,
                details={"server": target, "profile": request.profile.name},
            )

        # Drop any stale inbox messages left by earlier turns so we only
        # collect text produced by this call.
        manager.inbox.drain()

        cwd = str(request.working_directory) if request.working_directory else None
        result = manager.prompt_once(
            target,
            request.prompt,
            cwd=cwd,
            timeout=self.timeout,
            interactive=False,
        )

        raw_text = _collect_response_text(
            manager, server=target, session_id=result.session_id
        )

        metadata: dict[str, Any] = {
            "backend_target": target,
            "session_id": result.session_id,
            "stop_reason": result.stop_reason,
            "effective_cwd": result.cwd,
        }
        return BackendResponse(
            raw_text=raw_text,
            prompt_version="",  # ACP servers have no kanban-side spec version
            spec_name=request.profile.name,
            metadata=metadata,
        )


def _collect_response_text(
    manager: Any, *, server: str, session_id: str | None
) -> str:
    """Reassemble streamed RESPONSE chunks for exactly this prompt turn.

    Filters by (server, session_id) rather than server alone so late chunks
    from an earlier session on the same server — which the manager inbox
    is free to deliver after `prompt_once` returns — aren't misattributed
    to the current card. Chunks are concatenated verbatim: the inbox
    carries streamed fragments, not line records, so inserting newlines
    would corrupt payloads (a JSON fence split across two chunks would
    stop parsing as JSON).
    """
    from agentao.acp_client.inbox import MessageKind

    parts: list[str] = []
    for msg in manager.inbox.drain():
        if msg.server != server:
            continue
        if msg.kind != MessageKind.RESPONSE:
            continue
        # If we know the session_id of this turn, only accept chunks
        # tagged with it. Messages without a session_id fall through
        # (older servers, pre-session notifications).
        if session_id and msg.session_id and msg.session_id != session_id:
            continue
        if msg.text:
            parts.append(msg.text)
    return "".join(parts)
