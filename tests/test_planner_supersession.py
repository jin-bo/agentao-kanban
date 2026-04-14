"""Regression tests for the contract-downgrade finding in the Codex
adversarial review of v0.1.2 prompts.

A planner replan used to be able to silently drop a prior acceptance
criterion by returning a new list without the dropped one. Combined with
the reviewer/verifier treating "the planner's current criteria" as the
contract, that meant an unmet hard criterion could be replaced by an
easier one and the card could advance without ever proving the original
requirement. The fix: the executor now rejects any replan that drops a
prior criterion unless it is explicitly recorded in
``output.superseded[]`` with a reason.
"""
from __future__ import annotations

from pathlib import Path

from kanban.executors.agentao_multi import AgentaoMultiAgentExecutor
from kanban.models import AgentRole, Card, CardStatus

REPO_AGENTS_DIR = Path(__file__).resolve().parent.parent / "docs" / "agent-definitions"


class _FakeAgent:
    def __init__(self, response: str) -> None:
        self.response = response

    def chat(self, *a, **kw) -> str:
        return self.response


def _executor_with(response: str) -> AgentaoMultiAgentExecutor:
    return AgentaoMultiAgentExecutor(
        agents_dir=REPO_AGENTS_DIR,
        agent_factory=lambda spec, wd: _FakeAgent(response),
    )


def _planner_response(
    *,
    criteria: list[str],
    superseded: list[dict] | None = None,
) -> str:
    import json

    payload: dict = {
        "ok": True,
        "summary": "replanned",
        "acceptance_criteria": criteria,
    }
    if superseded is not None:
        payload["output"] = {"decision": "d", "superseded": superseded}
    else:
        payload["output"] = {"decision": "d"}
    return f"```json\n{json.dumps(payload)}\n```"


# ---------- dropping without supersession is rejected ----------


def test_replan_drops_criterion_without_superseded_is_blocked():
    """Simulate: original card had ["A", "B", "C"]. Planner returns only
    ["A", "B"] — silently drops C. Must be BLOCKED."""
    ex = _executor_with(_planner_response(criteria=["A", "B"]))
    card = Card(
        title="t", goal="g", acceptance_criteria=["A", "B", "C"]
    )
    result = ex.run(AgentRole.PLANNER, card)
    assert result.next_status == CardStatus.BLOCKED
    reason = result.updates["blocked_reason"]
    assert "without" in reason and "superseded" in reason


def test_replan_drops_criterion_with_superseded_is_accepted():
    """Same drop, but declared in output.superseded with a reason. Must
    progress out of INBOX (planner's next_status), criteria replaced."""
    ex = _executor_with(
        _planner_response(
            criteria=["A", "B"],
            superseded=[
                {"criterion": "C", "reason": "subsumed by B after clarifying goal"}
            ],
        )
    )
    card = Card(title="t", goal="g", acceptance_criteria=["A", "B", "C"])
    result = ex.run(AgentRole.PLANNER, card)
    assert result.next_status != CardStatus.BLOCKED
    assert result.updates["acceptance_criteria"] == ["A", "B"]


def test_replan_superseded_entry_without_reason_is_rejected():
    """A superseded entry missing a reason must not satisfy the gate —
    otherwise any placeholder lets drops through."""
    ex = _executor_with(
        _planner_response(
            criteria=["A", "B"],
            superseded=[{"criterion": "C", "reason": ""}],
        )
    )
    card = Card(title="t", goal="g", acceptance_criteria=["A", "B", "C"])
    result = ex.run(AgentRole.PLANNER, card)
    assert result.next_status == CardStatus.BLOCKED
    assert "superseded" in result.updates["blocked_reason"]


def test_replan_superseded_entry_mismatched_text_is_rejected():
    """Supersession must name the exact prior criterion text — otherwise
    a planner can declare a superseded entry for a criterion the card
    never had and bypass the gate on the real dropped one."""
    ex = _executor_with(
        _planner_response(
            criteria=["A", "B"],
            superseded=[{"criterion": "Z", "reason": "some reason"}],
        )
    )
    card = Card(title="t", goal="g", acceptance_criteria=["A", "B", "C"])
    result = ex.run(AgentRole.PLANNER, card)
    assert result.next_status == CardStatus.BLOCKED
    assert "superseded" in result.updates["blocked_reason"]


# ---------- additions / refinements don't trigger the gate ----------


def test_replan_adding_criteria_does_not_require_superseded():
    """Adding a new criterion is always allowed — more strict, not less."""
    ex = _executor_with(_planner_response(criteria=["A", "B", "C", "D"]))
    card = Card(title="t", goal="g", acceptance_criteria=["A", "B", "C"])
    result = ex.run(AgentRole.PLANNER, card)
    assert result.next_status != CardStatus.BLOCKED
    assert result.updates["acceptance_criteria"] == ["A", "B", "C", "D"]


def test_replan_omitting_criteria_list_still_preserves_existing():
    """(Cross-check with prior fix.) If the planner omits the list, the
    executor leaves existing criteria intact — supersession gate does
    not apply because there is no new list to validate."""
    import json

    payload = {
        "ok": True,
        "summary": "replanned",
        "output": {"decision": "just tightening notes"},
    }
    ex = _executor_with(f"```json\n{json.dumps(payload)}\n```")
    card = Card(title="t", goal="g", acceptance_criteria=["A", "B"])
    result = ex.run(AgentRole.PLANNER, card)
    assert result.next_status != CardStatus.BLOCKED
    assert "acceptance_criteria" not in result.updates


def test_first_plan_has_no_prior_criteria_so_no_supersession_needed():
    """On a fresh card (no existing criteria), the gate never fires —
    there is nothing to drop."""
    ex = _executor_with(_planner_response(criteria=["A", "B", "C"]))
    card = Card(title="t", goal="g")  # no acceptance_criteria
    result = ex.run(AgentRole.PLANNER, card)
    assert result.next_status != CardStatus.BLOCKED
    assert result.updates["acceptance_criteria"] == ["A", "B", "C"]


def test_replan_full_replace_requires_supersession_for_every_drop():
    """A full rewrite is allowed but every prior criterion must appear
    in output.superseded with a reason — even if some would have been
    acceptable to keep, the planner must explicitly acknowledge the drop."""
    ex = _executor_with(
        _planner_response(
            criteria=["X", "Y"],
            superseded=[{"criterion": "A", "reason": "replaced by X"}],
            # Notice: B is NOT listed — gate should still fire.
        )
    )
    card = Card(title="t", goal="g", acceptance_criteria=["A", "B"])
    result = ex.run(AgentRole.PLANNER, card)
    assert result.next_status == CardStatus.BLOCKED
    assert "superseded" in result.updates["blocked_reason"]
