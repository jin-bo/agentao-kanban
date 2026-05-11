from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..models import CardStatus
from ..store import BoardStore
from .terminal import detach_worktree_on_terminal


@dataclass(slots=True)
class WipPolicy:
    doing_limit: int = 2


_WIP_STATUSES = (CardStatus.DOING, CardStatus.REVIEW)

# Sentinel used to distinguish "executor had no `working_directory` attribute"
# from "executor had `working_directory = None`". Dataclass executors like
# `MultiBackendExecutor` default the field to `None`, so a plain `is None`
# check would incorrectly trigger ``del`` and break the next run with
# ``AttributeError``.
_MISSING: object = object()


class WorktreeMissingError(RuntimeError):
    """Raised when a retry cannot proceed because the card's worktree
    branch was deleted and cannot be recovered. Caller should BLOCK."""


def _patch_executor_cwd(executor, worktree_path: Path):
    """Point the executor (and its router policy / client) at ``worktree_path``.

    Returns a ``restore()`` callable that puts every patched attribute back.
    Without walking into ``executor.policy`` / ``policy.client``, a card
    running under per-card worktree isolation would have the backend
    invocation read from the worktree while the router agent still read
    from the shared checkout — defeating isolation for profile selection.

    The executor itself is patched unconditionally (mirrors the v0.1.3
    contract: ``MockAgentaoExecutor`` has no ``working_directory`` field
    but the legacy/serial and worker paths have always patched it). For
    the router policy and its lazily-loaded client we only patch when
    the attribute already exists, so simple callable policies are left
    alone.
    """
    saved: list[tuple[object, object]] = []

    saved.append((executor, getattr(executor, "working_directory", _MISSING)))
    executor.working_directory = worktree_path

    policy = getattr(executor, "policy", None)
    if policy is not None and hasattr(policy, "working_directory"):
        saved.append((policy, policy.working_directory))
        policy.working_directory = worktree_path
        client = getattr(policy, "client", None)
        if client is not None and hasattr(client, "working_directory"):
            saved.append((client, client.working_directory))
            client.working_directory = worktree_path

    def restore() -> None:
        for target, prev in saved:
            if prev is _MISSING:
                if hasattr(target, "working_directory"):
                    try:
                        del target.working_directory
                    except AttributeError:
                        pass
            else:
                target.working_directory = prev

    return restore


def advance_inbox_dependents(store: BoardStore, done_card_id: str) -> list[str]:
    """Auto-advance INBOX cards whose dependencies are now fully satisfied.

    Called from every path that transitions a card from (!= DONE) to DONE
    (orchestrator commit, legacy ``tick()``, CLI ``move``/``unblock``,
    MCP equivalents). For each card still in INBOX that lists
    ``done_card_id`` among its ``depends_on`` and whose *every* dep is
    now DONE, moves the card INBOX → READY and emits a
    ``dependencies.satisfied`` runtime event plus an explicit
    history/plain-event message.

    Not recursive: only this card's direct reverse-dependencies are
    considered. Deeper chains advance naturally as each parent reaches
    DONE. Never touches non-INBOX candidates (BLOCKED / READY / DOING /
    REVIEW / DONE keep their current state).

    Runs a single ``store.list_cards()`` scan, O(n) over the board. Fine
    at current board sizes and simpler than maintaining a reverse-dep
    index.
    """
    advanced: list[str] = []
    for candidate in store.list_cards():
        if candidate.status != CardStatus.INBOX:
            continue
        if done_card_id not in candidate.depends_on:
            continue
        all_done = True
        for dep_id in candidate.depends_on:
            try:
                dep = store.get_card(dep_id)
            except KeyError:
                all_done = False
                break
            if dep.status != CardStatus.DONE:
                all_done = False
                break
        if not all_done:
            continue
        note = (
            f"Dependency {done_card_id[:8]} finished; all dependencies "
            f"satisfied — auto-advancing from inbox to ready"
        )
        store.move_card(candidate.id, CardStatus.READY, note)
        store.append_runtime_event(
            candidate.id,
            event_type="dependencies.satisfied",
            message=note,
        )
        advanced.append(candidate.id)
    return advanced
