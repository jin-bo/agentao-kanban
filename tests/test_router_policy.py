from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from kanban.agent_profiles import (
    AgentProfileConfig,
    BackendSpec,
    ProfileSpec,
    RoleConfig,
    RouterConfig,
    load_default_config,
)
from kanban.executors.router_agent import (
    RouterCandidateProfile,
    RouterClient,
    RouterDecision,
    RouterFailureKind,
    RouterRequest,
    build_candidates,
    build_card_summary,
    render_request,
    _extract_json_object,
)
from kanban.executors.router_policy import ENV_KILL_SWITCH, RouterPolicy
from kanban.models import AgentRole, Card


# --------------------------- helpers -------------------------------------


def _card(**overrides) -> Card:
    return Card(title="t", goal="g", **overrides)


def _config_with_router(
    enabled: tuple[AgentRole, ...] = (AgentRole.WORKER,),
) -> AgentProfileConfig:
    cfg = load_default_config()
    cfg.router = RouterConfig(enabled_roles=frozenset(enabled), timeout_s=5.0)
    return cfg


@dataclass
class _FakeAgent:
    replies: list[str]
    calls: list[str] = field(default_factory=list)

    def chat(self, prompt: str, max_iterations: int = 2) -> str:
        self.calls.append(prompt)
        return self.replies.pop(0)


def _make_client(
    reply: str | Exception, *, timeout_s: float = 5.0
) -> tuple[RouterClient, _FakeAgent]:
    from kanban.agents import AgentSpec

    spec = AgentSpec(
        name="kanban-router",
        description="",
        version="test-v1",
        system_instructions="",
        max_turns=2,
        model=None,
        temperature=None,
        source_path=Path("<fake>"),
    )
    agent = _FakeAgent(replies=[reply] if isinstance(reply, str) else [])

    def factory(_spec, _cwd):
        if isinstance(reply, Exception):

            class _Boom:
                def chat(self_inner, *_a, **_k):
                    raise reply

            return _Boom()
        return agent

    client = RouterClient(spec=spec, agent_factory=factory, timeout_s=timeout_s)
    return client, agent


# --------------------------- policy tests --------------------------------


def test_policy_returns_profile_on_valid_selection() -> None:
    cfg = _config_with_router()
    client, _ = _make_client(
        json.dumps(
            {
                "profile": "gemini-worker",
                "reason": "coding task",
                "confidence": 0.9,
            }
        )
    )
    policy = RouterPolicy(client=client)
    card = _card()
    assert policy(AgentRole.WORKER, card, cfg) == "gemini-worker"
    out = policy.last_outcome(card.id, AgentRole.WORKER)
    assert out is not None
    assert out.router_invoked is True
    assert "gemini-worker" in out.reason
    assert out.prompt_version == "test-v1"


def test_policy_null_selection_returns_none() -> None:
    cfg = _config_with_router()
    client, _ = _make_client(json.dumps({"profile": None, "reason": "no fit"}))
    policy = RouterPolicy(client=client)
    card = _card()
    assert policy(AgentRole.WORKER, card, cfg) is None
    out = policy.last_outcome(card.id, AgentRole.WORKER)
    assert out is not None
    assert out.router_invoked is True
    assert "empty_choice" in out.reason


def test_policy_unknown_profile_is_invalid_choice() -> None:
    cfg = _config_with_router()
    client, _ = _make_client(json.dumps({"profile": "ghost-worker", "reason": "x"}))
    policy = RouterPolicy(client=client)
    card = _card()
    assert policy(AgentRole.WORKER, card, cfg) is None
    out = policy.last_outcome(card.id, AgentRole.WORKER)
    assert out is not None and "invalid_choice" in out.reason


def test_policy_cross_role_is_invalid_choice() -> None:
    cfg = _config_with_router()
    # "default-reviewer" is a real profile name but not a worker candidate.
    client, _ = _make_client(json.dumps({"profile": "default-reviewer", "reason": "x"}))
    policy = RouterPolicy(client=client)
    card = _card()
    assert policy(AgentRole.WORKER, card, cfg) is None
    out = policy.last_outcome(card.id, AgentRole.WORKER)
    assert out is not None and "invalid_choice" in out.reason


