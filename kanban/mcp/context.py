from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from ..daemon import assert_no_daemon
from ..store_markdown import MarkdownBoardStore


class CardClaimedError(RuntimeError):
    """MCP write refused because the target card has a live execution claim.

    Mirrors the CLI's ``_require_card_writable`` refusal so split-topology
    workers (which don't hold ``.daemon.lock``) still prevent operators
    from racing an in-flight execution.
    """


@dataclass
class ServerContext:
    """Runtime config bound to one MCP server process."""

    board_dir: Path
    force: bool = False
    executor_name: str = "mock"
    # Worktree isolation tri-state, matching the CLI's --worktree flag:
    # None = auto (enable in a Git repo, warn-and-disable otherwise),
    # True = hard-require a Git repo, False = always disabled.
    worktree_mode: bool | None = None

    def store(self) -> MarkdownBoardStore:
        # New store per call: cheap, and any out-of-band daemon write is
        # picked up on the next read without an explicit refresh().
        return MarkdownBoardStore(self.board_dir)

    def guard_write(self) -> None:
        """Refuse writes while a live daemon holds .daemon.lock.

        Mirrors the CLI's ``_require_writable`` semantics. ``--force`` on
        server startup disables the guard for recovery sessions.
        """
        if self.force:
            return
        assert_no_daemon(self.board_dir)

    def guard_card_write(self, card_id: str) -> None:
        """Board-lock + per-card live-claim guard (v0.1.2 split topology).

        Workers don't hold ``.daemon.lock`` but do hold live claims on
        specific cards while executing. Mutating a card with a live
        claim races the worker's next envelope — the worker's result can
        overwrite the operator's change, or the change can invalidate
        the assumptions of the in-flight execution. Refuse unless the
        server was started with ``--force``.
        """
        self.guard_write()
        if self.force:
            return
        claim = self.store().get_claim(card_id)
        if claim is None:
            return
        worker_tag = (
            f"worker={claim.worker_id}" if claim.worker_id else "unassigned"
        )
        raise CardClaimedError(
            f"Card {card_id[:8]} has a live execution claim "
            f"{claim.claim_id} ({worker_tag}); refuse to mutate. Stop "
            f"the claimed worker (or wait for it to finish), then retry. "
            f"Restart kanban-mcp with --force to override (may race with "
            f"in-flight execution)."
        )


def _terminal_worktree_mgr(ctx: ServerContext):
    """WorktreeManager for the shared ``transition_*`` operations, or ``None``.

    Quiet counterpart to :func:`_resolve_worktree_mgr`: never prints,
    never raises. Returns ``None`` when worktree isolation is disabled or
    the board is not in a Git repo, so the detach step inside the
    transition functions is then a no-op.
    """
    if ctx.worktree_mode is False:
        return None
    from ..cli import _find_git_root_optional

    project_root = _find_git_root_optional(ctx.board_dir)
    if project_root is None:
        return None
    from ..worktree import WorktreeManager

    return WorktreeManager.for_project(project_root)


def _resolve_worktree_mgr(ctx: ServerContext):
    """Resolve the WorktreeManager for this MCP server's tick/run path.

    Mirrors ``cli._resolve_worktree_mgr``: tri-state on ``worktree_mode``,
    same auto/required/disabled semantics, same warning text. Without
    this, MCP-driven ``tick``/``run`` would execute worker/reviewer/
    verifier in the shared checkout instead of the per-card worktree the
    daemon and CLI use.
    """
    if ctx.worktree_mode is False:
        return None
    from ..cli import _find_git_root_optional

    project_root = _find_git_root_optional(ctx.board_dir)
    if project_root is None:
        if ctx.worktree_mode is True:
            # Hard-require mode: surface a tool-level error rather than
            # SystemExit, which would kill the stdio server mid-request.
            # Startup validation in ``main()`` rejects this combination
            # up front; this branch is defense-in-depth for programmatic
            # ``ServerContext`` construction.
            raise RuntimeError(
                f"--worktree requires a Git repository (no repo found for "
                f"{ctx.board_dir}); restart kanban-mcp with --no-worktree "
                f"or move the board inside a repo."
            )
        print(
            "kanban-mcp: worktree isolation disabled (board not in a Git "
            "repo). Pass --no-worktree to silence, or --worktree to "
            "hard-require a repo.",
            file=sys.stderr,
        )
        return None
    from ..worktree import WorktreeManager

    return WorktreeManager(
        project_root=project_root,
        worktrees_root=project_root / "workspace" / "worktrees",
        artifacts_root=project_root / "workspace" / "raw",
    )
