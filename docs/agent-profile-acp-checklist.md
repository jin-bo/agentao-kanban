# Agent Profile + ACP Implementation Checklist

Related documents:

- ADR: [agent-profile-acp-adr.md](./agent-profile-acp-adr.md)
- Full design: [agent-profile-acp-design.md](./agent-profile-acp-design.md)
- Implementation plan: [implementation/agent-profile-acp-implementation.md](./implementation/agent-profile-acp-implementation.md)

## Goal

按最小风险路径实现：

- 固定 workflow `role`
- 新增 `agent_profile`
- 支持 `subagent` + `acp` 两类 backend
- ACP backend 复用 `agentao.acp_client`

## Phase 1: Data And Config

- [ ] 在 `Card` 模型中增加 `agent_profile: str | None`
- [ ] 在 `Card` 模型中增加 `agent_profile_source: str | None`
- [ ] 更新 store 持久化与反序列化，保证新字段可选且兼容旧卡
- [ ] 新增 `kanban/agent_profiles.py`
- [ ] 定义 `agent_profiles.yaml` 的最小 schema
- [ ] 支持 role 默认 profile
- [ ] 支持 profile -> `backend.type` / `backend.target`
- [ ] 支持 profile `fallback`
- [ ] 增加配置校验：profile 存在、role 匹配、fallback 同 role、fallback 无环

## Phase 2: Executor Structure

- [ ] 新增 `kanban/executors/profile_resolver.py`
- [ ] 定义 profile 解析优先级：
  - `card.agent_profile`
  - planner recommendation
  - policy match
  - role default profile
- [ ] 新增 `kanban/executors/backends/base.py`
- [ ] 定义统一 backend 接口
- [ ] 新增 `kanban/executors/backends/subagent_backend.py`
- [ ] 把现有 subagent 执行逻辑下沉到 `subagent_backend`
- [ ] 新增 `kanban/executors/backends/acp_backend.py`
- [ ] 新增 `kanban/executors/multi_backend.py`
- [ ] 让顶层 executor 负责：
  - resolve profile
  - select backend
  - 统一解析 raw response -> `AgentResult`

## Phase 3: ACP Backend

- [ ] 在 ACP backend 中复用 `ACPManager.from_project(...)`
- [ ] 默认使用 `prompt_once(..., interactive=False)` 执行一次任务
- [ ] 传入 per-call `cwd`
- [ ] 校验 `backend.target` 在 `.agentao/acp.json` 中存在
- [ ] 返回统一 raw text + backend metadata
- [ ] 不在 kanban 中重新实现 ACP client / session lifecycle

## Phase 4: Failure Mapping

- [ ] 定义 `AcpErrorCode -> kanban failure category` 映射表
- [ ] `CONFIG_INVALID` -> routing/config failure
- [ ] `SERVER_NOT_FOUND` -> routing/config failure
- [ ] `PROCESS_START_FAIL` -> infrastructure
- [ ] `HANDSHAKE_FAIL` -> infrastructure
- [ ] `REQUEST_TIMEOUT` -> infrastructure
- [ ] `TRANSPORT_DISCONNECT` -> infrastructure
- [ ] `PROTOCOL_ERROR` -> infrastructure
- [ ] `SERVER_BUSY` -> infrastructure
- [ ] `INTERACTION_REQUIRED` -> interaction-required
- [ ] 明确 `interaction-required` 默认不 retry、不 fallback
- [ ] 明确只有 infrastructure failure 允许 fallback

## Phase 5: Events And Observability

- [ ] 扩展 execution event 字段：
  - `agent_profile`
  - `backend_type`
  - `backend_target`
  - `routing_reason`
  - `fallback_from_profile`
- [ ] 增加事件类型：
  - `execution.routed`
  - `execution.rerouted`
  - `execution.backend_failed`
- [ ] 在 ACP backend 中接入 `get_status()` / `get_server_logs()` 的诊断信息
- [ ] 明确不解析 CLI 输出做诊断

## Phase 6: CLI

- [ ] `card edit --agent-profile <name>`
- [ ] `card edit --clear-agent-profile`
- [ ] `profiles list`
- [ ] `profiles show <name>`
- [ ] 可选 `profiles doctor`

## Phase 7: Tests

### Config

- [ ] `agent_profiles.yaml` 解析测试
- [ ] profile-role 校验测试
- [ ] fallback 校验测试
- [ ] 旧 card 数据兼容测试

### Routing

- [ ] card 显式 profile 优先
- [ ] planner recommendation 次优先
- [ ] default profile 回退
- [ ] 非法 profile 自动降级

### ACP Backend

- [ ] mock `ACPManager.prompt_once(...)`
- [ ] target 存在时正常执行
- [ ] target 缺失时稳定失败
- [ ] `AcpErrorCode` 映射测试
- [ ] `INTERACTION_REQUIRED` 不被误归为 infrastructure

### Executor Integration

- [ ] subagent profile 可运行
- [ ] acp profile 可运行
- [ ] infrastructure failure 触发 fallback
- [ ] functional failure 不触发 fallback
- [ ] 两类 backend 共用同一 response parser

### Workflow

- [ ] orchestrator 不感知 profile
- [ ] retry matrix 对 acp backend 生效
- [ ] execution events 带 profile/backend 元数据

## Suggested Initial Rollout

- [ ] 首批只启用：
  - `worker -> claude-code-worker`
  - `reviewer -> codex-reviewer`
- [ ] `planner` 先保持 `default-planner`
- [ ] `verifier` 先保持 `default-verifier`
- [ ] 小范围卡片试点
- [ ] 观察 fallback 频率、interaction-required 频率、backend 失败率

## Done Criteria

- [ ] 不修改 `AgentRole`
- [ ] 不在 Card 中存 ACP server 连接细节
- [ ] ACP backend 复用 `agentao.acp_client`
- [ ] 默认走非交互 `prompt_once(...)`
- [ ] `AcpErrorCode` 映射稳定
- [ ] 事件审计可区分 role/profile/backend
