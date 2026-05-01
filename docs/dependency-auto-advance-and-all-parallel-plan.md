# 依赖链自动推进与 `--role all` 真并行

> Status: implemented in v0.1.5. This document is retained as the design and
> release checklist for the dependency auto-advance and single-process
> parallel daemon work.

## Summary

- 在卡片**首次进入 `done`** 时，统一检查其反向依赖；把所有 `depends_on` 包含该卡、且当前仍在 `inbox`、且**全部依赖都已 `done`** 的子卡自动推进到 `ready`。
- 把 `uv run kanban daemon --role all --max-claims N` 从“单线程 `scheduler + 1 worker` 交替轮询”改为“**1 个 scheduler + N 个 worker** 同进程真并行”。
- 不改卡片 schema；对外可见的接口变化只有 CLI 语义和文档说明。

## Key Changes

### 1. DONE 触发的依赖链推进

- 新增一个共享 helper，职责是：
  - 输入：`store`、刚进入 `done` 的 `card_id`
  - 扫描全板找出反向依赖该卡的候选子卡
  - 仅处理 `status == inbox` 的子卡
  - 对每个候选复用现有依赖判定逻辑，确认其 `depends_on` **全部**为 `done`
  - 满足后执行 `inbox -> ready`
- helper 内加一行实现注释，说明这里按全板扫描反向依赖，复杂度为 O(n)，对当前 board 规模可接受。
- 自动推进时给子卡写明确 history/event，内容固定为“某依赖完成，所有依赖已满足，因此自动从 `inbox` 推进到 `ready`”；不做静默跳转。
- 不做递归级联；只处理“当前这张刚完成的父卡”的直接反向依赖。更深链路由后续父卡各自进入 `done` 时自然推进。
- 不改 `blocked/ready/doing/review/verify/done` 子卡状态；只动 `inbox`。
- 不补 planner 产物，不改 `owner_role`；该能力假设这些依赖型子卡已经是“可直接开工、只是被依赖门控”的卡。

### 2. DONE helper 的明确挂点

- orchestrator 正常路径：
  - 在 `orchestrator._apply_normal_result()` 里，`store.move_card(..., done, ...)` 之后、同一写上下文内立即调用 helper。
  - 不放到 `clear_claim` 之后；依赖推进事件应与当前 `done` 迁移处于同一提交语义里。
- CLI 路径：
  - `cmd_move(... done)`
  - `cmd_unblock(... --to done)`
- MCP 路径：
  - `tool_card_move(... done)`
  - `tool_card_unblock(... --to done)`
- 触发条件统一为**首次** `!= done -> done`；避免对已经在 `done` 的卡重复发推进事件。

### 3. `--role all --max-claims N` 真并行

- 重构 `CombinedDaemon`：
  - 保留一个 scheduler loop
  - 在同一进程内启动 **N 个 worker loop**
  - scheduler 与 workers 跑在独立线程
- `max_claims` 的语义统一为：
  - scheduler 侧：live-claim 上限
  - `all` 模式侧：worker 数量
- `all` 模式中的每个 worker 使用**独立**的 store/orchestrator/executor 实例，避免共享 executor cwd patch、store 缓存与心跳状态。
- 并发文件写入仍依赖现有多进程 split topology 已使用的同一套文件系统原子性保证；`all` 只是把“多进程并行”扩展为“单进程多线程 + 独立 store 实例”并复用同样的 claim/result/runtime 协议。
- `worker_id` 在 `all` 模式下作为前缀使用：
  - 显式传入时：`<prefix>-1 ... <prefix>-N`
  - 未传入时：生成一个基前缀，再派生 N 个稳定子 id
- `--once` 在 `all` 模式下明确为：
  - scheduler 做**一次** claim-creation / commit / recovery pass
  - 每个 worker 做**一次** acquire-execute-submit cycle
  - 然后全部线程退出

### 4. 线程模型与停机语义

