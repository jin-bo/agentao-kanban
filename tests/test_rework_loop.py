"""Integration tests for the reviewer/verifier rework loop.

Contract under test:

- Reviewer/verifier may attach a ``revision_request`` to an ``ok=False``
  result. The orchestrator routes this to ``_apply_rework`` instead of
  BLOCKING the card.
- Each accepted rework appends to ``card.revision_requests`` (cumulative),
  bumps ``card.rework_iteration``, moves the card REVIEW/VERIFY → READY,
  and keeps the worktree attached.
- After ``RetryPolicy.rework`` accepted reworks, the next rework ask
  BLOCKs the card (worktree detached, normal terminal path).
- Reviewer/verifier results without a ``revision_request`` retain the
  prior v0.1.3 behavior: card goes straight to BLOCKED.
"""

from __future__ import annotations

import subprocess
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from kanban import CardStatus, KanbanOrchestrator, MarkdownBoardStore
from kanban.executors import MockAgentaoExecutor
from kanban.models import (
    AgentResult,
    AgentRole,
    Card,
    RetryPolicy,
    RevisionRequest,
)
from kanban.worktree import WorktreeManager


def _init_repo(path: Path) -> Path:
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=path, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=path, check=True, capture_output=True,
    )
    (path / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=path, check=True, capture_output=True,
    )
    return path


class ScriptedRework(MockAgentaoExecutor):
    """Inject a rework decision queue for reviewer/verifier.

    Each queued entry for a role is consumed on the next call:

    - ``("rework", summary, hints, failing)`` — return a rework result
    - ``("block", reason)`` — return a plain terminal BLOCKED result
    - ``"approve"`` / missing — fall through to the mock's approve path

    Worker/planner stay on the default mock behavior.
    """

    def __init__(self) -> None:
        super().__init__()
        self.queue: dict[AgentRole, list] = {
            AgentRole.REVIEWER: [],
            AgentRole.VERIFIER: [],
        }
        self.worker_calls = 0

    def run(self, role: AgentRole, card: Card) -> AgentResult:
        if role == AgentRole.WORKER:
            self.worker_calls += 1
            return super().run(role, card)
        if role in (AgentRole.REVIEWER, AgentRole.VERIFIER) and self.queue[role]:
            action = self.queue[role].pop(0)
            if isinstance(action, tuple) and action[0] == "rework":
                _, summary, hints, failing = action
                revision = RevisionRequest(
                    at=datetime.now(timezone.utc),
                    from_role=role,
                    iteration=0,
                    summary=summary,
                    hints=list(hints),
                    failing_criteria=list(failing),
                )
                # Fallback next_status keeps the card safe if rework is ever
                # ignored by a future orchestrator; _apply_rework rewrites it.
                fallback = (
                    CardStatus.REVIEW
                    if role == AgentRole.REVIEWER
                    else CardStatus.VERIFY
                )
                return AgentResult(
                    role=role,
                    summary=f"{role.value} requested rework: {summary}",
                    next_status=fallback,
                    revision_request=revision,
                )
            if isinstance(action, tuple) and action[0] == "block":
                _, reason = action
                return AgentResult(
                    role=role,
                    summary=f"{role.value} blocked: {reason}",
                    next_status=CardStatus.BLOCKED,
                    updates={"blocked_reason": reason, "owner_role": None},
                )
        return super().run(role, card)


def _make_stack(
    repo: Path, *, retry_policy: RetryPolicy | None = None,
):
    board_dir = repo / "workspace" / "board"
    board_dir.mkdir(parents=True, exist_ok=True)
    store = MarkdownBoardStore(board_dir)
    executor = ScriptedRework()
    wt_mgr = WorktreeManager(
        project_root=repo,
        worktrees_root=repo / "workspace" / "worktrees",
    )
    (repo / "workspace" / "worktrees").mkdir(parents=True, exist_ok=True)
    orch = KanbanOrchestrator(
        store=store,
        executor=executor,
        worktree_mgr=wt_mgr,
        retry_policy=retry_policy or RetryPolicy(),
    )
    return store, orch, wt_mgr, executor


def _tick_through(orch: KanbanOrchestrator, n: int) -> None:
    for _ in range(n):
        orch.tick()


# ---------- reviewer rework: 3 accepted, 4th blocks ----------


