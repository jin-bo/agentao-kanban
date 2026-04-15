# Agent Profile + ACP Integration Design

Related documents:

- ADR: [agent-profile-acp-adr.md](./agent-profile-acp-adr.md)
- Implementation checklist: [agent-profile-acp-checklist.md](./agent-profile-acp-checklist.md)
- Implementation plan: [implementation/agent-profile-acp-implementation.md](./implementation/agent-profile-acp-implementation.md)

## Background

当前 kanban 系统已经建立了稳定的 workflow 模型，核心角色为：

- `planner`
- `worker`
- `reviewer`
- `verifier`

这些角色不只是 prompt 上的身份标签，还参与了系统运行时语义，包括：

- `Card.status -> AgentRole` 的调度映射
- claim / lease / timeout
- retry matrix
- execution event 审计
- workflow transition ownership

当前实现默认假设：

- 一个 workflow `role` 对应一个内置 subagent 定义
- executor 直接根据 `role` 选择对应 agent spec 并执行

这一模型在仅使用内置 subagent 时足够简单，但一旦引入 ACP Server 或多个不同能力的执行实现，就会出现新的需求。例如：

- `worker` 想交给 Claude Code
- `reviewer` 想交给 Codex
- 同一个 `worker` 还可能区分为 `python-worker`、`cpp-worker`、`data-worker`

这意味着系统需要支持：

- 同一 workflow role 下的多个可替换实现
- ACP Server 与内置 Subagent 混合执行
- 在 backend 不可用时进行有限回退
- 保持 workflow 语义不被 provider / skill 污染

## Problem Statement

当前系统缺少一个中间抽象层，无法稳定表达：

- 一个 `role` 具体由哪个 agent 实现
- 这个 agent 是通过 ACP 调用还是通过内置 subagent 调用
- backend 故障时应如何回退
- 不同实现的执行质量如何审计和比较

如果直接把 `python-worker`、`codex-reviewer` 之类概念提升为新的 `AgentRole`，会产生明显问题：

- workflow 角色与实现身份混杂
- 调度、超时、重试、事件模型复杂化
- role 数量出现 `阶段 × 技能 × provider` 组合爆炸
- 替换 provider 时需要修改状态机和运行时语义

因此问题本质不是“增加更多 role”，而是：

**需要在 workflow role 与执行 backend 之间引入一个独立的 `agent_profile` 层。**

## Goals

本设计的目标是：

- 保持现有 workflow role 不变
- 支持一个 role 对应多个可替换实现
- 支持 ACP Server 和内置 Subagent 混合执行
- 复用 `agentao` 已有 ACP client 能力，不重复实现 ACP runtime
- 让 orchestrator 不感知 provider / ACP 细节
- 保留清晰的失败分类、回退策略和审计能力
- 保持对现有 board 和 executor 设计的最小侵入

## Non-Goals

本设计不包含以下目标：

- 不将 provider、语言或能力标签并入 `AgentRole`
- 不把 ACP server 连接配置写进 Card
- 不在第一阶段做 LLM 驱动的智能动态路由
- 不在第一阶段做跨卡、跨 role 的 ACP 长会话复用
- 不在 kanban 内部复制一套完整 ACP 运维 CLI
- 不让外部 ACP server 参与 workflow transition 决策

## Core Model

系统引入三层概念：

- `role`
- `agent_profile`
- `backend`

三者职责分别为：

- `role`：流程职责，即当前这个卡在 workflow 中处于什么阶段
- `agent_profile`：这个职责由哪一种具体 agent 实现
- `backend`：这个实现通过什么运行时被调用

可以概括为：

- `role = 做什么`
- `profile = 谁来做 / 擅长怎么做`
- `backend = 怎么调用`

总体关系为：

```text
Card.status -> role
role + card metadata + routing policy -> agent_profile
agent_profile -> backend
backend -> ACP Server / Subagent / Local executor
```

## Role Model

`role` 保持现有固定集合：

- `planner`
- `worker`
- `reviewer`
- `verifier`

重要约束：

- role 只表示 workflow 阶段
- role 不包含 provider、模型、语言、工具能力
- retry / timeout / claim / transition 等运行时策略仍按 role 工作

这意味着：

