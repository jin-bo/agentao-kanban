# Agentao Router Agent Implementation Checklist

Related documents:

- ADR: [agent-router-adr.md](./agent-router-adr.md)
- Full design: [agent-router-design.md](./agent-router-design.md)
- Implementation plan: [implementation/agent-router-implementation.md](./implementation/agent-router-implementation.md)
- Profile/backend design: [agent-profile-acp-design.md](./agent-profile-acp-design.md)
- Profile/backend implementation plan: [implementation/agent-profile-acp-implementation.md](./implementation/agent-profile-acp-implementation.md)

## Goal

按最小风险路径实现：

- 用一个独立的 `agentao` router agent 为 `planner / worker / reviewer / verifier` 选择最合适的 profile
- 匹配不到时回退到各自 `default-*`
- 不改变 orchestrator 的 workflow 语义
- 不让 router agent 直接执行任务

## Phase 1: Router Contract

- [ ] 明确 router 的系统职责：只做 profile selection，不做 task execution
- [ ] 明确 router 只允许在当前 `role` 的 candidates 中选 profile
- [ ] 明确 router 任意失败都只能回退 default，不得 block 卡片
- [ ] 定义 router 输出 schema：
  - `profile`
  - `reason`
  - `confidence`
- [ ] 明确 `profile = null` 的语义：没有足够把握，回退 role default
- [ ] 明确非法输出的处理：解析失败、越权 profile、空 JSON 都视为 router failure

## Phase 2: Router Agent Definition

- [ ] 新增 `kanban/defaults/kanban-router.md`
- [ ] 在 router agent 定义中写清：
  - 你不是 task executor
  - 你只能从候选列表中选择
  - 没有明显合适项时返回 `null`
  - 必须输出严格 JSON
- [ ] 为 router agent 设定稳定的输出示例
- [ ] 为 router agent 设定简短、可审计的 `reason` 要求
- [ ] 确认 router agent 不依赖 repo 全量上下文

## Phase 3: Profile Metadata Readiness

- [ ] 审查 `kanban/agent_profiles.yaml` 中现有 profiles 是否都具备基本可读描述
- [ ] 为每个可参与自动路由的 profile 补 `description`
- [ ] 为每个 profile 补 `capabilities`
- [ ] 统一 profile 描述风格，避免 router 只能从名字猜语义
- [ ] 明确哪些 profiles 暂不参与自动路由

## Phase 4: Router Input Builder

- [ ] 新增内部 card summary builder
- [ ] router 输入包含：
  - `card_id`
  - `title`
  - `goal`
  - `role`
  - `priority`
  - `acceptance_criteria`
  - `context_refs`
- [ ] `context_refs` 只传 `path / kind / note`，不展开文件内容
- [ ] 新增 candidate builder，只保留当前 `role` 的 profiles
- [ ] candidate summary 至少包含：
  - `name`
  - `role`
  - `backend.type`
  - `backend.target`
  - `fallback`
  - `capabilities`
  - `description`
- [ ] 确保 default profile 也在候选列表中，方便 router 显式选择或放弃选择

## Phase 5: Router Client

- [ ] 新增 `kanban/executors/router_agent.py`
- [ ] 封装 router agent 的调用接口
- [ ] 复用现有 `agentao` / subagent 执行路径，不单独造一套 agent runtime
- [ ] 新增 JSON 解析与 schema 校验
- [ ] 校验输出的 `profile` 必须属于当前 candidate whitelist
- [ ] 为 router 调用增加超时
- [ ] 为 router 调用统一失败分类：
  - `parse_error`
  - `invalid_choice`
  - `timeout`
  - `backend_error`
  - `empty_choice`

## Phase 6: Policy Integration

- [ ] 新增 `kanban/executors/router_policy.py`
- [ ] 将 router 封装成 `policy(role, card, config) -> profile_name | None`
- [ ] 保持 `resolve_profile()` 优先级不变：
  - `card.agent_profile`
  - planner recommendation
  - router policy
  - role default
- [ ] 在 `policy()` 内部只处理 router 相关逻辑，不混入 backend fallback
- [ ] router 失败时返回 `None`，让 resolver 自然回退 default
- [ ] 不允许 router 覆盖 card pin
- [ ] 不允许 router 覆盖 planner recommendation

