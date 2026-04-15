from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from agentao.acp_client import AcpClientError, AcpErrorCode
from agentao.acp_client.inbox import Inbox, InboxMessage, MessageKind
from agentao.acp_client.models import PromptResult

from kanban.agent_profiles import load_default_config
from kanban.executors.backends.acp_backend import AcpBackend
from kanban.executors.backends.base import BackendRequest
from kanban.executors.multi_backend import MultiBackendExecutor
from kanban.models import AgentRole, Card, CardStatus


REPO_AGENTS_DIR = Path(__file__).resolve().parent.parent / "kanban" / "defaults"


@dataclass
class _FakeHandle:
    name: str


@dataclass
class _FakeManager:
    """Stand-in for `ACPManager` for unit tests. Only the surface the backend uses."""

    handles: dict[str, _FakeHandle]
    scripted_messages: list[InboxMessage] = field(default_factory=list)
    scripted_result: PromptResult | None = None
    raise_on_prompt: Exception | None = None
    inbox: Inbox = field(default_factory=Inbox)
    seen_calls: list[tuple[str, str, str | None]] = field(default_factory=list)

    def get_handle(self, name: str) -> _FakeHandle | None:
        return self.handles.get(name)

    def prompt_once(self, name, prompt, *, cwd=None, timeout=None, interactive=False, **kw):
        self.seen_calls.append((name, prompt, cwd))
        if self.raise_on_prompt is not None:
            raise self.raise_on_prompt
        for msg in self.scripted_messages:
            self.inbox.push(msg)
        return self.scripted_result or PromptResult(
            stop_reason="end_turn",
            raw={},
            session_id="sess-1",
            cwd=cwd,
        )


_WORKER_RAW = 'done\n```json\n{"ok": true, "summary": "did it", "output": "code"}\n```\n'


def _msg(server: str, text: str, kind: MessageKind = MessageKind.RESPONSE) -> InboxMessage:
    return InboxMessage(server=server, session_id="sess-1", kind=kind, text=text)


def test_acp_backend_invokes_prompt_once_and_collects_text() -> None:
    mgr = _FakeManager(
        handles={"gemini-worker": _FakeHandle("gemini-worker")},
        scripted_messages=[_msg("gemini-worker", _WORKER_RAW)],
    )
    backend = AcpBackend(manager_factory=lambda root: mgr)
    cfg = load_default_config()
    profile = cfg.get_profile("gemini-worker")
    request = BackendRequest(
        role=AgentRole.WORKER,
        card=Card(title="t", goal="g"),
        prompt="hi",
        profile=profile,
        working_directory=Path("/tmp/wd"),
    )

    response = backend.invoke(request)

    assert response.raw_text == _WORKER_RAW
    assert response.spec_name == "gemini-worker"
    assert response.metadata["backend_target"] == "gemini-worker"
    assert response.metadata["session_id"] == "sess-1"
    assert response.metadata["stop_reason"] == "end_turn"
    assert mgr.seen_calls == [("gemini-worker", "hi", "/tmp/wd")]


def test_acp_backend_filters_inbox_by_server_and_kind() -> None:
    mgr = _FakeManager(
        handles={"gemini-worker": _FakeHandle("gemini-worker")},
        scripted_messages=[
            _msg("gemini-worker", "hello "),
            _msg("other-server", "SHOULD-NOT-APPEAR"),
            _msg("gemini-worker", "world", kind=MessageKind.NOTIFICATION),
            _msg("gemini-worker", "world-text"),
        ],
    )
    backend = AcpBackend(manager_factory=lambda root: mgr)
    cfg = load_default_config()
    request = BackendRequest(
        role=AgentRole.WORKER,
        card=Card(title="t", goal="g"),
        prompt="hi",
        profile=cfg.get_profile("gemini-worker"),
    )
    response = backend.invoke(request)
    # Chunks concatenate verbatim — the inbox carries streamed fragments,
    # not line records. Joining with "\n" would corrupt split JSON fences.
    assert response.raw_text == "hello world-text"


def test_acp_backend_raises_server_not_found_for_unknown_target() -> None:
    mgr = _FakeManager(handles={})  # no servers defined
    backend = AcpBackend(manager_factory=lambda root: mgr)
    cfg = load_default_config()
    request = BackendRequest(
        role=AgentRole.WORKER,
        card=Card(title="t", goal="g"),
        prompt="hi",
        profile=cfg.get_profile("gemini-worker"),
    )
    with pytest.raises(AcpClientError) as exc:
        backend.invoke(request)
    assert exc.value.code == AcpErrorCode.SERVER_NOT_FOUND
    assert exc.value.details["server"] == "gemini-worker"


