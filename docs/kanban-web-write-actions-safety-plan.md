# Kanban Web Write Actions Safety Plan

> Status: proposed. Split out from the Web Result improvement plan so read-only
> Result / Dependency UX work can proceed without carrying mutation risk.

## Context

The Web UI is intentionally conservative today:

- Most routes are read-only.
- `POST /api/cards` is the only card-mutating route because creating a new card
  does not race an active worker over an existing card.
- Worktree operations with larger repository impact remain CLI-only.

This document covers the follow-up work that was deliberately excluded from the
Result UI plan:

1. Operator write safety design.
2. Operator card write actions.
3. Worktree mutations from the Web.

Structured `next_steps` / CLI JSON changes remain out of scope in
`docs/kanban-web-ui-result-improvement-plan.md`.

## Design Order

Do not implement Web write routes before the safety model is explicit and tested.

Recommended order:

1. P0: shared safety rules and route policy.
2. P1: card-level operator write actions.
3. P2: worktree mutation safety design.
4. P3: narrowly scoped worktree mutation routes, if still needed.

## Resolved: Web writes are CLI/MCP equivalents, not narrower UI mutations

Earlier drafts described P1 routes both as "reuse the CLI/store transition
helpers" and as "mutate card state only / do not touch worktrees". Those are
contradictory: `cmd_move` / `cmd_block` / `cmd_unblock`
(`kanban/cli/commands/board.py`) and the MCP equivalents (`kanban/mcp/tools.py`)
already do more than a card-file write — on a terminal transition they detach the
card's worktree, and on a transition into `DONE` they call
`advance_inbox_dependents`. Splitting the Web behaviour off would leak attached
worktrees and silently fail to release dependent cards, so the Web UI would
diverge from every other operator surface.

The `cmd_*` functions themselves are not the right thing to reuse, though: they
are CLI handlers wired to argparse, stdout/stderr, and process exit, and MCP
already has its own parallel tool functions. So P1 starts by **extracting
shared, transport-agnostic transition functions** (e.g. `transition_move`,
`transition_block`, `transition_unblock`, `transition_requeue` in a
`kanban/operations`-style module)
that take a store + plain arguments, perform the card write plus the terminal
side effects, and return a result object. CLI, MCP, and Web all call those. This
is also the single place where the commit-boundary and best-effort side-effect
rules below are implemented, so all three surfaces get the same behaviour rather
than the Web layer growing its own wrapper.

Decision: **Web card writes are exact equivalents of the CLI/MCP transition
commands**, because all three call the same shared transition functions. The Web
layer adds only the transport-level guards in P0 (`--enable-writes`, daemon lock,
live-claim, no `--force`, stable error envelope); it does not redefine what a
move/block/unblock *means*. Where this plan previously said "card state only",
read it as "the same card-plus-side-effects unit of work the CLI performs, and
nothing beyond it (no merge/prune/checkout/branch-delete, no daemon control)".

## P0: Shared Safety Rules

Define the common policy for any Web route that mutates an existing card, board
state, or repository state.

`POST /api/cards` remains a deliberate exception to the existing-card conflict
policy: it creates a fresh card id, validates dependencies before writing, and
does not overwrite an existing card file. It still requires `--enable-writes`,
but it does not need the `.daemon.lock` or live-claim guard unless its behavior
changes to mutate existing cards.

Required rules:

- Writes require `--enable-writes`.
- Writes that can conflict with the daemon must return `409 Conflict` while a
  live `.daemon.lock` is held.
- Writes to an existing card must return `409 Conflict` while that card has a
  live execution claim. This mirrors the CLI/MCP `_require_card_writable`
  protection for split worker topologies where workers may not hold
  `.daemon.lock`.
- No HTTP route exposes a `--force` equivalent.
- Every successful mutation records an event with enough context for later audit.
- Every failed mutation returns a stable JSON error shape; do not leak raw tracebacks.
- Update the `kanban/web.py` module docstring so the mutation surface is explicit.

Suggested error shape:

```json
{
  "error": "daemon_lock_held",
  "message": "The daemon is running; card mutation is disabled from the Web UI.",
  "retryable": true
}
```

