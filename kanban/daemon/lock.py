from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

LOCK_FILENAME = ".daemon.lock"


class DaemonLockError(RuntimeError):
    """Raised when another live process already holds the board lock."""


def lock_path(board_dir: Path) -> Path:
    return Path(board_dir) / LOCK_FILENAME


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but belongs to another user — treat as alive.
        return True
    return True


def read_lock(board_dir: Path) -> dict | None:
    path = lock_path(board_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def clear_stale_lock(board_dir: Path) -> bool:
    """Remove the lock if its recorded pid is no longer alive. Returns True if cleared."""
    data = read_lock(board_dir)
    if data is None:
        return False
    pid = int(data.get("pid", 0))
    if _pid_alive(pid):
        return False
    try:
        lock_path(board_dir).unlink(missing_ok=True)
    except OSError:
        return False
    return True


@contextmanager
def daemon_lock(board_dir: Path) -> Iterator[Path]:
    board_dir.mkdir(parents=True, exist_ok=True)
    path = lock_path(board_dir)
    clear_stale_lock(board_dir)

    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        data = read_lock(board_dir) or {}
        raise DaemonLockError(
            f"Another kanban daemon is running on this board "
            f"(pid={data.get('pid', '?')}, started={data.get('started_at', '?')})."
        )

    try:
        payload = json.dumps(
            {"pid": os.getpid(), "started_at": time.time()}, ensure_ascii=False
        )
        os.write(fd, payload.encode("utf-8"))
    finally:
        os.close(fd)

    try:
        yield path
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def assert_no_daemon(board_dir: Path) -> None:
    """Raise DaemonLockError if a live daemon currently holds the board."""
    clear_stale_lock(board_dir)
    data = read_lock(board_dir)
    if data is None:
        return
    pid = int(data.get("pid", 0))
    if _pid_alive(pid):
        raise DaemonLockError(
            f"Daemon (pid={pid}) is running on this board; refuse to mutate "
            f"while a dispatcher holds the lock. Stop the daemon or pass --force."
        )


def daemon_status(board_dir: Path) -> dict:
    """Read-only snapshot of ``.daemon.lock`` state for observability.

    Returns one of three shapes (always with the same keys, so callers
    don't need to defend against missing fields):

    - ``{"status": "stopped", "pid": None, "started_at": None, ...}``
      No lock file present.
    - ``{"status": "running", "pid": <int>, "started_at": <float>, ...}``
      Lock present, recorded pid is alive.
    - ``{"status": "stale", "pid": <int>, "started_at": <float>, ...}``
      Lock present, but the recorded pid is gone — the daemon crashed
      or was killed without unlinking the file.

    Crucially this does *not* clear stale locks (use
    :func:`clear_stale_lock` for that). The web UI calls this every
    poll and must stay strictly read-only.
    """
    path = lock_path(board_dir)
    base: dict = {
        "lock_path": str(path),
        "pid": None,
        "started_at": None,
    }
    data = read_lock(board_dir)
    if data is None:
        return {**base, "status": "stopped"}
    pid_raw = data.get("pid", 0)
    try:
        pid = int(pid_raw)
    except (TypeError, ValueError):
        pid = 0
    started_at = data.get("started_at")
    base["pid"] = pid if pid > 0 else None
    base["started_at"] = (
        float(started_at) if isinstance(started_at, (int, float)) else None
    )
    if pid > 0 and _pid_alive(pid):
        return {**base, "status": "running"}
    return {**base, "status": "stale"}
