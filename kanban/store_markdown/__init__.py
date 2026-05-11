from __future__ import annotations

import json
import logging
import os
import tomllib
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..models import (
    AgentResult,
    AgentRole,
    Card,
    CardEvent,
    CardStatus,
    ClaimConflictError,
    ClaimMismatchError,
    ContextRef,
    ExecutionClaim,
    ExecutionResultEnvelope,
    FailureCategory,
    ResourceUsage,
    TraceInfo,
    WorkerPresence,
    coerce_card_status,
    utc_now,
)
from .cards import (  # noqa: F401
    FRONT_MATTER_DELIM,
    _card_from_toml_dict,
    _card_to_toml_dict,
    _coerce_revision_requests,
    _extract_front_matter,
    _read_card,
    _render_body,
    _render_card,
)
from .events import _decode_event_line  # noqa: F401
from .runtime import (  # noqa: F401
    _agent_result_from_json,
    _agent_result_to_json,
    _atomic_write_json,
    _claim_from_json,
    _claim_to_json,
    _iso,
    _parse_iso,
    _resource_from_json,
    _resource_to_json,
    _result_from_json,
    _result_to_json,
    _revision_request_from_json,
    _revision_request_to_json,
    _worker_from_json,
    _worker_to_json,
)
from .toml_dump import (  # noqa: F401
    _dump_toml,
    _toml_inline_table,
    _toml_key,
    _toml_string,
    _toml_value,
)

DEFAULT_RAW_RETENTION = 5

_LOG = logging.getLogger(__name__)


def _tail(items: list[Any], limit: int | None) -> list[Any]:
    """Return the last `limit` items. `None` → all; `<=0` → none."""
    if limit is None:
        return items
    if limit <= 0:
        return []
    return items[-limit:]