- `worker` 始终表示实施阶段
- 无论本次是 `claude-code-worker` 还是 `python-worker` 执行，它在 workflow 层都仍然只是 `worker`

## Agent Profile Model

`agent_profile` 表示某个 role 的具体执行实现。

示例：

- `default-planner`
- `default-worker`
- `claude-code-worker`
- `python-worker`
- `cpp-worker`
- `default-reviewer`
- `codex-reviewer`
- `default-verifier`

建议 profile 包含以下字段：

- `name`
- `role`
- `backend.type`
- `backend.target`
- `fallback`
- `capabilities`
- 可选 `match`
- 可选 `description`

语义如下：

- `role`
  说明该 profile 只能服务于哪个 workflow role
- `backend.type`
  说明该 profile 通过什么 backend 执行，例如 `subagent` 或 `acp`
- `backend.target`
  指向具体的内置 agent 名称或 ACP server 名称
- `fallback`
  指向同一 role 下的另一个 profile，在基础设施失败时回退
- `capabilities`
  表达 profile 的能力标签，例如 `python`、`review`、`shell`
- `match`
  用于未来的自动路由 hint，不在 v1 强绑定自动行为

## Backend Model

backend 是 profile 的运行时实现层。

本设计支持三类 backend：

- `subagent`
- `acp`
- `local`

含义如下：

- `subagent`
  使用当前内置的 subagent 定义文件执行
- `acp`
  使用 `agentao` 的 ACP client 调用外部 ACP server
- `local`
  用于本地执行器，例如 test runner 或 deterministic verifier

第一阶段的重点是：

- `subagent`
- `acp`

`local` 可以作为未来 verifier 扩展预留，但不是 v1 的核心目标。

## Configuration Design

配置分两层：

- ACP server 配置
- profile routing 配置

### ACP Server Config

ACP server 配置直接复用 `agentao` 的项目级配置：

`.agentao/acp.json`

这一层负责：

- server 名称
- `command`
- `args`
- `env`
- `cwd`
- `requestTimeoutMs`
- `startupTimeoutMs`
- `capabilities`
- `description`

kanban 不再重复发明另一套 ACP server registry 格式。

### Profile Routing Config

kanban 自己新增一份 profile 配置，例如：

`kanban/agent_profiles.yaml`

这一层负责：

- role 的默认 profile
- profile -> backend.type / backend.target
- fallback
- match / routing hints

示例：

```yaml
roles:
  planner:
    default_profile: default-planner
  worker:
    default_profile: claude-code-worker
  reviewer:
    default_profile: codex-reviewer
  verifier:
    default_profile: default-verifier

profiles:
  default-planner:
    role: planner
    backend:
      type: subagent
      target: kanban-planner

  default-worker:
    role: worker
    backend:
      type: subagent
      target: kanban-worker

  claude-code-worker:
    role: worker
    backend:
      type: acp
      target: claude-code-main
    fallback: default-worker
    capabilities: [code, repo-edit, shell]

  python-worker:
    role: worker
    backend:
      type: acp
      target: claude-code-main
    fallback: default-worker
    capabilities: [python, code, repo-edit, shell]

  cpp-worker:
    role: worker
    backend:
      type: acp
      target: claude-code-main
    fallback: default-worker
    capabilities: [cpp, code, repo-edit, shell]

  default-reviewer:
    role: reviewer
    backend:
      type: subagent
      target: kanban-reviewer

  codex-reviewer:
    role: reviewer
    backend:
      type: acp
      target: codex-main
    fallback: default-reviewer
    capabilities: [review, diff-analysis]

  default-verifier:
    role: verifier
    backend:
      type: subagent
      target: kanban-verifier
```

## Card Model Changes

Card 只增加轻量字段，用于表达执行意图：

- `agent_profile: str | None`
- `agent_profile_source: str | None`

语义：

- `agent_profile`
  卡片显式指定的 profile 名称
- `agent_profile_source`
  profile 的来源，例如：
  - `manual`
  - `planner`
  - `policy`

Card 中不存以下内容：

- ACP server command
- provider endpoint
- API key
- transport mode
- backend process 参数

理由：

- Card 是业务对象，不应与基础设施配置耦合
- 环境迁移时不能要求批量修改历史卡片
- secrets 与连接细节应留在项目/本地配置中，而不是 board source of truth 中

