# kanban

一个最小可跑的多 Agent 看板,已经实现:

- 卡片与列状态建模、依赖与 WIP 规则
- 编排器按看板规则选择下一张卡(单一写入权威)
- 按角色路由的多 Agent 执行器(planner / worker / reviewer / verifier)
- 本地 dispatcher daemon + 单写锁 + 优雅退出
- 结构化执行事件与原始 transcript 留痕

`kanban` 是 `agentao` 项目的衍生项目,复用了 `agentao` 的多 Agent 定义方式
(Markdown + YAML frontmatter),并在其基础上补上看板状态流转、调度与持久化。

执行器默认是 `MockAgentaoExecutor`(离线状态机,适合 CI 与本地调试)。加
`--executor agentao` 才会真正调用四个角色的 sub-agent;daemon 启动时会在
INFO 日志里打印当前使用哪个 executor。

## 结构

```text
kanban/
  models.py                 # 卡片、状态、角色、AgentResult
  store.py                  # BoardStore Protocol + InMemoryBoardStore
  store_markdown.py         # MarkdownBoardStore(TOML front-matter + events.log + raw transcripts)
  orchestrator.py           # 调度与状态流转(唯一状态写入者)
  agents.py                 # ROLE_AGENTS 映射 + 定义文件加载器
  daemon.py                 # 本地 dispatcher + .daemon.lock
  executors/
    base.py                 # 执行器接口
    mock_agentao.py         # 离线状态机
    agentao_multi.py        # 按角色路由的 agentao 多 Agent 执行器
  cli.py                    # kanban CLI(含 daemon 子命令)
  demo.py                   # 最小演示
docs/
  agent-definitions/        # 四个角色的 sub-agent 定义(仓库跟踪,模板源)
  multi-agent-implementation-plan.md
main.py                     # demo 入口
```

## 运行

```bash
uv run python main.py                           # 内存态演示
uv run kanban card add --title T --goal G --priority HIGH
uv run kanban list
uv run kanban run                               # 单次跑到 idle
uv run kanban daemon                            # 前台 dispatcher(Ctrl-C 优雅退出)
uv run kanban daemon --once --poll-interval 0.2 # 单轮,方便调试
uv run kanban daemon --detach                   # 后台运行,日志写到 <board>/daemon.log
uv run kanban --executor agentao daemon         # 真正调用 sub-agent
```

## CLI Guide

README 保留概览和关键命令,完整的 CLI 使用指南放在
[docs/kanban-cli-guide.md](docs/kanban-cli-guide.md)。

如果你想快速掌握:

- 建卡、补 context、维护 acceptance criteria
- daemon 运行时如何安全操作
- 多 worker 模式下如何看 claims / workers / events
- blocked、超时、stale claim 时怎么恢复

直接看这份指南。

## 运维命令(v0.1.1)

所有写命令都遵守 `.daemon.lock`;加 `--force` 才能在守护进程运行期写入
(仅用于应急恢复)。CLI 写入的 history 条目统一带 `[system]` 前缀,与
自动状态迁移保持一致。

### 编辑卡片

```bash
uv run kanban card edit <id> --title "新标题"
uv run kanban card edit <id> --goal "新目标" --priority HIGH
uv run kanban card edit <id> --set-status blocked --blocked-reason "缺数据"
uv run kanban card edit <id> --clear-blocked-reason
```

`--set-status` 是操作员覆盖,仅允许 `inbox/ready/blocked/done`(有 owner
期望的 `doing/review/verify` 不可直接设,用 `requeue`)。设为 `blocked`
时必须同时提供 `--blocked-reason`。

### 管理 context_refs

```bash
uv run kanban card context list <id>
uv run kanban card context add <id> --path docs/api.md --kind required --note "API 合约"
uv run kanban card context rm  <id> --path docs/api.md
```

`--kind` 只接受 `required` 或 `optional`。同一 `path` 再次 `add` 走
upsert。

### 调整 acceptance criteria

