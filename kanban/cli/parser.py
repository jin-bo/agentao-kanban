"""Top-level argparse builder.

Each command group registers its own subparser via a ``register_*``
function in the matching ``cli.commands.<group>`` module, so adding a
new command is one new file plus one ``register_…`` call here.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .commands.board import register_board_commands
from .commands.card import register_card_commands
from .commands.daemon import register_daemon_commands
from .commands.misc import (
    register_demo_command,
    register_doctor_command,
    register_mcp_command,
    register_web_command,
)
from .commands.profiles import register_profiles_commands
from .commands.runtime import register_runtime_commands
from .commands.worktree import register_worktree_commands


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kanban", description="Kanban board CLI")
    p.add_argument(
        "--board",
        type=Path,
        default=None,
        help=(
            "Board directory. Default: walk up from cwd looking for a "
            "`.kanban/` marker (created by `kanban init`); without a marker "
            "fall back to `./workspace/board`."
        ),
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
    p.add_argument(
        "--force",
        action="store_true",
        help="Mutate the board even if a daemon holds the lock (for recovery only).",
    )

    sub = p.add_subparsers(dest="command", required=False)

    # `init` lives in kanban.init so the parser stays the single source of
    # truth for help text. `demo` is a first-run command from misc.
    from ..init import add_init_subparser
    add_init_subparser(sub)

    register_demo_command(sub)
    register_card_commands(sub)
    register_board_commands(sub)
    register_doctor_command(sub)
    register_runtime_commands(sub)
    register_profiles_commands(sub)
    register_daemon_commands(sub)
    register_mcp_command(sub)
    register_web_command(sub)
    register_worktree_commands(sub)

    return p
