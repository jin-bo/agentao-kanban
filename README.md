# kanban

**当前版本:v0.1.7-dev**。完整变更见 [CHANGELOG.md](CHANGELOG.md)。

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
    multi_backend.py        # profile 路由 + subagent / acp 后端
  cli.py                    # kanban CLI(含 daemon 子命令)
  web.py                    # FastAPI 看板 web UI
  mcp.py                    # stdio MCP server(kanban-mcp)
  defaults/                 # 打包的 sub-agent 定义与默认 agent_profiles.yaml
docs/                       # 设计文档与运维指南(详见各分节链接)
main.py                     # demo 入口
```

## 快速开始

不想 clone,只想一分钟看效果(需 `uv ≥ 0.4`):

```bash
mkdir my-kanban && cd my-kanban
uvx --from agentao-kanban kanban init --demo    # 一键脚手架 + 4 张示例卡
uvx --from agentao-kanban kanban demo           # 跑到 idle,全部进 DONE
uvx --from agentao-kanban kanban web            # 浏览器看板(只读)
```

(命令也可写成 `pipx run --spec agentao-kanban kanban ...`,只是用 pipx 时
要带 `--spec`。)

想本地开发或改 kanban 自身:

```bash
git clone https://github.com/jin-bo/agentao-kanban.git
cd agentao-kanban
uv sync
uv run kanban init --demo                       # 同上,但落在仓库根
uv run kanban list
uv run pytest -q                                # 自检
```

可选:打开 shell 补全(zsh/bash):

```bash
eval "$(register-python-argcomplete kanban)"
```

要切到真实 agentao sub-agent 或远端 ACP backend,完整安装路径见
[`docs/install.md`](docs/install.md)。

## 一分钟上手三连

| 子命令 | 做什么 |
|---|---|
| `kanban init [PATH] [--demo] [--copy-agents]` | 在当前目录建 `.kanban/`(项目根标记)+ `workspace/board/`,可选拷贝 agent 模板与种入示例卡 |
| `kanban demo [--no-run]` | 把 4 张示例卡推到 INBOX→READY→DOING→...→DONE,看完整管线 |
| `kanban` (无参) | 打印版本、当前板、daemon 状态与 5 条最常用命令 |

`kanban init` 会写一个 `.kanban/config.yaml` 把 `board_dir` 钉死;之后从该
目录及其任何子目录运行 `kanban list` / `kanban web` / `kanban daemon` 都
能自动找回这块板,无需带 `--board`。

## CLI 与日常运维

CLI 完整使用指南放在 [`docs/kanban-cli-guide.md`](docs/kanban-cli-guide.md),
覆盖:建卡、补 context、维护 acceptance criteria、daemon 运行时安全操作、
多 worker 模式下查看 claims / workers / events、blocked / 超时 / stale
claim 的恢复 playbook,以及 web 看板写入运维。

最常用的几条:

```bash
uv run kanban card add --title T --goal G       # 新建
uv run kanban list                              # 全板
uv run kanban show <card_id>                    # 单卡
uv run kanban move <card_id> ready              # 入队
uv run kanban daemon                            # 前台 dispatcher
uv run kanban daemon status                     # daemon 三态(running/stale/stopped)
uv run kanban daemon stop                       # SIGTERM 给锁文件里记录的 pid
uv run kanban daemon logs -f                    # 跟踪 <board>/daemon.log
uv run kanban events <card_id>                  # 事件流
uv run kanban doctor [--fix]                    # 板 + 环境体检;--fix 修复 stale lock / 缺失 board 等
uv run kanban card acceptance edit <card_id>    # $EDITOR 里改 acceptance
uv run kanban mcp install --client claude       # 一键拿到 claude mcp add 命令
```

所有写命令都遵守 `.daemon.lock`;加 `--force` 才能在守护进程运行期写入(仅
应急)。daemon 启动时会自动清理已死进程留下的 stale lock。

## 每卡 Worktree 隔离

当 board 位于 Git 仓库内时,worker 首次 claim 一张卡会自动创建独立
worktree(`workspace/worktrees/<card-id>`)和分支(`kanban/<card-id>`)。
worker / reviewer / verifier 都在这个 worktree 中执行,彼此不干扰。卡到
DONE 或 BLOCKED 时自动 detach(目录释放、分支保留)。

```bash
uv run kanban run                 # auto(git repo 内默认启用)
uv run kanban --no-worktree run   # 显式关闭,行为回到 v0.1.3
uv run kanban worktree list
uv run kanban worktree diff <card-id>
uv run kanban worktree prune --retention-days 14
```

**Artifact 抢救(v0.1.6)**:`detach()` 在 `git worktree remove` 之前会把
worktree 内被 gitignore 的交付物(常见 `workspace/reports/...`)快照到
`workspace/raw/<card-id>/artifacts-<ts>/`,避免随 worktree 删掉。默认每张
卡保留 5 份、500 MiB 上限,环境变量 `KANBAN_ARTIFACTS_MAX_BYTES` 可调。
`node_modules/`、`__pycache__/`、`.venv/`、`dist/`、`build/` 等构建/缓存路径
在计帐前先剔除。

完整设计见 [`docs/worktree-isolation-design.md`](docs/worktree-isolation-design.md)。

## MCP Server(`kanban-mcp`)

把看板当成 Claude Code / Codex / 任意 MCP client 的"调度中心":

```bash
claude mcp add kanban -- uv run --directory $(pwd) kanban-mcp \
    --board workspace/board
