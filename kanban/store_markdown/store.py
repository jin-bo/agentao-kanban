from __future__ import annotations

import os
from pathlib import Path

from ..models import (
    Card,
    CardEvent,
)
from .card_store import CardStore
from .event_store import EventStore
from .runtime_store import RuntimeStore

DEFAULT_RAW_RETENTION = 5


class MarkdownBoardStore:
    """Persist board state as one Markdown file per card under <root>/cards/.

    The TOML front-matter between ``+++`` fences is the source of truth. The
    body below is a regenerated human-readable view and is ignored on read.
    Events are appended to ``<root>/events.log`` as tab-separated lines.
    """

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        raw_root: str | os.PathLike[str] | None = None,
        raw_retention: int = DEFAULT_RAW_RETENTION,
    ) -> None:
        self.root = Path(root)
        self.cards_dir = self.root / "cards"
        self.events_path = self.root / "events.log"
        self.runtime_dir = self.root / "runtime"
        self.claims_dir = self.runtime_dir / "claims"
        self.results_dir = self.runtime_dir / "results"
        self.workers_dir = self.runtime_dir / "workers"
        # Raw transcripts live beside the board dir by default (workspace/raw/
        # for a board at workspace/board/). workspace/ is already gitignored.
        self.raw_root = Path(raw_root) if raw_root else self.root.parent / "raw"
        self.raw_retention = raw_retention
        # No mkdir here: read-only callers (e.g. the web server) should be
        # able to instantiate the store without materializing the board on
        # disk. Write paths (`_write_card`, `_write_event_line`, runtime
        # writers) mkdir their own directories lazily.

        self._cards: dict[str, Card] = {}
        self._events: list[CardEvent] = []
        self._unparseable: list[str] = []
        self.card_store = CardStore(self)
        self.event_store = EventStore(self)
        self.runtime_store = RuntimeStore(self)
        self._load()

    def __getattr__(self, name: str):
        for component in (
            self.card_store,
            self.event_store,
            self.runtime_store,
        ):
            if name in vars(type(component)):
                return getattr(component, name)
        raise AttributeError(
            f"{type(self).__name__!s} object has no attribute {name!r}"
        )
