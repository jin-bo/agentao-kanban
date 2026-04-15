"""Router policy — bridges the router agent into ``resolve_profile``.

The policy is a ``PolicyFn`` (see ``profile_resolver.PolicyFn``). It is
invoked after card pins and planner recommendations, before the role
default. It returns a profile name or ``None``; the resolver handles the
fallthrough. Any surprise — router disabled, spec missing, non-matching
role, timeout, parse error, explicit null — becomes a ``None`` return so
the card keeps moving.

The policy instance caches decisions in-memory keyed by
``(card_id, role, sha1(goal + acceptance_criteria))``. A goal or
criteria edit naturally changes the key; no explicit invalidation is
needed. Cache is per-process; the daemon restarts clear it.
"""

from __future__ import annotations

import hashlib
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

from ..agent_profiles import AgentProfileConfig, RouterConfig
from ..models import AgentRole, Card
from .router_agent import (
    RouterClient,
    RouterDecision,
    RouterFailureKind,
    RouterRequest,
    build_candidates,
    build_card_summary,
    load_router_spec,
    render_request,
)


ENV_KILL_SWITCH = "KANBAN_ROUTER"


@dataclass(slots=True, frozen=True)
class PolicyOutcome:
    """Internal record of what the policy decided, including why the
    router was *not* called. Exposed mainly to help observability enrich
    ``routing_reason``; the ``PolicyFn`` contract itself only returns a
    name-or-None."""

    profile: str | None
    reason: str
    router_invoked: bool
    prompt_version: str | None = None
    cached: bool = False


def _kill_switch_engaged() -> bool:
    """`KANBAN_ROUTER=off` (case-insensitive) bypasses the router even if
    the config enables it. Any other value, including unset, leaves the
    config in charge."""
    val = os.environ.get(ENV_KILL_SWITCH, "").strip().lower()
    return val == "off"


# Failure kinds we treat as transient and therefore refuse to memoize.
# Everything else (a successful pick, an explicit null, an invalid_choice
# bug in the router's own output) is a stable property of the request and
# is safe to cache until the request changes.
_TRANSIENT_FAILURES = frozenset(
    {
        RouterFailureKind.TIMEOUT,
        RouterFailureKind.BACKEND_ERROR,
        RouterFailureKind.PARSE_ERROR,
    }
)


def _is_cacheable(decision: RouterDecision) -> bool:
    return decision.failure not in _TRANSIENT_FAILURES


