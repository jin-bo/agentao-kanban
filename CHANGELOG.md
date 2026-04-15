# Changelog

本文件遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 风格,
版本号遵循 [SemVer](https://semver.org/lang/zh-CN/)。1.0 前的 minor 版本视为
可能包含破坏性变更的功能版本。

## [0.1.3] — 2026-04-15

当前版本。pyproject / README / 文档以此为准。

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

[0.1.3]: https://github.com/jin-bo/agentao-kanban/releases/tag/v0.1.3
[0.1.2]: https://github.com/jin-bo/agentao-kanban/releases/tag/v0.1.2
[0.1.1]: https://github.com/jin-bo/agentao-kanban/releases/tag/v0.1.1
[0.1.0]: https://github.com/jin-bo/agentao-kanban/releases/tag/v0.1.0
