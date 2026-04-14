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
        # Missing agent definition is a config error — not retryable.
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
            # Let the infrastructure error surface. WorkerDaemon catches it
            # and submits ok=False with FailureCategory.INFRASTRUCTURE, which
            # the retry matrix honors (2 retries before BLOCKED). Treating
            # a transient LLM 5xx / network blip as terminal here would turn
            # every flake into a manual unblock.
            raise RuntimeError(
                f"agentao call failed ({role.value}): {exc}"
            ) from exc

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        parsed = _parse_response(raw)
        # Agent self-declared failure = functional rejection. The matrix
        # treats this as unretryable (agent ran fine, said no).
        if parsed.get("ok") is False:
            reason = str(parsed.get("blocked_reason") or "Agent reported failure")
            return _blocked_result(role, reason, spec, elapsed_ms, raw)

        if role == AgentRole.PLANNER:
            if not parsed.get("_structured"):
                # Format drift (no ```json``` fence). Raise so the retry
                # matrix can recover — a transient LLM formatting miss
                # should not require manual recovery.
                raise RuntimeError(
                    "planner returned unstructured response "
                    "(no ```json``` fence); retrying via infrastructure path"
                )
            criteria = parsed.get("acceptance_criteria")
            normalized: list[str] = []
            if isinstance(criteria, list):
                normalized = [str(c).strip() for c in criteria if str(c).strip()]
            # Only require fresh criteria when the card has none. Replans
            # for a card that already has valid acceptance_criteria may
            # legitimately omit the list — _apply_parsed preserves the
            # existing criteria in that case.
            if not normalized and not card.acceptance_criteria:
                return _blocked_result(
                    role,
                    "planner must return 2-5 non-empty acceptance_criteria",
                    spec,
                    elapsed_ms,
                    raw,
                )

            # Contract-downgrade guard: when a replan returns a NEW list,
            # every criterion the card already carried that is NOT in the
            # new list must be explicitly recorded under
            # output.superseded[] with a non-empty reason. Otherwise the
            # planner could silently drop an unmet criterion and the card
            # could advance without anyone proving the original
            # requirement was satisfied.
            if normalized and card.acceptance_criteria:
                drop_error = _validate_supersession(
                    existing=list(card.acceptance_criteria),
                    new=normalized,
                    output=parsed.get("output"),
                )
                if drop_error is not None:
                    return _blocked_result(role, drop_error, spec, elapsed_ms, raw)

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
        last_obj["_structured"] = True
        return last_obj
    first_line = raw.strip().splitlines()[0] if raw.strip() else ""
    return {
        "_structured": False,
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
        normalized = []
        if isinstance(criteria, list):
            normalized = [str(c).strip() for c in criteria if str(c).strip()]
        if normalized:
            updates["acceptance_criteria"] = normalized
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


def _validate_supersession(
    *,
    existing: list[str],
    new: list[str],
    output: Any,
) -> str | None:
    """Return an error message if the replan drops prior criteria without
    recording each in ``output.superseded``, else None.

    Dropped means: present in ``existing`` (exact text match) but not in
    ``new``. Every dropped criterion must appear as ``{criterion, reason}``
    under ``output.superseded`` with a non-empty reason. Adding new
    criteria never requires a supersession record.
    """
    new_set = set(new)
    dropped = [c for c in existing if c not in new_set]
    if not dropped:
        return None

    superseded_raw = output.get("superseded") if isinstance(output, dict) else None
    if not isinstance(superseded_raw, list):
        return (
            "planner replan dropped "
            f"{len(dropped)} prior acceptance_criteria without "
            "`output.superseded`; silent contract downgrades are not allowed"
        )

    recorded: dict[str, str] = {}
    for entry in superseded_raw:
        if not isinstance(entry, dict):
            continue
        text = str(entry.get("criterion", "")).strip()
        reason = str(entry.get("reason", "")).strip()
        if text and reason:
            recorded[text] = reason

    missing = [c for c in dropped if c not in recorded]
    if missing:
        shown = ", ".join(repr(c) for c in missing[:3])
        more = "" if len(missing) <= 3 else f" (+{len(missing) - 3} more)"
        return (
            f"planner replan dropped {len(missing)} prior acceptance_criteria "
            f"without a matching `output.superseded` entry ({shown}{more}); "
            "each dropped criterion needs an explicit reason"
        )
    return None


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
