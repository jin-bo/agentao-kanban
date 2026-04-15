# Multi-Agent Implementation Plan

## TL;DR

Make the four roles real by defining each as an agentao sub-agent
(`kanban/defaults/kanban-<role>.md`), add a local daemon so scheduling
runs continuously, then add structured execution records. Defer ACP
until this local runtime is stable.

Invariants that hold across every phase:

- `KanbanOrchestrator` is the only component that writes card status
  transitions.
- Board files under `workspace/board/` are the source of truth; agents
  never mutate them directly.
- Failures normalize into `BLOCKED`; no silent retries in Phase 1.
- agentao's sub-agent definition format is reused verbatim; kanban
  does not maintain a parallel prompt or spec format.

## Goal

Evolve the current synchronous kanban prototype into a real multi-agent system while preserving the existing board model, card lifecycle, and CLI-driven development workflow.

This plan recommends:

- Phase 1: build on `agentao` sub-agents
- Phase 2: introduce ACP only when persistent runtime and distributed execution are actually needed

The key reason is that the current codebase already has a stable orchestration boundary:

- scheduler: `KanbanOrchestrator`
- execution boundary: `CardExecutor.run(role, card) -> AgentResult`
- persisted state: `MarkdownBoardStore`

That boundary is sufficient to support a real multi-agent runtime without forcing a full ACP migration immediately.

## Recommendation

Use `agentao` sub-agents first.

Do not start with ACP.

ACP should be treated as a later runtime upgrade for these needs:

- long-lived agents
- crash recovery
- queue-backed execution
- remote workers
- richer observability and operations

For this repository's current maturity, ACP is too large a step. It would couple "make agents real" with "build a distributed runtime". Those are separate problems and should be solved separately.

## Current State

Today the repository has:

- role definitions in `AgentRole`: `planner`, `worker`, `reviewer`, `verifier`
- state-driven scheduling in `KanbanOrchestrator`
- a deterministic mock executor for tests
- an `AgentaoExecutor` that uses one `agentao.Agentao().chat(...)` call per step

This means the repo already has a workflow engine, but not a true multi-agent runtime. The current `AgentaoExecutor` is still effectively "one generic agent wearing different hats via prompt".

## Target Phase 1 Architecture

Phase 1 should introduce four concrete agent lanes:

- `PlannerAgent`
- `WorkerAgent`
- `ReviewerAgent`
- `VerifierAgent`

Each lane should have:

- its own system prompt
- its own response contract
- its own optional workspace or memory scope
- its own execution policy such as iteration limits and retry rules

The orchestrator remains the top-level scheduler. It should not know agentao details. It should continue to decide:

- which card is next
- which role owns that card
- when a card transitions to another status

The executor layer becomes responsible for:

- resolving the correct sub-agent for a role
- building the role-specific request
- invoking the agent
- parsing the result
- normalizing failures into `BLOCKED`

## Proposed Module Design

### 1. Keep `KanbanOrchestrator` as the scheduler

Do not move board policy into agents.

Responsibilities that should remain in the orchestrator:

- priority ordering
- WIP limit
- dependency gating
- selection of next actionable card
- status transition ownership

This keeps the system debuggable and prevents agents from inventing workflow transitions.

### 2. Replace `AgentaoExecutor` with a role-routed multi-agent executor

Add a new executor implementation, for example:

- `kanban/executors/agentao_multi.py`

Suggested public shape:

```python
@dataclass
class AgentaoMultiAgentExecutor:
    registry: AgentRegistry
    max_iterations_by_role: dict[AgentRole, int]

    def run(self, role: AgentRole, card: Card) -> AgentResult:
        ...
```

This executor should:

- fetch a role-specific agent handle from the registry
- build a role-specific prompt from the card
- execute the role-specific agent
- parse the trailing JSON payload
- return `AgentResult`

### 3. Reuse agentao's sub-agent definition format

Do **not** invent a parallel `AgentSpec` / prompt module in kanban.
agentao already has a first-class sub-agent mechanism:

- definition format: Markdown with YAML frontmatter
- discovery: tracked templates under `kanban/defaults/*.md`,
  with optional project-local overrides in `.agentao/agents/*.md`
- fields: `name`, `description`, `tools`, `max_turns`, `model`,
  `temperature`, plus the Markdown body as system instructions
- runtime: `AgentManager` auto-discovers definitions and exposes each
  as a callable via `AgentToolWrapper` (sync or `run_in_background`)

Kanban should adopt this verbatim:

