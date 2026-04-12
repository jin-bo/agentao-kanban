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

## 下一步

- 每卡锁(`workspace/board/cards/<id>.lock`)与多卡并发
- ACP 远端 worker、持久队列、可观测性(Phase 2)
