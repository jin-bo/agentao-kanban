from __future__ import annotations

from pathlib import Path

import pytest

from kanban.cli import main
from kanban.store_markdown import MarkdownBoardStore


def _add_card(board: Path) -> str:
    assert main(["--board", str(board), "card", "add", "--title", "T", "--goal", "G"]) == 0
    return MarkdownBoardStore(board).list_cards()[0].id


class TestCardEditAgentProfile:
    def test_set_agent_profile_records_manual_source(self, tmp_path: Path):
        board = tmp_path / "board"
        cid = _add_card(board)
        rc = main([
            "--board", str(board), "card", "edit", cid,
            "--agent-profile", "gemini-worker",
        ])
        assert rc == 0
        card = MarkdownBoardStore(board).get_card(cid)
        assert card.agent_profile == "gemini-worker"
        assert card.agent_profile_source == "manual"
        assert any("Agent profile set to 'gemini-worker'" in h for h in card.history)

    def test_clear_agent_profile(self, tmp_path: Path):
        board = tmp_path / "board"
        cid = _add_card(board)
        main([
            "--board", str(board), "card", "edit", cid,
            "--agent-profile", "gemini-worker",
        ])
        rc = main([
            "--board", str(board), "card", "edit", cid,
            "--clear-agent-profile",
        ])
        assert rc == 0
        card = MarkdownBoardStore(board).get_card(cid)
        assert card.agent_profile is None
        assert card.agent_profile_source is None
        assert any("Agent profile cleared via CLI" in h for h in card.history)

    def test_unknown_profile_rejected(self, tmp_path: Path, capsys):
        board = tmp_path / "board"
        cid = _add_card(board)
        rc = main([
            "--board", str(board), "card", "edit", cid,
            "--agent-profile", "ghost-profile",
        ])
        assert rc == 2
        err = capsys.readouterr().err
        assert "unknown profile" in err
        card = MarkdownBoardStore(board).get_card(cid)
        assert card.agent_profile is None

    def test_agent_profile_and_clear_are_mutually_exclusive(self, tmp_path: Path):
        board = tmp_path / "board"
        cid = _add_card(board)
        with pytest.raises(SystemExit):
            main([
                "--board", str(board), "card", "edit", cid,
                "--agent-profile", "gemini-worker",
                "--clear-agent-profile",
            ])


class TestProfilesSubcommand:
    def test_profiles_list_shows_defaults(self, tmp_path: Path, capsys):
        rc = main(["--board", str(tmp_path / "board"), "profiles", "list"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "default-worker" in out
        assert "default for worker" in out
        assert "gemini-worker" in out
        assert "acp" in out  # backend column

    def test_profiles_show_known_profile(self, tmp_path: Path, capsys):
        rc = main([
            "--board", str(tmp_path / "board"),
            "profiles", "show", "gemini-worker",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "role:        worker" in out
        assert "backend:     acp -> gemini-worker" in out
        assert "fallback:    default-worker" in out
        assert "chain:       gemini-worker -> default-worker" in out

    def test_profiles_show_unknown(self, tmp_path: Path, capsys):
        rc = main([
            "--board", str(tmp_path / "board"),
            "profiles", "show", "ghost",
        ])
        assert rc == 1
        assert "unknown profile" in capsys.readouterr().err


class TestMultiBackendExecutorWiring:
    def test_build_executor_returns_multi_backend_instance(self) -> None:
        from kanban.cli import _build_executor
        from kanban.executors.multi_backend import MultiBackendExecutor
        assert isinstance(_build_executor("multi-backend"), MultiBackendExecutor)

    def test_build_executor_registers_both_backends(self) -> None:
        # Regression: an ACP-pinned card must not hit
        # "no backend registered for type 'acp'" from the CLI path.
        from kanban.cli import _build_executor
        from kanban.executors.backends.acp_backend import AcpBackend
        from kanban.executors.backends.subagent_backend import SubagentBackend

        executor = _build_executor("multi-backend")
        assert isinstance(executor.backends["subagent"], SubagentBackend)
        assert isinstance(executor.backends["acp"], AcpBackend)

    def test_executor_choice_accepts_multi_backend(self, tmp_path: Path) -> None:
        # The --executor flag must accept the new value so operators can
        # opt into profile-aware routing without patching the CLI.
        rc = main([
            "--board", str(tmp_path / "board"),
            "--executor", "multi-backend",
            "profiles", "list",
        ])
        assert rc == 0


class TestBoardScopedConfig:
    """Regression: commands that read profile config must resolve it
    relative to --board, not the shell cwd."""

    _TINY_CONFIG = """
roles:
  planner: {default_profile: tiny-planner}
  worker: {default_profile: tiny-worker}
  reviewer: {default_profile: tiny-reviewer}
  verifier: {default_profile: tiny-verifier}
profiles:
  tiny-planner:  {role: planner,  backend: {type: subagent, target: t}}
  tiny-worker:   {role: worker,   backend: {type: subagent, target: t}}
  tiny-reviewer: {role: reviewer, backend: {type: subagent, target: t}}
  tiny-verifier: {role: verifier, backend: {type: subagent, target: t}}
"""

    def _make_project(self, tmp_path: Path) -> Path:
        project = tmp_path / "other-project"
        (project / ".kanban").mkdir(parents=True)
        (project / ".kanban" / "agent_profiles.yaml").write_text(
            self._TINY_CONFIG, encoding="utf-8"
        )
        return project

    def test_profiles_list_reads_per_board_config(
        self, tmp_path: Path, capsys
    ) -> None:
        project = self._make_project(tmp_path)
        # Board lives inside the per-project directory.
        rc = main([
            "--board", str(project / "workspace" / "board"),
            "profiles", "list",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        # Tiny profile names from the project config must appear;
        # the packaged default's gemini-worker must NOT.
        assert "tiny-worker" in out
        assert "gemini-worker" not in out

    def test_card_edit_validates_against_board_config(
        self, tmp_path: Path, capsys
    ) -> None:
        project = self._make_project(tmp_path)
        board = project / "workspace" / "board"
        cid = _add_card(board)

        # `gemini-worker` is valid in the packaged default but unknown
        # in the tiny per-project config; edit must reject it.
        rc = main([
            "--board", str(board), "card", "edit", cid,
            "--agent-profile", "gemini-worker",
        ])
        assert rc == 2
        assert "unknown profile" in capsys.readouterr().err

        # `tiny-worker` is the per-project worker profile; edit must accept it.
        rc = main([
            "--board", str(board), "card", "edit", cid,
            "--agent-profile", "tiny-worker",
        ])
        assert rc == 0
        card = MarkdownBoardStore(board).get_card(cid)
        assert card.agent_profile == "tiny-worker"

    def test_build_executor_derives_project_root_from_board(
        self, tmp_path: Path
    ) -> None:
        """_build_executor must wire the per-board config into the
        MultiBackendExecutor, including the router-policy's agents_dir
        and the ACP backend's project_root."""
        project = self._make_project(tmp_path)
        board = project / "workspace" / "board"
        board.mkdir(parents=True)

        from kanban.cli import _build_executor

        executor = _build_executor("multi-backend", board=board)
        assert set(executor.config.profiles) == {
            "tiny-planner", "tiny-worker", "tiny-reviewer", "tiny-verifier"
        }
        assert executor.working_directory == project.resolve()
        assert executor.backends["acp"].project_root == project.resolve()