## Routing Rules

executor 内部负责解析本次执行使用哪个 profile。

建议优先级如下：

1. `card.agent_profile`
2. planner 推荐 profile
3. policy 自动匹配
4. role 默认 profile

解析后必须做以下校验：

- profile 是否存在
- `profile.role` 是否等于当前 `role`
- 若 `backend.type == acp`，则 `backend.target` 是否存在于 `.agentao/acp.json`
- fallback 是否存在
- fallback 的 role 是否与当前 role 一致
- fallback 是否形成环

校验失败时：

- 记录 routing failure / reroute 事件
- 回退到 role 默认 profile
- 若默认 profile 也无效，则归类为 infrastructure/config failure

## Planner Recommendation

planner 可以给出 profile 建议，但不能拥有最终决定权。

示例输出：

```json
{
  "recommended_profile": "python-worker",
  "reason": "acceptance criteria target pytest-based backend files"
}
```

执行约束：

- planner recommendation 是 hint
- executor / policy 才是最终 resolve 方
- planner 不能直接决定 backend

这样做的原因是：

- planner 本身也是 agent，不应直接控制运行时基础设施
- profile 选择属于 execution policy，不属于 workflow authoring 的唯一权威

## Runtime Flow

一次完整执行应按如下顺序进行：

1. orchestrator 根据 `Card.status` 决定当前 `role`
2. executor 根据 `role + card + profile config` resolve profile
3. executor 根据 `profile.backend.type` 选择 backend adapter
4. adapter 执行 subagent 或 ACP server
5. adapter 返回 raw text 和 backend metadata
6. executor 使用统一解析器转成 `AgentResult`
7. orchestrator 继续按现有逻辑 apply result / retry / recovery

关键边界：

- orchestrator 不感知 ACP
- backend 不拥有 workflow transition 权限
- executor 是 profile / backend 路由中心

## ACP Integration

kanban 不应自行实现新的 ACP client。
应直接复用 `agentao` 已有 ACP client 子系统。

复用范围包括：

- `.agentao/acp.json` 的配置加载
- 稳定的 embedding surface：`agentao.acp_client`
- `ACPManager.from_project(...)`
- `ACPManager.send_prompt(..., interactive=..., cwd=...)`
- `ACPManager.prompt_once(..., interactive=False, cwd=...)`
- 结构化错误：`AcpClientError` / `AcpErrorCode` / `AcpInteractionRequiredError`
- 程序化状态与日志：`get_status()` / `get_server_logs()`

建议新增 kanban adapter：

- `kanban/executors/backends/acp_backend.py`

职责：

1. 读取 `.agentao/acp.json`
2. 根据 profile 的 `backend.target` 找到对应 ACP server
3. 通过 `ACPManager.from_project(...)` 获取 manager
4. 优先调用 `ACPManager.prompt_once(...)` 完成一次独立执行
5. 在需要共享长生命周期连接时，再降级使用 `send_prompt(...)`
6. 返回 raw text 与执行元数据

不建议复用 `agentao` CLI 的 `/acp send` 交互语义。
kanban 要复用的是 ACP runtime 能力，而不是把 workflow executor 包装成 CLI 流程。

因此 v1 的默认集成方式应是：

- `ACPManager.from_project(project_root)`
- `prompt_once(name, prompt, cwd=..., interactive=False)`

而不是由 kanban 自己编排 `connect -> create_session -> prompt -> cleanup`。

## Session Strategy

ACP 是 session-based 协议，必须明确 kanban 如何使用 session。

v1 建议：

**每次执行使用 `ACPManager.prompt_once(...)` 驱动的独立 ACP session**

理由：

- 避免跨卡上下文污染
- 保持每次执行可审计、可重放
- 降低 session 生命周期管理复杂度
- 更贴合当前 kanban 一次 `run(role, card)` 的执行模型
- 与 `agentao` 已实现的 embedding API 对齐，避免在 kanban 内重复管理 session 生命周期

v1 不做：

- 跨卡 session 复用
- 跨 role session 复用
- 长期 memory 注入
- 后台 session 恢复策略

后续若确有必要，可在 Phase 2+ 评估按 `(card_id, role, backend_target)` 复用 session，但不属于当前设计范围。

