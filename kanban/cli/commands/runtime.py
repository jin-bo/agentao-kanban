"""Runtime / orchestrator commands.

Covers ``tick``, ``run``, ``claims``, ``workers``, ``recover``, ``traces``,
and ``requeue`` — everything that drives the orchestrator or inspects
its v0.1.2 runtime state (claims/workers/traces).
"""

from __future__ import annotations

import argparse
import json as _json
import sys
from datetime import datetime, timezone

from ...executors import MockAgentaoExecutor
from ...models import AgentRole, CardStatus
from ...orchestrator import KanbanOrchestrator
from ..helpers import (
    _make_orchestrator,
    _make_store,
    _require_card_writable,
    _require_writable,
    _resolve_card_id,
)
from ..rendering import _format_age


def cmd_tick(args: argparse.Namespace) -> int:
    _require_writable(args)
    _, orchestrator = _make_orchestrator(args)
    card = orchestrator.tick()
    if card is None:
        print("Board is idle.")
    else:
        print(f"Processed {card.id[:8]}: now {card.status.value}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    _require_writable(args)
    _, orchestrator = _make_orchestrator(args)
    processed = orchestrator.run_until_idle(max_steps=args.max_steps)
    print(f"Processed {len(processed)} step(s); board idle.")
    return 0


def cmd_claims(args: argparse.Namespace) -> int:
    store = _make_store(args)
    if args.card_id is not None:
        args.card_id = _resolve_card_id(store, args.card_id)

    now = datetime.now(timezone.utc)
    claims = store.list_claims()
    if args.card_id is not None:
        claims = [c for c in claims if c.card_id == args.card_id]
    claims.sort(key=lambda c: (c.claimed_at, c.card_id))

    if args.as_json:
        payload = [
            {
                "card_id": c.card_id,
                "claim_id": c.claim_id,
                "role": c.role.value,
                "status_at_claim": c.status_at_claim.value,
                "worker_id": c.worker_id,
                "attempt": c.attempt,
                "retry_count": c.retry_count,
                "retry_of_claim_id": c.retry_of_claim_id,
                "claimed_at": c.claimed_at.isoformat(),
                "heartbeat_at": c.heartbeat_at.isoformat(),
                "lease_expires_at": c.lease_expires_at.isoformat(),
                "timeout_s": c.timeout_s,
                "heartbeat_age_s": (now - c.heartbeat_at).total_seconds(),
                "lease_remaining_s": (c.lease_expires_at - now).total_seconds(),
                "expired": c.is_expired(now=now),
            }
            for c in claims
        ]
        print(_json.dumps(payload, ensure_ascii=False))
        return 0

    if not claims:
        print("(no active claims)")
        return 0
    print(f"{'card':10}  {'role':8}  {'attempt':>7}  {'worker':14}  {'hb_age':>8}  {'lease_rem':>10}  claim_id")
    for c in claims:
        hb_age = _format_age((now - c.heartbeat_at).total_seconds())
        remaining = _format_age((c.lease_expires_at - now).total_seconds())
        expired_tag = " *EXPIRED*" if c.is_expired(now=now) else ""
        print(
            f"{c.card_id[:8]:10}  {c.role.value:8}  {c.attempt:>7}  "
            f"{(c.worker_id or '-')[:14]:14}  {hb_age:>8}  {remaining:>10}  "
            f"{c.claim_id}{expired_tag}"
        )
    return 0


def cmd_workers(args: argparse.Namespace) -> int:
    store = _make_store(args)

    now = datetime.now(timezone.utc)
    workers = store.list_workers()
    workers.sort(key=lambda w: w.started_at)

    if args.as_json:
        payload = [
            {
                "worker_id": w.worker_id,
                "pid": w.pid,
                "host": w.host,
                "started_at": w.started_at.isoformat(),
                "heartbeat_at": w.heartbeat_at.isoformat(),
                "heartbeat_age_s": (now - w.heartbeat_at).total_seconds(),
            }
            for w in workers
        ]
        print(_json.dumps(payload, ensure_ascii=False))
        return 0

    if not workers:
        print("(no live workers)")
        return 0
    print(f"{'worker_id':24}  {'pid':>7}  {'uptime':>8}  {'hb_age':>8}  host")
    for w in workers:
        uptime = _format_age((now - w.started_at).total_seconds())
        hb_age = _format_age((now - w.heartbeat_at).total_seconds())
        print(
            f"{w.worker_id[:24]:24}  {w.pid:>7}  {uptime:>8}  {hb_age:>8}  "
            f"{w.host or '-'}"
        )
    return 0


def cmd_recover(args: argparse.Namespace) -> int:
    if not args.stale:
        print(
            "recover requires --stale (only stale-claim recovery is implemented).",
            file=sys.stderr,
        )
        return 2
    _require_writable(args)
    store = _make_store(args)
    # Capture the list *before* recovery so we can report per-card outcomes.
    stale_before = store.list_stale_claims()
    orchestrator = KanbanOrchestrator(store=store, executor=MockAgentaoExecutor())
    count = orchestrator.recover_stale_claims()

    if args.as_json:
        payload = {
            "recovered": count,
            "cards": [
                {
                    "card_id": c.card_id,
                    "claim_id": c.claim_id,
                    "role": c.role.value,
                    "attempt": c.attempt,
                    "retry_count": c.retry_count,
                }
                for c in stale_before
            ],
        }
        print(_json.dumps(payload, ensure_ascii=False))
        return 0

    for c in stale_before:
        fresh = store.get_card(c.card_id)
        disposition = "retried" if fresh.status != CardStatus.BLOCKED else "blocked"
        print(
            f"{c.card_id[:8]}  [{c.role.value}]  attempt={c.attempt}  "
            f"retry_count={c.retry_count}  → {disposition}"
        )
    print(f"recovered {count} stale claim(s).")
    return 0


def cmd_traces(args: argparse.Namespace) -> int:
    store = _make_store(args)
    args.card_id = _resolve_card_id(store, args.card_id)
    try:
        store.get_card(args.card_id)
    except KeyError:
        print(f"No card with id {args.card_id}", file=sys.stderr)
        return 1
    role = AgentRole(args.role) if args.role else None
    traces = store.list_traces(args.card_id, role=role, latest=args.latest)
    if not traces:
        print(f"no traces retained for {args.card_id}")
        return 0
    for t in traces:
        stamp = t.at.strftime("%Y-%m-%dT%H:%M:%SZ")
        print(f"{stamp}  [{t.role.value}]  {t.size:>8}  {t.path}")
    return 0


def cmd_requeue(args: argparse.Namespace) -> int:
    store = _make_store(args)
    args.card_id = _resolve_card_id(store, args.card_id)
    _require_card_writable(args, args.card_id)
    try:
        card = store.get_card(args.card_id)
    except KeyError:
        print(f"No card with id {args.card_id}", file=sys.stderr)
        return 1

    previous_status = card.status
    target = CardStatus(args.target)

    # Clear blocked_reason and reset owner_role — both target statuses
    # (INBOX, READY) expect no pending owner.
    store.update_card(card.id, blocked_reason=None, owner_role=None)

    suffix = f": {args.note}" if args.note else ""
    history_note = (
        f"Requeued from {previous_status.value} to {target.value}{suffix}"
    )
    store.move_card(card.id, target, history_note)
    print(history_note)
    return 0


def register_runtime_commands(sub) -> None:
    """Register ``traces / requeue / claims / workers / recover / tick / run``."""
    traces = sub.add_parser("traces", help="List retained raw agent transcripts")
    traces.add_argument("card_id")
    traces.add_argument(
        "--role",
        choices=[r.value for r in AgentRole],
        help="Only transcripts from this role",
    )
    traces.add_argument("--latest", action="store_true", help="Only the most recent transcript")

    requeue = sub.add_parser("requeue", help="Return a (usually blocked) card back to flow")
    requeue.add_argument("card_id")
    requeue.add_argument(
        "--to",
        dest="target",
        choices=["inbox", "ready"],
        default="inbox",
        help="Target status (default: inbox)",
    )
    requeue.add_argument("--note", default="", help="Recovery note appended to history")

    claims = sub.add_parser("claims", help="List active execution claims (v0.1.2 runtime)")
    claims.add_argument("card_id", nargs="?", help="Filter to one card")
    claims.add_argument("--json", dest="as_json", action="store_true")

    workers = sub.add_parser(
        "workers", help="List live worker presences (v0.1.2 runtime)"
    )
    workers.add_argument("--json", dest="as_json", action="store_true")

    recover = sub.add_parser(
        "recover", help="Run one-shot runtime recovery (v0.1.2)"
    )
    recover.add_argument(
        "--stale",
        action="store_true",
        help="Recover stale claims (lease expired). Required for now.",
    )
    recover.add_argument("--json", dest="as_json", action="store_true")

    sub.add_parser("tick", help="Run a single orchestrator step")
    run = sub.add_parser("run", help="Run orchestrator until idle")
    run.add_argument("--max-steps", type=int, default=100)
