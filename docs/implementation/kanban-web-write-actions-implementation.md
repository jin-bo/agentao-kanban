# Kanban Web Write Actions Implementation Plan

Related documents:

- Safety design (the *why*): [../kanban-web-write-actions-safety-plan.md](../kanban-web-write-actions-safety-plan.md)
- Web Result plan: [../kanban-web-ui-result-improvement-plan.md](../kanban-web-ui-result-improvement-plan.md)
- CLI guide: [../kanban-cli-guide.md](../kanban-cli-guide.md)

This doc covers *how* and *in what order*. The safety plan owns the rationale
for the commit boundary and best-effort side-effect rules; this doc summarizes
them where needed and does not re-argue them.

## Goal

Add narrowly scoped Web operator write actions without changing what those
actions mean:

- keep `POST /api/cards` as the only new-card write exception
- add existing-card Web writes only behind `--enable-writes`
- make Web `move` / `requeue` / `block` / `unblock` match CLI/MCP behavior by
  calling the same code, not by re-implementing transitions
- block daemon and live-claim races at the transport layer
- stop `block` / `unblock` / `requeue` leaving half-applied fields when a card
  write fails
- keep worktree merge/prune/delete/checkout out of this implementation

## Scope

In scope:

- a single-write `move_card` extension so a status change plus a couple of
  auxiliary fields persist in one card front-matter write
- a small `kanban/operations.py` module of plain transition functions
- CLI and MCP refactored to call those functions
- Web API routes for card `move`, `requeue`, `block`, `unblock`
- a Web write guard (writes enabled / daemon lock / live claim) and a stable
  JSON error envelope
- Web UI controls gated on `writes_enabled`
- tests for write safety, CLI/MCP/Web parity, and side-effect-failure behavior

Out of scope:

- transaction manager or rollback framework
- Web `--force`, bulk mutation, daemon start/stop
- merge, prune, checkout, branch delete, or filesystem cleanup routes
- a new MCP `requeue` tool (requeue stays CLI + Web)
- changing `POST /api/cards` daemon-lock behavior
- changing the CLI JSON/result contract

## Design summary (from the safety plan)

- The shared layer is **plain functions**, not a command bus.
- Transport guards stay in the transport layer. CLI/MCP keep their existing
  `_require_card_writable` guard; Web adds `--enable-writes` + daemon-lock +
  live-claim checks before calling a transition function.
- **Preflight** (guards, card existence, transition validity, required inputs
  like a non-blank block reason) happens before any store write and leaves the
  board byte-for-byte unchanged.
- The **first successful card front-matter write is the commit point.** Doing
  the auxiliary-field changes inside one `move_card` write removes the
  mid-transition window that `update_card` → `move_card` has today.
- **Post-card side effects are best-effort.** `advance_inbox_dependents` (on a
  move into `DONE`) and worktree detach (on a terminal landing) run after the
  commit point. A failure there does not roll the card transition back; it is
  recorded as the same recovery-style event the CLI/daemon already emit, and
  surfaced to the caller as a non-fatal warning. No compensation logic.

## Deliverables

- `kanban/store.py` / `kanban/store_markdown/card_store.py`: `move_card` accepts
  optional field updates and persists them in the single existing card write.
- `kanban/operations.py`:
  - `TransitionResult(card: Card, warnings: list[str])`
  - `transition_move(store, worktree_mgr, card_id, status) -> TransitionResult`
  - `transition_requeue(store, card_id, target, note=None) -> TransitionResult`
  - `transition_block(store, worktree_mgr, card_id, reason) -> TransitionResult`
  - `transition_unblock(store, worktree_mgr, card_id, target) -> TransitionResult`
- CLI `cmd_move` / `cmd_block` / `cmd_unblock` / `cmd_requeue` calling the above.
- MCP `tool_card_move` / `tool_card_block` / `tool_card_unblock` calling the above.
- Web request models, guard helper, four routes, response serialization.
- Web UI action controls in the card detail surface.
- Tests: operations unit tests, CLI/MCP parity, Web guard behavior, Web route
  success, UI visibility.

## Phase 1: Single-write `move_card`

**Objective:** remove the `update_card` → `move_card` partial-write window for
`block` / `unblock` / `requeue` without adding generic transactions.

Files: `kanban/store.py`, `kanban/store_markdown/card_store.py`, store tests.

Tasks:

