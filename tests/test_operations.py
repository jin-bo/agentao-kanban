"""Unit tests for ``kanban.operations`` — the shared transition functions.

These pin the contract the CLI / MCP / Web write paths all rely on:
single-write commit point, best-effort post-commit side effects surfaced
as warnings (never rolled back), and input validation before any write.
"""

from __future__ import annotations

import pytest

from kanban.models import AgentRole, Card, CardStatus
from kanban.operations import (
    OperationError,
    transition_block,
    transition_move,
    transition_requeue,
    transition_unblock,
)
from kanban.store import InMemoryBoardStore
from kanban.worktree.types import DetachResult


def _store_with_card(**kw) -> tuple[InMemoryBoardStore, Card]:
    store = InMemoryBoardStore()
    card = store.add_card(Card(title="t", goal="g", **kw))
    return store, card


class _FakeWorktreeMgr:
    """Minimal stand-in for ``WorktreeManager`` used by detach tests."""

    def __init__(
        self, *, raise_exc: bool = False, artifacts_path=None, removed: bool = True
    ):
        self.calls: list[str] = []
        self._raise = raise_exc
        self._artifacts_path = artifacts_path
        self._removed = removed

    def detach(self, card_id: str) -> DetachResult:
        self.calls.append(card_id)
        if self._raise:
            raise RuntimeError("detach blew up")
        return DetachResult(removed=self._removed, artifacts_path=self._artifacts_path)


def test_transition_move_changes_status() -> None:
    store, card = _store_with_card()
    result = transition_move(store, None, card.id, "ready", note="m")
    assert result.card.status == CardStatus.READY
    assert result.warnings == []
    assert store.get_card(card.id).status == CardStatus.READY


def test_transition_block_sets_reason_in_one_write() -> None:
    store, card = _store_with_card()
    result = transition_block(store, None, card.id, "  waiting on dep  ")
    assert result.card.status == CardStatus.BLOCKED
    assert result.card.blocked_reason == "waiting on dep"
    assert result.card.blocked_at is not None


def test_transition_unblock_clears_reason() -> None:
    store, card = _store_with_card()
    transition_block(store, None, card.id, "stuck")
    result = transition_unblock(store, None, card.id, "ready")
    assert result.card.status == CardStatus.READY
    assert result.card.blocked_reason is None
    assert result.card.blocked_at is None


def test_transition_requeue_clears_reason_and_owner() -> None:
    store, card = _store_with_card(owner_role=AgentRole.WORKER)
    transition_block(store, None, card.id, "stuck")
    result = transition_requeue(store, card.id, "ready", "after fix")
    assert result.card.status == CardStatus.READY
    assert result.card.blocked_reason is None
    assert result.card.owner_role is None
    assert any("after fix" in h for h in result.card.history)
    assert result.warnings == []


def test_done_landing_advances_inbox_dependents() -> None:
    store = InMemoryBoardStore()
    parent = store.add_card(Card(title="p", goal="g", status=CardStatus.REVIEW))
    child = store.add_card(
        Card(title="c", goal="g", status=CardStatus.INBOX, depends_on=[parent.id])
    )
    transition_move(store, None, parent.id, "done", note="done")
    assert store.get_card(child.id).status == CardStatus.READY


def test_terminal_landing_detaches_worktree_and_logs_artifacts() -> None:
    store, card = _store_with_card(status=CardStatus.DOING)
    store.update_card(card.id, worktree_branch="kanban/abc")
    mgr = _FakeWorktreeMgr(artifacts_path="/tmp/raw/abc/artifacts-1")
    result = transition_block(store, mgr, card.id, "done with errors")
    assert mgr.calls == [card.id]
    assert result.warnings == []
    types = {e.event_type for e in store.list_events() if e.event_type}
    assert "worktree.artifacts_saved" in types
    assert "worktree.detached" in types


def test_kept_worktree_is_surfaced_as_a_warning() -> None:
    # detach_worktree_on_terminal keeps the worktree (and logs
    # worktree.detach_failed) without raising when auto-commit fails;
    # the transition still commits but reports a warning.
    store, card = _store_with_card(status=CardStatus.DOING)
    store.update_card(card.id, worktree_branch="kanban/abc")
    mgr = _FakeWorktreeMgr(removed=False)
    result = transition_move(store, mgr, card.id, "done", note="done")
    assert store.get_card(card.id).status == CardStatus.DONE
    assert result.warnings and "kept attached" in result.warnings[0]
    assert any(e.event_type == "worktree.detach_failed" for e in store.list_events())


def test_requeue_never_touches_worktree() -> None:
    # transition_requeue takes no worktree_mgr — its targets are non-terminal.
    store, card = _store_with_card(status=CardStatus.DOING)
    store.update_card(card.id, worktree_branch="kanban/abc")
    result = transition_requeue(store, card.id, "ready")
    assert result.card.status == CardStatus.READY
    assert result.card.worktree_branch == "kanban/abc"


def test_bad_status_rejected_before_any_write() -> None:
    store, card = _store_with_card()
    before = len(store.list_events())
    with pytest.raises(OperationError):
        transition_move(store, None, card.id, "bogus")
    assert store.get_card(card.id).status == CardStatus.INBOX
    assert len(store.list_events()) == before


def test_blank_block_reason_rejected_before_any_write() -> None:
    store, card = _store_with_card()
    before = len(store.list_events())
    with pytest.raises(OperationError):
        transition_block(store, None, card.id, "   ")
    assert store.get_card(card.id).status == CardStatus.INBOX
    assert len(store.list_events()) == before


def test_requeue_rejects_terminal_target() -> None:
    store, card = _store_with_card()
    with pytest.raises(OperationError):
        transition_requeue(store, card.id, "done")
    with pytest.raises(OperationError):
        transition_unblock(store, None, card.id, "definitely-not-a-status")


def test_side_effect_failure_commits_transition_and_warns() -> None:
    store, card = _store_with_card(status=CardStatus.DOING)
    store.update_card(card.id, worktree_branch="kanban/abc")
    mgr = _FakeWorktreeMgr(raise_exc=True)
    result = transition_move(store, mgr, card.id, "done", note="done")
    # The transition committed despite the detach failure.
    assert store.get_card(card.id).status == CardStatus.DONE
    assert result.card.status == CardStatus.DONE
    assert result.warnings and "detaching worktree" in result.warnings[0]
    # A recovery-style event was recorded for the failed side effect.
    assert any(e.event_type == "worktree.detach_failed" for e in store.list_events())
