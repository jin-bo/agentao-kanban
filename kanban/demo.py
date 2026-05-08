from __future__ import annotations

from dataclasses import dataclass

from .executors import MockAgentaoExecutor
from .models import Card, CardPriority
from .orchestrator import KanbanOrchestrator
from .store import BoardStore, InMemoryBoardStore


DEMO_CARDS: tuple[dict, ...] = (
    {
        "title": "Read the docs and run the demo",
        "goal": "Skim README quickstart and confirm the local CLI works.",
        "priority": CardPriority.LOW,
        "acceptance_criteria": [
            "`uv run kanban list` prints the seeded cards",
            "`uv run kanban web` opens the board in a browser",
        ],
    },
    {
        "title": "Add per-card retry budget metric",
        "goal": "Surface the retry counter in events.log so verifiers can see backoff.",
        "priority": CardPriority.MEDIUM,
        "acceptance_criteria": [
            "events.log shows attempt= field on retry events",
            "`kanban events --execution-only` includes attempt= in plain output",
        ],
    },
    {
        "title": "Ship a richer doctor check",
        "goal": "Detect orphan worktrees and offer a one-line cleanup hint.",
        "priority": CardPriority.HIGH,
        "acceptance_criteria": [
            "doctor exit code is non-zero when orphan worktrees exist",
            "stderr names the cards involved",
        ],
    },
    {
        "title": "Investigate flaky verifier on macOS",
        "goal": "Determine whether the macOS-only failures are a Python or pytest issue.",
        "priority": CardPriority.MEDIUM,
        "acceptance_criteria": [
            "Reproduction recorded in workspace/scratch/<card-id>/repro.md",
        ],
    },
)


@dataclass(frozen=True)
class DemoSeedResult:
    created: int
    skipped: int


# Frozen lookup of (title, goal) tuples used by `kanban demo` to decide whether
# an already-populated board is the demo set or real work that should not be
# overwritten. Lifted to module scope so callers don't rebuild it per call.
DEMO_CARD_SIGNATURES: frozenset[tuple[str, str]] = frozenset(
    (c["title"], c["goal"]) for c in DEMO_CARDS
)


def is_demo_only(cards) -> bool:
    """True if every card matches a demo card by both title and goal."""
    return all((c.title, c.goal) in DEMO_CARD_SIGNATURES for c in cards)


def seed_demo_board(store: BoardStore) -> DemoSeedResult:
    """Populate ``store`` with the demo backlog if it's empty.

    No-op on a non-empty board: dedup-by-title would force operators to
    remember which titles came from the demo set, and overwriting card
    content would silently destroy edits.
    """
    if store.list_cards():
        return DemoSeedResult(created=0, skipped=len(DEMO_CARDS))
    for spec in DEMO_CARDS:
        store.add_card(
            Card(
                title=spec["title"],
                goal=spec["goal"],
                priority=spec["priority"],
                acceptance_criteria=list(spec.get("acceptance_criteria", [])),
            )
        )
    return DemoSeedResult(created=len(DEMO_CARDS), skipped=0)


def run_demo() -> None:
    """In-memory walkthrough used by ``main.py`` and the README quickstart."""
    store = InMemoryBoardStore()
    orchestrator = KanbanOrchestrator(store=store, executor=MockAgentaoExecutor())

    orchestrator.create_card(
        title="Bootstrap Kanban skeleton",
        goal="Create a runnable multi-agent kanban skeleton that can later call Agentao.",
        priority=CardPriority.HIGH,
    )
    orchestrator.create_card(
        title="Add a second sample card",
        goal="Show that multiple cards can move through the board.",
        priority=CardPriority.MEDIUM,
        acceptance_criteria=["Reach done state"],
    )

    orchestrator.run_until_idle()

    print("Final cards:")
    for card in store.list_cards():
        print(f"- {card.title}: {card.status.value}")
        for item in card.history:
            print(f"  history: {item}")

    print("\nBoard snapshot:")
    snapshot = store.board_snapshot()
    for status, titles in sorted(snapshot.items()):
        joined = ", ".join(titles)
        print(f"- {status}: {joined}")
