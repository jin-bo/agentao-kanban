"""Multi-agent executor backed by agentao sub-agent definitions.

Each role resolves to a `.agentao/agents/kanban-<role>.md` definition. The
executor constructs an ``Agentao`` instance per ``run()`` call, seeded with
that definition's ``system_instructions``, ``model``, ``temperature``, and
``max_turns``, and records the agent's ``prompt_version`` on the result.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from ..agents import AgentSpec, load_spec
from ..models import AgentResult, AgentRole, Card, CardStatus


_NEXT_STATUS: dict[AgentRole, CardStatus] = {
    AgentRole.PLANNER: CardStatus.READY,
    AgentRole.WORKER: CardStatus.REVIEW,
    AgentRole.REVIEWER: CardStatus.VERIFY,
    AgentRole.VERIFIER: CardStatus.DONE,
}

_OUTPUT_KEY: dict[AgentRole, str] = {
    AgentRole.PLANNER: "planner",
    AgentRole.WORKER: "implementation",
    AgentRole.REVIEWER: "review",
    AgentRole.VERIFIER: "verification",
}

_NEXT_OWNER: dict[AgentRole, AgentRole | None] = {
    AgentRole.PLANNER: None,
    AgentRole.WORKER: AgentRole.REVIEWER,
    AgentRole.REVIEWER: AgentRole.VERIFIER,
    AgentRole.VERIFIER: None,
}


class _AgentLike(Protocol):
    def chat(self, user_message: str, max_iterations: int = ...) -> str: ...


AgentFactory = Callable[[AgentSpec, Path | None], _AgentLike]


@dataclass
class AgentaoMultiAgentExecutor:
    """Route each role to its own agentao sub-agent definition."""

    agents_dir: Path | None = None
    working_directory: Path | None = None
    agent_factory: AgentFactory | None = None
    _specs: dict[AgentRole, AgentSpec] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.agent_factory is None:
            self.agent_factory = _default_agent_factory

    def spec_for(self, role: AgentRole) -> AgentSpec:
        spec = self._specs.get(role)
        if spec is None:
            spec = load_spec(role, self.agents_dir)
            self._specs[role] = spec
        return spec

    def run(self, role: AgentRole, card: Card) -> AgentResult:
        try:
            spec = self.spec_for(role)
        except FileNotFoundError as exc:
            return _blocked_result(role, f"agent definition missing: {exc}", None)

        prompt = _build_prompt(role, card)
        t0 = time.monotonic()
        try:
            agent = self.agent_factory(spec, self.working_directory)  # type: ignore[misc]
            raw = agent.chat(prompt, max_iterations=spec.max_turns)
        except Exception as exc:  # noqa: BLE001 — boundary to external harness
            elapsed = int((time.monotonic() - t0) * 1000)
            return _blocked_result(role, f"agentao call failed: {exc}", spec, elapsed, None)

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        parsed = _parse_response(raw)
        if parsed.get("ok") is False:
            reason = str(parsed.get("blocked_reason") or "Agent reported failure")
            return _blocked_result(role, reason, spec, elapsed_ms, raw)

        return _apply_parsed(role, card, parsed, spec, elapsed_ms, raw)


# ---------- prompt + parsing ----------


def _build_prompt(role: AgentRole, card: Card) -> str:
    header = {
        "id": card.id,
        "title": card.title,
        "goal": card.goal,
        "status": card.status.value,
        "acceptance_criteria": list(card.acceptance_criteria),
        "prior_outputs": dict(card.outputs),
    }
    parts = [
        f"You have been dispatched as the {role.value.upper()} for this card.",
        "",
        f"CARD:\n{json.dumps(header, indent=2, ensure_ascii=False)}",
    ]
    context_block = _render_context_block(card)
    if context_block:
        parts += ["", context_block]
    parts += [
        "",
        "Follow your role's system instructions. End with the required "
        "```json``` fenced block.",
    ]
    return "\n".join(parts)


def _render_context_block(card: Card) -> str:
    if not card.context_refs:
        return ""
    required = [r for r in card.context_refs if r.kind == "required"]
    optional = [r for r in card.context_refs if r.kind != "required"]
    sections: list[str] = []
    if required:
        sections.append("REQUIRED CONTEXT (must read before acting):")
        sections += [_format_ref(r) for r in required]
    if optional:
        if sections:
            sections.append("")
        sections.append("OPTIONAL CONTEXT (read if relevant):")
        sections += [_format_ref(r) for r in optional]
    return "\n".join(sections)


def _format_ref(ref: "Any") -> str:
    suffix = f" — {ref.note}" if ref.note else ""
    return f"- {ref.path}{suffix}"


_JSON_FENCE_START = re.compile(r"```json\s*", re.IGNORECASE)


def _parse_response(raw: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    last_obj: dict[str, Any] | None = None
    for match in _JSON_FENCE_START.finditer(raw):
        try:
            obj, _ = decoder.raw_decode(raw, match.end())
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            last_obj = obj
    if last_obj is not None:
        return last_obj
    first_line = raw.strip().splitlines()[0] if raw.strip() else ""
    return {
        "ok": True,
        "summary": first_line[:120] or "Agent produced unstructured output.",
        "output": raw.strip(),
    }


def _apply_parsed(
    role: AgentRole,
    card: Card,
    parsed: dict[str, Any],
    spec: AgentSpec,
    duration_ms: int,
    raw: str,
) -> AgentResult:
    summary = str(parsed.get("summary") or f"{role.value} completed.")
    output = parsed.get("output")
    updates: dict[str, Any] = {"owner_role": _NEXT_OWNER[role]}

    if role == AgentRole.PLANNER:
        criteria = parsed.get("acceptance_criteria")
        if isinstance(criteria, list) and criteria:
            updates["acceptance_criteria"] = [str(c) for c in criteria]
        elif not card.acceptance_criteria:
            updates["acceptance_criteria"] = [
                output if isinstance(output, str) and output else "Acceptance criteria TBD"
            ]
        if output is not None:
            outputs = dict(card.outputs)
            outputs[_OUTPUT_KEY[AgentRole.PLANNER]] = output
            updates["outputs"] = outputs
    else:
        key = _OUTPUT_KEY[role]
        outputs = dict(card.outputs)
        outputs[key] = output if output is not None else ""
        updates["outputs"] = outputs

    return AgentResult(
        role=role,
        summary=_tagged(summary, spec),
        next_status=_NEXT_STATUS[role],
        updates=updates,
        prompt_version=spec.version,
        duration_ms=duration_ms,
        raw_response=raw,
    )


def _blocked_result(
    role: AgentRole,
    reason: str,
    spec: AgentSpec | None,
    duration_ms: int = 0,
    raw: str | None = None,
) -> AgentResult:
    return AgentResult(
        role=role,
        summary=_tagged(f"{role.value} blocked: {reason}", spec),
        next_status=CardStatus.BLOCKED,
        updates={"blocked_reason": reason, "owner_role": None},
        prompt_version=spec.version if spec else "",
        duration_ms=duration_ms,
        raw_response=raw,
    )


def _tagged(summary: str, spec: AgentSpec | None) -> str:
    if spec is None:
        return summary
    return f"[{spec.name} v{spec.version}] {summary}"


# ---------- default factory ----------


def _default_agent_factory(spec: AgentSpec, working_directory: Path | None) -> _AgentLike:
    from agentao import Agentao  # lazy; raises ImportError if unavailable

    _load_home_dotenv()
    kwargs: dict[str, Any] = {"working_directory": working_directory}
    if spec.model:
        kwargs["model"] = spec.model
    if spec.temperature is not None:
        kwargs["temperature"] = spec.temperature
    agent = Agentao(**kwargs)
    # Propagate the role-specific system prompt into agentao's project
    # instructions hook so sub_agent.chat() prepends it to every turn.
    if spec.system_instructions:
        agent.project_instructions = spec.system_instructions
    return agent


def _load_home_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    home_env = Path.home() / ".env"
    if home_env.is_file():
        load_dotenv(home_env, override=False)
