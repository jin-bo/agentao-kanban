from __future__ import annotations

from ..models import CardStatus
from ..store import BoardStore


def block_card(
    store: BoardStore,
    worktree_mgr,
    card_id: str,
    reason: str,
    *,
    event_type: str | None = None,
    **event_fields,
):
    store.update_card(card_id, blocked_reason=reason)
    card = store.move_card(card_id, CardStatus.BLOCKED, f"Blocked: {reason}")
    if event_type is not None:
        store.append_runtime_event(
            card_id,
            event_type=event_type,
            message=reason,
            **event_fields,
        )
    detach_worktree_on_terminal(store, worktree_mgr, card_id, CardStatus.BLOCKED)
    return card


def detach_worktree_on_terminal(
    store: BoardStore,
    worktree_mgr,
    card_id: str,
    target_status: CardStatus,
) -> None:
    """Detach the card's worktree if the transition is terminal.

    Mirrors the inline logic ``KanbanOrchestrator._apply_normal_result``
    has used since v0.1.3. Factored out so manual CLI transitions
    (``kanban block``, ``kanban move <id> done``, ``card edit
    --set-status``) don't leak attached ``workspace/worktrees/<card-id>``
    directories — once attached, ``worktree prune`` skips the branch
    because the directory still exists.

    No-op when:

    - ``worktree_mgr`` is ``None`` (board not git-backed),
    - ``target_status`` is not ``DONE`` / ``BLOCKED``, or
    - the card was never attached to a worktree.
    """
    if worktree_mgr is None:
        return
    if target_status not in (CardStatus.DONE, CardStatus.BLOCKED):
        return
    card = store.get_card(card_id)
    if card.worktree_branch is None:
        return
    result = worktree_mgr.detach(card_id)
    if getattr(result, "artifacts_path", None) is not None:
        store.append_runtime_event(
            card_id,
            event_type="worktree.artifacts_saved",
            message=(
                f"Ignored deliverables saved to {result.artifacts_path} "
                "(see `kanban result`)."
            ),
            worktree_branch=card.worktree_branch,
        )
    if result:
        store.append_runtime_event(
            card_id,
            event_type="worktree.detached",
            message=(
                f"Worktree directory removed; result branch preserved: "
                f"{card.worktree_branch}. Use `kanban result <card-id>` "
                "or `kanban worktree diff <card-id>`."
            ),
            worktree_branch=card.worktree_branch,
        )
    else:
        store.append_runtime_event(
            card_id,
            event_type="worktree.detach_failed",
            message=(
                f"Worktree directory kept (uncommitted changes); branch "
                f"{card.worktree_branch} not finalized."
            ),
            worktree_branch=card.worktree_branch,
        )
