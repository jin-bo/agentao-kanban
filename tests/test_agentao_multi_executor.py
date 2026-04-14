from __future__ import annotations

from pathlib import Path

import pytest

from kanban import CardStatus, InMemoryBoardStore, KanbanOrchestrator
from kanban.agents import AgentSpec, ROLE_AGENTS, load_spec
from kanban.executors.agentao_multi import AgentaoMultiAgentExecutor
from kanban.models import AgentRole, Card, ContextRef


REPO_AGENTS_DIR = Path(__file__).resolve().parent.parent / "docs" / "agent-definitions"


# ---------- fixtures ----------


class _FakeAgent:
    def __init__(self, response: str) -> None:
        self.response = response
        self.prompts: list[str] = []
        self.max_iterations: int | None = None

    def chat(self, user_message: str, max_iterations: int = 50) -> str:
        self.prompts.append(user_message)
        self.max_iterations = max_iterations
        return self.response


def _executor_with(response: str) -> tuple[AgentaoMultiAgentExecutor, _FakeAgent, list[AgentSpec]]:
    agent = _FakeAgent(response)
    seen_specs: list[AgentSpec] = []

    def factory(spec: AgentSpec, wd):
        seen_specs.append(spec)
        return agent

    ex = AgentaoMultiAgentExecutor(agents_dir=REPO_AGENTS_DIR, agent_factory=factory)
    return ex, agent, seen_specs


# ---------- role-agent wiring ----------


def test_role_agents_mapping_is_complete():
    assert set(ROLE_AGENTS) == set(AgentRole)
    assert ROLE_AGENTS[AgentRole.PLANNER] == "kanban-planner"
    assert ROLE_AGENTS[AgentRole.WORKER] == "kanban-worker"
    assert ROLE_AGENTS[AgentRole.REVIEWER] == "kanban-reviewer"
    assert ROLE_AGENTS[AgentRole.VERIFIER] == "kanban-verifier"


def test_every_role_has_a_definition_file():
    for role in AgentRole:
        spec = load_spec(role, REPO_AGENTS_DIR)
        assert spec.name == ROLE_AGENTS[role]
        assert spec.version  # frontmatter has version: set
        assert spec.system_instructions  # body is non-empty


def test_executor_routes_to_role_specific_spec():
    ex, _, seen = _executor_with(
        '```json\n{"ok": true, "summary": "x", "acceptance_criteria": ["a"]}\n```'
    )
    ex.run(AgentRole.PLANNER, Card(title="t", goal="g"))
    ex.run(AgentRole.WORKER, Card(title="t", goal="g"))
    assert [s.name for s in seen] == ["kanban-planner", "kanban-worker"]


def test_executor_passes_max_turns_from_spec():
    ex, agent, _ = _executor_with(
        '```json\n{"ok": true, "summary": "x", "output": "y"}\n```'
    )
    ex.run(AgentRole.WORKER, Card(title="t", goal="g"))
    worker_spec = load_spec(AgentRole.WORKER, REPO_AGENTS_DIR)
    assert agent.max_iterations == worker_spec.max_turns


# ---------- per-role behavior ----------


def test_planner_sets_acceptance_criteria():
    ex, _, _ = _executor_with(
        '```json\n{"ok": true, "summary": "planned", "acceptance_criteria": ["a", "b"]}\n```'
    )
    result = ex.run(AgentRole.PLANNER, Card(title="t", goal="g"))
    assert result.next_status == CardStatus.READY
    assert result.updates["acceptance_criteria"] == ["a", "b"]


def test_worker_writes_implementation_output():
    ex, _, _ = _executor_with(
        '```json\n{"ok": true, "summary": "coded", "output": "diff goes here"}\n```'
    )
    result = ex.run(AgentRole.WORKER, Card(title="t", goal="g"))
    assert result.next_status == CardStatus.REVIEW
    assert result.updates["outputs"] == {"implementation": "diff goes here"}
    assert result.updates["owner_role"] == AgentRole.REVIEWER


def test_reviewer_rejection_blocks_card():
    ex, _, _ = _executor_with(
        '```json\n{"ok": false, "blocked_reason": "tests failing"}\n```'
    )
    result = ex.run(
        AgentRole.REVIEWER, Card(title="t", goal="g", outputs={"implementation": "x"})
    )
    assert result.next_status == CardStatus.BLOCKED
    assert result.updates["blocked_reason"] == "tests failing"


def test_verifier_rejection_blocks_card():
    ex, _, _ = _executor_with(
        '```json\n{"ok": false, "blocked_reason": "criterion 2 not met"}\n```'
    )
    result = ex.run(AgentRole.VERIFIER, Card(title="t", goal="g"))
    assert result.next_status == CardStatus.BLOCKED
    assert "criterion 2" in result.updates["blocked_reason"]


def test_agent_exception_blocks_card():
    class Boom:
        def chat(self, *a, **kw):
            raise RuntimeError("llm offline")

    ex = AgentaoMultiAgentExecutor(
        agents_dir=REPO_AGENTS_DIR, agent_factory=lambda spec, wd: Boom()
    )
    result = ex.run(AgentRole.WORKER, Card(title="t", goal="g"))
    assert result.next_status == CardStatus.BLOCKED
    assert "llm offline" in result.updates["blocked_reason"]