- 把 `_RoleDaemonBase._stop` 从 plain bool 改成 `threading.Event`，所有 loop 统一轮询该 event；不使用无保护共享布尔值。
- 顶层 `CombinedDaemon` 持有一个共享 stop event，并把它注入 scheduler/worker 子 daemon；停机广播只通过这个共享 event 传播。
- signal handler 只在**主线程**安装一次，由顶层 runner 负责；收到 `SIGINT`/`SIGTERM` 后设置共享 stop event，通知所有线程退出。
- 不在 worker/scheduler 线程里调用 `signal.signal(...)`。
- `request_stop()` 的“双击强退”语义保留，但强退逻辑仍只由主线程 handler 触发；子线程只响应共享 stop event。
- 退出时统一清理所有 worker presence，并等待内部线程有界 join，避免残留活 worker 记录。

### 5. `--detach` / fork 时序约束

- `--detach` 的 `os.fork()` 必须发生在任何 worker/scheduler 线程创建之前。
- `cmd_daemon()` 继续保持现有顺序：
  - 先处理 `--detach`
  - 再构造并启动 `CombinedDaemon` 内部线程
- 文档中明确写出：`--detach` 是“先 fork，后启线程”，避免未来回归到“线程后 fork”的错误实现。

### 6. 文档与默认说明

- README：
  - 更新 daemon role 章节，明确 `all` 现在是“单进程真并行”
  - 在同一句里说明 `--max-claims N` 同时决定 claim 上限和 `all` 模式 worker 数
  - 给出 `--role all --max-claims 4` 的推荐用法和适用场景
- `docs/kanban-cli-guide.md`：
  - 更新运维说明、`claims/workers` 的观测预期
  - 明确 `all` 模式下会看到 N 个 worker presence
  - 补 `--once` 的新精确定义
- 如有并发设计文档或 changelog 条目，同步修正“`all` = 1 worker”的旧表述。
- 不改 worker claim/envelope 协议；只把默认行为与文档说明对齐到真并行实现。

## Public Interface Changes

- `uv run kanban daemon --role all --max-claims N`
  - 旧：1 scheduler + 1 worker，`max_claims` 仅控制 claim 预算
  - 新：1 scheduler + N workers，`max_claims` 同时控制 claim 预算与 worker 数
- `--worker-id` 在 `--role all` 下从“单 worker id”变为“worker id 前缀”
- `--once` 在 `--role all` 下从“单次串行 combined tick”变为“1 次 scheduler pass + N 次 worker single-cycle”

## Test Plan

- 新增 orchestrator/store 级测试：
  - 父卡首次进入 `done` 时，`inbox` 直接子卡被推进到 `ready`
  - 子卡有多个依赖时，只有最后一个依赖完成后才推进
  - 非 `inbox` 子卡不受影响
  - 同一父卡重复 `done` 或 `done -> done` 不重复写推进事件
- 新增 CLI/MCP 路径测试：
  - `move <id> done` 触发依赖推进
  - `unblock <id> --to done` 触发依赖推进
  - MCP move/unblock 到 `done` 同样触发
- 新增 daemon 并发测试：
  - `CombinedDaemon(max_claims=2/3)` 启动后出现对应数量的 worker presence
  - 多张 `ready` 卡可由多个内置 worker 并发处理，而不是单线程串行
  - `--once` 下 scheduler 仅跑一轮、每个 worker 仅跑一轮
  - 停机后 worker presence 被清理
- 新增线程模型回归测试：
  - shared stop event 可让全部线程停止
  - `CombinedDaemon` 不在非主线程安装 signal handlers
- 回归测试：
  - 独立 `scheduler` / `worker` 多进程模式行为不变
  - claim/lease/recovery 语义不变
  - worktree 隔离在 `all` 模式多 worker 下仍不共享 cwd

## Assumptions

- 自动推进仅针对“依赖门控”的 `inbox` 子卡；这类卡被视为已具备直接进入 `ready` 的前置内容。
- 不新增单独的 `--workers` 参数；`max_claims` 直接承载 `all` 模式 worker 数，保持 CLI 简洁。
- 真并行只承诺单进程多线程并发执行；跨进程/跨机器并行继续沿用现有 `scheduler + worker` 分离模式。
