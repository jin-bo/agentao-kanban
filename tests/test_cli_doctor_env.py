"""Environment-level checks for `kanban doctor [--fix]`.

The card-level checks live in tests/test_cli.py::TestDoctor.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from kanban.cli import main
from kanban.daemon import lock_path


@pytest.fixture(autouse=True)
def _chdir_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Doctor's env checks read cwd to find the project root, so every
    test must run from a deterministic directory rather than the repo
    checkout it was launched in."""
    monkeypatch.chdir(tmp_path)


def _write_lock(board: Path, payload: dict | str) -> Path:
    board.mkdir(parents=True, exist_ok=True)
    path = lock_path(board)
    if isinstance(payload, dict):
        path.write_text(json.dumps(payload), encoding="utf-8")
    else:
        path.write_text(payload, encoding="utf-8")
    return path


class TestStaleLockCheck:
    def test_dead_pid_flagged_as_warning(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        board = tmp_path / "board"
        path = _write_lock(board, {"pid": 0, "started_at": 0.0})
        rc = main(["--board", str(board), "doctor"])
        assert rc == 1
        out = capsys.readouterr().out
        assert "cwd-stale-lock" in out
        assert path.exists()  # not yet fixed

    def test_fix_removes_stale_lock(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        board = tmp_path / "board"
        path = _write_lock(board, {"pid": 0, "started_at": 0.0})
        rc = main(["--board", str(board), "doctor", "--fix"])
        assert rc == 0
        assert not path.exists()
        out = capsys.readouterr().out
        assert "Applied fixes:" in out
        assert "cwd-stale-lock" in out

    def test_malformed_json_flagged_and_fixable(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        board = tmp_path / "board"
        path = _write_lock(board, "not valid json {")
        rc = main(["--board", str(board), "doctor", "--fix"])
        assert rc == 0
        assert not path.exists()
        out = capsys.readouterr().out
        assert "cwd-malformed-lock" in out

    def test_non_numeric_pid_flagged_and_fixable(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        board = tmp_path / "board"
        path = _write_lock(board, {"pid": "oops", "started_at": 0.0})
        rc = main(["--board", str(board), "doctor", "--fix"])
        assert rc == 0
        assert not path.exists()
        out = capsys.readouterr().out
        assert "cwd-malformed-lock" in out

    def test_alive_pid_not_flagged(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        # A live daemon's lock must NOT be auto-removed by `doctor --fix`;
        # operators have to use `daemon stop` for that path so the daemon
        # gets a chance to unwind cleanly.
        board = tmp_path / "board"
        _write_lock(board, {"pid": os.getpid(), "started_at": 0.0})
        rc = main(["--board", str(board), "doctor"])
        # Empty board so no other findings; alive pid produces no env warning.
        assert rc == 0
        out = capsys.readouterr().out
        assert "cwd-stale-lock" not in out
        assert "cwd-malformed-lock" not in out


class TestBoardDirCheck:
    def test_missing_board_flagged_as_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        # No mkdir on the board; doctor should flag it as missing.
        board = tmp_path / "no-such-board"
        rc = main(["--board", str(board), "doctor"])
        assert rc == 2
        out = capsys.readouterr().out
        assert "cwd-board-missing" in out
        assert "fixable" in out

    def test_fix_creates_missing_board(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        board = tmp_path / "no-such-board"
        rc = main(["--board", str(board), "doctor", "--fix"])
        assert rc == 0
        assert board.is_dir()
        out = capsys.readouterr().out
        assert "Applied fixes:" in out

    def test_board_path_is_a_file_errors_without_fix(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        # Pointing --board at a regular file is a misconfiguration we
        # cannot safely auto-recover from; doctor errors but offers no fix.
        board = tmp_path / "board-file"
        board.write_text("oops")
        rc = main(["--board", str(board), "doctor", "--fix"])
        assert rc == 2
        assert board.is_file()  # untouched
        out = capsys.readouterr().out
        assert "cwd-board-not-a-dir" in out


class TestMarkerConfigCheck:
    def test_marker_without_config_flagged(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        (tmp_path / ".kanban").mkdir()
        board = tmp_path / "workspace" / "board"
        board.mkdir(parents=True)
        rc = main(["--board", str(board), "doctor"])
        assert rc == 1
        out = capsys.readouterr().out
        assert "cwd-marker-no-config" in out

    def test_fix_writes_default_config(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        marker = tmp_path / ".kanban"
        marker.mkdir()
        board = tmp_path / "workspace" / "board"
        board.mkdir(parents=True)
        rc = main(["--board", str(board), "doctor", "--fix"])
        assert rc == 0
        cfg = marker / "config.yaml"
        assert cfg.is_file()
        assert "board_dir:" in cfg.read_text()

    def test_config_without_board_dir_flagged_and_fixable(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        marker = tmp_path / ".kanban"
        marker.mkdir()
        # Comments only — no parseable board_dir.
        (marker / "config.yaml").write_text("# nothing useful here\n")
        board = tmp_path / "workspace" / "board"
        board.mkdir(parents=True)
        rc = main(["--board", str(board), "doctor", "--fix"])
        assert rc == 0
        cfg = marker / "config.yaml"
        assert "board_dir:" in cfg.read_text()


class TestJsonOutputIncludesEnv:
    def test_env_findings_appear_in_json(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        board = tmp_path / "board"
        board.mkdir()
        _write_lock(board, {"pid": 0, "started_at": 0.0})
        rc = main(["--board", str(board), "doctor", "--json"])
        assert rc == 1
        payload = json.loads(capsys.readouterr().out)
        rules = {c["rule"] for c in payload["checks"]}
        assert "cwd-stale-lock" in rules
        assert payload["fixes_applied"] == []
        stale = next(c for c in payload["checks"] if c["rule"] == "cwd-stale-lock")
        assert stale["fixable"] is True

    def test_fixes_recorded_in_json(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        board = tmp_path / "board"
        board.mkdir()
        _write_lock(board, {"pid": 0, "started_at": 0.0})
        rc = main(["--board", str(board), "doctor", "--json", "--fix"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        rules = [f["rule"] for f in payload["fixes_applied"]]
        assert "cwd-stale-lock" in rules
        assert payload["checks"] == []
