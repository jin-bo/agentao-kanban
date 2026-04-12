from __future__ import annotations

from ..models import AgentResult, AgentRole, Card, CardStatus


class MockAgentaoExecutor:
    """Minimal stand-in for a future Agentao-backed executor.

    The orchestrator only depends on the executor interface, so this class can
    be replaced later by an adapter that invokes local Agentao sub-agents or ACP
    servers.
    """

    def run(self, role: AgentRole, card: Card) -> AgentResult:
        if role == AgentRole.PLANNER:
            criteria = card.acceptance_criteria or [
                "Have a clear implementation note",
                "Pass review",
                "Pass verification",
            ]
            return AgentResult(
                role=role,
                summary="Planner prepared the card for execution.",
                next_status=CardStatus.READY,
                updates={"acceptance_criteria": criteria, "owner_role": None},
            )

        if role == AgentRole.WORKER:
            outputs = dict(card.outputs)
            outputs["implementation"] = f"Implemented work for: {card.title}"
            return AgentResult(
                role=role,
                summary="Worker completed the implementation pass.",
                next_status=CardStatus.REVIEW,
                updates={"outputs": outputs, "owner_role": AgentRole.REVIEWER},
            )

        if role == AgentRole.REVIEWER:
            outputs = dict(card.outputs)
            outputs["review"] = "Review passed with no blocking issues."
            return AgentResult(
                role=role,
                summary="Reviewer approved the result.",
                next_status=CardStatus.VERIFY,
                updates={"outputs": outputs, "owner_role": AgentRole.VERIFIER},
            )

        if role == AgentRole.VERIFIER:
            outputs = dict(card.outputs)
            outputs["verification"] = "Acceptance criteria verified."
            return AgentResult(
                role=role,
                summary="Verifier marked the card as done.",
                next_status=CardStatus.DONE,
                updates={"outputs": outputs, "owner_role": None},
            )

        raise ValueError(f"Unsupported role: {role}")