### Commit boundary and partial-write policy

Today's CLI transition commands are not transactional: `cmd_block`, for example,
is `update_card(blocked_reason=...)` → `move_card(BLOCKED)` →
`_detach_worktree_after_terminal_cli`, and `cmd_move` into `DONE` adds
`advance_inbox_dependents`. So if `update_card` succeeds and `move_card` raises,
the card is already partially changed. The shared transition functions from the
section above are where we tidy this up — modestly, not with a transaction
manager:

- **Preflight before the first write.** All transport guards (writes enabled,
  daemon lock, live claim), card existence, status-transition validity, and
  required inputs (e.g. block reason) are checked before any store mutation.
  These produce 4xx with the stable envelope and leave the board untouched.
- **Do the card mutation as one write where the store allows it.** `block`,
  `unblock`, and `requeue` all currently do a two-step `update_card` (set/clear
  `blocked_reason`, and for requeue `owner_role`) then `move_card`. The shared
  function should instead set those fields and the new status in a single
  `move_card` call (or a small store helper that writes the front-matter once),
  removing the mid-transition window for those paths.
- **The first successful card write is the commit point.** If, despite the
  above, a card write still fails part-way (I/O error, etc.), behaviour is
  defined by what already hit disk: anything before the first successful
  front-matter write is a 4xx/5xx with the board unchanged; once a front-matter
  write has landed the request is committed and returns success even if a later
  step fails.
- **Post-card side effects are best-effort with recovery events.** Worktree
  detach and dependent advancement run after the commit point. A failure there
  does not roll back the card transition and does not turn a committed move into
  a 5xx; it is recorded as a recovery-style event (the same events the
  CLI/daemon already emit) so an operator or a later daemon tick can reconcile.
  Because this lives in the shared transition function, CLI and MCP get the same
  best-effort behaviour — this is a small parity improvement, not a Web-only
  wrapper. The Web response may include a non-fatal `warnings` array noting the
  deferred cleanup.
- **No new rollback machinery.** We do not add compensation logic that undoes a
  committed card write; collapsing the two-step writes above is the only
  structural change.

Testing:

- Existing-card mutating routes return 403 when writes are disabled. Keep
  `POST /api/cards` compatible with the existing disabled-write contract unless
  that route is intentionally migrated to the shared error envelope.
- Existing-card mutating routes return 409 when a live `.daemon.lock` is held.
- Existing-card mutating routes return 409 when the target card has a live
  execution claim.
- A request rejected at preflight (bad transition, missing reason, missing card,
  guard tripped) leaves the card file and events.log byte-for-byte unchanged.
- For `block` / `unblock` / `requeue`: a simulated store failure during the card
  write leaves the card fully on the old status with the old `blocked_reason`
  (and `owner_role`) — no half-applied fields — confirming the single-write
  collapse.
- A successful write appends the expected card-transition event(s); a forced
  failure in a post-card side effect (worktree detach / dependent advancement)
  still returns success, leaves the card in the new status, and emits the
  recovery event — and the same holds when the failure is triggered through the
  CLI/MCP path, since the behaviour lives in the shared transition function.
- Error responses use the stable JSON envelope for disabled writes, daemon
  locks, live claims, invalid input, and missing cards.

## P1: Operator Card Write Actions

Add only card-level actions after P0 is implemented:

```text
POST /api/cards/{card_id}/move
POST /api/cards/{card_id}/requeue
POST /api/cards/{card_id}/block
POST /api/cards/{card_id}/unblock
```

Scope:

- These routes are thin transports over the shared transition functions
  introduced above (`transition_move` / `transition_requeue` / `transition_block`
  / `transition_unblock`), which CLI and MCP also call. They do not re-implement
  transitions and do not call the argparse-coupled `cmd_*` handlers directly.