## Interaction Constraints

ACP server 可能发起用户交互请求，例如：

- permission approval
- ask-user 文本输入

但 kanban daemon/worker 是自动化工作流，不适合卡在等待用户交互。

因此本设计明确：

**kanban 中的 ACP backend 运行在非交互模式。**

行为约束：

- 若 ACP server 发起交互请求
- kanban 不进入 interactive wait
- `agentao` 应返回 `AcpInteractionRequiredError`
- kanban 将其映射为独立的 `interaction-required` 失败类别
- 该类别默认不等同于 `INFRASTRUCTURE`

这意味着：

- 只有适合无人值守执行的 ACP server 才适合作为 workflow backend
- 如果某 server 频繁请求用户批准，它不适合直接挂到 worker daemon

## Prompt Ownership

prompt 组装仍由 kanban executor 负责，不下沉到 backend。

原因：

- planner / worker / reviewer / verifier 的 prompt 契约属于 workflow 语义
- backend 只负责把 prompt 发给相应执行实现
- 同一个 role 无论走 `subagent` 还是 `acp`，都应遵守相同的输出契约

因此：

- `SubagentBackend` 接受已构建好的 prompt
- `AcpBackend` 也接受已构建好的 prompt
- prompt builder 仍位于 kanban executor 中

## Response Contract

无论 backend 类型是什么，最终都必须回到同一条解析路径：

- backend 返回 raw text
- executor 用统一 JSON-fence parser 解析
- executor 转成统一 `AgentResult`

不建议让 ACP backend 直接返回 workflow-level `AgentResult`。
原因是：

- 这样会让 backend 层侵入业务语义
- 两类 backend 容易出现行为分叉
- 会增加测试复杂度和审计不一致

统一 raw-response parsing 的好处是：

- role 输出契约只维护一份
- fallback 后行为一致
- subagent 和 ACP 可共享大量测试

## Timeout Semantics

引入 ACP 后，需要协调两类超时：

- workflow timeout
- backend request timeout

workflow timeout 仍由 kanban 的 role timeout 控制。
backend request timeout 则来自 ACP server config 中的 `requestTimeoutMs`。

建议规则：

```text
effective_timeout = min(role_timeout, acp.requestTimeoutMs)
```

含义：

- kanban workflow timeout 是最终上限
- backend adapter 不能等待超过当前 role 的允许时间
- 外部 ACP config 不能反向扩大 workflow 时间边界

这样可避免出现：

- workflow 已判定超时
- ACP 请求仍长时间阻塞未收敛

## Failure Model

失败分为三类：

### Routing / Config Failure

例如：

- profile 不存在
- `backend.target` 不存在
- profile-role 不匹配
- fallback 非法

处理：

- 记录 routing failure
- 回退默认 profile
- 默认 profile 也无效时再归类 infrastructure

### Infrastructure Failure

例如：

- ACP process 启动失败
- handshake 失败
- session 创建失败
- prompt timeout
- transport 断开
- protocol error
- server busy
- subagent runtime 抛异常

处理：

- 归类为 `FailureCategory.INFRASTRUCTURE`
- 先按现有 retry policy 重试
- retry 用尽后可进行一次 fallback reroute

### Interaction-Required Failure

例如：

- ACP backend 在 `interactive=False` 下收到 permission request
- ACP backend 在 `interactive=False` 下收到 ask-user 请求

处理：

- 归类为独立的 `interaction-required` 失败类别
- 默认不按 infrastructure retry
- 默认不触发 fallback
- 应视为“该 profile/后端不适合无人值守执行”的显式信号

### Functional Failure

例如：

- worker 返回 `ok: false`
- reviewer 拒绝
- verifier 判定未满足 acceptance criteria

处理：

- 不切 profile 重跑
- 保持现有 workflow 语义
- 按现有 BLOCKED / retry 规则处理

核心原则：

**只有基础设施失败才允许 fallback。**

ACP backend 具体应优先依据 `agentao.acp_client` 的结构化错误码做映射，而不是依赖异常消息文本。建议最少覆盖：