@dataclass
class RouterPolicy:
    """Callable that implements the ``PolicyFn`` contract.

    Usage::

        policy = RouterPolicy(client_factory=...)
        executor = MultiBackendExecutor(config=cfg, policy=policy)

    The client is lazily instantiated on first use so constructing a
    ``MultiBackendExecutor`` never fails just because the router spec is
    missing — missing spec simply means the policy is permanently
    ``None``-returning for this process.
    """

    agents_dir: Path | None = None
    working_directory: Path | None = None
    client: RouterClient | None = None
    # Last outcome per card/role for lightweight observability hooks; the
    # executor reads it to enrich routing_reason with router context
    # (cached/skipped/failed) without re-running the policy.
    _last_outcome: dict[tuple[str, AgentRole], PolicyOutcome] = field(
        default_factory=dict, init=False, repr=False
    )
    _decision_cache: dict[tuple[str, AgentRole, str], RouterDecision] = field(
        default_factory=dict, init=False, repr=False
    )
    _client_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _client_loaded: bool = field(default=False, init=False, repr=False)

    def __call__(
        self, role: AgentRole, card: Card, config: AgentProfileConfig
    ) -> str | None:
        outcome = self._decide(role, card, config)
        self._last_outcome[(card.id, role)] = outcome
        return outcome.profile

    # The resolver only needs `PolicyFn`, but `MultiBackendExecutor` calls
    # this to enrich the event with router details on the chosen path.
    def last_outcome(self, card_id: str, role: AgentRole) -> PolicyOutcome | None:
        return self._last_outcome.get((card_id, role))

    # ------------------------------------------------------------------

    def _decide(
        self, role: AgentRole, card: Card, config: AgentProfileConfig
    ) -> PolicyOutcome:
        if _kill_switch_engaged():
            return PolicyOutcome(
                profile=None,
                reason=f"router disabled via {ENV_KILL_SWITCH}=off",
                router_invoked=False,
            )

        router_cfg: RouterConfig = config.router
        if not router_cfg.is_enabled_for(role):
            return PolicyOutcome(
                profile=None,
                reason=f"router not enabled for role {role.value}",
                router_invoked=False,
            )

        candidates = build_candidates(role, config.profiles)
        if len(candidates) <= 1:
            return PolicyOutcome(
                profile=None,
                reason=f"single candidate for role {role.value}, router skipped",
                router_invoked=False,
            )

        request = RouterRequest(
            card=build_card_summary(card, role),
            candidates=candidates,
        )
        # Hash the full rendered request (card + candidates, in the exact
        # shape the router will see) so any change to fields the router
        # reads — title, priority, context_refs, goal, acceptance, or the
        # candidate set — produces a new cache key. Goal/acceptance alone
        # would leak stale decisions after context or priority edits.
        key = self._cache_key(card.id, role, request)
        cached = self._decision_cache.get(key)
        if cached is not None:
            return self._outcome_from_decision(cached, cached_hit=True)

        client = self._get_client(router_cfg.timeout_s)
        if client is None:
            return PolicyOutcome(
                profile=None,
                reason="router spec missing; falling back to role default",
                router_invoked=False,
            )

        decision = client.route(request)
        # Only cache real decisions. Transient infrastructure failures
        # (timeout, backend_error, parse_error) would otherwise stick for
        # the lifetime of the process and keep falling through to the
        # role default even after the underlying problem is gone. We
        # still cache the router's explicit ``null`` choice because that
        # is a deliberate "nothing fits" verdict, not an outage.
        if _is_cacheable(decision):
            self._decision_cache[key] = decision
        return self._outcome_from_decision(decision, cached_hit=False)

    def _outcome_from_decision(
        self, decision: RouterDecision, *, cached_hit: bool
    ) -> PolicyOutcome:
        suffix = " (cached)" if cached_hit else ""
        if decision.profile is not None:
            return PolicyOutcome(
                profile=decision.profile,
                reason=f"router selected {decision.profile}: {decision.reason}{suffix}",
                router_invoked=True,
                prompt_version=decision.prompt_version,
                cached=cached_hit,
            )
        # Failure / empty-choice: return None and record the failure kind
        # in the reason string so observability can surface it.
        kind = decision.failure.value if decision.failure is not None else "no_match"
        return PolicyOutcome(
            profile=None,
            reason=f"router {kind}: {decision.reason}{suffix}",
            router_invoked=True,
            prompt_version=decision.prompt_version,
            cached=cached_hit,
        )

    def _get_client(self, timeout_s: float) -> RouterClient | None:
        # Lazy, lock-guarded so the first-ever call constructs exactly
        # one client even when multiple threads race.
        if self._client_loaded:
            return self.client
        with self._client_lock:
            if self._client_loaded:
                return self.client
            if self.client is None:
                spec = load_router_spec(self.agents_dir)
                if spec is not None:
                    self.client = RouterClient(
                        spec=spec,
                        timeout_s=timeout_s,
                        working_directory=self.working_directory,
                    )
            elif self.client.timeout_s != timeout_s:
                # Honor config changes between daemon restarts when the
                # caller supplied a pre-built client with a stale timeout.
                self.client.timeout_s = timeout_s
            self._client_loaded = True
        return self.client

    @staticmethod
    def _cache_key(
        card_id: str, role: AgentRole, request: RouterRequest
    ) -> tuple[str, AgentRole, str]:
        payload = render_request(request)
        digest = hashlib.sha1(payload.encode("utf-8"), usedforsecurity=False).hexdigest()
        return (card_id, role, digest)
