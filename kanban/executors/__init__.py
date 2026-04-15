from .base import CardExecutor
from .mock_agentao import MockAgentaoExecutor

__all__ = [
    "CardExecutor",
    "MockAgentaoExecutor",
    "AgentaoMultiAgentExecutor",
    "MultiBackendExecutor",
]


def __getattr__(name: str):
    # Lazy-import the real executors so importing kanban.executors does not
    # require the optional `agentao` runtime dependency at import time.
    if name == "AgentaoMultiAgentExecutor":
        from .agentao_multi import AgentaoMultiAgentExecutor
        return AgentaoMultiAgentExecutor
    if name == "MultiBackendExecutor":
        from .multi_backend import MultiBackendExecutor
        return MultiBackendExecutor
    raise AttributeError(f"module 'kanban.executors' has no attribute {name!r}")
