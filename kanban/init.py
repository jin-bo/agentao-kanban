"""``kanban init`` — scaffold a project directory.

Creates a ``.kanban/`` marker plus ``workspace/board/`` and optionally
seeds demo cards or copies sub-agent templates. Idempotent: never
overwrites existing files.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from textwrap import dedent

from .demo import seed_demo_board


DEFAULT_BOARD_REL = Path("workspace/board")
MARKER_DIR = ".kanban"
LOCAL_AGENTS_DIR = Path(".agentao/agents")


def read_board_dir_override(cfg: Path) -> str | None:
    """Pluck a ``board_dir:`` literal out of a one-key config without dragging
    in the full PyYAML loader on every CLI invocation.
    """
    try:
        for raw in cfg.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            key, _, value = line.partition(":")
            if key.strip() != "board_dir":
                continue
            value = value.strip().split("#", 1)[0].strip()
            if (value.startswith('"') and value.endswith('"')) or (
                value.startswith("'") and value.endswith("'")
            ):
                value = value[1:-1]
            return value or None
    except OSError:
        pass
    return None


def _kanban_default_agents_dir() -> Path:
    return Path(__file__).resolve().parent / "defaults"


def write_marker_config(marker: Path, board_rel: str) -> bool:
    cfg = marker / "config.yaml"
    if cfg.exists():
        return False
    cfg.write_text(
        dedent(
            f"""\
            # Written by `kanban init`. Drives project-root discovery for the CLI.
            # `board_dir` is resolved relative to this file's parent and only
            # honored when --board is not passed.
            board_dir: {board_rel}
            """
        ),
        encoding="utf-8",
    )
    return True


def _copy_agent_templates(dest: Path) -> tuple[int, int]:
    """Copy bundled sub-agent definitions; never overwrite. Returns (copied, skipped)."""
    src = _kanban_default_agents_dir()
    copied = 0
    skipped = 0
    dest.mkdir(parents=True, exist_ok=True)
    for path in sorted(src.glob("kanban-*.md")):
        target = dest / path.name
        if target.exists():
            skipped += 1
            continue
        shutil.copy2(path, target)
        copied += 1
    return copied, skipped


def _print_next_steps(*, root: Path, board: Path, demo: bool) -> None:
    rel_board = board.relative_to(root) if board.is_relative_to(root) else board
    lines: list[str] = ["", "Done. Next steps:"]
    if demo:
        lines.append("  uv run kanban list                  # see the seeded demo cards")
        lines.append("  uv run kanban run                   # advance the mock executor to idle")
        lines.append("  uv run kanban web                   # browse the board")
    else:
        lines.append('  uv run kanban card add --title "T" --goal "G"')
        lines.append("  uv run kanban list")
        lines.append("  uv run kanban daemon                # start the dispatcher")
        lines.append("  uv run kanban web                   # browse the board")
    lines.append("")
    lines.append(f"  Board:        {rel_board}")
    lines.append(f"  Project root: {root}")
    print("\n".join(lines))


def cmd_init(args) -> int:
    from .cli import _find_git_root_optional  # noqa: PLC0415 — avoid circular import at module load

    root = Path(args.path or Path.cwd()).resolve()
    root.mkdir(parents=True, exist_ok=True)

    marker = root / MARKER_DIR
    fresh = not marker.exists()
    marker.mkdir(parents=True, exist_ok=True)

    # If a previous init wrote a custom board_dir, honor it on re-run so
    # the demo seed lands where _discover_board() will later look. New
    # inits write the default and use that.
    cfg = marker / "config.yaml"
    existing_board_dir = read_board_dir_override(cfg) if cfg.is_file() else None
    board_rel = Path(existing_board_dir) if existing_board_dir else DEFAULT_BOARD_REL
    board = (root / board_rel).resolve()

    cfg_written = write_marker_config(marker, str(board_rel))
    board.mkdir(parents=True, exist_ok=True)

    # Show paths relative to the project root when possible, fall back
    # to absolute when the board sits outside (e.g. shared scratch dir).
    board_label = (
        board.relative_to(root) if board.is_relative_to(root) else board
    )
    if fresh:
        print(f"Created project marker:   {marker.relative_to(root)}/")
    else:
        print(f"Project marker exists:    {marker.relative_to(root)}/")
    if cfg_written:
        print(f"Wrote                     {marker.relative_to(root)}/config.yaml")
    print(f"Board directory:          {board_label}/")

    if args.copy_agents:
        copied, skipped = _copy_agent_templates(root / LOCAL_AGENTS_DIR)
        print(
            f"Agent templates           "
            f"copied={copied} skipped={skipped} → {LOCAL_AGENTS_DIR}/"
        )

    if _find_git_root_optional(root) is not None:
        print(
            "Git repository detected — `kanban daemon` / `run` will use "
            "per-card worktree isolation by default."
        )
    else:
        print(
            "Not in a Git repository — worktree isolation is OFF by default. "
            "Run `git init` first if you want it."
        )

    if args.demo:
        from .store_markdown import MarkdownBoardStore

        store = MarkdownBoardStore(board)
        result = seed_demo_board(store)
        print(
            "Seeded demo cards         "
            f"created={result.created} skipped={result.skipped}"
        )

    _print_next_steps(root=root, board=board, demo=args.demo)
    return 0


def add_init_subparser(sub) -> None:
    p = sub.add_parser(
        "init",
        help="Scaffold a kanban project (.kanban/, workspace/board, optional agents).",
        description=(
            "Create a project marker and an empty board. Re-running is safe "
            "and never overwrites existing files."
        ),
    )
    p.add_argument(
        "path",
        nargs="?",
        default=None,
        help="Project root to initialize (default: current directory).",
    )
    p.add_argument(
        "--copy-agents",
        action="store_true",
        dest="copy_agents",
        help=(
            "Copy bundled sub-agent definitions into .agentao/agents/ so "
            "you can edit prompts locally without touching the package."
        ),
    )
    p.add_argument(
        "--demo",
        action="store_true",
        help="Seed example cards for a quick walkthrough.",
    )
