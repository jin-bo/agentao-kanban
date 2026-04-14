"""Board integrity checks for `kanban doctor`.

Each check returns zero or more `CheckResult` records with a stable
`rule` id and a `severity`. Exit codes are decided at the CLI layer:
0 = clean, 1 = warnings only, 2 = at least one error.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .models import CardStatus, CONTEXT_REF_KINDS
from .store_markdown import MarkdownBoardStore


@dataclass(slots=True)
class CheckResult:
    severity: str  # "error" | "warning"
    rule: str
    card_id: str
    message: str


@dataclass(slots=True)
class DoctorReport:
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(c.severity == "error" for c in self.checks)

    @property
    def has_warnings(self) -> bool:
        return any(c.severity == "warning" for c in self.checks)

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
                    "error",
                    "dep-missing",
                    card.id,
                    f"depends_on references unknown card {dep}",
                )


def _check_blocked_has_reason(store: MarkdownBoardStore) -> Iterable[CheckResult]:
    for card in store.list_cards():
        if card.status == CardStatus.BLOCKED and not card.blocked_reason:
            yield CheckResult(
                "warning",
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
                "warning",
                "done-no-verification",
                card.id,
                "done card has empty outputs.verification",
            )


_STAGE_REQUIRES = {
    CardStatus.REVIEW: ("implementation",),
    CardStatus.VERIFY: ("implementation", "review"),
}


def _check_stage_has_upstream(store: MarkdownBoardStore) -> Iterable[CheckResult]:
    for card in store.list_cards():
        required = _STAGE_REQUIRES.get(card.status)
        if not required:
            continue
        for key in required:
            if _verification_is_empty(card.outputs.get(key)):
                yield CheckResult(
                    "error",
                    "stage-missing-upstream",
                    card.id,
                    f"status={card.status.value} but outputs.{key} is missing",
                )


def _check_context_ref_kinds(store: MarkdownBoardStore) -> Iterable[CheckResult]:
    for card in store.list_cards():
        for ref in card.context_refs:
            if ref.kind not in CONTEXT_REF_KINDS:
                yield CheckResult(
                    "warning",
                    "invalid-context-kind",
                    card.id,
                    f"context_ref {ref.path!r} has kind={ref.kind!r}",
                )


def _check_unparseable(store: MarkdownBoardStore) -> Iterable[CheckResult]:
    for name in store.unparseable_cards():
        yield CheckResult(
            "error",
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
