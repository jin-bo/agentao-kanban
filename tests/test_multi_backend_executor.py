from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from kanban.agent_profiles import load_default_config
from kanban.executors.backends.base import Backend, BackendRequest, BackendResponse
from kanban.executors.backends.subagent_backend import SubagentBackend
from kanban.executors.multi_backend import MultiBackendExecutor
from kanban.models import AgentRole, Card, CardStatus


REPO_AGENTS_DIR = Path(__file__).resolve().parent.parent / "kanban" / "defaults"


@dataclass
class _RecordingBackend:
    backend_type: str
    raw: str
    seen: list[BackendRequest] = field(default_factory=list)

    def invoke(self, request: BackendRequest) -> BackendResponse:
        self.seen.append(request)
        return BackendResponse(
            raw_text=self.raw,
            prompt_version="test-v1",
            spec_name=f"test-{request.profile.name}",
            metadata={"backend_target": request.profile.backend.target},
        )


_WORKER_RAW = """done\n```json\n{"ok": true, "summary": "did it", "output": "code"}\n```\n"""


def test_multi_backend_routes_through_subagent_default() -> None:
    backend = _RecordingBackend(backend_type="subagent", raw=_WORKER_RAW)
    exec_ = MultiBackendExecutor(
        config=load_default_config(),
        agents_dir=REPO_AGENTS_DIR,
        backends={"subagent": backend},
    )
    card = Card(title="t", goal="g", status=CardStatus.DOING)
    result = exec_.run(AgentRole.WORKER, card)

    assert result.next_status == CardStatus.REVIEW
    assert result.prompt_version == "test-v1"
    assert result.updates["outputs"]["implementation"] == "code"
    assert len(backend.seen) == 1
    assert backend.seen[0].profile.name == "default-worker"


def test_card_agent_profile_overrides_default() -> None:
    sub = _RecordingBackend(backend_type="subagent", raw=_WORKER_RAW)
    acp = _RecordingBackend(backend_type="acp", raw=_WORKER_RAW)
    exec_ = MultiBackendExecutor(
        config=load_default_config(),
        agents_dir=REPO_AGENTS_DIR,
        backends={"subagent": sub, "acp": acp},
    )
    card = Card(
        title="t",
        goal="g",
        status=CardStatus.DOING,
        agent_profile="gemini-worker",
        agent_profile_source="manual",
    )
    exec_.run(AgentRole.WORKER, card)

    # gemini-worker has backend.type=acp → acp backend invoked, not subagent.
    assert len(acp.seen) == 1
    assert len(sub.seen) == 0
    assert acp.seen[0].profile.name == "gemini-worker"


def test_router_policy_selection_populates_event_fields() -> None:
    """When the attached policy is a RouterPolicy and it picks a profile,
    the AgentResult must carry router_prompt_version and a routing_reason
    that reflects the router's explanation — not the resolver's terse
    default string."""
    import json as _json
    from dataclasses import dataclass as _dc, field as _fld
    from pathlib import Path as _P

    from kanban.agent_profiles import RouterConfig
    from kanban.agents import AgentSpec
    from kanban.executors.router_agent import RouterClient
    from kanban.executors.router_policy import RouterPolicy

    spec = AgentSpec(
        name="kanban-router",
        description="",
        version="router-v9",
        system_instructions="",
        max_turns=2,
        model=None,
        temperature=None,
        source_path=_P("<fake>"),
    )

    @_dc
    class _FakeRouterAgent:
        calls: list[str] = _fld(default_factory=list)

        def chat(self, prompt: str, max_iterations: int = 2) -> str:
            self.calls.append(prompt)
            return _json.dumps(
                {"profile": "gemini-worker", "reason": "shell-heavy work"}
            )

    fake = _FakeRouterAgent()
    client = RouterClient(
        spec=spec, agent_factory=lambda s, c: fake, timeout_s=5.0
    )
    policy = RouterPolicy(client=client)

    cfg = load_default_config()
    cfg.router = RouterConfig(enabled_roles=frozenset({AgentRole.WORKER}), timeout_s=5.0)

    sub = _RecordingBackend("subagent", _WORKER_RAW)
    acp = _RecordingBackend("acp", _WORKER_RAW)
    exec_ = MultiBackendExecutor(
        config=cfg,
        agents_dir=REPO_AGENTS_DIR,
        backends={"subagent": sub, "acp": acp},
        policy=policy,
    )

    card = Card(title="t", goal="edit many files", status=CardStatus.DOING)
    result = exec_.run(AgentRole.WORKER, card)

    assert result.agent_profile == "gemini-worker"
    assert result.routing_source == "policy"
    assert "gemini-worker" in (result.routing_reason or "")
    assert "shell-heavy work" in (result.routing_reason or "")
    assert result.router_prompt_version == "router-v9"
    assert len(acp.seen) == 1
    assert len(fake.calls) == 1


