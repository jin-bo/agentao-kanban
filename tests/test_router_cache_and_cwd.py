"""Regression tests for the Codex review findings on the worktree
isolation + rework path:

1. ``_patch_executor_cwd`` must walk into the executor's router policy
   and the policy's lazily-loaded client, so router decisions are made
   against the same isolated checkout the backend will operate on.

2. ``KanbanOrchestrator._apply_rework`` must bust cached router
   decisions for the card after accepting a rework — the cache key is
   built from card fields the router actually sees and ignores
   ``rework_iteration`` / ``revision_requests``, so without an explicit
   bust the next worker dispatch reuses the pre-rework profile.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pytest

from kanban import CardStatus, KanbanOrchestrator, MarkdownBoardStore
from kanban.executors import MockAgentaoExecutor
from kanban.executors.router_agent import (
    RouterRequest,
    build_candidates,
    build_card_summary,
    render_request,
)
from kanban.executors.router_policy import RouterPolicy
from kanban.models import (
    AgentResult,
    AgentRole,
    Card,
    RetryPolicy,
    RevisionRequest,
)
from kanban.orchestrator import _MISSING, _patch_executor_cwd
from kanban.worktree import WorktreeManager


# ---------- Fix 1: _patch_executor_cwd walks executor + policy + client


@dataclass
class _FakeRouterClient:
    working_directory: Path | None = None


@dataclass
class _FakePolicy:
    """Stand-in for ``RouterPolicy`` for the cwd-patch test."""

    working_directory: Path | None = None
    client: _FakeRouterClient | None = None


@dataclass
class _ExecWithPolicy:
    """Executor double with the same shape as ``MultiBackendExecutor``."""

    working_directory: Path | None = None
    policy: _FakePolicy | None = None


class _PlainExecutor:
    """Mirrors ``MockAgentaoExecutor``: no ``working_directory`` field."""


class TestPatchExecutorCwd:
    def test_executor_without_attr_is_patched_then_cleared(self) -> None:
        ex = _PlainExecutor()
        assert not hasattr(ex, "working_directory")
        restore = _patch_executor_cwd(ex, Path("/tmp/wt"))
        assert ex.working_directory == Path("/tmp/wt")
        restore()
        assert not hasattr(ex, "working_directory")

    def test_executor_with_none_attr_is_restored_to_none(self) -> None:
        ex = _ExecWithPolicy(working_directory=None)
        restore = _patch_executor_cwd(ex, Path("/tmp/wt"))
        assert ex.working_directory == Path("/tmp/wt")
        restore()
        assert ex.working_directory is None

    def test_executor_with_path_attr_is_restored(self) -> None:
        ex = _ExecWithPolicy(working_directory=Path("/orig"))
        restore = _patch_executor_cwd(ex, Path("/tmp/wt"))
        assert ex.working_directory == Path("/tmp/wt")
        restore()
        assert ex.working_directory == Path("/orig")

    def test_policy_working_directory_is_patched_and_restored(self) -> None:
        ex = _ExecWithPolicy(
            working_directory=Path("/repo"),
            policy=_FakePolicy(working_directory=Path("/repo")),
        )
        restore = _patch_executor_cwd(ex, Path("/repo/wt/card-x"))
        assert ex.policy.working_directory == Path("/repo/wt/card-x")
        restore()
        assert ex.policy.working_directory == Path("/repo")

    def test_router_client_working_directory_is_patched_and_restored(
        self,
    ) -> None:
        client = _FakeRouterClient(working_directory=Path("/repo"))
        ex = _ExecWithPolicy(
            working_directory=Path("/repo"),
            policy=_FakePolicy(working_directory=Path("/repo"), client=client),
        )
        restore = _patch_executor_cwd(ex, Path("/repo/wt/card-x"))
        assert ex.working_directory == Path("/repo/wt/card-x")
        assert ex.policy.working_directory == Path("/repo/wt/card-x")
        assert ex.policy.client.working_directory == Path("/repo/wt/card-x")
        restore()
        assert ex.working_directory == Path("/repo")
        assert ex.policy.working_directory == Path("/repo")
        assert ex.policy.client.working_directory == Path("/repo")

    def test_callable_policy_without_attr_is_left_alone(self) -> None:
        # A bare ``PolicyFn`` callable has no working_directory; the
        # helper must not inject the attribute.
        callable_policy = lambda role, card, cfg: None  # noqa: E731
        ex = _ExecWithPolicy(working_directory=Path("/repo"))
        ex.policy = callable_policy  # type: ignore[assignment]
        restore = _patch_executor_cwd(ex, Path("/repo/wt"))
        assert not hasattr(ex.policy, "working_directory")
        restore()
        assert ex.working_directory == Path("/repo")

    def test_no_policy_attribute_at_all(self) -> None:
        ex = _PlainExecutor()
        restore = _patch_executor_cwd(ex, Path("/tmp/wt"))
        assert ex.working_directory == Path("/tmp/wt")
        restore()
        assert not hasattr(ex, "working_directory")

    def test_policy_with_none_client(self) -> None:
        ex = _ExecWithPolicy(
            working_directory=Path("/repo"),
            policy=_FakePolicy(working_directory=Path("/repo"), client=None),
        )
        restore = _patch_executor_cwd(ex, Path("/repo/wt"))
        assert ex.policy.working_directory == Path("/repo/wt")
        restore()
        assert ex.policy.working_directory == Path("/repo")


# ---------- Fix 2: RouterPolicy.invalidate_card


class TestRouterPolicyInvalidateCard:
    def _seed(self, policy: RouterPolicy, card_id: str, role: AgentRole) -> None:
        # Inject a synthetic cache entry directly. We only need the cache
        # eviction behavior — RouterDecision shape matters for the
        # outcome path, not the keying.
        from kanban.executors.router_agent import RouterDecision

        decision = RouterDecision(
            profile="some-worker",
            reason="test",
            failure=None,
            prompt_version="v1",
        )
        policy._decision_cache[(card_id, role, "digest")] = decision

    def test_invalidate_drops_only_target_card(self) -> None:
        policy = RouterPolicy()
        self._seed(policy, "card-A", AgentRole.WORKER)
        self._seed(policy, "card-A", AgentRole.REVIEWER)
        self._seed(policy, "card-B", AgentRole.WORKER)

        evicted = policy.invalidate_card("card-A")
        assert evicted == 2
        remaining = list(policy._decision_cache.keys())
        assert remaining == [("card-B", AgentRole.WORKER, "digest")]

    def test_invalidate_unknown_card_is_a_noop(self) -> None:
        policy = RouterPolicy()
        self._seed(policy, "card-A", AgentRole.WORKER)
        assert policy.invalidate_card("does-not-exist") == 0
        assert len(policy._decision_cache) == 1

    def test_invalidate_clears_last_outcome_for_card(self) -> None:
        from kanban.executors.router_policy import PolicyOutcome

        policy = RouterPolicy()
        policy._last_outcome[("card-A", AgentRole.WORKER)] = PolicyOutcome(
            profile="x", reason="r", router_invoked=True,
        )
        policy._last_outcome[("card-B", AgentRole.WORKER)] = PolicyOutcome(
            profile="y", reason="r", router_invoked=True,
        )
        policy.invalidate_card("card-A")
        assert policy.last_outcome("card-A", AgentRole.WORKER) is None
        assert policy.last_outcome("card-B", AgentRole.WORKER) is not None


# ---------- Fix 2 wiring: _apply_rework calls policy.invalidate_card


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    for key, value in (("user.email", "t@t.com"), ("user.name", "T")):
        subprocess.run(
            ["git", "config", key, value],
            cwd=path, check=True, capture_output=True,
        )
    (path / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=path, check=True, capture_output=True,
    )
    return path


@dataclass
class _RecordingPolicy:
    """Minimal policy stub exposing ``invalidate_card`` as the
    orchestrator does duck-typing on it."""

    invalidations: list[str] = field(default_factory=list)
    working_directory: Path | None = None

    def invalidate_card(self, card_id: str) -> int:
        self.invalidations.append(card_id)
        return 1


class _ExecutorWithPolicy(MockAgentaoExecutor):
    """MockAgentaoExecutor + a recording ``policy`` attribute, so we can
    assert the orchestrator hits invalidate_card after a rework."""

    def __init__(self, policy: _RecordingPolicy) -> None:
        super().__init__()
        self.policy = policy


class _ScriptedReworkOnce(_ExecutorWithPolicy):
    """Worker passes through; reviewer asks for one rework then approves."""

    def __init__(self, policy: _RecordingPolicy) -> None:
        super().__init__(policy)
        self._rework_pending = True

    def run(self, role: AgentRole, card: Card) -> AgentResult:
        if role == AgentRole.REVIEWER and self._rework_pending:
            self._rework_pending = False
            revision = RevisionRequest(
                at=datetime.now(timezone.utc),
                from_role=role,
                iteration=0,
                summary="please fix the bar",
                hints=["check baz"],
                failing_criteria=["criterion-1"],
            )
            return AgentResult(
                role=role,
                summary="reviewer rework",
                next_status=CardStatus.REVIEW,
                revision_request=revision,
            )
        return super().run(role, card)


def _make_orchestrator(repo: Path, executor) -> tuple[
    MarkdownBoardStore, KanbanOrchestrator
]:
    board = repo / "workspace" / "board"
    board.mkdir(parents=True, exist_ok=True)
    store = MarkdownBoardStore(board)
    wt_mgr = WorktreeManager(
        project_root=repo,
        worktrees_root=repo / "workspace" / "worktrees",
    )
    (repo / "workspace" / "worktrees").mkdir(parents=True, exist_ok=True)
    orch = KanbanOrchestrator(
        store=store,
        executor=executor,
        worktree_mgr=wt_mgr,
        retry_policy=RetryPolicy(),
    )
    return store, orch


class TestApplyReworkInvalidatesRouterCache:
    def test_accepted_rework_calls_policy_invalidate(self, tmp_path: Path):
        repo = _init_repo(tmp_path / "repo")
        policy = _RecordingPolicy()
        executor = _ScriptedReworkOnce(policy)
        store, orch = _make_orchestrator(repo, executor)

        card = orch.create_card("rework-cache", "Make foo work")
        # planner(INBOX→READY), worker(READY→REVIEW), reviewer(rework).
        for _ in range(3):
            orch.tick()

        got = store.get_card(card.id)
        assert got.status == CardStatus.READY
        assert got.rework_iteration == 1
        # The Codex finding: orchestrator must have invalidated this card
        # so the next worker dispatch re-routes against the rework state.
        assert policy.invalidations == [card.id]

    def test_terminal_block_path_does_not_invalidate(self, tmp_path: Path):
        # Rework budget = 0 → first reviewer rework BLOCKs the card and
        # goes through _apply_normal_result, not the rework accept path.
        # We must NOT call invalidate_card on this branch (no future
        # worker dispatch is coming for this card).
        repo = _init_repo(tmp_path / "repo")
        policy = _RecordingPolicy()
        executor = _ScriptedReworkOnce(policy)

        board = repo / "workspace" / "board"
        board.mkdir(parents=True, exist_ok=True)
        store = MarkdownBoardStore(board)
        wt_mgr = WorktreeManager(
            project_root=repo,
            worktrees_root=repo / "workspace" / "worktrees",
        )
        (repo / "workspace" / "worktrees").mkdir(parents=True, exist_ok=True)
        orch = KanbanOrchestrator(
            store=store,
            executor=executor,
            worktree_mgr=wt_mgr,
            retry_policy=RetryPolicy(rework=0),
        )

        card = orch.create_card("rework-budget-0", "Make foo work")
        for _ in range(3):
            orch.tick()

        got = store.get_card(card.id)
        assert got.status == CardStatus.BLOCKED
        assert policy.invalidations == []

    def test_executor_without_policy_attribute_is_safe(self, tmp_path: Path):
        # MockAgentaoExecutor has no .policy; _invalidate_router_cache
        # must short-circuit silently.
        repo = _init_repo(tmp_path / "repo")
        executor = MockAgentaoExecutor()
        # Inject a one-shot rework via a tiny subclass to drive the
        # orchestrator without a policy on the executor.
        class _OneShot(MockAgentaoExecutor):
            def __init__(self_inner):
                super().__init__()
                self_inner._pending = True

            def run(self_inner, role: AgentRole, card: Card) -> AgentResult:
                if role == AgentRole.REVIEWER and self_inner._pending:
                    self_inner._pending = False
                    revision = RevisionRequest(
                        at=datetime.now(timezone.utc),
                        from_role=role,
                        iteration=0,
                        summary="x", hints=[], failing_criteria=[],
                    )
                    return AgentResult(
                        role=role,
                        summary="rework",
                        next_status=CardStatus.REVIEW,
                        revision_request=revision,
                    )
                return super().run(role, card)

        store, orch = _make_orchestrator(repo, _OneShot())
        card = orch.create_card("no-policy", "do thing")
        for _ in range(3):
            orch.tick()
        # Survives the rework path without raising.
        assert store.get_card(card.id).rework_iteration == 1

    def test_executor_with_non_invalidating_policy_is_safe(
        self, tmp_path: Path
    ):
        # A policy that lacks ``invalidate_card`` (older policy
        # implementations, custom doubles): orchestrator must not crash.
        @dataclass
        class _NoOpPolicy:
            pass

        class _Exec(_ScriptedReworkOnce):
            def __init__(self_inner) -> None:
                # Bypass _RecordingPolicy; install a different shape.
                super().__init__(_RecordingPolicy())
                self_inner.policy = _NoOpPolicy()  # type: ignore[assignment]

        repo = _init_repo(tmp_path / "repo")
        store, orch = _make_orchestrator(repo, _Exec())
        card = orch.create_card("non-invalidating-policy", "x")
        for _ in range(3):
            orch.tick()
        assert store.get_card(card.id).rework_iteration == 1


# ---------- Codex P1 follow-up: cache key encodes rework_iteration so
# the split scheduler/worker topology naturally picks up rework changes
# even though the worker process never sees ``invalidate_card``.


class TestCacheKeyIncludesReworkIteration:
    def test_render_request_emits_rework_iteration(self) -> None:
        from kanban.agent_profiles import load_default_config

        cfg = load_default_config()
        card = Card(title="t", goal="g", rework_iteration=3)
        req = RouterRequest(
            card=build_card_summary(card, AgentRole.WORKER),
            candidates=build_candidates(AgentRole.WORKER, cfg.profiles),
        )
        payload = json.loads(render_request(req))
        assert payload["card"]["rework_iteration"] == 3

    def test_cache_key_diverges_after_rework(self) -> None:
        from kanban.agent_profiles import load_default_config

        cfg = load_default_config()
        before = Card(title="t", goal="g", id="c1", rework_iteration=0)
        after = Card(title="t", goal="g", id="c1", rework_iteration=1)

        req_before = RouterRequest(
            card=build_card_summary(before, AgentRole.WORKER),
            candidates=build_candidates(AgentRole.WORKER, cfg.profiles),
        )
        req_after = RouterRequest(
            card=build_card_summary(after, AgentRole.WORKER),
            candidates=build_candidates(AgentRole.WORKER, cfg.profiles),
        )
        # The Codex P1 fix: cache keys are computed per-process from
        # ``render_request``; if rework_iteration is encoded there, both
        # the scheduler's and a worker's cache key change after a rework
        # without any cross-process invalidation.
        key_before = RouterPolicy._cache_key("c1", AgentRole.WORKER, req_before)
        key_after = RouterPolicy._cache_key("c1", AgentRole.WORKER, req_after)
        assert key_before != key_after

    def test_worker_process_isolated_cache_naturally_invalidates(self) -> None:
        """Simulate the split topology: the scheduler's policy already
        has a cached decision for (card, worker, rework=0); a fresh
        worker-side policy in another process computes a NEW cache key
        for the same card after rework=1 and therefore re-routes.
        """
        from kanban.agent_profiles import load_default_config
        from kanban.executors.router_agent import RouterDecision

        cfg = load_default_config()

        # Scheduler-side cache state at rework_iteration=0.
        scheduler_policy = RouterPolicy()
        card_v0 = Card(title="t", goal="g", id="c1", rework_iteration=0)
        req_v0 = RouterRequest(
            card=build_card_summary(card_v0, AgentRole.WORKER),
            candidates=build_candidates(AgentRole.WORKER, cfg.profiles),
        )
        key_v0 = RouterPolicy._cache_key("c1", AgentRole.WORKER, req_v0)
        scheduler_policy._decision_cache[key_v0] = RouterDecision(
            profile="cheap-worker", reason="seeded", failure=None,
        )

        # Worker-side policy in another process — empty cache. It sees
        # the post-rework card (rework_iteration=1) and computes a key
        # that *cannot* hit any pre-rework cached entry.
        worker_policy = RouterPolicy()
        card_v1 = Card(title="t", goal="g", id="c1", rework_iteration=1)
        req_v1 = RouterRequest(
            card=build_card_summary(card_v1, AgentRole.WORKER),
            candidates=build_candidates(AgentRole.WORKER, cfg.profiles),
        )
        key_v1 = RouterPolicy._cache_key("c1", AgentRole.WORKER, req_v1)
        assert key_v1 != key_v0
        assert key_v1 not in worker_policy._decision_cache
