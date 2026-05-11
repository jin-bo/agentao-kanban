"""Tests for the transcript-browsing surface on ``kanban.web``.

These exercise ``GET /api/cards/{id}/traces`` (transcript listing) and
``GET /api/cards/{id}/traces/{trace_id}/file`` (raw transcript fetch).
The API path segment is ``traces`` (matching ``kanban traces``); the UI
labels the surface "Transcripts". Focus areas: the listing must be sorted
newest-first by timestamp (not filename glob, which mixes roles), the
file route validates the id and refuses symlinks/oversized payloads, and
nothing here requires ``--enable-writes``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kanban.models import Card, CardPriority
from kanban.store_markdown import MarkdownBoardStore
from kanban.web import _ARTIFACT_FILE_MAX_BYTES, create_app


# Two transcript stamps. Note the *filename* glob order (planner < worker
# alphabetically) is the reverse of the *timestamp* order we set up here
# (worker is newer) — so a test that asserts newest-first ordering proves
# the endpoint sorts by ``TraceInfo.at`` rather than relying on glob order.
TRACE_PLANNER = "planner-20260101T010101000001Z.md"  # older
TRACE_WORKER = "worker-20260202T020202000002Z.md"  # newer (latest)


@pytest.fixture
def board(tmp_path: Path) -> Path:
    board_dir = tmp_path / "workspace" / "board"
    board_dir.mkdir(parents=True)
    return board_dir


@pytest.fixture
def raw_root(board: Path) -> Path:
    root = board.parent / "raw"
    root.mkdir(parents=True)
    return root


@pytest.fixture
def client(board: Path) -> TestClient:
    return TestClient(create_app(board))


@pytest.fixture
def card_with_traces(board: Path, raw_root: Path):
    """Seed a card and two retained transcripts (older planner, newer worker).

    Returns ``(card_id, card_raw_dir)``.
    """
    store = MarkdownBoardStore(board)
    card = store.add_card(
        Card(title="trace card", goal="g", priority=CardPriority.MEDIUM)
    )
    card_dir = raw_root / card.id
    card_dir.mkdir(parents=True)
    (card_dir / TRACE_PLANNER).write_text("planner transcript\n", encoding="utf-8")
    (card_dir / TRACE_WORKER).write_text("worker transcript body\n", encoding="utf-8")
    return card.id, card_dir


def test_traces_empty_when_no_raw_dir(client: TestClient, board: Path) -> None:
    store = MarkdownBoardStore(board)
    card = store.add_card(Card(title="t", goal="g", priority=CardPriority.LOW))
    r = client.get(f"/api/cards/{card.id}/traces")
    assert r.status_code == 200
    assert r.json() == {"card_id": card.id, "traces": []}


def test_traces_unknown_card_404(client: TestClient) -> None:
    r = client.get("/api/cards/does-not-exist/traces")
    assert r.status_code == 404


def test_traces_sorted_newest_first(client: TestClient, card_with_traces) -> None:
    card_id, _dir = card_with_traces
    r = client.get(f"/api/cards/{card_id}/traces")
    assert r.status_code == 200
    data = r.json()
    assert data["card_id"] == card_id
    ids = [t["trace_id"] for t in data["traces"]]
    # Newest (worker, 2026-02-02) before older (planner, 2026-01-01),
    # which is the *opposite* of the filename glob order.
    assert ids == [TRACE_WORKER, TRACE_PLANNER]
    latest = data["traces"][0]
    assert latest["role"] == "worker"
    assert latest["at"].startswith("2026-02-02T02:02:02")
    assert latest["size"] == len("worker transcript body\n")
    assert latest["path"].endswith(f"/{card_id}/{TRACE_WORKER}")
    # Board isn't a Git repo here, so the display path is relative to the
    # board's parent: ``raw/<card>/<trace>``.
    assert latest["display_path"] == f"raw/{card_id}/{TRACE_WORKER}"


def test_trace_file_serves_text(client: TestClient, card_with_traces) -> None:
    card_id, _dir = card_with_traces
    r = client.get(f"/api/cards/{card_id}/traces/{TRACE_WORKER}/file")
    assert r.status_code == 200
    assert r.text == "worker transcript body\n"
    assert r.headers["content-type"].startswith("text/plain")
    # Inline so "open in new tab" renders rather than downloads, but the
    # filename is still advertised for "Save As".
    cd = r.headers["content-disposition"]
    assert cd.startswith("inline")
    assert TRACE_WORKER in cd


def test_trace_file_unknown_trace_id_404(client: TestClient, card_with_traces) -> None:
    card_id, _dir = card_with_traces
    r = client.get(f"/api/cards/{card_id}/traces/worker-29990101T000000000000Z.md/file")
    assert r.status_code == 404


def test_trace_file_unknown_card_404(client: TestClient) -> None:
    r = client.get(f"/api/cards/nope/traces/{TRACE_WORKER}/file")
    assert r.status_code == 404


def test_trace_file_rejects_non_transcript_file(client: TestClient, card_with_traces) -> None:
    # A stray file under raw/<card>/ that isn't a ``<role>-<stamp>.md``
    # transcript isn't in ``list_traces``, so it can't be fetched.
    card_id, card_dir = card_with_traces
    (card_dir / "notes.txt").write_text("secret-ish", encoding="utf-8")
    r = client.get(f"/api/cards/{card_id}/traces/notes.txt/file")
    assert r.status_code == 404


def test_trace_file_refuses_symlink_leaf(client: TestClient, card_with_traces) -> None:
    card_id, card_dir = card_with_traces
    # A symlink whose name still matches the transcript pattern would show
    # up in the listing; serving it would change the trust boundary, so
    # the file route refuses it.
    link = card_dir / "worker-20260303T030303000003Z.md"
    link.symlink_to(card_dir / TRACE_WORKER)
    r = client.get(f"/api/cards/{card_id}/traces/{link.name}/file")
    assert r.status_code == 403


def test_trace_file_413_when_over_cap(client: TestClient, card_with_traces) -> None:
    card_id, card_dir = card_with_traces
    big = card_dir / "worker-20260404T040404000004Z.md"
    with big.open("wb") as fp:
        fp.truncate(_ARTIFACT_FILE_MAX_BYTES + 1)
    r = client.get(f"/api/cards/{card_id}/traces/{big.name}/file")
    assert r.status_code == 413


def test_traces_display_path_under_git_root(tmp_path: Path) -> None:
    # Standard layout: board at <git_root>/workspace/board, transcripts at
    # <git_root>/workspace/raw/<card>/... — the display path should be
    # relative to the git root, i.e. ``workspace/raw/<card>/...``.
    import subprocess

    repo = tmp_path / "repo"
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    board = repo / "workspace" / "board"
    store = MarkdownBoardStore(board)
    card = store.add_card(Card(title="t", goal="g", priority=CardPriority.LOW))
    card_dir = repo / "workspace" / "raw" / card.id
    card_dir.mkdir(parents=True)
    (card_dir / TRACE_WORKER).write_text("body\n", encoding="utf-8")

    r = TestClient(create_app(board)).get(f"/api/cards/{card.id}/traces")
    assert r.status_code == 200
    disp = r.json()["traces"][0]["display_path"]
    assert disp == f"workspace/raw/{card.id}/{TRACE_WORKER}"
