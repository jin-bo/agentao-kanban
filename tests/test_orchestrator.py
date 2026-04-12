from __future__ import annotations

import pytest

from kanban import (
    CardPriority,
    CardStatus,
    InMemoryBoardStore,
    KanbanOrchestrator,
)
from kanban.executors import MockAgentaoExecutor
from kanban.orchestrator import WipPolicy


def _make(wip: int = 2) -> KanbanOrchestrator:
    store = InMemoryBoardStore()
    return KanbanOrchestrator(
        store=store,
        executor=MockAgentaoExecutor(),
        wip_policy=WipPolicy(doing_limit=wip),
    )


def test_priority_ordering_in_inbox():
    orch = _make()
    low = orch.create_card(title="low", goal="l", priority=CardPriority.LOW)
    high = orch.create_card(title="high", goal="h", priority=CardPriority.CRITICAL)
    picked = orch.tick()
    assert picked is not None
    assert picked.id == high.id
    assert low.status == CardStatus.INBOX


def test_runs_cards_to_done():
    orch = _make()
    a = orch.create_card(title="A", goal="a")
    b = orch.create_card(title="B", goal="b")
    orch.run_until_idle(max_steps=50)
    assert orch.store.get_card(a.id).status == CardStatus.DONE
    assert orch.store.get_card(b.id).status == CardStatus.DONE


def test_dependency_blocks_scheduling():
    orch = _make()
    parent = orch.create_card(title="parent", goal="p")
    child = orch.create_card(title="child", goal="c", depends_on=[parent.id])

    # tick only touches parent until it is DONE
    for _ in range(10):
        picked = orch.tick()
        if picked is None:
            break
        if orch.store.get_card(parent.id).status != CardStatus.DONE:
            assert picked.id == parent.id

    assert orch.store.get_card(parent.id).status == CardStatus.DONE
    # now child becomes actionable
    orch.run_until_idle(max_steps=20)
    assert orch.store.get_card(child.id).status == CardStatus.DONE


def test_blocked_card_is_skipped():
    orch = _make()
    blocked = orch.create_card(title="blocked", goal="b", priority=CardPriority.CRITICAL)
    other = orch.create_card(title="other", goal="o", priority=CardPriority.LOW)

    orch.block(blocked.id, "awaiting input")
    assert orch.store.get_card(blocked.id).status == CardStatus.BLOCKED

    # orchestrator should now work on the lower-priority card, ignoring BLOCKED
    orch.run_until_idle(max_steps=30)
    assert orch.store.get_card(other.id).status == CardStatus.DONE
    assert orch.store.get_card(blocked.id).status == CardStatus.BLOCKED
    assert orch.store.get_card(blocked.id).blocked_reason == "awaiting input"


def test_unblock_returns_card_to_flow():
    orch = _make()
    c = orch.create_card(title="c", goal="g")
    orch.block(c.id, "x")
    orch.unblock(c.id, CardStatus.INBOX)
    assert orch.store.get_card(c.id).blocked_reason is None
    assert orch.store.get_card(c.id).status == CardStatus.INBOX
    orch.run_until_idle(max_steps=20)
    assert orch.store.get_card(c.id).status == CardStatus.DONE


def test_wip_limit_prevents_extra_pulls_from_ready():
    orch = _make(wip=1)
    # Pre-seed two cards already in READY so planning can't run ahead.
    a = orch.create_card(title="A", goal="a")
    b = orch.create_card(title="B", goal="b")
    orch.store.move_card(a.id, CardStatus.READY, "seed")
    orch.store.move_card(b.id, CardStatus.READY, "seed")

    # First tick pulls A into DOING (WIP=1). Next tick must not pull B.
    picked1 = orch.tick()
    assert picked1 is not None and picked1.id == a.id
    assert orch.store.get_card(a.id).status == CardStatus.REVIEW  # after mock worker

    # WIP=1 (A in REVIEW). B in READY must stay queued; orchestrator should
    # pick A (REVIEW) next, not B.
    picked2 = orch.tick()
    assert picked2 is not None and picked2.id == a.id
    assert orch.store.get_card(b.id).status == CardStatus.READY


def test_missing_dependency_id_keeps_card_unscheduled():
    orch = _make()
    orch.create_card(title="orphan", goal="o", depends_on=["does-not-exist"])
    assert orch.tick() is None
