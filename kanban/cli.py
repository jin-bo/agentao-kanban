from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .daemon import (
    CombinedDaemon,
    DaemonConfig,
    DaemonLockError,
    KanbanDaemon,
    SchedulerDaemon,
    WorkerDaemon,
    assert_no_daemon,
    daemon_lock,
    detach_to_background,
)
import json as _json

import yaml

from .executors import CardExecutor, MockAgentaoExecutor
from .models import (
    CONTEXT_REF_KINDS,
    AgentRole,
    Card,
    CardEvent,
    CardPriority,
    CardStatus,
    ContextRef,
)
from .orchestrator import KanbanOrchestrator
from .store_markdown import MarkdownBoardStore

DEFAULT_BOARD = Path("workspace/board")


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

# Statuses an operator may force via `card edit --set-status`. doing/review/verify
# are excluded because they have an expected owner_role and would desync the
# orchestrator — use `requeue` for recovery paths.
_OPERATOR_STATUSES = (
    CardStatus.INBOX,
    CardStatus.READY,
    CardStatus.BLOCKED,
    CardStatus.DONE,
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kanban", description="Kanban board CLI")
    p.add_argument(
        "--board",
        type=Path,
        default=DEFAULT_BOARD,
        help=f"Board directory (default: {DEFAULT_BOARD})",
    )
    p.add_argument(
        "--executor",
        choices=["mock", "agentao", "multi-backend"],
        default="mock",
        help=(
            "Executor backend (default: mock). `agentao` uses the legacy "
            "role-keyed subagent executor; `multi-backend` uses the "
            "profile-aware executor that honors card.agent_profile and ACP "
            "backends. Both require the agentao package."
        ),
    )
    p.add_argument(
        "--worktree",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Per-card Git worktree isolation. Default: auto — on when the "
            "board is inside a Git repo, off otherwise (with a one-line "
            "warning to stderr). Pass --worktree to hard-require a repo "
            "(exits if none), or --no-worktree to disable."
        ),
    )
    sub = p.add_subparsers(dest="command", required=True)

    card = sub.add_parser("card", help="Card operations")
    card_sub = card.add_subparsers(dest="card_command", required=True)

    add = card_sub.add_parser("add", help="Create a new card")
    add.add_argument("--title", required=True)
    add.add_argument("--goal", required=True)
    add.add_argument(
        "--priority",
        choices=[p.name for p in CardPriority],
        default=CardPriority.MEDIUM.name,
    )
    add.add_argument("--acceptance", action="append", default=[], help="Acceptance criterion (repeatable)")
    add.add_argument("--depends", action="append", default=[], help="Card id this card depends on (repeatable)")

    edit = card_sub.add_parser("edit", help="Edit an existing card")
    edit.add_argument("card_id")
    edit.add_argument("--title")
    edit.add_argument("--goal")
    edit.add_argument(
        "--priority",
        choices=[p.name for p in CardPriority],
        help="New priority",
    )
    edit.add_argument(
        "--set-status",
        dest="set_status",
        choices=[s.value for s in _OPERATOR_STATUSES],
        help="Operator override; disallowed for doing/review/verify (use requeue instead).",
    )
    blocked_group = edit.add_mutually_exclusive_group()
    blocked_group.add_argument(
        "--blocked-reason",
        dest="blocked_reason",
        help="Set or update the blocked_reason field.",
    )
    blocked_group.add_argument(
        "--clear-blocked-reason",
        dest="clear_blocked_reason",
        action="store_true",
        help="Clear blocked_reason.",
    )

    profile_group = edit.add_mutually_exclusive_group()
    profile_group.add_argument(
        "--agent-profile",
        dest="agent_profile",
        help="Pin the card to a named agent profile (validated against agent_profiles.yaml).",
    )
    profile_group.add_argument(
        "--clear-agent-profile",
        dest="clear_agent_profile",
        action="store_true",
        help="Clear agent_profile and agent_profile_source.",
    )

    context = card_sub.add_parser("context", help="Manage card context_refs")
    context_sub = context.add_subparsers(dest="context_command", required=True)

    ctx_list = context_sub.add_parser("list", help="List context refs on a card")
    ctx_list.add_argument("card_id")

    ctx_add = context_sub.add_parser("add", help="Add or upsert a context ref by path")
    ctx_add.add_argument("card_id")
    ctx_add.add_argument("--path", required=True)
    ctx_add.add_argument(
        "--kind", choices=list(CONTEXT_REF_KINDS), default="optional"
    )
    ctx_add.add_argument("--note", default="")

    ctx_rm = context_sub.add_parser("rm", help="Remove a context ref by path")
    ctx_rm.add_argument("card_id")
    ctx_rm.add_argument("--path", required=True)

    acc = card_sub.add_parser("acceptance", help="Manage acceptance_criteria")
    acc_sub = acc.add_subparsers(dest="acceptance_command", required=True)

    acc_list = acc_sub.add_parser("list", help="List acceptance criteria")
    acc_list.add_argument("card_id")

    acc_add = acc_sub.add_parser("add", help="Append an acceptance criterion")
    acc_add.add_argument("card_id")
    acc_add.add_argument("--item", required=True)

    acc_rm = acc_sub.add_parser("rm", help="Remove a criterion by 1-based index")
    acc_rm.add_argument("card_id")
    acc_rm.add_argument("--index", type=int, required=True)

    acc_clear = acc_sub.add_parser("clear", help="Clear all criteria")
    acc_clear.add_argument("card_id")

    sub.add_parser("list", help="List cards grouped by status")

    show = sub.add_parser("show", help="Show a single card")
    show.add_argument("card_id")
    show.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit a single-line JSON object (for scripting).",
    )

    move = sub.add_parser("move", help="Move a card to a status")
    move.add_argument("card_id")
    move.add_argument("status", choices=[s.value for s in CardStatus])

    block = sub.add_parser("block", help="Move a card to BLOCKED with a reason")
    block.add_argument("card_id")
    block.add_argument("reason")

    unblock = sub.add_parser("unblock", help="Move a blocked card back (default: inbox)")
    unblock.add_argument("card_id")
    unblock.add_argument(
        "--to",
        dest="target",
        choices=[s.value for s in CardStatus],
        default=CardStatus.INBOX.value,
    )

    doctor = sub.add_parser("doctor", help="Run board integrity checks")
    doctor.add_argument("--json", dest="as_json", action="store_true", help="Emit machine-readable records")

    traces = sub.add_parser("traces", help="List retained raw agent transcripts")
    traces.add_argument("card_id")
    traces.add_argument(
        "--role",
        choices=[r.value for r in AgentRole],
        help="Only transcripts from this role",
    )
    traces.add_argument("--latest", action="store_true", help="Only the most recent transcript")

    events = sub.add_parser("events", help="Inspect events.log")
    events.add_argument("card_id", nargs="?", help="Filter to one card")
    events.add_argument(
        "--role",
        choices=[r.value for r in AgentRole],
        help="Filter to execution events for this role (hides plain events)",
    )
    events.add_argument(
        "--limit",
        type=_non_negative_int,
        default=50,
        help="Show the last N events (default 50; 0 = none)",
    )
    events.add_argument("--json", dest="as_json", action="store_true", help="Emit one JSON record per line")

    requeue = sub.add_parser("requeue", help="Return a (usually blocked) card back to flow")
    requeue.add_argument("card_id")
    requeue.add_argument(
        "--to",
        dest="target",
        choices=["inbox", "ready"],
        default="inbox",
        help="Target status (default: inbox)",
    )
    requeue.add_argument("--note", default="", help="Recovery note appended to history")

    claims = sub.add_parser("claims", help="List active execution claims (v0.1.2 runtime)")
    claims.add_argument("card_id", nargs="?", help="Filter to one card")
    claims.add_argument("--json", dest="as_json", action="store_true")

    workers = sub.add_parser(
        "workers", help="List live worker presences (v0.1.2 runtime)"
    )
    workers.add_argument("--json", dest="as_json", action="store_true")

    recover = sub.add_parser(
        "recover", help="Run one-shot runtime recovery (v0.1.2)"
    )
    recover.add_argument(
        "--stale",
        action="store_true",
        help="Recover stale claims (lease expired). Required for now.",
    )
    recover.add_argument("--json", dest="as_json", action="store_true")

    profiles = sub.add_parser("profiles", help="Inspect agent profile routing config")
    profiles_sub = profiles.add_subparsers(dest="profiles_command", required=True)
    profiles_sub.add_parser("list", help="List configured agent profiles")
    p_show = profiles_sub.add_parser("show", help="Show one profile's resolved configuration")
    p_show.add_argument("name")

    sub.add_parser("tick", help="Run a single orchestrator step")
    run = sub.add_parser("run", help="Run orchestrator until idle")
    run.add_argument("--max-steps", type=int, default=100)

    daemon = sub.add_parser("daemon", help="Run the dispatcher loop (foreground by default)")
    daemon.add_argument("--detach", action="store_true", help="Fork into the background")
    daemon.add_argument("--once", action="store_true", help="Run a single tick and exit")
    daemon.add_argument(
        "--poll-interval", type=float, default=2.0, help="Idle sleep in seconds (default 2.0)"
    )
    daemon.add_argument("--verbose", action="store_true", help="Enable DEBUG logging")
    daemon.add_argument(
        "--role",
        choices=["all", "scheduler", "worker", "legacy-serial"],
        default="all",
        help=(
            "Daemon role (default: all = scheduler+worker in one process). "
            "`scheduler` creates claims and holds the board lock; `worker` "
            "executes claimed cards and takes no board lock. `legacy-serial` "
            "runs the pre-v0.1.2 tick path."
        ),
    )
    daemon.add_argument(
        "--worker-id",
        dest="worker_id",
        help="Stable worker identifier for `--role worker` (default: random).",
    )
    daemon.add_argument(
        "--max-claims",
        dest="max_claims",
        type=int,
        default=2,
        help="Scheduler concurrency budget (default 2).",
    )

    p.add_argument(
        "--force",
        action="store_true",
        help="Mutate the board even if a daemon holds the lock (for recovery only).",
    )

    # --- web subcommand (read-only HTTP board) ---
    web_p = sub.add_parser(
        "web",
        help="Run the read-only web board (no writes; polls the board dir).",
    )
    web_p.add_argument("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1)")
    web_p.add_argument("--port", type=int, default=8000, help="Bind port (default 8000)")
    web_p.add_argument(
        "--poll-interval-ms",
        type=int,
        default=5000,
        dest="poll_interval_ms",
        help="Frontend poll interval in ms (default 5000)",
    )

    # --- worktree subcommands ---
    wt = sub.add_parser("worktree", help="Manage Git worktrees for cards")
    wt_sub = wt.add_subparsers(dest="worktree_command", required=True)
    wt_sub.add_parser("list", help="List active worktrees")
    wt_prune = wt_sub.add_parser("prune", help="Clean up stale worktree branches")
    wt_prune.add_argument("--retention-days", type=int, default=7)
    wt_diff = wt_sub.add_parser("diff", help="Show diff for a card's worktree")
    wt_diff.add_argument("card_id")

    return p


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


