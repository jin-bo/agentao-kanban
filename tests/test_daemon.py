from __future__ import annotations

import json
import os
import signal
import threading
import time
from pathlib import Path

import pytest

from kanban import CardStatus, KanbanOrchestrator
from kanban.daemon import (
    DaemonConfig,
    DaemonLockError,
    KanbanDaemon,
    assert_no_daemon,
    clear_stale_lock,
    daemon_lock,
    lock_path,
    read_lock,
)
from kanban.executors import MockAgentaoExecutor
from kanban.store_markdown import MarkdownBoardStore


def _make_orch(board_dir: Path) -> tuple[MarkdownBoardStore, KanbanOrchestrator]:
    store = MarkdownBoardStore(board_dir)
    return store, KanbanOrchestrator(store=store, executor=MockAgentaoExecutor())


# ---------- lock ----------


def test_daemon_lock_writes_and_removes_file(tmp_path: Path):
    assert not lock_path(tmp_path).exists()
    with daemon_lock(tmp_path) as path:
        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["pid"] == os.getpid()
    assert not lock_path(tmp_path).exists()


def test_daemon_lock_refuses_when_held(tmp_path: Path):
    with daemon_lock(tmp_path):
        with pytest.raises(DaemonLockError):
            with daemon_lock(tmp_path):
                pass


def test_stale_lock_cleared_when_pid_dead(tmp_path: Path):
    lock_path(tmp_path).write_text(
        json.dumps({"pid": 999999, "started_at": 0}), encoding="utf-8"
    )
    assert clear_stale_lock(tmp_path) is True
    assert read_lock(tmp_path) is None


def test_assert_no_daemon_passes_when_no_lock(tmp_path: Path):
    assert_no_daemon(tmp_path)  # no raise


def test_assert_no_daemon_raises_when_live_lock(tmp_path: Path):
    with daemon_lock(tmp_path):
        with pytest.raises(DaemonLockError):
            assert_no_daemon(tmp_path)


def test_assert_no_daemon_ignores_stale_lock(tmp_path: Path):
    lock_path(tmp_path).write_text(
        json.dumps({"pid": 999999, "started_at": 0}), encoding="utf-8"
    )
    assert_no_daemon(tmp_path)  # no raise — stale lock cleared
    assert read_lock(tmp_path) is None


# ---------- daemon loop ----------


def test_daemon_processes_cards_until_idle(tmp_path: Path):
    _, orch = _make_orch(tmp_path)
    card = orch.create_card(title="t", goal="g")

    daemon = KanbanDaemon(orch, config=DaemonConfig(poll_interval=0.01, max_idle_cycles=1))
    daemon.run()

    assert orch.store.get_card(card.id).status == CardStatus.DONE
    # Four roles + one READY→DOING transition = processed ticks
    assert daemon.ticks_processed >= 4


def test_daemon_does_not_process_blocked_cards(tmp_path: Path):
    store, orch = _make_orch(tmp_path)
    card = orch.create_card(title="t", goal="g")
    orch.block(card.id, "manually blocked")

    daemon = KanbanDaemon(orch, config=DaemonConfig(poll_interval=0.01, max_idle_cycles=1))
    daemon.run()

    final = store.get_card(card.id)
    assert final.status == CardStatus.BLOCKED
    assert daemon.ticks_processed == 0


def test_daemon_run_once_does_a_single_tick(tmp_path: Path):
    store, orch = _make_orch(tmp_path)
    card = orch.create_card(title="t", goal="g")

    daemon = KanbanDaemon(orch, config=DaemonConfig(poll_interval=0.01))
    assert daemon.run_once() is True
    assert store.get_card(card.id).status != CardStatus.INBOX


def test_daemon_stops_on_signal(tmp_path: Path):
    _, orch = _make_orch(tmp_path)

    daemon = KanbanDaemon(orch, config=DaemonConfig(poll_interval=10.0))
    thread = threading.Thread(target=daemon.run, daemon=True)
    thread.start()
    # let the loop enter its sleep
    time.sleep(0.1)
    daemon.request_stop(signal.SIGTERM)
    thread.join(timeout=2.0)
    assert not thread.is_alive()


def test_lock_blocks_second_daemon(tmp_path: Path):
    _, orch = _make_orch(tmp_path)
    with daemon_lock(tmp_path):
        with pytest.raises(DaemonLockError):
            with daemon_lock(tmp_path):
                KanbanDaemon(orch).run_once()


def test_daemon_startup_log_names_the_executor(tmp_path: Path, caplog):
    _, orch = _make_orch(tmp_path)
    daemon = KanbanDaemon(
        orch, config=DaemonConfig(poll_interval=0.01, max_idle_cycles=1)
    )
    with caplog.at_level("INFO", logger="kanban.daemon"):
        daemon.run()
    startup_lines = [
        r.getMessage() for r in caplog.records if r.getMessage().startswith("Daemon started")
    ]
    assert startup_lines, "expected a 'Daemon started' log line"
    assert "executor=MockAgentaoExecutor" in startup_lines[0]


def test_daemon_resumes_cleanly_after_stale_lock(tmp_path: Path):
    # Simulate hard-killed previous run.
    lock_path(tmp_path).write_text(
        json.dumps({"pid": 999999, "started_at": 0}), encoding="utf-8"
    )
    _, orch = _make_orch(tmp_path)
    card = orch.create_card(title="t", goal="g")

    with daemon_lock(tmp_path):
        KanbanDaemon(orch, config=DaemonConfig(poll_interval=0.01, max_idle_cycles=1)).run()

    assert orch.store.get_card(card.id).status == CardStatus.DONE
