"""Tests for `kanban card acceptance edit`."""

from __future__ import annotations

from pathlib import Path

import pytest

from kanban.cli import _parse_acceptance_buffer, main
from kanban.store_markdown import MarkdownBoardStore


def _add_card(board: Path) -> str:
    rc = main(
        ["--board", str(board), "card", "add", "--title", "T", "--goal", "G"]
    )
    assert rc == 0
    return MarkdownBoardStore(board).list_cards()[0].id


class TestParseBuffer:
    def test_drops_comments_and_blanks(self):
        text = (
            "# header line\n"
            "first item\n"
            "\n"
            "  # indented comment\n"
            "second item\n"
            "  \n"
        )
        assert _parse_acceptance_buffer(text) == ["first item", "second item"]

    def test_trims_each_line(self):
        assert _parse_acceptance_buffer("  one  \n\ttwo\t\n") == ["one", "two"]


class TestEditCommand:
    def test_writes_new_criteria(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        board = tmp_path / "board"
        cid = _add_card(board)

        # Stand in for $EDITOR: ignore the banner, replace with two lines.
        def fake_editor(initial: str, *, suffix: str = ".txt") -> str:
            return "criterion one\ncriterion two\n"

        monkeypatch.setattr("kanban.cli._open_in_editor", fake_editor)
        rc = main(
            ["--board", str(board), "card", "acceptance", "edit", cid]
        )
        assert rc == 0
        card = MarkdownBoardStore(board).get_card(cid)
        assert card.acceptance_criteria == ["criterion one", "criterion two"]

    def test_no_changes_is_noop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ):
        board = tmp_path / "board"
        cid = _add_card(board)

        # Editor returns None → unchanged buffer.
        monkeypatch.setattr(
            "kanban.cli._open_in_editor", lambda initial, *, suffix=".txt": None
        )
        rc = main(
            ["--board", str(board), "card", "acceptance", "edit", cid]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "No changes" in out

    def test_no_editor_env_aborts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        board = tmp_path / "board"
        cid = _add_card(board)
        monkeypatch.delenv("EDITOR", raising=False)
        monkeypatch.delenv("VISUAL", raising=False)
        with pytest.raises(SystemExit) as excinfo:
            main(["--board", str(board), "card", "acceptance", "edit", cid])
        assert "EDITOR" in str(excinfo.value)

    def test_missing_editor_binary_aborts_cleanly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from kanban import cli

        def explode(argv, check=False):
            raise FileNotFoundError(2, "No such file", argv[0])

        monkeypatch.setenv("EDITOR", "definitely-not-installed-editor")
        monkeypatch.setattr(cli.subprocess, "run", explode)
        with pytest.raises(SystemExit) as excinfo:
            cli._open_in_editor("hello\n")
        msg = str(excinfo.value)
        assert "Failed to launch editor" in msg
        assert "definitely-not-installed-editor" in msg

    def test_editor_with_flags_is_split(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Common real-world EDITOR values include flags. Confirm we
        # shlex.split rather than try to exec a binary literally named
        # "code --wait" (which would FileNotFoundError).
        from kanban import cli

        captured: dict[str, list[str]] = {}

        def fake_run(argv, check=False):
            captured["argv"] = list(argv)

            class _RV:
                returncode = 0

            return _RV()

        monkeypatch.setenv("EDITOR", "code --wait")
        monkeypatch.setattr(cli.subprocess, "run", fake_run)
        # Editor is a no-op so the buffer is unchanged → no-op return None.
        result = cli._open_in_editor("hello\n")
        assert result is None
        assert captured["argv"][:2] == ["code", "--wait"]
