# Agentao Router Agent Design

Related documents:

- ADR: [agent-router-adr.md](./agent-router-adr.md)
- Implementation checklist: [agent-router-checklist.md](./agent-router-checklist.md)
- Implementation plan: [implementation/agent-router-implementation.md](./implementation/agent-router-implementation.md)
- Profile/backend design: [agent-profile-acp-design.md](./agent-profile-acp-design.md)
- Profile/backend implementation plan: [implementation/agent-profile-acp-implementation.md](./implementation/agent-profile-acp-implementation.md)

## Background

当前 kanban 已经支持：

- 固定 workflow 角色：`planner` / `worker` / `reviewer` / `verifier`
- `agent_profile` 作为 role 下的具体执行实现
- `subagent` / `acp` 混合 backend
- profile fallback

但 profile 的选择仍然主要依赖：

1. `card.agent_profile`
2. planner recommendation
3. policy hook
4. role default profile

其中第 3 步虽然预留了 `policy` 接口，但当前没有智能路由实现。结果是：

- 编程任务不会自动挑到更适合 coding 的 worker
- review-heavy 卡片不会自动挑到更适合 diff-analysis 的 reviewer
- verifier / planner 也无法按任务内容选择更合适的 profile

这类选择如果用硬编码规则实现，早期可用，但很快会遇到：

- profile 数量增长后规则难维护
- 中英文混合任务描述难以靠关键词覆盖
- 选择理由不稳定，难以持续调优

因此本设计选择：

**使用一个专门的 `agentao` router agent 来完成 profile 智能选择。**

## Problem Statement

我们需要让系统在每次执行某个 workflow role 之前，都能根据卡片内容，从该 role 的候选 profiles 中选出最合适的一个。

核心要求：

- 只在同一 `role` 的 profiles 中选择
- 匹配不到时回退到该 role 的 `default-*`
- 不能让 router agent 直接执行任务
- 不能破坏现有 orchestrator / executor / fallback 语义
- 必须留下清晰的审计信息，解释为什么选中了某个 profile

## Goals

- 为 `planner / worker / reviewer / verifier` 四个角色提供统一的智能路由能力
- 复用 `agentao` agent 机制，而不是再写一套规则引擎
- 将“智能选择”与“实际执行”解耦
- 与当前 `resolve_profile()` 优先级兼容
- 在 router 出错、超时、输出不合法时安全回退到 role default
- 保持事件可审计、测试可覆盖、实现可渐进 rollout

## Non-Goals

- 不让 router agent 直接写代码、审查代码或验证结果
- 不让 router agent 跨 role 选 profile
- 不在 v1 做多 agent 投票式路由
- 不在 v1 让 router 读取整个仓库
- 不在 v1 引入持续学习、在线反馈优化或 profile 自动增删
- 不修改 orchestrator 的 workflow 状态机

## Design Principles

- 单一职责：router 只做 profile selection
- 严格边界：router 只能从候选列表里选，不能发明 profile 名
- 失败安全：router 任意失败都不得阻塞默认执行路径
- 解释优先：每次路由都必须能说明原因
- 小上下文：router 只看卡片摘要和 profile 摘要，不看全仓代码

## Architecture

新增一个逻辑角色：

- `router`

注意：

- `router` 不是 workflow role
- `router` 不出现在 `Card.status`
- `router` 不参与 claim / lease / retry matrix
- `router` 是 executor 内部的一个“前置选择代理”

整体调用链：

```text
Card.status -> workflow role
workflow role + card snapshot + candidate profiles
    -> router agent
router result
    -> resolved profile
resolved profile
    -> backend invoke (subagent / acp)
backend result
    -> orchestrator state transition
```

优先级保持不变：

1. `card.agent_profile`
2. planner recommendation
3. router-powered policy selection
4. role default profile

## Router Responsibilities

router agent 负责：

- 读取当前 role
- 理解卡片的目标、验收标准和上下文摘要
- 比较该 role 的候选 profiles
- 选择一个最适合的 profile，或明确放弃选择
- 返回简短、结构化、可审计的选择理由

router agent 不负责：

- 执行实际任务
- 读取或修改工作区文件
- 输出任务实现内容
- 判断 workflow 是否推进到下一阶段

## Candidate Profile Model

router 只会看到当前 role 的候选 profiles。

每个候选 profile 的输入摘要至少包含：

- `name`
- `role`
- `backend.type`
- `backend.target`
- `fallback`
- `capabilities`
- `description`

建议未来在 `agent_profiles.yaml` 中补强：

- `description`
- `capabilities`
- 可选 `examples` 或 `routing_hint`

router 不依赖 profile 的完整 YAML 原文，只依赖预先整理好的摘要。

## Card Summary Model

router 的卡片输入不应是完整 card 原文，而是标准化摘要。