- `SERVER_NOT_FOUND` -> routing/config failure
- `CONFIG_INVALID` -> routing/config failure
- `PROCESS_START_FAIL` -> infrastructure
- `HANDSHAKE_FAIL` -> infrastructure
- `REQUEST_TIMEOUT` -> infrastructure
- `TRANSPORT_DISCONNECT` -> infrastructure
- `PROTOCOL_ERROR` -> infrastructure
- `SERVER_BUSY` -> infrastructure
- `INTERACTION_REQUIRED` -> interaction-required

## Retry And Fallback Rules

推荐顺序：

1. 当前 profile 内先按现有 retry matrix 重试
2. retry 用尽后，如有合法 fallback，则切换 profile
3. fallback profile 执行一次
4. fallback 也失败后再按 workflow 进入 BLOCKED 或 recovery

约束：

- 单次执行链最多只允许一次 profile reroute
- fallback 不能形成环
- fallback 只能发生在同一 role 内
- functional failure 不触发 fallback

## State Consistency Invariants

为了避免引入 profile 后破坏现有 workflow，系统必须保持以下不变量：

1. workflow 状态机只依赖 `role`
2. profile 不得改变下一状态
3. profile 只能影响“由谁执行”，不能影响“执行后流向哪里”
4. backend 不得直接写 board workflow 状态
5. card source of truth 仍然是 board files，而不是 backend runtime

## Audit And Events

引入 profile 和 ACP 后，事件模型必须增强，否则无法做有效诊断。

建议新增记录字段：

- `agent_profile`
- `backend_type`
- `backend_target`
- `routing_reason`
- `fallback_from_profile`
- 可选 `session_id`
- 可选 `server_description`

现有字段仍保留：

- `role`
- `prompt_version`
- `duration_ms`
- `attempt`

建议新增事件类型：

- `execution.routed`
- `execution.rerouted`
- `execution.backend_failed`

这样后续可分析：

- 是 `worker` 角色整体质量问题
- 还是 `claude-code-main` 后端不稳定
- 或 `codex-reviewer` 的拒绝率异常高

## Compatibility

### Existing Boards

旧 board/card 文件没有 `agent_profile` 字段。
兼容策略应为：

- 缺失 `agent_profile` 时视为 `None`
- `None` 自动落到 role 默认 profile
- 无需批量迁移旧卡

### Existing Executors

若 `.agentao/acp.json` 不存在，只要 resolved profile 全是 `subagent`，系统仍应正常运行。

只有在：

- selected profile 的 backend.type 是 `acp`
- 但 `.agentao/acp.json` 缺失或 target 不存在

时，才应报 infrastructure / routing error。

这样可保持：

- 本地纯 subagent 开发可用
- CI 不强依赖 ACP 环境
- ACP 是增强能力，不是强制依赖

## Security And Trust Boundary

引入 ACP backend 后，系统的信任边界必须明确：

1. ACP server 是执行实现，不是 workflow authority
2. ACP server 的输出不能直接修改 board
3. 所有 workflow transition 仍由 kanban orchestrator 决定
4. fallback / retry / reroute 决策只能由 kanban runtime 做
5. board 仍是唯一 source of truth

这可防止未来出现“外部 agent 反向控制工作流”的失控模型。

## Suggested Module Plan

建议新增或重构以下模块：

- `kanban/agent_profiles.py`
  加载与校验 `agent_profiles.yaml`

- `kanban/executors/profile_resolver.py`
  负责 `role + card -> profile` 的解析

- `kanban/executors/backends/base.py`
  backend adapter 接口定义

- `kanban/executors/backends/subagent_backend.py`
  封装现有 role-specific subagent 执行路径

- `kanban/executors/backends/acp_backend.py`
  基于 `agentao.acp_client` 的 ACP backend

- `kanban/executors/multi_backend.py`
  顶层 executor，整合 resolver、backend adapter、parser

现有 `AgentaoMultiAgentExecutor` 可逐步演进到 `multi_backend` 模式，而不必一次性废弃。

## CLI Impact

建议新增最小 CLI 支持：

- `card edit --agent-profile <name>`
- `card edit --clear-agent-profile`
- `profiles list`
- `profiles show <name>`
- 可选 `profiles doctor`

不建议第一阶段在 kanban CLI 内复制 ACP server 运维命令，例如：

- start
- stop
- restart
- logs
- status

