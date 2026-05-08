"""Board integrity checks for `kanban doctor`.

Each check returns zero or more `CheckResult` records with a stable
`rule` id and a `severity`. Exit codes are decided at the CLI layer:
0 = clean, 1 = warnings only, 2 = at least one error.

Two check families:

- card-level checks operate on a :class:`MarkdownBoardStore` and detect
  issues inside the board (missing deps, blocked-without-reason, ...).
- environment checks operate on a project root + board path. They may
  carry a ``fix`` callable that ``kanban doctor --fix`` invokes.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .daemon import _pid_alive, lock_path, read_lock
from .init import DEFAULT_BOARD_REL, MARKER_DIR, read_board_dir_override, write_marker_config
from .models import CardStatus, CONTEXT_REF_KINDS
from .store_markdown import MarkdownBoardStore


# Severity values are part of the JSON schema (`doctor --json`), so they
# must stay literal strings. Constants here just guard against typos at
# the call sites that yield them.
ERROR = "error"
WARNING = "warning"


@dataclass(slots=True)
class CheckResult:
    severity: str  # "error" | "warning"
    rule: str
    card_id: str
    message: str
    # Optional remediation. When set, ``kanban doctor --fix`` invokes
    # the callable and prints the returned description. Card-level
    # checks leave this as ``None`` since recovery requires operator
    # judgement; environment checks set it for stale-lock / mkdir-style
    # repairs that are mechanical and idempotent.
    fix: Callable[[], str] | None = None


@dataclass(slots=True)
class DoctorReport:
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(c.severity == ERROR for c in self.checks)

    @property
    def has_warnings(self) -> bool:
        return any(c.severity == WARNING for c in self.checks)

    def exit_code(self) -> int:
        if self.has_errors:
            return 2
        if self.has_warnings:
            return 1
        return 0


# --- checks ---


def _check_deps(store: MarkdownBoardStore) -> Iterable[CheckResult]:
    known = {c.id for c in store.list_cards()}
    for card in store.list_cards():
        for dep in card.depends_on:
            if dep not in known:
                yield CheckResult(
                    ERROR,
                    "dep-missing",
                    card.id,
                    f"depends_on references unknown card {dep}",
                )


def _check_blocked_has_reason(store: MarkdownBoardStore) -> Iterable[CheckResult]:
    for card in store.list_cards():
        if card.status == CardStatus.BLOCKED and not card.blocked_reason:
            yield CheckResult(
                WARNING,
                "blocked-no-reason",
                card.id,
                "blocked card has no blocked_reason set",
            )


def _verification_is_empty(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (dict, list)):
        return len(value) == 0
    return False


def _check_done_has_verification(store: MarkdownBoardStore) -> Iterable[CheckResult]:
    for card in store.list_cards():
        if card.status != CardStatus.DONE:
            continue
        if _verification_is_empty(card.outputs.get("verification")):
            yield CheckResult(
                WARNING,
                "done-no-verification",
                card.id,
                "done card has empty outputs.verification",
            )


_STAGE_REQUIRES = {
    CardStatus.REVIEW: ("implementation",),
}


def _check_stage_has_upstream(store: MarkdownBoardStore) -> Iterable[CheckResult]:
    for card in store.list_cards():
        required = _STAGE_REQUIRES.get(card.status)
        if not required:
            continue
        for key in required:
            if _verification_is_empty(card.outputs.get(key)):
                yield CheckResult(
                    ERROR,
                    "stage-missing-upstream",
                    card.id,
                    f"status={card.status.value} but outputs.{key} is missing",
                )


def _check_context_ref_kinds(store: MarkdownBoardStore) -> Iterable[CheckResult]:
    for card in store.list_cards():
        for ref in card.context_refs:
            if ref.kind not in CONTEXT_REF_KINDS:
                yield CheckResult(
                    WARNING,
                    "invalid-context-kind",
                    card.id,
                    f"context_ref {ref.path!r} has kind={ref.kind!r}",
                )


def _check_unparseable(store: MarkdownBoardStore) -> Iterable[CheckResult]:
    for name in store.unparseable_cards():
        yield CheckResult(
            ERROR,
            "unparseable-card",
            name,  # we don't have a card id, use the filename
            f"card file {name} could not be parsed",
        )


_CHECKS = (
    _check_unparseable,
    _check_deps,
    _check_blocked_has_reason,
    _check_done_has_verification,
    _check_stage_has_upstream,
    _check_context_ref_kinds,
)


def run(store: MarkdownBoardStore) -> DoctorReport:
    report = DoctorReport()
    for check in _CHECKS:
        report.checks.extend(check(store))
    return report


# --- environment checks ---


def _remove_path(path: Path, label: str) -> str:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        return f"failed to remove {label} {path}: {exc}"
    return f"removed {label} {path}"


def _check_marker_config(root: Path) -> Iterable[CheckResult]:
    marker = root / MARKER_DIR
    if not marker.is_dir():
        return
    cfg = marker / "config.yaml"

    def fix_write_default() -> str:
        # `write_marker_config` is a no-op when the file already exists,
        # so unlink first to make `--fix` repair a malformed file too.
        cfg.unlink(missing_ok=True)
        write_marker_config(marker, str(DEFAULT_BOARD_REL))
        return f"wrote default {cfg}"

    if not cfg.is_file():
        yield CheckResult(
            WARNING,
            "cwd-marker-no-config",
            "",
            f"`.kanban/` marker at {marker} has no config.yaml",
            fix=fix_write_default,
        )
        return
    if read_board_dir_override(cfg) is None:
        yield CheckResult(
            WARNING,
            "cwd-config-no-board-dir",
            "",
            f"{cfg} is missing a parseable `board_dir:` entry",
            fix=fix_write_default,
        )


def _check_board_dir(board: Path) -> Iterable[CheckResult]:
    if board.is_dir():
        return
    if board.exists():
        # `--board` points at a regular file or a non-dir symlink;
        # auto-recovery would clobber whatever's there, so no fix.
        yield CheckResult(
            ERROR,
            "cwd-board-not-a-dir",
            "",
            f"board path exists but is not a directory: {board}",
        )
        return

    def fix_mkdir() -> str:
        board.mkdir(parents=True, exist_ok=True)
        return f"created board directory {board}"

    yield CheckResult(
        ERROR,
        "cwd-board-missing",
        "",
        f"board directory does not exist: {board}",
        fix=fix_mkdir,
    )


def _check_daemon_lock(board: Path) -> Iterable[CheckResult]:
    # An *alive* pid is left alone — that path requires operator judgement
    # (`kanban daemon stop`) so the daemon can unwind cleanly.
    path = lock_path(board)
    if not path.is_file():
        return
    data = read_lock(board)

    rule, message = _classify_daemon_lock(path, data)
    if rule is None:
        return
    label = "malformed lock" if rule == "cwd-malformed-lock" else "stale lock"
    yield CheckResult(
        WARNING,
        rule,
        "",
        message,
        fix=lambda: _remove_path(path, label),
    )


def _classify_daemon_lock(
    path: Path, data: dict | None
) -> tuple[str | None, str]:
    """Return (rule, message) for a lock file, or (None, "") if it's healthy."""
    if data is None:
        return "cwd-malformed-lock", f"daemon lock at {path} is unparseable"
    raw_pid = data.get("pid", 0)
    try:
        pid = int(raw_pid)
    except (TypeError, ValueError):
        return (
            "cwd-malformed-lock",
            f"daemon lock at {path} has non-numeric pid {raw_pid!r}",
        )
    if pid <= 0 or not _pid_alive(pid):
        return (
            "cwd-stale-lock",
            f"daemon lock at {path} points at dead pid {pid}",
        )
    return None, ""


def run_environment(root: Path, board: Path) -> list[CheckResult]:
    """Diagnose project configuration around ``root`` and ``board``.

    ``root`` and ``board`` are passed separately because
    ``--board /elsewhere`` legitimately decouples them.
    """
    results: list[CheckResult] = list(_check_marker_config(root))
    results.extend(_check_board_dir(board))
    if board.is_dir():
        results.extend(_check_daemon_lock(board))
    return results
