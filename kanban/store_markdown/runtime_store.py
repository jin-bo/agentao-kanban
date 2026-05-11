from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from ..models import (
    ClaimConflictError,
    ClaimMismatchError,
    ExecutionClaim,
    ExecutionResultEnvelope,
    WorkerPresence,
    utc_now,
)
from .component import StoreComponent
from .runtime import (
    _atomic_write_json,
    _claim_from_json,
    _claim_to_json,
    _result_from_json,
    _result_to_json,
    _worker_from_json,
    _worker_to_json,
)
from .store_utils import _LOG


class RuntimeStore(StoreComponent):
    def create_claim(self, claim: ExecutionClaim) -> ExecutionClaim:
        self.claims_dir.mkdir(parents=True, exist_ok=True)
        path = self._claim_path(claim.card_id)
        if path.exists():
            raise ClaimConflictError(
                f"claim already exists for card {claim.card_id}: {path}"
            )
        _atomic_write_json(path, _claim_to_json(claim))
        return claim

    def get_claim(self, card_id: str) -> ExecutionClaim | None:
        path = self._claim_path(card_id)
        if not path.is_file():
            return None
        try:
            return _claim_from_json(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            _LOG.warning("Skipping unparseable claim %s: %s", path.name, exc)
            return None

    def renew_claim(
        self,
        card_id: str,
        *,
        claim_id: str,
        heartbeat_at: datetime,
        lease_expires_at: datetime,
        worker_id: str | None = None,
    ) -> ExecutionClaim:
        current = self.get_claim(card_id)
        if current is None:
            raise KeyError(f"no claim for card {card_id}")
        if current.claim_id != claim_id:
            raise ClaimMismatchError(
                f"claim_id mismatch for {card_id}: "
                f"expected {current.claim_id}, got {claim_id}"
            )
        from dataclasses import replace

        updated = replace(
            current,
            heartbeat_at=heartbeat_at,
            lease_expires_at=lease_expires_at,
            worker_id=worker_id if worker_id is not None else current.worker_id,
        )
        _atomic_write_json(self._claim_path(card_id), _claim_to_json(updated))
        return updated

    def clear_claim(self, card_id: str, *, claim_id: str | None = None) -> None:
        current = self.get_claim(card_id)
        if current is None:
            return
        if claim_id is not None and current.claim_id != claim_id:
            raise ClaimMismatchError(
                f"claim_id mismatch for {card_id}: "
                f"expected {current.claim_id}, got {claim_id}"
            )
        try:
            self._claim_path(card_id).unlink()
        except FileNotFoundError:
            pass

    def list_claims(self) -> list[ExecutionClaim]:
        if not self.claims_dir.is_dir():
            return []
        claims: list[ExecutionClaim] = []
        for path in sorted(self.claims_dir.glob("*.json")):
            try:
                claims.append(
                    _claim_from_json(json.loads(path.read_text(encoding="utf-8")))
                )
            except FileNotFoundError:
                # Glob → read is non-atomic. A parallel committer or
                # scheduler may clear the claim between the listdir and
                # the read. That's a legitimate "claim is gone" signal,
                # not an error.
                continue
            except (json.JSONDecodeError, KeyError, ValueError) as exc:
                _LOG.warning("Skipping unparseable claim %s: %s", path.name, exc)
        return claims

    def list_stale_claims(
        self, *, now: datetime | None = None
    ) -> list[ExecutionClaim]:
        cutoff = now or utc_now()
        return [c for c in self.list_claims() if c.lease_expires_at < cutoff]

    def try_acquire_claim(
        self,
        card_id: str,
        *,
        worker_id: str,
        heartbeat_at: datetime | None = None,
        lease_expires_at: datetime | None = None,
    ) -> ExecutionClaim | None:
        """Atomic compare-and-swap: assign worker_id to an unassigned claim.

        Uses an `O_CREAT|O_EXCL` sentinel next to the claim file to serialize
        concurrent workers attempting to take the same claim. Returns the
        updated claim on success, or None if the claim is missing, already
        assigned, or another worker won the CAS race.
        """
        sentinel = self.claims_dir / f"{card_id}.acquiring"
        try:
            fd = os.open(sentinel, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return None
        try:
            current = self.get_claim(card_id)
            if current is None or current.worker_id is not None:
                return None
            from dataclasses import replace

            updated = replace(
                current,
                worker_id=worker_id,
                heartbeat_at=heartbeat_at or utc_now(),
                lease_expires_at=lease_expires_at or current.lease_expires_at,
            )
            _atomic_write_json(self._claim_path(card_id), _claim_to_json(updated))
            return updated
        finally:
            os.close(fd)
            try:
                sentinel.unlink()
            except FileNotFoundError:
                pass

    def write_result(self, result: ExecutionResultEnvelope) -> None:
        """Persist an envelope write-once per claim.

        Files are keyed by ``<card_id>-<claim_id>.json`` (not by attempt) so
        a second process cannot overwrite a pending envelope for the same
        claim. The write itself uses ``O_CREAT|O_EXCL`` to fail fast if
        the file already exists — this is the storage half of the
        single-writer trust boundary (the commit path verifies worker_id).
        """
        self.results_dir.mkdir(parents=True, exist_ok=True)
        path = self._result_path(result.card_id, result.claim_id)
        payload = (
            json.dumps(_result_to_json(result), ensure_ascii=False, sort_keys=True)
            + "\n"
        ).encode("utf-8")
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError as exc:
            raise FileExistsError(
                f"result envelope for claim {result.claim_id} already exists"
            ) from exc
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)

    def read_results(
        self, *, card_id: str | None = None
    ) -> list[ExecutionResultEnvelope]:
        if not self.results_dir.is_dir():
            return []
        results: list[ExecutionResultEnvelope] = []
        for path in sorted(self.results_dir.glob("*.json")):
            if card_id is not None and not path.name.startswith(f"{card_id}-"):
                continue
            try:
                results.append(
                    _result_from_json(json.loads(path.read_text(encoding="utf-8")))
                )
            except FileNotFoundError:
                # Glob → read is non-atomic under the parallel committer.
                continue
            except (json.JSONDecodeError, KeyError, ValueError) as exc:
                _LOG.warning("Skipping unparseable result %s: %s", path.name, exc)
        return results

    def delete_result(self, card_id: str, claim_id: str) -> None:
        try:
            self._result_path(card_id, claim_id).unlink()
        except FileNotFoundError:
            pass

    def quarantine_result(self, card_id: str, claim_id: str) -> None:
        """Move an orphan result envelope into runtime/results/orphans/.

        Used when the result's claim_id no longer matches the live claim,
        or the submitting worker_id does not match the claim owner. The
        envelope is preserved for audit rather than applied or deleted.
        """
        src = self._result_path(card_id, claim_id)
        if not src.is_file():
            return
        dest_dir = self.results_dir / "orphans"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name
        if dest.exists():
            # Same claim quarantined before — keep a stamped copy.
            suffix = utc_now().strftime("%Y%m%dT%H%M%S%fZ")
            dest = dest_dir / f"{src.stem}-{suffix}.json"
        os.replace(src, dest)

    def list_orphan_results(self) -> list[ExecutionResultEnvelope]:
        orphan_dir = self.results_dir / "orphans"
        if not orphan_dir.is_dir():
            return []
        out: list[ExecutionResultEnvelope] = []
        for path in sorted(orphan_dir.glob("*.json")):
            try:
                out.append(
                    _result_from_json(json.loads(path.read_text(encoding="utf-8")))
                )
            except (json.JSONDecodeError, KeyError, ValueError) as exc:
                _LOG.warning("Skipping unparseable orphan %s: %s", path.name, exc)
        return out

    def heartbeat_worker(self, presence: WorkerPresence) -> WorkerPresence:
        self.workers_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(
            self._worker_path(presence.worker_id), _worker_to_json(presence)
        )
        return presence

    def list_workers(self) -> list[WorkerPresence]:
        if not self.workers_dir.is_dir():
            return []
        workers: list[WorkerPresence] = []
        for path in sorted(self.workers_dir.glob("*.json")):
            try:
                workers.append(
                    _worker_from_json(json.loads(path.read_text(encoding="utf-8")))
                )
            except (json.JSONDecodeError, KeyError, ValueError) as exc:
                _LOG.warning("Skipping unparseable worker %s: %s", path.name, exc)
        return workers

    def remove_worker(self, worker_id: str) -> None:
        try:
            self._worker_path(worker_id).unlink()
        except FileNotFoundError:
            pass

    def _claim_path(self, card_id: str) -> Path:
        return self.claims_dir / f"{card_id}.json"

    def _result_path(self, card_id: str, claim_id: str) -> Path:
        return self.results_dir / f"{card_id}-{claim_id}.json"

    def _worker_path(self, worker_id: str) -> Path:
        return self.workers_dir / f"{worker_id}.json"

    def gc_orphaned_runtime(self) -> int:
        """Remove claim/result files whose card file is missing from disk.

        A card file that exists but fails to parse is treated as present:
        runtime state is preserved so a transient front-matter error or
        merge conflict does not permanently erase in-flight execution
        metadata. Returns count of files removed.
        """
        removed = 0
        known_ids = (
            {p.stem for p in self.cards_dir.glob("*.md")}
            if self.cards_dir.is_dir()
            else set()
        )
        if self.claims_dir.is_dir():
            for path in self.claims_dir.glob("*.json"):
                if path.stem in known_ids:
                    continue
                try:
                    path.unlink()
                    removed += 1
                    _LOG.warning("Removed orphan claim %s (card missing)", path.name)
                except OSError as exc:
                    _LOG.warning("Could not remove orphan claim %s: %s", path.name, exc)
            for path in self.claims_dir.glob("*.acquiring"):
                if path.stem not in known_ids:
                    try:
                        path.unlink()
                        removed += 1
                    except OSError:
                        pass
        if self.results_dir.is_dir():
            for path in self.results_dir.glob("*.json"):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    card_id = str(data.get("card_id", ""))
                except (json.JSONDecodeError, OSError):
                    continue
                if not card_id or card_id in known_ids:
                    continue
                try:
                    path.unlink()
                    removed += 1
                    _LOG.warning("Removed orphan result %s (card missing)", path.name)
                except OSError as exc:
                    _LOG.warning("Could not remove orphan result %s: %s", path.name, exc)
        return removed