```

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

Resources:`kanban://board/snapshot`、`kanban://card/{card_id}`、
`kanban://events/recent`。写类 tool 在 daemon 持锁时按 CLI 一致语义拒写。

设计细节:[`docs/mcp-server-design.md`](docs/mcp-server-design.md)。

## Web 看板(`kanban web`)

与 daemon / CLI / MCP 并行的 HTTP 观察面,**默认只读**;v0.1.6 起新增可选
写入入口。基于 FastAPI + uvicorn,前端原生 JS 按固定间隔轮询 `/api/board`。

```bash
uv run kanban --board workspace/board web                       # 只读
uv run kanban --board workspace/board web --enable-writes       # +POST /api/cards
uv run kanban --board workspace/board web \
    --host 0.0.0.0 --enable-writes --allow-remote-writes        # 远程绑定+写入
```

非 loopback bind 同时启用写入需要显式 `--allow-remote-writes`,否则 server
启动直接拒绝。Runtime 面板顶部展示 daemon 三态(running / stopped / stale
lock)+ pid + 启动时间,数据来自 `GET /api/daemon`。

启动模式、写入面与 `.daemon.lock` 的串行化关系、artifact 在 web 上的可见性
等运维细节,见 [`docs/kanban-cli-guide.md`](docs/kanban-cli-guide.md) §9。

## 并发内核(v0.1.2)

v0.1.2 把单进程串行 dispatcher 拆成 scheduler / worker 两种角色,允许多
worker 在同一 board 上并发执行不同卡,同时保留「只有调度器写状态」这条
核心不变式。

```bash
uv run kanban daemon                              # = --role all --max-claims 2
uv run kanban daemon --role all --max-claims 4    # 单进程 1 scheduler + 4 worker 线程
uv run kanban daemon --role scheduler --max-claims 4
uv run kanban daemon --role worker --worker-id worker-1
```

- `scheduler` 持 `.daemon.lock`,负责建 claim、commit 结果、回收过期 lease;
  全板只能有一个。
- `worker` 不持板锁,通过 `runtime/claims/<card>.json` 的原子 `O_EXCL`
  sentinel CAS 抢 claim,执行后写 `runtime/results/<card>-<attempt>.json`
  envelope,**不直接改卡状态**。
- 失败按 `FailureCategory` 分类(`infrastructure` / `lease_expiry` /
  `timeout` / `malformed` / `functional`),命中重试矩阵就重试,耗尽预算
  就 BLOCKED。

观测命令(`kanban claims` / `workers` / `recover --stale`)与重试矩阵详
见 [`docs/v0.1.2-concurrency-plan.md`](docs/v0.1.2-concurrency-plan.md)
和 [`docs/kanban-cli-guide.md`](docs/kanban-cli-guide.md) §10.4。

## Multi-backend 路由 & ACP(v0.1.3)

`--executor multi-backend` 在 v0.1.2 内核之上加了**按角色按卡选 backend**
的路由层:

- 每个角色按 `agent_profiles.yaml` 挑 profile,profile 绑定 `subagent` 或
  `acp` backend。本地覆盖放 `<cwd>/.kanban/agent_profiles.yaml`。
- ACP backend 通过 `.agentao/acp.json` 声明外部 CLI(样板见
  [`docs/acp.sample.json`](docs/acp.sample.json));API key 走环境变量,
  配置文件只放命令与占位符。
- Router agent (`kanban-router`) 可按卡动态在候选 profile 间挑选,不会
  覆盖卡 pin 或 planner 推荐;`KANBAN_ROUTER=off` 全局关闭。

安装步骤见 [`docs/install.md`](docs/install.md) 路径 C。设计细节:
[`docs/agent-router-design.md`](docs/agent-router-design.md)、
[`docs/agent-profile-acp-design.md`](docs/agent-profile-acp-design.md)。

## Workspace 目录约定

`workspace/` 已加入 `.gitignore`,是 kanban 运行态与 Agent 工作文件的统一家:

```text
workspace/
  board/              # 看板权威状态,orchestrator 独占写入,Agent 只读
  raw/<card>/<role>-<ts>.md           # kanban 管理的原始 transcript,保留最近 5 份
  raw/<card>/artifacts-<ts>/          # detach 时抢救的 gitignored 产出物(v0.1.6)
  scratch/<card>/     # Worker 的逐卡草稿目录
  reports/            # Worker 输出的人类可读交付物(Markdown)
  data/               # 长期数据集
  docs/               # 产出的稳定文档
  scripts/            # 值得留存的脚本
```

Agent 约定:

- `workspace/board/` 与 `workspace/raw/` 对所有角色只读
- Worker 草稿放 `workspace/scratch/<card-id>/`,正式交付物按 `reports/`、
  `data/`、`scripts/` 等目录分类;最终 `output` 里写明交付路径
- Reviewer 全程只读;Verifier 只允许在 `workspace/scratch/<card-id>/verify/`
  建临时脚手架,并在返回前清理
- Planner 写 acceptance criteria 时尽量点名 `workspace/` 下的具体路径,
  Verifier 才能机械化地验证

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
