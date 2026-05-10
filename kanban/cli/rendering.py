"""Rendering helpers for ``show``, ``result``, ``events``, ``claims``,
and ``workers``: card → YAML/JSON, event/claim/worker line formatters,
and the result-summary builder."""

from __future__ import annotations

import argparse
import json as _json
from pathlib import Path
from typing import Any

from ..models import Card, CardEvent, CardStatus, ContextRef
from ..store_markdown import MarkdownBoardStore
from .helpers import _find_git_root_optional


def _iso_z(dt) -> str:
    """Render a datetime as ISO-Z (matches ``_format_event_line``)."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _context_ref_to_mapping(ref: ContextRef) -> dict[str, object]:
    out: dict[str, object] = {"kind": ref.kind, "path": ref.path}
    if ref.note:
        out["note"] = ref.note
    return out


def _revision_to_mapping(rev) -> dict[str, object]:
    out: dict[str, object] = {
        "iteration": rev.iteration,
        "from_role": rev.from_role.value,
        "at": _iso_z(rev.at),
        "summary": rev.summary,
    }
    if rev.hints:
        out["hints"] = list(rev.hints)
    if rev.failing_criteria:
        out["failing_criteria"] = list(rev.failing_criteria)
    return out


def _card_to_mapping(card: Card) -> dict[str, object]:
    """Build the ordered dict rendered by both YAML and JSON outputs.

    Only set/non-empty fields are included so the block stays tight for
    cards that haven't hit the runtime path yet.
    """
    data: dict[str, object] = {
        "id": card.id,
        "title": card.title,
        "status": card.status.value,
        "priority": card.priority.name,
    }
    if card.owner_role is not None:
        data["owner_role"] = card.owner_role.value
    data["goal"] = card.goal
    if card.blocked_reason:
        data["blocked_reason"] = card.blocked_reason
    if card.blocked_at is not None:
        data["blocked_at"] = _iso_z(card.blocked_at)
    data["created_at"] = _iso_z(card.created_at)
    data["updated_at"] = _iso_z(card.updated_at)
    if card.agent_profile:
        data["agent_profile"] = card.agent_profile
    if card.agent_profile_source:
        data["agent_profile_source"] = card.agent_profile_source
    if card.worktree_branch:
        data["worktree_branch"] = card.worktree_branch
    if card.worktree_base_commit:
        data["worktree_base_commit"] = card.worktree_base_commit
    if card.rework_iteration:
        data["rework_iteration"] = card.rework_iteration
    if card.revision_requests:
        data["revision_requests"] = [
            _revision_to_mapping(r) for r in card.revision_requests
        ]
    if card.depends_on:
        data["depends_on"] = list(card.depends_on)
    if card.acceptance_criteria:
        data["acceptance_criteria"] = list(card.acceptance_criteria)
    if card.context_refs:
        data["context_refs"] = [_context_ref_to_mapping(r) for r in card.context_refs]
    if card.outputs:
        data["outputs"] = dict(card.outputs)
    if card.history:
        data["history"] = list(card.history)
    return data


# yaml import + _BlockDumper class build are deferred to the first
# `show`/`result` invocation; commands that emit JSON or never render
# never pay the ~7ms PyYAML import cost.
_BlockDumper = None  # type: ignore[assignment]


def _yaml_str_representer(dumper, data: str):
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


def _get_block_dumper():
    """Lazy-build the multi-line-aware SafeDumper.

    Default PyYAML renders ``"line1\\nline2"`` as a quoted single-line
    string with embedded escapes — unreadable for cards that carry
    transcripts in ``outputs``. A subclass (not a global representer
    mutation) keeps the override scoped to this CLI.
    """
    global _BlockDumper
    if _BlockDumper is None:
        import yaml
        class _BlockDumperImpl(yaml.SafeDumper):
            pass
        _BlockDumperImpl.add_representer(str, _yaml_str_representer)
        _BlockDumper = _BlockDumperImpl
    return _BlockDumper


def _render_card(
    card: Card,
    *,
    as_json: bool,
    extras: dict[str, object] | None = None,
) -> str:
    mapping = _card_to_mapping(card)
    if extras:
        mapping.update(extras)
    if as_json:
        return _json.dumps(mapping, ensure_ascii=False)
    import yaml
    return yaml.dump(
        mapping,
        Dumper=_get_block_dumper(),
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=100,
    )


def _worktree_state(args: argparse.Namespace, card: Card) -> tuple[str, Path | None]:
    """Return ``(state, path)`` for the card's worktree directory.

    ``state`` is one of:

    - ``"none"`` — the card was never attached to a worktree (no branch
      recorded).
    - ``"not-git"`` — the board isn't inside a Git repo, so worktree
      isolation is structurally impossible.
    - ``"active"`` — branch and on-disk worktree directory both exist.
    - ``"detached"`` — branch is preserved but the directory has been
      released (terminal status, manual prune target).
    - ``"missing"`` — branch metadata recorded on the card no longer
      resolves (manual `git branch -D` or filesystem corruption).
    """
    if not card.worktree_branch:
        return "none", None
    git_root = _find_git_root_optional(args.board)
    if git_root is None:
        return "not-git", None
    from ..worktree import WorktreeManager

    mgr = WorktreeManager(
        project_root=git_root,
        worktrees_root=git_root / "workspace" / "worktrees",
        artifacts_root=git_root / "workspace" / "raw",
    )
    info = mgr.get(card.id, base_commit=card.worktree_base_commit or "")
    if info is None:
        return "missing", None
    if info.path is not None:
        return "active", info.path
    return "detached", None


def _list_artifact_dirs(args: argparse.Namespace, card_id: str) -> list[Path]:
    """Return the per-card artifact snapshot directories, newest first.

    Empty list when artifacts capture is disabled, the board isn't git-backed,
    or the card never produced ignored deliverables.
    """
    git_root = _find_git_root_optional(args.board)
    if git_root is None:
        return []
    card_dir = git_root / "workspace" / "raw" / card_id
    if not card_dir.exists():
        return []
    snapshots = sorted(card_dir.glob("artifacts-*"))
    snapshots.reverse()
    return [p for p in snapshots if p.is_dir()]


def _summarize_card_result(
    args: argparse.Namespace, store: MarkdownBoardStore, card: Card
) -> dict[str, object]:
    """Collect everything a user means by "the result of this card".

    Pulls together status/summary, the worktree branch + its on-disk
    state, artifact snapshots, retained transcripts, and the next-step
    commands so callers don't have to know that results live in three
    different directories.
    """
    state, path = _worktree_state(args, card)
    artifacts = _list_artifact_dirs(args, card.id)
    traces: list = []
    try:
        traces = store.list_traces(card.id)
    except Exception:  # noqa: BLE001 — read-only enrichment, never fatal
        traces = []
    summary = ""
    output_paths: list[str] = []
    if isinstance(card.outputs, dict):
        last = card.outputs.get("last")
        if isinstance(last, dict):
            cand = last.get("summary")
            if isinstance(cand, str):
                summary = cand
            raw_outputs = last.get("output")
            if isinstance(raw_outputs, list):
                output_paths = [str(p) for p in raw_outputs]
            elif isinstance(raw_outputs, str) and raw_outputs:
                output_paths = [raw_outputs]
    next_steps: list[str] = []
    if state == "active":
        next_steps.append(
            f"kanban worktree diff {card.id[:8]}    # review the in-progress changes"
        )
    elif state == "detached":
        next_steps.append(
            f"kanban worktree diff {card.id[:8]}    # review changes on the preserved branch"
        )
        next_steps.append(
            f"git merge {card.worktree_branch}     # merge the result into the main checkout"
        )
    elif state == "missing":
        next_steps.append(
            f"kanban worktree prune                # clear stale metadata for {card.worktree_branch}"
        )
    if traces:
        next_steps.append(
            f"kanban traces {card.id[:8]} --latest  # inspect the most recent transcript"
        )
    if state == "none" and card.status == CardStatus.DONE:
        next_steps.append("(no worktree was ever attached to this card)")
    return {
        "card_id": card.id,
        "title": card.title,
        "status": card.status.value,
        "blocked_reason": card.blocked_reason,
        "summary": summary,
        "outputs": output_paths,
        "worktree": {
            "branch": card.worktree_branch,
            "base_commit": card.worktree_base_commit,
            "state": state,
            "path": str(path) if path is not None else None,
        },
        "artifacts": [str(p) for p in artifacts],
        "transcripts": [t.path for t in traces],
        "next_steps": next_steps,
    }


def _show_extras(
    args: argparse.Namespace, store: MarkdownBoardStore, card: Card
) -> dict[str, object]:
    """Compose a ``result:`` block to inline into ``kanban show`` output.

    Returns ``{}`` when nothing interesting exists (no worktree, no
    transcripts, no artifacts) so the show payload stays tight for fresh
    cards. Returned dicts are JSON-and-YAML safe — strings, lists of
    strings, and dicts of those.
    """
    summary = _summarize_card_result(args, store, card)
    wt = summary.get("worktree") or {}
    artifacts = summary.get("artifacts") or []
    transcripts = summary.get("transcripts") or []
    if (
        wt.get("state") in ("none", "not-git")
        and not artifacts
        and not transcripts
        and not summary.get("summary")
    ):
        return {}
    block: dict[str, object] = {
        "worktree_state": wt.get("state"),
    }
    if wt.get("path"):
        block["worktree_path"] = wt["path"]
    if summary.get("summary"):
        block["summary"] = summary["summary"]
    if artifacts:
        block["artifacts"] = list(artifacts)
    if transcripts:
        block["transcripts"] = list(transcripts)
    if summary.get("next_steps"):
        block["next"] = list(summary["next_steps"])
    return {"result": block}


def _format_result_block(result: dict[str, object], *, indent: str = "") -> str:
    """Render a result summary as a short, human-friendly block."""
    lines: list[str] = []
    lines.append(f"{indent}Result:")
    lines.append(f"{indent}  status: {result['status']}")
    if result.get("blocked_reason"):
        lines.append(f"{indent}  blocked_reason: {result['blocked_reason']}")
    if result.get("summary"):
        lines.append(f"{indent}  summary: {result['summary']}")
    wt = result.get("worktree") or {}
    state = wt.get("state")
    if state == "none":
        lines.append(f"{indent}  worktree: never attached")
    elif state == "not-git":
        lines.append(f"{indent}  worktree: n/a (board not in a Git repo)")
    elif state == "active":
        lines.append(
            f"{indent}  worktree: active at {wt.get('path')} (branch {wt.get('branch')})"
        )
    elif state == "detached":
        lines.append(
            f"{indent}  worktree: detached (directory released; branch {wt.get('branch')} preserved)"
        )
    elif state == "missing":
        lines.append(
            f"{indent}  worktree: branch {wt.get('branch')} no longer resolves"
        )
    outs = result.get("outputs") or []
    if outs:
        lines.append(f"{indent}  outputs:")
        for o in outs:
            lines.append(f"{indent}    - {o}")
    arts = result.get("artifacts") or []
    if arts:
        lines.append(f"{indent}  artifacts:")
        for a in arts:
            lines.append(f"{indent}    - {a}")
    traces = result.get("transcripts") or []
    if traces:
        latest = traces[-1]
        more = len(traces) - 1
        suffix = f" (+{more} earlier)" if more > 0 else ""
        lines.append(f"{indent}  transcripts: {latest}{suffix}")
    next_steps = result.get("next_steps") or []
    if next_steps:
        lines.append(f"{indent}  next:")
        for n in next_steps:
            lines.append(f"{indent}    - {n}")
    return "\n".join(lines) + "\n"


def _event_to_json(e: CardEvent) -> dict[str, Any]:
    record: dict[str, Any] = {
        "at": e.at.isoformat(),
        "card_id": e.card_id,
        "message": e.message,
    }
    if e.is_execution:
        record["role"] = e.role.value if e.role else None
        record["prompt_version"] = e.prompt_version
        record["duration_ms"] = e.duration_ms
        record["attempt"] = e.attempt
        if e.raw_path is not None:
            record["raw_path"] = e.raw_path
    # Runtime lifecycle fields (PR4/M3). Present on claimed / finished /
    # failed / retried / claim_recovered / result_orphaned events.
    for key, value in (
        ("event_type", e.event_type),
        ("claim_id", e.claim_id),
        ("worker_id", e.worker_id),
        ("failure_reason", e.failure_reason),
        ("failure_category", e.failure_category),
        ("retry_of_claim_id", e.retry_of_claim_id),
        ("worktree_branch", e.worktree_branch),
        ("rework_iteration", e.rework_iteration),
    ):
        if value is not None:
            record[key] = value
    return record


def _format_event_line(e: CardEvent) -> str:
    stamp = e.at.strftime("%Y-%m-%dT%H:%M:%SZ") if e.at.tzinfo else e.at.isoformat()
    # Runtime events lead with [event_type]; execution events with [role];
    # plain events with [system]. Operators scanning the log should be able
    # to tell the three apart at a glance.
    if e.event_type is not None:
        tag = f"[{e.event_type}]"
    elif e.role is not None:
        tag = f"[{e.role.value}]"
    else:
        tag = "[system]"
    extras: list[str] = []
    if e.claim_id:
        extras.append(f"claim={e.claim_id}")
    if e.worker_id:
        extras.append(f"worker={e.worker_id}")
    if e.attempt is not None and e.event_type is not None:
        extras.append(f"attempt={e.attempt}")
    if e.retry_of_claim_id:
        extras.append(f"retry_of={e.retry_of_claim_id}")
    if e.worktree_branch:
        extras.append(f"wt={e.worktree_branch}")
    if e.rework_iteration is not None:
        extras.append(f"rework={e.rework_iteration}")
    suffix = ("  " + " ".join(extras)) if extras else ""
    return f"{stamp}  {e.card_id[:8]}  {tag}  {e.message}{suffix}"


def _format_age(delta_seconds: float) -> str:
    """Short human age: 3s / 42s / 5m12s / 2h03m / 3d04h."""
    s = int(delta_seconds)
    sign = "-" if s < 0 else ""
    s = abs(s)
    if s < 60:
        return f"{sign}{s}s"
    if s < 3600:
        return f"{sign}{s // 60}m{s % 60:02d}s"
    if s < 86400:
        return f"{sign}{s // 3600}h{(s % 3600) // 60:02d}m"
    return f"{sign}{s // 86400}d{(s % 86400) // 3600:02d}h"
