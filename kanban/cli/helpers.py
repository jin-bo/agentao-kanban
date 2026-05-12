"""Cross-command helpers: board discovery, store/orchestrator factories,
writability guards, and git-root probes shared by every subcommand."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..daemon import DaemonLockError, assert_no_daemon
from ..executors import CardExecutor, MockAgentaoExecutor
from ..gitutil import find_git_root_optional as _find_git_root_optional
from ..init import (
    DEFAULT_BOARD_REL as DEFAULT_BOARD,
    MARKER_DIR,
    read_board_dir_override,
)
from ..models import CardStatus
from ..operations import OperationError
from ..orchestrator import KanbanOrchestrator
from ..store_markdown import MarkdownBoardStore


def _discover_board(cwd: Path | None = None) -> Path:
    """Walk up from ``cwd`` looking for a ``kanban init`` marker.

    Mirrors git's "first ancestor wins" semantics so a user in a deep
    subdirectory hits the same board as one at the repo root. With no
    marker we fall back to ``<cwd>/workspace/board`` to preserve the
    pre-init "just clone and run" flow.
    """
    start = (cwd or Path.cwd()).resolve()
    for candidate in (start, *start.parents):
        marker = candidate / MARKER_DIR
        if not marker.is_dir():
            continue
        cfg = marker / "config.yaml"
        if cfg.is_file():
            override = read_board_dir_override(cfg)
            if override:
                return (candidate / override).resolve()
        return (candidate / "workspace" / "board").resolve()
    return (start / DEFAULT_BOARD).resolve()


def _apply_limit(items: list, limit: int | None) -> list:
    """Mirror the store's tail semantics: None=all, <=0=none."""
    if limit is None:
        return items
    if limit <= 0:
        return []
    return items[-limit:]


def _non_negative_int(value: str) -> int:
    n = int(value)
    if n < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0, got {n}")
    return n