建议包含：

- `card_id`
- `title`
- `goal`
- `role`
- `priority`
- `acceptance_criteria`
- `context_refs`
- `current_agent_profile`（如有）

其中 `context_refs` 仅传摘要：

- `path`
- `kind`
- `note`

不直接展开文件内容。这样可以控制上下文大小，并避免 router 参与具体实现。

## Prompting Strategy

router prompt 应明确约束：

- 你是 profile router，不是 task executor
- 你只能从给定 candidates 中选一个
- 如果没有明显合适的，就返回 `null`
- 你必须输出合法 JSON
- 选择依据应简短、可审计、与卡片内容直接相关

router 的系统约束建议包含：

- 不要编造 profile
- 不要输出额外说明文字
- 不要建议跨 role profile
- 不要把 fallback 当成主选项理由

## Router Output Schema

建议输出严格 JSON：

```json
{
  "profile": "gemini-worker",
  "reason": "The card is a coding task and this profile is optimized for code and shell work.",
  "confidence": 0.86
}
```

字段定义：

- `profile`: `string | null`
  - 必须是候选列表中的 profile 名
  - `null` 表示没有足够把握，应回退默认 profile
- `reason`: `string`
  - 1-2 句短理由
  - 要引用卡片的真实任务信号，例如 coding / review / verification / planning
- `confidence`: `number`
  - `0.0 ~ 1.0`
  - 仅作为诊断字段，不作为唯一决策依据

可选扩展字段：

- `signals`: `list[str]`
- `rejected_profiles`: `list[str]`

但 v1 不强制。

## Routing Decision Rules

router 结果的应用规则：

1. `profile` 为合法候选名
   - 接受该选择
   - `routing_source = "policy"`
   - `routing_reason` 记录 router reason

2. `profile = null`
   - 忽略 router 选择
   - 落到 role default profile

3. 输出无法解析
   - 视为 router failure
   - 落到 role default profile

4. `profile` 不在候选列表
   - 视为 invalid router result
   - 落到 role default profile

5. router 超时 / backend 失败
   - 视为 router infrastructure failure
   - 落到 role default profile

重要约束：

- router failure 不能让卡片进入 `blocked`
- router failure 不能跳过实际执行阶段
- router 只是“选择失败”，不是“任务失败”

## Fallback Matrix

需要区分两层 fallback：

### Layer 1: Router fallback

这是“选谁来做”这一层的回退。

- card pin 生效：不走 router
- planner recommendation 生效：不走 router
- router 无结论 / 失败：回退 role default profile

### Layer 2: Profile fallback

这是“已选中 profile 但 backend 调用失败”这一层的回退。

例如：

- router 选中 `gemini-worker`
- `gemini-worker` 是 ACP profile
- ACP backend 发生 infrastructure failure
- 再走 `gemini-worker.fallback -> default-worker`

因此完整链路可能是：

```text
router failed
  -> default-worker

router chose gemini-worker
  -> gemini-worker failed on infra
  -> default-worker
```

两层 fallback 不应混淆。

## Integration Points

### 1. New Router Agent Definition

新增一份 agent 定义，例如：

- `kanban/defaults/kanban-router.md`

该 agent 的职责仅是 route selection。

### 2. Executor Wiring

在 `MultiBackendExecutor` 或其构造路径中增加 router 调用器。

推荐做法：

- 保持 `resolve_profile()` 不变
- 将 router 封装为一个 `policy(role, card, config) -> profile_name | None`
- 把该 policy 注入 `MultiBackendExecutor`

这样可以复用现有优先级：

- card pin
- planner recommendation
- policy
- default

### 3. Candidate Builder

需要一层内部适配逻辑，将配置中的 profiles 过滤为当前 role 的 candidates，并整理成 router 输入摘要。

### 4. Router Client

需要一个内部调用器，负责：

- 加载 router agent spec
- 发送 prompt
- 解析 JSON
- 校验输出
- 规范化失败类型

## Suggested Code Changes

建议新增或修改以下文件：

- `kanban/defaults/kanban-router.md`
- `kanban/executors/router_policy.py`
- `kanban/executors/router_agent.py`
- `kanban/executors/multi_backend.py`
- `kanban/cli.py`
- `kanban/agent_profiles.yaml`

职责建议：

- `router_policy.py`
  - 组装 router 输入
  - 调用 router
  - 把 router 输出转成 `policy()` 返回值

- `router_agent.py`
  - 与 `agentao` agent 执行接口交互
  - 解析和验证 router JSON 结果

- `multi_backend.py`
  - 继续消费统一 `policy`

- `cli.py`
  - 在 `_build_executor("multi-backend")` 时注入 router policy

## Event And Observability Design

现有执行事件已经有：

