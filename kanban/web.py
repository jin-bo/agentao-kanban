"""Read-only HTTP board for the kanban project.

The goal is a local/intranet observability window into a live board dir —
no writes, no auth, no SSE. Run with::

    uv run kanban web --board workspace/board

Design notes:

- Synchronous handlers (``def`` rather than ``async def``) so FastAPI runs
  them on its threadpool. The store is file-backed and blocking.
- A fresh :class:`MarkdownBoardStore` is constructed per request. That
  guarantees every response reflects out-of-band writes from the CLI,
  MCP server, or the daemon without coupling to any refresh cadence.
- Runtime state (``list_claims``/``list_workers``) returns an empty list
  when ``runtime/`` is absent, so a board that was never touched by the
  daemon still renders.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from .mcp import card_to_dict, event_to_dict
from .models import AgentRole, CardStatus
from .store_markdown import MarkdownBoardStore


# Fixed column order for the frontend. Mirrors CardStatus declaration order.
COLUMN_ORDER: tuple[CardStatus, ...] = (
    CardStatus.INBOX,
    CardStatus.READY,
    CardStatus.DOING,
    CardStatus.REVIEW,
    CardStatus.VERIFY,
    CardStatus.DONE,
    CardStatus.BLOCKED,
)

COLUMN_TITLES: dict[CardStatus, str] = {
    CardStatus.INBOX: "Inbox",
    CardStatus.READY: "Ready",
    CardStatus.DOING: "Doing",
    CardStatus.REVIEW: "Review",
    CardStatus.VERIFY: "Verify",
    CardStatus.DONE: "Done",
    CardStatus.BLOCKED: "Blocked",
}

_VALID_ROLES = tuple(r.value for r in AgentRole)
_VALID_STATUSES = tuple(s.value for s in CardStatus)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _card_summary(card_dict: dict[str, Any]) -> dict[str, Any]:
    """Slim board-column record.

    The card detail endpoint uses the full ``card_to_dict``; the board
    snapshot only needs the fields the column renders, which keeps the
    payload small on boards with many DONE cards.
    """
    keys = (
        "id",
        "title",
        "status",
        "priority",
        "owner_role",
        "blocked_reason",
        "updated_at",
        "created_at",
        "depends_on",
        "rework_iteration",
        "agent_profile",
    )
    return {k: card_dict.get(k) for k in keys}


def _claim_to_dict(claim) -> dict[str, Any]:
    return {
        "card_id": claim.card_id,
        "claim_id": claim.claim_id,
        "role": claim.role.value,
        "status_at_claim": claim.status_at_claim.value,
        "attempt": claim.attempt,
        "worker_id": claim.worker_id,
        "claimed_at": claim.claimed_at.isoformat(),
        "lease_expires_at": claim.lease_expires_at.isoformat(),
        "heartbeat_at": claim.heartbeat_at.isoformat(),
    }


def _worker_to_dict(worker) -> dict[str, Any]:
    return {
        "worker_id": worker.worker_id,
        "pid": worker.pid,
        "host": worker.host,
        "started_at": worker.started_at.isoformat(),
        "heartbeat_at": worker.heartbeat_at.isoformat(),
    }


def _display_tag(event_dict: dict[str, Any]) -> str:
    """Short label for event coloring in the UI."""
    if event_dict.get("event_type"):
        return str(event_dict["event_type"])
    if event_dict.get("role"):
        return str(event_dict["role"])
    return "info"


def _annotate_event(event_dict: dict[str, Any]) -> dict[str, Any]:
    event_dict["display_tag"] = _display_tag(event_dict)
    return event_dict


def _assets_dir() -> Path:
    """Concrete filesystem path for ``kanban/web_assets/``.

    The assets ship alongside ``kanban/web.py`` inside both the source
    tree and the wheel (hatchling include list), so resolving relative
    to this module's file gives a real directory in both editable and
    installed layouts. We avoid ``importlib.resources`` here because
    it can hand back a :class:`MultiplexedPath` when the parent package
    is a namespace-style namespace at discovery time, which
    :class:`StaticFiles` cannot mount.
    """
    return Path(__file__).resolve().parent / "web_assets"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Currently a no-op. Kept as the forward-compatible home for any
    # future background refresh or cache priming.
    yield


def create_app(
    board_dir: str | Path,
    *,
    poll_interval_ms: int = 5000,
) -> FastAPI:
    """Build the read-only FastAPI app bound to a board directory.

    ``board_dir`` is not required to exist yet. ``MarkdownBoardStore``
    no longer materializes ``cards/`` at construction time, so handlers
    can serve a missing board as an empty one without performing any
    filesystem writes — preserving the read-only contract.
    """
    board_path = Path(board_dir).resolve()
    app = FastAPI(
        title="Kanban read-only board",
        lifespan=_lifespan,
    )
    app.state.board_dir = board_path
    app.state.poll_interval_ms = max(int(poll_interval_ms), 250)

    assets_dir = _assets_dir()

    def _store() -> MarkdownBoardStore:
        return MarkdownBoardStore(board_path)

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {
            "status": "ok",
            "board_dir": str(board_path),
            "poll_interval_ms": app.state.poll_interval_ms,
        }

    @app.get("/api/board")
    def api_board() -> dict[str, Any]:
        store = _store()
        by_status: dict[CardStatus, list[dict[str, Any]]] = {
            s: [] for s in COLUMN_ORDER
        }
        for card in store.list_cards():
            if card.status not in by_status:
                # Defensive: unknown status values would be dropped. In
                # practice ``CardStatus`` is exhaustive on the store side.
                continue
            by_status[card.status].append(_card_summary(card_to_dict(card)))
        columns = []
        for status in COLUMN_ORDER:
            cards = by_status[status]
            # Stable order inside a column: high priority first, then oldest
            # created_at. Mirrors store.list_by_status's sort key so board
            # order matches ``kanban list``.
            cards.sort(
                key=lambda c: (
                    -_priority_rank(c.get("priority")),
                    c.get("created_at") or "",
                )
            )
            columns.append(
                {
                    "status": status.value,
                    "title": COLUMN_TITLES[status],
                    "count": len(cards),
                    "cards": cards,
                }
            )
        recent = [
            _annotate_event(event_to_dict(e))
            for e in store.list_events(limit=20)
        ]
        runtime = {
            "claims": [_claim_to_dict(c) for c in store.list_claims()],
            "workers": [_worker_to_dict(w) for w in store.list_workers()],
        }
        return {
            "generated_at": _iso_now(),
            "board_dir": str(board_path),
            "poll_interval_ms": app.state.poll_interval_ms,
            "columns": columns,
            "recent_events": recent,
            "runtime": runtime,
        }

    @app.get("/api/cards/{card_id}")
    def api_card(card_id: str) -> dict[str, Any]:
        store = _store()
        try:
            card = store.get_card(card_id)
        except KeyError:
            raise HTTPException(
                status_code=404, detail=f"card {card_id} not found"
            )
        payload = card_to_dict(card)
        payload["recent_events"] = [
            _annotate_event(event_to_dict(e))
            for e in store.events_for_card(card.id)[-20:]
        ]
        return payload

    @app.get("/api/events")
    def api_events(
        limit: int = Query(50, ge=0, le=500),
        card_id: str | None = Query(None),
        role: str | None = Query(None),
        execution_only: bool = Query(False),
    ) -> dict[str, Any]:
        role_enum: AgentRole | None = None
        if role is not None:
            try:
                role_enum = AgentRole(role.lower())
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail=f"role must be one of {_VALID_ROLES}",
                )
        store = _store()
        if execution_only or role_enum is not None:
            events = store.list_execution_events(
                card_id=card_id, role=role_enum, limit=limit
            )
        else:
            events = store.list_events(limit=None)
            if card_id is not None:
                events = [e for e in events if e.card_id == card_id]
            if limit == 0:
                events = []
            elif limit is not None:
                events = events[-limit:]
        return {
            "generated_at": _iso_now(),
            "count": len(events),
            "events": [_annotate_event(event_to_dict(e)) for e in events],
        }

    @app.get("/", response_class=HTMLResponse)
    def index() -> Response:
        html = (assets_dir / "index.html").read_text(encoding="utf-8")
        html = html.replace(
            "__POLL_INTERVAL_MS__", str(app.state.poll_interval_ms)
        )
        return HTMLResponse(html)

    app.mount(
        "/static",
        StaticFiles(directory=str(assets_dir)),
        name="static",
    )

    return app


_PRIORITY_RANK = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1}


def _priority_rank(name: str | None) -> int:
    if name is None:
        return 0
    return _PRIORITY_RANK.get(name.upper(), 0)


def main(
    board_dir: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    poll_interval_ms: int = 5000,
) -> int:
    """Run the read-only web server via uvicorn. Returns the process rc."""
    import uvicorn

    app = create_app(board_dir, poll_interval_ms=poll_interval_ms)
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0
