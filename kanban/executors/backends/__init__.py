from .base import Backend, BackendRequest, BackendResponse
from .subagent_backend import SubagentBackend

__all__ = [
    "Backend",
    "BackendRequest",
    "BackendResponse",
    "SubagentBackend",
    "AcpBackend",
]


def __getattr__(name: str):
    # Lazy so `agentao.acp_client` is only imported when the ACP backend is used.
    if name == "AcpBackend":
        from .acp_backend import AcpBackend
        return AcpBackend
    raise AttributeError(f"module 'kanban.executors.backends' has no attribute {name!r}")
