"""Subagent backend: executes a profile via `.agentao/agents/<target>.md`.

This is the profile-aware counterpart of `AgentaoMultiAgentExecutor`'s inline
subagent path. The backend knows nothing about workflow transitions or JSON
parsing — it loads the spec named by `profile.backend.target`, instantiates
an `Agentao` agent, calls `.chat()`, and returns the raw text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from ...agents import AgentSpec, load_spec_by_name
from .base import BackendRequest, BackendResponse


class SubagentSpecMissing(FileNotFoundError):
    """Raised when the agent definition file for a subagent target is absent.

    Distinct from a generic ``FileNotFoundError`` raised *inside* an agent
    turn (e.g. the agent opened a repo file that disappeared), so the
    executor can treat config-missing as terminal while still letting
    in-run file errors flow through the retry path.
    """


class _AgentLike(Protocol):
    def chat(self, user_message: str, max_iterations: int = ...) -> str: ...


AgentFactory = Callable[[AgentSpec, Path | None], _AgentLike]


@dataclass
class SubagentBackend:
    """Run a resolved profile through an agentao subagent definition."""

    agents_dir: Path | None = None
    agent_factory: AgentFactory | None = None
    _specs: dict[str, AgentSpec] = field(default_factory=dict, init=False, repr=False)

    backend_type: str = field(default="subagent", init=False)

    def __post_init__(self) -> None:
        if self.agent_factory is None:
            from ..agentao_multi import _default_agent_factory  # reuse dotenv/Agentao wiring
            self.agent_factory = _default_agent_factory

    def spec_for(self, target: str) -> AgentSpec:
        spec = self._specs.get(target)
        if spec is None:
            try:
                spec = load_spec_by_name(target, self.agents_dir)
            except FileNotFoundError as exc:
                raise SubagentSpecMissing(str(exc)) from exc
            self._specs[target] = spec
        return spec

    def invoke(self, request: BackendRequest) -> BackendResponse:
        target = request.profile.backend.target
        spec = self.spec_for(target)
        agent = self.agent_factory(spec, request.working_directory)  # type: ignore[misc]
        raw = agent.chat(request.prompt, max_iterations=spec.max_turns)
        metadata: dict[str, Any] = {
            "backend_target": target,
            "spec_version": spec.version,
        }
        return BackendResponse(
            raw_text=raw,
            prompt_version=spec.version,
            spec_name=spec.name,
            metadata=metadata,
        )
