from __future__ import annotations

from pathlib import Path

import pytest

from kanban.agent_profiles import (
    AgentProfileConfig,
    ProfileConfigError,
    USER_CONFIG_RELPATH,
    load_config,
    load_default_config,
    resolve_config_path,
)
from kanban.models import AgentRole


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "profiles.yaml"
    p.write_text(body, encoding="utf-8")
    return p


# Boilerplate that supplies role defaults for every AgentRole. Tests that
# only care about a specific validation (fallback, backend.type, etc.)
# append this so the new "roles must cover every AgentRole" check doesn't
# shadow the more specific failure they're exercising.
_FULL_ROLES_YAML = """
roles:
  planner: {default_profile: _p}
  worker: {default_profile: _w}
  reviewer: {default_profile: _r}
  verifier: {default_profile: _v}
"""
_FULL_PROFILE_STUBS = """
  _p: {role: planner,  backend: {type: subagent, target: t}}
  _w: {role: worker,   backend: {type: subagent, target: t}}
  _r: {role: reviewer, backend: {type: subagent, target: t}}
  _v: {role: verifier, backend: {type: subagent, target: t}}
"""


def test_packaged_default_config_loads() -> None:
    cfg = load_default_config()
    assert isinstance(cfg, AgentProfileConfig)
    # Every role defined in AgentRole should have a default profile.
    for role in AgentRole:
        profile = cfg.default_profile_for(role)
        assert profile.role == role


def test_valid_config_round_trip(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, """
roles:
  planner:  {default_profile: _p}
  worker:   {default_profile: default-worker}
  reviewer: {default_profile: _r}
  verifier: {default_profile: _v}
profiles:
  _p: {role: planner,  backend: {type: subagent, target: t}}
  _r: {role: reviewer, backend: {type: subagent, target: t}}
  _v: {role: verifier, backend: {type: subagent, target: t}}
  default-worker:
    role: worker
    backend:
      type: subagent
      target: kanban-worker
  gemini-worker:
    role: worker
    backend:
      type: acp
      target: gemini-worker
    fallback: default-worker
    capabilities: [code, shell]
"""))
    worker = cfg.get_profile("gemini-worker")
    assert worker.role == AgentRole.WORKER
    assert worker.backend.type == "acp"
    assert worker.backend.target == "gemini-worker"
    assert worker.fallback == "default-worker"
    assert worker.capabilities == ("code", "shell")
    assert cfg.default_profile_for(AgentRole.WORKER).name == "default-worker"
    assert cfg.fallback_chain("gemini-worker") == ["gemini-worker", "default-worker"]


def test_invalid_backend_type_rejected(tmp_path: Path) -> None:
    with pytest.raises(ProfileConfigError, match="backend.type"):
        load_config(_write(tmp_path, """
profiles:
  bad:
    role: worker
    backend:
      type: grpc
      target: t
"""))


def test_unknown_role_rejected(tmp_path: Path) -> None:
    with pytest.raises(ProfileConfigError, match="role"):
        load_config(_write(tmp_path, """
profiles:
  bad:
    role: scribe
    backend:
      type: subagent
      target: t
"""))


def test_default_profile_must_exist(tmp_path: Path) -> None:
    with pytest.raises(ProfileConfigError, match="does not exist"):
        load_config(_write(tmp_path, """
roles:
  planner:  {default_profile: _p}
  worker:   {default_profile: ghost}
  reviewer: {default_profile: _r}
  verifier: {default_profile: _v}
profiles:
  _p: {role: planner,  backend: {type: subagent, target: t}}
  _r: {role: reviewer, backend: {type: subagent, target: t}}
  _v: {role: verifier, backend: {type: subagent, target: t}}
  default-worker:
    role: worker
    backend: {type: subagent, target: t}
"""))


