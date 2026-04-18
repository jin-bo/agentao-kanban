from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from pathlib import Path
from typing import TYPE_CHECKING

from .executors.base import CardExecutor
from .models import (
    AgentResult,
    AgentRole,
    Card,
    CardPriority,
    CardStatus,
    ExecutionClaim,
    ExecutionEventType,
    ExecutionResultEnvelope,
    FailureCategory,
    LeasePolicy,
    ResourceUsage,
    RetryPolicy,
    RevisionRequest,
    utc_now,
)
from .store import BoardStore

if TYPE_CHECKING:
    from .worktree import WorktreeManager


@dataclass(slots=True)
class WipPolicy:
    doing_limit: int = 2


_WIP_STATUSES = (CardStatus.DOING, CardStatus.REVIEW, CardStatus.VERIFY)

# Sentinel used to distinguish "executor had no `working_directory` attribute"
# from "executor had `working_directory = None`". Dataclass executors like
# `MultiBackendExecutor` default the field to `None`, so a plain `is None`
# check would incorrectly trigger ``del`` and break the next run with
# ``AttributeError``.
_MISSING: object = object()


def detach_worktree_on_terminal(
    store: BoardStore,
    worktree_mgr,
    card_id: str,
    target_status: CardStatus,
) -> None:
    """Detach the card's worktree if the transition is terminal.

    Mirrors the inline logic ``KanbanOrchestrator._apply_normal_result``
    has used since v0.1.3. Factored out so manual CLI transitions
    (``kanban block``, ``kanban move <id> done``, ``card edit
    --set-status``) don't leak attached ``workspace/worktrees/<card-id>``
    directories — once attached, ``worktree prune`` skips the branch
    because the directory still exists.

    No-op when:

    - ``worktree_mgr`` is ``None`` (board not git-backed),
    - ``target_status`` is not ``DONE`` / ``BLOCKED``, or
    - the card was never attached to a worktree.
    """
    if worktree_mgr is None:
        return
    if target_status not in (CardStatus.DONE, CardStatus.BLOCKED):
        return
    card = store.get_card(card_id)
    if card.worktree_branch is None:
        return
    if worktree_mgr.detach(card_id):
        store.append_runtime_event(
            card_id,
            event_type="worktree.detached",
            message=f"Worktree detached: {card.worktree_branch}",
            worktree_branch=card.worktree_branch,
        )
    else:
        store.append_runtime_event(
            card_id,
            event_type="worktree.detach_failed",
            message=(
                f"Worktree detach aborted (uncommitted changes preserved): "
                f"{card.worktree_branch}"
            ),
            worktree_branch=card.worktree_branch,
        )


def _patch_executor_cwd(executor, worktree_path: Path):
    """Point the executor (and its router policy / client) at ``worktree_path``.

    Returns a ``restore()`` callable that puts every patched attribute back.
    Without walking into ``executor.policy`` / ``policy.client``, a card
    running under per-card worktree isolation would have the backend
    invocation read from the worktree while the router agent still read
    from the shared checkout — defeating isolation for profile selection.

    The executor itself is patched unconditionally (mirrors the v0.1.3
    contract: ``MockAgentaoExecutor`` has no ``working_directory`` field
    but the legacy/serial and worker paths have always patched it). For
    the router policy and its lazily-loaded client we only patch when
    the attribute already exists, so simple callable policies are left
    alone.
    """
    saved: list[tuple[object, object]] = []

    saved.append((executor, getattr(executor, "working_directory", _MISSING)))
    executor.working_directory = worktree_path

    policy = getattr(executor, "policy", None)
    if policy is not None and hasattr(policy, "working_directory"):
        saved.append((policy, policy.working_directory))
        policy.working_directory = worktree_path
        client = getattr(policy, "client", None)
        if client is not None and hasattr(client, "working_directory"):
            saved.append((client, client.working_directory))
            client.working_directory = worktree_path

    def restore() -> None:
        for target, prev in saved:
            if prev is _MISSING:
                if hasattr(target, "working_directory"):
                    try:
                        del target.working_directory
                    except AttributeError:
                        pass
            else:
                target.working_directory = prev

    return restore


class WorktreeMissingError(RuntimeError):
    """Raised when a retry cannot proceed because the card's worktree
    branch was deleted and cannot be recovered. Caller should BLOCK."""