def _resolve_card_id(store: MarkdownBoardStore, given: str) -> str:
    """Expand a unique card-id prefix to the full id.

    - Exact full-id match → returned as-is (fast path).
    - Unique prefix match → expanded to the full id.
    - No match → returned unchanged so the caller's KeyError path owns
      the "No card with id X" error and echoes the operator's input.
    - Ambiguous prefix (2+ matches) → SystemExit(2) with the candidate
      list, so operators can't silently address the wrong card.
    """
    try:
        store.get_card(given)
        return given
    except KeyError:
        pass
    matches = [c.id for c in store.list_cards() if c.id.startswith(given)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        shown = ", ".join(m[:12] for m in matches[:5])
        more = f" (+{len(matches) - 5} more)" if len(matches) > 5 else ""
        print(
            f"Ambiguous card id prefix {given!r} matches {len(matches)} "
            f"cards: {shown}{more}. Provide more characters.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return given


# Statuses an operator may force via `card edit --set-status`. doing/review
# are excluded because they have an expected owner_role and would desync the
# orchestrator — use `requeue` for recovery paths.
_OPERATOR_STATUSES = (
    CardStatus.INBOX,
    CardStatus.READY,
    CardStatus.BLOCKED,
    CardStatus.DONE,
)


def _project_root_for(board: Path) -> Path:
    """Infer the project root from ``--board``.

    Walks up from the board directory and returns the first ancestor
    that contains a ``.kanban/`` or ``.agentao/`` marker. When no marker
    is found we fall back to the Git toplevel for the board path — which
    is the right answer for the documented default layout
    (``workspace/board`` inside a fresh checkout that hasn't created
    ``.kanban/``/``.agentao/`` yet) so agentao runs against the source
    tree rather than the board directory. As a last resort, return the
    resolved board path. We never use ``Path.cwd()``, so
    ``kanban --board /elsewhere/board ...`` can't silently pick up the
    shell cwd's ``.kanban/``/``.agentao/`` config.
    """
    try:
        current = board.resolve()
    except OSError:
        return board
    for candidate in (current, *current.parents):
        if (candidate / ".kanban").is_dir() or (candidate / ".agentao").is_dir():
            return candidate
    git_root = _find_git_root_optional(board)
    if git_root is not None:
        return git_root
    return current


def _find_git_root(board: Path) -> Path:
    """Find the Git toplevel for a board path, raising SystemExit if none.

    Used by subcommands that cannot function outside a Git repo
    (``worktree list/prune/diff``) and by the ``--worktree`` explicit path.
    """
    root = _find_git_root_optional(board)
    if root is None:
        raise SystemExit(
            f"--worktree requires a Git repository (no repo found for {board})"
        )
    return root


def _project_root_or_cwd(board: Path | None) -> Path:
    return _project_root_for(board) if board is not None else Path.cwd()


def _agents_dir_for(project_root: Path) -> Path:
    """Return ``<project_root>/.agentao/agents`` even when it's missing.

    We always return a concrete path so downstream spec loaders
    (``SubagentBackend``, ``RouterPolicy``) get an explicit, board-scoped
    search root instead of falling through to their ``Path.cwd()``
    default. A non-existent path is harmless: the spec loader's
    ``is_file()`` check skips it and the packaged-defaults fallback
    still fires.
    """
    return project_root / ".agentao" / "agents"


def _build_executor(name: str, board: Path | None = None) -> CardExecutor:
    if name == "mock":
        return MockAgentaoExecutor()
    if name == "agentao":
        try:
            from ..executors.agentao_multi import AgentaoMultiAgentExecutor
        except ImportError as exc:
            raise SystemExit(
                "agentao package is not installed. Run `uv add --editable ../agentao` first."
            ) from exc
        project_root = _project_root_or_cwd(board)
        return AgentaoMultiAgentExecutor(
            agents_dir=_agents_dir_for(project_root),
            working_directory=project_root,
        )
    if name == "multi-backend":
        try:
            from ..agent_profiles import ProfileConfigError, load_default_config
            from ..executors.backends.acp_backend import AcpBackend
            from ..executors.backends.subagent_backend import SubagentBackend
            from ..executors.multi_backend import MultiBackendExecutor
            from ..executors.router_policy import RouterPolicy
        except ImportError as exc:
            raise SystemExit(
                "agentao package is not installed. Run `uv add --editable ../agentao` first."
            ) from exc

        # Derive the project root from --board so config, subagent specs,
        # ACP server definitions, and router spec all come from the same
        # place. A bare `kanban --board /elsewhere ...` invocation must
        # not silently read from the shell's cwd.
        project_root = _project_root_or_cwd(board)
        agents_dir = _agents_dir_for(project_root)
        # Pass the *intended* agents dir even when it doesn't exist on
        # disk: the spec loader's ``is_file()`` guard skips missing
        # paths, and passing ``None`` would let it fall back to
        # ``Path.cwd()/.agentao/agents`` — reintroducing the shell-cwd
        # leak we just closed in ``_project_root_for``.

        try:
            config = load_default_config(base=project_root)
        except ProfileConfigError as exc:
            raise SystemExit(f"agent_profiles.yaml: {exc}") from exc
        # Register both backend types so ACP-routed profiles — including
        # card-pinned ones like `gemini-worker` — can actually run.
        # `AcpBackend` loads the ACPManager lazily on first invoke, so
        # environments without `.agentao/acp.json` only fail when a card
        # actually routes to an ACP profile.
        #
        # The router policy is always installed; its own guards
        # (KANBAN_ROUTER=off, router.enabled_roles, missing spec,
        # single-candidate short-circuit) decide whether it actually
        # calls the router agent. Installing it unconditionally keeps
        # CLI startup independent of whether the router spec is present.
        return MultiBackendExecutor(
            config=config,
            working_directory=project_root,
            agents_dir=agents_dir,
            backends={
                "subagent": SubagentBackend(agents_dir=agents_dir),
                "acp": AcpBackend(project_root=project_root),
            },
            policy=RouterPolicy(
                agents_dir=agents_dir,
                working_directory=project_root,
            ),
        )
    raise ValueError(f"Unknown executor: {name}")


def _make_store(args: argparse.Namespace) -> MarkdownBoardStore:
    """Open the board store without constructing an executor.

    Use this for read-only commands (`list`, `show`, `events`, `traces`,
    `doctor`) and for write commands that do not need an agent runner
    (`card add/edit/context/acceptance`, `block`, `unblock`, `requeue`,
    `move`). Building the executor can import the optional `agentao`
    package, which these commands don't need.
    """
    return MarkdownBoardStore(args.board)


def _detach_worktree_after_terminal_cli(
    args: argparse.Namespace, store: MarkdownBoardStore, card_id: str
) -> None:
    """CLI counterpart to ``KanbanOrchestrator._apply_normal_result``'s
    detach step. Called by manual transitions to BLOCKED/DONE so they
    release the attached ``workspace/worktrees/<card-id>`` directory
    (otherwise ``worktree prune`` skips the branch because the
    directory still exists, and manually-blocked cards accumulate
    stale attached worktrees forever).

    Quietly no-ops when:

    - ``--no-worktree`` was passed (operator explicitly opted out of
      touching Git state on this command),
    - ``--force`` is set on a board outside any Git repo,
    - the card was never attached to a worktree,
    - the transition is not terminal.

    Suppresses the auto-resolver's "worktree disabled" stderr warning
    that would otherwise fire on every block/move/edit on a non-git
    board — that warning is for orchestrator init, not cleanup.
    """
    if getattr(args, "worktree", None) is False:
        return
    try:
        card = store.get_card(card_id)
    except KeyError:
        return
    if card.worktree_branch is None:
        return
    if card.status not in (CardStatus.DONE, CardStatus.BLOCKED):
        return
    project_root = _find_git_root_optional(args.board)
    if project_root is None:
        return
    from ..orchestrator import detach_worktree_on_terminal
    from ..worktree import WorktreeManager

    detach_worktree_on_terminal(
        store, WorktreeManager.for_project(project_root), card_id, card.status
    )


def _run_transition(card_id: str, thunk):
    """Run a CLI ``transition_*`` call, mapping its failures to ``(result, rc)``.

    ``thunk`` is a zero-arg callable returning a ``TransitionResult``. On
    success returns ``(result, 0)`` after printing any post-commit warnings
    to stderr; a missing card is ``(None, 1)``; bad input is ``(None, 2)``.
    Callers own the success line.
    """
    try:
        result = thunk()
    except KeyError:
        print(f"No card with id {card_id}", file=sys.stderr)
        return None, 1
    except OperationError as exc:
        print(str(exc), file=sys.stderr)
        return None, 2
    for warning in result.warnings:
        print(warning, file=sys.stderr)
    return result, 0


def _detach_worktree_mgr(args: argparse.Namespace):
    """Worktree manager for manual transitions, or ``None``.

    Quiet counterpart to :func:`_resolve_worktree_mgr` used by the shared
    ``transition_*`` operations: never prints, never raises. Returns
    ``None`` when ``--no-worktree`` was passed or the board is not in a
    Git repo, so manual transitions on non-git boards stay silent (the
    detach step itself is then a no-op).
    """
    if getattr(args, "worktree", None) is False:
        return None
    project_root = _find_git_root_optional(args.board)
    if project_root is None:
        return None
    from ..worktree import WorktreeManager

    return WorktreeManager.for_project(project_root)


def _resolve_worktree_mgr(args: argparse.Namespace):
    """Resolve the worktree manager from ``--worktree`` tri-state semantics.

    - ``None`` (default): auto — enable when the board is in a Git repo,
      otherwise emit a one-line warning and disable.
    - ``True`` (``--worktree``): hard-require a Git repo, SystemExit if none.
    - ``False`` (``--no-worktree``): always disabled.
    """
    requested = getattr(args, "worktree", None)
    if requested is False:
        return None
    project_root = _find_git_root_optional(args.board)
    if project_root is None:
        if requested is True:
            # Reuse the raising helper to surface the existing error text.
            _find_git_root(args.board)
        print(
            "kanban: worktree isolation disabled (board not in a Git "
            "repo). Pass --no-worktree to silence, or --worktree to "
            "hard-require a repo.",
            file=sys.stderr,
        )
        return None
    from ..worktree import WorktreeManager

    return WorktreeManager.for_project(project_root)


def _make_orchestrator(
    args: argparse.Namespace,
) -> tuple[MarkdownBoardStore, KanbanOrchestrator]:
    store = _make_store(args)
    orchestrator = KanbanOrchestrator(
        store=store,
        executor=_build_executor(args.executor, board=args.board),
        worktree_mgr=_resolve_worktree_mgr(args),
    )
    return store, orchestrator


def _make_worktree_mgr(args: argparse.Namespace):
    from ..worktree import WorktreeManager

    return WorktreeManager.for_project(_find_git_root(args.board))


def _require_writable(args: argparse.Namespace) -> None:
    """Refuse to mutate the board while a live daemon holds the lock.

    Note: ``.daemon.lock`` is held only by ``scheduler``, ``all``, and
    ``legacy-serial`` roles. A ``--role worker`` process takes no board
    lock, so this check alone does NOT guarantee safe mutation in the
    split topology — per-card writers should also call
    :func:`_require_card_writable` to refuse while a live claim exists.
    """
    if getattr(args, "force", False):
        return
    try:
        assert_no_daemon(args.board)
    except DaemonLockError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)


def _require_card_writable(args: argparse.Namespace, card_id: str) -> None:
    """Board lock + per-card live-claim guard (v0.1.2 split topology).

    Workers don't hold ``.daemon.lock``, but they do hold live claims on
    specific cards while executing. Mutating a card with a live claim
    races the worker's next envelope (operator edits can be overwritten
    by a pending result, or the worker can step on the edit). Refuse
    unless ``--force`` is set.
    """
    _require_writable(args)
    if getattr(args, "force", False):
        return
    store = _make_store(args)
    claim = store.get_claim(card_id)
    if claim is None:
        return
    worker_tag = (
        f"worker={claim.worker_id}" if claim.worker_id else "unassigned"
    )
    print(
        f"Card {card_id[:8]} has a live execution claim {claim.claim_id} "
        f"({worker_tag}); refuse to mutate. Run `kanban claims {card_id}` "
        f"and `kanban workers` to check, stop the claimed worker (or wait "
        f"for it to finish), then retry. Pass --force to override (may "
        f"race with in-flight execution).",
        file=sys.stderr,
    )
    raise SystemExit(2)
