"""Backend adapter interface for profile-aware execution.

A backend is the concrete execution implementation behind a resolved profile.
It takes a prompt and returns raw agent text plus diagnostic metadata; it does
NOT parse the JSON fence, apply workflow transitions, or touch the board —
those concerns stay in `multi_backend.MultiBackendExecutor` so parsing and
validation remain centralized across backend types (subagent, acp, ...).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ...agent_profiles import ProfileSpec
from ...models import AgentRole, Card


@dataclass(slots=True)
class BackendRequest:
    role: AgentRole
    card: Card
    prompt: str
    profile: ProfileSpec
    working_directory: Path | None = None


@dataclass(slots=True)
class BackendResponse:
    raw_text: str
    prompt_version: str = ""
    spec_name: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class Backend(Protocol):
    """Protocol for a profile backend adapter.

    Implementations must be synchronous and either return a `BackendResponse`
    or raise. Infrastructure failures should raise so the top-level executor
    can map them onto the retry matrix — do NOT smuggle a failure back as
    `raw_text`. Agent-level 'refusal' (the model said no) belongs in the raw
    response and is decided by the parser, not by the backend.
    """

    backend_type: str

    def invoke(self, request: BackendRequest) -> BackendResponse: ...
