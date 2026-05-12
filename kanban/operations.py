"""Shared card transition operations.

Plain functions — no command bus, no state machine. The CLI, the MCP
tools, and the Web API all call these so ``move`` / ``requeue`` /
``block`` / ``unblock`` mean exactly one thing regardless of transport.

Contract (see ``docs/kanban-web-write-actions-safety-plan.md``):

* Each function validates its own inputs and raises :class:`OperationError`
  (a ``ValueError`` subclass) *before any store write* — bad input leaves
  the board byte-for-byte unchanged. Card-id resolution stays in the
  caller; a missing card surfaces as ``KeyError`` from ``store.get_card``.
* The first successful single-write :meth:`BoardStore.move_card` is the
  commit point. Auxiliary fields (``blocked_reason``, ``owner_role``) ride
  in that one write.
* Post-commit side effects — :func:`advance_inbox_dependents` on a fresh
  ``DONE`` landing, worktree detach on a terminal landing — are
  best-effort. A failure there is recorded as a recovery-style runtime
  event when possible and returned to the caller as a non-fatal warning
  string in :class:`TransitionResult.warnings`. It is never rolled back.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .models import Card, CardStatus, coerce_card_status
from .orchestrator import advance_inbox_dependents, detach_worktree_on_terminal
from .store import BoardStore

__all__ = [
    "OperationError",
    "TransitionResult",
    "transition_move",
    "transition_requeue",
    "transition_block",
    "transition_unblock",
]


class OperationError(ValueError):
    """Invalid input to a transition function — raised before any write."""


_TERMINAL = (CardStatus.DONE, CardStatus.BLOCKED)
# Targets ``kanban unblock --to`` / ``kanban requeue --to`` accept.
_UNBLOCK_TARGETS = frozenset(CardStatus)
_REQUEUE_TARGETS = frozenset({CardStatus.INBOX, CardStatus.READY})


@dataclass(slots=True)
class TransitionResult:
    card: Card
    warnings: list[str] = field(default_factory=list)


def _coerce_status(
    value: object, *, allowed: frozenset[CardStatus] | None = None
) -> CardStatus:
    valid = tuple(s.value for s in CardStatus if allowed is None or s in allowed)
    try:
        status = coerce_card_status(value)  # type: ignore[arg-type]
    except (ValueError, KeyError) as exc:
        raise OperationError(
            f"status must be one of {valid}, got {value!r}"
        ) from exc
    if allowed is not None and status not in allowed:
        raise OperationError(
            f"status must be one of {valid}, got {value!r}"
        )
    return status


def _run_side_effect(
    store: BoardStore,
    card_id: str,
    label: str,
    fn: Callable[[], object],
    *,
    event_type: str,
    warnings: list[str],
):
    """Run a post-commit side effect; never let it unwind the transition.

    Returns ``fn()``'s value on success, or ``None`` if it raised (in
    which case a recovery event is logged and a warning recorded).
    """
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - best-effort, must not propagate
        message = f"{label} failed after the transition committed: {exc}"
        try:
            store.append_runtime_event(
                card_id,
                event_type=event_type,
                message=message,
                failure_reason=str(exc),
            )
        except Exception as log_exc:  # noqa: BLE001
            warnings.append(
                f"{message} (and recording the recovery event also failed: {log_exc})"
            )
        else:
            warnings.append(message)
        return None


def _post_commit_side_effects(
    store: BoardStore,
    worktree_mgr,
    card_id: str,
    *,
    previous_status: CardStatus,
    new_status: CardStatus,
    warnings: list[str],
) -> None:
    if new_status == CardStatus.DONE and previous_status != CardStatus.DONE:
        _run_side_effect(
            store,
            card_id,
            "advancing inbox dependents",
            lambda: advance_inbox_dependents(store, card_id),
            event_type="dependencies.advance_failed",
            warnings=warnings,
        )
    if worktree_mgr is not None and new_status in _TERMINAL:
        detach_result = _run_side_effect(
            store,
            card_id,
            "detaching worktree",
            lambda: detach_worktree_on_terminal(store, worktree_mgr, card_id, new_status),
            event_type="worktree.detach_failed",
            warnings=warnings,
        )
        # detach_worktree_on_terminal records a worktree.detach_failed event
        # but doesn't raise when it keeps the worktree (uncommitted changes
        # couldn't be auto-committed) — surface that as a warning too, so the
        # best-effort-failure contract holds for API/CLI callers.
        if detach_result is not None and not detach_result.removed:
            warnings.append(
                f"worktree for card {card_id} was kept attached: uncommitted "
                f"changes could not be auto-committed. Finalize it manually "
                f"(see `kanban worktree diff {card_id[:8]}`)."
            )


def transition_move(
    store: BoardStore,
    worktree_mgr,
    card_id: str,
    status: object,
    *,
    note: str = "Manual move",
) -> TransitionResult:
    target = _coerce_status(status)
    previous_status = store.get_card(card_id).status
    card = store.move_card(card_id, target, note)
    warnings: list[str] = []
    _post_commit_side_effects(
        store,
        worktree_mgr,
        card.id,
        previous_status=previous_status,
        new_status=card.status,
        warnings=warnings,
    )
    return TransitionResult(card=card, warnings=warnings)


def transition_block(
    store: BoardStore,
    worktree_mgr,
    card_id: str,
    reason: str,
) -> TransitionResult:
    reason = (reason or "").strip()
    if not reason:
        raise OperationError("block reason must not be blank")
    previous_status = store.get_card(card_id).status
    card = store.move_card(
        card_id, CardStatus.BLOCKED, f"Blocked: {reason}", blocked_reason=reason
    )
    warnings: list[str] = []
    _post_commit_side_effects(
        store,
        worktree_mgr,
        card.id,
        previous_status=previous_status,
        new_status=card.status,
        warnings=warnings,
    )
    return TransitionResult(card=card, warnings=warnings)


def transition_unblock(
    store: BoardStore,
    worktree_mgr,
    card_id: str,
    target: object = CardStatus.INBOX,
) -> TransitionResult:
    status = _coerce_status(target, allowed=_UNBLOCK_TARGETS)
    previous_status = store.get_card(card_id).status
    card = store.move_card(
        card_id, status, f"Unblocked to {status.value}", blocked_reason=None
    )
    warnings: list[str] = []
    _post_commit_side_effects(
        store,
        worktree_mgr,
        card.id,
        previous_status=previous_status,
        new_status=card.status,
        warnings=warnings,
    )
    return TransitionResult(card=card, warnings=warnings)


def transition_requeue(
    store: BoardStore,
    card_id: str,
    target: object = CardStatus.INBOX,
    note: str | None = None,
) -> TransitionResult:
    status = _coerce_status(target, allowed=_REQUEUE_TARGETS)
    card = store.get_card(card_id)
    previous_status = card.status
    suffix = f": {note}" if note else ""
    history_note = f"Requeued from {previous_status.value} to {status.value}{suffix}"
    # Clear blocked_reason and reset owner_role — both requeue targets
    # (INBOX, READY) expect no pending owner — in the single card write.
    card = store.move_card(
        card_id, status, history_note, blocked_reason=None, owner_role=None
    )
    # Requeue targets are non-terminal: no DONE-dependents pass, no
    # worktree detach. Keep the worktree attached so the next dispatch
    # can resume in it.
    return TransitionResult(card=card, warnings=[])