```bash
uv run kanban card acceptance list  <id>
uv run kanban card acceptance add   <id> --item "生成 workspace/reports/summary.md"
uv run kanban card acceptance rm    <id> --index 2   # 1-based
uv run kanban card acceptance clear <id>
```

### 从 BLOCKED 恢复

```bash
uv run kanban requeue <id>                      # 默认 → inbox,清空 blocked_reason
uv run kanban requeue <id> --to ready
uv run kanban requeue <id> --to ready --note "已补充数据集"
```

### 查看事件与 transcript

```bash
uv run kanban events                            # 最近 50 条(跨卡)
uv run kanban events <id>                       # 过滤到一张卡
uv run kanban events <id> --role worker         # 只看执行事件(排除系统事件)
uv run kanban events --limit 20 --json          # 机读 JSONL

uv run kanban traces <id>                       # 列出保留的原始 transcript
uv run kanban traces <id> --role worker
uv run kanban traces <id> --latest
```

`--role` 过滤期间,系统事件(状态迁移、手工编辑等)不会出现——要同时看
就不要加 `--role`。

### 健康体检

```bash
uv run kanban doctor                            # 人类可读报表
uv run kanban doctor --json                     # 机读报表
```

退出码:`0` = 健康,`1` = 只有 warning,`2` = 至少一条 error。

JSON 输出字段冻结:`checks[].{severity, rule, card_id, message}`。当前
规则:

- `dep-missing` (error) — `depends_on` 指向不存在的卡
- `blocked-no-reason` (warning) — 卡在 `blocked` 但 `blocked_reason` 为空(防御性)
- `done-no-verification` (warning) — `done` 卡 `outputs.verification` 为空
- `stage-missing-upstream` (error) — `review`/`verify` 卡缺上游产出
- `invalid-context-kind` (warning) — `context_refs[].kind` 不在 `{required, optional}`
- `unparseable-card` (error) — 卡文件加载时被跳过

CLI 默认把状态持久化到 `workspace/board/`(每张卡一个 `.md` 文件,事件写
JSONL 到 `events.log`)。用 `--board DIR` 可切换目录。daemon 运行时持有
`workspace/board/.daemon.lock`;其他写命令会被拒绝,加 `--force` 才强制
(仅用于应急恢复)。陈旧锁(pid 已死)启动时自动清理。

## Workspace 目录约定

`workspace/` 已加入 `.gitignore`,是 kanban 运行态与 Agent 工作文件的统一家:

```text
workspace/
  board/              # 看板权威状态,orchestrator 独占写入,Agent 只读
  raw/<card>/<role>-<ts>.md   # kanban 管理的原始 transcript,保留最近 5 份
  scratch/<card>/     # Worker 的逐卡草稿目录
  reports/            # Worker 输出的人类可读交付物(Markdown)
  data/               # 长期数据集
  docs/               # 产出的稳定文档
  scripts/            # 值得留存的脚本
  Downloads/          # 外部资源
```

Agent 约定:

- `workspace/board/` 与 `workspace/raw/` 对所有角色只读。
- Worker 草稿放 `workspace/scratch/<card-id>/`,正式交付物按 `reports/`、
  `data/`、`scripts/` 等目录分类;最终 `output` 里写明交付路径。
- Reviewer 全程只读;Verifier 只允许在 `workspace/scratch/<card-id>/verify/`
  建临时脚手架,并在返回前清理。
- Planner 写 acceptance criteria 时尽量点名 `workspace/` 下的具体路径,
  Verifier 才能机械化地验证。

## Agent 定义与 `.agentao`

仓库跟踪四份 sub-agent 定义作为模板源(Markdown + YAML frontmatter):

- `docs/agent-definitions/kanban-planner.md`
- `docs/agent-definitions/kanban-worker.md`
- `docs/agent-definitions/kanban-reviewer.md`
- `docs/agent-definitions/kanban-verifier.md`

frontmatter 里的 `version:` 字段会被执行器读出,写入 `events.log` 每一条
执行事件,便于回溯是哪个版本的 prompt 产出了哪次结果。

`.agentao/` 被视为本地运行目录(缓存、数据库等),整个目录已 `.gitignore`
不提交。运行时加载规则:

- 若 `<cwd>/.agentao/agents/*.md` 存在,优先使用(供开发者本地覆盖)
- 否则回退到仓库内的 `docs/agent-definitions/*.md`

要按 agentao 的项目目录习惯管理提示词,可先复制一份本地覆盖:

```bash
mkdir -p .agentao/agents
cp docs/agent-definitions/*.md .agentao/agents/
```

## v0.1.2 并发内核

`v0.1.2` 把单进程串行 dispatcher 拆成 scheduler / worker 两种角色,允许多
worker 在同一 board 上并发执行不同卡,同时保留「只有调度器写状态」这条
核心不变式。完整设计:`docs/v0.1.2-concurrency-plan.md`。

### Daemon 角色

```bash
uv run kanban daemon                              # = --role all(scheduler + 1 worker 同进程)
uv run kanban daemon --role scheduler --max-claims 4
uv run kanban daemon --role worker --worker-id worker-1
uv run kanban daemon --role legacy-serial         # v0.1.1 的串行路径,兼容用
```

- `scheduler` 持 `.daemon.lock`,负责建 claim、commit 结果、回收过期 lease;
  全板只能有一个。
- `worker` 不持板锁,可开多个进程/机器;通过 `runtime/claims/<card>.json`
  的原子 `O_EXCL` sentinel CAS 互斥地抢 claim,执行后只写
  `runtime/results/<card>-<attempt>.json` envelope,**不再直接改卡状态**。
- `all` 是本地便利模式(同进程里跑一个 scheduler + 一个 worker),真并行
  请跑独立进程。

### 运行时布局

```text
workspace/board/
  cards/                          # 看板权威(不变)
  events.log                      # 追加 runtime 生命周期事件
  runtime/
    claims/<card>.json            # 单个 card 同时最多一条活 claim
    results/<card>-<attempt>.json # worker 提交的 envelope,等调度器 commit
    results/orphans/              # claim_id 不匹配的孤儿 envelope(保留审计)
    workers/<worker-id>.json      # worker 心跳存在文件
```

### 重试矩阵

失败按 `FailureCategory` 分类(worker / scheduler 在 envelope 上打标),
命中矩阵就重试,耗尽预算就 BLOCKED(原因带 `[category=... attempts=...]`):

| 类别 | 默认预算 | 语义 |
|---|---|---|
| `infrastructure` | 2 | executor 抛异常 / LLM 5xx |
| `lease_expiry` | 1 | scheduler 检到租约过期 |
| `timeout` | 1 | 同上(lease 即 timeout) |
| `malformed` | 0 | 响应无法解析,立即 BLOCKED |
| `functional` | 0 | reviewer/verifier 业务驳回,立即 BLOCKED |

重试时新建 claim(attempt+1 + `retry_of_claim_id` 指回上次),
`execution.retried` 事件带整条链路。改默认:传
`KanbanOrchestrator(retry_policy=RetryPolicy(infrastructure=0))`。

### 运行时观测 CLI

```bash
uv run kanban claims                     # 所有活 claim: lease 剩余 / 心跳年龄 / 归属 worker
uv run kanban claims <card_id>           # 单卡过滤
uv run kanban claims --json              # 机读
uv run kanban workers                    # 活跃 worker: pid / uptime / 最近心跳
uv run kanban recover --stale            # 一次性跑过期 claim 恢复,输出每卡处置(retried / blocked)
uv run kanban events <id>                # 事件行按 event_type 分组打标([execution.retried] 等)
uv run kanban events <id> --json         # 带 claim_id / worker_id / failure_category / retry_of_claim_id 等字段
```

`claims` 里 `*EXPIRED*` 表示租约已过但 scheduler 还没跑到恢复步(下一次
`daemon --role scheduler` 或 `recover --stale` 就会处理)。

### 下一步 (Phase 2)

- ACP 远端 worker、跨机部署
- 持久队列(替代 `runtime/` 文件集),事件流化
- 观测面板、重试链时间线、资源占用曲线