class KanbanOrchestrator:
    def __init__(
        self,
        store: BoardStore,
        executor: CardExecutor,
        wip_policy: WipPolicy | None = None,
        lease_policy: LeasePolicy | None = None,
        retry_policy: RetryPolicy | None = None,
        worktree_mgr: WorktreeManager | None = None,
    ) -> None:
        self.store = store
        self.executor = executor
        self.wip_policy = wip_policy or WipPolicy()
        self.lease_policy = lease_policy or LeasePolicy()
        self.retry_policy = retry_policy or RetryPolicy()
        self.worktree_mgr = worktree_mgr

    def create_card(
        self,
        title: str,
        goal: str,
        priority: CardPriority = CardPriority.MEDIUM,
        acceptance_criteria: list[str] | None = None,
        depends_on: list[str] | None = None,
    ) -> Card:
        card = Card(
            title=title,
            goal=goal,
            priority=priority,
            acceptance_criteria=acceptance_criteria or [],
            depends_on=depends_on or [],
        )
        return self.store.add_card(card)

    def block(self, card_id: str, reason: str) -> Card:
        self.store.update_card(card_id, blocked_reason=reason)
        card = self.store.move_card(
            card_id, CardStatus.BLOCKED, f"Blocked: {reason}"
        )
        detach_worktree_on_terminal(
            self.store, self.worktree_mgr, card_id, CardStatus.BLOCKED
        )
        return card

    def unblock(self, card_id: str, target: CardStatus = CardStatus.INBOX) -> Card:
        self.store.update_card(card_id, blocked_reason=None)
        card = self.store.move_card(card_id, target, f"Unblocked to {target.value}")
        detach_worktree_on_terminal(
            self.store, self.worktree_mgr, card_id, target
        )
        return card

    LEGACY_SERIAL_WORKER_ID = "local-serial"

    def tick(self) -> Card | None:
        """Legacy serial path: select → claim → execute → apply in one process.

        Preserved for `kanban daemon --role legacy-serial` and the existing
        `kanban tick` / `kanban run` CLI commands. The claim is created with
        an owner (``LEGACY_SERIAL_WORKER_ID``) so a parallel worker daemon
        cannot steal it between claim and execute — only a split-topology
        scheduler publishes unassigned claims.
        """
        claim = self.select_and_claim(worker_id=self.LEGACY_SERIAL_WORKER_ID)
        if claim is None:
            return None

        card = self.store.get_card(claim.card_id)
        _restore_cwd = None
        if claim.worktree_path is not None:
            _restore_cwd = _patch_executor_cwd(
                self.executor, Path(claim.worktree_path)
            )
        try:
            result = self.executor.run(claim.role, card)
        finally:
            if _restore_cwd is not None:
                _restore_cwd()
        self.apply_claim_result(claim, result)
        return self.store.get_card(claim.card_id)

    # ---------- v0.1.2 scheduler / worker split ----------

    def select_and_claim(self, worker_id: str | None = None) -> ExecutionClaim | None:
        """Scheduler step: pick the next actionable card and create a claim.

        If `worker_id` is None the claim is created unassigned (the open-
        questions decision). A worker daemon later calls
        :meth:`BoardStore.try_acquire_claim` to take ownership. The legacy
        serial path passes `worker_id="local"` so the returned claim is
        immediately owned.

        Walks actionable cards in priority order and skips any that
        already have a live claim — one claimed front card must not
        prevent the scheduler from filling remaining ``max_claims``
        capacity behind it. Cards that fail worktree setup are moved to
        BLOCKED and the scan continues to the next candidate, so a single
        bad card cannot stall lower-priority work in the same tick.
        Returns ``None`` only when *every* actionable card is already
        claimed, blocked, or there is no actionable card at all.
        """
        for candidate in self._iter_actionable_cards():
            if self.store.get_claim(candidate.id) is not None:
                continue

            role = self._role_for(candidate)

            # Status-at-claim reflects where the worker will see the card.
            # READY → DOING at claim time so scheduler owns all workflow-status
            # transitions into executable states.
            status_at_claim = (
                CardStatus.DOING
                if candidate.status == CardStatus.READY
                else candidate.status
            )

            now = utc_now()
            lease_expires = now + timedelta(seconds=self.lease_policy.lease_seconds)
            claim = ExecutionClaim(
                card_id=candidate.id,
                claim_id=f"clm-{uuid4().hex[:12]}",
                role=role,
                status_at_claim=status_at_claim,
                attempt=1,
                claimed_at=now,
                heartbeat_at=now,
                lease_expires_at=lease_expires,
                timeout_s=self.lease_policy.timeout_for(role),
                worker_id=worker_id,
            )

            if not self._setup_worktree_for_claim(candidate, role, claim):
                # Card was blocked by worktree setup; try the next candidate
                # so one broken card does not stall the rest of the queue.
                continue

            # Persist the claim BEFORE moving status. If create_claim raises
            # (stale sentinel, fs error, duplicate), the card stays in READY —
            # the scheduler will simply retry on the next tick. Only after the
            # claim is safely on disk do we advance the card, so we can never
            # leave a DOING card without a live claim.
            self.store.create_claim(claim)
            if candidate.status == CardStatus.READY:
                try:
                    self.store.move_card(
                        candidate.id, CardStatus.DOING,
                        "Dispatcher moved card to doing",
                    )
                except Exception:
                    # Roll back the claim so the next tick can retry cleanly.
                    try:
                        self.store.clear_claim(
                            candidate.id, claim_id=claim.claim_id,
                        )
                    except Exception:  # noqa: BLE001 — best-effort rollback
                        pass
                    raise
            return claim
        return None

    def _setup_worktree_for_claim(
        self, card: Card, role: AgentRole, claim: ExecutionClaim,
    ) -> bool:
        """Resolve the card's worktree and stamp ``claim.worktree_path``.

        Returns ``True`` when the claim is ready to persist. Returns
        ``False`` after blocking the card on a worktree error so the
        caller can move on to the next actionable candidate.
        """
        if self.worktree_mgr is None or role == AgentRole.PLANNER:
            return True

        from .worktree import WorktreeCreateError

        if card.worktree_branch is None and role in (
            AgentRole.REVIEWER, AgentRole.VERIFIER,
        ):
            # Card reached REVIEW/VERIFY without a worktree — either the
            # board pre-dates the worktree feature, or metadata was
            # cleared. Block rather than run unisolated against the
            # worker's (presumably modified) main checkout.
            reason = (
                f"card has no worktree branch; cannot run "
                f"{role.value} in isolation"
            )
            self.store.update_card(card.id, blocked_reason=reason)
            self.store.move_card(
                card.id, CardStatus.BLOCKED, f"Blocked: {reason}",
            )
            self.store.append_runtime_event(
                card.id,
                event_type="worktree.missing",
                message=reason,
            )
            return False

        if card.worktree_branch is None and role == AgentRole.WORKER:
            try:
                wt_info = self.worktree_mgr.create(card.id)
            except WorktreeCreateError as exc:
                self.store.update_card(card.id, blocked_reason=str(exc))
                self.store.move_card(
                    card.id, CardStatus.BLOCKED, f"Blocked: {exc}",
                )
                return False
            self.store.update_card(
                card.id,
                worktree_branch=wt_info.branch,
                worktree_base_commit=wt_info.base_commit,
            )
            claim.worktree_path = str(wt_info.path)
            self.store.append_runtime_event(
                card.id,
                event_type="worktree.created",
                message=(
                    f"Worktree created: {wt_info.branch}"
                    f" from {wt_info.base_commit[:12]}"
                ),
                worktree_branch=wt_info.branch,
            )
            return True

        if card.worktree_branch is not None:
            wt_info = self.worktree_mgr.get(
                card.id, base_commit=card.worktree_base_commit or "",
            )
            if wt_info is not None and wt_info.path is not None:
                claim.worktree_path = str(wt_info.path)
                return True
            if wt_info is not None and wt_info.path is None:
                # Worktree was detached (card unblocked/requeued) — re-checkout
                recheckout = self.worktree_mgr.recheckout(
                    card.id, card.worktree_branch,
                )
                if recheckout is not None:
                    claim.worktree_path = str(recheckout)
                    return True
                reason = (
                    f"failed to recheckout worktree {card.worktree_branch};"
                    " cannot run without isolation"
                )
                self.store.update_card(card.id, blocked_reason=reason)
                self.store.move_card(
                    card.id, CardStatus.BLOCKED, f"Blocked: {reason}",
                )
                self.store.append_runtime_event(
                    card.id,
                    event_type="worktree.missing",
                    message=reason,
                    worktree_branch=card.worktree_branch,
                )
                return False
            if wt_info is None and role == AgentRole.WORKER:
                # Branch was pruned but card metadata is stale. Clear it
                # and create a fresh worktree so isolation is restored.
                try:
                    wt_info_new = self.worktree_mgr.create(card.id)
                except WorktreeCreateError as exc:
                    self.store.update_card(card.id, blocked_reason=str(exc))
                    self.store.move_card(
                        card.id, CardStatus.BLOCKED, f"Blocked: {exc}",
                    )
                    return False
                self.store.update_card(
                    card.id,
                    worktree_branch=wt_info_new.branch,
                    worktree_base_commit=wt_info_new.base_commit,
                )
                claim.worktree_path = str(wt_info_new.path)
                self.store.append_runtime_event(
                    card.id,
                    event_type="worktree.created",
                    message=(
                        f"Worktree recreated after prune: {wt_info_new.branch}"
                        f" from {wt_info_new.base_commit[:12]}"
                    ),
                    worktree_branch=wt_info_new.branch,
                )
                return True
            if wt_info is None and role in (
                AgentRole.REVIEWER, AgentRole.VERIFIER,
            ):
                # Reviewer/verifier cannot run without the worker's branch;
                # block the card rather than silently running in the main
                # checkout.
                reason = (
                    f"worktree branch {card.worktree_branch} missing; "
                    "cannot review/verify deleted work"
                )
                self.store.update_card(card.id, blocked_reason=reason)
                self.store.move_card(
                    card.id, CardStatus.BLOCKED, f"Blocked: {reason}",
                )
                self.store.append_runtime_event(
                    card.id,
                    event_type="worktree.missing",
                    message=reason,
                    worktree_branch=card.worktree_branch,
                )
                return False

        return True

    def apply_claim_result(
        self, claim: ExecutionClaim, result: AgentResult
    ) -> Card:
        """Legacy single-process commit path (used by ``tick()`` and
        ``--role legacy-serial``).

        In the PR2/PR3 split topology, workers call :meth:`submit_result`
        instead and the scheduler/committer picks the envelope up via
        :meth:`commit_pending_results`. This method remains so the serial
        path does not incur the envelope round-trip.
        """
        self._apply_result(claim.card_id, result)
        self.store.clear_claim(claim.card_id, claim_id=claim.claim_id)
        return self.store.get_card(claim.card_id)

    # ---------- v0.1.2 commit path (PR3/M2) ----------

    def submit_result(
        self,
        claim: ExecutionClaim,
        result: AgentResult | None,
        *,
        worker_id: str,
        started_at,
        finished_at=None,
        ok: bool = True,
        failure_reason: str | None = None,
        failure_category: FailureCategory | None = None,
        resource_usage: ResourceUsage | None = None,
    ) -> ExecutionResultEnvelope:
        """Worker step: persist the executor outcome as an envelope.

        Emits **no** runtime events. An envelope's authenticity cannot be
        verified until :meth:`commit_pending_results` (scheduler-side)
        validates the claim_id + worker_id match. Authoritative
        ``execution.finished`` / ``execution.failed`` events are emitted
        there, after ownership checks pass. A forged envelope is
        quarantined and logged as ``execution.result_orphaned`` — it
        cannot leave a success/failure entry in the trusted audit trail.

        The worker MUST NOT move the card or clear the claim after calling
        this; the committer is the sole writer of post-execution
        workflow status transitions.
        """
        finished = finished_at or utc_now()
        duration_ms = int((finished - started_at).total_seconds() * 1000)
        envelope = ExecutionResultEnvelope(
            card_id=claim.card_id,
            claim_id=claim.claim_id,
            role=claim.role,
            attempt=claim.attempt,
            started_at=started_at,
            finished_at=finished,
            duration_ms=duration_ms,
            ok=ok,
            agent_result=result,
            worker_id=worker_id,
            failure_reason=failure_reason,
            failure_category=failure_category,
            resource_usage=resource_usage,
        )
        self.store.write_result(envelope)
        return envelope

    def commit_pending_results(self) -> int:
        """Scheduler/committer: apply any pending result envelopes.

        An envelope is accepted only when all three ownership checks pass:

        1. the envelope's card has a live claim;
        2. the live claim's ``claim_id`` matches the envelope;
        3. the live claim has been acquired (``worker_id is not None``) AND
           the envelope's ``worker_id`` equals the claim's owner.

        Any failure quarantines the envelope as an orphan — a second worker
        that forged a submission under the wrong identity cannot coerce the
        committer into writing its result into the board. Returns the number
        of envelopes processed (committed + quarantined).
        """
        processed = 0
        for env in self.store.read_results():
            # Defense-in-depth: if the card was deleted externally, drop the
            # envelope + any live claim rather than crashing in _apply_result.
            try:
                self.store.get_card(env.card_id)
            except KeyError:
                try:
                    self.store.delete_result(env.card_id, env.claim_id)
                    existing = self.store.get_claim(env.card_id)
                    if existing is not None:
                        self.store.clear_claim(env.card_id, claim_id=existing.claim_id)
                except Exception:  # noqa: BLE001
                    pass
                processed += 1
                continue
            claim = self.store.get_claim(env.card_id)
            if claim is None or claim.claim_id != env.claim_id:
                self._emit_orphan(env, claim, reason="claim_id mismatch")
                self.store.quarantine_result(env.card_id, env.claim_id)
                processed += 1
                continue
            if claim.worker_id is None or claim.worker_id != env.worker_id:
                self._emit_orphan(
                    env,
                    claim,
                    reason=(
                        f"worker_id mismatch (envelope={env.worker_id!r}, "
                        f"claim={claim.worker_id!r})"
                    ),
                )
                self.store.quarantine_result(env.card_id, env.claim_id)
                processed += 1
                continue
            # Ownership verified — emit the authoritative lifecycle event
            # BEFORE applying side effects so the audit trail reflects the
            # trusted outcome even if apply/block transitions fail later.
            event_type = (
                ExecutionEventType.FINISHED.value
                if env.ok
                else ExecutionEventType.FAILED.value
            )
            self.store.append_runtime_event(
                env.card_id,
                event_type=event_type,
                message=(
                    env.failure_reason
                    if env.failure_reason
                    else (
                        env.agent_result.summary
                        if env.agent_result is not None
                        else "executor finished"
                    )
                ),
                role=env.role,
                claim_id=env.claim_id,
                worker_id=env.worker_id,
                attempt=env.attempt,
                duration_ms=env.duration_ms,
                failure_reason=env.failure_reason,
                failure_category=(
                    env.failure_category.value
                    if env.failure_category is not None
                    else None
                ),
            )
            if env.ok and env.agent_result is not None:
                self._apply_result(env.card_id, env.agent_result)
                self.store.clear_claim(env.card_id, claim_id=env.claim_id)
            else:
                self._handle_failed_envelope(env, claim)
            self.store.delete_result(env.card_id, env.claim_id)
            processed += 1
        return processed

    def recover_stale_claims(self, *, now=None) -> int:
        """Scheduler: handle claims whose lease has expired (plan §Stuck Task
        Recovery). Categorized as ``lease_expiry``; consults the retry matrix
        to either create a linked replacement claim or block the card.

        The actual claim-clear + replacement runs under the transactional
        guarantees of :meth:`_retry_or_block`: the stale claim is only
        dropped once the replacement state (retry claim OR BLOCKED) is
        durably established, so a mid-recovery failure can't strand the
        card in an execution status without a claim.
        """
        stale = self.store.list_stale_claims(now=now or utc_now())
        for claim in stale:
            # Drop stale claim if its card was deleted externally.
            try:
                self.store.get_card(claim.card_id)
            except KeyError:
                try:
                    self.store.clear_claim(claim.card_id, claim_id=claim.claim_id)
                except Exception:  # noqa: BLE001
                    pass
                continue
            reason = (
                f"runtime lease expired on attempt {claim.attempt} "
                f"(role={claim.role.value}, claim={claim.claim_id})"
            )
            self.store.append_runtime_event(
                claim.card_id,
                event_type=ExecutionEventType.CLAIM_RECOVERED.value,
                message=reason,
                role=claim.role,
                claim_id=claim.claim_id,
                worker_id=claim.worker_id,
                attempt=claim.attempt,
                failure_category=FailureCategory.LEASE_EXPIRY.value,
                failure_reason=reason,
            )
            self._retry_or_block(claim, FailureCategory.LEASE_EXPIRY, reason)
        return len(stale)

    # ---------- retry matrix ----------

    def retry_claim(
        self,
        previous: ExecutionClaim,
        *,
        reason: str,
        category: FailureCategory,
    ) -> ExecutionClaim:
        """Create a linked replacement claim with ``attempt+1``.

        Transactional rollback: the old claim is cleared first so the
        store-level uniqueness check accepts the new claim. If
        ``create_claim`` then raises, we restore the original claim so the
        card never ends up in an execution status without a live claim.
        Emits ``execution.retried`` on success.
        """
        from uuid import uuid4

        now = utc_now()
        lease_expires = now + timedelta(seconds=self.lease_policy.lease_seconds)
        new_claim = ExecutionClaim(
            card_id=previous.card_id,
            claim_id=f"clm-{uuid4().hex[:12]}",
            role=previous.role,
            status_at_claim=previous.status_at_claim,
            attempt=previous.attempt + 1,
            claimed_at=now,
            heartbeat_at=now,
            lease_expires_at=lease_expires,
            timeout_s=previous.timeout_s,
            worker_id=None,
            retry_count=previous.retry_count + 1,
            retry_of_claim_id=previous.claim_id,
            worktree_path=previous.worktree_path,
        )

        # Validate the worktree still exists. The previous claim may have
        # had a worktree_path, but the branch could have been deleted (via
        # prune or external git branch -D) between executions. If so,
        # either recreate it (WORKER) or raise so caller can BLOCK
        # (REVIEWER/VERIFIER).
        if (
            self.worktree_mgr is not None
            and previous.role != AgentRole.PLANNER
        ):
            card = self.store.get_card(previous.card_id)
            if card.worktree_branch is not None:
                wt_info = self.worktree_mgr.get(
                    card.id, base_commit=card.worktree_base_commit or "",
                )
                if wt_info is not None and wt_info.path is not None:
                    new_claim.worktree_path = str(wt_info.path)
                elif wt_info is not None and wt_info.path is None:
                    recheckout = self.worktree_mgr.recheckout(
                        card.id, card.worktree_branch,
                    )
                    if recheckout is not None:
                        new_claim.worktree_path = str(recheckout)
                    else:
                        raise WorktreeMissingError(
                            f"cannot recheckout {card.worktree_branch}; "
                            f"cannot retry {previous.role.value} role"
                        )
                elif wt_info is None and previous.role == AgentRole.WORKER:
                    from .worktree import WorktreeCreateError

                    try:
                        wt_info_new = self.worktree_mgr.create(card.id)
                    except WorktreeCreateError as exc:
                        raise WorktreeMissingError(
                            f"cannot recreate worktree for retry: {exc}"
                        ) from exc
                    self.store.update_card(
                        card.id,
                        worktree_branch=wt_info_new.branch,
                        worktree_base_commit=wt_info_new.base_commit,
                    )
                    new_claim.worktree_path = str(wt_info_new.path)
                elif wt_info is None:
                    new_claim.worktree_path = None
                    raise WorktreeMissingError(
                        f"worktree branch {card.worktree_branch} missing; "
                        f"cannot retry {previous.role.value} role"
                    )

        self.store.clear_claim(previous.card_id, claim_id=previous.claim_id)
        try:
            self.store.create_claim(new_claim)
        except Exception:
            # Rollback: restore the old claim so the card isn't stranded.
            try:
                self.store.create_claim(previous)
            except Exception:  # noqa: BLE001 — best-effort rollback
                pass
            raise

        self.store.append_runtime_event(
            new_claim.card_id,
            event_type=ExecutionEventType.RETRIED.value,
            message=f"retry attempt {new_claim.attempt} after {category.value}: {reason}",
            role=new_claim.role,
            claim_id=new_claim.claim_id,
            attempt=new_claim.attempt,
            failure_category=category.value,
            failure_reason=reason,
            retry_of_claim_id=previous.claim_id,
        )
        return new_claim

    def _handle_failed_envelope(
        self, env: ExecutionResultEnvelope, claim: ExecutionClaim
    ) -> None:
        category = env.failure_category or FailureCategory.INFRASTRUCTURE
        reason = env.failure_reason or "executor reported failure"
        # ``_retry_or_block`` owns the clear-old-claim step; it keeps the
        # claim alive until the replacement state is durable.
        self._retry_or_block(claim, category, reason)

    def _retry_or_block(
        self,
        failed_claim: ExecutionClaim,
        category: FailureCategory,
        reason: str,
    ) -> None:
        """Apply retry matrix: either link a new claim or BLOCK the card.

        Invariant: a card is never left in an execution status (DOING /
        REVIEW / VERIFY) without either a live claim or a completed
        terminal transition to BLOCKED. Both paths below enforce this:

        - Retry: :meth:`retry_claim` clears → creates with rollback on
          failure (old claim restored).
        - Block: move the card to BLOCKED *first*, then clear the claim.
          If ``update_card`` or ``move_card`` raises, the claim is still
          live and the next scheduler tick can redo the recovery.
        """
        budget = self.retry_policy.budget_for(category)
        retries_used = failed_claim.retry_count
        if retries_used < budget:
            try:
                self.retry_claim(failed_claim, reason=reason, category=category)
                return
            except WorktreeMissingError as exc:
                # Branch was deleted and cannot be recovered — fall through
                # to BLOCKED instead of running in the main checkout.
                reason = f"{reason} ({exc})"

        # Exhausted — terminal BLOCKED transition FIRST (card status moves
        # out of any execution state), THEN clear the claim. If the move
        # fails, the old claim is preserved so recovery can retry.
        block_reason = (
            f"{reason} [category={category.value} attempts={failed_claim.attempt}]"
        )
        self.store.update_card(failed_claim.card_id, blocked_reason=block_reason)
        self.store.move_card(
            failed_claim.card_id, CardStatus.BLOCKED, f"Blocked: {block_reason}"
        )
        if self.worktree_mgr is not None:
            card_obj = self.store.get_card(failed_claim.card_id)
            if card_obj.worktree_branch is not None:
                if self.worktree_mgr.detach(failed_claim.card_id):
                    self.store.append_runtime_event(
                        failed_claim.card_id,
                        event_type="worktree.detached",
                        message=f"Worktree detached: {card_obj.worktree_branch}",
                        worktree_branch=card_obj.worktree_branch,
                    )
                else:
                    self.store.append_runtime_event(
                        failed_claim.card_id,
                        event_type="worktree.detach_failed",
                        message=(
                            f"Worktree detach aborted (uncommitted changes preserved): "
                            f"{card_obj.worktree_branch}"
                        ),
                        worktree_branch=card_obj.worktree_branch,
                    )
        self.store.clear_claim(
            failed_claim.card_id, claim_id=failed_claim.claim_id
        )

    def _emit_orphan(
        self,
        env: ExecutionResultEnvelope,
        live_claim: ExecutionClaim | None,
        *,
        reason: str,
    ) -> None:
        live_id = live_claim.claim_id if live_claim is not None else None
        self.store.append_runtime_event(
            env.card_id,
            event_type=ExecutionEventType.RESULT_ORPHANED.value,
            message=(
                f"envelope for claim={env.claim_id} rejected "
                f"({reason}); live_claim={live_id or '<none>'}"
            ),
            role=env.role,
            claim_id=env.claim_id,
            worker_id=env.worker_id,
            attempt=env.attempt,
            failure_reason=reason,
        )

    def run_until_idle(self, max_steps: int = 20) -> list[Card]:
        processed: list[Card] = []
        for _ in range(max_steps):
            card = self.tick()
            if card is None:
                break
            processed.append(card)
        return processed

    def _wip_count(self) -> int:
        return sum(len(self.store.list_by_status(s)) for s in _WIP_STATUSES)

    def _deps_satisfied(self, card: Card) -> bool:
        for dep_id in card.depends_on:
            try:
                dep = self.store.get_card(dep_id)
            except KeyError:
                return False
            if dep.status != CardStatus.DONE:
                return False
        return True

    def _ready_cards(self, status: CardStatus):
        """Yield all dependency-satisfied cards in ``status`` (priority order)."""
        for card in self.store.list_by_status(status):
            if self._deps_satisfied(card):
                yield card

    def _first_ready(self, status: CardStatus) -> Card | None:
        return next(iter(self._ready_cards(status)), None)

    def _next_actionable_card(self) -> Card | None:
        return next(iter(self._iter_actionable_cards()), None)

    def _iter_actionable_cards(self):
        """Yield every actionable card in scheduler priority order.

        Order:
          1. Finish-what-you-started: VERIFY, then REVIEW.
          2. If WIP budget allows, READY (pulled into DOING on claim).
          3. Planning: INBOX (does not count against WIP).

        ``select_and_claim`` iterates this generator and skips candidates
        that already have a live claim, so one claimed front card cannot
        starve the scheduler's remaining capacity.
        """
        for status in (CardStatus.VERIFY, CardStatus.REVIEW):
            yield from self._ready_cards(status)

        if self._wip_count() < self.wip_policy.doing_limit:
            yield from self._ready_cards(CardStatus.READY)

        yield from self._ready_cards(CardStatus.INBOX)

    def _role_for(self, card: Card) -> AgentRole:
        mapping = {
            CardStatus.INBOX: AgentRole.PLANNER,
            CardStatus.READY: AgentRole.WORKER,
            CardStatus.REVIEW: AgentRole.REVIEWER,
            CardStatus.VERIFY: AgentRole.VERIFIER,
        }
        return mapping[card.status]

    def _apply_result(self, card_id: str, result: AgentResult) -> None:
        # Reviewer/verifier rework takes a dedicated path so the worktree
        # stays attached and ``rework_iteration`` / ``revision_requests``
        # stay internally consistent. A terminal rework exhaustion produces
        # a synthetic BLOCKED result and delegates back to the normal path.
        if result.revision_request is not None:
            self._apply_rework(card_id, result)
            return
        self._apply_normal_result(card_id, result)

    def _apply_normal_result(self, card_id: str, result: AgentResult) -> None:
        card = self.store.update_card(card_id, **result.updates)
        card.add_history(result.summary, role=result.role)
        self.store.append_execution_event(card_id, result)
        self.store.move_card(
            card_id,
            result.next_status,
            f"Status changed to {result.next_status.value}",
        )
        # Detach on any terminal transition so a reviewer/verifier rejection
        # (next_status=BLOCKED) doesn't leave workspace/worktrees/<card>
        # attached forever — prune_stale() skips cards whose directory
        # still exists, so those branches would otherwise accumulate.
        detach_worktree_on_terminal(
            self.store, self.worktree_mgr, card_id, result.next_status,
        )

    def _apply_rework(self, card_id: str, result: AgentResult) -> None:
        """Handle a reviewer/verifier revision request.

        Accepts up to ``retry_policy.rework`` reworks per card. Each accepted
        rework appends to ``card.revision_requests``, bumps
        ``card.rework_iteration``, and moves the card REVIEW/VERIFY → READY
        so the worker is re-dispatched on the next scheduler tick. The
        worktree stays attached — the worker picks up where it left off.

        Budget exhaustion synthesizes a BLOCKED ``AgentResult`` and delegates
        to :meth:`_apply_normal_result` so the standard detach + event path
        still runs.
        """
        assert result.revision_request is not None  # dispatcher guarantees this
        req = result.revision_request
        card = self.store.get_card(card_id)
        next_iter = card.rework_iteration + 1
        budget = int(self.retry_policy.rework)

        if next_iter > budget:
            # Budget exhausted — block the card. Still record this last
            # revision request for postmortem so the operator sees the
            # final ask that tipped it over.
            stamped = RevisionRequest(
                at=req.at,
                from_role=req.from_role,
                iteration=next_iter,
                summary=req.summary,
                hints=list(req.hints),
                failing_criteria=list(req.failing_criteria),
            )
            new_requests = list(card.revision_requests) + [stamped]
            self.store.update_card(
                card_id,
                revision_requests=new_requests,
            )
            reason = (
                f"rework budget exhausted ({budget} iterations). "
                f"Last ask from {req.from_role.value}: {req.summary}"
            )
            blocked = AgentResult(
                role=result.role,
                summary=(
                    f"{result.role.value} exhausted rework budget "
                    f"({budget} iterations)"
                ),
                next_status=CardStatus.BLOCKED,
                updates={"blocked_reason": reason, "owner_role": None},
                prompt_version=result.prompt_version,
                duration_ms=result.duration_ms,
                attempt=result.attempt,
                raw_response=result.raw_response,
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
            self._apply_normal_result(card_id, blocked)
            return

        # Accept rework: stamp iteration, append, bump counter, rewind to READY.
        stamped = RevisionRequest(
            at=req.at,
            from_role=req.from_role,
            iteration=next_iter,
            summary=req.summary,
            hints=list(req.hints),
            failing_criteria=list(req.failing_criteria),
        )
        new_requests = list(card.revision_requests) + [stamped]
        updates = dict(result.updates)
        updates["revision_requests"] = new_requests
        updates["rework_iteration"] = next_iter
        updates["owner_role"] = AgentRole.WORKER
        card = self.store.update_card(card_id, **updates)
        card.add_history(
            f"rework requested (iteration {next_iter}/{budget}): {req.summary}",
            role=result.role,
        )
        self.store.append_execution_event(card_id, result)
        self.store.append_runtime_event(
            card_id,
            event_type="rework.requested",
            message=(
                f"iteration {next_iter}/{budget} by {req.from_role.value}: "
                f"{req.summary}"
            ),
            role=req.from_role,
            rework_iteration=next_iter,
            worktree_branch=card.worktree_branch,
        )
        self.store.move_card(
            card_id,
            CardStatus.READY,
            (
                f"Rework iteration {next_iter}/{budget} requested by "
                f"{req.from_role.value}"
            ),
        )
        # The router cache key is built from card fields the router sees
        # (title/goal/acceptance/context_refs/...) and ignores rework
        # state, so the next worker dispatch would otherwise reuse the
        # pre-rework profile. Bust the entry so the new revision_requests
        # / rework_iteration trigger a fresh routing decision.
        self._invalidate_router_cache(card_id)

    def _invalidate_router_cache(self, card_id: str) -> None:
        """Best-effort: drop cached router decisions for ``card_id``.

        Decoupled via duck-typing on ``executor.policy.invalidate_card``
        so executors without a router policy (mock, agentao_multi,
        custom) need no extra surface.
        """
        policy = getattr(self.executor, "policy", None)
        if policy is None:
            return
        invalidate = getattr(policy, "invalidate_card", None)
        if callable(invalidate):
            invalidate(card_id)
