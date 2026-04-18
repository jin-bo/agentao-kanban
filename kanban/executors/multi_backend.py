"""Profile-aware executor that delegates to a pluggable backend.

Responsibilities that stay here (not in the backend):
- prompt construction
- raw-response parsing (```json``` fence)
- role-specific validation (planner supersession)
- building the `AgentResult` applied to the card

Backends only produce raw agent text plus diagnostic metadata. This split
means adding a new backend (ACP in Phase 4, future ones later) does not
duplicate parsing or workflow-transition code.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from ..agent_profiles import AgentProfileConfig, ProfileConfigError, ProfileSpec, load_default_config
from ..agents import AgentSpec
from ..models import AgentResult, AgentRole, Card, CardStatus
from . import agentao_multi as _legacy
from .acp_failure import AcpFailureKind, classify as _classify_acp
from .backends.base import Backend, BackendRequest, BackendResponse
from .profile_resolver import PolicyFn, ResolvedProfile, resolve_profile
from .backends.subagent_backend import SubagentBackend


BackendRegistry = dict[str, Backend]


@dataclass
class MultiBackendExecutor:
    """Main profile-aware `CardExecutor`.

    Backends are registered by `backend_type` ("subagent", "acp", ...). If a
    registry is not supplied, a default `SubagentBackend` is installed; ACP
    wiring is added in Phase 4.
    """

    config: AgentProfileConfig = field(default_factory=load_default_config)
    working_directory: Path | None = None
    agents_dir: Path | None = None
    backends: BackendRegistry = field(default_factory=dict)
    policy: PolicyFn | None = None
    planner_recommendation_fn: Callable[[Card], str | None] | None = None

    def __post_init__(self) -> None:
        if "subagent" not in self.backends:
            self.backends["subagent"] = SubagentBackend(agents_dir=self.agents_dir)

    def run(self, role: AgentRole, card: Card) -> AgentResult:
        # Resolution failures are config-level: not retryable.
        try:
            resolved = self._resolve(role, card)
        except ProfileConfigError as exc:
            return _legacy._blocked_result(role, f"profile resolution failed: {exc}", None)

        router_prompt_version = self._router_prompt_version(card, role, resolved)
        resolved = self._enrich_with_router_reason(resolved, card, role)

        prompt = _legacy._build_prompt(role, card)

        t0 = time.monotonic()
        try:
            response, used_profile, fallback_from = self._invoke_with_fallback(
                role, card, resolved, prompt
            )
        except _ProfileUnsupported as exc:
            return _tag_routing(
                _legacy._blocked_result(role, str(exc), None),
                resolved=resolved,
                used_profile=resolved.profile,
                fallback_from=None,
                response=None,
                router_prompt_version=router_prompt_version,
            )
        except _AcpTerminal as exc:
            return _tag_routing(
                _legacy._blocked_result(role, exc.reason, None),
                resolved=resolved,
                used_profile=resolved.profile,
                fallback_from=None,
                response=None,
                router_prompt_version=router_prompt_version,
            )

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        parsed = _legacy._parse_response(response.raw_text)

        synth_spec = _synth_spec(response, used_profile.name)

        def _tag(result: AgentResult) -> AgentResult:
            return _tag_routing(
                result,
                resolved=resolved,
                used_profile=used_profile,
                fallback_from=fallback_from,
                response=response,
                router_prompt_version=router_prompt_version,
            )

        if parsed.get("ok") is False:
            reason = str(parsed.get("blocked_reason") or "Agent reported failure")
            revision = _legacy._extract_revision_request(role, parsed)
            if revision is not None:
                return _tag(_legacy._rework_result(
                    role, revision, synth_spec, elapsed_ms, response.raw_text,
                ))
            return _tag(_legacy._blocked_result(role, reason, synth_spec, elapsed_ms, response.raw_text))

        if role == AgentRole.PLANNER:
            if not parsed.get("_structured"):
                raise RuntimeError(
                    "planner returned unstructured response "
                    "(no ```json``` fence); retrying via infrastructure path"
                )
            criteria = parsed.get("acceptance_criteria")
            normalized: list[str] = []
            if isinstance(criteria, list):
                normalized = [str(c).strip() for c in criteria if str(c).strip()]
            if not normalized and not card.acceptance_criteria:
                return _tag(_legacy._blocked_result(
                    role,
                    "planner must return 2-5 non-empty acceptance_criteria",
                    synth_spec,
                    elapsed_ms,
                    response.raw_text,
                ))
            if normalized and card.acceptance_criteria:
                drop_error = _legacy._validate_supersession(
                    existing=list(card.acceptance_criteria),
                    new=normalized,
                    output=parsed.get("output"),
                )
                if drop_error is not None:
                    return _tag(_legacy._blocked_result(
                        role, drop_error, synth_spec, elapsed_ms, response.raw_text
                    ))

        return _tag(_legacy._apply_parsed(
            role, card, parsed, synth_spec, elapsed_ms, response.raw_text
        ))

    def _resolve(self, role: AgentRole, card: Card) -> ResolvedProfile:
        rec = self.planner_recommendation_fn(card) if self.planner_recommendation_fn else None
        return resolve_profile(
            role,
            card,
            self.config,
            planner_recommendation=rec,
            policy=self.policy,
        )

    def _router_outcome(self, card: Card, role: AgentRole):
        """Fetch the last router-policy outcome for (card, role) if the
        attached policy exposes ``last_outcome``. Keeps the executor
        decoupled from ``RouterPolicy`` concrete type — any policy that
        provides the same accessor plugs in cleanly."""
        policy = self.policy
        if policy is None:
            return None
        last = getattr(policy, "last_outcome", None)
        if last is None:
            return None
        return last(card.id, role)

    def _router_prompt_version(
        self, card: Card, role: AgentRole, resolved: ResolvedProfile
    ) -> str | None:
        # The policy's ``last_outcome`` persists across runs of the same
        # (card, role). If *this* run short-circuited on a card pin or
        # planner recommendation, the stored outcome is from a previous
        # run and must not be attributed to the current execution event.
        # ``resolve_profile`` only calls the policy when source is
        # "policy" or "default", so any other source means the policy
        # was not consulted this turn.
        if resolved.source not in ("policy", "default"):
            return None
        outcome = self._router_outcome(card, role)
        if outcome is None or not outcome.router_invoked:
            return None
        return outcome.prompt_version

    def _enrich_with_router_reason(
        self, resolved: ResolvedProfile, card: Card, role: AgentRole
    ) -> ResolvedProfile:
        """When the router was consulted this turn, replace the resolver's
        terse reason with the policy's richer explanation. Keeps ``source``
        untouched so downstream logic still distinguishes policy-hit from
        default-fallthrough. Gated on ``resolved.source`` for the same
        reason ``_router_prompt_version`` is — the policy's last_outcome
        may be stale from a prior run of this card/role.
        """
        if resolved.source not in ("policy", "default"):
            return resolved
        outcome = self._router_outcome(card, role)
        if outcome is None or not outcome.router_invoked:
            return resolved
        if resolved.source == "policy":
            return replace(resolved, reason=outcome.reason)
        if resolved.source == "default":
            return replace(
                resolved,
                reason=f"{resolved.reason} ({outcome.reason})",
            )
        return resolved

    def _invoke_with_fallback(
        self,
        role: AgentRole,
        card: Card,
        resolved: ResolvedProfile,
        prompt: str,
    ) -> tuple[BackendResponse, ProfileSpec, str | None]:
        """Run primary profile; on ACP infrastructure failure fall back once.

        Returns (response, profile_actually_used, fallback_from_profile_name).
        On terminal ACP failures (CONFIG, INTERACTION_REQUIRED) raises
        ``_AcpTerminal`` so the caller blocks the card; on other exceptions
        re-raises ``RuntimeError`` so the retry matrix treats it as
        INFRASTRUCTURE.
        """
        response = self._try_profile(role, card, resolved.profile, prompt)
        if isinstance(response, _InfrastructureFailure):
            fallback_name = resolved.profile.fallback
            if fallback_name is None:
                raise response.as_runtime(role, resolved.profile)
            fallback_profile = self.config.get_profile(fallback_name)
            fb_response = self._try_profile(role, card, fallback_profile, prompt)
            if isinstance(fb_response, _InfrastructureFailure):
                raise fb_response.as_runtime(role, fallback_profile)
            return fb_response, fallback_profile, resolved.profile.name
        return response, resolved.profile, None

    def _try_profile(
        self,
        role: AgentRole,
        card: Card,
        profile: ProfileSpec,
        prompt: str,
    ) -> "BackendResponse | _InfrastructureFailure":
        backend = self.backends.get(profile.backend.type)
        if backend is None:
            raise _ProfileUnsupported(
                f"no backend registered for type {profile.backend.type!r} "
                f"(profile {profile.name!r})"
            )
        request = BackendRequest(
            role=role,
            card=card,
            prompt=prompt,
            profile=profile,
            working_directory=self.working_directory,
        )
        try:
            return backend.invoke(request)
        except Exception as exc:  # noqa: BLE001 — boundary; map by classification
            return _classify_exception(exc, profile=profile, role=role)


def backend_spec_name(agents_dir: Path | None, resolved: ResolvedProfile) -> str:
    """Return a stable spec name used for event tagging when the backend
    itself does not supply one (ACP targets, for example). Kept as a free
    function so ACP backend can share the lookup later.
    """
    return resolved.profile.name


class _ProfileUnsupported(RuntimeError):
    """Raised when a profile's backend type has no registered backend."""


