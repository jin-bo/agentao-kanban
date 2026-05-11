"""Shared "what is the result of this card?" aggregator.

Pulls together status/summary, the worktree branch + its on-disk state,
artifact snapshots, retained transcripts, and the next-step commands so
callers don't have to know that results live in three different
directories. Used by the CLI (``kanban result`` / ``kanban show``) and by
the Web Result endpoint.

Note on artifact roots: the CLI resolves artifact snapshots under
``<git_root>/workspace/raw`` while the Web layer uses the store's
``raw_root`` (``board_dir.parent / "raw"`` by default). Callers pass the
root they want via ``artifacts_root``; in the standard ``kanban init``
layout the two coincide. Transcripts always come from the passed-in
store's ``list_traces`` (i.e. ``store.raw_root``).
"""

from __future__ import annotations

from pathlib import Path

from .gitutil import find_git_root_optional
from .models import Card, CardStatus
from .worktree import WorktreeManager


def worktree_state(board_dir: Path, card: Card) -> tuple[str, Path | None]:
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
      resolves (manual ``git branch -D`` or filesystem corruption).
    """
    if not card.worktree_branch:
        return "none", None
    git_root = find_git_root_optional(board_dir)
    if git_root is None:
        return "not-git", None
    # Read-only probe: never mutate the repo (this path backs `kanban
    # result`/`kanban show` and the read-only web result endpoint).
    mgr = WorktreeManager.for_project(git_root, manage_exclude=False)
    info = mgr.get(card.id, base_commit=card.worktree_base_commit or "")
    if info is None:
        return "missing", None
    if info.path is not None:
        return "active", info.path
    return "detached", None


def list_artifact_dirs(artifacts_root: Path | None, card_id: str) -> list[Path]:
    """Return the per-card artifact snapshot directories, newest first.

    Empty list when ``artifacts_root`` is ``None`` (artifacts capture
    disabled / board not git-backed for the CLI caller) or the card never
    produced ignored deliverables.
    """
    if artifacts_root is None:
        return []
    card_dir = artifacts_root / card_id
    if not card_dir.exists():
        return []
    snapshots = sorted(card_dir.glob("artifacts-*"))
    snapshots.reverse()
    return [p for p in snapshots if p.is_dir()]


def cli_artifacts_root(board_dir: Path) -> Path | None:
    """Artifacts root the CLI uses: ``<git_root>/workspace/raw`` or ``None``.

    ``None`` when the board isn't inside a Git repo — the CLI doesn't
    surface artifacts in that case (and worktree isolation is unavailable
    anyway).
    """
    git_root = find_git_root_optional(board_dir)
    if git_root is None:
        return None
    return git_root / "workspace" / "raw"


def summarize_card_result(
    board_dir: Path,
    store,
    card: Card,
    *,
    artifacts_root: Path | None,
) -> dict[str, object]:
    """Collect everything a user means by "the result of this card".

    ``artifacts_root`` is where artifact snapshots live (see module
    docstring); transcripts come from ``store.list_traces``. ``next_steps``
    are human-readable command strings — the same shape the CLI has always
    returned.
    """
    state, path = worktree_state(board_dir, card)
    artifacts = list_artifact_dirs(artifacts_root, card.id)
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
