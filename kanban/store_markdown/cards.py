from __future__ import annotations

import logging
import tomllib
from dataclasses import fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..models import (
    AgentRole,
    Card,
    CardPriority,
    ContextRef,
    RevisionRequest,
    coerce_card_status,
)
from .toml_dump import _dump_toml

_LOG = logging.getLogger(__name__)

FRONT_MATTER_DELIM = "+++"

_CARD_FIELD_NAMES = {f.name for f in fields(Card)}


def _render_card(card: Card) -> str:
    fm = _dump_toml(_card_to_toml_dict(card))
    body = _render_body(card)
    return f"{FRONT_MATTER_DELIM}\n{fm}{FRONT_MATTER_DELIM}\n\n{body}"


def _card_to_toml_dict(card: Card) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": card.id,
        "title": card.title,
        "status": card.status.value,
        "priority": int(card.priority),
        "goal": card.goal,
        "acceptance_criteria": list(card.acceptance_criteria),
        "context_refs": [
            {"path": r.path, "kind": r.kind, "note": r.note} for r in card.context_refs
        ],
        "depends_on": list(card.depends_on),
        "history": list(card.history),
        "created_at": card.created_at,
        "updated_at": card.updated_at,
    }
    if card.blocked_at is not None:
        data["blocked_at"] = card.blocked_at
    if card.owner_role is not None:
        data["owner_role"] = card.owner_role.value
    if card.blocked_reason is not None:
        data["blocked_reason"] = card.blocked_reason
    if card.agent_profile is not None:
        data["agent_profile"] = card.agent_profile
    if card.agent_profile_source is not None:
        data["agent_profile_source"] = card.agent_profile_source
    if card.outputs:
        data["outputs"] = dict(card.outputs)
    if card.worktree_branch is not None:
        data["worktree_branch"] = card.worktree_branch
    if card.worktree_base_commit is not None:
        data["worktree_base_commit"] = card.worktree_base_commit
    if card.revision_requests:
        data["revision_requests"] = [
            {
                "at": r.at,
                "from_role": r.from_role.value,
                "iteration": int(r.iteration),
                "summary": r.summary,
                "hints": list(r.hints),
                "failing_criteria": list(r.failing_criteria),
            }
            for r in card.revision_requests
        ]
    if card.rework_iteration:
        data["rework_iteration"] = int(card.rework_iteration)
    return data


def _render_body(card: Card) -> str:
    lines: list[str] = [f"# {card.title}", "", "## Goal", "", card.goal, ""]
    if card.acceptance_criteria:
        lines += ["## Acceptance Criteria", ""]
        lines += [f"- {item}" for item in card.acceptance_criteria]
        lines.append("")
    if card.context_refs:
        lines += ["## Context", ""]
        for ref in card.context_refs:
            suffix = f" — {ref.note}" if ref.note else ""
            lines.append(f"- [{ref.kind}] `{ref.path}`{suffix}")
        lines.append("")
    if card.outputs:
        lines += ["## Outputs", ""]
        for key, value in card.outputs.items():
            lines += [f"### {key}", "", str(value), ""]
    if card.history:
        lines += ["## History", ""]
        lines += [f"- {item}" for item in card.history]
        lines.append("")
    return "\n".join(lines)


def _read_card(path: Path) -> Card:
    text = path.read_text(encoding="utf-8")
    fm = _extract_front_matter(text, path)
    data = tomllib.loads(fm)
    return _card_from_toml_dict(data)


def _extract_front_matter(text: str, path: Path) -> str:
    lines = text.splitlines()
    if not lines or lines[0].strip() != FRONT_MATTER_DELIM:
        raise ValueError(f"Missing front-matter opener in {path}")
    for i in range(1, len(lines)):
        if lines[i].strip() == FRONT_MATTER_DELIM:
            return "\n".join(lines[1:i]) + "\n"
    raise ValueError(f"Unclosed front-matter in {path}")


def _card_from_toml_dict(data: dict[str, Any]) -> Card:
    kwargs: dict[str, Any] = {}
    for key, value in data.items():
        if key not in _CARD_FIELD_NAMES:
            continue
        if key == "status":
            kwargs[key] = coerce_card_status(value)
        elif key == "priority":
            kwargs[key] = CardPriority(int(value))
        elif key == "owner_role":
            kwargs[key] = AgentRole(value) if value is not None else None
        elif key == "context_refs":
            coerced: list[ContextRef] = []
            for raw in value:
                ref = ContextRef.try_coerce(raw)
                if ref is None:
                    _LOG.warning(
                        "Dropping malformed context_ref in card %s: %r",
                        data.get("id", "<unknown>"),
                        raw,
                    )
                    continue
                coerced.append(ref)
            kwargs[key] = coerced
        elif key == "revision_requests":
            kwargs[key] = _coerce_revision_requests(
                value, card_id=data.get("id", "<unknown>"),
            )
        else:
            kwargs[key] = value
    return Card(**kwargs)


def _coerce_revision_requests(
    raw: Any, *, card_id: str,
) -> list[RevisionRequest]:
    if not isinstance(raw, list):
        return []
    out: list[RevisionRequest] = []
    for item in raw:
        if not isinstance(item, dict):
            _LOG.warning(
                "Dropping malformed revision_request in card %s: %r",
                card_id, item,
            )
            continue
        try:
            at = item.get("at")
            if isinstance(at, str):
                at = datetime.fromisoformat(at)
            if not isinstance(at, datetime):
                raise ValueError(f"bad at: {at!r}")
            if at.tzinfo is None:
                at = at.replace(tzinfo=UTC)
            from_role = AgentRole(str(item["from_role"]))
            out.append(
                RevisionRequest(
                    at=at,
                    from_role=from_role,
                    iteration=int(item.get("iteration", 0)),
                    summary=str(item.get("summary", "")),
                    hints=[str(h) for h in (item.get("hints") or [])],
                    failing_criteria=[
                        str(c) for c in (item.get("failing_criteria") or [])
                    ],
                )
            )
        except (KeyError, ValueError, TypeError) as exc:
            _LOG.warning(
                "Dropping malformed revision_request in card %s: %r (%s)",
                card_id, item, exc,
            )
    return out
