"""``kanban`` CLI package.

- ``parser.py`` — top-level argparse, composed from per-module ``register_*``.
- ``helpers.py`` — board/store/orchestrator factories, writability guards,
  project-root + git-root discovery.
- ``rendering.py`` — YAML/JSON dumpers, event formatters, result summary.
- ``commands/<group>.py`` — one module per command group.

To add a new subcommand: create ``commands/<group>.py``, expose a
``register_<group>_commands(sub)`` and any ``cmd_*`` handlers, then wire
both into ``parser.py`` and ``main()``.
"""

from __future__ import annotations

# os, subprocess are exposed at the package level so test fixtures that do
# ``monkeypatch.setattr(kanban.cli.subprocess, "run", ...)`` hit the same
# module objects the submodules use.
import os  # noqa: F401
import subprocess  # noqa: F401
from pathlib import Path

from .helpers import (  # noqa: F401
    _OPERATOR_STATUSES,
    _agents_dir_for,
    _apply_limit,
    _build_executor,
    _detach_worktree_after_terminal_cli,
    _discover_board,
    _find_git_root,
    _find_git_root_optional,
    _make_orchestrator,
    _make_store,
    _make_worktree_mgr,
    _non_negative_int,
    _project_root_for,
    _project_root_or_cwd,
    _require_card_writable,
    _require_writable,
    _resolve_card_id,
    _resolve_worktree_mgr,
)
from .rendering import (  # noqa: F401
    _BlockDumper,
    _card_to_mapping,
    _context_ref_to_mapping,
    _event_to_json,
    _format_age,
    _format_event_line,
    _format_result_block,
    _iso_z,
    _list_artifact_dirs,
    _render_card,
    _revision_to_mapping,
    _show_extras,
    _summarize_card_result,
    _worktree_state,
    _yaml_str_representer,
)
from .commands.card import (  # noqa: F401
    _ACCEPTANCE_EDIT_BANNER,
    _open_in_editor,
    _parse_acceptance_buffer,
    cmd_card_acceptance_add,
    cmd_card_acceptance_clear,
    cmd_card_acceptance_edit,
    cmd_card_acceptance_list,
    cmd_card_acceptance_rm,
    cmd_card_add,
    cmd_card_context_add,
    cmd_card_context_list,
    cmd_card_context_rm,
    cmd_card_edit,
    dispatch_card,
)
from .commands.board import (  # noqa: F401
    cmd_block,
    cmd_events,
    cmd_list,
    cmd_move,
    cmd_result,
    cmd_show,
    cmd_unblock,
)
from .commands.runtime import (  # noqa: F401
    cmd_claims,
    cmd_recover,
    cmd_requeue,
    cmd_run,
    cmd_tick,
    cmd_traces,
    cmd_workers,
)
from .commands.daemon import (  # noqa: F401
    _force_remove_lock,
    _looks_like_kanban_daemon,
    _pid_command,
    cmd_daemon,
    cmd_daemon_logs,
    cmd_daemon_status,
    cmd_daemon_stop,
)
from .commands.worktree import (  # noqa: F401
    cmd_worktree_diff,
    cmd_worktree_list,
    cmd_worktree_prune,
    dispatch_worktree,
)
from .commands.profiles import (  # noqa: F401
    cmd_profiles_list,
    cmd_profiles_show,
    dispatch_profiles,
)
from .commands.misc import (  # noqa: F401
    _doctor_project_root,
    _mcp_install_args,
    cmd_demo,
    cmd_doctor,
    cmd_mcp_install,
    cmd_web,
)
from .parser import build_parser  # noqa: F401


def _print_banner(board: Path) -> None:
    from .. import __version__ as kanban_version
    from ..daemon import daemon_status

    status = daemon_status(board)
    daemon_line = status.get("status", "unknown")
    if status.get("pid"):
        daemon_line = f"{daemon_line} (pid {status['pid']})"

    print(f"kanban v{kanban_version}")
    print(f"  Board:  {board}")
    print(f"  Daemon: {daemon_line}")
    print()
    print("Most-used commands:")
    print('  kanban init                    scaffold a new project')
    print('  kanban demo                    seed example cards + run them')
    print('  kanban card add --title T --goal G')
    print('  kanban list                    show cards by status')
    print('  kanban daemon                  start the dispatcher')
    print('  kanban web                     open the read-only board')
    print()
    print("Full help: kanban --help")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()

    # argcomplete no-ops unless a shell completion handshake variable is
    # set; install via `eval "$(register-python-argcomplete kanban)"`.
    try:
        import argcomplete
        argcomplete.autocomplete(parser)
    except ImportError:
        pass

    args = parser.parse_args(argv)

    # Resolve the default --board against the real cwd at call time so
    # test fixtures with per-call chdir pick up the right marker.
    if args.board is None:
        args.board = _discover_board()

    # No subcommand → friendly banner instead of argparse usage error.
    if args.command is None:
        _print_banner(args.board)
        return 0

    if args.command == "init":
        from ..init import cmd_init
        return cmd_init(args)
    if args.command == "demo":
        return cmd_demo(args)
    if args.command == "mcp":
        if args.mcp_command == "install":
            return cmd_mcp_install(args)
        parser.error(f"Unknown mcp subcommand: {args.mcp_command}")

    if args.command == "card":
        return dispatch_card(args, parser)
    if args.command == "profiles":
        return dispatch_profiles(args, parser)
    if args.command == "worktree":
        return dispatch_worktree(args)

    dispatch = {
        "list": cmd_list,
        "show": cmd_show,
        "result": cmd_result,
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