- `agent_profile`
- `backend_type`
- `routing_source`
- `routing_reason`
- `fallback_from_profile`

这足以支撑 router rollout，但建议补充以下观测语义：

- 当 router 选中 profile 时：
  - `routing_source = "policy"`
  - `routing_reason = "router selected gemini-worker: coding task, shell needed"`

- 当 router 返回空结果时：
  - 仍使用 default profile
  - `routing_source = "default"`
  - `routing_reason = "role default for worker (router found no strong match)"`

- 当 router 失败时：
  - 仍使用 default profile
  - `routing_source = "default"`
  - `routing_reason = "role default for worker (router failed: timeout)"`

是否单独记录 router event：

- v1 建议不引入独立 event 类型
- 先把结果折叠进 execution event 的 `routing_reason`
- 如果后续排障需求上来，再增加独立 router diagnostic event

## Failure Model

router 调用失败分类建议：

- `parse_error`
  - 输出不是合法 JSON
- `invalid_choice`
  - 选中的 profile 不在候选列表
- `timeout`
  - router 调用超时
- `backend_error`
  - router agent 本身调用失败
- `empty_choice`
  - router 显式返回 `null`

这些错误都不应改变卡片状态，只能影响路由结果。

## Security And Trust Boundary

router agent 是“建议者”，不是“执行者”。

需要明确以下信任边界：

- router 不能直接执行 shell
- router 不能直接编辑 repo
- router 不能访问未显式提供的上下文
- router 输出必须经过宿主进程校验
- 宿主进程只接受白名单候选值

这使得即使 router prompt 被误导，也最多导致“回退默认”，而不是越权执行。

## Rollout Plan

建议分阶段 rollout：

### Phase 1: Worker only

- 先只对 `worker` 开启 router
- 验证 coding task 的命中质量
- 验证默认回退路径

### Phase 2: Reviewer and Verifier

- 扩到 `reviewer` / `verifier`
- 重点观察 `routing_reason` 可解释性

### Phase 3: Planner

- 最后接 `planner`
- planner 的误选风险更高，因为会影响 acceptance criteria 质量

### Phase 4: All-role default-on

- 四个 role 都默认启用 router
- 保留开关，允许临时禁用 router

## Configuration And Operator Controls

建议增加最小可控开关：

- 全局启用/禁用 router
- 可按 role 启用/禁用 router

形式可以是：

- CLI 参数
- 环境变量
- 或后续加到 `agent_profiles.yaml`

v1 推荐最简单方案：

- 代码内默认开启 `worker`
- 其余 role 通过显式配置打开

如果直接全开，则必须先补齐测试和观测。

## Testing Plan

### Unit Tests

`tests/test_router_policy.py`

- 只给当前 role 的 candidates
- router 选中合法 profile
- router 返回 `null`
- router 返回非法 profile
- router 输出不可解析
- router 超时

`tests/test_profile_resolver.py`

- card pin 仍优先于 router
- planner recommendation 仍优先于 router
- router 命中时优先于 default
- router 失败时回退 default

`tests/test_multi_backend_executor.py`

- router 为四个 role 分别选中不同 profile
- 被选 profile 执行后事件字段正确
- 被选 ACP profile 失败后走 profile fallback

### Integration Tests

`tests/test_multi_backend_integration.py`

- 一张 coding-heavy 卡：
  - planner 走 default-planner
  - worker 走 gemini-worker
  - reviewer 走 default-reviewer 或 gemini-reviewer
  - verifier 走 default-verifier

- 一张 review-heavy 卡：
  - reviewer 被 router 命中

- 一张普通卡：
  - 四个 role 都回退各自 default

### Negative Tests

- router agent 定义缺失
- router 输出额外文本包裹 JSON
- router 返回跨 role profile
- router backend 故障

## Open Questions

- router agent 应使用单独 backend，还是复用现有 subagent 机制？
- router 是否需要自己的 prompt version 审计字段？
- `confidence` 是否要参与阈值判断，还是仅作日志字段？
- 是否需要为 router 决策保留原始 transcript？
- planner recommendation 与 router 是否应该融合，还是保持独立来源？

## Recommended Initial Decisions

- router v1 复用现有 subagent backend
- router transcript 默认不单独持久化
- `confidence` 仅作诊断字段，不作硬门槛
- router failure 一律回退 role default
- 先从 `worker` rollout，再扩到其余角色

## Summary

本设计的核心不是“让 LLM 直接决定 workflow”，而是：

- 用一个边界明确的 router agent
- 在 role 内部做 profile 选择
- 保持 card pin / planner recommendation / default fallback 的原有优先级
- 让所有失败都安全退回默认 profile

这样可以在不破坏现有 kanban 状态机的前提下，为四个执行角色增加可演进的智能路由能力。
