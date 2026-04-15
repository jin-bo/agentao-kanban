from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from kanban.agent_profiles import load_default_config
from kanban.executors.backends.base import BackendRequest, BackendResponse
from kanban.executors.multi_backend import MultiBackendExecutor
from kanban.models import AgentRole, Card, CardStatus
from kanban.store_markdown import MarkdownBoardStore


REPO_AGENTS_DIR = Path(__file__).resolve().parent.parent / "kanban" / "defaults"

_OK_RAW = 'done\n```json\n{"ok": true, "summary": "ok", "output": "code"}\n```\n'


@dataclass
class _Backend:
    backend_type: str
    raw: str = _OK_RAW
    metadata: dict = field(default_factory=dict)

    def invoke(self, request: BackendRequest) -> BackendResponse:
        return BackendResponse(
            raw_text=self.raw,
            prompt_version="v1",
            spec_name=request.profile.name,
            metadata=dict(self.metadata),
        )


def test_agent_result_carries_profile_and_routing_metadata() -> None:
    backend = _Backend("acp", metadata={"session_id": "sess-abc", "stop_reason": "end_turn"})
    exec_ = MultiBackendExecutor(
        config=load_default_config(),
        agents_dir=REPO_AGENTS_DIR,
        backends={"subagent": _Backend("subagent"), "acp": backend},
    )
    card = Card(
        title="t",
        goal="g",
        status=CardStatus.DOING,
        agent_profile="gemini-worker",
        agent_profile_source="manual",
    )
    result = exec_.run(AgentRole.WORKER, card)

    assert result.agent_profile == "gemini-worker"
    assert result.backend_type == "acp"
    assert result.backend_target == "gemini-worker"
    assert result.routing_source == "card"
    assert "gemini-worker" in result.routing_reason
    assert result.fallback_from_profile is None
    assert result.session_id == "sess-abc"
    assert result.backend_metadata["stop_reason"] == "end_turn"


def test_fallback_sets_fallback_from_profile_and_keeps_used_backend() -> None:
    from agentao.acp_client import AcpClientError, AcpErrorCode

    @dataclass
    class _Boom:
        backend_type: str = "acp"
        def invoke(self, request):
            raise AcpClientError("t", code=AcpErrorCode.REQUEST_TIMEOUT)

    sub = _Backend("subagent")
    exec_ = MultiBackendExecutor(
        config=load_default_config(),
        agents_dir=REPO_AGENTS_DIR,
        backends={"subagent": sub, "acp": _Boom()},
    )
    card = Card(
        title="t",
        goal="g",
        status=CardStatus.DOING,
        agent_profile="gemini-worker",
        agent_profile_source="manual",
    )
    result = exec_.run(AgentRole.WORKER, card)
    # Fallback ran through the subagent profile.
    assert result.agent_profile == "default-worker"
    assert result.backend_type == "subagent"
    assert result.fallback_from_profile == "gemini-worker"


def test_default_routing_source_when_card_has_no_profile() -> None:
    exec_ = MultiBackendExecutor(
        config=load_default_config(),
        agents_dir=REPO_AGENTS_DIR,
        backends={"subagent": _Backend("subagent")},
    )
    result = exec_.run(AgentRole.WORKER, Card(title="t", goal="g", status=CardStatus.DOING))
    assert result.agent_profile == "default-worker"
    assert result.routing_source == "default"
    assert result.fallback_from_profile is None


def test_execution_event_persists_routing_fields(tmp_path: Path) -> None:
    store = MarkdownBoardStore(tmp_path)
    exec_ = MultiBackendExecutor(
        config=load_default_config(),
        agents_dir=REPO_AGENTS_DIR,
        backends={
            "subagent": _Backend("subagent"),
            "acp": _Backend("acp", metadata={"session_id": "s-42", "stop_reason": "end_turn"}),
        },
    )
    card = Card(
        title="t",
        goal="g",
        status=CardStatus.DOING,
        agent_profile="gemini-worker",
        agent_profile_source="manual",
    )
    store.add_card(card)
    result = exec_.run(AgentRole.WORKER, card)
    store.append_execution_event(card.id, result)

    # In-memory event carries the fields.
    events = store.list_execution_events(card_id=card.id)
    assert len(events) == 1
    ev = events[0]
    assert ev.agent_profile == "gemini-worker"
    assert ev.backend_type == "acp"
    assert ev.backend_target == "gemini-worker"
    assert ev.routing_source == "card"
    assert ev.session_id == "s-42"

    # The JSONL line on disk carries the fields, and reloading restores them.
    log_lines = (tmp_path / "events.log").read_text(encoding="utf-8").strip().splitlines()
    record = json.loads(log_lines[-1])
    assert record["agent_profile"] == "gemini-worker"
    assert record["backend_type"] == "acp"
    assert record["backend_target"] == "gemini-worker"
    assert record["routing_source"] == "card"
    assert record["session_id"] == "s-42"

    reloaded = MarkdownBoardStore(tmp_path)
    reloaded_events = reloaded.list_execution_events(card_id=card.id)
    assert reloaded_events[-1].agent_profile == "gemini-worker"
    assert reloaded_events[-1].session_id == "s-42"
    # Backend diagnostics survive board reload too — otherwise ACP
    # postmortem metadata only exists on the in-memory AgentResult.
    assert reloaded_events[-1].backend_metadata == {
        "session_id": "s-42",
        "stop_reason": "end_turn",
    }
    assert record["backend_metadata"] == {
        "session_id": "s-42",
        "stop_reason": "end_turn",
    }


def test_execution_event_omits_routing_fields_for_legacy_results(tmp_path: Path) -> None:
    from kanban.models import AgentResult

    store = MarkdownBoardStore(tmp_path)
    card = Card(title="t", goal="g", status=CardStatus.DOING)
    store.add_card(card)
    # Legacy executor path: no profile/backend fields populated.
    legacy_result = AgentResult(
        role=AgentRole.WORKER,
        summary="ok",
        next_status=CardStatus.REVIEW,
        prompt_version="v1",
        duration_ms=10,
        attempt=1,
    )
    store.append_execution_event(card.id, legacy_result)

    log_lines = (tmp_path / "events.log").read_text(encoding="utf-8").strip().splitlines()
    record = json.loads(log_lines[-1])
    # None-valued fields must not bloat the log with null keys.
    for absent in (
        "agent_profile",
        "backend_type",
        "backend_target",
        "routing_source",
        "routing_reason",
        "fallback_from_profile",
        "session_id",
        "router_prompt_version",
        "backend_metadata",
    ):
        assert absent not in record


def test_in_memory_store_preserves_router_prompt_version() -> None:
    """Regression: ``InMemoryBoardStore.append_execution_event`` must
    carry ``router_prompt_version`` onto the resulting ``CardEvent`` so
    the observability contract is identical to ``MarkdownBoardStore``."""
    from kanban.models import AgentResult
    from kanban.store import InMemoryBoardStore

    store = InMemoryBoardStore()
    card = Card(title="t", goal="g", status=CardStatus.DOING)
    store.add_card(card)

    result = AgentResult(
        role=AgentRole.WORKER,
        summary="ok",
        next_status=CardStatus.REVIEW,
        prompt_version="exec-v1",
        duration_ms=10,
        attempt=1,
        routing_source="policy",
        routing_reason="router selected gemini-worker",
        router_prompt_version="router-v9",
    )
    store.append_execution_event(card.id, result)

    exec_events = [e for e in store.events_for_card(card.id) if e.is_execution]
    assert len(exec_events) == 1
    assert exec_events[0].router_prompt_version == "router-v9"
    assert exec_events[0].routing_source == "policy"