def test_policy_unparseable_output_is_parse_error() -> None:
    cfg = _config_with_router()
    client, _ = _make_client("not json at all")
    policy = RouterPolicy(client=client)
    card = _card()
    assert policy(AgentRole.WORKER, card, cfg) is None
    out = policy.last_outcome(card.id, AgentRole.WORKER)
    assert out is not None and "parse_error" in out.reason


def test_policy_backend_exception_is_backend_error() -> None:
    cfg = _config_with_router()
    client, _ = _make_client(RuntimeError("boom"))
    policy = RouterPolicy(client=client)
    card = _card()
    assert policy(AgentRole.WORKER, card, cfg) is None
    out = policy.last_outcome(card.id, AgentRole.WORKER)
    assert out is not None and "backend_error" in out.reason


def test_policy_single_candidate_short_circuits() -> None:
    # Verifier has only default-verifier in the default config.
    cfg = _config_with_router(enabled=(AgentRole.VERIFIER,))
    client, agent = _make_client(json.dumps({"profile": "default-verifier"}))
    policy = RouterPolicy(client=client)
    card = _card()
    assert policy(AgentRole.VERIFIER, card, cfg) is None
    out = policy.last_outcome(card.id, AgentRole.VERIFIER)
    assert out is not None
    assert out.router_invoked is False
    assert "single candidate" in out.reason
    assert agent.calls == []  # router was never invoked


def test_policy_kill_switch_bypasses(monkeypatch) -> None:
    cfg = _config_with_router()
    client, agent = _make_client(json.dumps({"profile": "gemini-worker"}))
    policy = RouterPolicy(client=client)
    monkeypatch.setenv(ENV_KILL_SWITCH, "off")
    card = _card()
    assert policy(AgentRole.WORKER, card, cfg) is None
    out = policy.last_outcome(card.id, AgentRole.WORKER)
    assert out is not None and "KANBAN_ROUTER=off" in out.reason
    assert agent.calls == []


def test_policy_per_role_allowlist() -> None:
    cfg = _config_with_router(enabled=(AgentRole.WORKER,))
    client, agent = _make_client(json.dumps({"profile": "gemini-reviewer"}))
    policy = RouterPolicy(client=client)
    card = _card()
    # Reviewer is NOT in enabled_roles.
    assert policy(AgentRole.REVIEWER, card, cfg) is None
    out = policy.last_outcome(card.id, AgentRole.REVIEWER)
    assert out is not None and out.router_invoked is False
    assert agent.calls == []


def test_policy_caches_decision() -> None:
    cfg = _config_with_router()
    reply = json.dumps({"profile": "gemini-worker", "reason": "r"})
    client, agent = _make_client(reply)
    policy = RouterPolicy(client=client)
    card = _card()
    # First call invokes; second should not.
    assert policy(AgentRole.WORKER, card, cfg) == "gemini-worker"
    # Preload one more reply in case cache fails — then assert it wasn't used.
    agent.replies.append(reply)
    assert policy(AgentRole.WORKER, card, cfg) == "gemini-worker"
    out = policy.last_outcome(card.id, AgentRole.WORKER)
    assert out is not None and out.cached is True
    assert "(cached)" in out.reason
    assert len(agent.calls) == 1


def test_policy_cache_key_changes_with_goal() -> None:
    cfg = _config_with_router()
    reply = json.dumps({"profile": "gemini-worker", "reason": "r"})
    client, agent = _make_client(reply)
    agent.replies.append(reply)  # second invocation needs its own reply
    policy = RouterPolicy(client=client)
    card = Card(title="t", goal="g1", id="card-1")
    policy(AgentRole.WORKER, card, cfg)
    card.goal = "g2"
    policy(AgentRole.WORKER, card, cfg)
    assert len(agent.calls) == 2


