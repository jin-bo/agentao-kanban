"""Phase 8: workflow integration for MultiBackendExecutor.

Proves that the packaged profile config + Phase 3 executor drive a card end
to end through the orchestrator, routing each role through the right
backend. Uses the limited-rollout mapping from the implementation plan:

    planner  -> default-planner   (subagent)
    worker   -> gemini-worker      (acp, falls back to default-worker)
    reviewer -> gemini-reviewer    (acp, falls back to default-reviewer)
    verifier -> default-verifier  (subagent)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from kanban.agent_profiles import RoleConfig, load_default_config
from kanban.executors.backends.base import BackendRequest, BackendResponse
from kanban.executors.multi_backend import MultiBackendExecutor
from kanban.models import AgentRole, Card, CardStatus
from kanban.orchestrator import KanbanOrchestrator
from kanban.store_markdown import MarkdownBoardStore


REPO_AGENTS_DIR = Path(__file__).resolve().parent.parent / "kanban" / "defaults"


def _rollout_config():
    """Flip worker/reviewer defaults to the ACP-backed profiles."""
    cfg = load_default_config()
    cfg.roles[AgentRole.WORKER] = RoleConfig(default_profile="gemini-worker")
    cfg.roles[AgentRole.REVIEWER] = RoleConfig(default_profile="gemini-reviewer")
    return cfg


def _raw(role: AgentRole, card_title: str) -> str:
    if role == AgentRole.PLANNER:
        body = {
            "ok": True,
            "summary": "plan",
            "acceptance_criteria": ["A", "B", "C"],
            "output": {"plan": "go"},
        }
    elif role == AgentRole.WORKER:
        body = {"ok": True, "summary": f"did {card_title}", "output": "code"}
    elif role == AgentRole.REVIEWER:
        body = {"ok": True, "summary": "lgtm", "output": "ok"}
    else:  # VERIFIER
        body = {"ok": True, "summary": "verified", "output": "ok"}
    return f"... something ...\n```json\n{json.dumps(body)}\n```\n"


@dataclass
class _ScriptedBackend:
    backend_type: str
    acting_for: list[AgentRole] = field(default_factory=list)  # which roles ran through me
    metadata_per_profile: dict[str, dict] = field(default_factory=dict)

    def invoke(self, request: BackendRequest) -> BackendResponse:
        self.acting_for.append(request.role)
        return BackendResponse(
            raw_text=_raw(request.role, request.card.title),
            prompt_version="v1",
            spec_name=request.profile.name,
            metadata=self.metadata_per_profile.get(request.profile.name, {}),
        )


def test_card_completes_end_to_end_with_rollout_routing(tmp_path: Path) -> None:
    store = MarkdownBoardStore(tmp_path)
    acp = _ScriptedBackend(
        backend_type="acp",
        metadata_per_profile={
            "gemini-worker": {"session_id": "s-w", "stop_reason": "end_turn"},
            "gemini-reviewer": {"session_id": "s-r", "stop_reason": "end_turn"},
        },
    )
    subagent = _ScriptedBackend(backend_type="subagent")
    executor = MultiBackendExecutor(
        config=_rollout_config(),
        agents_dir=REPO_AGENTS_DIR,
        backends={"subagent": subagent, "acp": acp},
    )
    orch = KanbanOrchestrator(store=store, executor=executor)
    card = orch.create_card(title="ship-it", goal="do thing")
    orch.run_until_idle(max_steps=20)

    reloaded = MarkdownBoardStore(tmp_path).get_card(card.id)
    assert reloaded.status == CardStatus.DONE

    # ACP backend handled worker + reviewer; subagent handled planner + verifier.
    assert AgentRole.WORKER in acp.acting_for
    assert AgentRole.REVIEWER in acp.acting_for
    assert AgentRole.PLANNER in subagent.acting_for
    assert AgentRole.VERIFIER in subagent.acting_for
    assert AgentRole.WORKER not in subagent.acting_for
    assert AgentRole.REVIEWER not in subagent.acting_for

    # Each execution event carries the profile/backend/session it ran through.
    events = store.list_execution_events(card_id=card.id)
    by_role = {e.role: e for e in events if e.role is not None}
    assert by_role[AgentRole.PLANNER].agent_profile == "default-planner"
    assert by_role[AgentRole.PLANNER].backend_type == "subagent"
    assert by_role[AgentRole.WORKER].agent_profile == "gemini-worker"
    assert by_role[AgentRole.WORKER].backend_type == "acp"
    assert by_role[AgentRole.WORKER].session_id == "s-w"
    assert by_role[AgentRole.REVIEWER].agent_profile == "gemini-reviewer"
    assert by_role[AgentRole.REVIEWER].session_id == "s-r"
    assert by_role[AgentRole.VERIFIER].agent_profile == "default-verifier"
    # Routing source is default for every role (no card override in this test).
    assert {e.routing_source for e in events if e.routing_source} == {"default"}


def test_workflow_fallback_on_acp_infra_failure_keeps_card_moving(tmp_path: Path) -> None:
    """If the worker's ACP backend suffers transport failure, the card
    should still complete via the fallback subagent profile — the
    orchestrator shouldn't have to know about the fallback at all.
    """
    from agentao.acp_client import AcpClientError, AcpErrorCode

    store = MarkdownBoardStore(tmp_path)

    @dataclass
    class _FlakyAcp:
        backend_type: str = "acp"
        fail_for_roles: tuple[AgentRole, ...] = (AgentRole.WORKER,)
        calls: int = 0

        def invoke(self, request: BackendRequest) -> BackendResponse:
            self.calls += 1
            if request.role in self.fail_for_roles:
                raise AcpClientError("dropped", code=AcpErrorCode.TRANSPORT_DISCONNECT)
            return BackendResponse(
                raw_text=_raw(request.role, request.card.title),
                prompt_version="v1",
                spec_name=request.profile.name,
            )

    flaky = _FlakyAcp()
    subagent = _ScriptedBackend(backend_type="subagent")
    executor = MultiBackendExecutor(
        config=_rollout_config(),
        agents_dir=REPO_AGENTS_DIR,
        backends={"subagent": subagent, "acp": flaky},
    )
    orch = KanbanOrchestrator(store=store, executor=executor)
    card = orch.create_card(title="flake", goal="go")
    orch.run_until_idle(max_steps=20)

    assert MarkdownBoardStore(tmp_path).get_card(card.id).status == CardStatus.DONE

    worker_events = [
        e for e in store.list_execution_events(card_id=card.id)
        if e.role == AgentRole.WORKER
    ]
    # The single worker event reflects the actual (fallback) profile.
    assert len(worker_events) == 1
    assert worker_events[0].agent_profile == "default-worker"
    assert worker_events[0].backend_type == "subagent"
    assert worker_events[0].fallback_from_profile == "gemini-worker"


def test_card_pinned_to_worker_profile_still_completes_all_stages(tmp_path: Path) -> None:
    """Regression: a card pinned to a role-specific profile like
    ``gemini-worker`` must not block planning/review/verify stages.
    The pin should only apply when the executor runs the matching role;
    other roles fall through to their defaults.
    """
    store = MarkdownBoardStore(tmp_path)
    acp = _ScriptedBackend(backend_type="acp")
    subagent = _ScriptedBackend(backend_type="subagent")
    # Packaged defaults: planner/worker/reviewer/verifier all on subagent.
    # Pinning the card to gemini-worker (worker-only, acp) must not
    # cause the planner step to raise a role-mismatch error.
    executor = MultiBackendExecutor(
        config=load_default_config(),
        agents_dir=REPO_AGENTS_DIR,
        backends={"subagent": subagent, "acp": acp},
    )
    orch = KanbanOrchestrator(store=store, executor=executor)
    card = orch.create_card(title="pinned", goal="go")
    # Pin after creation so the planner still runs first.
    store.update_card(
        card.id, agent_profile="gemini-worker", agent_profile_source="manual"
    )
    orch.run_until_idle(max_steps=20)

    assert MarkdownBoardStore(tmp_path).get_card(card.id).status == CardStatus.DONE

    events = store.list_execution_events(card_id=card.id)
    by_role = {e.role: e for e in events if e.role is not None}
    # Planner/reviewer/verifier ran through subagent defaults.
    assert by_role[AgentRole.PLANNER].backend_type == "subagent"
    assert by_role[AgentRole.PLANNER].routing_source == "default"
    assert by_role[AgentRole.REVIEWER].backend_type == "subagent"
    assert by_role[AgentRole.VERIFIER].backend_type == "subagent"
    # Worker honored the card pin.
    assert by_role[AgentRole.WORKER].backend_type == "acp"
    assert by_role[AgentRole.WORKER].agent_profile == "gemini-worker"
    assert by_role[AgentRole.WORKER].routing_source == "card"


def test_default_config_still_defaults_to_subagent() -> None:
    """Regression guard: rollout is opt-in. Ship with subagent defaults so
    a repo without .agentao/acp.json still boots with the multi-backend
    executor.
    """
    cfg = load_default_config()
    for role in (AgentRole.PLANNER, AgentRole.WORKER, AgentRole.REVIEWER, AgentRole.VERIFIER):
        default = cfg.default_profile_for(role)
        assert default.backend.type == "subagent", (
            f"default profile for {role.value} is {default.backend.type!r}; "
            "flipping to acp would break CI without ACP config"
        )
