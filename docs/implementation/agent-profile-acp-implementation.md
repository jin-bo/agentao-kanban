# Agent Profile + ACP Implementation Plan

Related documents:

- Full design: [../agent-profile-acp-design.md](../agent-profile-acp-design.md)
- ADR: [../agent-profile-acp-adr.md](../agent-profile-acp-adr.md)
- Checklist: [../agent-profile-acp-checklist.md](../agent-profile-acp-checklist.md)

## Goal

Implement agent-profile routing for kanban with minimal disruption to the
existing workflow engine:

- keep `AgentRole` fixed
- add `agent_profile` as a routing layer under role
- support `subagent` and `acp` backends
- reuse `agentao.acp_client` instead of building a parallel ACP runtime

## Scope

In scope:

- card/storage schema extension for `agent_profile`
- profile config loading and validation
- executor refactor to support multiple backends
- ACP backend integration via `ACPManager`
- structured ACP error mapping
- execution event enrichment
- minimal CLI/profile inspection support
- tests for routing, backend behavior, and workflow integration

Out of scope:

- changing `AgentRole`
- long-lived ACP session reuse
- dynamic routing beyond explicit/default selection
- copying ACP server lifecycle commands into kanban CLI

## Deliverables

- `kanban/agent_profiles.py`
- `kanban/executors/profile_resolver.py`
- `kanban/executors/backends/base.py`
- `kanban/executors/backends/subagent_backend.py`
- `kanban/executors/backends/acp_backend.py`
- `kanban/executors/multi_backend.py`
- `kanban/agent_profiles.yaml` or equivalent packaged config
- card/store support for `agent_profile` fields
- enriched execution events
- CLI commands for setting/clearing agent profile
- tests covering config, routing, ACP mapping, and workflow behavior

## Constraints

- orchestrator remains role-based
- workflow transitions remain owned by orchestrator
- ACP backend runs non-interactively
- fallback is allowed only for infrastructure failures
- ACP integration must use `agentao.acp_client` stable embedding surface

## Implementation Phases

### Phase 1: Model And Persistence

Objective:
Add `agent_profile` to the card model without breaking existing boards.

Tasks:

- Add `agent_profile: str | None` to `Card`
- Add `agent_profile_source: str | None` to `Card`
- Update markdown/TOML serialization
- Update markdown/TOML deserialization
- Preserve backward compatibility for cards without these fields
- Add store round-trip tests for old/new cards

Exit criteria:

- existing boards load unchanged
- new fields persist correctly
- old cards default to `None`

### Phase 2: Profile Config Loader

Objective:
Create a typed config layer for role/profile/backend routing.

Tasks:

- Define schema for `agent_profiles.yaml`
- Support:
  - role default profile
  - profile role binding
  - backend type
  - backend target
  - fallback
- Implement config validation:
  - profile exists
  - role matches
  - fallback same role
  - fallback has no cycles
- Add config tests

Exit criteria:

- invalid config fails deterministically
- role/profile lookup is typed and test-covered

### Phase 3: Executor Refactor

Objective:
Split the current role-driven executor into resolver + backend adapters + parser.

Tasks:

- Introduce `profile_resolver.py`
- Introduce backend interface in `backends/base.py`
- Move current subagent execution path into `subagent_backend.py`
- Introduce `multi_backend.py` as the main executor
- Keep raw-response parsing centralized in top-level executor

Resolution order:

1. `card.agent_profile`
2. planner recommendation
3. policy match
4. role default profile

Exit criteria:

- executor can run the same workflow role via different backends
- raw response parsing remains shared

### Phase 4: ACP Backend

Objective:
Integrate ACP as a backend by reusing `agentao.acp_client`.

Tasks:

- Implement `acp_backend.py`
- Use `ACPManager.from_project(project_root)`
- Default to `prompt_once(..., interactive=False, cwd=...)`
- Validate `backend.target` exists in `.agentao/acp.json`
- Return normalized backend result with:
  - raw text
  - backend type
  - backend target
  - optional diagnostics metadata