def test_router_prompt_version_not_inherited_when_pin_wins_on_later_run() -> None:
    """Regression for P3: a card that was previously routed by the policy
    and then pinned (or reached via planner recommendation) must not carry
    a stale router_prompt_version into the pinned run."""
    import json as _json
    from dataclasses import dataclass as _dc, field as _fld
    from pathlib import Path as _P

    from kanban.agent_profiles import RouterConfig
    from kanban.agents import AgentSpec
    from kanban.executors.router_agent import RouterClient
    from kanban.executors.router_policy import RouterPolicy

    spec = AgentSpec(
        name="kanban-router",
        description="",
        version="router-v7",
        system_instructions="",
        max_turns=2,
        model=None,
        temperature=None,
        source_path=_P("<fake>"),
    )

    @_dc
    class _FakeRouterAgent:
        calls: list[str] = _fld(default_factory=list)

        def chat(self, prompt: str, max_iterations: int = 2) -> str:
            self.calls.append(prompt)
            return _json.dumps({"profile": "gemini-worker", "reason": "r"})

    fake = _FakeRouterAgent()
    client = RouterClient(spec=spec, agent_factory=lambda s, c: fake, timeout_s=5.0)
    policy = RouterPolicy(client=client)
    cfg = load_default_config()
    cfg.router = RouterConfig(enabled_roles=frozenset({AgentRole.WORKER}), timeout_s=5.0)

    sub = _RecordingBackend("subagent", _WORKER_RAW)
    acp = _RecordingBackend("acp", _WORKER_RAW)
    exec_ = MultiBackendExecutor(
        config=cfg,
        agents_dir=REPO_AGENTS_DIR,
        backends={"subagent": sub, "acp": acp},
        policy=policy,
    )

    # Run 1: no pin → policy invoked, router picks gemini-worker.
    card = Card(title="t", goal="g", status=CardStatus.DOING, id="card-stable")
    first = exec_.run(AgentRole.WORKER, card)
    assert first.routing_source == "policy"
    assert first.router_prompt_version == "router-v7"

    # Run 2: operator pins the card to default-worker. Policy is NOT
    # consulted this turn — the prior router outcome is stale.
    card.status = CardStatus.DOING
    card.agent_profile = "default-worker"
    card.agent_profile_source = "manual"
    second = exec_.run(AgentRole.WORKER, card)
    assert second.routing_source == "card"
    assert second.router_prompt_version is None
    assert "gemini-worker" not in (second.routing_reason or "")


def test_router_disabled_for_role_leaves_router_prompt_version_unset() -> None:
    """When the router is not enabled for the role (or short-circuited),
    the result must NOT carry router_prompt_version — the field exists
    exactly to signal that the router was consulted."""
    from kanban.agent_profiles import RouterConfig
    from kanban.executors.router_policy import RouterPolicy

    policy = RouterPolicy()  # no client; missing-spec path is fine
    cfg = load_default_config()
    # Router disabled for every role.
    cfg.router = RouterConfig(enabled_roles=frozenset(), timeout_s=5.0)

    sub = _RecordingBackend("subagent", _WORKER_RAW)
    exec_ = MultiBackendExecutor(
        config=cfg,
        agents_dir=REPO_AGENTS_DIR,
        backends={"subagent": sub},
        policy=policy,
    )
    card = Card(title="t", goal="g", status=CardStatus.DOING)
    result = exec_.run(AgentRole.WORKER, card)

    assert result.routing_source == "default"
    assert result.router_prompt_version is None