这些运维职责继续留给 `agentao` 自己的 ACP client / CLI。

## Testing Strategy

建议测试分为五层。

### 1. Profile Config Tests

验证：

- `agent_profiles.yaml` 正常解析
- profile-role 约束
- fallback 合法性
- backend.type 合法性

### 2. Routing Tests

验证：

- card 显式 profile 优先
- planner recommendation 生效或被忽略
- default profile 正常回退
- 非法 profile 自动降级

### 3. ACP Backend Tests

验证：

- `backend.target` 存在时能解析 server config
- target 缺失时稳定报错
- `AcpErrorCode` 到 kanban failure category 的映射稳定
- `INTERACTION_REQUIRED` 不被误归为 infrastructure
- handshake / timeout / transport error 映射为 infrastructure
- raw response 能稳定返回

### 4. Executor Integration Tests

验证：

- subagent profile 可运行
- acp profile 可运行
- fallback 到 subagent 正常
- 两类 backend 都共享同一 response parser

### 5. Workflow Tests

验证：

- orchestrator 不感知 profile
- retry matrix 对 acp backend 一样生效
- execution events 带 profile/backend 元数据

推荐做法：

- ACP backend 测试优先 mock `ACPManager` 的 embedding API，尤其是 `prompt_once(...)`
- 不让大多数测试依赖真实 ACP subprocess

## Observability

一旦进入多 backend 模式，必须具备最基本的可观测性。

建议至少统计：

- 每个 `role` 的成功率
- 每个 `agent_profile` 的成功率
- 每个 `backend_target` 的失败率
- 平均执行耗时
- fallback 触发频率
- infrastructure vs functional failure 占比

即使 v1 不做仪表盘，也应先把事件字段留好。

ACP backend 的运维与诊断优先通过 `agentao.acp_client` 的程序化 facade 获取，例如：

- `ACPManager.get_status()`
- `ACPManager.get_server_logs()`

而不是解析 CLI 输出文本。

## Operational Guidance

建议给使用方明确以下原则：

- 只有适合无人值守执行的 ACP server 才适合作为 workflow backend
- review / coding 场景更适合优先接 ACP
- 高交互依赖的 agent 不应直接挂到 daemon worker
- 新 profile 应先在少量卡上试点，再扩大范围
- verifier 若高度依赖 deterministic checks，应优先考虑 `local` backend 或保持 subagent/local 实现

## Migration Plan

建议分四期推进。

### Phase 1

- 增加 `agent_profile`
- 增加 `agent_profiles.yaml`
- 支持 `subagent` + `acp`
- 只支持显式 profile 与 role 默认 profile
- ACP session 采用一次执行一 session

### Phase 2

- 增加 infrastructure fallback
- 增加 routing / reroute / backend_failed 事件

### Phase 3

- planner recommendation
- executor 可选择采纳

### Phase 4

- policy 自动匹配
- profile 统计与效果优化
- 评估 session 复用是否值得引入

## Recommended Initial Rollout

建议第一批只上线两个明确场景：

- `worker -> claude-code-worker (acp)`
- `reviewer -> codex-reviewer (acp)`

其余保持默认 subagent：

- `planner -> default-planner`
- `verifier -> default-verifier`

原因：

- worker / reviewer 更容易从专门 agent 中获益
- planner / verifier 通常更依赖稳定契约与确定性
- 初期将 ACP 使用面压小，更容易排查问题

## Final Recommendation

建议采用本设计。

最终原则如下：

- 保持固定 workflow role，不把 provider 或 skill 并入 `AgentRole`
- 新增 `agent_profile` 作为 role 下的实现路由层
- ACP server 配置直接复用 `agentao` 的 `.agentao/acp.json`
- kanban 复用 `agentao.acp_client` 作为 ACP backend 基础设施层
- kanban 自己只新增 profile/routing 配置与 backend adapter
- backend 统一返回 raw text，由 kanban executor 统一解析为 `AgentResult`
- fallback 仅用于基础设施失败，不用于功能性失败

这个方案在不破坏 orchestrator 与 workflow 语义的前提下，以最小侵入方式支持：

- 同一 role 的多实现执行
- ACP 与 Subagent 混合运行
- 基础设施级回退
- 细粒度审计与后续优化
