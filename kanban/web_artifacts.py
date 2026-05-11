"""Artifact browsing helpers for the local web UI."""

from __future__ import annotations

import os
import stat as stat_mod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from fastapi.responses import FileResponse

from .worktree import ARTIFACT_DIR_NAME_RE


# Cap inline file responses. 8 MiB is generous for logs/text but small
# enough to keep the loopback server snappy and memory-bounded. Operators
# who need bigger payloads can copy from disk.
ARTIFACT_FILE_MAX_BYTES = 8 * 1024 * 1024

# Defensive cap on per-snapshot file enumeration. A pathological worker
# emitting tens of thousands of tiny files would still respect the byte cap
# upstream but could blow out the JSON payload and the DOM.
ARTIFACT_LISTING_MAX_FILES = 5000


def artifacts_root_for(board_dir: Path) -> Path:
    """Conventional artifacts root for a given board."""
    return board_dir.parent / "raw"


def list_artifact_snapshots(card_id: str, root: Path) -> list[dict[str, Any]]:
    """Enumerate ``raw/<card_id>/artifacts-*`` snapshots, newest first."""
    card_dir = root / card_id
    if not card_dir.is_dir():
        return []
    snapshots: list[dict[str, Any]] = []
    for snap in sorted(card_dir.glob("artifacts-*"), reverse=True):
        if not snap.is_dir() or not ARTIFACT_DIR_NAME_RE.match(snap.name):
            continue
        files: list[dict[str, Any]] = []
        total_bytes = 0
        total_count = 0
        truncated = False
        for dirpath, _dirnames, filenames in os.walk(snap, followlinks=False):
            dpath = Path(dirpath)
            for name in filenames:
                full = dpath / name
                try:
                    st = full.lstat()
                except OSError:
                    continue
                if not stat_mod.S_ISREG(st.st_mode):
                    continue
                total_count += 1
                total_bytes += st.st_size
                if len(files) >= ARTIFACT_LISTING_MAX_FILES:
                    truncated = True
                    continue
                try:
                    rel = full.relative_to(snap)
                except ValueError:
                    continue
                files.append({"path": str(rel), "size": st.st_size})
        try:
            created = datetime.fromtimestamp(
                snap.stat().st_mtime, tz=timezone.utc
            ).isoformat()
        except OSError:
            created = None
        files.sort(key=lambda f: f["path"])
        record: dict[str, Any] = {
            "snapshot": snap.name,
            "abs_path": str(snap.resolve()),
            "created_at": created,
            "file_count": len(files),
            "total_file_count": total_count,
            "total_bytes": total_bytes,
            "files": files,
        }
        if truncated:
            record["truncated"] = True
        snapshots.append(record)
    return snapshots


def serve_file_under_root(
    unresolved: Path,
    root: Path,
    *,
    media_type: str | None = None,
    inline: bool = False,
) -> FileResponse:
    """Serve a regular file after checking it resolves under ``root``."""
    if unresolved.is_symlink():
        raise HTTPException(status_code=403, detail="symlinks not served")
    target = unresolved.resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=400, detail="path escapes the served directory")
    try:
        st = target.stat()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="file not found")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"stat failed: {exc}")
    if not stat_mod.S_ISREG(st.st_mode):
        raise HTTPException(status_code=400, detail="not a regular file")
    if st.st_size > ARTIFACT_FILE_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"file is {st.st_size} bytes; the inline cap is "
                f"{ARTIFACT_FILE_MAX_BYTES}. Copy directly from {target}."
            ),
        )
    kwargs: dict[str, Any] = {"filename": target.name}
    if media_type is not None:
        kwargs["media_type"] = media_type
    if inline:
        kwargs["content_disposition_type"] = "inline"
    return FileResponse(target, **kwargs)