def test_missing_backend_type_blocks_card() -> None:
    # Card routes to an ACP profile, but no ACP backend is registered.
    exec_ = MultiBackendExecutor(
        config=load_default_config(),
        agents_dir=REPO_AGENTS_DIR,
        backends={"subagent": _RecordingBackend("subagent", _WORKER_RAW)},
    )
    card = Card(
        title="t",
        goal="g",
        status=CardStatus.DOING,
        agent_profile="gemini-worker",  # backend.type = acp
        agent_profile_source="manual",
    )
    result = exec_.run(AgentRole.WORKER, card)
    assert result.next_status == CardStatus.BLOCKED
    assert "no backend registered" in result.updates["blocked_reason"]


def test_unknown_card_profile_blocks() -> None:
    exec_ = MultiBackendExecutor(
        config=load_default_config(),
        agents_dir=REPO_AGENTS_DIR,
        backends={"subagent": _RecordingBackend("subagent", _WORKER_RAW)},
    )
    card = Card(
        title="t",
        goal="g",
        status=CardStatus.DOING,
        agent_profile="ghost",
        agent_profile_source="manual",
    )
    result = exec_.run(AgentRole.WORKER, card)
    assert result.next_status == CardStatus.BLOCKED
    assert "profile resolution failed" in result.updates["blocked_reason"]


def test_agent_refusal_blocks_card() -> None:
    refused = """nope\n```json\n{"ok": false, "blocked_reason": "unclear goal"}\n```\n"""
    exec_ = MultiBackendExecutor(
        config=load_default_config(),
        agents_dir=REPO_AGENTS_DIR,
        backends={"subagent": _RecordingBackend("subagent", refused)},
    )
    card = Card(title="t", goal="g", status=CardStatus.DOING)
    result = exec_.run(AgentRole.WORKER, card)
    assert result.next_status == CardStatus.BLOCKED
    assert "unclear goal" in result.updates["blocked_reason"]


def test_backend_exception_raises_for_retry() -> None:
    class _Boom:
        backend_type = "subagent"
        def invoke(self, request: BackendRequest) -> BackendResponse:
            raise RuntimeError("boom")

    exec_ = MultiBackendExecutor(
        config=load_default_config(),
        agents_dir=REPO_AGENTS_DIR,
        backends={"subagent": _Boom()},
    )
    card = Card(title="t", goal="g", status=CardStatus.DOING)
    with pytest.raises(RuntimeError, match="backend subagent call failed"):
        exec_.run(AgentRole.WORKER, card)


def test_subagent_backend_loads_spec_by_target() -> None:
    class _FakeAgent:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int]] = []
        def chat(self, message: str, max_iterations: int = 15) -> str:
            self.calls.append((message, max_iterations))
            return _WORKER_RAW

    fake = _FakeAgent()
    backend = SubagentBackend(
        agents_dir=REPO_AGENTS_DIR,
        agent_factory=lambda spec, wd: fake,
    )
    exec_ = MultiBackendExecutor(
        config=load_default_config(),
        agents_dir=REPO_AGENTS_DIR,
        backends={"subagent": backend},
    )
    card = Card(title="t", goal="g", status=CardStatus.DOING)
    result = exec_.run(AgentRole.WORKER, card)

    assert result.next_status == CardStatus.REVIEW
    assert len(fake.calls) == 1
    # The spec's version should propagate through to the result.
    assert result.prompt_version  # pulled from the loaded kanban-worker.md
