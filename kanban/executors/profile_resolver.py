"""Role + card -> ProfileSpec resolution.

Precedence (see design doc §Routing Rules):

1. `card.agent_profile`  (operator or planner-recorded explicit choice)
2. planner recommendation passed by the caller
3. policy match (placeholder — no rules-engine yet; hook reserved)
4. role default profile from `AgentProfileConfig`

After resolution, the profile is validated to match the requested role; a
role/profile mismatch is a routing error (not retryable) and raises
`ProfileConfigError`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..agent_profiles import AgentProfileConfig, ProfileConfigError, ProfileSpec
from ..models import AgentRole, Card


RoutingSource = str  # "card" | "planner" | "policy" | "default"


@dataclass(slots=True, frozen=True)
class ResolvedProfile:
    profile: ProfileSpec
    source: RoutingSource
    reason: str


PolicyFn = Callable[[AgentRole, Card, AgentProfileConfig], str | None]


def resolve_profile(
    role: AgentRole,
    card: Card,
    config: AgentProfileConfig,
    *,
    planner_recommendation: str | None = None,
    policy: PolicyFn | None = None,
) -> ResolvedProfile:
    """Resolve the profile the executor should use for *this* role.

    A card pin (`card.agent_profile`) or a planner recommendation names a
    single profile, which is always tied to one role (profiles cannot span
    roles). Since a card flows through planner → worker → reviewer →
    verifier, a role-specific pin like ``gemini-worker`` only applies
    when the executor runs that role; for any other role the pin is
    silently ignored and resolution falls through to policy / role default.
    Unknown profile names remain a hard configuration error.
    """
    card_match = _match_named(card.agent_profile, role, config)
    if card_match is not None:
        return ResolvedProfile(
            profile=card_match,
            source="card",
            reason=f"card.agent_profile={card.agent_profile!r} "
                   f"(source={card.agent_profile_source or 'unknown'})",
        )

    planner_match = _match_named(planner_recommendation, role, config)
    if planner_match is not None:
        return ResolvedProfile(
            profile=planner_match,
            source="planner",
            reason=f"planner recommended {planner_recommendation!r}",
        )

    if policy is not None:
        match_name = policy(role, card, config)
        policy_match = _match_named(match_name, role, config, strict=True)
        if policy_match is not None:
            return ResolvedProfile(
                profile=policy_match,
                source="policy",
                reason=f"policy matched {match_name!r}",
            )

    profile = config.default_profile_for(role)
    return ResolvedProfile(
        profile=profile,
        source="default",
        reason=f"role default for {role.value}",
    )


def _match_named(
    name: str | None,
    role: AgentRole,
    config: AgentProfileConfig,
    *,
    strict: bool = False,
) -> "ProfileSpec | None":
    """Resolve ``name`` to a profile if it applies to ``role``.

    Returns None when ``name`` is empty or when the profile is pinned to a
    different role (the pin simply doesn't apply to this stage). Unknown
    names always raise ``ProfileConfigError`` — a typo in the config is a
    hard failure regardless of which stage is running. When ``strict`` is
    True, a role mismatch also raises; use for callers (like a policy
    function) that must not silently discard a match.
    """
    if not name:
        return None
    profile = config.get_profile(name)
    if profile.role == role:
        return profile
    if strict:
        raise ProfileConfigError(
            f"profile {name!r} has role {profile.role.value!r} but was selected "
            f"for role {role.value!r}"
        )
    return None
