# Changelog

本文件遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 风格,
版本号遵循 [SemVer](https://semver.org/lang/zh-CN/)。1.0 前的 minor 版本视为
可能包含破坏性变更的功能版本。

## [Unreleased] — 2026-04-18

### Added
- **Daemon 并行执行**:daemon 支持同时认领并执行多张卡(`--max-claims N`,默认 1),
  调度器与 worker 角色解耦后可并发推进互不依赖的卡片。
- **依赖自动推进**:orchestrator 在 tick 时检测所有依赖均已 DONE 的卡,自动将其
  从 PENDING 推进到 READY,无需手动 unblock。
- **只读 Web UI**(`kanban web`):新增本地 HTTP server(`kanban/web.py` +
  `kanban/web_assets/`),在浏览器中展示看板状态、卡片详情与事件流,仅读不写。
  `pyproject.toml` 新增 `aiohttp` 依赖。
- MCP server 工具集补充:新增 `tick_once` / `run_until_idle` 等 tool。
- `uv.toml.example`:项目级 uv 配置样例。
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

[Unreleased]: https://github.com/jin-bo/agentao-kanban/compare/v0.1.4-rc1...HEAD
[0.1.4-rc1]: https://github.com/jin-bo/agentao-kanban/compare/v0.1.3...v0.1.4-rc1
[0.1.3]: https://github.com/jin-bo/agentao-kanban/releases/tag/v0.1.3
[0.1.2]: https://github.com/jin-bo/agentao-kanban/releases/tag/v0.1.2
[0.1.1]: https://github.com/jin-bo/agentao-kanban/releases/tag/v0.1.1
[0.1.0]: https://github.com/jin-bo/agentao-kanban/releases/tag/v0.1.0