def test_acp_backend_drops_stale_inbox_messages_before_turn() -> None:
    mgr = _FakeManager(
        handles={"gemini-worker": _FakeHandle("gemini-worker")},
        scripted_messages=[_msg("gemini-worker", _WORKER_RAW)],
    )
    # Pre-seed inbox with a leftover from an earlier turn.
    mgr.inbox.push(_msg("gemini-worker", "STALE-SHOULD-BE-DROPPED"))

    backend = AcpBackend(manager_factory=lambda root: mgr)
    cfg = load_default_config()
    response = backend.invoke(BackendRequest(
        role=AgentRole.WORKER,
        card=Card(title="t", goal="g"),
        prompt="hi",
        profile=cfg.get_profile("gemini-worker"),
    ))
    assert "STALE" not in response.raw_text
    assert response.raw_text == _WORKER_RAW


def test_acp_backend_wires_end_to_end_through_multi_backend() -> None:
    mgr = _FakeManager(
        handles={"gemini-worker": _FakeHandle("gemini-worker")},
        scripted_messages=[_msg("gemini-worker", _WORKER_RAW)],
    )
    acp = AcpBackend(manager_factory=lambda root: mgr)
    exec_ = MultiBackendExecutor(
        config=load_default_config(),
        agents_dir=REPO_AGENTS_DIR,
        backends={"acp": acp},
    )
    card = Card(
        title="t",
        goal="g",
        status=CardStatus.DOING,
        agent_profile="gemini-worker",
        agent_profile_source="manual",
    )
    result = exec_.run(AgentRole.WORKER, card)
    assert result.next_status == CardStatus.REVIEW
    assert result.updates["outputs"]["implementation"] == "code"


def test_acp_backend_scopes_collection_to_current_session() -> None:
    # The manager inbox may still hold RESPONSE messages from a prior
    # session on the same server. The collector must drop those rather
    # than letting an earlier card's output leak into this run.
    mgr = _FakeManager(
        handles={"gemini-worker": _FakeHandle("gemini-worker")},
        scripted_messages=[
            InboxMessage(
                server="gemini-worker",
                session_id="OLD-SESSION",
                kind=MessageKind.RESPONSE,
                text="STALE-FROM-PRIOR-SESSION",
            ),
            InboxMessage(
                server="gemini-worker",
                session_id="sess-1",
                kind=MessageKind.RESPONSE,
                text=_WORKER_RAW,
            ),
        ],
        scripted_result=PromptResult(
            stop_reason="end_turn", raw={}, session_id="sess-1", cwd="/wd",
        ),
    )
    backend = AcpBackend(manager_factory=lambda root: mgr)
    cfg = load_default_config()
    response = backend.invoke(BackendRequest(
        role=AgentRole.WORKER,
        card=Card(title="t", goal="g"),
        prompt="hi",
        profile=cfg.get_profile("gemini-worker"),
    ))
    assert "STALE-FROM-PRIOR-SESSION" not in response.raw_text
    assert response.raw_text == _WORKER_RAW


def test_acp_backend_preserves_split_json_fence_across_chunks() -> None:
    # A JSON fence split across two streamed chunks must still parse.
    # Joining with "\n" would inject a newline inside the fence and
    # break the JSON payload.
    payload = '```json\n{"ok": true, "summary": "ok", "output": "code"}\n```\n'
    chunk_a = "preamble ```json\n{\"ok\": tr"
    chunk_b = "ue, \"summary\": \"ok\", \"output\": \"code\"}\n```\n"
    mgr = _FakeManager(
        handles={"gemini-worker": _FakeHandle("gemini-worker")},
        scripted_messages=[
            _msg("gemini-worker", chunk_a),
            _msg("gemini-worker", chunk_b),
        ],
    )
    backend = AcpBackend(manager_factory=lambda root: mgr)
    cfg = load_default_config()
    response = backend.invoke(BackendRequest(
        role=AgentRole.WORKER,
        card=Card(title="t", goal="g"),
        prompt="hi",
        profile=cfg.get_profile("gemini-worker"),
    ))
    assert response.raw_text == chunk_a + chunk_b
    # Sanity: downstream parser can still extract the JSON payload.
    from kanban.executors.agentao_multi import _parse_response
    parsed = _parse_response(response.raw_text)
    assert parsed.get("ok") is True
    assert parsed.get("output") == "code"
    # And it's treated as structured (fence detected), not a raw-text fallback.
    assert parsed.get("_structured") is True


def test_acp_backend_passes_working_directory_as_cwd() -> None:
    mgr = _FakeManager(
        handles={"gemini-worker": _FakeHandle("gemini-worker")},
        scripted_messages=[_msg("gemini-worker", _WORKER_RAW)],
    )
    backend = AcpBackend(manager_factory=lambda root: mgr)
    cfg = load_default_config()
    request = BackendRequest(
        role=AgentRole.WORKER,
        card=Card(title="t", goal="g"),
        prompt="hi",
        profile=cfg.get_profile("gemini-worker"),
        working_directory=None,
    )
    backend.invoke(request)
    assert mgr.seen_calls[-1][2] is None  # cwd forwarded as None, not "None"