1. Track four role definitions in the repo under `kanban/defaults/`:
   - `kanban-planner.md`
   - `kanban-worker.md`
   - `kanban-reviewer.md`
   - `kanban-verifier.md`
2. Treat `.agentao/agents/` as an optional local override location for
   developers who want to mirror agentao's project-level discovery.
3. Keep `kanban/agents.py` minimal — only a `dict[AgentRole, str]`
   mapping role to agent name:

   ```python
   ROLE_AGENTS: dict[AgentRole, str] = {
       AgentRole.PLANNER:  "kanban-planner",
       AgentRole.WORKER:   "kanban-worker",
       AgentRole.REVIEWER: "kanban-reviewer",
       AgentRole.VERIFIER: "kanban-verifier",
   }
   ```
4. Record prompt versioning via a custom `version:` key in each
   definition's frontmatter. The executor reads it from the loaded
   agent definition and writes it into `events.log`.
5. Drop the separate `kanban/prompts.py` module from the file plan.
   Prompt text lives in the tracked agent definition files — single
   source of truth, editable without touching Python.

Benefits:

- operators tune prompts / tool scope / iteration limits without code
  changes
- no drift between a kanban-local spec format and agentao's format
- answers §3a directly: a sub-agent *is* one tracked definition file
  file loaded by `AgentManager`

### 3a. What a "sub-agent" is, concretely

A kanban sub-agent **is one `kanban/defaults/<name>.md` file**
loaded by
agentao's `AgentManager`. The frontmatter declares tool scope and
iteration limits; the body is the system prompt.

Phase 1A still needs a short design note answering:

- Instance boundary: one `AgentToolWrapper` invocation per role per
  `executor.run()` call. No shared sessions across roles.
- Session lifetime: stateless across ticks — each `run()` rebuilds
  context from the card. A worker resumed after reviewer rejection
  gets a fresh invocation with the reviewer's feedback in the prompt.
- Working directory: per-card scratch dir for worker; read-only repo
  view for planner/reviewer/verifier. Set via whatever CWD/workdir
  option agentao exposes at invocation time.
- Tool scope: declared in each definition's `tools:` frontmatter key,
  not in kanban code.
- Invocation mode: default to synchronous (`run_in_background=False`)
  in Phase 1A. Async mode is a Phase 1B concern tied to the daemon.

### 4. Role prompts live in agent definition files

Role prompts are the body of each
`kanban/defaults/kanban-<role>.md`. There is no
`kanban/prompts.py`.

Each definition file should contain:

