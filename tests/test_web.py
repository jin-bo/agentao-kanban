"""Tests for ``kanban.web`` — the read-only FastAPI board.

These exercise the HTTP surface through :class:`fastapi.testclient.TestClient`
(which uses httpx under the hood). The service layer wraps the same
:class:`MarkdownBoardStore` the CLI and MCP server use, so the tests focus
on the HTTP contract (shape, filtering, 404) rather than re-validating
store semantics.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kanban.models import Card, CardPriority, CardStatus
from kanban.store_markdown import MarkdownBoardStore
from kanban.web import COLUMN_ORDER, create_app


@pytest.fixture
def board(tmp_path: Path) -> Path:
    board_dir = tmp_path / "board"
    board_dir.mkdir()
    return board_dir


@pytest.fixture
def client(board: Path) -> TestClient:
    app = create_app(board, poll_interval_ms=1234)
    return TestClient(app)


@pytest.fixture
def seeded(board: Path):
    store = MarkdownBoardStore(board)
    a = store.add_card(Card(title="A card", goal="g", priority=CardPriority.HIGH))
    b = store.add_card(Card(title="B card", goal="g", priority=CardPriority.MEDIUM))
    store.move_card(b.id, CardStatus.READY, "ready for work")
    return store, a, b


def test_healthz(client: TestClient, board: Path) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    payload = r.json()
    assert payload["status"] == "ok"
    assert payload["board_dir"].endswith("board")
    assert payload["poll_interval_ms"] == 1234


def test_requests_against_missing_board_dont_create_directories(
    tmp_path: Path,
) -> None:
    # Read-only contract: hitting the API against a board that has never
    # been touched must not materialize anything on disk.
    board_dir = tmp_path / "ghost-board"
    app = create_app(board_dir)
    client = TestClient(app)

    assert client.get("/healthz").status_code == 200
    assert client.get("/api/board").status_code == 200
    assert client.get("/api/events").status_code == 200

    assert not board_dir.exists()


def test_board_has_seven_columns_in_fixed_order(client: TestClient) -> None:
    r = client.get("/api/board")
    assert r.status_code == 200
    data = r.json()
    assert len(data["columns"]) == 7
    statuses = [col["status"] for col in data["columns"]]
    assert statuses == [s.value for s in COLUMN_ORDER]
    for col in data["columns"]:
        assert col["count"] == len(col["cards"])
    # Runtime keys present even when runtime/ does not exist yet
    assert data["runtime"] == {"claims": [], "workers": []}


def test_board_groups_cards_by_status(client: TestClient, seeded) -> None:
    _, a, b = seeded
    data = client.get("/api/board").json()
    by_status = {col["status"]: col for col in data["columns"]}
    inbox_ids = [c["id"] for c in by_status["inbox"]["cards"]]
    ready_ids = [c["id"] for c in by_status["ready"]["cards"]]
    assert a.id in inbox_ids
    assert b.id in ready_ids
    # Priority/title carried through to the column payload.
    entry = next(c for c in by_status["inbox"]["cards"] if c["id"] == a.id)
    assert entry["title"] == "A card"
    assert entry["priority"] == "HIGH"


def test_card_detail_returns_full_card_and_events(
    client: TestClient, seeded
) -> None:
    _, a, _ = seeded
    r = client.get(f"/api/cards/{a.id}")
    assert r.status_code == 200
    card = r.json()
    assert card["id"] == a.id
    assert card["title"] == "A card"
    # acceptance_criteria is part of the full card_to_dict shape
    assert "acceptance_criteria" in card
    # recent_events is the per-card event tail
    assert isinstance(card["recent_events"], list)
    assert any("created" in e["message"].lower() for e in card["recent_events"])


def test_card_detail_404_for_unknown_id(client: TestClient) -> None:
    r = client.get("/api/cards/does-not-exist")
    assert r.status_code == 404


def test_events_tail_and_filter_by_card(client: TestClient, seeded) -> None:
    _, a, b = seeded
    r = client.get("/api/events", params={"limit": 50})
    assert r.status_code == 200
    events = r.json()["events"]
    assert len(events) >= 3  # two creates + one move
    # Filter by card_id keeps only that card's events
    filtered = client.get(
        "/api/events", params={"card_id": a.id, "limit": 50}
    ).json()["events"]
    assert all(e["card_id"] == a.id for e in filtered)
    assert all("display_tag" in e for e in filtered)


def test_events_execution_only_filters_non_execution(
    client: TestClient, seeded
) -> None:
    r = client.get("/api/events", params={"execution_only": "true"})
    assert r.status_code == 200
    # Seeded cards never ran an executor, so no execution events exist.
    assert r.json()["events"] == []


def test_events_role_filter_rejects_bad_value(client: TestClient) -> None:
    r = client.get("/api/events", params={"role": "junk"})
    assert r.status_code == 400


def test_runtime_missing_returns_empty_lists(client: TestClient) -> None:
    # Board has no runtime/ dir yet; list_claims/list_workers return []
    data = client.get("/api/board").json()
    assert data["runtime"]["claims"] == []
    assert data["runtime"]["workers"] == []


def test_index_injects_poll_interval(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")
    body = r.text
    # Config is interpolated into the HTML at request time.
    assert "pollIntervalMs: 1234" in body
    assert "__POLL_INTERVAL_MS__" not in body


def test_static_assets_served(client: TestClient) -> None:
    r = client.get("/static/app.js")
    assert r.status_code == 200
    assert "fetchJSON" in r.text
    r = client.get("/static/styles.css")
    assert r.status_code == 200