def test_policy_cache_key_changes_with_title() -> None:
    """Regression for P2: title/priority/context_refs are in the router
    request, so edits to them must invalidate the cache."""
    from kanban.models import CardPriority

    cfg = _config_with_router()
    reply = json.dumps({"profile": "gemini-worker", "reason": "r"})
    client, agent = _make_client(reply)
    agent.replies.append(reply)
    agent.replies.append(reply)
    policy = RouterPolicy(client=client)

    card = Card(title="first", goal="g", id="card-1")
    policy(AgentRole.WORKER, card, cfg)
    card.title = "second"
    policy(AgentRole.WORKER, card, cfg)
    card.priority = CardPriority.HIGH
    policy(AgentRole.WORKER, card, cfg)
    assert len(agent.calls) == 3


def test_policy_cache_key_changes_with_context_refs() -> None:
    from kanban.models import ContextRef

    cfg = _config_with_router()
    reply = json.dumps({"profile": "gemini-worker", "reason": "r"})
    client, agent = _make_client(reply)
    agent.replies.append(reply)
    policy = RouterPolicy(client=client)

    card = Card(title="t", goal="g", id="card-1")
    policy(AgentRole.WORKER, card, cfg)
    card.context_refs = [ContextRef(path="src/foo.py", kind="required", note="new")]
    policy(AgentRole.WORKER, card, cfg)
    assert len(agent.calls) == 2


def test_resolve_router_spec_path_falls_through_when_override_missing(tmp_path) -> None:
    """Regression: when agents_dir is given but lacks kanban-router.md,
    the resolver must still return the packaged default instead of None.
    A project with a local .agentao/agents/ for other custom agents
    should not silently lose routing."""
    from kanban.executors.router_agent import resolve_router_spec_path

    # agents_dir exists but contains no kanban-router.md.
    (tmp_path / ".agentao" / "agents").mkdir(parents=True)
    resolved = resolve_router_spec_path(tmp_path / ".agentao" / "agents")
    assert resolved is not None
    assert resolved.parent.name == "defaults"
    assert resolved.name == "kanban-router.md"


def test_resolve_router_spec_path_prefers_override(tmp_path) -> None:
    from kanban.executors.router_agent import resolve_router_spec_path

    override = tmp_path / "agents"
    override.mkdir()
    local = override / "kanban-router.md"
    local.write_text(
        "---\nname: kanban-router\nversion: local\nmax_turns: 1\n---\nhi\n",
        encoding="utf-8",
    )
    assert resolve_router_spec_path(override) == local


def test_policy_does_not_cache_timeout(monkeypatch) -> None:
    """Regression: a transient router timeout must not be memoized for
    the lifetime of the process. The next call with the same request
    must consult the router again."""
    from kanban.agents import AgentSpec
    from kanban.executors.router_agent import RouterClient

    spec = AgentSpec(
        name="kanban-router",
        description="",
        version="v",
        system_instructions="",
        max_turns=2,
        model=None,
        temperature=None,
        source_path=Path("<fake>"),
    )

    calls: list[str] = []

    class _Flaky:
        def __init__(self) -> None:
            self.first = True

        def chat(self, prompt: str, max_iterations: int = 2) -> str:
            calls.append(prompt)
            if self.first:
                self.first = False
                raise TimeoutError("slow")
            return json.dumps({"profile": "gemini-worker", "reason": "r"})

    flaky = _Flaky()
    client = RouterClient(
        spec=spec, agent_factory=lambda s, c: flaky, timeout_s=5.0
    )
    policy = RouterPolicy(client=client)
    cfg = _config_with_router()
    card = _card()

    # First call fails transiently → None, but must NOT be cached.
    assert policy(AgentRole.WORKER, card, cfg) is None
    # Second call with the same request goes through to the router again
    # and succeeds.
    assert policy(AgentRole.WORKER, card, cfg) == "gemini-worker"
    assert len(calls) == 2


