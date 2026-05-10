# Changelog

本文件遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 风格,
版本号遵循 [SemVer](https://semver.org/lang/zh-CN/)。1.0 前的 minor 版本视为
可能包含破坏性变更的功能版本。

## [Unreleased]

### Added
- **Web UI:Add Card 模态支持 `depends_on`**。新增"Depends on"区:
  chip 列表 + 搜索框 + native `<datalist>` 自动补全,候选来自最近一次
  `/api/board` 快照(过滤掉 DONE),按短 ID + 标题展示。输入接受全 UUID
  或唯一短前缀,Enter / "+ Add" 按钮提交,× 移除。后端 `CardCreateRequest.depends_on`
  自 0.1.6 就在,缺的只是前端入口——之前 web 上建出来的卡都没法挂依赖,
  必须切到 CLI 的 `kanban card add --depends`。后端 happy-path 测试同步补齐。

- **Web UI:Artifacts 面板**。卡片详情模态框新增 Artifacts 区,展示每张
  卡 `workspace/raw/<card-id>/artifacts-<ts>/` 下被 `WorktreeManager.detach()`
  抢救出来的 gitignored 产物——文件路径 + 大小,点击直接打开。新端点
  `GET /api/cards/{card_id}/artifacts`(列出快照与文件)与
  `GET /api/cards/{card_id}/artifacts/{snapshot}/file?path=...`(取单文件)。
  路径校验:snapshot 名必须匹配 `artifacts-<utc-stamp>`,`path` 不允许
  `..`/绝对路径/leading slash,leaf 是 symlink 直接 403,intermediate
  symlink 跳出 snapshot 走 400。单文件响应上限 8 MiB,超过返回 413
  并提示磁盘路径。读侧契约不变:不需要 `--enable-writes`。
  - 0.1.6 引入 artifacts 抢救之后,events.log 里只有 `worktree.artifacts_saved`
    一行事件,产物本身需要打开终端 `ls workspace/raw/...` 才能看到。这次让
    web 直接可见,补齐了那次大改在 UI 上缺失的最后一截。

## [0.1.7] — 2026-05-07

### Added
- **`kanban init`** — 一键脚手架。建 `.kanban/`(项目根标记 + `config.yaml`,
  里头记录 `board_dir`)、`workspace/board/`,可选 `--copy-agents` 把
  `kanban/defaults/*.md` 复制到 `.agentao/agents/`,可选 `--demo` 顺手种入
  示例卡。在 git 仓库内会提示 worktree 隔离默认开启。
- **`kanban demo`** — 在文件级板上种入 4 张示例卡(覆盖不同 priority 与
  acceptance 形态)并跑 mock orchestrator 到 idle,所有卡进 DONE,展示完
  整管线;`--no-run` 只种不跑。`init --demo` 复用同一个 seed 函数,
  非空板拒绝重复种入,避免污染真实工作板。
- **项目根发现**:`--board` 不显式指定时,从 cwd 向上找 `.kanban/`,
  按 `.kanban/config.yaml` 的 `board_dir` 解析(默认 `workspace/board`)。
  没有 marker 时回落到 `<cwd>/workspace/board`,与 v0.1.6 之前行为一致。
- **`kanban daemon` 子命令**:
  - `kanban daemon status` — 与 `GET /api/daemon` 同源的三态(running /
    stale / stopped)+ pid + 启动时间;`--json` 输出便于脚本消费。
  - `kanban daemon stop` — 读 `.daemon.lock` 里的 pid,SIGTERM,等待最多
    `--timeout`(默认 5s)看锁文件被释放。`--force` 用 SIGKILL。stale lock
    走清理路径而不是发信号。
  - `kanban daemon logs [-f] [-n N]` — tail / follow `<board>/daemon.log`,
    无文件时打印一行排查提示。
- **`kanban mcp install`** — 直接吐 `claude mcp add ...` / `codex mcp add ...`
  命令行(默认两个都打);`--client claude|codex` 选其一,`--run` 直接执行。
  服务端仍是单独的 `kanban-mcp` 二进制,这里只解决"运行命令长 + 易拼错"
  的问题。
- **`kanban card acceptance edit`** — 在 `$EDITOR` / `$VISUAL` 里编辑
  acceptance 列表,行首 `#` 与空行自动剔除。`add/rm/list/clear` 仍保留
  作为脚本路径。
- **No-args banner**:`uv run kanban` 不带子命令时不再 argparse 报错退出,
  改为打印版本、当前板、daemon 状态与 5 条常用命令,rc=0。
- **shell 补全**:`pyproject.toml` 加 `argcomplete>=3.5`,parser 末尾
  `argcomplete.autocomplete(parser)` 接入。一句 `eval "$(register-python-
  argcomplete kanban)"` 即可在 zsh/bash 里获得子命令与 status 名补全。
- `kanban/__init__.py` 暴露 `__version__`(`importlib.metadata` 取,
  source-tree 回落读 `pyproject.toml`),供 banner 与下游脚本使用。
- **`kanban doctor` 环境检查 + `--fix`**:在原有卡级检查之外,新增以
  `cwd-` 为前缀的环境检查,覆盖 `<cwd>` 周围的常见配置故障——`.kanban/`
  marker 缺 `config.yaml`(`cwd-marker-no-config`)、`config.yaml` 没有
  可解析的 `board_dir:`(`cwd-config-no-board-dir`)、`--board` 指向不存
  在的目录(`cwd-board-missing`)、`--board` 指向普通文件
  (`cwd-board-not-a-dir`,无 fix)、`.daemon.lock` 不可解析或 pid 已死
  (`cwd-malformed-lock` / `cwd-stale-lock`)。`--fix` 一键应用安全的
  幂等修复(写默认 config、mkdir、unlink stale lock);卡级问题永远
  不会自动改写,只有环境问题可修。`--json` 输出新增 `fixes_applied`
  数组与每条 check 的 `fixable` 布尔。

### Changed
- README quickstart 顶端改为 `uvx --from agentao-kanban kanban init/demo/web`
  三步,git clone 路径降级为"本地开发"小节。
- 修正 `docs/kanban-cli-guide.md` §9.2 的 doc drift:`POST /api/cards`
  并不与 `.daemon.lock` 串行化(代码自 v0.1.6 起就是 race-free 例外),
  文档改为如实描述这点。

## [0.1.6] — 2026-05-06

### Added
- **Web UI 可写入口**(opt-in):`kanban web --enable-writes` 暴露
  `POST /api/cards`,在浏览器里以模态卡片形式创建 INBOX 卡。默认关闭以保留只读
  契约;`--allow-remote-writes` 是 non-loopback 绑定下的显式逃生口,无此 flag
  时拒绝在 `0.0.0.0` 上同时启用写入。卡片详情面板也改为模态弹窗(原右侧 panel
  下线,grid 收成 5 列),Add Card 与 Card Detail 共享 `.card-modal` 视觉语言。
- **Daemon 状态面板**:`kanban/daemon.py` 新增 `daemon_status()`(只读,不清理
  stale lock);Web UI 在 Runtime 面板顶部显示三态(running / stopped / stale
  lock)+ 颜色点 + `pid X, started Ym ago`。新增 `GET /api/daemon` 端点。
- **Worktree artifact 抢救**:`WorktreeManager.detach()` 在 `git worktree remove`
  之前快照 worktree 内被 gitignore 的文件到 `workspace/raw/<card-id>/artifacts-<ts>/`,
  避免 worker 写到 gitignored 路径(常见的 `workspace/reports/...`)的交付物随
  worktree 目录一起被删除。修复了一个真实的产物丢失场景。
  - 默认每张卡保留 5 份快照,体积上限 500 MiB,可通过环境变量
    `KANBAN_ARTIFACTS_MAX_BYTES` 调整。
  - **按文件计帐 + denylist**:`node_modules/`、`__pycache__/`、`.venv/`、
    `dist/`、`build/`、`target/`、`*.pyc` 等构建/缓存路径在计帐前先剔除;
    剩余文件按 git 枚举顺序填入,直到耗尽预算——不再因为单个超大文件
    全盘放弃。skip 项数和字节数会出现在 warning log 里。
  - 新事件类型 `worktree.artifacts_saved` 在 events.log / Web UI 里可见。

### Changed
- `WorktreeManager.detach()` 返回值从 `bool` 改为 `DetachResult`(`removed` +
  `artifacts_path` + `artifacts_skipped_reason`),保留 `__bool__` 以兼容
  `if mgr.detach(...)` 这类用法。直接 `is True/False` 的调用点需要改为
  `.removed`;库内已全量更新。
- 依赖升级:`agentao>=0.3.0` → `agentao>=0.4.2`。0.4.0 起 agentao 拆分为 core
  + 多个可选 extras,kanban 仅使用 core 内的 `agentao.acp_client`、
  `agentao.embedding`、`Agentao` 构造器,无需声明 extras;0.4.2 把
  `agentao.harness` 重命名为 `agentao.host`(旧名称作为带 `DeprecationWarning`
  的别名继续可用),kanban 运行时与测试均未直接引用 `agentao.harness`,故无
  导入路径修改。522 个测试在 agentao 0.4.2 下全部通过。

## [0.1.5] — 2026-05-01

### Added
- **Daemon 并行执行**:daemon 支持同时认领并执行多张卡(`--max-claims N`,默认 1),
  调度器与 worker 角色解耦后可并发推进互不依赖的卡片。
- **依赖自动推进**:orchestrator 在 tick 时检测所有依赖均已 DONE 的卡,自动将其
  从 INBOX 推进到 READY,无需手动 unblock。
- **只读 Web UI**(`kanban web`):新增本地 HTTP server(`kanban/web.py` +
  `kanban/web_assets/`),在浏览器中展示看板状态、卡片详情与事件流,仅读不写。
  `pyproject.toml` 新增 `fastapi` 与 `uvicorn[standard]` 依赖。
- MCP server 工具集补充:扩展 `tick` / `run` 路径对 worktree 隔离、依赖自动推进
  和终态 worktree detach 的支持,并补齐 card 序列化里的返工反馈字段。
- `uv.toml.example`:项目级 uv 配置样例。
- 新文档:`docs/dependency-auto-advance-and-all-parallel-plan.md`、
  `docs/kanban-web-readonly-plan.md`。
- 新测试:`tests/test_combined_parallel.py`、`tests/test_dependency_auto_advance.py`、
  `tests/test_web.py`。

## [0.1.4-rc1] — 2026-04-17

预发布候选(Release Candidate)。

### Added
- **MCP Server (`kanban-mcp`)**:新增 stdio MCP server,把 card CRUD
  (add/list/show/move/block/unblock)、`events.log` 尾流、orchestrator
  `tick`/`run` 共 9 个 tool 暴露给 Claude Code / Codex / 任意 MCP client;
  另外注册 `kanban://board/snapshot`、`kanban://card/{id}`、
  `kanban://events/recent` 三个 resource。所有写类 tool 复用 CLI 的
  `assert_no_daemon` 守卫(daemon 持锁时拒写,启动 `--force` 可绕过)。
  入口注册在 `[project.scripts]` 的 `kanban-mcp = "kanban.mcp:main"`,
  实现见 `kanban/mcp.py`,设计文档见
  [`docs/mcp-server-design.md`](docs/mcp-server-design.md)。
- **Reviewer/Verifier 返工闭环**:reviewer/verifier 的 `ok=false` JSON 新增
  第三种形态 `{"ok": false, "revision_request": {"summary", "hints[]",
  "failing_criteria[]"}}`。orchestrator 检测到 `revision_request` 时累加到
  `Card.revision_requests`(按时间序,永不覆盖)、bump `Card.rework_iteration`、
  把卡从 REVIEW/VERIFY rewind 到 READY(worktree 保持 attached),worker 下
  一轮 prompt 里自动注入完整 REWORK HISTORY。预算由 `RetryPolicy.rework`
  控制(默认 3 次),超限合成 BLOCKED 走原有终态路径。不带 `revision_request`
  的旧 `{"ok": false, "blocked_reason"}` 继续直接 BLOCKED,向后兼容。
- 新事件 `rework.requested`,带 `rework_iteration` extra,`events.log` +
  `kanban events` 文本流同步可见。
- reviewer / verifier agent spec bump 到 v2:新增 Request rework vs Terminal
  rejection 的输出契约与选择指南。
- **每卡 Git worktree 隔离（默认 auto）**:worker 首次 claim 时自动创建
  `kanban/<card-id>` 分支与 `workspace/worktrees/<card-id>` 工作目录,
  worker/reviewer/verifier 在同一 worktree 内串行执行;DONE/BLOCKED 自动
  detach(目录释放、分支保留),scheduler idle tick 与 `kanban worktree
  prune` 在 merged-to-base 或 BLOCKED retention(默认 7 天)后删分支。
  `--worktree` tri-state:默认 auto(git repo 内启用、非 git 仓库 stderr
  warning 后降级关闭);`--worktree` 硬要求 git repo;`--no-worktree` 关。
- Worktree 运行时注入:worker 在调用 executor 前把 `executor.working_directory`
  临时覆盖为 `claim.worktree_path`,对 executor protocol 零侵入。
- 新事件:`worktree.created` / `worktree.detached` / `worktree.pruned`,
  `events.log` + `kanban events` 文本流均带 `wt=kanban/<card-id>` extra。
- 新子命令:`kanban worktree list` / `prune [--retention-days N]` / `diff <card-id>`。
- Card TOML front-matter 新增 `worktree_branch` 与 `worktree_base_commit`
  (fork-point SHA,不可变);ExecutionClaim JSON 新增 `worktree_path`。
- `docs/worktree-isolation-design.md` — 每卡 Git worktree 隔离设计方案。

## [0.1.3] — 2026-04-15

### Added
- Multi-backend 执行器 (`--executor multi-backend`):按角色走 `agent_profiles.yaml`
  路由,支持 `subagent` 与 `acp` 两种 backend。
- ACP 远端 worker 支持:通过 `.agentao/acp.json` 声明外部 CLI(样板见
  `docs/acp.sample.json`),默认包含 Gemini CLI (`npx @google/gemini-cli --acp`)
  的 `gemini-worker` / `gemini-reviewer` profile。
- Router agent 层 (`kanban-router`):可按卡动态在 role 候选 profile 间挑选,
  `KANBAN_ROUTER=off` 全局关闭,失败自动回落 role 默认。
- Agent profiles 配置加载:优先 `<cwd>/.kanban/agent_profiles.yaml`,
  回退打包默认 `kanban/defaults/agent_profiles.yaml`。
- 发布元数据:`pyproject.toml` 补齐 `license` / `authors` / `maintainers` /
  `keywords` / `classifiers` / `[project.urls]`。
- README 安装指南拆分三条路径(纯 mock / agentao sub-agent / multi-backend + ACP),
  明确首次安装的前置条件与 API key。
- `CHANGELOG.md` (本文件)。

### Changed
- `--executor agentao` 与 `multi-backend` 都从 `--board` 推导的 project root
  读 `.agentao/` 与 `.kanban/`,不再跟随 shell cwd。

## [0.1.2] — 并发内核

### Added
- Scheduler / worker 角色拆分:`daemon --role {scheduler,worker,all,legacy-serial}`。
- 运行时状态机:`workspace/board/runtime/{claims,results,workers}/`,claim
  CAS 互斥抢占,worker 只写 envelope,scheduler 独占提交。
- 重试矩阵:`FailureCategory` (infrastructure / lease_expiry / timeout /
  malformed / functional) + `RetryPolicy` 预算。
- 运维 CLI:`kanban claims` / `kanban workers` / `kanban recover --stale`,
  events 日志扩展 `claim_id` / `worker_id` / `failure_category` /
  `retry_of_claim_id` 字段。
- 结构化 reviewer / verifier 输出契约,planner supersession-gated replan。

### Fixed
- Commit path 所有权(Codex 对抗评审),lease 续约原子性,crash-safe
  worker submit。

## [0.1.1] — 操作员 CLI

### Added
- 结构化卡片模型:`context_refs` (required/optional)、`acceptance_criteria`
  列表、`blocked_reason`、`history` 带 `[system]` 前缀。
- 操作员命令:`card edit`、`card context {list,add,rm}`、`card acceptance
  {list,add,rm,clear}`、`requeue`、`events`、`traces`、`doctor`。
- `MarkdownBoardStore`:TOML front-matter + `events.log` JSONL + 原始
  transcript 保留最近 5 份。
- `.daemon.lock` 单写锁,`--force` 覆盖限应急用;陈旧锁启动时自动清理。

## [0.1.0] — 初始骨架

- 多 Agent 看板最小闭环:卡片/列/依赖/WIP、scheduler、按角色路由的执行器
  (planner / worker / reviewer / verifier)、本地 dispatcher daemon、基于
  `MockAgentaoExecutor` 的离线状态机。

[Unreleased]: https://github.com/jin-bo/agentao-kanban/compare/v0.1.7...HEAD
[0.1.7]: https://github.com/jin-bo/agentao-kanban/compare/v0.1.6...v0.1.7
[0.1.6]: https://github.com/jin-bo/agentao-kanban/compare/v0.1.5...v0.1.6
[0.1.5]: https://github.com/jin-bo/agentao-kanban/compare/v0.1.4-rc1...v0.1.5
[0.1.4-rc1]: https://github.com/jin-bo/agentao-kanban/compare/v0.1.3...v0.1.4-rc1
[0.1.3]: https://github.com/jin-bo/agentao-kanban/releases/tag/v0.1.3
[0.1.2]: https://github.com/jin-bo/agentao-kanban/releases/tag/v0.1.2
[0.1.1]: https://github.com/jin-bo/agentao-kanban/releases/tag/v0.1.1
[0.1.0]: https://github.com/jin-bo/agentao-kanban/releases/tag/v0.1.0
