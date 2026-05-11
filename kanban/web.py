"""HTTP board for the kanban project.

The goal is a local/intranet observability window into a live board dir.
By default it is read-only — no writes, no auth, no SSE. Run with::

    uv run kanban web --board workspace/board

A narrow write opt-in (`--enable-writes`) exposes ``POST /api/cards`` so
operators can drop new INBOX cards from the browser. Card creation is
the only mutation served here intentionally: it doesn't race the daemon
(new cards have fresh UUIDs and the daemon doesn't write them before
claiming) and it doesn't participate in ``.daemon.lock``. State changes
(``move``/``block``/``unblock``) stay on the CLI/MCP write paths.

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

import os
import stat as stat_mod
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from ipaddress import ip_address
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .daemon import daemon_status
from .gitutil import find_git_root_optional
from .mcp import card_to_dict, event_to_dict
from .models import AgentRole, Card, CardPriority, CardStatus
from .result import summarize_card_result
from .store_markdown import MarkdownBoardStore
from .worktree import ARTIFACT_DIR_NAME_RE


# Fixed column order for the frontend. Mirrors CardStatus declaration order.
COLUMN_ORDER: tuple[CardStatus, ...] = (
    CardStatus.INBOX,
    CardStatus.READY,
    CardStatus.DOING,
    CardStatus.REVIEW,
    CardStatus.DONE,
    CardStatus.BLOCKED,
)

COLUMN_TITLES: dict[CardStatus, str] = {
    CardStatus.INBOX: "Inbox",
    CardStatus.READY: "Ready",
    CardStatus.DOING: "Doing",
    CardStatus.REVIEW: "Review",
    CardStatus.DONE: "Done",
    CardStatus.BLOCKED: "Blocked",
}

_VALID_ROLES = tuple(r.value for r in AgentRole)
_VALID_STATUSES = tuple(s.value for s in CardStatus)

# Cap inline file responses. 8 MiB is generous for logs/text but small
# enough to keep the loopback server snappy and memory-bounded. Operators
# who need bigger payloads can copy from disk — the listing endpoint
# always advertises the exact byte size so the cap is observable.
_ARTIFACT_FILE_MAX_BYTES = 8 * 1024 * 1024

# Defensive cap on per-snapshot file enumeration. A pathological worker
# emitting tens of thousands of tiny files would still respect the
# byte cap upstream but could blow out the JSON payload and the DOM.
# Truncated listings advertise ``truncated: true`` + ``total_file_count``
# so the UI can hint the operator to copy from disk.
_ARTIFACT_LISTING_MAX_FILES = 5000


def _artifacts_root_for(board_dir: Path) -> Path:
    """Conventional artifacts root for a given board.

    ``WorktreeManager`` (driven from the CLI) writes snapshots under
    ``<git_root>/workspace/raw``. With the standard ``kanban init``
    layout the board lives at ``<git_root>/workspace/board``, so
    ``board_dir.parent / "raw"`` resolves to the same directory. For
    non-conventional layouts the directory simply won't exist and the
    artifacts surface stays empty rather than 500ing.
    """
    return board_dir.parent / "raw"


def _list_artifact_snapshots(
    card_id: str, root: Path
) -> list[dict[str, Any]]:
    """Enumerate ``raw/<card_id>/artifacts-*`` snapshots, newest first.

    Symlinks and non-regular files are skipped — the file-fetch route
    won't serve them anyway, so listing them would only confuse the UI.
    Listings are capped at ``_ARTIFACT_LISTING_MAX_FILES`` per snapshot;
    when truncated the record carries ``truncated: True`` and the full
    count is reported in ``total_file_count``.
    """
    card_dir = root / card_id
    if not card_dir.is_dir():
        return []
    snapshots: list[dict[str, Any]] = []
    for snap in sorted(card_dir.glob("artifacts-*"), reverse=True):
        if not snap.is_dir() or not ARTIFACT_DIR_NAME_RE.match(snap.name):
            continue
        files: list[dict[str, Any]] = []
        total_bytes = 0
        total_count = 0
        truncated = False
        # ``os.walk`` with ``followlinks=False`` plus a single ``lstat``
        # per entry is ~3× cheaper than the equivalent ``Path.rglob`` +
        # ``is_symlink``/``is_file``/``stat`` chain on snapshots with
        # many small files.
        for dirpath, _dirnames, filenames in os.walk(snap, followlinks=False):
            dpath = Path(dirpath)
            for name in filenames:
                full = dpath / name
                try:
                    st = full.lstat()
                except OSError:
                    continue
                if not stat_mod.S_ISREG(st.st_mode):
                    continue
                total_count += 1
                total_bytes += st.st_size
                if len(files) >= _ARTIFACT_LISTING_MAX_FILES:
                    truncated = True
                    continue
                try:
                    rel = full.relative_to(snap)
                except ValueError:
                    continue
                files.append({"path": str(rel), "size": st.st_size})
        try:
            created = datetime.fromtimestamp(
                snap.stat().st_mtime, tz=timezone.utc
            ).isoformat()
        except OSError:
            created = None
        files.sort(key=lambda f: f["path"])
        record: dict[str, Any] = {
            "snapshot": snap.name,
            "created_at": created,
            "file_count": len(files),
            "total_file_count": total_count,
            "total_bytes": total_bytes,
            "files": files,
        }
        if truncated:
            record["truncated"] = True
        snapshots.append(record)
    return snapshots


def _display_path(abs_path: str, *, board_dir: Path, git_root: Path | None) -> str:
    """A human-friendly relative rendering of an absolute result path.

    Tries the Git root first (so the standard layout yields
    ``workspace/raw/<card>/...``), then the board's parent (yielding
    ``raw/<card>/...`` for boards outside a repo). Falls back to the
    absolute path if it lives under neither — callers should treat this
    purely as a display aid, never as something to join against a root.
    """
    p = Path(abs_path)
    roots = [board_dir.parent]
    if git_root is not None:
        roots.insert(0, git_root)
    for root in roots:
        try:
            return str(p.relative_to(root))
        except ValueError:
            continue
    return abs_path


def _display_path_map(
    abs_paths: list[str], *, board_dir: Path, git_root: Path | None
) -> dict[str, str]:
    return {
        a: _display_path(a, board_dir=board_dir, git_root=git_root)
        for a in abs_paths
    }


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


class CardCreateRequest(BaseModel):
    """POST /api/cards body. Mirrors ``kanban card add`` arguments.

    Pydantic validates the shape; semantic coercion (priority enum,
    acceptance trimming) happens in the handler so we can return clean
    400s with field-specific messages instead of pydantic's default
    422 envelope for the enum case.
    """

    title: str = Field(min_length=1, max_length=500)
    goal: str = Field(default="", max_length=4000)
    priority: str = Field(default="MEDIUM")
    acceptance_criteria: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)


def create_app(
    board_dir: str | Path,
    *,
    poll_interval_ms: int = 5000,
    enable_writes: bool = False,
) -> FastAPI:
    """Build the FastAPI app bound to a board directory.

    ``board_dir`` is not required to exist yet. ``MarkdownBoardStore``
    no longer materializes ``cards/`` at construction time, so handlers
    can serve a missing board as an empty one without performing any
    filesystem writes — preserving the read-only contract for read paths.

    ``enable_writes`` is the single switch for the write surface
    (currently just ``POST /api/cards``). Default off keeps the original
    contract; turning it on is a deliberate operator choice.
    """
    board_path = Path(board_dir).resolve()
    app = FastAPI(
        title="Kanban board",
        lifespan=_lifespan,
    )
    app.state.board_dir = board_path
    app.state.poll_interval_ms = max(int(poll_interval_ms), 250)
    app.state.enable_writes = bool(enable_writes)

    assets_dir = _assets_dir()

    def _store() -> MarkdownBoardStore:
        return MarkdownBoardStore(board_path)

    def _get_card_or_404(store: MarkdownBoardStore, card_id: str) -> Card:
        try:
            return store.get_card(card_id)
        except KeyError:
            raise HTTPException(
                status_code=404, detail=f"card {card_id} not found"
            )

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {
            "status": "ok",
            "board_dir": str(board_path),
            "poll_interval_ms": app.state.poll_interval_ms,
            "writes_enabled": app.state.enable_writes,
        }

    @app.get("/api/board")
    def api_board() -> dict[str, Any]:
        store = _store()
        by_status: dict[CardStatus, list[dict[str, Any]]] = {
            s: [] for s in CardStatus
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
            "writes_enabled": app.state.enable_writes,
            "columns": columns,
            "recent_events": recent,
            "runtime": runtime,
            "daemon": daemon_status(board_path),
        }

    @app.get("/api/daemon")
    def api_daemon() -> dict[str, Any]:
        return daemon_status(board_path)

    @app.post("/api/cards", status_code=201)
    def api_create_card(payload: CardCreateRequest) -> dict[str, Any]:
        if not app.state.enable_writes:
            # 405 (not 403) so the surface advertises "writes are off",
            # which the frontend uses to hide the form anyway.
            raise HTTPException(
                status_code=405,
                detail=(
                    "writes are disabled; start the server with "
                    "--enable-writes to allow card creation"
                ),
            )
        title = payload.title.strip()
        if not title:
            raise HTTPException(status_code=400, detail="title must not be blank")
        try:
            priority = CardPriority[payload.priority.upper()]
        except KeyError:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"priority must be one of "
                    f"{[p.name for p in CardPriority]}"
                ),
            )
        acceptance = [c.strip() for c in payload.acceptance_criteria if c.strip()]
        depends = [d.strip() for d in payload.depends_on if d.strip()]
        # Card creation is intentionally not gated on .daemon.lock: the new
        # card has a fresh UUID (no card-file overwrite race) and events.log
        # appends are POSIX-atomic for short lines (see
        # store_markdown._write_event_line). The daemon picks the card up on
        # its next tick the same way it picks up CLI-created cards.
        store = _store()
        # Validate depends_on early so callers see a 400 instead of getting
        # a half-created card with a stale dependency reference.
        for dep in depends:
            try:
                store.get_card(dep)
            except KeyError:
                raise HTTPException(
                    status_code=400,
                    detail=f"depends_on references unknown card {dep!r}",
                )
        card = store.add_card(
            Card(
                title=title,
                goal=payload.goal.strip(),
                priority=priority,
                acceptance_criteria=acceptance,
                depends_on=depends,
            )
        )
        return card_to_dict(card)

    @app.get("/api/cards/{card_id}")
    def api_card(card_id: str) -> dict[str, Any]:
        store = _store()
        card = _get_card_or_404(store, card_id)
        payload = card_to_dict(card)
        payload["recent_events"] = [
            _annotate_event(event_to_dict(e))
            for e in store.events_for_card(card.id)[-20:]
        ]
        return payload

    @app.get("/api/cards/{card_id}/result")
    def api_card_result(card_id: str) -> dict[str, Any]:
        # Web equivalent of `kanban result --json`: same field semantics,
        # plus Web-only `*_display_paths` maps (absolute path -> relative
        # rendering) for the UI. Read-only; no `--enable-writes` needed.
        # Artifact snapshots resolve against the store's raw root
        # (board_dir.parent/raw) so this and /api/cards/{id}/artifacts
        # never disagree; transcripts come from the store.
        store = _store()
        card = _get_card_or_404(store, card_id)
        payload = summarize_card_result(
            board_path, store, card, artifacts_root=store.raw_root
        )
        artifacts = list(payload.get("artifacts") or [])
        transcripts = list(payload.get("transcripts") or [])
        # Only probe for the Git root when there's actually a path to
        # render relative to it — a fresh card has none.
        git_root = (
            find_git_root_optional(board_path)
            if (artifacts or transcripts)
            else None
        )
        payload["artifact_display_paths"] = _display_path_map(
            artifacts, board_dir=board_path, git_root=git_root
        )
        payload["transcript_display_paths"] = _display_path_map(
            transcripts, board_dir=board_path, git_root=git_root
        )
        return payload

    @app.get("/api/cards/{card_id}/artifacts")
    def api_list_artifacts(card_id: str) -> dict[str, Any]:
        # Validate the card exists so a typo'd id surfaces as 404 instead
        # of an empty list — matches /api/cards/{card_id}'s contract.
        _get_card_or_404(_store(), card_id)
        snapshots = _list_artifact_snapshots(
            card_id, _artifacts_root_for(board_path)
        )
        return {"card_id": card_id, "snapshots": snapshots}

    @app.get("/api/cards/{card_id}/artifacts/{snapshot}/file")
    def api_artifact_file(
        card_id: str,
        snapshot: str,
        path: str = Query(..., min_length=1, max_length=2048),
    ):
        # Cheap syntactic checks first — reject malformed/probing
        # requests before we touch the store or the filesystem.
        # FastAPI path segments don't span '/' so card_id can't contain
        # one via routing; backslash and '..' can still arrive
        # url-decoded.
        if "\\" in card_id or ".." in card_id:
            raise HTTPException(status_code=400, detail="invalid card id")
        if not ARTIFACT_DIR_NAME_RE.match(snapshot):
            raise HTTPException(status_code=400, detail="invalid snapshot id")
        if path.startswith(("/", "\\")):
            raise HTTPException(status_code=400, detail="path must be relative")
        parts = path.replace("\\", "/").split("/")
        if any(p in ("", "..") for p in parts):
            raise HTTPException(status_code=400, detail="invalid path")

        _get_card_or_404(_store(), card_id)

        root = _artifacts_root_for(board_path).resolve()
        snap_dir = (root / card_id / snapshot).resolve()
        try:
            snap_dir.relative_to(root)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid snapshot path")
        if not snap_dir.is_dir():
            raise HTTPException(status_code=404, detail="snapshot not found")

        unresolved = snap_dir / path
        # Refuse symlink leaves: WorktreeManager preserves symlinks in
        # snapshots but the web surface is a read-only browser, and
        # following them would change the trust boundary.
        if unresolved.is_symlink():
            raise HTTPException(status_code=403, detail="symlinks not served")
        target = unresolved.resolve()
        try:
            target.relative_to(snap_dir)
        except ValueError:
            # An intermediate symlink resolved out of the snapshot.
            raise HTTPException(status_code=400, detail="path escapes snapshot")
        try:
            st = target.stat()
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="file not found")
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"stat failed: {exc}")
        if not stat_mod.S_ISREG(st.st_mode):
            raise HTTPException(status_code=400, detail="not a regular file")
        if st.st_size > _ARTIFACT_FILE_MAX_BYTES:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"file is {st.st_size} bytes; the inline cap is "
                    f"{_ARTIFACT_FILE_MAX_BYTES}. Copy directly from "
                    f"{target}."
                ),
            )
        return FileResponse(target, filename=target.name)

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


def _is_loopback_host(host: str) -> bool:
    """True if the bind host is unambiguously a loopback address.

    ``"localhost"`` is special-cased rather than resolved: we don't want
    to depend on /etc/hosts here, and the only sane configuration that
    binds to "localhost" is loopback in practice. Numeric addresses are
    parsed via :class:`ipaddress.ip_address` so IPv4-mapped-in-IPv6 like
    ``"::1"`` is recognized.
    """
    if host in ("localhost", ""):
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def check_writes_host_safety(host: str, *, allow_remote_writes: bool) -> None:
    """Refuse a non-loopback bind under ``--enable-writes`` without an explicit override.

    Raised as :class:`SystemExit` with a clear message so the CLI fails
    early rather than silently exposing an unauthenticated write
    endpoint to the network.
    """
    if allow_remote_writes:
        return
    if _is_loopback_host(host):
        return
    raise SystemExit(
        f"refusing to enable writes on non-loopback host {host!r}: "
        f"the write endpoint has no authentication. "
        f"Use 127.0.0.1 (default) or pass --allow-remote-writes if you "
        f"have a reverse proxy or firewall in front of the server."
    )


def main(
    board_dir: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    poll_interval_ms: int = 5000,
    enable_writes: bool = False,
    allow_remote_writes: bool = False,
) -> int:
    """Run the web server via uvicorn. Returns the process rc."""
    import uvicorn

    if enable_writes:
        check_writes_host_safety(host, allow_remote_writes=allow_remote_writes)

    app = create_app(
        board_dir,
        poll_interval_ms=poll_interval_ms,
        enable_writes=enable_writes,
    )
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0
