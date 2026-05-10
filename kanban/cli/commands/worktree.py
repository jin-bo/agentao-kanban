"""``kanban worktree …`` subcommands.

Read state from ``WorktreeManager`` (``list`` / ``diff``) and clean it
up (``prune``). The prune path mutates card metadata + appends a
``worktree.pruned`` runtime event, so it respects ``.daemon.lock`` like
every other write path.
"""

from __future__ import annotations

import argparse
import sys

from ..helpers import (
    _make_store,
    _make_worktree_mgr,
    _require_writable,
    _resolve_card_id,
)


def cmd_worktree_list(args: argparse.Namespace) -> int:
    mgr = _make_worktree_mgr(args)
    active = mgr.list_active()
    if not active:
        # The empty case is the most-asked-about UX: users see "No active
        # worktrees" and assume their results vanished. Spell out that
        # active = on disk, while detached card branches and saved
        # artifacts still hold the actual deliverables.
        print(
            "No active worktree directories.\n"
            "Detached card branches (kanban/<card-id>) and any saved\n"
            "artifacts still hold the result. Try:\n"
            "  kanban result <card-id>          # unified result view\n"
            "  kanban worktree diff <card-id>   # inspect a preserved branch\n"
            "  kanban worktree prune            # clean up stale branches"
        )
        return 0
    for wt in active:
        path_str = str(wt.path) if wt.path else "(detached)"
        print(f"{wt.card_id[:8]}  {wt.branch}  {path_str}  HEAD={wt.head_commit[:12]}")
    return 0


def cmd_worktree_prune(args: argparse.Namespace) -> int:
    # Mutates card metadata (worktree_branch / worktree_base_commit) and
    # appends runtime events; must respect .daemon.lock like every other
    # write path so it doesn't race the scheduler's own prune cycle.
    _require_writable(args)
    mgr = _make_worktree_mgr(args)
    store = _make_store(args)
    all_cards = store.list_cards()
    card_statuses = {c.id: c.status for c in all_cards}
    card_blocked_at = {
        c.id: c.blocked_at for c in all_cards if c.blocked_at is not None
    }
    pruned = mgr.prune_stale(
        card_statuses,
        retention_days=args.retention_days,
        card_blocked_at=card_blocked_at,
    )
    if not pruned:
        print("No stale branches to prune.")
    else:
        for cid in pruned:
            # Clear stale worktree metadata so later reruns rebuild isolation.
            # Match the scheduler's idle-prune path in kanban/daemon.py so
            # manual prunes are also visible in events.log / `kanban events`.
            try:
                store.update_card(
                    cid, worktree_branch=None, worktree_base_commit=None,
                )
                store.append_runtime_event(
                    cid,
                    event_type="worktree.pruned",
                    message=f"Worktree branch pruned: kanban/{cid}",
                    worktree_branch=f"kanban/{cid}",
                )
            except KeyError:
                pass
            print(f"Pruned kanban/{cid}")
    return 0


def cmd_worktree_diff(args: argparse.Namespace) -> int:
    mgr = _make_worktree_mgr(args)
    store = _make_store(args)
    args.card_id = _resolve_card_id(store, args.card_id)
    try:
        card = store.get_card(args.card_id)
    except KeyError:
        print(f"No card with id {args.card_id}", file=sys.stderr)
        return 1
    if not card.worktree_base_commit:
        print(f"Card {args.card_id[:8]} has no worktree base commit.", file=sys.stderr)
        return 1
    from ...worktree import WorktreeDiffError

    try:
        diff = mgr.diff_summary(card.id, card.worktree_base_commit)
    except WorktreeDiffError as exc:
        print(f"worktree diff failed: {exc}", file=sys.stderr)
        return 1
    if diff:
        print(diff, end="")
    else:
        print("No changes.")
    return 0


def register_worktree_commands(sub) -> None:
    """Register the ``kanban worktree`` parser group."""
    wt = sub.add_parser("worktree", help="Manage Git worktrees for cards")
    wt_sub = wt.add_subparsers(dest="worktree_command", required=True)
    wt_sub.add_parser("list", help="List active worktrees")
    wt_prune = wt_sub.add_parser("prune", help="Clean up stale worktree branches")
    wt_prune.add_argument("--retention-days", type=int, default=7)
    wt_diff = wt_sub.add_parser("diff", help="Show diff for a card's worktree")
    wt_diff.add_argument("card_id")


def dispatch_worktree(args: argparse.Namespace) -> int:
    handlers = {
        "list": cmd_worktree_list,
        "prune": cmd_worktree_prune,
        "diff": cmd_worktree_diff,
    }
    return handlers[args.worktree_command](args)
