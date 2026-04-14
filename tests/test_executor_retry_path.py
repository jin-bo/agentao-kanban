"""Integration tests tying the agentao executor's retryable-failure
contract to the v0.1.2 retry matrix. Codex flagged that planner format
drift and LLM-call exceptions were becoming terminal BLOCKED transitions
instead of going through the infrastructure-retry path. These tests
prove the new behavior end-to-end via WorkerDaemon.
"""
from __future__ import annotations

from pathlib import Path

from kanban import CardStatus, KanbanOrchestrator
from kanban.daemon import DaemonConfig, WorkerDaemon
from kanban.executors.agentao_multi import AgentaoMultiAgentExecutor
from kanban.models import AgentRole, Card, RetryPolicy
from kanban.store_markdown import MarkdownBoardStore

REPO_AGENTS_DIR = Path(__file__).resolve().parent.parent / "docs" / "agent-definitions"


class _FlakyAgent:
    """Raises on first N ``chat`` calls, then succeeds with a valid
    planner response."""

    def __init__(self, fails: int, response: str) -> None:
        self.fails = fails
        self.response = response
        self.calls = 0

    def chat(self, *a, **kw) -> str:
        self.calls += 1
        if self.calls <= self.fails:
            raise RuntimeError(f"llm offline (attempt {self.calls})")
        return self.response


def _make_board(board: Path, executor) -> tuple[MarkdownBoardStore, KanbanOrchestrator]:
    store = MarkdownBoardStore(board)
    orch = KanbanOrchestrator(
        store=store,
        executor=executor,
        retry_policy=RetryPolicy(infrastructure=2),  # plan default
    )
    return store, orch


def _ready_worker_card(store: MarkdownBoardStore) -> Card:
    return store.add_card(
        Card(
            title="t",
            goal="g",
            status=CardStatus.READY,
            owner_role=AgentRole.WORKER,
            acceptance_criteria=["x"],
        )
    )


# ---------- executor + WorkerDaemon + retry matrix ----------


def test_llm_chat_exception_retries_via_infrastructure_category(tmp_path: Path):
    """An LLM 5xx-style failure raises from the executor, WorkerDaemon
    catches it and submits ok=False with FailureCategory.INFRASTRUCTURE,
    and the retry matrix creates a fresh claim with attempt=2. The second
    attempt succeeds and the card progresses out of DOING."""
    agent = _FlakyAgent(
        fails=1,
        response='```json\n{"ok": true, "summary": "worker ok", "output": "impl"}\n```',
    )
    executor = AgentaoMultiAgentExecutor(
        agents_dir=REPO_AGENTS_DIR, agent_factory=lambda spec, wd: agent
    )
    store, orch = _make_board(tmp_path, executor)
    card = _ready_worker_card(store)

    # Simulate scheduler: create claim, worker picks up, executor raises,
    # worker submits ok=False envelope, committer retries.
    orch.select_and_claim(worker_id=None)
    worker = WorkerDaemon(orch, config=DaemonConfig(worker_id="w1"))
    worker.run_once()  # first attempt: _FlakyAgent raises → infra failure

    # Commit the failed envelope — retry matrix creates a retry claim.
    orch.commit_pending_results()
    retry = store.get_claim(card.id)
    assert retry is not None and retry.attempt == 2, (
        "executor exception should trigger retry, not terminal BLOCKED"
    )

    # Second attempt: agent succeeds, envelope commits, card moves to REVIEW.
    worker.run_once()
    orch.commit_pending_results()
    assert store.get_claim(card.id) is None
    assert store.get_card(card.id).status == CardStatus.REVIEW
    assert agent.calls == 2


def test_planner_format_drift_retries_instead_of_blocking(tmp_path: Path):
    """Planner returns free-form text on its first call (format drift),
    structured json on retry. The card must not end up in BLOCKED after
    attempt 1 — it must go through the retry matrix and succeed on 2."""
    planner_agent = _FlakyAgent(
        fails=0,
        response="free-form prose, definitely no json fence here",
    )

    class FlakyOnceThenGood:
        def __init__(self) -> None:
            self.calls = 0

        def chat(self, *a, **kw) -> str:
            self.calls += 1
            if self.calls == 1:
                return "free-form prose, definitely no json fence here"
            return (
                '```json\n{"ok": true, "summary": "planned", '
                '"acceptance_criteria": ["crit a", "crit b"]}\n```'
            )

    agent = FlakyOnceThenGood()
    executor = AgentaoMultiAgentExecutor(
        agents_dir=REPO_AGENTS_DIR, agent_factory=lambda spec, wd: agent
    )
    store, orch = _make_board(tmp_path, executor)
    # Start the card in INBOX so planner is the executing role.
    card = store.add_card(Card(title="t", goal="g"))

    orch.select_and_claim(worker_id=None)
    worker = WorkerDaemon(orch, config=DaemonConfig(worker_id="w1"))
    worker.run_once()
    orch.commit_pending_results()

    # After attempt 1, card must NOT be BLOCKED — retry claim created.
    assert store.get_card(card.id).status != CardStatus.BLOCKED
    retry = store.get_claim(card.id)
    assert retry is not None and retry.attempt == 2

    # Attempt 2 succeeds.
    worker.run_once()
    orch.commit_pending_results()
    assert store.get_claim(card.id) is None
    assert store.get_card(card.id).status != CardStatus.BLOCKED
    assert agent.calls == 2


def test_planner_replan_without_new_criteria_does_not_block(tmp_path: Path):
    """End-to-end: a card bouncing back to INBOX for replan already has
    criteria. The planner omits the ``acceptance_criteria`` field in its
    JSON response. The executor must NOT block; existing criteria survive."""

    class NarrowReplanAgent:
        def chat(self, *a, **kw) -> str:
            return (
                '```json\n{"ok": true, "summary": "replanned", '
                '"output": "narrow scope"}\n```'
            )

    executor = AgentaoMultiAgentExecutor(
        agents_dir=REPO_AGENTS_DIR, agent_factory=lambda spec, wd: NarrowReplanAgent()
    )
    store, orch = _make_board(tmp_path, executor)
    card = store.add_card(
        Card(
            title="t",
            goal="g",
            acceptance_criteria=["existing crit 1", "existing crit 2"],
        )
    )
    orch.select_and_claim(worker_id=None)
    worker = WorkerDaemon(orch, config=DaemonConfig(worker_id="w1"))
    worker.run_once()
    orch.commit_pending_results()

    fresh = store.get_card(card.id)
    assert fresh.status != CardStatus.BLOCKED
    # Existing criteria preserved.
    assert fresh.acceptance_criteria == ["existing crit 1", "existing crit 2"]