def test_missing_definition_blocks_card(tmp_path: Path):
    # No .md files — load_spec must raise and executor must return BLOCKED.
    ex = AgentaoMultiAgentExecutor(
        agents_dir=tmp_path, agent_factory=lambda spec, wd: _FakeAgent("")
    )
    result = ex.run(AgentRole.PLANNER, Card(title="t", goal="g"))
    assert result.next_status == CardStatus.BLOCKED
    assert "agent definition missing" in result.updates["blocked_reason"]


def test_unstructured_response_still_progresses():
    ex, _, _ = _executor_with("Just some free-form text, no JSON here.")
    result = ex.run(AgentRole.VERIFIER, Card(title="t", goal="g"))
    assert result.next_status == CardStatus.DONE
    assert result.updates["outputs"]["verification"].startswith("Just some free-form")


def test_summary_is_tagged_with_spec_version():
    ex, _, _ = _executor_with(
        '```json\n{"ok": true, "summary": "planned", "acceptance_criteria": ["a"]}\n```'
    )
    result = ex.run(AgentRole.PLANNER, Card(title="t", goal="g"))
    assert "[kanban-planner v" in result.summary


# ---------- end-to-end ----------


def test_end_to_end_one_card_through_all_four_roles():
    responses = {
        "kanban-planner": '```json\n{"ok": true, "summary": "p", "acceptance_criteria": ["crit"]}\n```',
        "kanban-worker": '```json\n{"ok": true, "summary": "w", "output": "impl"}\n```',
        "kanban-reviewer": '```json\n{"ok": true, "summary": "r", "output": "lgtm"}\n```',
        "kanban-verifier": '```json\n{"ok": true, "summary": "v", "output": "verified"}\n```',
    }

    class RoleAwareAgent:
        def __init__(self, spec: AgentSpec) -> None:
            self.spec = spec

        def chat(self, user_message: str, max_iterations: int = 50) -> str:
            return responses[self.spec.name]

    ex = AgentaoMultiAgentExecutor(
        agents_dir=REPO_AGENTS_DIR,
        agent_factory=lambda spec, wd: RoleAwareAgent(spec),
    )
    store = InMemoryBoardStore()
    orch = KanbanOrchestrator(store=store, executor=ex)
    card = orch.create_card(title="e2e", goal="multi-agent")
    orch.run_until_idle(max_steps=20)
    final = store.get_card(card.id)
    assert final.status == CardStatus.DONE
    assert final.acceptance_criteria == ["crit"]
    assert final.outputs == {
        "implementation": "impl",
        "review": "lgtm",
        "verification": "verified",
    }


# ---------- context refs in prompt ----------


def test_context_refs_appear_in_prompt():
    ex, agent, _ = _executor_with(
        '```json\n{"ok": true, "summary": "w", "output": "impl"}\n```'
    )
    card = Card(
        title="t",
        goal="g",
        context_refs=[
            ContextRef(path="docs/api.md", kind="required", note="api contract"),
            ContextRef(path="workspace/data/x.jsonl", kind="optional"),
        ],
    )
    ex.run(AgentRole.WORKER, card)
    prompt = agent.prompts[-1]
    assert "REQUIRED CONTEXT" in prompt
    assert "docs/api.md" in prompt
    assert "api contract" in prompt
    assert "OPTIONAL CONTEXT" in prompt
    assert "workspace/data/x.jsonl" in prompt


def test_no_context_section_when_refs_empty():
    ex, agent, _ = _executor_with(
        '```json\n{"ok": true, "summary": "w", "output": "impl"}\n```'
    )
    ex.run(AgentRole.WORKER, Card(title="t", goal="g"))
    prompt = agent.prompts[-1]
    assert "REQUIRED CONTEXT" not in prompt
    assert "OPTIONAL CONTEXT" not in prompt


# ---------- planner output ----------


def test_planner_output_merged_into_outputs():
    ex, _, _ = _executor_with(
        '```json\n{"ok": true, "summary": "p", '
        '"acceptance_criteria": ["a"], '
        '"output": {"decision": "focus on reports/"}}\n```'
    )
    result = ex.run(AgentRole.PLANNER, Card(title="t", goal="g"))
    assert result.updates["outputs"]["planner"] == {"decision": "focus on reports/"}


def test_planner_without_output_does_not_touch_outputs():
    ex, _, _ = _executor_with(
        '```json\n{"ok": true, "summary": "p", "acceptance_criteria": ["a"]}\n```'
    )
    result = ex.run(AgentRole.PLANNER, Card(title="t", goal="g"))
    assert "outputs" not in result.updates


# ---------- load_spec frontmatter ----------


def test_load_spec_rejects_file_without_frontmatter(tmp_path: Path):
    bad = tmp_path / "kanban-planner.md"
    bad.write_text("no frontmatter here", encoding="utf-8")
    with pytest.raises(ValueError, match="Missing YAML frontmatter"):
        load_spec(AgentRole.PLANNER, tmp_path)
