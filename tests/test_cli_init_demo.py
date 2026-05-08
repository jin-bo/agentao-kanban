from __future__ import annotations

from pathlib import Path

import pytest

from kanban.cli import _discover_board, main
from kanban.store_markdown import MarkdownBoardStore


class TestInit:
    def test_creates_marker_and_board(self, tmp_path: Path, capsys):
        rc = main(["init", str(tmp_path)])
        assert rc == 0
        assert (tmp_path / ".kanban").is_dir()
        assert (tmp_path / ".kanban" / "config.yaml").is_file()
        assert (tmp_path / "workspace" / "board").is_dir()

    def test_idempotent(self, tmp_path: Path):
        assert main(["init", str(tmp_path)]) == 0
        # Second run should not raise and should leave existing config alone.
        cfg = (tmp_path / ".kanban" / "config.yaml").read_text()
        assert main(["init", str(tmp_path)]) == 0
        assert (tmp_path / ".kanban" / "config.yaml").read_text() == cfg

    def test_copy_agents_drops_definitions(self, tmp_path: Path):
        rc = main(["init", str(tmp_path), "--copy-agents"])
        assert rc == 0
        agents = tmp_path / ".agentao" / "agents"
        assert agents.is_dir()
        names = {p.name for p in agents.glob("kanban-*.md")}
        # We only require that at least the four core role definitions
        # were copied — the bundled set may grow.
        assert {
            "kanban-planner.md",
            "kanban-worker.md",
            "kanban-reviewer.md",
            "kanban-verifier.md",
        }.issubset(names)

    def test_demo_flag_seeds_cards(self, tmp_path: Path):
        rc = main(["init", str(tmp_path), "--demo"])
        assert rc == 0
        store = MarkdownBoardStore(tmp_path / "workspace" / "board")
        assert len(store.list_cards()) >= 3

    def test_rerun_with_external_board_dir(self, tmp_path: Path):
        # Custom board_dir pointing outside the project root must not crash
        # the print path. Use a sibling directory to keep tmp_path tidy.
        root = tmp_path / "proj"
        root.mkdir()
        external = tmp_path / "shared-board"
        marker = root / ".kanban"
        marker.mkdir()
        (marker / "config.yaml").write_text(f"board_dir: {external}\n")
        rc = main(["init", str(root), "--demo"])
        assert rc == 0
        assert len(MarkdownBoardStore(external).list_cards()) >= 3

    def test_rerun_honors_existing_custom_board_dir(self, tmp_path: Path):
        # Pre-write a marker with a custom board_dir, then re-run init --demo
        # and confirm the demo seed lands at the custom location (not at
        # the hardcoded workspace/board).
        marker = tmp_path / ".kanban"
        marker.mkdir()
        (marker / "config.yaml").write_text("board_dir: custom/place\n")
        rc = main(["init", str(tmp_path), "--demo"])
        assert rc == 0
        # Custom location got the cards.
        custom = tmp_path / "custom" / "place"
        assert custom.is_dir()
        assert len(MarkdownBoardStore(custom).list_cards()) >= 3
        # Default location must NOT have been created.
        assert not (tmp_path / "workspace" / "board").exists()


class TestDemoSubcommand:
    def test_seeds_and_runs_to_done(self, tmp_path: Path, capsys):
        board = tmp_path / "board"
        rc = main(["--board", str(board), "--no-worktree", "demo"])
        assert rc == 0
        store = MarkdownBoardStore(board)
        cards = store.list_cards()
        assert len(cards) >= 3
        # MockAgentaoExecutor takes every card to DONE in run_until_idle.
        assert all(c.status.value == "done" for c in cards)

    def test_refuses_on_non_demo_board(self, tmp_path: Path, capsys):
        board = tmp_path / "board"
        # Pre-seed with a card the demo set does not include.
        rc = main([
            "--board", str(board), "card", "add",
            "--title", "Real backlog item", "--goal", "G",
        ])
        assert rc == 0
        rc = main(["--board", str(board), "--no-worktree", "demo"])
        assert rc == 2
        err = capsys.readouterr().err
        assert "non-demo" in err

    def test_idempotent_when_board_is_demo_seeded(self, tmp_path: Path, capsys):
        board = tmp_path / "board"
        # First run seeds + advances the cards to DONE.
        assert main(["--board", str(board), "--no-worktree", "demo"]) == 0
        # Second run sees only demo cards present and re-runs the orchestrator
        # without erroring out — supports the documented init→demo flow.
        rc = main(["--board", str(board), "--no-worktree", "demo"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "already seeded" in out

    def test_refuses_title_collision_with_different_goal(
        self, tmp_path: Path, capsys
    ):
        # A user's real card whose TITLE happens to match a demo card but
        # whose GOAL differs must not be treated as a demo card. Otherwise
        # `kanban demo` would rewrite its history.
        from kanban.demo import DEMO_CARDS

        board = tmp_path / "board"
        rc = main([
            "--board", str(board), "card", "add",
            "--title", DEMO_CARDS[0]["title"],
            "--goal", "Totally different goal — this is real work.",
        ])
        assert rc == 0
        rc = main(["--board", str(board), "--no-worktree", "demo"])
        assert rc == 2
        err = capsys.readouterr().err
        assert "non-demo" in err

    def test_no_run_leaves_cards_in_inbox(self, tmp_path: Path):
        board = tmp_path / "board"
        rc = main(["--board", str(board), "--no-worktree", "demo", "--no-run"])
        assert rc == 0
        store = MarkdownBoardStore(board)
        statuses = {c.status.value for c in store.list_cards()}
        assert statuses == {"inbox"}


class TestBoardDiscovery:
    def test_walks_up_to_marker(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        (tmp_path / ".kanban").mkdir()
        deep = tmp_path / "a" / "b" / "c"
        deep.mkdir(parents=True)
        monkeypatch.chdir(deep)
        resolved = _discover_board()
        assert resolved == (tmp_path / "workspace" / "board").resolve()

    def test_honors_config_board_dir_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        marker = tmp_path / ".kanban"
        marker.mkdir()
        (marker / "config.yaml").write_text("board_dir: my/custom/board\n")
        monkeypatch.chdir(tmp_path)
        assert _discover_board() == (tmp_path / "my" / "custom" / "board").resolve()

    def test_falls_back_to_cwd_workspace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # No marker anywhere in the chain — should resolve under cwd.
        monkeypatch.chdir(tmp_path)
        assert _discover_board() == (tmp_path / "workspace" / "board").resolve()


class TestNoArgsBanner:
    def test_prints_banner_and_returns_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ):
        monkeypatch.chdir(tmp_path)
        rc = main([])
        assert rc == 0
        out = capsys.readouterr().out
        assert "kanban v" in out
        assert "kanban init" in out
        assert "Full help" in out
