"""Tests for `kanban daemon stop|status|logs`."""

# ruff: noqa: E402  -- imports follow the existing test layout

from __future__ import annotations

import json
import os
import signal
from pathlib import Path

import pytest

from kanban.cli import main
from kanban.daemon import DAEMON_LOG_FILENAME, daemon_lock, lock_path


class TestDaemonStatus:
    def test_stopped_when_no_lock(self, tmp_path: Path, capsys):
        rc = main(["--board", str(tmp_path), "daemon", "status"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "status:     stopped" in out
        assert ".daemon.lock" in out

    def test_json_output(self, tmp_path: Path, capsys):
        rc = main(["--board", str(tmp_path), "daemon", "status", "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "stopped"
        assert payload["pid"] is None
        assert payload["lock_path"].endswith(".daemon.lock")

    def test_running_status(self, tmp_path: Path, capsys):
        with daemon_lock(tmp_path):
            rc = main(["--board", str(tmp_path), "daemon", "status", "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "running"
        assert payload["pid"] == os.getpid()


class TestKanbanDaemonHeuristic:
    """Direct tests for the pid-cmdline heuristic that guards `daemon stop`."""

    def _patch_command(self, monkeypatch, cmdline: str) -> None:
        from kanban import cli

        monkeypatch.setattr(cli, "_pid_command", lambda pid: cmdline)

    def test_accepts_direct_entry_point(self, monkeypatch):
        from kanban import cli

        self._patch_command(monkeypatch, "kanban daemon")
        assert cli._looks_like_kanban_daemon(123) is True

    def test_accepts_kanban_mcp(self, monkeypatch):
        from kanban import cli

        self._patch_command(monkeypatch, "/usr/local/bin/kanban-mcp --board /x")
        assert cli._looks_like_kanban_daemon(123) is True

    def test_accepts_python_dash_m(self, monkeypatch):
        from kanban import cli

        self._patch_command(monkeypatch, "python3 -m kanban daemon")
        assert cli._looks_like_kanban_daemon(123) is True

    def test_accepts_uv_run(self, monkeypatch):
        from kanban import cli

        self._patch_command(monkeypatch, "uv run --project /repo kanban daemon")
        assert cli._looks_like_kanban_daemon(123) is True

    def test_rejects_grep_kanban(self, monkeypatch):
        from kanban import cli

        self._patch_command(monkeypatch, "grep kanban")
        assert cli._looks_like_kanban_daemon(123) is False

    def test_rejects_editor_with_kanban_in_path(self, monkeypatch):
        from kanban import cli

        self._patch_command(monkeypatch, "vim /home/me/kanban/cli.py")
        assert cli._looks_like_kanban_daemon(123) is False

    def test_handles_missing_ps(self, monkeypatch):
        from kanban import cli

        monkeypatch.setattr(cli, "_pid_command", lambda pid: None)
        assert cli._looks_like_kanban_daemon(123) is False


class TestDaemonStop:
    def test_clears_stale_lock(self, tmp_path: Path, capsys):
        # Synthetic stale lock — pid 1 is rarely the daemon; the
        # status check classifies it as alive though, so we use a
        # definitely-dead pid by writing one and then ensuring
        # daemon_status reports "stale" via _pid_alive=False.
        # The cleanest synthetic is pid 0, which `_pid_alive` rejects.
        path = lock_path(tmp_path)
        tmp_path.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pid": 0, "started_at": 0.0}))

        rc = main(["--board", str(tmp_path), "daemon", "stop"])
        assert rc == 0
        assert not path.exists()
        out = capsys.readouterr().out
        assert "stale lock" in out

    def test_no_daemon_returns_nonzero(self, tmp_path: Path, capsys):
        rc = main(["--board", str(tmp_path), "daemon", "stop"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "No daemon" in err

    def test_clears_lock_after_pid_dies(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        from kanban import cli

        path = lock_path(tmp_path)
        tmp_path.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pid": os.getpid(), "started_at": 0.0}))

        def fake_kill(pid, sig):
            # Re-write the lock with a dead pid (0) so clear_stale_lock
            # accepts it on the next poll.
            path.write_text(json.dumps({"pid": 0, "started_at": 0.0}))

        monkeypatch.setattr(cli.os, "kill", fake_kill)
        # Pytest's process command does not contain "kanban", so we must
        # bypass the pid-reuse guard for this synthetic test.
        monkeypatch.setattr(cli, "_looks_like_kanban_daemon", lambda pid: True)
        rc = main([
            "--board", str(tmp_path),
            "daemon", "stop", "--force", "--timeout", "1",
        ])
        assert rc == 0
        assert not path.exists()

    def test_removes_malformed_invalid_json_lock(
        self, tmp_path: Path, capsys
    ):
        # Invalid JSON makes daemon_status return "stopped" while the file
        # persists. Without recovery here, the user would have to rm the
        # file by hand because `kanban daemon` later refuses to start.
        path = lock_path(tmp_path)
        tmp_path.mkdir(parents=True, exist_ok=True)
        path.write_text("not valid json {")
        rc = main(["--board", str(tmp_path), "daemon", "stop"])
        assert rc == 0
        assert not path.exists()

    def test_removes_lock_with_non_numeric_pid(
        self, tmp_path: Path, capsys
    ):
        # daemon_status classifies this as "stale" (pid coerced to 0), but
        # clear_stale_lock would crash on int("oops"). Make sure stop still
        # cleans up.
        path = lock_path(tmp_path)
        tmp_path.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pid": "oops", "started_at": 0.0}))
        rc = main(["--board", str(tmp_path), "daemon", "stop"])
        assert rc == 0
        assert not path.exists()

    def test_refuses_to_signal_unrelated_pid(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        # Lock points at this pid (alive, status=running), but the pid-reuse
        # guard reports false → stop should refuse rather than send SIGTERM.
        from kanban import cli

        path = lock_path(tmp_path)
        tmp_path.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pid": os.getpid(), "started_at": 0.0}))
        monkeypatch.setattr(cli, "_looks_like_kanban_daemon", lambda pid: False)

        # daemon_status's liveness probe also calls os.kill(pid, 0), so we
        # only count real-signal invocations (sig != 0).
        signal_sigs: list[int] = []

        def fake_kill(pid, sig):
            if sig != 0:
                signal_sigs.append(sig)

        monkeypatch.setattr(cli.os, "kill", fake_kill)
        rc = main(["--board", str(tmp_path), "daemon", "stop"])
        assert rc == 1
        assert signal_sigs == []
        err = capsys.readouterr().err
        assert "Refusing to signal" in err


class TestDaemonLogs:
    def test_missing_log_returns_nonzero(self, tmp_path: Path, capsys):
        rc = main(["--board", str(tmp_path), "daemon", "logs"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "No daemon log" in err

    def test_prints_tail(self, tmp_path: Path, capsys):
        log = tmp_path / DAEMON_LOG_FILENAME
        log.write_text("\n".join(f"line {i}" for i in range(100)) + "\n")
        rc = main(["--board", str(tmp_path), "daemon", "logs", "-n", "5"])
        assert rc == 0
        out = capsys.readouterr().out.splitlines()
        assert out == [f"line {i}" for i in range(95, 100)]

    def test_n_zero_prints_no_backlog(self, tmp_path: Path, capsys):
        # -n 0 should print nothing (useful paired with -f to watch only
        # new entries), not dump the entire log like the original
        # implementation did.
        log = tmp_path / DAEMON_LOG_FILENAME
        log.write_text("\n".join(f"line {i}" for i in range(50)) + "\n")
        rc = main(["--board", str(tmp_path), "daemon", "logs", "-n", "0"])
        assert rc == 0
        assert capsys.readouterr().out == ""