def _find_git_root_optional(board: Path) -> Path | None:
    """Return the Git toplevel for a board path, or None if none exists.

    Uses ``git rev-parse --show-toplevel`` which works for regular repos,
    linked worktrees (``.git`` is a file), and boards nested inside repos.
    When the board path does not yet exist, walk up to the first existing
    ancestor so we bind to the correct repo even on fresh boards. Returns
    None (rather than raising) when no existing ancestor or no repo can
    be found, or when the ``git`` binary itself is unavailable — callers
    decide whether that's a hard failure.
    """
    import subprocess

    try:
        start = board.resolve(strict=False)
    except OSError:
        start = board
    probe = start
    while not probe.exists():
        parent = probe.parent
        if parent == probe:
            return None
        probe = parent
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=probe,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip())


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
            from .executors.agentao_multi import AgentaoMultiAgentExecutor
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
            from .agent_profiles import ProfileConfigError, load_default_config
            from .executors.backends.acp_backend import AcpBackend
            from .executors.backends.subagent_backend import SubagentBackend
            from .executors.multi_backend import MultiBackendExecutor
            from .executors.router_policy import RouterPolicy
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
    from .orchestrator import detach_worktree_on_terminal
    from .worktree import WorktreeManager

    wt_mgr = WorktreeManager(
        project_root=project_root,
        worktrees_root=project_root / "workspace" / "worktrees",
    )
    detach_worktree_on_terminal(store, wt_mgr, card_id, card.status)


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
    from .worktree import WorktreeManager

    return WorktreeManager(
        project_root=project_root,
        worktrees_root=project_root / "workspace" / "worktrees",
    )


