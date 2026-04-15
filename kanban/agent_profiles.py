"""Agent-profile routing config.

Loads and validates `agent_profiles.yaml`, which maps kanban roles onto concrete
execution profiles (subagent or ACP backend). Phase 2 of the agent-profile rollout
— see `docs/implementation/agent-profile-acp-implementation.md`.

The config is the single source of truth for role default profiles and
profile -> backend bindings. It does not hold backend credentials or server
commands; ACP server definitions live in `.agentao/acp.json` and are resolved
later by the ACP backend via `agentao.acp_client`.

Lookup precedence (see ``resolve_config_path``):

1. ``<base>/.kanban/agent_profiles.yaml``           — operator override
2. ``<install>/kanban/defaults/agent_profiles.yaml`` — packaged default

``base`` defaults to ``Path.cwd()`` but callers that know the selected
project root (for example the CLI, which derives it from ``--board``)
should pass it explicitly so multi-board workflows read the right
config. The packaged default ships inside the ``kanban`` wheel so
installed users get a working CLI out of the box.
``docs/agent_profiles.sample.yaml`` is a human-readable sample kept in
sync with the packaged default; it is not on the load path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .models import AgentRole

BACKEND_TYPES = ("subagent", "acp")


class ProfileConfigError(ValueError):
    """Raised when an agent-profile config file is structurally invalid."""


@dataclass(slots=True, frozen=True)
class BackendSpec:
    type: str
    target: str


@dataclass(slots=True, frozen=True)
class ProfileSpec:
    name: str
    role: AgentRole
    backend: BackendSpec
    fallback: str | None = None
    capabilities: tuple[str, ...] = ()
    description: str = ""


@dataclass(slots=True, frozen=True)
class RoleConfig:
    default_profile: str


DEFAULT_ROUTER_TIMEOUT_S = 10.0


@dataclass(slots=True, frozen=True)
class RouterConfig:
    """Router-policy controls (see docs/agent-router-design.md).

    ``enabled_roles`` is the per-role allowlist. When the config file has no
    ``router:`` section, Phase-1 rollout defaults to worker-only.
    ``timeout_s`` overrides the client-side call timeout.
    """

    enabled_roles: frozenset[AgentRole] = frozenset({AgentRole.WORKER})
    timeout_s: float = DEFAULT_ROUTER_TIMEOUT_S

    def is_enabled_for(self, role: AgentRole) -> bool:
        return role in self.enabled_roles


@dataclass(slots=True)
class AgentProfileConfig:
    roles: dict[AgentRole, RoleConfig] = field(default_factory=dict)
    profiles: dict[str, ProfileSpec] = field(default_factory=dict)
    router: RouterConfig = field(default_factory=RouterConfig)

    def get_profile(self, name: str) -> ProfileSpec:
        try:
            return self.profiles[name]
        except KeyError as e:
            raise ProfileConfigError(f"unknown profile: {name!r}") from e

    def default_profile_for(self, role: AgentRole) -> ProfileSpec:
        cfg = self.roles.get(role)
        if cfg is None:
            raise ProfileConfigError(f"no default profile configured for role {role.value!r}")
        return self.get_profile(cfg.default_profile)

    def fallback_chain(self, name: str) -> list[str]:
        """Return [name, fallback, fallback-of-fallback, ...]. Cycle-free (validated at load)."""
        chain: list[str] = []
        current: str | None = name
        while current is not None:
            chain.append(current)
            current = self.profiles[current].fallback
        return chain


def load_config(path: str | Path) -> AgentProfileConfig:
    p = Path(path)
    try:
        raw_text = p.read_text(encoding="utf-8")
    except OSError as e:
        raise ProfileConfigError(f"cannot read profile config {p}: {e}") from e
    try:
        data = yaml.safe_load(raw_text) or {}
    except yaml.YAMLError as e:
        raise ProfileConfigError(f"invalid YAML in {p}: {e}") from e
    if not isinstance(data, dict):
        raise ProfileConfigError(f"{p}: top-level must be a mapping")
    return _build_config(data, source=str(p))


USER_CONFIG_RELPATH = Path(".kanban") / "agent_profiles.yaml"


def packaged_default_config_path() -> Path:
    """Absolute path to the default config shipped inside the package."""
    return Path(__file__).resolve().parent / "defaults" / "agent_profiles.yaml"


def resolve_config_path(base: Path | None = None) -> Path:
    """Resolve the active config path using the documented precedence.

    Returns the first existing path among:

    1. ``<base>/.kanban/agent_profiles.yaml``
    2. ``<install>/kanban/defaults/agent_profiles.yaml``

    ``base`` defaults to ``Path.cwd()``. Callers that know the project
    root (for example the CLI, which derives it from ``--board``) should
    pass it explicitly so ``kanban --board /other/project/...`` reads
    the right config instead of the shell's cwd.

    Raises ``ProfileConfigError`` if neither exists — the packaged default
    is part of the wheel, so this should only happen in a corrupted install.
    """
    search_base = base if base is not None else Path.cwd()
    user = search_base / USER_CONFIG_RELPATH
    if user.is_file():
        return user
    packaged = packaged_default_config_path()
    if packaged.is_file():
        return packaged
    raise ProfileConfigError(
        f"no agent_profiles config found: looked at {user} and {packaged}. "
        f"Reinstall kanban or create {USER_CONFIG_RELPATH} manually."
    )


def load_default_config(base: Path | None = None) -> AgentProfileConfig:
    """Load the active profile config (user override or packaged default).

    ``base`` is forwarded to :func:`resolve_config_path`; see its docstring
    for the precedence rules and why callers should pass it when they
    know the project root.
    """
    return load_config(resolve_config_path(base))


def _build_config(data: dict[str, Any], *, source: str) -> AgentProfileConfig:
    profiles_raw = data.get("profiles") or {}
    roles_raw = data.get("roles") or {}
    if not isinstance(profiles_raw, dict):
        raise ProfileConfigError(f"{source}: 'profiles' must be a mapping")
    if not isinstance(roles_raw, dict):
        raise ProfileConfigError(f"{source}: 'roles' must be a mapping")

    profiles: dict[str, ProfileSpec] = {}
    for name, spec in profiles_raw.items():
        profiles[str(name)] = _parse_profile(str(name), spec, source=source)

    roles: dict[AgentRole, RoleConfig] = {}
    for role_name, spec in roles_raw.items():
        try:
            role = AgentRole(str(role_name))
        except ValueError as e:
            raise ProfileConfigError(f"{source}: unknown role {role_name!r}") from e
        if not isinstance(spec, dict):
            raise ProfileConfigError(f"{source}: roles.{role_name} must be a mapping")
        default = spec.get("default_profile")
        if not isinstance(default, str) or not default:
            raise ProfileConfigError(
                f"{source}: roles.{role_name}.default_profile must be a non-empty string"
            )
        roles[role] = RoleConfig(default_profile=default)

    router = _parse_router(data.get("router"), source=source)

    cfg = AgentProfileConfig(roles=roles, profiles=profiles, router=router)
    _validate(cfg, source=source)
    return cfg


def _parse_router(raw: Any, *, source: str) -> RouterConfig:
    """Parse the optional top-level ``router:`` section.

    Missing section → Phase-1 rollout default (worker-only, 10s timeout).
    An empty mapping is treated the same as "missing" so operators can opt
    in later without rewriting everything.
    """
    if raw is None:
        return RouterConfig()
    if not isinstance(raw, dict):
        raise ProfileConfigError(f"{source}: 'router' must be a mapping")

    enabled_raw = raw.get("enabled_roles")
    if enabled_raw is None:
        enabled: frozenset[AgentRole] = frozenset({AgentRole.WORKER})
    else:
        if not isinstance(enabled_raw, list) or not all(isinstance(r, str) for r in enabled_raw):
            raise ProfileConfigError(
                f"{source}: router.enabled_roles must be a list of role name strings"
            )
        roles_set: set[AgentRole] = set()
        for r in enabled_raw:
            try:
                roles_set.add(AgentRole(r))
            except ValueError as e:
                raise ProfileConfigError(
                    f"{source}: router.enabled_roles contains unknown role {r!r}"
                ) from e
        enabled = frozenset(roles_set)

    timeout_raw = raw.get("timeout_s", DEFAULT_ROUTER_TIMEOUT_S)
    if not isinstance(timeout_raw, (int, float)) or timeout_raw <= 0:
        raise ProfileConfigError(
            f"{source}: router.timeout_s must be a positive number"
        )

    return RouterConfig(enabled_roles=enabled, timeout_s=float(timeout_raw))


def _parse_profile(name: str, spec: Any, *, source: str) -> ProfileSpec:
    if not isinstance(spec, dict):
        raise ProfileConfigError(f"{source}: profiles.{name} must be a mapping")

    role_raw = spec.get("role")
    if not isinstance(role_raw, str):
        raise ProfileConfigError(f"{source}: profiles.{name}.role must be a string")
    try:
        role = AgentRole(role_raw)
    except ValueError as e:
        raise ProfileConfigError(
            f"{source}: profiles.{name}.role {role_raw!r} is not a valid AgentRole"
        ) from e

    backend_raw = spec.get("backend")
    if not isinstance(backend_raw, dict):
        raise ProfileConfigError(f"{source}: profiles.{name}.backend must be a mapping")
    btype = backend_raw.get("type")
    btarget = backend_raw.get("target")
    if btype not in BACKEND_TYPES:
        raise ProfileConfigError(
            f"{source}: profiles.{name}.backend.type must be one of {BACKEND_TYPES}, got {btype!r}"
        )
    if not isinstance(btarget, str) or not btarget:
        raise ProfileConfigError(
            f"{source}: profiles.{name}.backend.target must be a non-empty string"
        )

    fallback = spec.get("fallback")
    if fallback is not None and (not isinstance(fallback, str) or not fallback):
        raise ProfileConfigError(
            f"{source}: profiles.{name}.fallback must be a non-empty string if set"
        )

    capabilities_raw = spec.get("capabilities") or []
    if not isinstance(capabilities_raw, list) or not all(
        isinstance(c, str) for c in capabilities_raw
    ):
        raise ProfileConfigError(
            f"{source}: profiles.{name}.capabilities must be a list of strings"
        )

    description = spec.get("description", "")
    if not isinstance(description, str):
        raise ProfileConfigError(f"{source}: profiles.{name}.description must be a string")

    return ProfileSpec(
        name=name,
        role=role,
        backend=BackendSpec(type=btype, target=btarget),
        fallback=fallback,
        capabilities=tuple(capabilities_raw),
        description=description,
    )


def _validate(cfg: AgentProfileConfig, *, source: str) -> None:
    # Every AgentRole must have a default profile. Missing roles make the
    # config appear to load cleanly but then block cards at execution time,
    # which is exactly what this validator exists to prevent.
    missing = [r.value for r in AgentRole if r not in cfg.roles]
    if missing:
        raise ProfileConfigError(
            f"{source}: roles section is missing defaults for "
            f"{', '.join(sorted(missing))}"
        )

    # Role defaults point at existing profiles with matching role.
    for role, rc in cfg.roles.items():
        profile = cfg.profiles.get(rc.default_profile)
        if profile is None:
            raise ProfileConfigError(
                f"{source}: roles.{role.value}.default_profile {rc.default_profile!r} "
                f"does not exist in profiles"
            )
        if profile.role != role:
            raise ProfileConfigError(
                f"{source}: roles.{role.value}.default_profile {rc.default_profile!r} "
                f"has role {profile.role.value!r}, expected {role.value!r}"
            )

    # Fallbacks must exist, share role, and be cycle-free.
    for name, profile in cfg.profiles.items():
        if profile.fallback is None:
            continue
        target = cfg.profiles.get(profile.fallback)
        if target is None:
            raise ProfileConfigError(
                f"{source}: profiles.{name}.fallback {profile.fallback!r} does not exist"
            )
        if target.role != profile.role:
            raise ProfileConfigError(
                f"{source}: profiles.{name}.fallback {profile.fallback!r} has role "
                f"{target.role.value!r}, expected {profile.role.value!r}"
            )
        _check_no_cycle(cfg, name, source=source)


def _check_no_cycle(cfg: AgentProfileConfig, start: str, *, source: str) -> None:
    seen: set[str] = set()
    current: str | None = start
    while current is not None:
        if current in seen:
            raise ProfileConfigError(
                f"{source}: fallback cycle detected starting at profile {start!r}"
            )
        seen.add(current)
        current = cfg.profiles[current].fallback
