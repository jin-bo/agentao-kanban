"""Load agentao sub-agent definitions for kanban roles.

Each role maps to one agent definition file (Markdown + YAML frontmatter).
At runtime we prefer project-local overrides in ``<cwd>/.agentao/agents/``.
If those files are absent, we fall back to the packaged defaults under
``kanban/defaults/`` so the repository remains self-contained.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import AgentRole


ROLE_AGENTS: dict[AgentRole, str] = {
    AgentRole.PLANNER: "kanban-planner",
    AgentRole.WORKER: "kanban-worker",
    AgentRole.REVIEWER: "kanban-reviewer",
    AgentRole.VERIFIER: "kanban-verifier",
}


@dataclass(frozen=True)
class AgentSpec:
    """Parsed agent definition — the runtime shape of one `.agentao/agents/*.md`."""

    name: str
    description: str
    version: str
    system_instructions: str
    max_turns: int
    model: str | None
    temperature: float | None
    source_path: Path


def default_agents_dir() -> Path:
    cwd_agents = Path.cwd() / ".agentao" / "agents"
    if cwd_agents.is_dir():
        return cwd_agents
    return Path(__file__).resolve().parent / "defaults"


def _spec_search_path(agents_dir: Path | None) -> list[Path]:
    """Ordered list of directories to search for one agent spec file.

    Per-file (not per-directory) fallback: when an operator points the
    executor at ``<project>/.agentao/agents/`` but only overrides one or
    two specs, the others should still resolve against the packaged
    defaults instead of raising. The chain is:

    1. ``agents_dir`` when explicitly passed (or ``<cwd>/.agentao/agents``
       otherwise, mirroring the historical ``default_agents_dir()`` rule)
    2. ``<install>/kanban/defaults`` — packaged defaults shipped in both
       the wheel and the sdist, so this tier always resolves.
    """
    paths: list[Path] = []
    if agents_dir is not None:
        paths.append(agents_dir)
    else:
        cwd = Path.cwd() / ".agentao" / "agents"
        if cwd.is_dir():
            paths.append(cwd)
    here = Path(__file__).resolve()
    paths.append(here.parent / "defaults")
    return paths


def load_spec(role: AgentRole, agents_dir: Path | None = None) -> AgentSpec:
    """Load and parse the agent definition file for ``role``."""
    return load_spec_by_name(ROLE_AGENTS[role], agents_dir)


def load_spec_by_name(name: str, agents_dir: Path | None = None) -> AgentSpec:
    """Load an agent definition by its file-stem name.

    Searches :func:`_spec_search_path` in order and returns the first
    hit. Raises ``FileNotFoundError`` only when none of the directories
    contain the requested file — so overriding one spec in a local
    ``.agentao/agents/`` does not disable the others.
    """
    tried: list[Path] = []
    for directory in _spec_search_path(agents_dir):
        path = directory / f"{name}.md"
        tried.append(path)
        if path.is_file():
            return parse_spec_file(path)
    raise FileNotFoundError(
        f"Agent definition not found for profile target {name!r}: "
        f"looked at {', '.join(str(p) for p in tried)}"
    )


def parse_spec_file(path: Path) -> AgentSpec:
    text = path.read_text(encoding="utf-8")
    frontmatter, body = _split_frontmatter(text, path)
    name = str(frontmatter.get("name") or path.stem)
    description = str(frontmatter.get("description") or "")
    version = str(frontmatter.get("version") or "unversioned")
    max_turns = int(frontmatter.get("max_turns", 15))
    model = frontmatter.get("model")
    model_str: str | None = str(model) if model else None
    raw_temp = frontmatter.get("temperature")
    temperature: float | None = float(raw_temp) if raw_temp is not None else None
    return AgentSpec(
        name=name,
        description=description,
        version=version,
        system_instructions=body.strip(),
        max_turns=max_turns,
        model=model_str,
        temperature=temperature,
        source_path=path,
    )


def _split_frontmatter(text: str, path: Path) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        raise ValueError(f"Missing YAML frontmatter in {path}")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise ValueError(f"Unclosed YAML frontmatter in {path}")
    import yaml  # agentao depends on PyYAML, so it's on the path

    frontmatter = yaml.safe_load(parts[1]) or {}
    if not isinstance(frontmatter, dict):
        raise ValueError(f"Frontmatter is not a mapping in {path}")
    return frontmatter, parts[2]