- Do not manually reimplement ACP session lifecycle

Exit criteria:

- one ACP-backed role can execute end-to-end
- backend respects per-call cwd
- no CLI output parsing is required

### Phase 5: Failure Mapping

Objective:
Map ACP structured errors into kanban failure semantics.

Tasks:

- Define mapping from `AcpErrorCode` to kanban failure categories
- Treat these as routing/config failures:
  - `CONFIG_INVALID`
  - `SERVER_NOT_FOUND`
- Treat these as infrastructure failures:
  - `PROCESS_START_FAIL`
  - `HANDSHAKE_FAIL`
  - `REQUEST_TIMEOUT`
  - `TRANSPORT_DISCONNECT`
  - `PROTOCOL_ERROR`
  - `SERVER_BUSY`
- Treat `INTERACTION_REQUIRED` as its own category
- Ensure `INTERACTION_REQUIRED` does not use infrastructure retry/fallback by default
- Add tests for each mapping path

Exit criteria:

- ACP failures are classified without string matching
- fallback only fires for infrastructure failures

### Phase 6: Events And Diagnostics

Objective:
Make profile/backend choice observable in audit logs.

Tasks:

- Extend execution event payload with:
  - `agent_profile`
  - `backend_type`
  - `backend_target`
  - `routing_reason`
  - `fallback_from_profile`
- Add runtime/backend-failure events as needed
- Include `session_id` when available
- Use `ACPManager.get_status()` / `get_server_logs()` for diagnostics

Exit criteria:

- events distinguish role/profile/backend
- ACP failures can be diagnosed from runtime data

### Phase 7: CLI And Operator Surface

Objective:
Expose the minimum operator controls needed for profile-aware execution.

Tasks:

- Add `card edit --agent-profile <name>`
- Add `card edit --clear-agent-profile`
- Add `profiles list`
- Add `profiles show <name>`
- Optionally add `profiles doctor`

Exit criteria:

- operator can inspect and assign profiles without editing files by hand

### Phase 8: Integration Tests And Rollout

Objective:
Prove the new model works before broad rollout.

Tasks:

- Add profile-config tests
- Add routing precedence tests
- Add ACP backend tests mocking `ACPManager.prompt_once(...)`
- Add executor integration tests
- Add workflow regression tests
- Roll out only:
  - `worker -> claude-code-worker`
  - `reviewer -> codex-reviewer`
- Keep planner/verifier on default subagent initially

Exit criteria:

- no orchestrator changes required beyond executor wiring
- ACP-backed worker/reviewer runs are stable in limited rollout

## File Plan

New files:

- `kanban/agent_profiles.py`
- `kanban/executors/profile_resolver.py`
- `kanban/executors/backends/base.py`
- `kanban/executors/backends/subagent_backend.py`
- `kanban/executors/backends/acp_backend.py`
- `kanban/executors/multi_backend.py`
- `kanban/agent_profiles.yaml`
- `docs/implementation/agent-profile-acp-implementation.md`

Likely modified files:

- `kanban/models.py`
- `kanban/store_markdown.py`
- `kanban/cli.py`
- `kanban/executors/__init__.py`
- current role-specific executor module(s)
- test files under `tests/`

## Risk Notes

- Mixing workflow role and implementation identity would destabilize scheduling; do not modify `AgentRole`
- ACP interaction-required failures must not be treated as transient infrastructure failures
- ACP backend should prefer `prompt_once(...)` to avoid accidental long-lived session coupling
- event schema drift is a real risk; add metadata fields early

## Suggested Order Of Work

1. Card/store schema
2. Profile config loader
3. Executor refactor
4. ACP backend
5. Failure mapping
6. Event enrichment
7. CLI
8. Integration tests
9. Limited rollout

## Done Criteria

- `AgentRole` unchanged
- `agent_profile` persists and round-trips
- profile config validated at load time
- executor can route by profile
- ACP backend uses `agentao.acp_client`
- ACP errors map via `AcpErrorCode`
- execution events include role/profile/backend
- limited rollout succeeds for ACP-backed worker and reviewer
