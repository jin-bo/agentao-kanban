"""``kanban profiles list / show`` — inspect the routing config."""

from __future__ import annotations

import argparse
import sys

from ..helpers import _project_root_for


def cmd_profiles_list(args: argparse.Namespace) -> int:
    from ...agent_profiles import ProfileConfigError, load_default_config
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
    from ...agent_profiles import ProfileConfigError, load_default_config
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


def register_profiles_commands(sub) -> None:
    profiles = sub.add_parser("profiles", help="Inspect agent profile routing config")
    profiles_sub = profiles.add_subparsers(dest="profiles_command", required=True)
    profiles_sub.add_parser("list", help="List configured agent profiles")
    p_show = profiles_sub.add_parser("show", help="Show one profile's resolved configuration")
    p_show.add_argument("name")


def dispatch_profiles(args: argparse.Namespace, parser) -> int:
    if args.profiles_command == "list":
        return cmd_profiles_list(args)
    if args.profiles_command == "show":
        return cmd_profiles_show(args)
    parser.error(f"Unknown profiles subcommand: {args.profiles_command}")
