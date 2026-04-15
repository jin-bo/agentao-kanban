# Kanban

Python 3.12+ multi-agent kanban board. Synchronous by default, dispatcher daemon for continuous runs.

## Package Management

**Always use `uv`**, not pip:

```bash
uv sync                       # install
uv add package-name           # add dep
uv run python main.py         # run demo
uv run kanban <subcommand>    # run CLI
```

## Layout

- `main.py` — demo entry point (`run_demo`)
- `kanban/models.py` — Card, statuses, roles, `AgentResult`
- `kanban/store.py` — `BoardStore` Protocol + `InMemoryBoardStore`
- `kanban/store_markdown.py` — `MarkdownBoardStore` (TOML front-matter + events.log + raw transcripts)
- `kanban/orchestrator.py` — scheduler (`tick`, `run_until_idle`, WIP policy)
- `kanban/agents.py` — `ROLE_AGENTS` mapping + agent definition loader
- `kanban/executors/` — `CardExecutor` protocol, `MockAgentaoExecutor`, `AgentaoMultiAgentExecutor`
- `kanban/daemon.py` — dispatcher loop + `.daemon.lock` single-writer guard
- `kanban/cli.py` — `kanban` CLI entry point
- `kanban/demo.py` — runnable end-to-end demo
- `.agentao/agents/kanban-<role>.md` — role-specific sub-agent definitions

## Persistence

`MarkdownBoardStore(Path("workspace/board"))` writes:

- `workspace/board/cards/<card-id>.md` — one file per card. TOML front-matter between `+++` fences is source of truth; body is regenerated on each write.
- `workspace/board/events.log` — JSONL. Execution events include `role`, `prompt_version`, `duration_ms`, `attempt`, optional `raw_path`.
- `workspace/raw/<card-id>/<role>-<ts>.md` — optional full agent transcripts. Retention: last 5 per (card, role). `workspace/` is gitignored.
- `workspace/board/.daemon.lock` — single-writer guard while the daemon runs.

The CLI uses this store by default at `workspace/board` (override with `--board DIR`).

## CLI

```bash
uv run kanban card add --title T --goal G [--priority HIGH] [--acceptance "..."] [--depends <id>]
uv run kanban list
uv run kanban show <card_id>
uv run kanban move <card_id> <status>
uv run kanban block <card_id> "reason"
uv run kanban unblock <card_id> [--to <status>]
uv run kanban tick
uv run kanban run
uv run kanban daemon [--detach] [--once] [--poll-interval 2.0] [--verbose]
```

Add `--executor agentao` (before the subcommand) to drive real agentao sub-agents
instead of the mock. Pass `--force` to mutate the board even when the daemon
holds the lock (recovery only).

## Executors

- `MockAgentaoExecutor` (default): deterministic state machine for CI + offline dev.
- `AgentaoMultiAgentExecutor` (`kanban/executors/agentao_multi.py`): loads role-specific
  sub-agent definitions from `.agentao/agents/kanban-<role>.md`, constructs one
  `agentao.Agentao` instance per role per `run()`, and parses the trailing ```json
  fence (`{ok, summary, output[, acceptance_criteria][, blocked_reason]}`). On any
  exception the card is moved to `BLOCKED`. Each result carries `prompt_version`,
  `duration_ms`, and the full raw response for audit.
- `MultiBackendExecutor` (`--executor multi-backend`): profile-aware executor that
  routes each role through an `agent_profiles.yaml` profile and a pluggable
  backend (`subagent` / `acp`). Config is resolved as `<cwd>/.kanban/agent_profiles.yaml`
  with a shipped fallback at `docs/agent_profiles.sample.yaml`. Before the backend
  runs, a `RouterPolicy` may ask the `kanban-router` agent to pick a profile
  from the role's candidates; the router never overrides a card pin or planner
  recommendation, and any failure (disabled, spec missing, parse error,
  timeout, ...) falls through to the role default. Disable at runtime with
  `KANBAN_ROUTER=off`; per-role enablement lives in the top-level `router:`
  section of the config file.

## Daemon

`uv run kanban daemon` runs in the foreground by default (Ctrl-C = graceful stop).
`--detach` double-forks and redirects stdout/stderr to `<board>/daemon.log`.
`--once` does a single tick and exits. Only one daemon may hold a board at a time;
CLI write commands refuse to mutate while the lock is held (override with `--force`).
Stale locks (pid no longer alive) are cleared automatically on daemon start.
