from .base import CardExecutor
from .mock_agentao import MockAgentaoExecutor

__all__ = ["CardExecutor", "MockAgentaoExecutor", "AgentaoMultiAgentExecutor"]


def __getattr__(name: str):
    # Lazy-import the real executor so importing kanban.executors does not
    # require the optional `agentao` runtime dependency at import time.
    if name == "AgentaoMultiAgentExecutor":
        from .agentao_multi import AgentaoMultiAgentExecutor
        return AgentaoMultiAgentExecutor
    raise AttributeError(f"module 'kanban.executors' has no attribute {name!r}")
