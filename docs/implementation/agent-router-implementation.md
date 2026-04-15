# Agentao Router Agent Implementation Plan

Related documents:

- Full design: [../agent-router-design.md](../agent-router-design.md)
- ADR: [../agent-router-adr.md](../agent-router-adr.md)
- Checklist: [../agent-router-checklist.md](../agent-router-checklist.md)
- Profile/backend design: [../agent-profile-acp-design.md](../agent-profile-acp-design.md)

## Goal

Implement a router-agent-driven profile selection layer for kanban with minimal
disruption to the existing execution model:

- keep `AgentRole` fixed
- keep `resolve_profile()` priority semantics
- add an `agentao` router agent behind the `policy` hook
- allow all four workflow roles to use the same routing mechanism
- safely fall back to the role default profile when routing is uncertain or fails

## Scope

In scope:

- router agent definition
- router input/output contract
- router client wrapper and validation
- policy integration for `MultiBackendExecutor`
- routing observability
- gradual rollout controls by role
- tests for router behavior and fallback semantics

Out of scope:

- replacing profile/backend fallback
- changing `AgentRole`
- letting router execute work
- full rule-engine fallback implementation
- long-lived router sessions or learning loops

## Deliverables

- router agent spec, resolved by lookup order:
  - primary: `.agentao/agents/kanban-router.md`
  - fallback: `kanban/defaults/kanban-router.md`
- `kanban/executors/router_agent.py`
- `kanban/executors/router_policy.py`
- `kanban/agent_profiles.yaml` — add top-level `router:` section (see Phase 8)
- CLI wiring in `kanban/cli.py`
- tests for router unit, executor integration, and workflow routing

Already-existing docs (update only if the contract actually changes, do **not** recreate):

- `docs/agent-router-adr.md`
- `docs/agent-router-design.md`
- `docs/agent-router-checklist.md`

## Constraints

- router only selects profiles, never performs task execution
- router can only choose from current-role candidates
- router failures must not block cards
- router integration should be limited to `multi-backend`
- default routing remains the safety net

## High-Level Execution Path

Desired path:

```text
role execution requested
  -> resolve card pin?
  -> resolve planner recommendation?
  -> invoke router policy?
  -> if router picks valid profile, use it
  -> else use role default
  -> execute selected profile
  -> if selected profile infra-fails, use profile fallback chain
```

## Phase 1: Contract And Prompt Surface

Objective:
Lock the router contract before any executor integration.

Files:

- router agent spec (write at `.agentao/agents/kanban-router.md`; `kanban/defaults/kanban-router.md` is only a documentation fallback for environments that don't ship `.agentao/`)
- existing `docs/agent-router-design.md` / `docs/agent-router-adr.md` — audit only, update iff the contract in this phase diverges from them

Tasks:

- define the router prompt role and boundaries
- define the exact JSON output contract:
  - `profile`
  - `reason`
  - `confidence`
- define valid `null` selection behavior
- define output invalidation rules:
  - non-JSON output
  - unknown profile
  - cross-role profile
  - empty payload

Implementation notes:

- the router prompt should explicitly say it is not the worker/reviewer/etc.
- the router prompt should be fed only card summary plus candidate profiles
- keep examples short and role-specific

Exit criteria:

- router contract is documented and stable
- prompt examples cover `worker`, `reviewer`, `verifier`, `planner`

## Phase 2: Profile Metadata Readiness

Objective:
Make profile descriptions usable by the router.

Files:

- `kanban/agent_profiles.yaml`
- `tests/test_agent_profiles.py`

Tasks:

- audit every existing profile for missing `description`
- audit every existing profile for missing or weak `capabilities`
- normalize descriptions so they explain what the profile is good at
- decide which profiles should be eligible for router selection

Implementation notes:

- avoid relying on profile names alone
- descriptions should be written for machine-assisted comparison
- keep wording concrete: code, shell, diff review, acceptance verification, planning

Exit criteria:

- every router-eligible profile has meaningful metadata
- config tests still pass after metadata additions

## Phase 3: Router Input Builder

Objective:
Create a stable, minimal routing payload from a card and config.

Files:

- `kanban/executors/router_policy.py`
- possibly `kanban/executors/profile_resolver.py` for type reuse only
- `tests/test_router_policy.py`

Tasks:

- add a card summary builder
- add a candidate profile builder filtered by current role
- include default profile in the candidate list
- exclude cross-role profiles before router invocation
- short-circuit when the filtered candidate set has `<= 1` entry (only the role default): skip router entirely, emit `routing_source = "default"` and `routing_reason = "role default for <role> (single candidate, router skipped)"`

Suggested internal types (all shared types live in `router_agent.py` — or a new `router_types.py` — so `router_policy.py` only consumes them; this avoids circular imports):

- `RouterCardSummary`
- `RouterCandidateProfile`
- `RouterRequest`

Payload fields:

- card summary:
  - `card_id`
  - `title`
  - `goal`
  - `role`
  - `priority`
  - `acceptance_criteria`
  - `context_refs`
- candidate summary:
  - `name`
  - `role`
  - `backend_type`
  - `backend_target`
  - `fallback`
  - `capabilities`
  - `description`

Implementation notes:

- do not inline context file contents
- keep ordering stable so tests remain deterministic
- use a compact JSON payload or structured prompt block

Exit criteria:

- router input is deterministic
- candidate whitelist is role-safe

## Phase 4: Router Client Wrapper

Objective:
Encapsulate router invocation and output validation.

Files:

- `kanban/executors/router_agent.py`
- `tests/test_router_policy.py`

Tasks:

- load the `kanban-router` spec with this lookup order:
  1. `.agentao/agents/kanban-router.md`
  2. `kanban/defaults/kanban-router.md`
  - first match wins; if neither exists, router is **not enabled** — policy returns `None`, the card is **not** blocked, and no error is raised
- invoke it through the existing subagent path or shared agent interface
- parse raw output
- validate against schema
- normalize failures into a small internal error taxonomy
- enforce an explicit timeout `ROUTER_TIMEOUT_S` (default 10s, overridable via the `router:` section in `agent_profiles.yaml` — see Phase 8); timeouts map to the `timeout` failure kind

Suggested internals (colocated with the other router types — see Phase 3 note on type placement):

- `RouterDecision`
- `RouterFailureKind`
- `RouterClient`

Failure kinds:

- `parse_error`
- `invalid_choice`
- `timeout`
- `backend_error`
- `empty_choice`

Implementation notes:

- `profile = null` is not an error; it is a valid no-match outcome
- invalid profile names should be converted into a router failure, not propagated as config errors
- keep the failure surface explicit so `routing_reason` can explain fallback
- v1 **must ignore** `confidence` as a threshold signal — parse it, pass it through to diagnostics only; any future threshold gate requires a separate decision plus regression tests

Exit criteria:

- router invocation is isolated from executor core
- every router output path is testable in isolation

## Phase 5: Policy Integration

Objective:
Plug the router into the existing profile resolution flow.

Files:

- `kanban/executors/router_policy.py`
- `kanban/executors/multi_backend.py`
- `tests/test_profile_resolver.py`
- `tests/test_multi_backend_executor.py`

Tasks:

- implement `policy(role, card, config) -> str | None`
- return router-selected profile name when valid
- return `None` for no-match or router failure
- preserve resolver priority:
  - card pin
  - planner recommendation
  - router policy
  - default

Implementation notes:

- do not embed router logic directly in `resolve_profile()`
- keep router policy as an injected dependency
- preserve current behavior when no policy is configured
- memoize decisions inside the policy instance to avoid re-invoking the router on retry/replan ticks:
  - key = `(card_id, role, sha1(card.goal + "\n" + acceptance_criteria))`
  - on cache hit, reuse the prior `RouterDecision` and append ` (cached)` to `routing_reason`
  - mutations to `card.goal` or `acceptance_criteria` naturally change the key — no explicit invalidation
  - in-process only for v1; not persisted across CLI runs

Exit criteria:

- router policy is transparent to the resolver
- card pin and planner recommendation still override router

## Phase 6: Executor And CLI Wiring

Objective:
Turn router policy on for `multi-backend` without affecting other executors.

Files:

- `kanban/cli.py`
- `kanban/executors/multi_backend.py`
- `tests/test_cli_profiles.py`

Tasks:

- inject router policy in `_build_executor("multi-backend")`
- keep `mock` executor unchanged
- keep legacy `agentao` executor unchanged
- ensure `multi-backend` still works if router is disabled or unavailable

Implementation notes:

- do not make router a hard dependency for basic CLI commands
- failure to instantiate router policy should degrade to no-policy mode only if explicitly intended
- prefer a configuration flag to enable roles gradually

Exit criteria:

- only `multi-backend` uses router
- non-router executor paths remain stable

## Phase 7: Routing Observability

Objective:
Make router-driven selection visible in execution events.

Files:

- `kanban/executors/multi_backend.py`
- `kanban/models.py`
- `kanban/store_markdown.py`
- `tests/test_multi_backend_integration.py`

Tasks:

- `routing_source` and `routing_reason` **already exist** on execution events — this phase only extends their values and fill rules, it does **not** add new schema fields (except `router_prompt_version` below)
- emit `routing_source = "policy"` when router successfully chooses a profile
- emit `routing_source = "default"` when router no-matches, fails, or is short-circuited
- include router-derived `reason` in `routing_reason`
- encode failure fallback reason in a short operator-readable form
- add one new field `router_prompt_version` on the execution event, populated **only** when the router was actually invoked (not set when router is disabled or short-circuited); resolves design Open Question #2

Suggested reason patterns:

- `router selected gemini-worker: coding task with shell-oriented implementation`
- `role default for worker (router found no strong match)`
- `role default for reviewer (router failed: parse_error)`

Implementation notes:

- v1 does not need a separate router event type
- use existing execution-event enrichment first
- keep reasons short enough for logs and CLI output

Exit criteria:

- operators can tell whether routing came from router or default
- fallback cause is visible without opening raw traces

## Phase 8: Rollout Controls

Objective:
Allow incremental enablement by role.

Files:

- `kanban/cli.py`
- optional config file or env-based control surface
- `tests/test_cli_profiles.py`

Tasks:

- introduce a router enable/disable control
- introduce per-role enablement
- define the initial rollout sequence:
  - `worker`
  - `reviewer`
  - `verifier`
  - `planner`

Control surfaces (v1 — fixed, not optional):

1. **Global kill switch**: environment variable `KANBAN_ROUTER=off` → `router_policy.policy()` short-circuits to `None` before any candidate building. Intended as the fast rollback path.
2. **Per-role allowlist**: top-level `router:` section in `kanban/agent_profiles.yaml`:
   ```yaml
   router:
     enabled_roles: [worker]
     timeout_s: 10
   ```
   Only roles listed in `enabled_roles` invoke the router; others bypass directly to default. `timeout_s` overrides `ROUTER_TIMEOUT_S`.
3. **Code default**: when the `router:` section is absent, default to `enabled_roles: [worker]` so a fresh install matches the Phase 1 rollout.

Exit criteria:

- router can be disabled quickly
- roles can be enabled one by one

## Phase 9: Tests

Objective:
Cover router behavior from unit to workflow level.

Files:

- `tests/test_router_policy.py`
- `tests/test_profile_resolver.py`
- `tests/test_multi_backend_executor.py`
- `tests/test_multi_backend_integration.py`
- `tests/test_cli_profiles.py`

Tests are layered to avoid duplicating the same scenario across files. Each layer injects fakes for the one below it.

**Layer A — `tests/test_router_policy.py`** (policy logic, fake `RouterClient`):

- valid router selection is returned as the chosen profile name
- router returns `null` → policy returns `None`
- router returns a profile not in the role's candidate list → policy returns `None`
- router returns a cross-role profile → policy returns `None`
- single-candidate short-circuit: role has only the default profile → router is **not** invoked, policy returns `None`
- `KANBAN_ROUTER=off` env var → policy returns `None` without touching the client
- per-role allowlist: role not in `router.enabled_roles` → policy returns `None` without touching the client

**Layer B — router client unit tests** (can live in `tests/test_router_policy.py` or a dedicated file):

- valid JSON parses into a `RouterDecision`
- unparseable output → `parse_error`
- timeout → `timeout`
- backend raises → `backend_error`
- router selects a name that equals the role's default profile → treated as a normal `policy` hit (`routing_source = "policy"`), **not** folded into the default path

**Layer C — `tests/test_profile_resolver.py`** (priority only, fake policy):

- card pin beats policy
- planner recommendation beats policy
- policy beats default when it returns a name
- policy returning `None` falls through to default

**Layer D — `tests/test_multi_backend_executor.py` / `tests/test_multi_backend_integration.py`**:

- router picks a non-default profile for each enabled role
- router-selected ACP profile that fails infra still walks its `fallback` chain (Layer 2 fallback, distinct from Layer 1)
- `routing_source`, `routing_reason`, and `router_prompt_version` are written correctly on the execution event
- coding-heavy card routes worker away from default; review-heavy card routes reviewer away from default; generic card falls through to defaults for all four roles

Exit criteria:

- router behavior is deterministic under test
- fallback semantics are covered at both routing and backend layers

## Phase 10: Documentation And Operator Guidance

Objective:
Explain router behavior to developers and operators.

Files:

- `README.md`
- `docs/kanban-cli-guide.md`
- `docs/agent-router-design.md`
- `docs/agent-router-checklist.md`

Tasks:

- document router precedence relative to card pin and planner recommendation
- document the difference between router fallback and profile fallback
- document how to disable router (`KANBAN_ROUTER=off`, removing a role from `router.enabled_roles`)
- document how to make a new profile router-eligible
- update `CLAUDE.md` Executors section with one line stating that router is a pre-selection step in front of `multi-backend` (no effect on `mock` or legacy `agentao` executors)

Exit criteria:

- operators understand why a profile was chosen
- developers understand how to add new routable profiles

## Suggested Delivery Order

1. Audit existing router design / ADR / checklist against this plan; revise those docs only if the contract has actually diverged
2. Add `.agentao/agents/kanban-router.md` (and, if needed for docs-only environments, `kanban/defaults/kanban-router.md`)
3. Enrich profile metadata
4. Build router client + router policy
5. Wire into `multi-backend`
6. Add observability
7. Enable `worker`
8. Expand to `reviewer`
9. Expand to `verifier`
10. Expand to `planner`

## Risks

- router reasons may be too vague early on
- poor profile descriptions can make the router look worse than it is
- planner routing may be harder than worker routing because task intent is broader
- if routing and backend fallback are conflated, debugging will get confusing fast

## Recommended Initial Defaults

- enable router only for `worker` first
- keep router transcript off by default
- treat router `confidence` as diagnostic only
- never block a card because of router failure

## Done Criteria

- router is integrated through `policy`, not by patching workflow logic
- router output is strictly validated
- router can only choose same-role candidates
- router failure always falls through to default profile
- routing decisions are visible in execution events
- router can be rolled out by role with a fast rollback path
- single-candidate roles short-circuit without invoking the router
- `KANBAN_ROUTER=off` bypasses the router in seconds without a code change
- when the router is invoked, the execution event records `router_prompt_version`
