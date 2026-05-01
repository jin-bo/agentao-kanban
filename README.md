# kanban

**当前版本:v0.1.5**。完整变更见 [CHANGELOG.md](CHANGELOG.md)。

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
  defaults/                 # 打包的 sub-agent 定义与默认 agent_profiles.yaml
docs/
  multi-agent-implementation-plan.md
main.py                     # demo 入口
```

## 安装

按你想跑哪个 executor 挑一条路径。**不要混装**:ACP 路径依赖外部 CLI 和
API key,而纯 mock 路径什么都不需要。

### 先决条件(所有模式共用)

- Python ≥ 3.12
- [`uv`](https://docs.astral.sh/uv/) (包管理)
- Git clone 后在仓库根目录 `uv sync` 一次

```bash
git clone https://github.com/jin-bo/agentao-kanban.git
cd agentao-kanban
uv sync
```

> `agentao` 已发布到 PyPI(≥ 0.3.0),`uv sync` 默认直接从 PyPI 拉,
> 不需要同级 `../agentao` 源码。只有同时在改 agentao 的开发者才需要在
> 当前虚拟环境里手动覆盖为本地 editable:
>
> ```bash
> uv sync
> uv pip install --editable ../agentao
> ```
>
> `uv` 0.11.x 不再接受把项目依赖 `sources` 写进 `uv.toml`。上面的做法只
> 覆盖当前 `.venv`,不会改仓库里的 `pyproject.toml`/`uv.lock`。

### 路径 A:纯 mock 模式(默认,推荐首次上手)

`MockAgentaoExecutor` 是离线状态机,不调用任何 LLM,也不读 `.agentao/`。
装完 `uv sync` 即可跑:

```bash
uv run python main.py                           # 内存态演示,最短闭环
uv run kanban card add --title T --goal G --priority HIGH
uv run kanban list
uv run kanban run                               # 跑到 idle
uv run kanban daemon                            # 前台 dispatcher,Ctrl-C 退出
uv run kanban daemon --once --poll-interval 0.2 # 单轮调试
uv run kanban daemon --detach                   # 后台,日志写 <board>/daemon.log
```

到这一步就完了,不要继续装下面的东西。

### 路径 B:agentao sub-agent 模式

用 `--executor agentao` 调四个本地 sub-agent(planner / worker / reviewer /
verifier)。额外要求:

1. `agentao` 包可用。`uv sync` 已经从 PyPI 装好(≥ 0.3.0);只有同时在改
   agentao 源码的开发者才需要按先决条件里的说明再执行一次
   `uv pip install --editable ../agentao` 覆盖当前 `.venv`。
2. sub-agent 定义:默认从 `kanban/defaults/*.md` 读,仓库已打包。若要本地
   改 prompt:

   ```bash
   mkdir -p .agentao/agents
   cp kanban/defaults/*.md .agentao/agents/
   ```

   `.agentao/` 已 `.gitignore`,不会被提交。

3. 跑:

   ```bash
   uv run kanban --executor agentao run
   uv run kanban --executor agentao daemon
   ```

### 路径 C:multi-backend + ACP 远端模式

`--executor multi-backend` 走 profile 路由,默认每个角色仍落到 `subagent`
backend(等同路径 B)。**只有**把卡上 `agent_profile` 手动 pin 到 `gemini-*`
profile,或让 router 选中,才会真正调 ACP 后端。此时额外要求:

1. 准备 ACP 服务器配置。仓库的 `docs/acp.sample.json` 是样板,拷到运行
   目录:

   ```bash
   mkdir -p .agentao
   cp docs/acp.sample.json .agentao/acp.json
   ```

   默认样板里的 `gemini-worker` / `gemini-reviewer` 都是 `npx
   @google/gemini-cli@latest --acp`。

2. 准备外部 CLI 与凭据:

   - Node.js + `npx` 可用(让 `npx @google/gemini-cli@latest` 可跑)。
   - 导出 `GEMINI_API_KEY`(`acp.json` 里的 `env` 用 `$GEMINI_API_KEY`
     占位符,进程启动时展开)。

   ```bash
   export GEMINI_API_KEY=...
   npx @google/gemini-cli@latest --version   # 自检
   ```

3. (可选)调整 profile 路由。默认配置 `kanban/defaults/agent_profiles.yaml`
   已经声明了 `gemini-worker` / `gemini-reviewer` profile 但**不**作为默认;
   要按项目改,复制一份本地覆盖:

   ```bash
   mkdir -p .kanban
   cp docs/agent_profiles.sample.yaml .kanban/agent_profiles.yaml
   ```

4. 跑:

   ```bash
   uv run kanban --executor multi-backend daemon
   ```

   若一张卡被路由到 ACP profile 但 `.agentao/acp.json` 缺失 / 外部命令
   起不来,该卡会以 `FailureCategory=infrastructure` 进入重试/BLOCKED;
   其它仍走 subagent 的卡不受影响。

### 自检

装完推荐跑一次:

```bash
uv run pytest -q                                # 单元+集成测试
uv run kanban doctor                            # 板健康体检
```

## 每卡 Worktree 隔离(默认 auto)

当 board 位于 Git 仓库内时,worker 首次 claim 一张卡会自动创建独立
worktree(`workspace/worktrees/<card-id>`)和分支(`kanban/<card-id>`)。
worker / reviewer / verifier 都在这个 worktree 中执行,彼此不干扰。卡
到 DONE 或 BLOCKED 时自动 detach(目录释放、分支保留),分支等你合 PR
或手动 unblock。

`--worktree` 是 tri-state:

```bash
uv run kanban run                 # auto(git repo 内默认启用)
uv run kanban --worktree run      # 硬要求 git repo,否则 SystemExit
uv run kanban --no-worktree run   # 显式关闭,行为回到 v0.1.3
```

非 git 仓库下默认会在 stderr 打印一行 warning 后自动降级关闭,不影响
mock / 临时目录场景。

子命令:

```bash
uv run kanban worktree list                  # 列出活跃 worktree
uv run kanban worktree diff <card-id>        # 基于持久化的 fork-point 出 diff
uv run kanban worktree prune                 # 清 merged/过期分支
uv run kanban worktree prune --retention-days 14
```

完整设计见 [`docs/worktree-isolation-design.md`](docs/worktree-isolation-design.md)。

## 作为 MCP Server 暴露(`kanban-mcp`)

把看板当成 Claude Code / Codex / 任意 MCP client 的"调度中心":

```bash
# Claude Code:在仓库根注册一个 stdio MCP server,作用域是当前项目
claude mcp add kanban -- uv run --directory $(pwd) kanban-mcp \
    --board workspace/board

# Codex 或其它 client 同理,改前缀即可:
#   command = "uv", args = ["run", "--directory", "<repo>", "kanban-mcp", "--board", "workspace/board"]
```

启动参数:

| 参数 | 说明 |
|---|---|
| `--board DIR` | 板路径,默认 `$KANBAN_BOARD` 或 `workspace/board` |
| `--executor {mock,agentao,multi-backend}` | `tick`/`run` 用哪个执行器,默认 `mock` |
| `--force` | 跳过 daemon-lock 守卫(仅在 daemon 异常残留锁时使用) |

暴露的 tools(均直接调 `BoardStore`,不走 subprocess):

| Tool | 等价 CLI |
|---|---|
| `card_add(title, goal, priority?, acceptance?, depends?)` | `kanban card add` |
| `card_list(status?)` | `kanban list` |
| `card_show(card_id)` | `kanban show` |
| `card_move(card_id, status)` | `kanban move` |
| `card_block(card_id, reason)` | `kanban block` |
| `card_unblock(card_id, to?)` | `kanban unblock` |
| `events_tail(limit?, card_id?, role?, execution_only?)` | `kanban events` |
| `tick()` | `kanban tick` |
| `run(max_steps?)` | `kanban run` |

Resources(供 client 通过资源接口缓存读取):

- `kanban://board/snapshot` — 当前 `{status: [titles]}` 快照
- `kanban://card/{card_id}` — 单卡完整 JSON
- `kanban://events/recent` — 最近 50 条事件 JSON 数组

写类 tool 在 daemon 持有 `.daemon.lock` 时会拒绝,文案与 CLI `--force`
路径一致;读类 tool / resource 不受守卫影响。

## 只读 Web 看板(`kanban web`)

本地/内网实时观察板面的只读 HTTP 服务,不做写入、不抢 `.daemon.lock`,
与 CLI / MCP / daemon 并行运行。基于 FastAPI + uvicorn,前端原生 JS
按固定间隔轮询 `/api/board`。

```bash
uv run kanban --board workspace/board web            # 默认 127.0.0.1:8000
uv run kanban --board workspace/board web \
    --host 0.0.0.0 --port 8080 --poll-interval-ms 3000
```

| 端点 | 说明 |
|---|---|
| `GET /` | 单页看板(7 列 + 事件尾 + runtime 面板) |
| `GET /healthz` | `{status, board_dir, poll_interval_ms}` |
| `GET /api/board` | 列聚合 + 最近 20 条事件 + claims/workers |
| `GET /api/cards/{id}` | 单卡完整 JSON + 最近 20 条本卡事件;未知卡 404 |
| `GET /api/events?limit&card_id&role&execution_only` | 事件尾过滤 |
| `GET /static/*` | 前端资源(`app.js` / `styles.css` / `index.html`) |

所有 handler 每次请求都新建一份 `MarkdownBoardStore`,因此 daemon 或
CLI 的 out-of-band 写入在下一次轮询就可见。`runtime/` 目录缺失时
(板从未被 daemon 接管过),`claims` / `workers` 返回空列表。首版
不附带鉴权、缓存、SSE/WebSocket,`--host 0.0.0.0` 是显式选择;敏感
环境请自行加反代或防火墙。

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

- `kanban/defaults/kanban-planner.md`
- `kanban/defaults/kanban-worker.md`
- `kanban/defaults/kanban-reviewer.md`
- `kanban/defaults/kanban-verifier.md`

frontmatter 里的 `version:` 字段会被执行器读出,写入 `events.log` 每一条
执行事件,便于回溯是哪个版本的 prompt 产出了哪次结果。

`.agentao/` 被视为本地运行目录(缓存、数据库等),整个目录已 `.gitignore`
不提交。运行时加载规则:

- 若 `<cwd>/.agentao/agents/*.md` 存在,优先使用(供开发者本地覆盖)
- 否则回退到仓库内的 `kanban/defaults/*.md`

要按 agentao 的项目目录习惯管理提示词,可先复制一份本地覆盖:

```bash
mkdir -p .agentao/agents
cp kanban/defaults/*.md .agentao/agents/
```

## v0.1.2 并发内核

`v0.1.2` 把单进程串行 dispatcher 拆成 scheduler / worker 两种角色,允许多
worker 在同一 board 上并发执行不同卡,同时保留「只有调度器写状态」这条
核心不变式。完整设计:`docs/v0.1.2-concurrency-plan.md`。

### Daemon 角色

```bash
uv run kanban daemon                              # = --role all --max-claims 2(1 scheduler + 2 workers 同进程真并行)
uv run kanban daemon --role all --max-claims 4    # 1 scheduler + 4 worker 线程,推荐的本地并行入口
uv run kanban daemon --role scheduler --max-claims 4
uv run kanban daemon --role worker --worker-id worker-1
uv run kanban daemon --role legacy-serial         # v0.1.1 的串行路径,兼容用
```

- `scheduler` 持 `.daemon.lock`,负责建 claim、commit 结果、回收过期 lease;
  全板只能有一个。
- `worker` 不持板锁,可开多个进程/机器;通过 `runtime/claims/<card>.json`
  的原子 `O_EXCL` sentinel CAS 互斥地抢 claim,执行后只写
  `runtime/results/<card>-<attempt>.json` envelope,**不再直接改卡状态**。
- `all` 现在是**单进程真并行**:在同一个进程里启动 1 个 scheduler 线程 +
  N 个 worker 线程,N = `--max-claims`。每个 worker 拥有自己的 store /
  executor,不共享 cwd / router 缓存。`--max-claims` 同时决定调度器 claim
  预算和 `all` 模式 worker 数(保持 CLI 简单)。适合本地跑 `--max-claims 4`
  级别的并发;跨进程/跨机器并行继续用独立的 `scheduler` + `worker`。
- `--worker-id` 在 `--role all` 下作为**前缀**,派生 `<prefix>-1 ..
  <prefix>-N`;不传则随机生成一个基前缀。
- `--once` 在 `--role all` 下 = 1 次 scheduler pass(commit / recover /
  create claims) + 每个 worker 1 次 acquire-execute-submit,然后全部线程
  退出。
- `--detach` 必须先 fork,再启线程;`cmd_daemon` 已经保证了这个顺序。

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

- ACP 远端 worker、跨机部署(**v0.1.3 已落地,见下节**)
- 持久队列(替代 `runtime/` 文件集),事件流化
- 观测面板、重试链时间线、资源占用曲线

## v0.1.3 Multi-backend 路由 & ACP

`v0.1.3` 在 v0.1.2 的 scheduler/worker 内核之上加了**按角色按卡选 backend**
的路由层,并把 ACP 远端 worker 落地为一等 backend。

- `--executor multi-backend`:按 `agent_profiles.yaml` 给每个角色挑 profile,
  profile 绑定 `subagent` 或 `acp` backend。默认配置见
  `kanban/defaults/agent_profiles.yaml`,本地覆盖放
  `<cwd>/.kanban/agent_profiles.yaml`。
- ACP backend:外部 CLI (如 `npx @google/gemini-cli@latest --acp`) 通过
  `.agentao/acp.json` 声明;样板见 `docs/acp.sample.json`。API key 从环境
  变量注入,配置文件只放命令与占位符。
- Router agent (`kanban-router`):可按卡动态在候选 profile 间挑选,不会
  覆盖卡 pin 或 planner 推荐;`KANBAN_ROUTER=off` 全局关闭。
- 所有 profile/ACP/router 路径都从 `--board` 推导的 project root 读取,
  不会跟随 shell cwd。

安装步骤(含 `.agentao/acp.json`、`GEMINI_API_KEY` 等前置条件)见本 README
开头的「路径 C」。设计细节:`docs/agent-router-design.md`、
`docs/agent-profile-acp-design.md`。
