# ADR: Use An Agentao Router Agent For Profile Selection

Related documents:

- Full design: [agent-router-design.md](./agent-router-design.md)
- Implementation checklist: [agent-router-checklist.md](./agent-router-checklist.md)
- Implementation plan: [implementation/agent-router-implementation.md](./implementation/agent-router-implementation.md)

## Status

Proposed

## Context

当前 kanban 已经有稳定的三层执行模型：

- `role`
- `agent_profile`
- `backend`

其中：

- `role` 表示 workflow 阶段
- `agent_profile` 表示该阶段下的具体执行实现
- `backend` 表示该实现如何被调用

当前 profile 路由优先级为：

1. `card.agent_profile`
2. planner recommendation
3. policy
4. role default

问题不在于“能不能支持多个 profile”，而在于：

- 现有系统还不能根据 job 内容自动挑出更合适的 profile
- `worker`、`reviewer`、`verifier`、`planner` 都可能存在多个同 role 但能力不同的 profile
- 当 profile 数量增长时，单纯依赖静态默认值会使系统持续落到 `default-*`

因此需要一个“智能选择层”，在不破坏 workflow 语义的前提下，根据卡片内容从同 role 的候选 profiles 中挑选一个更合适的实现。

## Decision

采用一个独立的 `agentao` router agent 来做 profile selection，而不是在宿主代码中首先实现规则引擎。

router agent 的职责：

- 输入当前 workflow `role`
- 输入卡片摘要
- 输入该 role 的候选 profile 摘要
- 从候选列表中选择一个 profile，或返回空选择

router agent 的边界：

- 不是 workflow role
- 不参与状态机
- 不执行任务
- 不直接读写 repo
- 不直接决定 workflow transition

它只是现有 `policy` 钩子的一种实现方式。

整体语义保持不变：

```text
card pin > planner recommendation > router-powered policy > role default
```

当 router 无法做出可靠选择时，系统回退到该 role 的 `default-*` profile。

## Alternatives Considered

### 1. 规则引擎 / 关键词匹配

示例做法：

- 看 `title` / `goal` 是否包含 `python`、`review`、`verify`
- 通过 capability 标签和关键字表做硬编码匹配
- 匹配不到时回退 default

优点：

- 实现简单
- 没有额外 agent 调用成本
- 行为可预测

缺点：

- profile 数量一增长，规则迅速膨胀
- 中英文混合、短标题、隐式任务信号很难靠关键词稳定覆盖
- 不同 role 的判断逻辑会分散在一堆 if/else 中
- 新增 profile 往往需要同时改代码和规则
- “为什么选这个 profile” 的解释会越来越脆弱

结论：

- 规则引擎适合作为临时兜底，不适合作为主要的长期路由机制

### 2. 把更多能力做成新的 `AgentRole`

示例：

- `python-worker`
- `reviewer-codex`
- `shell-verifier`

优点：

- 调度时看起来直接

缺点：

- 污染 workflow 状态机
- 让 role 混入 provider / language / skill 概念
- claim / retry / timeout / event 审计复杂度扩大
- role 数量出现组合爆炸

结论：

- 拒绝。role 仍然只表示 workflow 阶段

### 3. 由 planner 直接决定所有后续 profile

示例：

- planner 在输出里直接指定 `worker=... reviewer=... verifier=...`

优点：

- 表面上减少一个单独 router 调用

缺点：

- planner 任务被迫承担路由职责，违反单一职责
- planner 失败会同时影响 acceptance criteria 和 profile selection
- 无法独立调优路由提示词
- 后续每个 role 的最新上下文 planner 未必最清楚

结论：

- 不作为主方案。planner recommendation 仍可保留，但不是唯一来源

## Why Router Agent

选择 router agent 的核心原因：

### 1. 路由本身就是语义判断

profile 选择不是简单的 schema 校验，而是对任务类型、风险、输出形式和能力适配度的判断。

例如：

- “写一个 Python 程序打印 2026 年年历”
- “审查最近一批改动有没有回归风险”
- “验证 acceptance criteria 是否机械可验证”

这些都更适合由一个受约束的 agent 做语义判断，而不是在宿主代码中堆更多规则。

### 2. 让宿主代码保持小而稳定

宿主代码更适合负责：

- 候选收集
- 白名单校验
- 回退语义
- 审计记录

而不适合承载大量“任务理解”逻辑。

把语义判断外置到 router agent 后，宿主代码仍然保持：

- 可验证
- 可测试
- 可回退

### 3. 适应 profile 增长

当 profile 从几个增长到十几个甚至更多时：

- 规则引擎维护成本会线性甚至指数上涨
- router agent 仍然只需要比较当前 role 的候选摘要

新增 profile 时，主要工作会变成：

- 补好 `description`
- 补好 `capabilities`

而不是改大量 hard-coded policy 逻辑。

### 4. 更容易解释与调优

router agent 被要求输出：

- `profile`
- `reason`
- `confidence`

这样每次选择都能留下简短理由。

后续如果命中质量不好，优先调：

- router prompt
- profile 描述质量

而不是持续修改宿主判断代码。

## Consequences

### Positive

- 路由能力可以跨四个 workflow role 复用
- 宿主代码不需要维护越来越复杂的规则引擎
- profile 扩展更自然
- 路由选择可解释、可审计
- 失败路径天然安全：router 失败直接回退 default

### Negative

- 增加一次额外 agent 调用
- 引入 prompt 质量和 router 输出稳定性问题
- 需要更严格的 JSON 校验和白名单约束
- rollout 初期需要较强观测，避免静默误选

### Neutral / Acceptable Tradeoff

- router 不是强一致决策器，而是“受约束建议者”
- 宿主仍保留最终裁决权，只接受白名单候选
- router 失败不会中断主执行流

## Guardrails

为了避免 router agent 失控，必须施加以下约束：

- 只给当前 role 的候选 profiles
- 只接受候选白名单中的 profile 名
- 输出必须是严格 JSON
- `profile = null` 合法，表示回退 default
- router 失败不 block 卡片
- router 不能替代 profile backend fallback

因此两层回退始终分开：

```text
router failure -> role default profile
selected profile infra failure -> profile.fallback
```

## Rollout Decision

采用渐进 rollout：

1. 先对 `worker` 启用 router
2. 再扩到 `reviewer`
3. 再扩到 `verifier`
4. 最后扩到 `planner`

原因：

- `worker` 的任务信号通常最明确
- `planner` 的误选成本最高，应该最后接入

## What We Are Not Doing

- 不把规则引擎作为主方案
- 不把 router 变成 workflow role
- 不让 router 直接执行任务
- 不把 router failure 视为 task failure
- 不把 router 结果持久化到 card 作为强绑定状态

## Summary

本 ADR 的结论是：

- **profile 的智能选择采用 `agentao` router agent**
- **宿主代码负责候选收集、校验、回退和审计**
- **规则引擎不是主方案，只能作为未来可选兜底**

这让系统在保持现有 workflow 模型不变的前提下，获得可扩展、可解释、可回退的 profile 智能路由能力。