- Because they call those shared functions, they inherit — and must not suppress
  — the same terminal side effects the CLI performs:
  - on a transition into `DONE` (via `move`, or `unblock --to done`),
    `advance_inbox_dependents` runs;
  - on any terminal transition (`DONE` / `BLOCKED`), the card's worktree is
    detached via the same `_detach_worktree_after_terminal_cli` path, which
    snapshots gitignored artifacts before removal.
  These run after the card transition commits and follow the best-effort /
  recovery-event policy from P0, not as a separate "worktree mutation" feature.
- Beyond those inherited side effects they touch nothing else: no branches, no
  files outside the board and its worktree-detach snapshot, no daemon process
  state, no merge/prune/checkout/branch-delete.
- The Web UI should show these actions only when `--enable-writes` is active.

Non-goals:

- No force moves.
- No bulk card mutation.
- No daemon start/stop controls.
- No merge/prune/checkout/branch-delete actions.
- No *suppressing* the CLI's terminal side effects either — the goal is parity
  with the CLI, not a stripped-down variant.

Testing:

- Valid state transitions match CLI behavior, including: moving to `DONE`
  advances inbox dependents; moving to a terminal status detaches the worktree
  and records `worktree.artifacts_saved` when artifacts exist; `unblock` to a
  non-terminal status leaves the worktree attached.
- Invalid transitions return stable 400/409 responses.
- Block requires a reason; unblock clears the reason.
- Requeue behavior matches the current CLI/store semantics.
- A Web move/block of a card with no worktree behaves identically to the CLI
  (no error, no spurious event).

## P2: Worktree Mutation Safety Design

Worktree mutations have a larger blast radius than card status writes. Design
them separately after P1 has proven the card-write safety model.

Operations under consideration:

- merge preserved result branch
- prune stale metadata
- delete branch
- checkout/open worktree
- filesystem cleanup

Safety questions to answer before implementation:

- Which operations are repo-local and reversible enough for Web?
- Which operations require a clean main checkout?
- How should conflicts be surfaced?
- What is the exact lock policy for active workers?
- Should any operation require command-copy confirmation instead of one-click UI?
- What event should be recorded for each repository mutation?

Default posture:

- Keep all worktree mutation actions CLI-only until this design is accepted.
- Preserve copyable commands in Result for high-impact actions such as `git merge`
  and `kanban worktree prune`.

## P3: Worktree Mutation Routes

Only add routes after P2 is accepted. Start with the lowest-risk operation and
ship one operation at a time.

### Route shape must match the operation's real scope

`merge` is genuinely card-scoped (it acts on one preserved result branch), so a
`POST /api/cards/{card_id}/worktree/merge` route is honest:

```text
POST /api/cards/{card_id}/worktree/merge
```

`prune` is **not** card-scoped today: `kanban worktree prune` walks the whole
board, and can clear metadata / events for multiple cards in one run. A
`POST /api/cards/{card_id}/worktree/prune` route would imply a per-card safety
model that does not exist. Two acceptable options, pick one in P2 before
shipping P3:

1. **Add a real single-card prune** to the store/CLI first (prune exactly one
   card's stale worktree metadata), then expose
   `POST /api/cards/{card_id}/worktree/prune` over that new operation.
2. **Expose a board-scoped route** — e.g. `POST /api/worktrees/prune` — that
   mirrors the CLI's actual blast radius. Then the P0 guards must run for the
   *entire pruned set*: a live `.daemon.lock` blocks it, and a live execution
   claim on *any* card that would be pruned makes the whole request `409`. The
   response enumerates which cards were affected.

Do not ship the card-scoped prune route over the current board-wide operation.

Implementation rules (apply to whichever routes ship):

- Recompute worktree state server-side before every mutation; for a board-scoped
  prune, recompute for every card in the candidate set.
- Refuse `none`, `not-git`, and `missing` states unless the operation is
  explicitly designed for that state.
- Return conflict details without swallowing the underlying Git result.
- Record an event on success — one per affected card for board-scoped prune.
- Do not chain multiple Git operations behind one button unless the entire
  sequence has a rollback/error story.

## Cross-Document Boundary

The Result UI plan owns read-only inspection and dependency navigation.

This document owns Web mutation safety and write routes.

The CLI JSON contract stays owned by the CLI/result contract discussion and is
not changed by either document.
