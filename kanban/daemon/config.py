from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4


@dataclass
class DaemonConfig:
    poll_interval: float = 2.0
    max_idle_cycles: int | None = None  # None = run forever
    # v0.1.2 role split knobs. Legacy (KanbanDaemon) ignores these.
    max_claims: int = 2
    # ``None`` (the default) means "auto-derive" — ``__post_init__`` fills in
    # a random ``worker-<8-hex>`` value and records that the id was generated.
    # That flag lets ``CombinedDaemon._derive_worker_prefix`` distinguish the
    # auto-id from an operator who explicitly passed a string that happens to
    # match the default shape (e.g. ``--worker-id worker-deadbeef``).
    worker_id: str | None = None

    def __post_init__(self) -> None:
        if self.worker_id is None:
            self._worker_id_auto = True
            self.worker_id = f"worker-{uuid4().hex[:8]}"
        else:
            self._worker_id_auto = False