- base instructions for the role
- output contract (the trailing ```json fence schema) specific to that
  role
- `version:` frontmatter key used for runtime correlation

Shared instructions (e.g., "board is source of truth; never mutate
card files directly") should be duplicated into each definition rather
than factored into a kanban-side template engine. Prompt reuse via
templating is a problem to solve after four files prove too much to
maintain, not before.

### 5. Persist execution metadata per role run

Extend the card or event model so each agent execution can be audited.

Suggested data to persist:

- `role`
- `attempt`
- `started_at`
- `finished_at`
- `summary`
- `blocked_reason`
- `raw_response_path` or truncated response preview

Do not store everything only in `history`. `history` is readable, but it is too lossy for runtime debugging.

A practical first step is:

- keep `history` as-is
- append structured execution events to `events.log`, including
  `prompt_version` from the `AgentSpec` used for the run
- optionally write raw agent transcripts to `workspace/raw/<card-id>/<role>-<timestamp>.md`

Transcript directory rules:

- `workspace/raw/` is ephemeral and must be gitignored
- retention policy: keep the last N runs per (card, role); default N=5
- never treat transcripts as source of truth — board files are authoritative

### 6. Add a dispatcher service beside the CLI

The repo currently has orchestration logic but not a persistent dispatcher.

Phase 1 should add a simple long-running process, for example:

- `uv run kanban daemon`

Responsibilities:

- loop over `orchestrator.tick()`
- sleep when idle
- reload board state from markdown store
- emit logs
- handle graceful shutdown

This is still local and simple. It gives the system an actual runtime without requiring ACP.

Run modes (foreground is default, to keep debugging cheap):

- `uv run kanban daemon` — foreground. Logs to stdout/stderr. Ctrl-C
  (SIGINT) triggers the same graceful-shutdown path as SIGTERM: finish
  the current `executor.run()`, flush stores, release the lock.
- `uv run kanban daemon --detach` — fork, write pid file, redirect
  logs to `workspace/board/daemon.log`. Same lock, same shutdown path.
- `uv run kanban daemon --once` — run a single `tick()` and exit.
  Intended for stepping through in a debugger.
- `uv run kanban daemon --poll-interval <sec>` — idle sleep duration.
  Default 2s; drop to 0.2s when iterating locally.
- `uv run kanban daemon --verbose` — DEBUG logging (prompt summaries,
  raw-transcript paths). Default is INFO.

Lock behavior, stale-lock recovery, and append safety are identical
across all modes — `--detach` is purely a process-lifecycle flag.

Phase 1B must answer these questions before merging, not after:

- Single-writer guard: `workspace/board/.daemon.lock` containing pid +
  start time. CLI write commands must refuse to run while the lock is
  held, or explicitly opt in with `--force`.
- External edits: daemon either polls card mtimes on each tick or uses
  fsevents/inotify. Default: poll on tick, no watcher daemon in 1B.
- Append safety: `events.log` is append-only, line-buffered, and all
  writers (CLI + daemon) must use `O_APPEND` writes of complete lines
  to keep interleaving safe.
- Graceful shutdown: on SIGTERM, finish the current `executor.run()`
  call, flush stores, release the lock. Hard-kill mid-run leaves the
  card in its pre-run status (orchestrator only writes transitions
  after `run()` returns).
- In-flight recovery: if the lock file exists but the pid is dead,
  daemon on startup logs a warning, removes the lock, and continues.
  No automatic card rollback.

## Execution Flow

The target execution flow should be:

1. User or API creates a card in `INBOX`
2. Dispatcher calls `tick()`
3. Orchestrator selects the card and maps `INBOX -> PLANNER`
4. Multi-agent executor resolves the `PlannerAgent`
5. Planner returns acceptance criteria and summary
6. Orchestrator writes updates and moves card to `READY`
7. Dispatcher later picks `READY`, moves it to `DOING`, and routes to `WorkerAgent`
8. Worker returns implementation output and ownership shifts to reviewer
9. Reviewer either:
   - returns approval and moves card to `VERIFY`
   - or returns `ok=false`, causing `BLOCKED`
10. Verifier either:
   - returns success and moves card to `DONE`
   - or returns `ok=false`, causing `BLOCKED`

This keeps workflow authority centralized and role behavior delegated.

## Why Agentao Sub-Agents First

### Advantages

- Minimal codebase disruption
- Reuses the existing `CardExecutor` boundary
- Keeps tests mostly intact
- Local iteration is fast
- Easier failure analysis than ACP
- Lower operational complexity

### Tradeoffs

- Agents are still hosted in the same local process boundary or local runtime family
- Persistence of agent sessions may be limited
- Scaling beyond one machine will be awkward
- Queueing and retries will be basic unless explicitly added

Those tradeoffs are acceptable at the current stage.

## Why Not ACP First

ACP becomes compelling only after the repository proves that:

- role boundaries are stable
- prompts are stable
- card lifecycle semantics are stable
- background dispatch behavior is stable
- failure and retry policy is understood

Without those, ACP will cause premature platform work.

If ACP is introduced too early, the team will have to solve all of these at once:

- transport and process boundaries
- state reconciliation
- async job control
- runtime health management
- remote execution semantics
- multi-agent workflow semantics

That is unnecessary for this repository right now.

## Suggested Phase Plan

### Phase 1A: True role-specific agents on current scheduler

Deliverables:

- short sub-agent design note (see section 3a) merged before code
- four `kanban/defaults/kanban-<role>.md` files with `version:`
  frontmatter
- `ROLE_AGENTS` mapping in `kanban/agents.py`
- `AgentaoMultiAgentExecutor` that resolves role → agent name →
  `AgentManager` invocation
- tests for planner/worker/reviewer/verifier routing
- end-to-end test: one card flows `INBOX → DONE` through four mocked
  role agents via the new executor
- delete the old `AgentaoExecutor` in the same PR; do not ship two
  executors in parallel

Acceptance:

- each role is configured independently
- executor no longer relies on one shared instruction table embedded inline
- failures still normalize into `BLOCKED`
- `events.log` entries include role and `prompt_version`

### Phase 1B: Local dispatcher daemon

Deliverables:

- `kanban daemon`
- idle polling loop
- basic structured logs
- lock file or single-process guard for board directory

Acceptance:

- cards created in markdown storage are processed without manually running `tick`
- daemon can be stopped and restarted without corrupting the board

### Phase 1C: Structured execution records

Deliverables:

- per-role execution events
- raw transcript snapshots for debugging
- attempt counter and timing data

Acceptance:

- every `BLOCKED` card has enough evidence to debug why it blocked
- reviewers and verifiers leave machine-readable artifacts

### Phase 2: ACP integration

Deliverables:

- ACP-backed executor or ACP-backed agent runtime adapter
- remote worker option
- durable queue or job transport
- observability hooks

Acceptance:

- dispatcher can hand off work to external ACP agents
- retries and recovery work across process restarts
- board state remains source of truth

## File-Level Change Plan

Suggested new files:

- `docs/multi-agent-implementation-plan.md`
- `kanban/defaults/kanban-planner.md`
- `kanban/defaults/kanban-worker.md`
- `kanban/defaults/kanban-reviewer.md`
- `kanban/defaults/kanban-verifier.md`
- `kanban/agents.py` (just the `ROLE_AGENTS` mapping)
- `kanban/executors/agentao_multi.py`
- `kanban/daemon.py`
- `tests/test_role_agents.py`
- `tests/test_agentao_multi_executor.py`
- `tests/test_daemon.py`

Removed from the earlier draft:

- `kanban/prompts.py` — prompts live in `kanban/defaults/*.md`
  bodies
- `tests/test_agent_registry.py` — no registry class to test

Suggested updates:

- `kanban/cli.py`
  Add `daemon` subcommand and executor selection for the new runtime.
- `kanban/executors/__init__.py`
  Export the new executor.
- `kanban/models.py`
  Optionally add structured execution metadata types if needed.
- `kanban/store_markdown.py`
  Persist richer execution events and optional raw artifacts.
- `README.md`
  Document the runtime modes and recommended local workflow.

## Failure Policy

The system needs explicit failure rules before adding concurrency.

Recommended rules:

- agent exception: move card to `BLOCKED`
- malformed response: move card to `BLOCKED`
- role timeout: move card to `BLOCKED`
- reviewer rejection: move card to `BLOCKED`
- verifier rejection: move card to `BLOCKED`
- manual unblock target:
  - default to `INBOX` if re-planning is desired
  - allow `READY`, `REVIEW`, or `VERIFY` for resume-from-stage workflows

Avoid automatic silent retries in Phase 1. They make debugging harder. Start with explicit failure and manual operator recovery.

## Concurrency Guidance

Do not start with unrestricted parallelism.

Recommended Phase 1 model:

- one dispatcher process per board
- one active role execution per card
- multiple cards may run concurrently only after board locking and artifact isolation are designed

This repository writes to markdown files under one board directory. That makes concurrent mutation a real risk. Concurrency should be added only after file-level isolation and lock strategy are explicit.

Sketch the multi-card lock model during Phase 1B (not later), even if
the daemon still runs cards serially:

- per-card lock file `workspace/board/cards/<card-id>.lock` held for
  the duration of `executor.run()`
- daemon selects next card from `tick()` only if no per-card lock is
  held
- orchestrator writes card transitions only after the lock is released

Documenting this in 1B prevents 1C from being rewritten when
concurrency is turned on.

## Board as Source of Truth

Keep the board storage authoritative.

Agents may produce suggestions, outputs, reviews, and verification reports. Agents should not directly mutate the board files. Only orchestrator and store code should write state transitions.

That rule is important even after ACP is introduced.

## Testing Strategy

Minimum test coverage for Phase 1:

Unit:

- spec table returns the correct spec for each role
- executor routes to the correct role-specific agent
- planner updates acceptance criteria
- worker output is written under the expected key
- reviewer rejection blocks the card
- verifier rejection blocks the card
- daemon does not process `BLOCKED` cards

Integration:

- end-to-end: one card `INBOX → DONE` through all four role agents
  (mocked) via the real orchestrator + executor + markdown store
- daemon processes cards until idle against a real temp-dir board
- daemon + CLI lock contention: CLI refuses to write while daemon
  holds the lock
- daemon restart preserves board state after SIGTERM mid-tick
- stale lock recovery: daemon starts cleanly when prior lock's pid is
  dead

Use mock agent factories heavily. Do not make tests depend on a real remote runtime.

## Decision Summary

Recommended path:

1. Keep `KanbanOrchestrator` as the workflow authority
2. Introduce role-specific `agentao` sub-agents through a registry-backed executor
3. Add a local daemon so scheduling is actually continuous
4. Add structured execution artifacts
5. Reassess ACP only after the local runtime is stable

This sequence solves the real missing piece first: turning the current role model into an actual runnable multi-agent system. ACP should be an optimization and scaling step, not the starting point.
