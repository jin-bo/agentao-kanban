from __future__ import annotations

from .executors import MockAgentaoExecutor
from .models import CardPriority
from .orchestrator import KanbanOrchestrator
from .store import InMemoryBoardStore


def run_demo() -> None:
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
