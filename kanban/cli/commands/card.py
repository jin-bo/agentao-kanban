"""``kanban card …`` subcommands: add, edit, context, acceptance."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from ...models import (
    CONTEXT_REF_KINDS,
    Card,
    CardPriority,
    CardStatus,
    ContextRef,
)
from ...orchestrator import advance_inbox_dependents
from ..helpers import (
    _OPERATOR_STATUSES,
    _detach_worktree_after_terminal_cli,
    _make_store,
    _project_root_for,
    _require_card_writable,
    _require_writable,
    _resolve_card_id,
)


_ACCEPTANCE_EDIT_BANNER = (
    "# Edit the acceptance criteria for this card, one per line.\n"
    "# Lines starting with '#' and blank lines are ignored.\n"
    "# Save and quit your editor to apply, or quit without saving to abort.\n"
)


def _open_in_editor(initial: str, *, suffix: str = ".txt") -> str | None:
    """Hand a buffer to ``$EDITOR`` and return the saved contents.

    Returns ``None`` if the buffer is unchanged (operator likely closed
    without saving) so callers can treat that as "no-op, abort". Raises
    :class:`SystemExit` with a clear message when ``$EDITOR`` is unset
    or the editor exits non-zero — better to refuse than to silently
    write an empty list.
    """
    import shlex
    import tempfile

    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not editor:
        raise SystemExit(
            "No $EDITOR (or $VISUAL) set. Set one (e.g. `export EDITOR=vi`) "
            "and re-run, or use `kanban card acceptance add/rm/clear`."
        )

    # shlex.split handles `EDITOR="code --wait"` and `EDITOR="vim -n"`,
    # which are common when the editor needs flags to behave like a
    # blocking foreground process.
    editor_argv = shlex.split(editor)
    if not editor_argv:
        raise SystemExit("$EDITOR is empty after parsing; aborting.")

    with tempfile.NamedTemporaryFile(
        mode="w+", suffix=suffix, encoding="utf-8", delete=False
    ) as fh:
        fh.write(initial)
        path = Path(fh.name)
    try:
        try:
            rv = subprocess.run(editor_argv + [str(path)], check=False)
        except OSError as exc:
            # Catches FileNotFoundError when $EDITOR is set but its binary
            # isn't on PATH (e.g. stale `EDITOR=code --wait` without code
            # installed). Without this the user sees a traceback instead
            # of a clean abort message.
            raise SystemExit(
                f"Failed to launch editor {editor_argv[0]!r}: {exc}. "
                f"Set $EDITOR to an installed binary and re-run."
            )
        if rv.returncode != 0:
            raise SystemExit(
                f"Editor exited with rc={rv.returncode}; aborting without changes."
            )
        edited = path.read_text(encoding="utf-8")
    finally:
        try:
            path.unlink()
        except OSError:
            pass
    if edited == initial:
        return None
    return edited


def _parse_acceptance_buffer(text: str) -> list[str]:
    """Strip comments + blanks, return one trimmed item per surviving line."""
    items: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        items.append(line.strip())
    return items


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
        from ...agent_profiles import ProfileConfigError, load_default_config
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


def cmd_card_acceptance_edit(args: argparse.Namespace) -> int:
    store = _make_store(args)
    args.card_id = _resolve_card_id(store, args.card_id)
    _require_card_writable(args, args.card_id)
    try:
        card = store.get_card(args.card_id)
    except KeyError:
        print(f"No card with id {args.card_id}", file=sys.stderr)
        return 1

    initial = _ACCEPTANCE_EDIT_BANNER + "\n".join(card.acceptance_criteria)
    if card.acceptance_criteria:
        initial += "\n"

    # Resolved via ``kanban.cli`` so test monkeypatches on the package
    # namespace reach the call site.
    from kanban import cli as _cli
    edited = _cli._open_in_editor(initial, suffix=".kanban-acceptance.txt")
    if edited is None:
        print("No changes (buffer unchanged); leaving criteria untouched.")
        return 0

    new_items = _parse_acceptance_buffer(edited)
    if new_items == list(card.acceptance_criteria):
        print("No changes (criteria identical after parse).")
        return 0

    store.update_card(card.id, acceptance_criteria=new_items)
    fresh = store.get_card(card.id)
    note = f"Acceptance criteria edited via $EDITOR ({len(new_items)} item(s))"
    fresh.add_history(note, role="system")
    store.update_card(fresh.id)
    store.append_event(fresh.id, note)
    print(note)
    return 0


def register_card_commands(sub) -> None:
    """Wire up ``kanban card …`` parsers onto the top-level subparser."""
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
        help="Operator override; disallowed for doing/review (use requeue instead).",
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

    acc_edit = acc_sub.add_parser(
        "edit",
        help=(
            "Open the criteria in $EDITOR (one per line). Lines starting "
            "with '#' and blank lines are dropped on save."
        ),
    )
    acc_edit.add_argument("card_id")


def dispatch_card(args: argparse.Namespace, parser) -> int:
    """Route ``kanban card <sub>`` invocations to their handlers."""
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
            "edit": cmd_card_acceptance_edit,
        }.get(args.acceptance_command)
        if handler is None:
            parser.error(f"Unknown acceptance subcommand: {args.acceptance_command}")
        return handler(args)
    parser.error(f"Unknown card subcommand: {args.card_command}")