def test_default_profile_role_must_match(tmp_path: Path) -> None:
    with pytest.raises(ProfileConfigError, match="expected 'worker'"):
        load_config(_write(tmp_path, """
roles:
  planner:  {default_profile: _p}
  worker:   {default_profile: the-reviewer}
  reviewer: {default_profile: _r}
  verifier: {default_profile: _v}
profiles:
  _p: {role: planner,  backend: {type: subagent, target: t}}
  _r: {role: reviewer, backend: {type: subagent, target: t}}
  _v: {role: verifier, backend: {type: subagent, target: t}}
  the-reviewer:
    role: reviewer
    backend: {type: subagent, target: t}
"""))


def test_fallback_must_share_role(tmp_path: Path) -> None:
    with pytest.raises(ProfileConfigError, match="fallback"):
        load_config(_write(tmp_path, """
profiles:
  w:
    role: worker
    backend: {type: subagent, target: t}
    fallback: r
  r:
    role: reviewer
    backend: {type: subagent, target: t}
"""))


def test_fallback_cycle_detected(tmp_path: Path) -> None:
    with pytest.raises(ProfileConfigError, match="cycle"):
        load_config(_write(tmp_path, """
profiles:
  a:
    role: worker
    backend: {type: subagent, target: t}
    fallback: b
  b:
    role: worker
    backend: {type: subagent, target: t}
    fallback: a
"""))


def test_fallback_missing_target(tmp_path: Path) -> None:
    with pytest.raises(ProfileConfigError, match="does not exist"):
        load_config(_write(tmp_path, f"""
{_FULL_ROLES_YAML}
profiles:
{_FULL_PROFILE_STUBS}
  a:
    role: worker
    backend: {{type: subagent, target: t}}
    fallback: ghost
"""))


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ProfileConfigError, match="cannot read"):
        load_config(tmp_path / "nope.yaml")


def test_invalid_yaml(tmp_path: Path) -> None:
    with pytest.raises(ProfileConfigError, match="invalid YAML"):
        load_config(_write(tmp_path, "profiles: [:\n"))


def test_get_profile_unknown_raises() -> None:
    cfg = load_default_config()
    with pytest.raises(ProfileConfigError, match="unknown profile"):
        cfg.get_profile("nope")


def test_missing_role_defaults_fail_at_load(tmp_path: Path) -> None:
    # Config has profiles but omits several role defaults — must fail fast
    # rather than deferring the error until a card tries to run that role.
    with pytest.raises(ProfileConfigError, match="missing defaults for"):
        load_config(_write(tmp_path, """
roles:
  worker:
    default_profile: default-worker
profiles:
  default-worker:
    role: worker
    backend: {type: subagent, target: t}
  default-planner:
    role: planner
    backend: {type: subagent, target: t}
  default-reviewer:
    role: reviewer
    backend: {type: subagent, target: t}
  default-verifier:
    role: verifier
    backend: {type: subagent, target: t}
"""))


def test_resolve_config_path_uses_explicit_base(tmp_path: Path) -> None:
    """Regression: resolve_config_path must honor the ``base`` arg so a
    caller that knows the project root (e.g. the CLI deriving it from
    --board) does not silently pick up the shell cwd's config."""
    (tmp_path / ".kanban").mkdir()
    user_cfg = tmp_path / USER_CONFIG_RELPATH
    user_cfg.write_text(
        _FULL_ROLES_YAML + "profiles:" + _FULL_PROFILE_STUBS, encoding="utf-8"
    )
    # With base=tmp_path, the per-project file wins over the packaged default.
    assert resolve_config_path(base=tmp_path) == user_cfg


def test_load_default_config_uses_base(tmp_path: Path) -> None:
    """When base points at a project with its own `.kanban/` config, that
    config's profile set must override the packaged default."""
    (tmp_path / ".kanban").mkdir()
    (tmp_path / USER_CONFIG_RELPATH).write_text(
        _FULL_ROLES_YAML + "profiles:" + _FULL_PROFILE_STUBS, encoding="utf-8"
    )
    cfg = load_default_config(base=tmp_path)
    # This tiny config only has _p/_w/_r/_v; the packaged default has
    # gemini-worker / gemini-reviewer as well. Presence of only the
    # stub profiles proves the override took effect.
    assert set(cfg.profiles) == {"_p", "_w", "_r", "_v"}

