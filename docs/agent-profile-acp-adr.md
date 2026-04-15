# ADR: Agent Profile + ACP Integration

Related documents:

- Full design: [agent-profile-acp-design.md](./agent-profile-acp-design.md)
- Implementation checklist: [agent-profile-acp-checklist.md](./agent-profile-acp-checklist.md)
- Implementation plan: [implementation/agent-profile-acp-implementation.md](./implementation/agent-profile-acp-implementation.md)

## Status

Proposed

## Context

当前 kanban 的 workflow role 固定为：

- `planner`
- `worker`
- `reviewer`
- `verifier`

这些 role 已深度参与：

- `status -> role` 调度
- claim / lease / timeout
- retry matrix
- execution event 审计

因此 role 表达的是 **workflow 阶段**，而不是 provider、语言或技能标签。

随着 ACP Server 接入，系统需要支持：

- 同一 role 下的多个执行实现
- ACP Server 与内置 subagent 混合执行
- 后端故障时有限回退

如果直接把 `python-worker`、`codex-reviewer` 等概念做成新的 `AgentRole`，会污染状态机并放大调度复杂度。

## Decision

采用三层模型：

- `role`
- `agent_profile`
- `backend`

语义如下：

- `role`：流程职责
- `agent_profile`：该职责的具体实现选择
- `backend`：该实现的调用方式

总体关系：

```text
Card.status -> role
role + card/profile policy -> agent_profile
agent_profile -> backend
backend -> ACP Server / Subagent / Local executor
```

### Role

保持现有 `AgentRole` 不变。

role 只表达 workflow 阶段，不引入：

- provider
- 语言
- 工具能力

### Agent Profile

新增 `agent_profile` 作为 role 下的实现路由层。

示例：

- `default-worker`
- `claude-code-worker`
- `python-worker`
- `codex-reviewer`

profile 至少包含：

- `name`
- `role`
- `backend.type`
- `backend.target`
- `fallback`

### Backend

第一阶段支持两类 backend：

- `subagent`
- `acp`

其中：

- `subagent` 走现有内置 agent definition
- `acp` 复用 `agentao.acp_client`

## Configuration

配置拆成两层：

### 1. ACP server config

直接复用：

- `.agentao/acp.json`

由 `agentao` 负责：

- server registry
- command / args / env / cwd
- timeout
- capabilities

kanban 不重复设计 ACP server 配置格式。

### 2. Profile routing config

新增：

- `kanban/agent_profiles.yaml`

负责：

- role 默认 profile
- profile -> backend.type / backend.target
- fallback

## Card Changes

Card 仅增加：

- `agent_profile: str | None`
- `agent_profile_source: str | None`

Card 不存：

- ACP server command
- endpoint
- env / secrets
- transport 细节

## ACP Integration

kanban 不自行实现 ACP client。

直接复用 `agentao.acp_client` 的稳定 embedding surface，尤其是：

- `ACPManager.from_project(...)`
- `ACPManager.prompt_once(...)`
- `AcpClientError`
- `AcpErrorCode`
- `AcpInteractionRequiredError`
- `get_status()`
- `get_server_logs()`

v1 默认集成方式：

```python
mgr = ACPManager.from_project(project_root)
result = mgr.prompt_once(
    server_name,
    prompt,
    cwd=workdir,
    interactive=False,
)
```

kanban 不自己编排：

- connect
- create_session
- cleanup

## Session Strategy

v1 使用：

- 每次执行一次独立 ACP session

具体由 `ACPManager.prompt_once(...)` 提供。

不做：

- 跨卡 session 复用
- 跨 role session 复用

## Failure Mapping

kanban 必须按 `AcpErrorCode` 做结构化映射，而不是匹配错误字符串。

建议最小映射：

- `CONFIG_INVALID` -> routing/config failure
- `SERVER_NOT_FOUND` -> routing/config failure
- `PROCESS_START_FAIL` -> infrastructure
- `HANDSHAKE_FAIL` -> infrastructure
- `REQUEST_TIMEOUT` -> infrastructure
- `TRANSPORT_DISCONNECT` -> infrastructure
- `PROTOCOL_ERROR` -> infrastructure
- `SERVER_BUSY` -> infrastructure
- `INTERACTION_REQUIRED` -> interaction-required

关键规则：

- 只有 infrastructure failure 可触发 fallback
- `INTERACTION_REQUIRED` 默认不重试、不 fallback
- functional failure 不触发 fallback

## Non-Interactive Constraint

kanban daemon/worker 必须运行在非交互模式。

因此：

- 若 ACP server 请求 permission / ask-user
- kanban 不等待用户输入
- `agentao` 返回 `AcpInteractionRequiredError`
- kanban 将其视为 profile/backend 不适合无人值守执行

## Executor Boundary

orchestrator 继续只关心：

- `status -> role`
- apply result
- retry / recovery

executor 负责：

- `role -> profile`
- `profile -> backend`
- backend raw response -> `AgentResult`

backend 不拥有 workflow transition 权限。

## Observability

execution events 至少应补充：

- `agent_profile`
- `backend_type`
- `backend_target`
- `routing_reason`
- `fallback_from_profile`

ACP 诊断优先走：

- `ACPManager.get_status()`
- `ACPManager.get_server_logs()`

而不是解析 CLI 输出。

## Consequences

优点：

- 保持 workflow role 语义稳定
- 可同时支持 ACP 与内置 subagent
- 最小化对 orchestrator 的侵入
- 复用 agentao 已实现的嵌入式 ACP 能力
- 错误分类与审计更细粒度

代价：

- 需要新增 profile config 与 resolver
- 需要定义 `AcpErrorCode -> kanban failure category` 映射
- 需要扩展 execution event 字段

## Rollout

### Phase 1

- 增加 `agent_profile`
- 增加 `agent_profiles.yaml`
- 支持 `subagent` + `acp`
- ACP backend 默认走 `prompt_once(..., interactive=False)`

### Phase 2

- 增加 infrastructure fallback
- 增加 routing / reroute / backend_failed 事件

### Phase 3

- planner recommendation
- policy 自动匹配

## Initial Scope

建议首批只接两个 ACP profile：

- `worker -> claude-code-worker`
- `reviewer -> codex-reviewer`

其余先保持 subagent：

- `planner -> default-planner`
- `verifier -> default-verifier`