def test_reviewer_rework_accepted_three_times_then_blocks(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    store, orch, wt_mgr, executor = _make_stack(repo)

    card = orch.create_card("Rework me", "Write hello.py")
    # Queue 4 rework asks so we can observe the budget cap.
    for i in range(1, 5):
        executor.queue[AgentRole.REVIEWER].append(
            ("rework", f"fix pass {i}", [f"hint-{i}"], [f"criterion-{i}"])
        )

    # planner(INBOX→READY), worker(READY→REVIEW), then reviewer reworks.
    _tick_through(orch, 2)
    assert store.get_card(card.id).status == CardStatus.REVIEW

    worktree_branch = store.get_card(card.id).worktree_branch
    assert worktree_branch is not None
    worktree_path = wt_mgr.worktrees_root / card.id
    assert worktree_path.exists()

    # Rework iteration 1: reviewer asks → card back to READY, worker re-runs.
    orch.tick()  # reviewer (produces rework)
    got = store.get_card(card.id)
    assert got.status == CardStatus.READY
    assert got.rework_iteration == 1
    assert len(got.revision_requests) == 1
    assert got.revision_requests[0].iteration == 1
    assert got.revision_requests[0].summary == "fix pass 1"
    assert got.revision_requests[0].hints == ["hint-1"]
    assert got.revision_requests[0].failing_criteria == ["criterion-1"]
    # Worktree must stay attached — worker picks up where it left off.
    assert worktree_path.exists()
    assert got.worktree_branch == worktree_branch

    # Iteration 2
    orch.tick()  # worker re-runs
    orch.tick()  # reviewer rework #2
    got = store.get_card(card.id)
    assert got.status == CardStatus.READY
    assert got.rework_iteration == 2
    assert [r.iteration for r in got.revision_requests] == [1, 2]
    assert worktree_path.exists()

    # Iteration 3
    orch.tick()  # worker
    orch.tick()  # reviewer rework #3
    got = store.get_card(card.id)
    assert got.status == CardStatus.READY
    assert got.rework_iteration == 3
    assert [r.iteration for r in got.revision_requests] == [1, 2, 3]
    assert worktree_path.exists()

    # 4th rework ask exceeds budget — BLOCKED + worktree detached.
    orch.tick()  # worker
    orch.tick()  # reviewer rework #4 (budget exhausted)
    got = store.get_card(card.id)
    assert got.status == CardStatus.BLOCKED
    assert got.blocked_reason is not None
    assert "rework budget exhausted" in got.blocked_reason
    assert "3 iterations" in got.blocked_reason
    # The 4th (rejected) request is still recorded for postmortem.
    assert [r.iteration for r in got.revision_requests] == [1, 2, 3, 4]
    # Worktree directory detached; branch kept.
    assert not worktree_path.exists()
    branch_exists = subprocess.run(
        ["git", "rev-parse", "--verify", f"refs/heads/{worktree_branch}"],
        cwd=repo, capture_output=True,
    )
    assert branch_exists.returncode == 0


# ---------- verifier rework: same path ----------


def test_verifier_rework_cycles_back_through_worker(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    store, orch, wt_mgr, executor = _make_stack(repo)

    card = orch.create_card("Verifier rework", "g")
    executor.queue[AgentRole.VERIFIER].append(
        ("rework", "missing file", [], ["create hello.py"])
    )

    # planner, worker, reviewer(approves) → card reaches VERIFY
    _tick_through(orch, 3)
    assert store.get_card(card.id).status == CardStatus.VERIFY

    # Verifier asks rework → card rewinds to READY
    orch.tick()
    got = store.get_card(card.id)
    assert got.status == CardStatus.READY
    assert got.rework_iteration == 1
    assert got.revision_requests[0].from_role == AgentRole.VERIFIER
    # Worker should increment its call count on the next tick.
    prior_calls = executor.worker_calls
    orch.tick()  # worker re-runs
    assert executor.worker_calls == prior_calls + 1


# ---------- backward compat: no revision_request → BLOCKED ----------


def test_plain_rejection_without_revision_request_still_blocks(tmp_path: Path):
    """v0.1.3 reviewer contract (no revision_request) must still block."""
    repo = _init_repo(tmp_path / "repo")
    store, orch, wt_mgr, executor = _make_stack(repo)

    card = orch.create_card("Legacy reject", "g")
    executor.queue[AgentRole.REVIEWER].append(("block", "no good"))

    _tick_through(orch, 2)  # planner, worker
    assert store.get_card(card.id).status == CardStatus.REVIEW

    orch.tick()  # reviewer blocks
    got = store.get_card(card.id)
    assert got.status == CardStatus.BLOCKED
    assert got.blocked_reason == "no good"
    assert got.rework_iteration == 0
    assert got.revision_requests == []


# ---------- configurable budget ----------


def test_rework_budget_is_configurable(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    store, orch, wt_mgr, executor = _make_stack(
        repo, retry_policy=RetryPolicy(rework=1),
    )

    card = orch.create_card("Short budget", "g")
    executor.queue[AgentRole.REVIEWER].extend([
        ("rework", "pass 1", [], []),
        ("rework", "pass 2", [], []),
    ])

    _tick_through(orch, 2)  # planner, worker
    orch.tick()  # reviewer rework #1 — accepted
    assert store.get_card(card.id).rework_iteration == 1
    assert store.get_card(card.id).status == CardStatus.READY

    orch.tick()  # worker re-runs
    orch.tick()  # reviewer rework #2 — over budget → BLOCKED
    got = store.get_card(card.id)
    assert got.status == CardStatus.BLOCKED
    assert "1 iterations" in (got.blocked_reason or "")


# ---------- worker prompt sees rework history ----------


def test_worker_prompt_includes_revision_requests(tmp_path: Path):
    """Verifies the rework block renders into the worker prompt so the
    agentao executor hands it to the LLM on subsequent passes."""
    from kanban.executors.agentao_multi import _build_prompt

    card = Card(title="t", goal="g")
    card.revision_requests = [
        RevisionRequest(
            at=datetime.now(timezone.utc),
            from_role=AgentRole.REVIEWER,
            iteration=1,
            summary="add a test",
            hints=["write tests/test_foo.py with one case"],
            failing_criteria=["tests exist"],
        ),
        RevisionRequest(
            at=datetime.now(timezone.utc),
            from_role=AgentRole.VERIFIER,
            iteration=2,
            summary="test must actually run",
            hints=["uv run pytest tests/test_foo.py"],
            failing_criteria=[],
        ),
    ]
    prompt = _build_prompt(AgentRole.WORKER, card)
    assert "REWORK HISTORY" in prompt
    assert "iteration 1 by REVIEWER" in prompt
    assert "add a test" in prompt
    assert "write tests/test_foo.py" in prompt
    assert "iteration 2 by VERIFIER" in prompt
    assert "test must actually run" in prompt


# ---------- persistence round-trip ----------


def test_revision_requests_survive_toml_round_trip(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    store, orch, _, executor = _make_stack(repo)

    card = orch.create_card("rt", "g")
    executor.queue[AgentRole.REVIEWER].append(
        ("rework", "redo X", ["do Y", "do Z"], ["c1"])
    )
    _tick_through(orch, 3)  # planner, worker, reviewer rework

    # Force a fresh store so we exercise the file-read path.
    fresh = MarkdownBoardStore(repo / "workspace" / "board")
    got = fresh.get_card(card.id)
    assert got.rework_iteration == 1
    assert len(got.revision_requests) == 1
    r = got.revision_requests[0]
    assert r.from_role == AgentRole.REVIEWER
    assert r.iteration == 1
    assert r.summary == "redo X"
    assert r.hints == ["do Y", "do Z"]
    assert r.failing_criteria == ["c1"]


# ---------- rework event is emitted ----------


def test_in_memory_store_accepts_rework_iteration_kwarg() -> None:
    """``_apply_rework`` emits ``append_runtime_event(rework_iteration=N)``.
    Both store implementations (markdown + in-memory) must accept it — the
    in-memory store is used by many unit tests and the runtime path must
    not raise TypeError when rework fires there."""
    from kanban.store import InMemoryBoardStore

    store = InMemoryBoardStore()
    store.append_runtime_event(
        card_id="c1",
        event_type="rework.requested",
        message="redo X",
        role=AgentRole.REVIEWER,
        rework_iteration=2,
    )
    events = store.list_events()
    assert len(events) == 1
    assert events[0].event_type == "rework.requested"
    assert events[0].rework_iteration == 2


def test_rework_requested_event_emitted(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    store, orch, _, executor = _make_stack(repo)

    card = orch.create_card("evt", "g")
    executor.queue[AgentRole.REVIEWER].append(
        ("rework", "do the thing", [], [])
    )
    _tick_through(orch, 3)  # planner, worker, reviewer rework

    events = list(store.events_for_card(card.id))
    rework_events = [e for e in events if e.event_type == "rework.requested"]
    assert len(rework_events) == 1
    ev = rework_events[0]
    assert ev.rework_iteration == 1
    assert ev.role == AgentRole.REVIEWER
    assert "do the thing" in ev.message
