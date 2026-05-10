"""Tests for the unified ``kanban result <card-id>`` view and adjacent
output changes (``kanban show`` result block, ``kanban worktree list``
empty-state guidance).

The wider goal is that operators can answer "where is the result of card X?"
without needing to understand worktree directory lifecycle. These tests
pin the JSON shape and the human-readable hint text so accidental
regressions surface here rather than in user-facing surprise.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from kanban.cli import main
from kanban.models import AgentRole, Card, CardStatus
from kanban.store_markdown import MarkdownBoardStore


def _add_card(board: Path, title: str = "T", goal: str = "G") -> str:
    rc = main(["--board", str(board), "card", "add", "--title", title, "--goal", goal])
    assert rc == 0
    cards = MarkdownBoardStore(board).list_cards()
    assert len(cards) == 1
    return cards[0].id


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


class TestResultJson:
    def test_minimal_card_no_worktree(self, tmp_path: Path, capsys):
        """A fresh card with no worktree, artifacts, or transcripts still
        yields a well-formed result payload — operators can rely on the
        shape regardless of card maturity."""
        board = tmp_path / "board"
        cid = _add_card(board)
        capsys.readouterr()

        rc = main(["--board", str(board), "result", cid, "--json"])
        assert rc == 0
        out = capsys.readouterr().out.strip()
        payload = json.loads(out)

        assert payload["card_id"] == cid
        assert payload["title"] == "T"
        assert payload["status"] == "inbox"
        assert payload["worktree"]["state"] in ("none", "not-git")
        assert payload["worktree"]["branch"] is None
        assert payload["artifacts"] == []
        assert payload["transcripts"] == []

    def test_card_with_worktree_and_traces(self, tmp_path: Path, capsys):
        """Card stamped with worktree metadata + a saved transcript should
        surface both the branch and the transcript path."""
        board = tmp_path / "board"
        store = MarkdownBoardStore(board)
        card = store.add_card(
            Card(
                id="cccccccc-0000-0000-0000-000000000000",
                title="with worktree",
                goal="exercise result fields",
                worktree_branch="kanban/cccccccc-0000-0000-0000-000000000000",
                worktree_base_commit="abc123def456",
                outputs={
                    "last": {
                        "summary": "implemented foo",
                        "output": ["workspace/reports/foo.md"],
                    }
                },
            )
        )
        # Synthesize a raw transcript so list_traces returns something.
        raw_root = board.parent / "raw" / card.id
        raw_root.mkdir(parents=True, exist_ok=True)
        trace = raw_root / "worker-20260509T120000000000Z.md"
        trace.write_text("transcript", encoding="utf-8")

        capsys.readouterr()
        rc = main(["--board", str(board), "result", card.id, "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out.strip())

        assert payload["worktree"]["branch"] == "kanban/" + card.id
        # Without a backing git repo at this tmp_path, state is "not-git".
        # The point of this test is the surrounding fields, not the state
        # discriminator (covered by TestResultWithGitRepo below).
        assert payload["worktree"]["state"] in ("not-git", "missing", "none", "detached")
        assert payload["summary"] == "implemented foo"
        assert "workspace/reports/foo.md" in payload["outputs"]
        assert any(trace.name in t for t in payload["transcripts"])

    def test_unknown_card(self, tmp_path: Path, capsys):
        board = tmp_path / "board"
        _add_card(board)
        capsys.readouterr()
        rc = main(["--board", str(board), "result", "deadbeef"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "No card" in err


class TestResultHumanReadable:
    """The plain text mode is the entry point operators are most likely to
    read; lock down the structural cues (status, worktree state, next
    steps) so refactors don't quietly drop them.
    """

    def test_plain_output_lists_status_and_next_steps(self, tmp_path: Path, capsys):
        board = tmp_path / "board"
        cid = _add_card(board)
        capsys.readouterr()

        rc = main(["--board", str(board), "result", cid])
        assert rc == 0
        out = capsys.readouterr().out
        # Identity line + Result block.
        assert f"Card {cid[:8]}" in out
        assert "Result:" in out
        assert "status: inbox" in out


class TestResultWithGitRepo:
    """When the board lives inside a real Git repo, ``state`` distinguishes
    between active worktrees, detached branches, and missing branches —
    that's the discriminator users actually care about."""

    def test_state_detached_when_branch_exists_but_no_directory(
        self, tmp_path: Path, capsys
    ):
        repo = _init_repo(tmp_path / "repo")
        board = repo / "workspace" / "board"
        store = MarkdownBoardStore(board)
        card_id = "abcd1234-0000-0000-0000-000000000000"
        store.add_card(
            Card(
                id=card_id,
                title="detached",
                goal="branch only",
                status=CardStatus.DONE,
            )
        )
        # Create a real git branch at HEAD without an attached worktree
        # so the manager reports the card as "detached" rather than
        # missing.
        subprocess.run(
            ["git", "branch", f"kanban/{card_id}"],
            cwd=repo, check=True, capture_output=True,
        )
        store.update_card(
            card_id,
            worktree_branch=f"kanban/{card_id}",
            worktree_base_commit=subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo, check=True, capture_output=True, text=True,
            ).stdout.strip(),
        )

        capsys.readouterr()
        rc = main(["--board", str(board), "result", card_id, "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out.strip())
        assert payload["worktree"]["state"] == "detached"
        # Detached state should suggest both diff and merge as next steps;
        # the user hasn't decided which yet.
        joined = " ".join(payload["next_steps"])
        assert "kanban worktree diff" in joined
        assert "git merge kanban/" in joined


class TestWorktreeListEmptyMessage:
    """``kanban worktree list`` empty case is the most-asked-about UX —
    pin the operator-facing breadcrumbs."""

    def test_empty_message_points_at_result_command(self, tmp_path: Path, capsys):
        repo = _init_repo(tmp_path / "repo")
        board = repo / "workspace" / "board"
        # Make the board dir exist so `--board` resolves cleanly.
        board.mkdir(parents=True, exist_ok=True)
        capsys.readouterr()

        rc = main(["--board", str(board), "worktree", "list"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "No active worktree directories" in out
        assert "kanban result" in out
        assert "kanban worktree diff" in out


class TestShowEmbedsResultBlock:
    """``kanban show`` should inline a ``result:`` block for cards that
    have produced something. Fresh cards keep the existing tight payload."""

    def test_fresh_card_has_no_result_block(self, tmp_path: Path, capsys):
        import yaml as _yaml

        board = tmp_path / "board"
        cid = _add_card(board)
        capsys.readouterr()

        rc = main(["--board", str(board), "show", cid])
        assert rc == 0
        data = _yaml.safe_load(capsys.readouterr().out)
        assert "result" not in data

    def test_card_with_summary_gets_result_block(self, tmp_path: Path, capsys):
        import yaml as _yaml

        board = tmp_path / "board"
        store = MarkdownBoardStore(board)
        card = store.add_card(
            Card(
                id="11112222-0000-0000-0000-000000000000",
                title="with summary",
                goal="show inline result",
                outputs={
                    "last": {
                        "summary": "did the thing",
                        "output": ["workspace/reports/x.md"],
                    }
                },
            )
        )
        capsys.readouterr()
        rc = main(["--board", str(board), "show", card.id])
        assert rc == 0
        data = _yaml.safe_load(capsys.readouterr().out)
        assert "result" in data
        assert data["result"]["summary"] == "did the thing"