## Phase 7: Executor Wiring

- [ ] 在 `MultiBackendExecutor` 构造路径注入 router policy
- [ ] 在 `kanban/cli.py` 的 `_build_executor("multi-backend")` 中接入 router policy
- [ ] 确认 `mock` executor 不受影响
- [ ] 确认旧 `agentao` executor 不受影响
- [ ] 确认只有 `multi-backend` 路径启用 router

## Phase 8: Fallback Semantics

- [ ] 区分 router fallback 与 profile fallback
- [ ] router 无选择或失败：
  - 回退到 role default profile
- [ ] router 选中 profile 后，如果 backend infrastructure failure：
  - 再走 profile 自身 `fallback`
- [ ] 确保不会把 router failure 误记为 task failure
- [ ] 确保不会把 router failure 误记为 execution blocked

## Phase 9: Observability

- [ ] 复用现有 execution event 字段：
  - `agent_profile`
  - `backend_type`
  - `routing_source`
  - `routing_reason`
  - `fallback_from_profile`
- [ ] router 命中时记录：
  - `routing_source = "policy"`
  - `routing_reason` 含 router 简要理由
- [ ] router 返回空结果时记录：
  - `routing_source = "default"`
  - `routing_reason` 说明 router 无明确匹配
- [ ] router 失败时记录：
  - `routing_source = "default"`
  - `routing_reason` 说明 router 失败类型
- [ ] 决定 v1 是否保留 router 原始 transcript
- [ ] 若不保留 transcript，确保 `routing_reason` 足够可审计

## Phase 10: Config And Rollout Controls

- [ ] 增加 router 总开关
- [ ] 支持按 `role` 开关 router
- [ ] 明确默认 rollout 顺序：
  - `worker`
  - `reviewer`
  - `verifier`
  - `planner`
- [ ] 提供快速禁用 router 的操作方式，便于故障回退

## Phase 11: Tests

### Router Unit Tests

- [ ] router 选中合法 profile
- [ ] router 返回 `null`
- [ ] router 返回不存在的 profile
- [ ] router 返回跨 role profile
- [ ] router 输出无法解析
- [ ] router 超时
- [ ] router backend 失败

### Resolver Tests

- [ ] `card.agent_profile` 优先于 router
- [ ] planner recommendation 优先于 router
- [ ] router 命中优先于 default
- [ ] router 失败回退 default

### Executor Tests

- [ ] worker 可被 router 命中到非默认 profile
- [ ] reviewer 可被 router 命中到非默认 profile
- [ ] verifier 可被 router 命中到非默认 profile
- [ ] planner 可被 router 命中到非默认 profile
- [ ] 被选 ACP profile 失败后走 profile fallback
- [ ] router failure 不影响任务继续执行

### Integration Tests

- [ ] coding-heavy 卡片命中 coding worker
- [ ] review-heavy 卡片命中 review profile
- [ ] verification-heavy 卡片命中 verifier profile
- [ ] 普通卡片四个 role 都回退 default
- [ ] execution events 中能看出 router 的选择结果

## Phase 12: Documentation

- [ ] 在 `README.md` 或 CLI guide 中补充 router 行为说明
- [ ] 说明 router 与 `card.agent_profile` 的优先级关系
- [ ] 说明 router 与 profile fallback 的区别
- [ ] 说明如何禁用 router
- [ ] 说明如何给新 profile 提供可路由的 `description / capabilities`

## Suggested Initial Rollout

- [ ] 首先只对 `worker` 启用 router
- [ ] 观察：
  - router 命中率
  - 回退 default 比例
  - 选错 profile 的案例
- [ ] 第二阶段启用 `reviewer`
- [ ] 第三阶段启用 `verifier`
- [ ] 最后启用 `planner`
- [ ] 在每个阶段收集实际 `routing_reason` 样本，修 prompt 而不是临时堆规则

## Done Criteria

- [ ] router 只负责 profile selection
- [ ] router 不能跨 role 选 profile
- [ ] router 任意失败都安全回退 default
- [ ] 四个 workflow role 都可接入同一智能路由机制
- [ ] 事件中可以解释为何选中了某个 profile
- [ ] rollout 可按 role 渐进开启与回退