def _make_orchestrator(args: argparse.Namespace) -> tuple[MarkdownBoardStore, KanbanOrchestrator]:
    store = _make_store(args)
    orchestrator = KanbanOrchestrator(
        store=store,
        executor=_build_executor(args.executor, board=args.board),
        worktree_mgr=_resolve_worktree_mgr(args),
    )
    return store, orchestrator


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


def _iso_z(dt) -> str:
    """Render a datetime as ISO-Z (matches ``_format_event_line``)."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


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


class _BlockDumper(yaml.SafeDumper):
    """SafeDumper that renders multi-line strings as ``|`` block scalars.

    Default PyYAML renders ``"line1\\nline2"`` as a quoted single-line
    string with embedded escapes, which is unreadable when the card
    carries agent transcripts in ``outputs``. Subclassing (instead of
    mutating ``yaml.SafeDumper`` globally) keeps the custom representer
    scoped to this CLI.
    """


def _yaml_str_representer(dumper: yaml.SafeDumper, data: str):
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_BlockDumper.add_representer(str, _yaml_str_representer)


def _render_card(card: Card, *, as_json: bool) -> str:
    mapping = _card_to_mapping(card)
    if as_json:
        return _json.dumps(mapping, ensure_ascii=False)
    return yaml.dump(
        mapping,
        Dumper=_BlockDumper,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=100,
    )


def cmd_card_edit(args: argparse.Namespace) -> int:
    store = _make_store(args)
    args.card_id = _resolve_card_id(store, args.card_id)
    _require_card_writable(args, args.card_id)
    try:
        card = store.get_card(args.card_id)
    except KeyError:
        print(f"No card with id {args.card_id}", file=sys.stderr)
        return 1

    new_status: CardStatus | None = None
    if args.set_status is not None:
        new_status = CardStatus(args.set_status)
        if new_status == CardStatus.BLOCKED and not args.blocked_reason:
            print(
                "--set-status blocked requires --blocked-reason in the same call.",
                file=sys.stderr,
            )
            return 2

    # --blocked-reason is only valid when the card actually is (or in this
    # same call becomes) BLOCKED. Writing a live reason on a non-blocked
    # card leaves contradictory state: `show` reports a block reason while
    # the dispatcher keeps processing the card.
    if args.blocked_reason is not None:
        effective_status = new_status if new_status is not None else card.status
        if effective_status != CardStatus.BLOCKED:
            print(
                "--blocked-reason is only valid when the card is or is being moved to blocked "
                f"(current={card.status.value}"
                + (f", --set-status {new_status.value}" if new_status is not None else "")
                + ").",
                file=sys.stderr,
            )
            return 2

    scalar_updates: dict[str, object] = {}
    if args.title is not None:
        scalar_updates["title"] = args.title
    if args.goal is not None:
        scalar_updates["goal"] = args.goal
    if args.priority is not None:
        scalar_updates["priority"] = CardPriority[args.priority]

    blocked_changed = False
    if args.blocked_reason is not None:
        scalar_updates["blocked_reason"] = args.blocked_reason
        blocked_changed = True
    elif args.clear_blocked_reason:
        scalar_updates["blocked_reason"] = None
        blocked_changed = True

    profile_changed = False
    if getattr(args, "agent_profile", None) is not None:
        from .agent_profiles import ProfileConfigError, load_default_config
        try:
            load_default_config(
                base=_project_root_for(args.board)
            ).get_profile(args.agent_profile)
        except ProfileConfigError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        scalar_updates["agent_profile"] = args.agent_profile
        scalar_updates["agent_profile_source"] = "manual"
        profile_changed = True
    elif getattr(args, "clear_agent_profile", False):
        scalar_updates["agent_profile"] = None
        scalar_updates["agent_profile_source"] = None
        profile_changed = True

    if not scalar_updates and new_status is None:
        print("Nothing to edit. Pass at least one flag.", file=sys.stderr)
        return 2

    if scalar_updates:
        store.update_card(card.id, **scalar_updates)
        fresh = store.get_card(card.id)
        notes: list[str] = []
        if args.title or args.goal or args.priority:
            notes.append("Manual edit via CLI")
        if blocked_changed:
            notes.append(
                "Blocked reason cleared via CLI"
                if args.clear_blocked_reason
                else "Blocked reason updated via CLI"
            )
        if profile_changed:
            notes.append(
                "Agent profile cleared via CLI"
                if getattr(args, "clear_agent_profile", False)
                else f"Agent profile set to {args.agent_profile!r} via CLI"
            )
        for note in notes:
            fresh.add_history(note, role="system")
            store.append_event(fresh.id, note)
        if notes and new_status is None:
            # Flush the in-memory history mutation to disk. The move_card
            # path below would do it, but we may not be taking it.
            store.update_card(fresh.id)

    if new_status is not None:
        # --set-status always resets owner_role; operator-forced statuses
        # never carry an implicit agent expectation. It also clears any
        # stale blocked_reason when moving AWAY from BLOCKED — an operator
        # forcing the card back into flow has no business leaving the old
        # block note behind (which would make `show` contradict itself).
        forced_updates: dict[str, object] = {"owner_role": None}
        current = store.get_card(card.id)
        if (
            new_status != CardStatus.BLOCKED
            and current.blocked_reason is not None
            and not blocked_changed
        ):
            forced_updates["blocked_reason"] = None
        store.update_card(card.id, **forced_updates)
        previous_status = card.status
        store.move_card(
            card.id,
            new_status,
            f"Status manually set to {new_status.value} via CLI",
        )
        if new_status == CardStatus.DONE and previous_status != CardStatus.DONE:
            from .orchestrator import advance_inbox_dependents

            advance_inbox_dependents(store, card.id)
        _detach_worktree_after_terminal_cli(args, store, card.id)

    print(f"Edited {card.id}")
    return 0


def cmd_card_context_list(args: argparse.Namespace) -> int:
    store = _make_store(args)
    args.card_id = _resolve_card_id(store, args.card_id)
    try:
        card = store.get_card(args.card_id)
    except KeyError:
        print(f"No card with id {args.card_id}", file=sys.stderr)
        return 1
    if not card.context_refs:
        print("(no context refs)")
        return 0
    for ref in card.context_refs:
        suffix = f"  — {ref.note}" if ref.note else ""
        print(f"[{ref.kind}] {ref.path}{suffix}")
    return 0


def cmd_card_context_add(args: argparse.Namespace) -> int:
    store = _make_store(args)
    args.card_id = _resolve_card_id(store, args.card_id)
    _require_card_writable(args, args.card_id)
    try:
        card = store.get_card(args.card_id)
    except KeyError:
        print(f"No card with id {args.card_id}", file=sys.stderr)
        return 1
    try:
        new_ref = ContextRef.coerce({"path": args.path, "kind": args.kind, "note": args.note})
    except (KeyError, TypeError, ValueError) as exc:
        print(f"Invalid context ref: {exc}", file=sys.stderr)
        return 2

    refs = list(card.context_refs)
    existing_idx = next(
        (i for i, r in enumerate(refs) if r.path == new_ref.path), None
    )
    if existing_idx is not None:
        refs[existing_idx] = new_ref
        note = f"Context updated: {new_ref.path} [{new_ref.kind}]"
    else:
        refs.append(new_ref)
        note = f"Context added: {new_ref.path} [{new_ref.kind}]"

    store.update_card(card.id, context_refs=refs)
    fresh = store.get_card(card.id)
    fresh.add_history(note, role="system")
    store.update_card(fresh.id)
    store.append_event(fresh.id, note)
    print(note)
    return 0


def cmd_card_context_rm(args: argparse.Namespace) -> int:
    store = _make_store(args)
    args.card_id = _resolve_card_id(store, args.card_id)
    _require_card_writable(args, args.card_id)
    try:
        card = store.get_card(args.card_id)
    except KeyError:
        print(f"No card with id {args.card_id}", file=sys.stderr)
        return 1

    refs = [r for r in card.context_refs if r.path != args.path]
    if len(refs) == len(card.context_refs):
        print(f"No context ref with path {args.path}", file=sys.stderr)
        return 1

    store.update_card(card.id, context_refs=refs)
    fresh = store.get_card(card.id)
    note = f"Context removed: {args.path}"
    fresh.add_history(note, role="system")
    store.update_card(fresh.id)
    store.append_event(fresh.id, note)
    print(note)
    return 0


def cmd_card_acceptance_list(args: argparse.Namespace) -> int:
    store = _make_store(args)
    args.card_id = _resolve_card_id(store, args.card_id)
    try:
        card = store.get_card(args.card_id)
    except KeyError:
        print(f"No card with id {args.card_id}", file=sys.stderr)
        return 1
    if not card.acceptance_criteria:
        print("(no acceptance criteria)")
        return 0
    for i, item in enumerate(card.acceptance_criteria, start=1):
        print(f"{i}. {item}")
    return 0


def cmd_card_acceptance_add(args: argparse.Namespace) -> int:
    store = _make_store(args)
    args.card_id = _resolve_card_id(store, args.card_id)
    _require_card_writable(args, args.card_id)
    try:
        card = store.get_card(args.card_id)
    except KeyError:
        print(f"No card with id {args.card_id}", file=sys.stderr)
        return 1
    criteria = list(card.acceptance_criteria) + [args.item]
    store.update_card(card.id, acceptance_criteria=criteria)
    fresh = store.get_card(card.id)
    note = f"Acceptance criterion added: {args.item}"
    fresh.add_history(note, role="system")
    store.update_card(fresh.id)
    store.append_event(fresh.id, note)
    print(note)
    return 0


def cmd_card_acceptance_rm(args: argparse.Namespace) -> int:
    store = _make_store(args)
    args.card_id = _resolve_card_id(store, args.card_id)
    _require_card_writable(args, args.card_id)
    try:
        card = store.get_card(args.card_id)
    except KeyError:
        print(f"No card with id {args.card_id}", file=sys.stderr)
        return 1
    idx = args.index
    if idx < 1 or idx > len(card.acceptance_criteria):
        print(
            f"Invalid index {idx}; card has {len(card.acceptance_criteria)} criteria.",
            file=sys.stderr,
        )
        return 2
    criteria = list(card.acceptance_criteria)
    removed = criteria.pop(idx - 1)
    store.update_card(card.id, acceptance_criteria=criteria)
    fresh = store.get_card(card.id)
    note = f"Acceptance criterion removed at index {idx}"
    fresh.add_history(note, role="system")
    store.update_card(fresh.id)
    store.append_event(fresh.id, note)
    print(f"{note}: {removed}")
    return 0


def cmd_card_acceptance_clear(args: argparse.Namespace) -> int:
    store = _make_store(args)
    args.card_id = _resolve_card_id(store, args.card_id)
    _require_card_writable(args, args.card_id)
    try:
        card = store.get_card(args.card_id)
    except KeyError:
        print(f"No card with id {args.card_id}", file=sys.stderr)
        return 1
    if not card.acceptance_criteria:
        print("(already empty)")
        return 0
    store.update_card(card.id, acceptance_criteria=[])
    fresh = store.get_card(card.id)
    note = "Acceptance criteria cleared via CLI"
    fresh.add_history(note, role="system")
    store.update_card(fresh.id)
    store.append_event(fresh.id, note)
    print("Cleared acceptance criteria")
    return 0


def cmd_card_add(args: argparse.Namespace) -> int:
    _require_writable(args)
    store = _make_store(args)
    depends_on = [_resolve_card_id(store, dep) for dep in args.depends]
    card = store.add_card(
        Card(
            title=args.title,
            goal=args.goal,
            priority=CardPriority[args.priority],
            acceptance_criteria=list(args.acceptance),
            depends_on=depends_on,
        )
    )
    print(f"Created card {card.id}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    store = _make_store(args)
    snapshot = store.board_snapshot()
    if not snapshot:
        print("(empty board)")
        return 0
    for status in CardStatus:
        titles = snapshot.get(status.value, [])
        if not titles:
            continue
        print(f"{status.value}:")
        for card in store.list_by_status(status):
            print(f"  - {card.id[:8]}  {card.title}  (priority={card.priority.name})")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    store = _make_store(args)
    args.card_id = _resolve_card_id(store, args.card_id)
    try:
        card = store.get_card(args.card_id)
    except KeyError:
        print(f"No card with id {args.card_id}", file=sys.stderr)
        return 1
    rendered = _render_card(card, as_json=getattr(args, "as_json", False))
    # yaml.safe_dump already ends with "\n"; json.dumps does not. Use
    # print's implicit newline for JSON, raw write for YAML so we don't
    # emit a blank trailing line.
    if getattr(args, "as_json", False):
        print(rendered)
    else:
        sys.stdout.write(rendered)
    return 0


def cmd_move(args: argparse.Namespace) -> int:
    store = _make_store(args)
    args.card_id = _resolve_card_id(store, args.card_id)
    _require_card_writable(args, args.card_id)
    try:
        previous_status = store.get_card(args.card_id).status
        card = store.move_card(args.card_id, CardStatus(args.status), "Manual move via CLI")
    except KeyError:
        print(f"No card with id {args.card_id}", file=sys.stderr)
        return 1
    if card.status == CardStatus.DONE and previous_status != CardStatus.DONE:
        from .orchestrator import advance_inbox_dependents

        advance_inbox_dependents(store, card.id)
    _detach_worktree_after_terminal_cli(args, store, card.id)
    print(f"Moved {card.id} to {card.status.value}")
    return 0


def cmd_block(args: argparse.Namespace) -> int:
    store = _make_store(args)
    args.card_id = _resolve_card_id(store, args.card_id)
    _require_card_writable(args, args.card_id)
    try:
        store.update_card(args.card_id, blocked_reason=args.reason)
        card = store.move_card(args.card_id, CardStatus.BLOCKED, f"Blocked: {args.reason}")
    except KeyError:
        print(f"No card with id {args.card_id}", file=sys.stderr)
        return 1
    _detach_worktree_after_terminal_cli(args, store, card.id)
    print(f"Blocked {card.id}: {args.reason}")
    return 0


def cmd_unblock(args: argparse.Namespace) -> int:
    store = _make_store(args)
    args.card_id = _resolve_card_id(store, args.card_id)
    _require_card_writable(args, args.card_id)
    try:
        target = CardStatus(args.target)
        previous_status = store.get_card(args.card_id).status
        store.update_card(args.card_id, blocked_reason=None)
        card = store.move_card(args.card_id, target, f"Unblocked to {target.value}")
    except KeyError:
        print(f"No card with id {args.card_id}", file=sys.stderr)
        return 1
    if card.status == CardStatus.DONE and previous_status != CardStatus.DONE:
        from .orchestrator import advance_inbox_dependents

        advance_inbox_dependents(store, card.id)
    # Unblocking to DONE is the only terminal target here; INBOX/READY/etc.
    # leave the worktree attached so the next worker dispatch can resume.
    _detach_worktree_after_terminal_cli(args, store, card.id)
    print(f"Unblocked {card.id} to {card.status.value}")
    return 0


def _event_to_json(e: CardEvent) -> dict[str, object]:
    record: dict[str, object] = {
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


def cmd_events(args: argparse.Namespace) -> int:
    store = _make_store(args)
    if args.card_id is not None:
        args.card_id = _resolve_card_id(store, args.card_id)
    if args.role is not None:
        role = AgentRole(args.role)
        records = store.list_execution_events(
            card_id=args.card_id, role=role, limit=args.limit
        )
    elif args.card_id is not None:
        records = list(store.events_for_card(args.card_id))
        records = _apply_limit(records, args.limit)
    else:
        records = store.list_events(limit=args.limit)

    if args.as_json:
        for e in records:
            print(_json.dumps(_event_to_json(e), ensure_ascii=False))
    else:
        if not records:
            print("(no events)")
            return 0
        for e in records:
            print(_format_event_line(e))
    return 0


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


def cmd_claims(args: argparse.Namespace) -> int:
    store = _make_store(args)
    if args.card_id is not None:
        args.card_id = _resolve_card_id(store, args.card_id)
    from datetime import datetime, timezone as _tz

    now = datetime.now(_tz.utc)
    claims = store.list_claims()
    if args.card_id is not None:
        claims = [c for c in claims if c.card_id == args.card_id]
    claims.sort(key=lambda c: (c.claimed_at, c.card_id))

    if args.as_json:
        payload = [
            {
                "card_id": c.card_id,
                "claim_id": c.claim_id,
                "role": c.role.value,
                "status_at_claim": c.status_at_claim.value,
                "worker_id": c.worker_id,
                "attempt": c.attempt,
                "retry_count": c.retry_count,
                "retry_of_claim_id": c.retry_of_claim_id,
                "claimed_at": c.claimed_at.isoformat(),
                "heartbeat_at": c.heartbeat_at.isoformat(),
                "lease_expires_at": c.lease_expires_at.isoformat(),
                "timeout_s": c.timeout_s,
                "heartbeat_age_s": (now - c.heartbeat_at).total_seconds(),
                "lease_remaining_s": (c.lease_expires_at - now).total_seconds(),
                "expired": c.is_expired(now=now),
            }
            for c in claims
        ]
        print(_json.dumps(payload, ensure_ascii=False))
        return 0

    if not claims:
        print("(no active claims)")
        return 0
    print(f"{'card':10}  {'role':8}  {'attempt':>7}  {'worker':14}  {'hb_age':>8}  {'lease_rem':>10}  claim_id")
    for c in claims:
        hb_age = _format_age((now - c.heartbeat_at).total_seconds())
        remaining = _format_age((c.lease_expires_at - now).total_seconds())
        expired_tag = " *EXPIRED*" if c.is_expired(now=now) else ""
        print(
            f"{c.card_id[:8]:10}  {c.role.value:8}  {c.attempt:>7}  "
            f"{(c.worker_id or '-')[:14]:14}  {hb_age:>8}  {remaining:>10}  "
            f"{c.claim_id}{expired_tag}"
        )
    return 0


def cmd_workers(args: argparse.Namespace) -> int:
    store = _make_store(args)
    from datetime import datetime, timezone as _tz

    now = datetime.now(_tz.utc)
    workers = store.list_workers()
    workers.sort(key=lambda w: w.started_at)

    if args.as_json:
        payload = [
            {
                "worker_id": w.worker_id,
                "pid": w.pid,
                "host": w.host,
                "started_at": w.started_at.isoformat(),
                "heartbeat_at": w.heartbeat_at.isoformat(),
                "heartbeat_age_s": (now - w.heartbeat_at).total_seconds(),
            }
            for w in workers
        ]
        print(_json.dumps(payload, ensure_ascii=False))
        return 0

    if not workers:
        print("(no live workers)")
        return 0
    print(f"{'worker_id':24}  {'pid':>7}  {'uptime':>8}  {'hb_age':>8}  host")
    for w in workers:
        uptime = _format_age((now - w.started_at).total_seconds())
        hb_age = _format_age((now - w.heartbeat_at).total_seconds())
        print(
            f"{w.worker_id[:24]:24}  {w.pid:>7}  {uptime:>8}  {hb_age:>8}  "
            f"{w.host or '-'}"
        )
    return 0


def cmd_recover(args: argparse.Namespace) -> int:
    if not args.stale:
        print(
            "recover requires --stale (only stale-claim recovery is implemented).",
            file=sys.stderr,
        )
        return 2
    _require_writable(args)
    store = _make_store(args)
    # Capture the list *before* recovery so we can report per-card outcomes.
    stale_before = store.list_stale_claims()
    orchestrator = KanbanOrchestrator(store=store, executor=MockAgentaoExecutor())
    count = orchestrator.recover_stale_claims()

    if args.as_json:
        payload = {
            "recovered": count,
            "cards": [
                {
                    "card_id": c.card_id,
                    "claim_id": c.claim_id,
                    "role": c.role.value,
                    "attempt": c.attempt,
                    "retry_count": c.retry_count,
                }
                for c in stale_before
            ],
        }
        print(_json.dumps(payload, ensure_ascii=False))
        return 0

    for c in stale_before:
        fresh = store.get_card(c.card_id)
        disposition = "retried" if fresh.status != CardStatus.BLOCKED else "blocked"
        print(
            f"{c.card_id[:8]}  [{c.role.value}]  attempt={c.attempt}  "
            f"retry_count={c.retry_count}  → {disposition}"
        )
    print(f"recovered {count} stale claim(s).")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from . import doctor as _doctor

    store = _make_store(args)
    report = _doctor.run(store)

    if args.as_json:
        payload = {
            "checks": [
                {
                    "severity": c.severity,
                    "rule": c.rule,
                    "card_id": c.card_id,
                    "message": c.message,
                }
                for c in report.checks
            ]
        }
        print(_json.dumps(payload, ensure_ascii=False))
    else:
        if not report.checks:
            print("Board is healthy.")
        else:
            for c in report.checks:
                print(f"[{c.severity}] {c.rule}  {c.card_id[:8]}  {c.message}")
    return report.exit_code()


def cmd_traces(args: argparse.Namespace) -> int:
    store = _make_store(args)
    args.card_id = _resolve_card_id(store, args.card_id)
    try:
        store.get_card(args.card_id)
    except KeyError:
        print(f"No card with id {args.card_id}", file=sys.stderr)
        return 1
    role = AgentRole(args.role) if args.role else None
    traces = store.list_traces(args.card_id, role=role, latest=args.latest)
    if not traces:
        print(f"no traces retained for {args.card_id}")
        return 0
    for t in traces:
        stamp = t.at.strftime("%Y-%m-%dT%H:%M:%SZ")
        print(f"{stamp}  [{t.role.value}]  {t.size:>8}  {t.path}")
    return 0


def cmd_requeue(args: argparse.Namespace) -> int:
    store = _make_store(args)
    args.card_id = _resolve_card_id(store, args.card_id)
    _require_card_writable(args, args.card_id)
    try:
        card = store.get_card(args.card_id)
    except KeyError:
        print(f"No card with id {args.card_id}", file=sys.stderr)
        return 1

    previous_status = card.status
    target = CardStatus(args.target)

    # Clear blocked_reason and reset owner_role — both target statuses
    # (INBOX, READY) expect no pending owner.
    store.update_card(card.id, blocked_reason=None, owner_role=None)

    suffix = f": {args.note}" if args.note else ""
    history_note = (
        f"Requeued from {previous_status.value} to {target.value}{suffix}"
    )
    store.move_card(card.id, target, history_note)
    print(history_note)
    return 0


def cmd_profiles_list(args: argparse.Namespace) -> int:
    from .agent_profiles import ProfileConfigError, load_default_config
    try:
        cfg = load_default_config(base=_project_root_for(args.board))
    except ProfileConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    defaults = {rc.default_profile: role.value for role, rc in cfg.roles.items()}
    width = max((len(n) for n in cfg.profiles), default=4)
    header = f"{'PROFILE':<{width}}  ROLE       BACKEND  TARGET"
    print(header)
    for name, profile in sorted(cfg.profiles.items()):
        default_tag = f"  (default for {defaults[name]})" if name in defaults else ""
        print(
            f"{name:<{width}}  {profile.role.value:<9}  "
            f"{profile.backend.type:<7}  {profile.backend.target}{default_tag}"
        )
    return 0


def cmd_profiles_show(args: argparse.Namespace) -> int:
    from .agent_profiles import ProfileConfigError, load_default_config
    try:
        cfg = load_default_config(base=_project_root_for(args.board))
        profile = cfg.get_profile(args.name)
    except ProfileConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(f"name:        {profile.name}")
    print(f"role:        {profile.role.value}")
    print(f"backend:     {profile.backend.type} -> {profile.backend.target}")
    print(f"fallback:    {profile.fallback or '-'}")
    if profile.capabilities:
        print(f"capabilities: {', '.join(profile.capabilities)}")
    if profile.description:
        print(f"description: {profile.description}")
    chain = cfg.fallback_chain(profile.name)
    if len(chain) > 1:
        print(f"chain:       {' -> '.join(chain)}")
    return 0


def cmd_tick(args: argparse.Namespace) -> int:
    _require_writable(args)
    _, orchestrator = _make_orchestrator(args)
    card = orchestrator.tick()
    if card is None:
        print("Board is idle.")
    else:
        print(f"Processed {card.id[:8]}: now {card.status.value}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    _require_writable(args)
    _, orchestrator = _make_orchestrator(args)
    processed = orchestrator.run_until_idle(max_steps=args.max_steps)
    print(f"Processed {len(processed)} step(s); board idle.")
    return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.detach and args.once:
        print("--detach and --once are mutually exclusive", file=sys.stderr)
        return 2

    board_dir = args.board
    role = args.role
    # `--detach` MUST fork before any scheduler/worker thread is created.
    # CombinedDaemon spawns threads inside run(); forking after threads
    # exist would leave orphan threads in the parent and break signal
    # handling in the child.
    if args.detach:
        detach_to_background(board_dir)

    def _build_config() -> DaemonConfig:
        cfg_kwargs: dict[str, object] = {
            "poll_interval": args.poll_interval,
            "max_idle_cycles": 1 if args.once else None,
            "max_claims": args.max_claims,
        }
        if args.worker_id:
            cfg_kwargs["worker_id"] = args.worker_id
        return DaemonConfig(**cfg_kwargs)

    # Workers do not hold the board lock — only scheduler/legacy/all do.
    needs_board_lock = role in ("scheduler", "legacy-serial", "all")

    def _run_daemon(lock_file: Path | None = None) -> int:
        _, orchestrator = _make_orchestrator(args)
        config = _build_config()
        if role == "scheduler":
            daemon = SchedulerDaemon(orchestrator, config=config)
        elif role == "worker":
            daemon = WorkerDaemon(orchestrator, config=config)
        elif role == "legacy-serial":
            daemon = KanbanDaemon(orchestrator, config=config)
        else:  # all — real N-way parallel; workers need their own orchestrators
            def _fresh_orchestrator() -> KanbanOrchestrator:
                _, o = _make_orchestrator(args)
                return o

            daemon = CombinedDaemon(
                orchestrator,
                config=config,
                orchestrator_factory=_fresh_orchestrator,
            )
        if lock_file is not None:
            daemon.add_force_exit_cleanup(
                lambda p=lock_file: p.unlink(missing_ok=True)
            )
        # Signal handlers live ONLY on the main thread. CombinedDaemon
        # relies on its shared stop event to propagate into child threads;
        # no sub-daemon calls signal.signal(...) itself.
        daemon.install_signal_handlers()
        if args.once:
            did = daemon.run_once()
            if not did:
                print("Board is idle.")
            return 0
        return daemon.run()

    try:
        if needs_board_lock:
            with daemon_lock(board_dir) as lock_file:
                return _run_daemon(lock_file)
        return _run_daemon()
    except DaemonLockError as exc:
        print(str(exc), file=sys.stderr)
        return 2


def _make_worktree_mgr(args: argparse.Namespace):
    from .worktree import WorktreeManager

    project_root = _find_git_root(args.board)
    return WorktreeManager(
        project_root=project_root,
        worktrees_root=project_root / "workspace" / "worktrees",
    )


def cmd_worktree_list(args: argparse.Namespace) -> int:
    mgr = _make_worktree_mgr(args)
    active = mgr.list_active()
    if not active:
        print("No active worktrees.")
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
    from .worktree import WorktreeDiffError

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


def cmd_web(args: argparse.Namespace) -> int:
    # Read-only endpoint: no ``_require_writable`` check so the daemon can
    # hold ``.daemon.lock`` while the UI observes it. The server mounts a
    # fresh store per request, which picks up daemon/CLI writes on the
    # next poll.
    from .web import main as web_main

    return web_main(
        args.board,
        host=args.host,
        port=args.port,
        poll_interval_ms=args.poll_interval_ms,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "card":
        if args.card_command == "add":
            return cmd_card_add(args)
        if args.card_command == "edit":
            return cmd_card_edit(args)
        if args.card_command == "context":
            if args.context_command == "list":
                return cmd_card_context_list(args)
            if args.context_command == "add":
                return cmd_card_context_add(args)
            if args.context_command == "rm":
                return cmd_card_context_rm(args)
            parser.error(f"Unknown context subcommand: {args.context_command}")
        if args.card_command == "acceptance":
            handler = {
                "list": cmd_card_acceptance_list,
                "add": cmd_card_acceptance_add,
                "rm": cmd_card_acceptance_rm,
                "clear": cmd_card_acceptance_clear,
            }.get(args.acceptance_command)
            if handler is None:
                parser.error(f"Unknown acceptance subcommand: {args.acceptance_command}")
            return handler(args)
        parser.error(f"Unknown card subcommand: {args.card_command}")
    if args.command == "profiles":
        if args.profiles_command == "list":
            return cmd_profiles_list(args)
        if args.profiles_command == "show":
            return cmd_profiles_show(args)
        parser.error(f"Unknown profiles subcommand: {args.profiles_command}")
    if args.command == "worktree":
        handlers = {
            "list": cmd_worktree_list,
            "prune": cmd_worktree_prune,
            "diff": cmd_worktree_diff,
        }
        return handlers[args.worktree_command](args)
    dispatch = {
        "list": cmd_list,
        "show": cmd_show,
        "move": cmd_move,
        "block": cmd_block,
        "unblock": cmd_unblock,
        "requeue": cmd_requeue,
        "events": cmd_events,
        "traces": cmd_traces,
        "doctor": cmd_doctor,
        "claims": cmd_claims,
        "workers": cmd_workers,
        "recover": cmd_recover,
        "tick": cmd_tick,
        "run": cmd_run,
        "daemon": cmd_daemon,
        "web": cmd_web,
    }
    return dispatch[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