class _AcpTerminal(RuntimeError):
    """Raised when an ACP call fails with a non-retryable classification."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(slots=True)
class _InfrastructureFailure:
    """Sentinel return from a single backend attempt: infrastructure-class error."""

    exc: Exception

    def as_runtime(self, role: AgentRole, profile: ProfileSpec) -> RuntimeError:
        return RuntimeError(
            f"backend {profile.backend.type} call failed "
            f"({role.value} via {profile.name}): {self.exc}"
        )


def _classify_exception(
    exc: Exception, *, profile: ProfileSpec, role: AgentRole
) -> _InfrastructureFailure:
    """Map a backend exception to a terminal raise or an infra sentinel.

    AcpClientError carries a structured ``code``; we use it rather than
    string matching. Non-ACP exceptions default to INFRASTRUCTURE so
    transient library/LLM errors still flow through the retry matrix.
    """
    try:
        from agentao.acp_client import AcpClientError
    except ImportError:
        AcpClientError = ()  # type: ignore[assignment]

    if isinstance(exc, AcpClientError):  # type: ignore[arg-type]
        kind = _classify_acp(exc)
        if kind == AcpFailureKind.CONFIG:
            raise _AcpTerminal(
                f"profile {profile.name!r} backend config failure "
                f"(code={_code_str(exc)}): {exc}"
            ) from exc
        if kind == AcpFailureKind.INTERACTION_REQUIRED:
            raise _AcpTerminal(
                f"profile {profile.name!r} backend requires user input "
                f"(code={_code_str(exc)}): {exc}"
            ) from exc
    from .backends.subagent_backend import SubagentSpecMissing

    if isinstance(exc, SubagentSpecMissing):
        # Missing subagent spec is a config problem — retrying won't conjure the file.
        # Note: we match the dedicated subclass, not bare FileNotFoundError, so
        # file errors raised *inside* agent.chat() still flow through retry.
        raise _AcpTerminal(
            f"profile {profile.name!r} agent definition missing: {exc}"
        ) from exc
    return _InfrastructureFailure(exc=exc)


def _code_str(exc: Exception) -> str:
    acp_code = getattr(exc, "acp_code", None)
    if acp_code is not None:
        return getattr(acp_code, "value", str(acp_code))
    code = getattr(exc, "code", None)
    return getattr(code, "value", str(code) if code is not None else "")


def _tag_routing(
    result: AgentResult,
    *,
    resolved: ResolvedProfile,
    used_profile: ProfileSpec,
    fallback_from: str | None,
    response: BackendResponse | None,
    router_prompt_version: str | None = None,
) -> AgentResult:
    """Attach routing/backend diagnostics to an AgentResult in-place.

    Runs on every return path so `append_execution_event` can distinguish
    role / profile / backend without re-running resolution against the card
    (whose `agent_profile` may change between runs).
    """
    result.agent_profile = used_profile.name
    result.backend_type = used_profile.backend.type
    result.backend_target = used_profile.backend.target
    result.routing_source = resolved.source
    result.routing_reason = resolved.reason
    result.fallback_from_profile = fallback_from
    if router_prompt_version:
        result.router_prompt_version = router_prompt_version
    if response is not None:
        meta = response.metadata or {}
        session = meta.get("session_id")
        if isinstance(session, str) and session:
            result.session_id = session
        # Preserve backend diagnostics (stop_reason, effective_cwd, ...) so
        # ACP failures can be triaged from the event log alone.
        result.backend_metadata = dict(meta)
    return result


def _synth_spec(response, spec_name: str) -> AgentSpec:
    """Build a minimal AgentSpec from a BackendResponse so `_apply_parsed`
    and `_blocked_result` can keep using their existing AgentSpec fields
    (name + version) without branching for each backend type.
    """
    name = response.spec_name or spec_name
    return AgentSpec(
        name=name,
        description="",
        version=response.prompt_version,
        system_instructions="",
        max_turns=0,
        model=None,
        temperature=None,
        source_path=Path("<backend>"),
    )
