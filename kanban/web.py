"""HTTP board for the kanban project.

The goal is a local/intranet observability window into a live board dir.
By default it is read-only — no writes, no auth, no SSE. Run with::

    uv run kanban web --board workspace/board

Write surface:

- ``POST /api/cards`` — create a new INBOX card. Always available under
  ``--enable-writes``; returns ``405`` when writes are off. Not gated on
  ``.daemon.lock`` (fresh UUID, no overwrite race).
- ``POST /api/cards/{id}/move`` — change a card's status.
- ``POST /api/cards/{id}/requeue`` — return a card to ``inbox`` / ``ready``.
- ``POST /api/cards/{id}/block`` — move a card to ``blocked`` with a reason.
- ``POST /api/cards/{id}/unblock`` — move a blocked card back.

The four existing-card actions require ``--enable-writes`` (``403`` otherwise),
refuse to run while a live daemon holds ``.daemon.lock`` (``409``) or while
the target card has a live execution claim (``409``), and call the shared
``kanban.operations.transition_*`` functions so they behave exactly like
the CLI/MCP equivalents. There is no ``--force``, no bulk mutation, no
daemon control, and no worktree merge/prune/delete/checkout route here.

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
from ipaddress import ip_address
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from .daemon import daemon_status
from .gitutil import find_git_root_optional
from .mcp import card_to_dict, event_to_dict
from .models import AgentRole, Card, CardPriority, CardStatus
from .operations import (
    OperationError,
    transition_block,
    transition_move,
    transition_requeue,
    transition_unblock,
)
from .result import summarize_card_result, worktree_state
from .store_markdown import MarkdownBoardStore
from .worktree import ARTIFACT_DIR_NAME_RE, WorktreeDiffError, WorktreeManager
from .web_artifacts import (
    artifacts_root_for,
    list_artifact_snapshots,
    serve_file_under_root,
)
from .web_serializers import (
    annotate_event,
    card_summary,
    claim_to_dict,
    display_path,
    display_path_map,
    priority_rank,
    worker_to_dict,
)


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

# Cap on the inline ``git diff --stat`` body the /diff route returns.
# ``--stat`` is one line per changed file, so 1 MiB is already an
# enormous changeset; past that the response is truncated and flagged so
# the UI can point the operator at ``kanban worktree diff`` for the full
# output.
_DIFF_MAX_BYTES = 1 * 1024 * 1024

# Operator-facing copy for the worktree states that can't produce a diff.
# ``missing`` is rendered as an actionable error by the UI; the others are
# clean empty states. Keeps the /diff route from ever 500ing on a card
# that simply has no worktree.
_DIFF_STATE_MESSAGES = {
    "none": "No worktree was attached to this card, so there is nothing to diff.",
    "not-git": (
        "Board is not inside a Git repository; worktree isolation — and "
        "therefore a worktree diff — is unavailable."
    ),
    "missing": (
        "The recorded worktree branch no longer resolves. Run "
        "`kanban worktree prune` to clear the stale metadata."
    ),
}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


class CardMoveRequest(BaseModel):
    """POST /api/cards/{id}/move body."""

    status: str = Field(min_length=1)


class CardRequeueRequest(BaseModel):
    """POST /api/cards/{id}/requeue body. ``note`` is an optional history line."""

    target: str = Field(default=CardStatus.INBOX.value, min_length=1)
    note: str | None = Field(default=None, max_length=2000)


class CardBlockRequest(BaseModel):
    """POST /api/cards/{id}/block body."""

    reason: str = Field(min_length=1, max_length=2000)


class CardUnblockRequest(BaseModel):
    """POST /api/cards/{id}/unblock body."""

    target: str = Field(default=CardStatus.INBOX.value, min_length=1)


class WriteRejected(Exception):
    """A mutating Web request rejected with a stable JSON error envelope.

    ``error`` is a short machine code; ``message`` is operator-facing;
    ``retryable`` tells the UI whether the same request might succeed
    later (daemon lock / live claim) or never (writes disabled, bad
    input, missing card).
    """

    def __init__(
        self, *, status_code: int, error: str, message: str, retryable: bool
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error = error
        self.message = message
        self.retryable = retryable


def _model_validate(model_cls: type[BaseModel], raw: object):
    """Parse ``raw`` into ``model_cls``, on either Pydantic v1 or v2.

    ``BaseModel.model_validate`` is v2-only; ``parse_obj`` is the v1 name
    (still present, deprecated, on v2). FastAPI declares no pydantic
    major, so support both. Raises :class:`pydantic.ValidationError`.
    """
    validate = getattr(model_cls, "model_validate", None)
    if validate is not None:
        return validate(raw)
    return model_cls.parse_obj(raw)


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
            by_status[card.status].append(card_summary(card_to_dict(card)))
        columns = []
        for status in COLUMN_ORDER:
            cards = by_status[status]
            # Stable order inside a column: high priority first, then oldest
            # created_at. Mirrors store.list_by_status's sort key so board
            # order matches ``kanban list``.
            cards.sort(
                key=lambda c: (
                    -priority_rank(c.get("priority")),
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
            annotate_event(event_to_dict(e))
            for e in store.list_events(limit=20)
        ]
        runtime = {
            "claims": [claim_to_dict(c) for c in store.list_claims()],
            "workers": [worker_to_dict(w) for w in store.list_workers()],
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

    @app.exception_handler(WriteRejected)
    async def _on_write_rejected(_request, exc: WriteRejected) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": exc.error,
                "message": exc.message,
                "retryable": exc.retryable,
            },
        )

    def _require_writes_enabled() -> None:
        """Reject every existing-card mutation when ``--enable-writes`` is off.

        Checked before the request body is even parsed, so a read-only
        server returns the same ``403`` envelope for any card id and any
        body shape — it never echoes the action schema or reveals
        daemon/claim state.
        """
        if not app.state.enable_writes:
            raise WriteRejected(
                status_code=403,
                error="writes_disabled",
                message=(
                    "card writes are disabled; start the server with "
                    "--enable-writes to allow this action"
                ),
                retryable=False,
            )

    def _guard_card_write(store: MarkdownBoardStore, card_id: str) -> None:
        """Daemon-lock + live-claim guard for existing-card mutations.

        Call :func:`_require_writes_enabled` first. No ``--force`` escape
        hatch; neither check distinguishes existent from nonexistent cards.
        """
        status = daemon_status(board_path).get("status")
        if status == "running":
            raise WriteRejected(
                status_code=409,
                error="daemon_active",
                message=(
                    "a live daemon holds the board lock; stop the daemon "
                    "before mutating cards over HTTP"
                ),
                retryable=True,
            )
        claim = store.get_claim(card_id)
        if claim is not None:
            worker_tag = (
                f"worker={claim.worker_id}" if claim.worker_id else "unassigned"
            )
            raise WriteRejected(
                status_code=409,
                error="live_claim",
                message=(
                    f"card {card_id} has a live execution claim "
                    f"{claim.claim_id} ({worker_tag}); wait for the worker "
                    f"to finish or stop it, then retry"
                ),
                retryable=True,
            )

    def _web_worktree_mgr():
        """WorktreeManager for terminal landings, or ``None`` off-repo."""
        git_root = find_git_root_optional(board_path)
        if git_root is None:
            return None
        return WorktreeManager.for_project(git_root)

    def _transition_payload(result) -> dict[str, Any]:
        return {"card": card_to_dict(result.card), "warnings": list(result.warnings)}

    def _do_card_transition(card_id: str, raw_body, model_cls, run):
        """Blocking half of an existing-card mutation (runs in the threadpool).

        Order: daemon-lock / live-claim guard → body validation → card
        existence → ``run(payload, store, card_id)``. The writes-enabled
        gate has already fired in the async wrapper. The card id must be
        the full id — these routes don't do the CLI's prefix expansion,
        same as the read routes.
        """
        store = _store()
        _guard_card_write(store, card_id)
        try:
            payload = _model_validate(model_cls, raw_body)
        except ValidationError as exc:
            details = "; ".join(
                f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}"
                for e in exc.errors()
            )
            raise WriteRejected(
                status_code=400,
                error="invalid_input",
                message=details or "invalid request body",
                retryable=False,
            )
        try:
            store.get_card(card_id)
        except KeyError:
            raise WriteRejected(
                status_code=404,
                error="not_found",
                message=f"card {card_id} not found",
                retryable=False,
            )
        try:
            result = run(payload, store, card_id)
        except OperationError as exc:
            raise WriteRejected(
                status_code=400,
                error="invalid_input",
                message=str(exc),
                retryable=False,
            )
        except KeyError:
            # The card vanished between the existence check and the write
            # (e.g. a concurrent delete). Treat as not found.
            raise WriteRejected(
                status_code=404,
                error="not_found",
                message=f"card {card_id} not found",
                retryable=False,
            )
        return _transition_payload(result)

    async def _run_card_transition(
        card_id: str, request: Request, model_cls, run
    ) -> dict[str, Any]:
        """Guard, parse the body, run a ``transition_*`` call.

        The ``--enable-writes`` gate fires *before* the request body is
        read, so a read-only server can't be probed via a malformed body.
        Everything blocking after that (store load, lock/claim guard,
        transition) runs in the threadpool, matching the sync read routes.
        """
        _require_writes_enabled()
        try:
            raw_body = await request.json()
        except Exception:
            raise WriteRejected(
                status_code=400,
                error="invalid_input",
                message="request body must be a JSON object",
                retryable=False,
            )
        return await run_in_threadpool(
            _do_card_transition, card_id, raw_body, model_cls, run
        )

    @app.post("/api/cards/{card_id}/move")
    async def api_card_move(card_id: str, request: Request) -> dict[str, Any]:
        return await _run_card_transition(
            card_id,
            request,
            CardMoveRequest,
            lambda p, store, cid: transition_move(
                store, _web_worktree_mgr(), cid, p.status, note="Manual move via Web"
            ),
        )

    @app.post("/api/cards/{card_id}/requeue")
    async def api_card_requeue(card_id: str, request: Request) -> dict[str, Any]:
        return await _run_card_transition(
            card_id,
            request,
            CardRequeueRequest,
            lambda p, store, cid: transition_requeue(
                store, cid, p.target, (p.note or None)
            ),
        )

    @app.post("/api/cards/{card_id}/block")
    async def api_card_block(card_id: str, request: Request) -> dict[str, Any]:
        return await _run_card_transition(
            card_id,
            request,
            CardBlockRequest,
            lambda p, store, cid: transition_block(
                store, _web_worktree_mgr(), cid, p.reason
            ),
        )

    @app.post("/api/cards/{card_id}/unblock")
    async def api_card_unblock(card_id: str, request: Request) -> dict[str, Any]:
        return await _run_card_transition(
            card_id,
            request,
            CardUnblockRequest,
            lambda p, store, cid: transition_unblock(
                store, _web_worktree_mgr(), cid, p.target
            ),
        )

    @app.get("/api/cards/{card_id}")
    def api_card(card_id: str) -> dict[str, Any]:
        store = _store()
        card = _get_card_or_404(store, card_id)
        payload = card_to_dict(card)
        payload["recent_events"] = [
            annotate_event(event_to_dict(e))
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
        payload["artifact_display_paths"] = display_path_map(
            artifacts, board_dir=board_path, git_root=git_root
        )
        payload["transcript_display_paths"] = display_path_map(
            transcripts, board_dir=board_path, git_root=git_root
        )
        return payload

    @app.get("/api/cards/{card_id}/diff")
    def api_card_diff(card_id: str) -> dict[str, Any]:
        # Web equivalent of `kanban worktree diff <card-id>`: a read-only
        # `git diff --stat` of the card's branch vs its base, plus any
        # uncommitted changes in an active worktree. States that can't
        # produce a diff (none / not-git / missing) return 200 with a
        # message rather than an error so the route never 500s.
        store = _store()
        card = _get_card_or_404(store, card_id)
        state, _path = worktree_state(board_path, card)
        base: dict[str, Any] = {
            "card_id": card.id,
            "state": state,
            "branch": card.worktree_branch,
            "base_commit": card.worktree_base_commit,
            "diff": None,
            "truncated": False,
            "message": None,
        }
        if state not in ("active", "detached"):
            base["message"] = _DIFF_STATE_MESSAGES.get(
                state, f"worktree state {state!r} has no diff"
            )
            return base
        git_root = find_git_root_optional(board_path)
        if git_root is None:
            # worktree_state already classifies this as not-git, but be
            # defensive against a layout where the probe disagrees.
            base["state"] = "not-git"
            base["message"] = _DIFF_STATE_MESSAGES["not-git"]
            return base
        # Read-only manager: never touch `.git/info/exclude`. `diff_summary`
        # only shells out to `git` (with a timeout) — no repo mutation.
        mgr = WorktreeManager.for_project(git_root, manage_exclude=False)
        try:
            diff = mgr.diff_summary(card.id, card.worktree_base_commit or "")
        except WorktreeDiffError as exc:
            base["message"] = str(exc)
            return base
        encoded = diff.encode("utf-8", "replace")
        if len(encoded) > _DIFF_MAX_BYTES:
            base["diff"] = encoded[:_DIFF_MAX_BYTES].decode("utf-8", "ignore")
            base["truncated"] = True
            base["message"] = (
                f"diff is {len(encoded)} bytes; showing the first "
                f"{_DIFF_MAX_BYTES}. Run `kanban worktree diff "
                f"{card.id[:8]}` for the full output."
            )
        else:
            base["diff"] = diff
        return base

    @app.get("/api/cards/{card_id}/traces")
    def api_list_traces(card_id: str) -> dict[str, Any]:
        # Web equivalent of `kanban traces` (the UI labels this surface
        # "Transcripts"; the API path stays `traces` to match the CLI).
        # `store.list_traces` only orders by filename glob, which mixes
        # roles — sort explicitly by timestamp so the first entry is the
        # true latest. Read-only; no `--enable-writes` needed.
        store = _store()
        _get_card_or_404(store, card_id)
        traces = sorted(
            store.list_traces(card_id), key=lambda t: t.at, reverse=True
        )
        git_root = find_git_root_optional(board_path) if traces else None
        return {
            "card_id": card_id,
            "traces": [
                {
                    "trace_id": Path(t.path).name,
                    "role": t.role.value,
                    "at": t.at.isoformat(),
                    "path": t.path,
                    "display_path": display_path(
                        t.path, board_dir=board_path, git_root=git_root
                    ),
                    "size": t.size,
                }
                for t in traces
            ],
        }

    @app.get("/api/cards/{card_id}/traces/{trace_id}/file")
    def api_trace_file(card_id: str, trace_id: str):
        if "\\" in card_id or ".." in card_id:
            raise HTTPException(status_code=400, detail="invalid card id")
        if "/" in trace_id or "\\" in trace_id or trace_id in ("", ".", ".."):
            raise HTTPException(status_code=400, detail="invalid trace id")
        store = _store()
        _get_card_or_404(store, card_id)
        # Resolve the trace by exact filename match against the store's own
        # listing rather than trusting the path segment to point at a real
        # file. `match.path` is then a known transcript under raw/<card>/.
        match = next(
            (t for t in store.list_traces(card_id) if Path(t.path).name == trace_id),
            None,
        )
        if match is None:
            raise HTTPException(status_code=404, detail="trace not found")
        # text/plain + inline so "open in new tab" renders the transcript
        # in the browser instead of triggering a download.
        return serve_file_under_root(
            Path(match.path),
            (store.raw_root / card_id).resolve(),
            media_type="text/plain; charset=utf-8",
            inline=True,
        )

    @app.get("/api/cards/{card_id}/artifacts")
    def api_list_artifacts(card_id: str) -> dict[str, Any]:
        # Validate the card exists so a typo'd id surfaces as 404 instead
        # of an empty list — matches /api/cards/{card_id}'s contract.
        _get_card_or_404(_store(), card_id)
        snapshots = list_artifact_snapshots(
            card_id, artifacts_root_for(board_path)
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

        root = artifacts_root_for(board_path).resolve()
        snap_dir = (root / card_id / snapshot).resolve()
        try:
            snap_dir.relative_to(root)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid snapshot path")
        if not snap_dir.is_dir():
            raise HTTPException(status_code=404, detail="snapshot not found")

        return serve_file_under_root(snap_dir / path, snap_dir)

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
            "events": [annotate_event(event_to_dict(e)) for e in events],
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
