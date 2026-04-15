from __future__ import annotations

import pytest

from kanban.agent_profiles import ProfileConfigError, load_default_config
from kanban.executors.profile_resolver import resolve_profile
from kanban.models import AgentRole, Card


def _card(**overrides) -> Card:
    return Card(title="t", goal="g", **overrides)


def test_card_profile_wins_over_planner_and_default() -> None:
    cfg = load_default_config()
    card = _card(agent_profile="gemini-worker", agent_profile_source="manual")
    resolved = resolve_profile(
        AgentRole.WORKER,
        card,
        cfg,
        planner_recommendation="default-worker",
        policy=lambda r, c, cf: "default-worker",
    )
    assert resolved.profile.name == "gemini-worker"
    assert resolved.source == "card"


def test_planner_beats_policy_and_default() -> None:
    cfg = load_default_config()
    resolved = resolve_profile(
        AgentRole.WORKER,
        _card(),
        cfg,
        planner_recommendation="gemini-worker",
        policy=lambda r, c, cf: "default-worker",
    )
    assert resolved.profile.name == "gemini-worker"
    assert resolved.source == "planner"


def test_policy_beats_default() -> None:
    cfg = load_default_config()
    resolved = resolve_profile(
        AgentRole.WORKER,
        _card(),
        cfg,
        policy=lambda r, c, cf: "gemini-worker",
    )
    assert resolved.source == "policy"
    assert resolved.profile.name == "gemini-worker"


def test_default_fallback() -> None:
    cfg = load_default_config()
    resolved = resolve_profile(AgentRole.REVIEWER, _card(), cfg)
    assert resolved.source == "default"
    assert resolved.profile.name == "default-reviewer"
    assert resolved.profile.role == AgentRole.REVIEWER


def test_card_pin_for_other_role_falls_through_to_default() -> None:
    # A card pinned to a worker profile should NOT block planning/review/verify;
    # the pin only applies when the executor actually runs the worker role.
    cfg = load_default_config()
    card = _card(agent_profile="gemini-worker", agent_profile_source="manual")
    resolved = resolve_profile(AgentRole.PLANNER, card, cfg)
    assert resolved.source == "default"
    assert resolved.profile.name == "default-planner"


def test_card_pin_for_matching_role_still_wins() -> None:
    cfg = load_default_config()
    card = _card(agent_profile="gemini-worker", agent_profile_source="manual")
    resolved = resolve_profile(AgentRole.WORKER, card, cfg)
    assert resolved.source == "card"
    assert resolved.profile.name == "gemini-worker"


def test_planner_recommendation_for_other_role_falls_through() -> None:
    cfg = load_default_config()
    resolved = resolve_profile(
        AgentRole.PLANNER,
        _card(),
        cfg,
        planner_recommendation="gemini-worker",
    )
    assert resolved.source == "default"
    assert resolved.profile.name == "default-planner"


def test_policy_role_mismatch_raises() -> None:
    cfg = load_default_config()
    with pytest.raises(ProfileConfigError, match="role 'reviewer'"):
        resolve_profile(
            AgentRole.WORKER,
            _card(),
            cfg,
            policy=lambda r, c, cf: "default-reviewer",
        )


def test_unknown_profile_raises() -> None:
    cfg = load_default_config()
    card = _card(agent_profile="ghost", agent_profile_source="manual")
    with pytest.raises(ProfileConfigError, match="unknown profile"):
        resolve_profile(AgentRole.WORKER, card, cfg)
