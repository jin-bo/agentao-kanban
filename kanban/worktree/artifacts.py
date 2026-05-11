"""Artifact snapshotting for managed worktrees."""

from __future__ import annotations

import logging
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .types import ARTIFACTS_MAX_BYTES_ENV, _is_denylisted

log = logging.getLogger("kanban.worktree")


def save_artifacts(
    *,
    card_id: str,
    wt_path: Path,
    artifacts_root: Path | None,
    artifacts_retention: int,
    artifacts_max_bytes: int,
    artifacts_denylist: tuple[str, ...],
) -> tuple[Path | None, str | None]:
    """Snapshot gitignored worktree content before detach."""
    if artifacts_root is None or artifacts_retention <= 0:
        return None, None

    ls = subprocess.run(
        [
            "git", "ls-files",
            "--others", "--ignored", "--exclude-standard",
            "-z",
        ],
        cwd=wt_path, capture_output=True, check=False,
    )
    if ls.returncode != 0:
        log.warning(
            "git ls-files failed in %s: %s",
            wt_path, ls.stderr.decode(errors="replace").strip(),
        )
        return None, "ls-files-failed"

    raw = ls.stdout.split(b"\x00")
    rels = [r.decode("utf-8", errors="replace") for r in raw if r]
    if not rels:
        return None, "no-artifacts"

    kept: list[tuple[Path, str, int]] = []
    skipped_pattern = 0
    skipped_oversize = 0
    skipped_oversize_bytes = 0
    total = 0
    for rel in rels:
        if _is_denylisted(rel, artifacts_denylist):
            skipped_pattern += 1
            continue
        src = wt_path / rel
        try:
            st = src.lstat()
        except OSError:
            continue
        size = st.st_size
        if total + size > artifacts_max_bytes:
            skipped_oversize += 1
            skipped_oversize_bytes += size
            continue
        total += size
        kept.append((src, rel, size))

    if not kept:
        if skipped_oversize > 0:
            log.warning(
                "artifact snapshot for %s skipped %d file(s) totaling "
                "%d bytes; cap %d bytes. Raise %s or "
                "WorktreeManager.artifacts_max_bytes if you need them.",
                card_id, skipped_oversize, skipped_oversize_bytes,
                artifacts_max_bytes, ARTIFACTS_MAX_BYTES_ENV,
            )
            return None, "size-cap-exceeded"
        return None, "no-artifacts"

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    card_dir = artifacts_root / card_id
    snapshot = card_dir / f"artifacts-{stamp}"
    snapshot.mkdir(parents=True, exist_ok=True)
    wt_resolved = wt_path.resolve()
    for src, rel_str, _ in kept:
        try:
            rel = src.resolve(strict=False).relative_to(wt_resolved)
        except ValueError:
            rel = Path(rel_str)
        dst = snapshot / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            if src.is_symlink():
                target = src.readlink()
                if dst.exists() or dst.is_symlink():
                    dst.unlink()
                dst.symlink_to(target)
            else:
                shutil.copy2(src, dst, follow_symlinks=False)
        except OSError as exc:
            log.warning("could not snapshot %s: %s", src, exc)

    existing = sorted(card_dir.glob("artifacts-*"))
    for stale in existing[: -artifacts_retention]:
        try:
            shutil.rmtree(stale)
        except OSError:
            pass

    if skipped_oversize > 0:
        log.warning(
            "snapshotted %d file(s) (%d bytes) for %s to %s; "
            "skipped %d additional file(s) totaling %d bytes due to "
            "size cap (%d). Raise %s or "
            "WorktreeManager.artifacts_max_bytes to capture them.",
            len(kept), total, card_id, snapshot,
            skipped_oversize, skipped_oversize_bytes,
            artifacts_max_bytes, ARTIFACTS_MAX_BYTES_ENV,
        )
    else:
        log.info(
            "snapshotted %d ignored file(s) (%d bytes) for %s to %s%s",
            len(kept), total, card_id, snapshot,
            f"; skipped {skipped_pattern} cache-pattern path(s)"
            if skipped_pattern else "",
        )
    return snapshot, None