def test_policy_does_not_cache_parse_error() -> None:
    cfg = _config_with_router()
    client, _ = _make_client("not json")
    policy = RouterPolicy(client=client)
    card = _card()
    assert policy(AgentRole.WORKER, card, cfg) is None
    # No entry in cache for this key → next call will re-route. We verify
    # by introspecting the private cache directly; it's the simplest
    # black-box check short of counting router invocations.
    assert policy._decision_cache == {}


def test_policy_caches_explicit_null_choice() -> None:
    """The router's deliberate ``"profile": null`` is a real decision and
    should be cached — only infrastructure failures are transient."""
    cfg = _config_with_router()
    client, agent = _make_client(json.dumps({"profile": None, "reason": "nothing fits"}))
    policy = RouterPolicy(client=client)
    card = _card()

    assert policy(AgentRole.WORKER, card, cfg) is None
    # Second call with the same request must reuse the cached null
    # decision rather than pestering the router again.
    agent.replies.append(json.dumps({"profile": "gemini-worker", "reason": "new"}))
    assert policy(AgentRole.WORKER, card, cfg) is None
    out = policy.last_outcome(card.id, AgentRole.WORKER)
    assert out is not None and out.cached is True
    assert len(agent.calls) == 1


def test_policy_missing_spec_disables_router(monkeypatch, tmp_path) -> None:
    # Point the agents_dir at an empty directory so no kanban-router.md exists.
    cfg = _config_with_router()
    # Also block the shipped fallback (the spec still exists at
    # kanban/defaults/, so we monkeypatch resolve_router_spec_path to
    # force a miss instead of relying on agents_dir alone).
    from kanban.executors import router_agent as ra

    def _fake_resolve(agents_dir: Path | None = None) -> Path | None:
        return None

    monkeypatch.setattr(ra, "resolve_router_spec_path", _fake_resolve)
    policy = RouterPolicy(agents_dir=tmp_path)
    card = _card()
    assert policy(AgentRole.WORKER, card, cfg) is None
    out = policy.last_outcome(card.id, AgentRole.WORKER)
    assert out is not None and "spec missing" in out.reason


# --------------------------- client parsing tests ------------------------


def test_extract_json_object_accepts_fenced() -> None:
    raw = 'some prose\n```json\n{"profile": "x", "reason": "y"}\n```\n'
    parsed = _extract_json_object(raw)
    assert parsed == {"profile": "x", "reason": "y"}


def test_extract_json_object_rejects_garbage() -> None:
    assert _extract_json_object("no json here") is None


def test_client_router_selects_default_name_is_still_policy_hit() -> None:
    cfg = _config_with_router()
    client, _ = _make_client(
        json.dumps({"profile": "default-worker", "reason": "simple task"})
    )
    policy = RouterPolicy(client=client)
    card = _card()
    # Router picked the default — still counts as a policy selection.
    assert policy(AgentRole.WORKER, card, cfg) == "default-worker"
    out = policy.last_outcome(card.id, AgentRole.WORKER)
    assert out is not None
    assert out.router_invoked is True
    assert "default-worker" in out.reason


# --------------------------- builder tests -------------------------------


def test_build_candidates_filters_by_role() -> None:
    cfg = load_default_config()
    workers = build_candidates(AgentRole.WORKER, cfg.profiles)
    names = [c.name for c in workers]
    assert "default-worker" in names
    assert "gemini-worker" in names
    assert all(c.role == AgentRole.WORKER for c in workers)
    # No reviewer profiles leak in.
    assert "default-reviewer" not in names


def test_render_request_is_stable_json() -> None:
    cfg = load_default_config()
    card = Card(title="t", goal="g", id="c1")
    req = RouterRequest(
        card=build_card_summary(card, AgentRole.WORKER),
        candidates=build_candidates(AgentRole.WORKER, cfg.profiles),
    )
    a = render_request(req)
    b = render_request(req)
    assert a == b
    payload = json.loads(a)
    assert payload["card"]["card_id"] == "c1"
    assert payload["card"]["role"] == "worker"
    assert isinstance(payload["candidates"], list)
    assert all(c["role"] == "worker" for c in payload["candidates"])
