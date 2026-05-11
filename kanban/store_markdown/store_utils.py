from __future__ import annotations

import logging
from typing import Any

_LOG = logging.getLogger(__name__)


def _tail(items: list[Any], limit: int | None) -> list[Any]:
    """Return the last `limit` items. `None` -> all; `<=0` -> none."""
    if limit is None:
        return items
    if limit <= 0:
        return []
    return items[-limit:]
