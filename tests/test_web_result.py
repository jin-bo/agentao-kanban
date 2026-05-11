"""Tests for ``GET /api/cards/{card_id}/result`` — the Web equivalent of
``kanban result --json``.

The point of the endpoint is that an operator opening a card in the Web
UI can answer "did this card produce a result, where is it, what's the
next step" without dropping to the CLI. So the key invariant under test
is *parity*: the Web payload carries the same field values as
``kanban result --json`` for the same card, plus the Web-only
``*_display_paths`` maps.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kanban.cli import main as cli_main
from kanban.models import Card, CardStatus
from kanban.store_markdown import MarkdownBoardStore
from kanban.web import create_app


def _init_repo(path: Path) -> Path:
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=path, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=path, check=True, capture_output=True,
    )
    (path / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=path, check=True, capture_output=True,
    )
    return path


def _cli_result_json(board: Path, card_id: str, capsys) -> dict:
    capsys.readouterr()
    rc = cli_main(["--board", str(board), "result", card_id, "--json"])
    assert rc == 0
    return json.loads(capsys.readouterr().out.strip())


def _norm_paths(paths: list[str]) -> set[Path]:
    """Normalize for comparison: the CLI keeps ``--board`` unresolved
    while the web layer ``.resolve()``s it, so on platforms where TMPDIR
    sits behind a symlink (macOS ``/var`` -> ``/private/var``) the raw
    strings differ even though they point at the same file."""
    return {Path(p).resolve() for p in paths}


def test_result_minimal_card(tmp_path: Path) -> None:
    board = tmp_path / "board"
    store = MarkdownBoardStore(board)
    card = store.add_card(Card(title="fresh", goal="g"))
    client = TestClient(create_app(board))

    r = client.get(f"/api/cards/{card.id}/result")
    assert r.status_code == 200
    payload = r.json()
    assert payload["card_id"] == card.id
    assert payload["title"] == "fresh"
    assert payload["status"] == "inbox"
    assert payload["worktree"]["state"] in ("none", "not-git")
    assert payload["worktree"]["branch"] is None
    assert payload["artifacts"] == []
    assert payload["transcripts"] == []
    assert payload["next_steps"] == []
    # Web-only additions, always present even when empty.
    assert payload["artifact_display_paths"] == {}
    assert payload["transcript_display_paths"] == {}


def test_result_unknown_card_404(tmp_path: Path) -> None:
    board = tmp_path / "board"
    MarkdownBoardStore(board).add_card(Card(title="x", goal="g"))
    client = TestClient(create_app(board))
    r = client.get("/api/cards/does-not-exist/result")
    assert r.status_code == 404


def test_result_matches_cli_for_card_with_worktree_and_transcript(
    tmp_path: Path, capsys
) -> None:
    repo = _init_repo(tmp_path / "repo")
    board = repo / "workspace" / "board"
    store = MarkdownBoardStore(board)
    card_id = "cccccccc-0000-0000-0000-000000000000"
    store.add_card(
        Card(
            id=card_id,
            title="with worktree",
            goal="exercise result fields",
            worktree_branch=f"kanban/{card_id}",
            worktree_base_commit="abc123def456",
            outputs={
                "last": {
                    "summary": "implemented foo",
                    "output": ["workspace/reports/foo.md"],
                }
            },
        )
    )
    # Synthesize a raw transcript under the store's raw root so both the
    # CLI (git_root/workspace/raw == board.parent/raw here) and the web
    # layer pick it up.
    trace = board.parent / "raw" / card_id / "worker-20260509T120000000000Z.md"
    trace.parent.mkdir(parents=True, exist_ok=True)
    trace.write_text("transcript", encoding="utf-8")

    cli_payload = _cli_result_json(board, card_id, capsys)
    web_payload = TestClient(create_app(board)).get(
        f"/api/cards/{card_id}/result"
    ).json()

    # Scalar / structured fields: identical.
    for key in ("card_id", "title", "status", "blocked_reason", "summary",
                "outputs", "worktree", "next_steps"):
        assert web_payload[key] == cli_payload[key], key
    # Path arrays: same files (normalized for symlinked TMPDIR).
    assert _norm_paths(web_payload["artifacts"]) == _norm_paths(cli_payload["artifacts"])
    assert _norm_paths(web_payload["transcripts"]) == _norm_paths(cli_payload["transcripts"])
    assert any(trace.name in t for t in web_payload["transcripts"])
    # Web-only display maps cover every absolute path returned.
    assert set(web_payload["transcript_display_paths"]) == set(web_payload["transcripts"])
    for abs_p, disp in web_payload["transcript_display_paths"].items():
        assert not Path(disp).is_absolute()
        assert abs_p.endswith(disp)
    # Standard layout -> display path is relative to the git root.
    assert all(
        d.startswith("workspace/raw/")
        for d in web_payload["transcript_display_paths"].values()
    )


def test_result_detached_state_with_git_repo(tmp_path: Path, capsys) -> None:
    repo = _init_repo(tmp_path / "repo")
    board = repo / "workspace" / "board"
    store = MarkdownBoardStore(board)
    card_id = "abcd1234-0000-0000-0000-000000000000"
    store.add_card(
        Card(id=card_id, title="detached", goal="branch only", status=CardStatus.DONE)
    )
    subprocess.run(
        ["git", "branch", f"kanban/{card_id}"],
        cwd=repo, check=True, capture_output=True,
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()
    store.update_card(
        card_id, worktree_branch=f"kanban/{card_id}", worktree_base_commit=head
    )

    web_payload = TestClient(create_app(board)).get(
        f"/api/cards/{card_id}/result"
    ).json()
    cli_payload = _cli_result_json(board, card_id, capsys)

    assert web_payload["worktree"]["state"] == "detached"
    assert web_payload["worktree"] == cli_payload["worktree"]
    joined = " ".join(web_payload["next_steps"])
    assert "kanban worktree diff" in joined
    assert f"git merge kanban/{card_id}" in joined
    assert web_payload["next_steps"] == cli_payload["next_steps"]


def test_result_endpoint_is_side_effect_free(tmp_path: Path) -> None:
    """The read-only result endpoint must not touch the repo — in
    particular it must not create/append ``.git/info/exclude`` while
    probing a card's worktree state (regression: WorktreeManager's
    initializer used to do that bookkeeping unconditionally)."""
    repo = _init_repo(tmp_path / "repo")
    exclude = repo / ".git" / "info" / "exclude"
    if exclude.exists():
        exclude.unlink()
    board = repo / "workspace" / "board"
    store = MarkdownBoardStore(board)
    card_id = "deadbeef-0000-0000-0000-000000000000"
    store.add_card(
        Card(
            id=card_id,
            title="has worktree meta",
            goal="g",
            worktree_branch=f"kanban/{card_id}",
            worktree_base_commit="abc123",
        )
    )

    r = TestClient(create_app(board)).get(f"/api/cards/{card_id}/result")
    assert r.status_code == 200
    # state may be "missing" (no real branch) — what matters is no write.
    assert not exclude.exists()


def test_result_non_standard_board_layout_does_not_500(tmp_path: Path, capsys) -> None:
    """Board dir not at ``<git_root>/workspace/board``: the CLI resolves
    artifacts under ``git_root/workspace/raw`` while the web endpoint uses
    the store's ``board_dir.parent/raw``. They may legitimately diverge;
    the contract is just that neither crashes."""
    repo = _init_repo(tmp_path / "repo")
    board = repo / "weird" / "place" / "board"  # not workspace/board
    store = MarkdownBoardStore(board)
    card = store.add_card(Card(title="odd layout", goal="g"))

    r = TestClient(create_app(board)).get(f"/api/cards/{card.id}/result")
    assert r.status_code == 200
    assert r.json()["card_id"] == card.id

    cli_payload = _cli_result_json(board, card.id, capsys)
    assert cli_payload["card_id"] == card.id


@pytest.mark.parametrize("status", [CardStatus.INBOX, CardStatus.DONE, CardStatus.BLOCKED])
def test_result_parity_across_statuses(tmp_path: Path, capsys, status: CardStatus) -> None:
    repo = _init_repo(tmp_path / f"repo-{status.value}")
    board = repo / "workspace" / "board"
    store = MarkdownBoardStore(board)
    card = store.add_card(Card(title=f"s-{status.value}", goal="g"))
    if status is not CardStatus.INBOX:
        if status is CardStatus.BLOCKED:
            store.update_card(card.id, blocked_reason="stuck")
        store.move_card(card.id, status, "test transition")

    web_payload = TestClient(create_app(board)).get(
        f"/api/cards/{card.id}/result"
    ).json()
    cli_payload = _cli_result_json(board, card.id, capsys)
    for key in ("card_id", "title", "status", "blocked_reason", "summary",
                "outputs", "worktree", "artifacts", "transcripts", "next_steps"):
        assert web_payload[key] == cli_payload[key], (status, key)