- Extend `move_card` to accept optional keyword field updates, applied before
  the one `_write_card` call, e.g.:
  `move_card(card_id, status, note, *, blocked_reason=..., owner_role=...)`.
  Keep the existing `blocked_at` rules (set on entering `BLOCKED`, clear on
  leaving) and the existing history-note + `append_event` behavior.
- Mirror the change in `InMemoryBoardStore` and the `BoardStore` protocol.
- The markdown store still writes the card through the existing atomic path.
- **Don't pollute the in-memory cache on a failed write.** Today `move_card`
  mutates the live `_cards[card_id]` object and then calls `_write_card`; if the
  write raises, the process keeps a dirty card. Either apply the field changes
  to a copy and only swap it into `_cards` after `_write_card` succeeds, or
  restore the previous field values on failure. (`append_event` should likewise
  not record the transition if the card write didn't land.) This is the cache
  side of the same partial-write guarantee — still no transaction manager.

Don't: add a generic `transition_card`, a `updates={...}` dict parameter, or a
separate write for the auxiliary fields.

Exit criteria:

- A simulated `_write_card` failure during this `move_card` call leaves status,
  `blocked_reason`, and `owner_role` on their old values **both on disk and in a
  subsequent `get_card` from the same store instance**, and emits no event.
- Plain `move_card(card_id, status, note)` behavior is unchanged.

## Phase 2: Shared card operations

**Objective:** move the transition semantics that CLI and MCP currently
duplicate into `kanban/operations.py`.

Files: new `kanban/operations.py`, operation tests. (Reuse
`orchestrator.advance_inbox_dependents` and `orchestrator.detach_worktree_on_terminal`;
don't reinvent them.)

Tasks:

- `TransitionResult` as above — `card` plus a `warnings` list. Nothing else.
- Each function:
  - receives a canonical card id (ID resolution stays in the caller),
  - validates its own inputs (status coercion, non-blank block reason, target
    status in the set the matching CLI command allows) and raises a small local
    exception (or `ValueError`) on bad input — no write happens,
  - performs the card transition via the Phase 1 single-write `move_card`
    (`block`: status `BLOCKED` + `blocked_reason`; `unblock`: target +
    `blocked_reason=None`; `requeue`: target + `blocked_reason=None` +
    `owner_role=None`; `move`: status only),
  - then runs post-card side effects: `advance_inbox_dependents` when the new
    status is `DONE` and the previous wasn't; worktree detach when landing in
    `DONE` or `BLOCKED` and a `worktree_mgr` was supplied,
  - catches a side-effect failure, emits the recovery-style event when it can,
    and appends a string to `warnings` (if emitting the event also fails, add
    that to `warnings` too and let the committed transition stand).
- `transition_requeue` takes no `worktree_mgr` — its targets are non-terminal.

Don't: invent a new state machine; copy the orchestrator's full result-apply
path; add per-function config objects.

Exit criteria:

- CLI, MCP, and Web can all call these.
- Tests cover: normal transitions; `DONE` → dependents advanced; terminal →
  worktree detached (and `worktree.artifacts_saved` emitted when artifacts
  exist); bad input rejected before any write; a forced side-effect failure
  returns a `TransitionResult` with the card committed and a warning.

## Phase 3: Refactor CLI and MCP onto the shared functions

**Objective:** make the existing non-Web write surfaces use the shared behavior
before adding Web routes, so parity is by construction.

Files: `kanban/cli/commands/board.py`, `kanban/cli/commands/runtime.py`,
`kanban/mcp/tools.py`, CLI/MCP tests.

Tasks:

- Replace the inline transition logic in `cmd_move`, `cmd_block`, `cmd_unblock`,
  `cmd_requeue`, `tool_card_move`, `tool_card_block`, `tool_card_unblock` with a
  call to the matching `transition_*` function. Keep the existing
  `_resolve_card_id` + `_require_card_writable` calls in front of it.
- Preserve current CLI stdout text and exit codes; map the operations' input
  exception to the existing "bad value" path.
- Preserve the MCP response shape; add an optional `warnings: list[str]` key
  (absent or empty when there's nothing to report — non-breaking for callers).
- CLI: print each warning as a line to stderr after the success line.
- No new MCP `requeue` tool.

Exit criteria:

- Existing CLI/MCP tests pass unchanged (modulo the new optional `warnings` key).
- A test forcing a post-card side-effect failure shows identical behavior via
  CLI, via MCP, and via a direct `transition_*` call.

## Phase 4: Web write guards and API routes

**Objective:** expose the four actions over HTTP with explicit safety gates.

Files: `kanban/web.py`, `tests/test_web.py`.

Routes and bodies:

```text
POST /api/cards/{card_id}/move      { "status": "done" }
POST /api/cards/{card_id}/requeue   { "target": "ready", "note": "retry after fix" }
POST /api/cards/{card_id}/block     { "reason": "waiting on dependency" }
POST /api/cards/{card_id}/unblock   { "target": "inbox" }
```

Tasks:

- Pydantic request models with the small explicit fields above (`note` optional).
- A Web guard helper for existing-card writes:
  - `--enable-writes` required, else `403` (the status the safety plan pins for
    existing-card mutating routes — do not reuse whatever status the
    `POST /api/cards` create path returns);
  - live `.daemon.lock` → `409`;
  - live execution claim on the target card → `409`;
  - no force override.
- `POST /api/cards` keeps its current disabled-write contract; it is not
  migrated to the `403` / error-envelope behavior here.
- Map the operations' input exception to a stable `400`; missing card to a
  stable `404`.
- Call the matching `transition_*` function after all guards pass.
- Response: `{ "card": <serialized card>, "warnings": [...] }`.
- Error envelope (no raw tracebacks):

  ```json
  { "error": "live_claim", "message": "Card abc123 has a live execution claim.", "retryable": true }
  ```

- Update the `kanban/web.py` module docstring to list the new mutation surface.

Exit criteria:

- Existing-card writes return the documented disabled-write status when
  `--enable-writes` is off; `409` under a live daemon lock or live claim.
- A request rejected at preflight leaves card files and `events.log` unchanged.
- A successful response carries the updated card; a forced post-card side-effect
  failure still returns `200` with a `warnings` entry.

## Phase 5: Web UI controls

**Objective:** operator controls in the detail view, not a workflow engine.

Files: `kanban/web_assets/*` (notably `detail_modal.js` / `detail_sections.js`
and `api.js`), Web UI tests if present.

Tasks:

- Render the controls only when `writes_enabled` is true.
- Put them in the card detail surface — not as drag/drop board mutation.
- Controls: move-to-status, requeue-target, block-with-reason, unblock-target.
- Disable them while the card has a live claim, *if* that flag is already in the
  detail payload — don't add a new endpoint just for this.
- On submit: call the route; on success refresh the detail (and board if status
  changed); show `warnings` non-disruptively; show the error envelope's
  `message` for 4xx/409.

Don't: bulk actions, worktree merge/prune buttons, optimistic board re-render.

Exit criteria:

- No write controls appear when `writes_enabled` is false.
- A successful action updates the card without a full page reload.
- Conflict and validation errors are visible to the operator.

## Phase 6: Docs and final verification

Files: `docs/kanban-cli-guide.md`; the safety plan if the shipped behavior
diverges from it; tests.

Tasks:

- In the CLI guide's Web section: list the four routes, explain `--enable-writes`,
  explain the daemon-lock / live-claim `409`s, and keep the `POST /api/cards`
  exception documented.
- Make sure tests exist for: operations, CLI parity, MCP parity, Web guards,
  Web route success, UI visibility when writes are disabled.
- Run focused suites first, then the full suite:

  ```bash
  uv run pytest tests/test_web.py
  uv run pytest tests/test_mcp*.py tests/test_cli*.py
  uv run pytest
  ```

Exit criteria:

- Focused tests pass; read-only Web routes unchanged; docs describe exactly the
  mutation surface shipped.

## Acceptance criteria

- `POST /api/cards` behavior stays compatible with the existing contract.
- Existing-card Web writes are unavailable unless `--enable-writes` is active,
  and return `409` under a live daemon lock or a live claim on the target card.
- `move` / `requeue` / `block` / `unblock` share transition code across CLI,
  MCP, and Web — no surface re-implements them.
- `block` / `unblock` / `requeue` never leave half-applied auxiliary fields when
  the card write fails — neither on disk nor in the store's in-memory cache.
- Post-card side-effect failures don't roll back committed transitions and are
  auditable via warnings and/or recovery events.
- No Web route exposes force, bulk mutation, daemon control, or worktree
  merge/prune/delete/checkout.

## Suggested PR split

1. Phase 1 + 2 — single-write `move_card` and `kanban/operations.py`, with unit tests.
2. Phase 3 — CLI/MCP refactored onto the shared functions, parity tests.
3. Phase 4 — Web guard, routes, error envelope.
4. Phase 5 — Web UI controls.
5. Phase 6 — docs and final parity tests.
