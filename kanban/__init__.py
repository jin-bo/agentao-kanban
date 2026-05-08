from importlib import metadata as _metadata

from .models import Card, CardPriority, CardStatus
from .orchestrator import KanbanOrchestrator
from .store import BoardStore, InMemoryBoardStore
from .store_markdown import MarkdownBoardStore


def _read_version() -> str:
    try:
        return _metadata.version("agentao-kanban")
    except _metadata.PackageNotFoundError:
        pass
    try:
        import tomllib
        from pathlib import Path

        pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
        if pyproject.is_file():
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            return str(data.get("project", {}).get("version", "0.0.0+unknown"))
    except (OSError, ValueError):
        pass
    return "0.0.0+unknown"


def __getattr__(name: str) -> str:
    # PEP 562 module __getattr__: defer the metadata lookup until someone
    # actually reads __version__ (banner, scripts), so plain `import kanban`
    # — which the CLI does on every invocation — doesn't pay the cost.
    if name == "__version__":
        version = _read_version()
        globals()["__version__"] = version
        return version
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "BoardStore",
    "Card",
    "CardPriority",
    "CardStatus",
    "InMemoryBoardStore",
    "KanbanOrchestrator",
    "MarkdownBoardStore",
    "__version__",
]
