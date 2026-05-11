"""Tests for the artifact-browsing surface on ``kanban.web``.

These exercise ``GET /api/cards/{id}/artifacts`` (snapshot listing) and
``GET /api/cards/{id}/artifacts/{snapshot}/file`` (file fetch), focusing
on the HTTP contract — listing shape, ordering, cap enforcement, and the
path-validation guards that protect the read-only browser surface.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kanban.models import Card, CardPriority
from kanban.store_markdown import MarkdownBoardStore
from kanban.web import create_app
from kanban.web_artifacts import (
    ARTIFACT_FILE_MAX_BYTES,
    artifacts_root_for,
    list_artifact_snapshots,
)


# Snapshot dir names match the ``artifacts-<utc-stamp>`` pattern that
# ``WorktreeManager._save_artifacts`` emits. Two distinct stamps so we
# can pin newest-first ordering without depending on filesystem mtime.
SNAP_OLD = "artifacts-20260101T010101000001Z"
SNAP_NEW = "artifacts-20260202T020202000002Z"


@pytest.fixture
def board(tmp_path: Path) -> Path:
    board_dir = tmp_path / "workspace" / "board"
    board_dir.mkdir(parents=True)
    return board_dir


@pytest.fixture
def raw_root(board: Path) -> Path:
    # Mirror WorktreeManager's layout so artifacts_root_for picks it up.
    root = board.parent / "raw"
    root.mkdir(parents=True)
    return root


@pytest.fixture
def client(board: Path) -> TestClient:
    return TestClient(create_app(board))


@pytest.fixture
def card_with_snapshot(board: Path, raw_root: Path):
    """Seed a card and one populated artifact snapshot.

    Returns ``(card_id, snapshot_dir)``. Tests that need additional
    files write into ``snapshot_dir`` directly.
    """
    store = MarkdownBoardStore(board)
    card = store.add_card(
        Card(title="art card", goal="g", priority=CardPriority.MEDIUM)
    )
    snap = raw_root / card.id / SNAP_NEW
    snap.mkdir(parents=True)
    (snap / "report.md").write_text("# hello\n", encoding="utf-8")
    (snap / "logs").mkdir()
    (snap / "logs" / "run.log").write_text("ok\n", encoding="utf-8")
    return card.id, snap


def test_artifacts_root_follows_board_parent(tmp_path: Path) -> None:
    board = tmp_path / "workspace" / "board"
    assert artifacts_root_for(board) == tmp_path / "workspace" / "raw"


def test_list_empty_when_no_raw_dir(client: TestClient, board: Path) -> None:
    store = MarkdownBoardStore(board)
    card = store.add_card(Card(title="t", goal="g", priority=CardPriority.LOW))
    r = client.get(f"/api/cards/{card.id}/artifacts")
    assert r.status_code == 200
    assert r.json() == {"card_id": card.id, "snapshots": []}


def test_list_unknown_card_returns_404(client: TestClient) -> None:
    r = client.get("/api/cards/does-not-exist/artifacts")
    assert r.status_code == 404


def test_list_returns_files_and_totals(
    client: TestClient, card_with_snapshot
) -> None:
    card_id, _snap = card_with_snapshot
    r = client.get(f"/api/cards/{card_id}/artifacts")
    assert r.status_code == 200
    data = r.json()
    assert data["card_id"] == card_id
    assert len(data["snapshots"]) == 1
    snap = data["snapshots"][0]
    assert snap["snapshot"] == SNAP_NEW
    assert snap["file_count"] == 2
    assert snap["total_bytes"] == len("# hello\n") + len("ok\n")
    paths = sorted(f["path"] for f in snap["files"])
    assert paths == ["logs/run.log", "report.md"]


def test_list_includes_snapshot_abs_path(
    client: TestClient, card_with_snapshot
) -> None:
    card_id, snap = card_with_snapshot
    r = client.get(f"/api/cards/{card_id}/artifacts")
    assert r.status_code == 200
    rec = r.json()["snapshots"][0]
    # Absolute, points at the snapshot dir, and the UI joins file paths
    # onto it for the "copy path" affordance.
    assert rec["abs_path"] == str(snap.resolve())
    assert Path(rec["abs_path"]).is_absolute()


def test_multiple_snapshots_listed_newest_first(
    client: TestClient, raw_root: Path, board: Path
) -> None:
    store = MarkdownBoardStore(board)
    card = store.add_card(Card(title="t", goal="g", priority=CardPriority.LOW))
    for stamp, body in ((SNAP_OLD, "old\n"), (SNAP_NEW, "new\n")):
        snap = raw_root / card.id / stamp
        snap.mkdir(parents=True)
        (snap / "f.txt").write_text(body, encoding="utf-8")
    r = client.get(f"/api/cards/{card.id}/artifacts")
    assert r.status_code == 200
    names = [s["snapshot"] for s in r.json()["snapshots"]]
    assert names == [SNAP_NEW, SNAP_OLD]


def test_list_skips_non_artifact_dirs(
    client: TestClient, raw_root: Path, board: Path
) -> None:
    # WorktreeManager only writes ``artifacts-<stamp>`` dirs; the listing
    # must ignore anything else that may appear under raw/<card>/ (e.g.
    # the role-stamped transcripts that ``MarkdownBoardStore`` writes
    # alongside artifacts when ``raw_response`` is set).
    store = MarkdownBoardStore(board)
    card = store.add_card(Card(title="t", goal="g", priority=CardPriority.LOW))
    card_dir = raw_root / card.id
    card_dir.mkdir(parents=True)
    (card_dir / "worker-20260101T010101000001Z.md").write_text("trace")
    (card_dir / "artifacts-noisy").mkdir()  # malformed stamp
    snap = card_dir / SNAP_NEW
    snap.mkdir()
    (snap / "ok.txt").write_text("ok")

    r = client.get(f"/api/cards/{card.id}/artifacts")
    assert r.status_code == 200
    names = [s["snapshot"] for s in r.json()["snapshots"]]
    assert names == [SNAP_NEW]


def test_list_skips_symlinks(
    client: TestClient, card_with_snapshot
) -> None:
    card_id, snap = card_with_snapshot
    # Symlink pointing inside the snapshot — listing still skips it
    # because the file-fetch route refuses to serve symlinks. Listing
    # them would advertise paths that 403.
    (snap / "alias.md").symlink_to(snap / "report.md")
    r = client.get(f"/api/cards/{card_id}/artifacts")
    files = {f["path"] for f in r.json()["snapshots"][0]["files"]}
    assert "alias.md" not in files
    assert "report.md" in files


def test_file_serves_text(client: TestClient, card_with_snapshot) -> None:
    card_id, _snap = card_with_snapshot
    r = client.get(
        f"/api/cards/{card_id}/artifacts/{SNAP_NEW}/file",
        params={"path": "report.md"},
    )
    assert r.status_code == 200
    assert r.text == "# hello\n"


def test_file_serves_nested(client: TestClient, card_with_snapshot) -> None:
    card_id, _snap = card_with_snapshot
    r = client.get(
        f"/api/cards/{card_id}/artifacts/{SNAP_NEW}/file",
        params={"path": "logs/run.log"},
    )
    assert r.status_code == 200
    assert r.text == "ok\n"


def test_file_rejects_traversal(
    client: TestClient, card_with_snapshot
) -> None:
    card_id, _snap = card_with_snapshot
    r = client.get(
        f"/api/cards/{card_id}/artifacts/{SNAP_NEW}/file",
        params={"path": "../../etc/passwd"},
    )
    assert r.status_code == 400


def test_file_rejects_absolute_path(
    client: TestClient, card_with_snapshot
) -> None:
    card_id, _snap = card_with_snapshot
    r = client.get(
        f"/api/cards/{card_id}/artifacts/{SNAP_NEW}/file",
        params={"path": "/etc/passwd"},
    )
    assert r.status_code == 400


def test_file_rejects_invalid_snapshot_name(
    client: TestClient, card_with_snapshot
) -> None:
    card_id, _snap = card_with_snapshot
    r = client.get(
        f"/api/cards/{card_id}/artifacts/not-a-snapshot/file",
        params={"path": "report.md"},
    )
    assert r.status_code == 400


def test_file_404_for_missing_snapshot(
    client: TestClient, card_with_snapshot
) -> None:
    card_id, _snap = card_with_snapshot
    r = client.get(
        f"/api/cards/{card_id}/artifacts/{SNAP_OLD}/file",
        params={"path": "report.md"},
    )
    # SNAP_OLD passes the regex but the directory doesn't exist for
    # this card, so we 404 (snapshot missing) rather than 400.
    assert r.status_code == 404


def test_file_refuses_symlink_leaf(
    client: TestClient, card_with_snapshot, tmp_path: Path
) -> None:
    card_id, snap = card_with_snapshot
    # Symlink leaf pointing inside the snapshot — still refused, the
    # contract is "no symlinks served" regardless of target.
    (snap / "alias.md").symlink_to(snap / "report.md")
    r = client.get(
        f"/api/cards/{card_id}/artifacts/{SNAP_NEW}/file",
        params={"path": "alias.md"},
    )
    assert r.status_code == 403


def test_file_refuses_symlink_escaping_snapshot(
    client: TestClient, card_with_snapshot, tmp_path: Path
) -> None:
    card_id, snap = card_with_snapshot
    secret = tmp_path / "secret.txt"
    secret.write_text("nope")
    (snap / "escape.txt").symlink_to(secret)
    r = client.get(
        f"/api/cards/{card_id}/artifacts/{SNAP_NEW}/file",
        params={"path": "escape.txt"},
    )
    # Caught by the leaf-symlink check before we'd even resolve the
    # target — either way the body of secret.txt is never returned.
    assert r.status_code in (400, 403)


def test_file_413_when_over_cap(
    client: TestClient, card_with_snapshot
) -> None:
    card_id, snap = card_with_snapshot
    big = snap / "big.bin"
    # We don't want to actually allocate 8 MiB; truncate sets the
    # apparent size without writing real bytes on most filesystems.
    with big.open("wb") as fp:
        fp.truncate(ARTIFACT_FILE_MAX_BYTES + 1)
    r = client.get(
        f"/api/cards/{card_id}/artifacts/{SNAP_NEW}/file",
        params={"path": "big.bin"},
    )
    assert r.status_code == 413


def test_helper_handles_missing_root(tmp_path: Path) -> None:
    # list_artifact_snapshots is the inner contract; make sure it
    # short-circuits cleanly when the raw root isn't materialized at
    # all (the common case for fresh boards).
    assert list_artifact_snapshots("any-id", tmp_path / "raw") == []
