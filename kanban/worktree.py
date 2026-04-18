"""Per-card Git worktree isolation manager.

Creates, queries, detaches, and prunes Git worktrees so each kanban card
can execute in an isolated branch without conflicting with concurrent work.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .models import CardStatus

log = logging.getLogger("kanban.worktree")

BRANCH_PREFIX = "kanban/"


class WorktreeCreateError(RuntimeError):
    """Raised when ``git worktree add`` fails."""


class WorktreeDiffError(RuntimeError):
    """Raised when ``diff_summary`` cannot resolve refs or run git diff."""


@dataclass
class WorktreeInfo:
    card_id: str
    path: Path | None
    branch: str
    base_commit: str
    head_commit: str


@dataclass
class WorktreeManager:
    project_root: Path
    worktrees_root: Path
    base_ref: str = "HEAD"

    def __post_init__(self) -> None:
        self._ensure_ignored()

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

    def detach(self, card_id: str) -> bool:
        """Remove the worktree directory, keeping the branch.

        Returns True if the worktree was removed (or never existed), False
        if the removal was aborted to preserve uncommitted work.
        """
        wt_path = self.worktrees_root / card_id
        if wt_path.exists():
            if not self._auto_commit(card_id, wt_path):
                log.warning(
                    "skipping worktree removal for %s — auto-commit failed",
                    card_id,
                )
                return False
            self._git("worktree", "remove", str(wt_path), "--force")
            log.info("detached worktree %s (branch preserved)", wt_path)
        else:
            self._git("worktree", "prune", check=False)
        return True

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
