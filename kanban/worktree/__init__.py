"""Per-card Git worktree isolation manager.

Creates, queries, detaches, and prunes Git worktrees so each kanban card
can execute in an isolated branch without conflicting with concurrent work.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..models import CardStatus
from .types import (  # noqa: F401
    ARTIFACT_DIR_NAME_RE,
    ARTIFACTS_MAX_BYTES_ENV,
    BRANCH_PREFIX,
    DEFAULT_ARTIFACTS_DENYLIST,
    DEFAULT_ARTIFACTS_MAX_BYTES,
    DEFAULT_ARTIFACTS_RETENTION,
    DetachResult,
    WorktreeCreateError,
    WorktreeDiffError,
    WorktreeInfo,
    _is_denylisted,
)

log = logging.getLogger("kanban.worktree")


@dataclass
class WorktreeManager:
    project_root: Path
    worktrees_root: Path
    base_ref: str = "HEAD"
    # Where to snapshot gitignored worktree content before detach.
    # ``workspace/`` is .gitignored in this project (and most projects
    # using kanban), so a worker's deliverables under ``workspace/...``
    # are invisible to ``_auto_commit`` and would be deleted with the
    # worktree directory. The snapshot rescues them. None disables.
    artifacts_root: Path | None = None
    artifacts_retention: int = DEFAULT_ARTIFACTS_RETENTION
    artifacts_max_bytes: int = DEFAULT_ARTIFACTS_MAX_BYTES
    artifacts_denylist: tuple[str, ...] = DEFAULT_ARTIFACTS_DENYLIST
    # When False, skip the ``.git/info/exclude`` bookkeeping in
    # ``__post_init__``. Set this for read-only probes (e.g. result
    # rendering behind the read-only web endpoint) that must not touch
    # the repository at all.
    manage_exclude: bool = True

    @classmethod
    def for_project(
        cls,
        project_root: Path,
        *,
        base_ref: str = "HEAD",
        manage_exclude: bool = True,
    ) -> WorktreeManager:
        """Build a manager for the conventional layout: worktrees under
        ``<project_root>/workspace/worktrees`` and artifact snapshots under
        ``<project_root>/workspace/raw``. Pass ``manage_exclude=False`` for
        a side-effect-free, read-only manager."""
        return cls(
            project_root=project_root,
            worktrees_root=project_root / "workspace" / "worktrees",
            artifacts_root=project_root / "workspace" / "raw",
            base_ref=base_ref,
            manage_exclude=manage_exclude,
        )

    def __post_init__(self) -> None:
        if self.manage_exclude:
            self._ensure_ignored()
        # Env override applied per-instance so test fixtures and
        # subprocesses can dial the cap without changing call sites.
        env_cap = os.environ.get(ARTIFACTS_MAX_BYTES_ENV)
        if env_cap:
            try:
                self.artifacts_max_bytes = int(env_cap)
            except ValueError:
                log.warning(
                    "ignoring %s=%r (not an integer)",
                    ARTIFACTS_MAX_BYTES_ENV, env_cap,
                )

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=self.project_root,
            capture_output=True,
            text=True,
            check=check,
        )

    def _ensure_ignored(self) -> None:
        """Add the worktrees root to ``.git/info/exclude`` so linked worktrees
        don't show up as untracked in the main checkout. Uses local-only
        exclude so we don't mutate the tracked ``.gitignore``.
        """
        try:
            git_dir = subprocess.run(
                ["git", "rev-parse", "--git-common-dir"],
                cwd=self.project_root, capture_output=True, text=True, check=False,
            )
            if git_dir.returncode != 0:
                return
            common_dir = Path(git_dir.stdout.strip())
            if not common_dir.is_absolute():
                common_dir = (self.project_root / common_dir).resolve()
            exclude_file = common_dir / "info" / "exclude"
            exclude_file.parent.mkdir(parents=True, exist_ok=True)
            try:
                rel = self.worktrees_root.resolve().relative_to(
                    self.project_root.resolve()
                )
            except ValueError:
                return
            entry = f"/{rel.as_posix()}/"
            existing = exclude_file.read_text() if exclude_file.exists() else ""
            if entry in existing.splitlines():
                return
            with exclude_file.open("a") as fh:
                if existing and not existing.endswith("\n"):
                    fh.write("\n")
                fh.write(f"# kanban worktree isolation\n{entry}\n")
        except OSError as exc:
            log.warning("could not update .git/info/exclude: %s", exc)

    def create(self, card_id: str) -> WorktreeInfo:
        base_commit = self._git("rev-parse", self.base_ref).stdout.strip()
        branch = f"{BRANCH_PREFIX}{card_id}"
        self.worktrees_root.mkdir(parents=True, exist_ok=True)
        wt_path = self.worktrees_root / card_id
        try:
            self._git(
                "worktree", "add", "-b", branch, str(wt_path), base_commit,
            )
        except subprocess.CalledProcessError as exc:
            raise WorktreeCreateError(
                f"Failed to create worktree for {card_id}: {exc.stderr.strip()}"
            ) from exc
        head = self._git("rev-parse", branch).stdout.strip()
        log.info("created worktree %s at %s (base %s)", branch, wt_path, base_commit[:12])
        return WorktreeInfo(
            card_id=card_id,
            path=wt_path,
            branch=branch,
            base_commit=base_commit,
            head_commit=head,
        )

    def _is_managed_path(self, wt_path_str: str | None) -> bool:
        """Whether ``wt_path_str`` lives under the managed worktrees root.

        Used to ignore branches checked out in the main repo (or anywhere
        outside ``self.worktrees_root``) so we never hand the shared
        checkout to a worker as if it were an isolated worktree.
        """
        if not wt_path_str:
            return False
        try:
            wt_resolved = Path(wt_path_str).resolve()
            root_resolved = self.worktrees_root.resolve()
        except OSError:
            return False
        try:
            wt_resolved.relative_to(root_resolved)
        except ValueError:
            return False
        return True

    def get(self, card_id: str, base_commit: str = "") -> WorktreeInfo | None:
        branch = f"{BRANCH_PREFIX}{card_id}"
        entries = self._parse_worktree_list()
        for entry in entries:
            if entry.get("branch") != f"refs/heads/{branch}":
                continue
            wt_path_str = entry.get("worktree")
            # If the branch is checked out somewhere outside the managed
            # worktrees root (e.g., the main repo checkout), ignore that
            # entry. Otherwise a worker would run in the shared checkout
            # and detach()/prune_stale() — which only look under
            # ``self.worktrees_root`` — would miss the active checkout.
            if not self._is_managed_path(wt_path_str):
                continue
            head = entry.get("HEAD", "")
            wt_path = Path(wt_path_str) if wt_path_str else None
            if wt_path and not wt_path.exists():
                wt_path = None
            return WorktreeInfo(
                card_id=card_id,
                path=wt_path,
                branch=branch,
                base_commit=base_commit,
                head_commit=head,
            )
        result = self._git("rev-parse", "--verify", branch, check=False)
        if result.returncode == 0:
            return WorktreeInfo(
                card_id=card_id,
                path=None,
                branch=branch,
                base_commit=base_commit,
                head_commit=result.stdout.strip(),
            )
        return None

    def recheckout(self, card_id: str, branch: str) -> Path | None:
        """Re-attach a worktree for an existing branch after detach.

        If the target directory already exists, verify it's a valid linked
        worktree for ``branch`` before returning it; otherwise remove the
        stale dir and create a fresh worktree.
        """
        wt_path = self.worktrees_root / card_id
        if wt_path.exists():
            if self._is_valid_worktree_for(wt_path, branch):
                return wt_path
            log.warning(
                "stale directory at %s is not a valid worktree for %s; removing",
                wt_path, branch,
            )
            import shutil
            shutil.rmtree(wt_path, ignore_errors=True)
        # Clear any stale admin entries from `git worktree list` — e.g. if
        # `workspace/worktrees/` was deleted out-of-band, git still thinks
        # the branch is checked out there and blocks `git worktree add`.
        self._git("worktree", "prune", check=False)
        self.worktrees_root.mkdir(parents=True, exist_ok=True)
        result = self._git(
            "worktree", "add", str(wt_path), branch, check=False,
        )
        if result.returncode != 0:
            log.warning(
                "failed to recheckout worktree for %s: %s",
                card_id, result.stderr.strip(),
            )
            return None
        log.info("re-checked-out worktree %s at %s", branch, wt_path)
        return wt_path

    def _is_valid_worktree_for(self, wt_path: Path, branch: str) -> bool:
        """Check whether ``wt_path`` is a linked worktree pointing at ``branch``."""
        try:
            resolved = wt_path.resolve()
        except OSError:
            return False
        for entry in self._parse_worktree_list():
            entry_path = entry.get("worktree")
            if not entry_path:
                continue
            try:
                if Path(entry_path).resolve() != resolved:
                    continue
            except OSError:
                continue
            return entry.get("branch") == f"refs/heads/{branch}"
        return False

    def detach(self, card_id: str) -> DetachResult:
        """Remove the worktree directory, keeping the branch.

        Order of operations matters: gitignored deliverables are
        snapshotted *before* ``git worktree remove --force`` because that
        command deletes the directory tree, taking ignored files with
        it. ``_auto_commit`` only catches tracked + untracked-not-ignored
        paths, so anything written under a gitignored prefix
        (commonly ``workspace/`` in this project) would otherwise be
        unrecoverable.

        Returns a :class:`DetachResult`. ``removed=False`` is reserved
        for the auto-commit-failed branch where the worktree dir is
        kept to preserve uncommitted work.
        """
        wt_path = self.worktrees_root / card_id
        if not wt_path.exists():
            self._git("worktree", "prune", check=False)
            return DetachResult(removed=True)

        artifacts_path: Path | None = None
        skipped_reason: str | None = None
        try:
            artifacts_path, skipped_reason = self._save_artifacts(card_id, wt_path)
        except Exception:  # noqa: BLE001 — never let snapshot abort detach
            log.exception(
                "artifact snapshot failed for %s; continuing with detach",
                card_id,
            )

        if not self._auto_commit(card_id, wt_path):
            log.warning(
                "skipping worktree removal for %s — auto-commit failed",
                card_id,
            )
            return DetachResult(
                removed=False,
                artifacts_path=artifacts_path,
                artifacts_skipped_reason=skipped_reason,
            )
        self._git("worktree", "remove", str(wt_path), "--force")
        log.info("detached worktree %s (branch preserved)", wt_path)
        return DetachResult(
            removed=True,
            artifacts_path=artifacts_path,
            artifacts_skipped_reason=skipped_reason,
        )

    def _save_artifacts(
        self, card_id: str, wt_path: Path
    ) -> tuple[Path | None, str | None]:
        """Snapshot gitignored worktree content before detach.

        Returns ``(snapshot_path, skipped_reason)``:

        - ``(Path, None)`` when files were rescued (possibly partial)
        - ``(None, "no-artifacts")`` when nothing was eligible
        - ``(None, "<reason>")`` when nothing was rescued for a specific reason
        - ``(None, None)`` when artifacts capture is disabled

        Strategy is best-effort, not all-or-nothing:

        1. Enumerate ignored files with ``git ls-files --others --ignored
           --exclude-standard`` (the set ``_auto_commit`` cannot reach).
        2. Drop paths matching :attr:`artifacts_denylist` (build caches,
           ``node_modules``, ``__pycache__``, etc.) so a single bloated
           cache directory never crowds out real deliverables.
        3. Walk the rest in git's enumeration order, copying each file
           that fits within the remaining size budget. Once the cap is
           reached, subsequent files are skipped (not the whole snapshot).

        Untracked-but-not-ignored files are already covered by
        ``git add -A`` in ``_auto_commit`` and are intentionally *not*
        included here to avoid duplicating what ends up in the rescue
        commit.
        """
        if self.artifacts_root is None or self.artifacts_retention <= 0:
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
        # Split on NUL (the -z form) and drop the trailing empty entry.
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
            if _is_denylisted(rel, self.artifacts_denylist):
                skipped_pattern += 1
                continue
            src = wt_path / rel
            try:
                st = src.lstat()
            except OSError:
                continue
            size = st.st_size
            if total + size > self.artifacts_max_bytes:
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
                    self.artifacts_max_bytes, ARTIFACTS_MAX_BYTES_ENV,
                )
                return None, "size-cap-exceeded"
            return None, "no-artifacts"

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        card_dir = self.artifacts_root / card_id
        snapshot = card_dir / f"artifacts-{stamp}"
        snapshot.mkdir(parents=True, exist_ok=True)
        wt_resolved = wt_path.resolve()
        for src, rel_str, _ in kept:
            try:
                rel = src.resolve(strict=False).relative_to(wt_resolved)
            except ValueError:
                # Symlink resolved outside the worktree — copy it as a
                # symlink at its declared relative path instead, so we
                # never follow it out of the sandbox.
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

        # Retention: keep the most recent N artifact dirs per card.
        existing = sorted(card_dir.glob("artifacts-*"))
        for stale in existing[: -self.artifacts_retention]:
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
                self.artifacts_max_bytes, ARTIFACTS_MAX_BYTES_ENV,
            )
        else:
            log.info(
                "snapshotted %d ignored file(s) (%d bytes) for %s to %s%s",
                len(kept), total, card_id, snapshot,
                f"; skipped {skipped_pattern} cache-pattern path(s)"
                if skipped_pattern else "",
            )
        return snapshot, None

    def _auto_commit(self, card_id: str, wt_path: Path) -> bool:
        """Commit any uncommitted changes so they survive worktree removal.

        Returns True if no uncommitted changes exist or if the commit
        succeeded. Returns False if the commit failed (caller must not
        delete the worktree).
        """
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=wt_path, capture_output=True, text=True, check=False,
        )
        if not status.stdout.strip():
            return True
        subprocess.run(
            ["git", "add", "-A"], cwd=wt_path, capture_output=True, check=False,
        )
        # Pin identity inline so detach works on machines (CI, fresh repos)
        # that have no user.name/user.email configured.
        result = subprocess.run(
            [
                "git",
                "-c", "user.name=kanban",
                "-c", "user.email=kanban@local",
                "commit",
                "-m", f"kanban: auto-save for {card_id}",
            ],
            cwd=wt_path, capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            log.error(
                "auto-commit failed in %s, worktree preserved: %s",
                wt_path, result.stderr.strip(),
            )
            return False
        log.info("auto-committed uncommitted changes in %s", wt_path)
        return True

    def prune_branch(self, card_id: str, *, force: bool = False) -> bool:
        branch = f"{BRANCH_PREFIX}{card_id}"
        flag = "-D" if force else "-d"
        result = self._git("branch", flag, branch, check=False)
        if result.returncode == 0:
            log.info("pruned branch %s (force=%s)", branch, force)
            return True
        return False

    def prune_stale(
        self,
        card_statuses: dict[str, CardStatus],
        retention_days: int = 7,
        *,
        card_blocked_at: dict[str, datetime] | None = None,
    ) -> list[str]:
        """Prune stale branches.

        For BLOCKED cards, age is measured from ``card.blocked_at`` — the
        timestamp persisted when the card transitioned into BLOCKED. Using
        ``card.updated_at`` would let unrelated edits (worktree metadata,
        manual tweaks) reset the clock, and the branch tip commit date
        would inherit the base commit's age for cards that never produced
        a commit.
        """
        pruned: list[str] = []
        now = datetime.now(timezone.utc)
        merged_result = self._git("branch", "--merged", self.base_ref, check=False)
        merged_branches = set()
        if merged_result.returncode == 0:
            for line in merged_result.stdout.splitlines():
                merged_branches.add(line.strip().lstrip("* "))

        card_blocked_at = card_blocked_at or {}

        for card_id, status in card_statuses.items():
            branch = f"{BRANCH_PREFIX}{card_id}"
            wt_path = self.worktrees_root / card_id
            if wt_path.exists():
                continue
            branch_exists = self._git(
                "rev-parse", "--verify", f"refs/heads/{branch}", check=False,
            ).returncode == 0
            if not branch_exists:
                continue

            if status == CardStatus.DONE and branch in merged_branches:
                if self.prune_branch(card_id):
                    pruned.append(card_id)
            elif status == CardStatus.BLOCKED:
                blocked_at = card_blocked_at.get(card_id)
                if blocked_at is None:
                    # No block timestamp recorded — legacy card or missing
                    # metadata; skip rather than guess its age.
                    continue
                if blocked_at.tzinfo is None:
                    blocked_at = blocked_at.replace(tzinfo=timezone.utc)
                age = (now - blocked_at).days
                if age >= retention_days:
                    if self.prune_branch(card_id, force=True):
                        pruned.append(card_id)
        return pruned

    def diff_summary(self, card_id: str, base_commit: str) -> str:
        branch = f"{BRANCH_PREFIX}{card_id}"
        wt_path = self.worktrees_root / card_id
        # Validate refs exist so callers see explicit errors rather than
        # an empty diff that masks missing branch or base commits.
        if not base_commit:
            raise WorktreeDiffError("missing base_commit")
        for ref in (base_commit, f"refs/heads/{branch}"):
            probe = self._git("rev-parse", "--verify", ref, check=False)
            if probe.returncode != 0:
                raise WorktreeDiffError(
                    f"ref not found: {ref} ({probe.stderr.strip()})"
                )
        # Committed changes vs base
        result = self._git("diff", f"{base_commit}...{branch}", "--stat", check=False)
        parts: list[str] = []
        if result.returncode != 0:
            raise WorktreeDiffError(
                f"git diff failed: {result.stderr.strip()}"
            )
        if result.stdout.strip():
            parts.append(result.stdout)
        # Uncommitted changes in the active worktree
        if wt_path.exists():
            wt_diff = subprocess.run(
                ["git", "diff", "HEAD", "--stat"],
                cwd=wt_path, capture_output=True, text=True, check=False,
            )
            untracked = subprocess.run(
                ["git", "ls-files", "--others", "--exclude-standard"],
                cwd=wt_path, capture_output=True, text=True, check=False,
            )
            uncommitted_lines: list[str] = []
            if wt_diff.returncode == 0 and wt_diff.stdout.strip():
                uncommitted_lines.append(wt_diff.stdout.rstrip())
            if untracked.returncode == 0 and untracked.stdout.strip():
                for f in untracked.stdout.strip().splitlines():
                    uncommitted_lines.append(f" {f} (untracked)")
            if uncommitted_lines:
                parts.append("Uncommitted changes:\n" + "\n".join(uncommitted_lines))
        return "\n".join(parts)

    def list_active(self) -> list[WorktreeInfo]:
        entries = self._parse_worktree_list()
        result: list[WorktreeInfo] = []
        for entry in entries:
            branch_ref = entry.get("branch", "")
            if not branch_ref.startswith(f"refs/heads/{BRANCH_PREFIX}"):
                continue
            wt_path_str = entry.get("worktree")
            # Only surface worktrees this manager owns so operator tools
            # never confuse a kanban/<id> branch checked out in the main
            # repo with an isolated worker checkout.
            if not self._is_managed_path(wt_path_str):
                continue
            branch = branch_ref.removeprefix("refs/heads/")
            card_id = branch.removeprefix(BRANCH_PREFIX)
            wt_path = Path(wt_path_str) if wt_path_str else None
            result.append(WorktreeInfo(
                card_id=card_id,
                path=wt_path,
                branch=branch,
                base_commit="",
                head_commit=entry.get("HEAD", ""),
            ))
        return result

    def _parse_worktree_list(self) -> list[dict[str, str]]:
        result = self._git("worktree", "list", "--porcelain", check=False)
        if result.returncode != 0:
            return []
        entries: list[dict[str, str]] = []
        current: dict[str, str] = {}
        for line in result.stdout.splitlines():
            if not line.strip():
                if current:
                    entries.append(current)
                    current = {}
                continue
            if line.startswith("worktree "):
                current["worktree"] = line[len("worktree "):]
            elif line.startswith("HEAD "):
                current["HEAD"] = line[len("HEAD "):]
            elif line.startswith("branch "):
                current["branch"] = line[len("branch "):]
            elif line == "bare":
                current["bare"] = "true"
            elif line == "detached":
                current["detached"] = "true"
        if current:
            entries.append(current)
        return entries