class MarkdownBoardStore:
    """Persist board state as one Markdown file per card under <root>/cards/.

    The TOML front-matter between ``+++`` fences is the source of truth. The
    body below is a regenerated human-readable view and is ignored on read.
    Events are appended to ``<root>/events.log`` as tab-separated lines.
    """

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        raw_root: str | os.PathLike[str] | None = None,
        raw_retention: int = DEFAULT_RAW_RETENTION,
    ) -> None:
        self.root = Path(root)
        self.cards_dir = self.root / "cards"
        self.events_path = self.root / "events.log"
        self.runtime_dir = self.root / "runtime"
        self.claims_dir = self.runtime_dir / "claims"
        self.results_dir = self.runtime_dir / "results"
        self.workers_dir = self.runtime_dir / "workers"
        # Raw transcripts live beside the board dir by default (workspace/raw/
        # for a board at workspace/board/). workspace/ is already gitignored.
        self.raw_root = Path(raw_root) if raw_root else self.root.parent / "raw"
        self.raw_retention = raw_retention
        # No mkdir here: read-only callers (e.g. the web server) should be
        # able to instantiate the store without materializing the board on
        # disk. Write paths (`_write_card`, `_write_event_line`, runtime
        # writers) mkdir their own directories lazily.

        self._cards: dict[str, Card] = {}
        self._events: list[CardEvent] = []
        self._unparseable: list[str] = []
        self._load()

    def refresh(self) -> None:
        """Reload cards/events so long-running daemons observe external edits."""
        self._cards = {}
        self._events = []
        self._unparseable = []
        self._load()

    def add_card(self, card: Card) -> Card:
        self._cards[card.id] = card
        self._write_card(card)
        self.append_event(card.id, f"Card created in {card.status.value}")
        return card

    def get_card(self, card_id: str) -> Card:
        return self._cards[card_id]

    def list_cards(self) -> list[Card]:
        return list(self._cards.values())

    def list_by_status(self, status: CardStatus) -> list[Card]:
        cards = [c for c in self._cards.values() if c.status == status]
        return sorted(cards, key=lambda c: (-int(c.priority), c.created_at))

    def move_card(self, card_id: str, status: CardStatus, note: str) -> Card:
        card = self.get_card(card_id)
        previous = card.status
        card.status = status
        if status == CardStatus.BLOCKED and previous != CardStatus.BLOCKED:
            card.blocked_at = utc_now()
        elif status != CardStatus.BLOCKED:
            card.blocked_at = None
        card.add_history(note, role="system")
        self._write_card(card)
        self.append_event(card_id, note)
        return card

    def update_card(self, card_id: str, **updates: object) -> Card:
        card = self.get_card(card_id)
        for key, value in updates.items():
            if key == "context_refs":
                value = [ContextRef.coerce(v) for v in value]  # type: ignore[arg-type]
            elif key == "owner_role" and isinstance(value, str):
                value = AgentRole(value)
            elif key == "status" and isinstance(value, str):
                value = coerce_card_status(value)
            setattr(card, key, value)
        card.updated_at = utc_now()
        self._write_card(card)
        return card

    def append_event(self, card_id: str, message: str) -> None:
        event = CardEvent(card_id=card_id, message=message)
        self._events.append(event)
        record = {"at": event.at.isoformat(), "card_id": card_id, "message": message}
        self._write_event_line(record)

    def append_runtime_event(
        self,
        card_id: str,
        *,
        event_type: str,
        message: str,
        role: AgentRole | None = None,
        claim_id: str | None = None,
        worker_id: str | None = None,
        attempt: int | None = None,
        duration_ms: int | None = None,
        failure_reason: str | None = None,
        failure_category: str | None = None,
        retry_of_claim_id: str | None = None,
        worktree_branch: str | None = None,
        rework_iteration: int | None = None,
    ) -> None:
        """Emit a structured runtime lifecycle event (plan §Event Model Upgrade).

        Separate from ``append_event`` so readers (``doctor``, ``events``
        CLI) can filter by ``event_type`` without scraping messages.
        """
        at = utc_now()
        event = CardEvent(
            card_id=card_id,
            message=message,
            at=at,
            role=role,
            attempt=attempt,
            duration_ms=duration_ms,
            event_type=event_type,
            claim_id=claim_id,
            worker_id=worker_id,
            failure_reason=failure_reason,
            failure_category=failure_category,
            retry_of_claim_id=retry_of_claim_id,
            worktree_branch=worktree_branch,
            rework_iteration=rework_iteration,
        )
        self._events.append(event)
        record: dict[str, Any] = {
            "at": at.isoformat(),
            "card_id": card_id,
            "message": message,
            "event_type": event_type,
        }
        if role is not None:
            record["role"] = role.value
        for key, value in (
            ("claim_id", claim_id),
            ("worker_id", worker_id),
            ("attempt", attempt),
            ("duration_ms", duration_ms),
            ("failure_reason", failure_reason),
            ("failure_category", failure_category),
            ("retry_of_claim_id", retry_of_claim_id),
            ("worktree_branch", worktree_branch),
            ("rework_iteration", rework_iteration),
        ):
            if value is not None:
                record[key] = value
        self._write_event_line(record)

    def append_execution_event(self, card_id: str, result: AgentResult) -> None:
        at = utc_now()
        raw_path_str: str | None = None
        if result.raw_response is not None:
            raw_path = self._write_raw_transcript(
                card_id, result.role, at, result.raw_response
            )
            if raw_path is not None:
                try:
                    raw_path_str = str(raw_path.relative_to(self.root.parent))
                except ValueError:
                    raw_path_str = str(raw_path)
        event = CardEvent(
            card_id=card_id,
            message=result.summary,
            at=at,
            role=result.role,
            prompt_version=result.prompt_version,
            duration_ms=result.duration_ms,
            attempt=result.attempt,
            raw_path=raw_path_str,
            agent_profile=result.agent_profile,
            backend_type=result.backend_type,
            backend_target=result.backend_target,
            routing_source=result.routing_source,
            routing_reason=result.routing_reason,
            fallback_from_profile=result.fallback_from_profile,
            session_id=result.session_id,
            router_prompt_version=result.router_prompt_version,
            backend_metadata=dict(result.backend_metadata),
        )
        self._events.append(event)
        record: dict[str, Any] = {
            "at": at.isoformat(),
            "card_id": card_id,
            "role": result.role.value,
            "prompt_version": result.prompt_version,
            "duration_ms": result.duration_ms,
            "attempt": result.attempt,
            "message": result.summary,
        }
        if raw_path_str is not None:
            record["raw_path"] = raw_path_str
        for field_name in (
            "agent_profile",
            "backend_type",
            "backend_target",
            "routing_source",
            "routing_reason",
            "fallback_from_profile",
            "session_id",
            "router_prompt_version",
        ):
            value = getattr(result, field_name)
            if value is not None:
                record[field_name] = value
        if result.backend_metadata:
            record["backend_metadata"] = dict(result.backend_metadata)
        self._write_event_line(record)

    def list_events(self, *, limit: int | None = None) -> list[CardEvent]:
        return _tail(list(self._events), limit)

    def list_execution_events(
        self,
        *,
        card_id: str | None = None,
        role: AgentRole | None = None,
        limit: int | None = None,
    ) -> list[CardEvent]:
        events = [e for e in self._events if e.is_execution]
        if card_id is not None:
            events = [e for e in events if e.card_id == card_id]
        if role is not None:
            events = [e for e in events if e.role == role]
        return _tail(events, limit)

    def list_traces(
        self,
        card_id: str,
        *,
        role: AgentRole | None = None,
        latest: bool = False,
    ) -> list[TraceInfo]:
        card_dir = self.raw_root / card_id
        if not card_dir.exists():
            return []
        traces: list[TraceInfo] = []
        for path in sorted(card_dir.glob("*.md")):
            role_str, sep, stamp_with_ext = path.name.partition("-")
            if not sep or not stamp_with_ext.endswith(".md"):
                continue
            try:
                this_role = AgentRole(role_str)
            except ValueError:
                continue
            if role is not None and this_role != role:
                continue
            stamp_str = stamp_with_ext[:-3]
            try:
                at = datetime.strptime(stamp_str, "%Y%m%dT%H%M%S%fZ").replace(
                    tzinfo=UTC
                )
            except ValueError:
                at = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            traces.append(
                TraceInfo(
                    card_id=card_id,
                    role=this_role,
                    at=at,
                    path=str(path),
                    size=path.stat().st_size,
                )
            )
        if latest and traces:
            return [max(traces, key=lambda t: t.at)]
        return traces

    def _write_event_line(self, record: dict[str, Any]) -> None:
        # O_APPEND writes are atomic for < PIPE_BUF on POSIX, so concurrent
        # CLI + daemon writers don't interleave whole lines.
        self.root.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
        fd = os.open(self.events_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)

    def _write_raw_transcript(
        self,
        card_id: str,
        role: AgentRole,
        at: datetime,
        raw_response: str,
    ) -> Path | None:
        if self.raw_retention <= 0:
            return None
        card_dir = self.raw_root / card_id
        card_dir.mkdir(parents=True, exist_ok=True)
        stamp = at.strftime("%Y%m%dT%H%M%S%fZ")
        path = card_dir / f"{role.value}-{stamp}.md"
        path.write_text(raw_response, encoding="utf-8")
        # Retention: keep the most recent N per (card, role).
        role_files = sorted(card_dir.glob(f"{role.value}-*.md"))
        for stale in role_files[: -self.raw_retention]:
            try:
                stale.unlink()
            except OSError:
                pass
        return path

    def events_for_card(self, card_id: str) -> list[CardEvent]:
        return [e for e in self._events if e.card_id == card_id]

    def board_snapshot(self) -> dict[str, list[str]]:
        grouped: dict[str, list[str]] = defaultdict(list)
        for card in self.list_cards():
            grouped[card.status.value].append(card.title)
        return dict(grouped)

    # ---------- v0.1.2 runtime surface ----------

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

    # ---------- internals ----------

    def _card_path(self, card_id: str) -> Path:
        return self.cards_dir / f"{card_id}.md"

    def _claim_path(self, card_id: str) -> Path:
        return self.claims_dir / f"{card_id}.json"

    def _result_path(self, card_id: str, claim_id: str) -> Path:
        return self.results_dir / f"{card_id}-{claim_id}.json"

    def _worker_path(self, worker_id: str) -> Path:
        return self.workers_dir / f"{worker_id}.json"

    def _load(self) -> None:
        for path in sorted(self.cards_dir.glob("*.md")):
            # Valid TOML can still miss fields the Card constructor requires
            # (TypeError) or reject a value type (KeyError/ValueError). Treat
            # any failure in this reader path as an unparseable card rather
            # than letting a single bad file break the whole board.
            try:
                card = _read_card(path)
            except (tomllib.TOMLDecodeError, TypeError, KeyError, ValueError) as exc:
                _LOG.warning("Skipping unparseable card %s: %s", path.name, exc)
                self._unparseable.append(path.name)
                continue
            self._cards[card.id] = card
        if self.events_path.exists():
            self._load_events()

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

    def unparseable_cards(self) -> list[str]:
        return list(self._unparseable)

    def _load_events(self) -> None:
        with self.events_path.open("r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                event = _decode_event_line(line)
                if event is not None:
                    self._events.append(event)

    def _write_card(self, card: Card) -> None:
        self.cards_dir.mkdir(parents=True, exist_ok=True)
        path = self._card_path(card.id)
        tmp = path.with_suffix(".md.tmp")
        content = _render_card(card)
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)
