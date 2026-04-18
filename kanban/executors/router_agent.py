"""Router agent client: invokes the `kanban-router` subagent and validates
its JSON output.

The router is a profile *selector*, not an executor. It only proposes a
profile name from the whitelist this module passes in; the host
(`router_policy.RouterPolicy`) is responsible for enforcing the whitelist
and falling through to the role default on any failure.

All shared router types live here so `router_policy` only consumes them;
keeping them in one module avoids a circular import between policy and
client.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable

from ..agent_profiles import ProfileSpec
from ..agents import AgentSpec, parse_spec_file
from ..models import AgentRole, Card, ContextRef


# ---------- Input / output types -----------------------------------------


@dataclass(slots=True, frozen=True)
class RouterCardSummary:
    card_id: str
    title: str
    goal: str
    role: AgentRole
    priority: str
    acceptance_criteria: tuple[str, ...]
    context_refs: tuple[dict[str, str], ...]
    current_agent_profile: str | None
    # Bumped each time the orchestrator accepts a reviewer/verifier rework
    # ask. Surfaced in the router input so per-process cache keys
    # naturally diverge in the split scheduler/worker topology — a worker
    # daemon's RouterPolicy._decision_cache is in a different process
    # from the scheduler that called ``invalidate_card``. Routers may
    # also use it to bias toward a more capable profile after repeated
    # reworks.
    rework_iteration: int = 0


@dataclass(slots=True, frozen=True)
class RouterCandidateProfile:
    name: str
    role: AgentRole
    backend_type: str
    backend_target: str
    fallback: str | None
    capabilities: tuple[str, ...]
    description: str


@dataclass(slots=True, frozen=True)
class RouterRequest:
    card: RouterCardSummary
    candidates: tuple[RouterCandidateProfile, ...]


class RouterFailureKind(StrEnum):
    PARSE_ERROR = "parse_error"
    INVALID_CHOICE = "invalid_choice"
    TIMEOUT = "timeout"
    BACKEND_ERROR = "backend_error"
    EMPTY_CHOICE = "empty_choice"  # router explicitly returned null
    SPEC_MISSING = "spec_missing"


@dataclass(slots=True, frozen=True)
class RouterDecision:
    """Outcome of a single router call.

    On success, ``profile`` is the chosen profile name and ``failure`` is
    None. On any non-success outcome — including the explicit
    ``"profile": null`` case — ``profile`` is None and ``failure`` names
    the reason. ``prompt_version`` is recorded only when the router was
    actually invoked (not on spec-missing / skipped paths).
    """

    profile: str | None
    reason: str
    confidence: float | None = None
    failure: RouterFailureKind | None = None
    prompt_version: str | None = None


# ---------- Spec loading --------------------------------------------------


def resolve_router_spec_path(agents_dir: Path | None = None) -> Path | None:
    """Return the router spec path, using this precedence:

    1. ``<agents_dir>/kanban-router.md`` (when caller passed an explicit
       override directory) or ``<cwd>/.agentao/agents/kanban-router.md``
       (when no directory was passed)
    2. ``<install>/kanban/defaults/kanban-router.md`` (packaged fallback)

    Falls back per-file, not per-directory, so an operator who points at
    a local ``.agentao/agents/`` for other custom agents but has no
    ``kanban-router.md`` still gets the packaged router. Returns None
    only when neither location has the file — the caller treats that as
    "router disabled" rather than raising.
    """
    if agents_dir is not None:
        primary = agents_dir / "kanban-router.md"
    else:
        primary = Path.cwd() / ".agentao" / "agents" / "kanban-router.md"
    if primary.is_file():
        return primary
    shipped = (
        Path(__file__).resolve().parent.parent
        / "defaults"
        / "kanban-router.md"
    )
    if shipped.is_file():
        return shipped
    return None


def load_router_spec(agents_dir: Path | None = None) -> AgentSpec | None:
    path = resolve_router_spec_path(agents_dir)
    if path is None:
        return None
    return parse_spec_file(path)


# ---------- Candidate / request builders ---------------------------------


def build_card_summary(card: Card, role: AgentRole) -> RouterCardSummary:
    return RouterCardSummary(
        card_id=card.id,
        title=card.title,
        goal=card.goal,
        role=role,
        priority=card.priority.name,
        acceptance_criteria=tuple(card.acceptance_criteria),
        context_refs=tuple(_summarize_ref(r) for r in card.context_refs),
        current_agent_profile=card.agent_profile,
        rework_iteration=card.rework_iteration,
    )


def _summarize_ref(ref: ContextRef) -> dict[str, str]:
    return {"path": ref.path, "kind": ref.kind, "note": ref.note}


def build_candidates(
    role: AgentRole, profiles: dict[str, ProfileSpec]
) -> tuple[RouterCandidateProfile, ...]:
    """Filter profiles to those belonging to ``role``. Order is stable
    (insertion order from the config) so router inputs and tests are
    deterministic."""
    out: list[RouterCandidateProfile] = []
    for spec in profiles.values():
        if spec.role != role:
            continue
        out.append(
            RouterCandidateProfile(
                name=spec.name,
                role=spec.role,
                backend_type=spec.backend.type,
                backend_target=spec.backend.target,
                fallback=spec.fallback,
                capabilities=spec.capabilities,
                description=spec.description,
            )
        )
    return tuple(out)


def render_request(request: RouterRequest) -> str:
    """Serialize the router input into the exact JSON shape documented in
    the kanban-router agent spec. Kept deterministic (no timestamps, no
    random ordering) so cached decisions match byte-for-byte."""
    payload = {
        "card": {
            "card_id": request.card.card_id,
            "title": request.card.title,
            "goal": request.card.goal,
            "role": request.card.role.value,
            "priority": request.card.priority,
            "acceptance_criteria": list(request.card.acceptance_criteria),
            "context_refs": [dict(r) for r in request.card.context_refs],
            "current_agent_profile": request.card.current_agent_profile,
            "rework_iteration": request.card.rework_iteration,
        },
        "candidates": [
            {
                "name": c.name,
                "role": c.role.value,
                "backend_type": c.backend_type,
                "backend_target": c.backend_target,
                "fallback": c.fallback,
                "capabilities": list(c.capabilities),
                "description": c.description,
            }
            for c in request.candidates
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


# ---------- Client --------------------------------------------------------


AgentFactory = Callable[[AgentSpec, Path | None], Any]


@dataclass
class RouterClient:
    """Thin wrapper around an agentao subagent that normalizes router I/O.

    Kept separate from ``SubagentBackend`` on purpose: the router is not a
    profile backend — it does not consume a ``BackendRequest``, does not
    return a ``BackendResponse``, and does not participate in the
    profile-fallback chain.
    """

    spec: AgentSpec
    agent_factory: AgentFactory | None = None
    timeout_s: float = 10.0
    working_directory: Path | None = None

    def __post_init__(self) -> None:
        if self.agent_factory is None:
            from .agentao_multi import _default_agent_factory

            self.agent_factory = _default_agent_factory

    @property
    def prompt_version(self) -> str:
        return self.spec.version

    def route(self, request: RouterRequest) -> RouterDecision:
        """Invoke the router agent and normalize its reply into a
        ``RouterDecision``. Never raises; every exception is folded into
        a ``RouterFailureKind`` so the host can fall through safely.
        """
        prompt = render_request(request)
        try:
            raw = self._invoke_with_timeout(prompt)
        except TimeoutError:
            return RouterDecision(
                profile=None,
                reason="router call timed out",
                failure=RouterFailureKind.TIMEOUT,
                prompt_version=self.spec.version,
            )
        except Exception as exc:  # noqa: BLE001 — boundary
            return RouterDecision(
                profile=None,
                reason=f"router backend raised: {exc!s}",
                failure=RouterFailureKind.BACKEND_ERROR,
                prompt_version=self.spec.version,
            )
        return self._parse(raw, request)

    def _invoke_with_timeout(self, prompt: str) -> str:
        agent = self.agent_factory(self.spec, self.working_directory)  # type: ignore[misc]
        result: dict[str, Any] = {}

        def _call() -> None:
            try:
                result["raw"] = agent.chat(prompt, max_iterations=self.spec.max_turns)
            except Exception as exc:  # noqa: BLE001
                result["error"] = exc

        thread = threading.Thread(target=_call, daemon=True)
        thread.start()
        thread.join(timeout=self.timeout_s)
        if thread.is_alive():
            # We can't hard-kill the thread, but we can abandon it; daemon
            # threads die with the process. The failure is still observable
            # from the host's perspective.
            raise TimeoutError(f"router agent did not reply within {self.timeout_s}s")
        if "error" in result:
            raise result["error"]  # re-raised into the outer except
        raw = result.get("raw")
        if not isinstance(raw, str):
            raise RuntimeError("router agent returned non-string reply")
        return raw

    def _parse(self, raw: str, request: RouterRequest) -> RouterDecision:
        payload = _extract_json_object(raw)
        if payload is None:
            return RouterDecision(
                profile=None,
                reason="router output is not parseable JSON",
                failure=RouterFailureKind.PARSE_ERROR,
                prompt_version=self.spec.version,
            )

        profile = payload.get("profile")
        reason = str(payload.get("reason") or "").strip()
        confidence = payload.get("confidence")
        confidence_val: float | None
        if isinstance(confidence, (int, float)):
            confidence_val = float(confidence)
        else:
            confidence_val = None

        if profile is None:
            return RouterDecision(
                profile=None,
                reason=reason or "router declined to choose",
                confidence=confidence_val,
                failure=RouterFailureKind.EMPTY_CHOICE,
                prompt_version=self.spec.version,
            )

        if not isinstance(profile, str) or not profile:
            return RouterDecision(
                profile=None,
                reason="router returned non-string profile value",
                confidence=confidence_val,
                failure=RouterFailureKind.PARSE_ERROR,
                prompt_version=self.spec.version,
            )

        allowed = {c.name for c in request.candidates}
        if profile not in allowed:
            return RouterDecision(
                profile=None,
                reason=f"router returned {profile!r} which is not in the candidate list",
                confidence=confidence_val,
                failure=RouterFailureKind.INVALID_CHOICE,
                prompt_version=self.spec.version,
            )

        return RouterDecision(
            profile=profile,
            reason=reason or f"router selected {profile}",
            confidence=confidence_val,
            prompt_version=self.spec.version,
        )


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Find the first top-level JSON object in ``text`` and parse it.

    Accepts either a bare JSON object or one fenced by ```json``` / ``` ```.
    Tolerant of surrounding prose even though the router spec tells the
    agent not to emit any — the host must still not choke on stray
    whitespace.
    """
    stripped = text.strip()
    if not stripped:
        return None

    # Try the whole payload first — the happy path.
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        return parsed

    # Fall back to extracting the first `{...}` span. This handles the
    # case where the model wrapped its reply in a fence or prose.
    # Track string state so braces inside JSON strings (e.g. "prefer
    # {shell} tasks") don't corrupt the depth counter.
    start = stripped.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(stripped)):
            ch = stripped[i]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = stripped[start : i + 1]
                    try:
                        parsed = json.loads(candidate)
                    except json.JSONDecodeError:
                        break
                    if isinstance(parsed, dict):
                        return parsed
                    break
        start = stripped.find("{", start + 1)
    return None
