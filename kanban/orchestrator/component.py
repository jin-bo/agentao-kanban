from __future__ import annotations


class OrchestratorComponent:
    def __init__(self, orchestrator) -> None:
        self.orchestrator = orchestrator

    def __getattr__(self, name: str):
        return getattr(self.orchestrator, name)
