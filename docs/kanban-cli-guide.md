# Kanban CLI Guide

这份文档面向日常使用 `kanban` CLI 的操作者,重点不是罗列所有参数,而是讲清楚:

- 什么时候用哪条命令
- 守护进程与人工操作如何配合
- 多 worker 模式下哪些命令是安全的
- 遇到卡住、阻塞、超时、审计需求时怎么查

如果你只想知道项目是什么、目录怎么组织、并发内核怎么设计,先看 [README](../README.md)。

## 1. 5 分钟上手

如果你第一次用 `kanban`,先走完这一条最短路径。

### 1.1 创建一张卡

```bash
uv run kanban card add \
  --title "生成周报摘要" \
  --goal "从 workspace/data/sales.csv 生成一份 Markdown 周报"
```

### 1.2 看卡 ID

```bash
uv run kanban list
```

记下新卡的 `<card_id>`。

### 1.3 补必要输入和验收条件

```bash
uv run kanban card context add <card_id> \
  --path workspace/data/sales.csv \
  --kind required

uv run kanban card acceptance add <card_id> \
  --item "生成 workspace/reports/weekly-summary.md"
```

### 1.4 把卡放进可执行队列

```bash
uv run kanban move <card_id> ready
```

### 1.5 开始执行

本地一次性跑完:

```bash
uv run kanban run
```

持续跑:

```bash
uv run kanban daemon
```

### 1.6 查看结果

```bash
uv run kanban show <card_id>
uv run kanban events <card_id>
```

如果卡被阻塞:

```bash
uv run kanban requeue <card_id> --to ready --note "已补充缺失输入"
```

读到这里,你已经掌握了最短闭环:

1. 建卡
2. 补上下文和验收条件
3. 移到 `ready`
4. 执行
5. 查看结果
6. 阻塞时恢复

## 2. 先建立正确心智模型

`kanban` 的 CLI 可以分成四类:

1. 建卡和维护卡片内容
2. 驱动执行
3. 观察运行时状态
4. 恢复异常

最重要的约束只有两条:

- card 文件是看板事实来源,由 orchestrator 提交状态变更
- daemon 运行时,普通写命令默认会被 `.daemon.lock` 拒绝

这意味着:

- 日常编辑卡片,优先在 daemon 停止时做
- daemon 跑起来后,优先用只读命令观察
- `--force` 只用于恢复,不要当常规操作

## 3. 最常见的工作流

### 建卡

```bash
uv run kanban card add \
  --title "生成周报摘要" \
  --goal "从 workspace/data/sales.csv 生成一份 Markdown 周报" \
  --priority HIGH \
  --acceptance "生成 workspace/reports/weekly-summary.md" \
  --acceptance "报告包含总销售额、TOP 5 产品和异常说明"
```

建卡后先看一眼:

```bash
uv run kanban list
uv run kanban show <card_id>
```

适用场景:

- `list` 看整个板子的状态分布
- `show` 看某张卡的目标、验收条件、输出和历史

### 补充上下文和验收标准

```bash
uv run kanban card context add <card_id> \
  --path workspace/data/sales.csv \
  --kind required \
  --note "原始销售数据"

uv run kanban card acceptance add <card_id> \
  --item "报告包含环比变化"
```

经验上:

- `context` 适合补“做事必须看的输入”
- `acceptance` 适合补“做完以后怎么判定算完成”

### 让卡进入可执行状态

如果卡刚创建在 `inbox`,通常需要人工移到 `ready`:

```bash
uv run kanban move <card_id> ready
```

如果依赖和上下文都齐了,也可以批量按你的流程把卡整理到 `ready` 后再启动 daemon。

### 执行

单次跑到空闲:

```bash
uv run kanban run
```

持续运行:

```bash
uv run kanban daemon
```

真正并发执行:

```bash
uv run kanban daemon --role scheduler --max-claims 4
uv run kanban daemon --role worker --worker-id worker-1
uv run kanban daemon --role worker --worker-id worker-2
```

说明:

- `run` 适合本地小规模手动推进
- `daemon` 适合持续消费
- `daemon --role all` 只是一个进程内的便利模式,不是多 worker 拓扑

## 4. 推荐命令分工

### 卡片维护

```bash
uv run kanban card add ...
uv run kanban card edit <card_id> --title "新标题"
uv run kanban card context list <card_id>
uv run kanban card context add <card_id> --path docs/spec.md --kind required
uv run kanban card acceptance list <card_id>
```

什么时候用:

- `card add`: 新建工作项
- `card edit`: 改标题、目标、优先级、blocked reason
- `card context ...`: 管理输入材料
- `card acceptance ...`: 管理验收条件

不要混淆:

- `blocked_reason` 是“为什么现在不能继续”
- `acceptance_criteria` 是“做成什么样才算完成”

### 状态调整

```bash
uv run kanban move <card_id> ready
uv run kanban block <card_id> "缺少上游数据"
uv run kanban unblock <card_id> --to ready
uv run kanban requeue <card_id> --to ready --note "已补数据"
```

建议:

- 正常状态流转尽量交给 orchestrator
- `block` 用于明确外部阻塞
- `requeue` 用于从失败/阻塞现场重新放回流程

`requeue` 比 `move` 更适合恢复场景,因为它会处理 `blocked_reason` 并留下恢复语义。

### 运行与调试

```bash
uv run kanban tick
uv run kanban run --max-steps 20
uv run kanban daemon --once
uv run kanban daemon --verbose
```

适用场景:

- `tick`: 只推进一步,适合调状态机
- `run --max-steps`: 限制最大步数,避免死循环式调试
- `daemon --once`: 看一次 scheduler/worker 轮询会做什么
- `--verbose`: 查 claim、心跳、恢复逻辑时打开

## 5. daemon 运行时怎么协作

v0.1.2 的运行时里有两层互斥保护,作用范围不同,别混起来:

### 5.1 `.daemon.lock` (板级) —— 只保护 scheduler 入口

`.daemon.lock` 只由以下三种 daemon 角色持有:

- `--role scheduler`
- `--role all` (scheduler + worker 同进程)
- `--role legacy-serial` (v0.1.1 串行路径)

**`--role worker` 故意不持这把锁**,因为多 worker 是设计目标。这意味着:

只要上面三个角色之一在跑,以下命令都**因为板锁**被拒绝(加 `--force` 才行):

- `card add`
- `tick` / `run` / `recover --stale`

而**不持板锁的单 worker 模式下**,板锁检查不会触发,上面这些命令可以照常跑——但**每张卡自己的活 claim 才是真正的互斥信号**,见 §5.2。

### 5.2 live claim (卡级) —— 保护正在被 worker 处理的单张卡

以下**针对具体 card_id 的写命令**,除了 `.daemon.lock` 外,还会检查目标卡有没有 live claim;有则拒绝:

- `card edit <id>`
- `card context add/rm <id>`
- `card acceptance add/rm/clear <id>`
- `move <id>`
- `block <id>` / `unblock <id>`
- `requeue <id>`

错误消息会告诉你 claim id 和归属 worker,比如:

```text
Card e360a897 has a live execution claim clm-a1b2c3 (worker=worker-1);
refuse to mutate. Run `kanban claims e360a897` and `kanban workers` to check,
stop the claimed worker (or wait for it to finish), then retry.
Pass --force to override (may race with in-flight execution).
```

### 5.3 `--force` 的真实语义

`--force` 同时绕过板锁和 live-claim 检查。**它不是同步机制——只是让你绕开检查**。
用它做 `--force unblock` 之类的恢复时,你必须清楚:

1. Worker 可能正在改这张卡——你的编辑会被它的下一个 envelope 覆盖。
2. 就算 daemon 持内存缓存没看到你的改动,PR5 已经加了 `store.refresh()`,
   scheduler 每轮都会回读盘面——但这是**最终一致**,不是强同步。

所以 `--force` 只适合应急,不是常规工作流。

### 5.4 安全编辑正在被 worker 处理的卡——操作员 playbook

如果你确实要在运行时改一张活 claim 下的卡:

1. `kanban claims <id>` 确认归属 worker(`worker_id` 字段)。
2. **停掉那个 worker 进程**(SIGINT 正常退出,或 `kill <pid>`;pid 在
   `kanban workers` 里)。worker 正常退出时会清自己的 WorkerPresence 文件,
   但**不会自动清 claim**——claim 会继续活着直到 lease 过期。
3. 等一下:
   - 如果 worker 已经 submit 了 envelope,`kanban claims <id>` 会在 scheduler 下一轮 commit 后变空。
   - 如果没 submit,lease 最多 `lease_seconds`(默认 60s)后过期。scheduler 下一轮会按重试矩阵
     处理(infra 退路 2 次,lease_expiry 1 次)。
   - 宁可再跑一次 `kanban recover --stale` 确认清空。
4. `kanban claims <id>` **返回空**后才改卡。
5. 改完,重启那个 worker(如果还需要它继续跑)。

**如果你愿意接受覆盖风险**(比如确认 worker 卡死了、或你就是想覆盖):

```bash
uv run kanban --force edit/move/requeue <id> ...
```

## 6. 多 worker 模式下该怎么看

并发模式下有三个观察入口最有用:

```bash
uv run kanban claims
uv run kanban workers
uv run kanban events <card_id>
```

它们分别回答三个问题:

- `claims`: 现在有哪些 card 正在被处理
- `workers`: 哪些 worker 还活着
- `events`: 这张 card 最近到底发生了什么

典型排查套路:

1. 先 `claims` 看 card 是否有 live claim
2. 再 `workers` 看 claim 对应的 worker 是否还活着
3. 最后 `events <card_id>` 看是正常完成、重试、超时还是被阻塞

## 7. 遇到问题怎么处理

### card 卡在 `blocked`

先看详情:

```bash
uv run kanban show <card_id>
uv run kanban events <card_id>
```

如果是外部依赖补齐后恢复:

```bash
uv run kanban requeue <card_id> --to ready --note "已补齐缺失输入"
```

### worker 崩了或超时了

先看运行时状态:

```bash
uv run kanban claims
uv run kanban workers
```

如果 claim 已经过期但还没被 scheduler 回收:

```bash
uv run kanban recover --stale
```

恢复逻辑会根据失败类别决定:

- 重试
- 或把 card 移到 `blocked`

### 不知道某张卡为什么没继续跑

按这个顺序看:

```bash
uv run kanban show <card_id>
uv run kanban claims <card_id>
uv run kanban events <card_id>
uv run kanban doctor
```

常见原因:

- 还在 `inbox`
- 依赖未完成
- 已被 `blocked`
- 有 live claim 但 worker 还没提交结果
- 数据损坏被 `doctor` 检出

## 8. 审计与留痕

如果你关心“谁做了什么、什么时候做的、产出了什么”,优先看:

```bash
uv run kanban events <card_id>
uv run kanban traces <card_id>
```

区别:

- `events` 是结构化摘要和生命周期事件
- `traces` 是保留的原始 transcript

通常先看 `events`,只有在需要还原 agent 原始响应时再看 `traces`。

## 9. 命令速查

下面这张速查表按“我现在想做什么”组织。

### 9.1 建卡和维护内容

| 目的 | 命令 |
|---|---|
| 新建卡 | `uv run kanban card add --title T --goal G` |
| 看单卡详情 | `uv run kanban show <card_id>` |
| 改标题或目标 | `uv run kanban card edit <card_id> --title "..." --goal "..."` |
| 加 context | `uv run kanban card context add <card_id> --path P --kind required` |
| 看 context | `uv run kanban card context list <card_id>` |
| 加 acceptance | `uv run kanban card acceptance add <card_id> --item "..."` |
| 看 acceptance | `uv run kanban card acceptance list <card_id>` |

### 9.2 调整状态

| 目的 | 命令 |
|---|---|
| 移到 ready | `uv run kanban move <card_id> ready` |
| 主动阻塞 | `uv run kanban block <card_id> "reason"` |
| 解除阻塞 | `uv run kanban unblock <card_id> --to ready` |
| 从失败现场恢复 | `uv run kanban requeue <card_id> --to ready --note "..."` |

### 9.3 执行和观察

| 目的 | 命令 |
|---|---|
| 推进一步 | `uv run kanban tick` |
| 跑到空闲 | `uv run kanban run` |
| 前台 daemon | `uv run kanban daemon` |
| 调试一轮 daemon | `uv run kanban daemon --once --verbose` |
| 看全板状态 | `uv run kanban list` |
| 看事件 | `uv run kanban events <card_id>` |
| 看原始 transcript | `uv run kanban traces <card_id>` |
| 体检 | `uv run kanban doctor` |

### 9.4 并发运行时

| 目的 | 命令 |
|---|---|
| 起 scheduler | `uv run kanban daemon --role scheduler --max-claims 4` |
| 起 worker | `uv run kanban daemon --role worker --worker-id worker-1` |
| 看活跃 claims | `uv run kanban claims` |
| 看活跃 workers | `uv run kanban workers` |
| 回收过期 claim | `uv run kanban recover --stale` |

## 10. 给新用户的最低可行命令集

如果团队里有人只需要先上手最核心能力,先学这 10 条就够了:

```bash
uv run kanban card add --title T --goal G
uv run kanban list
uv run kanban show <card_id>
uv run kanban card context add <card_id> --path P --kind required
uv run kanban card acceptance add <card_id> --item "..."
uv run kanban move <card_id> ready
uv run kanban daemon
uv run kanban events <card_id>
uv run kanban block <card_id> "reason"
uv run kanban requeue <card_id> --to ready
```

## 11. 文档放置建议

这个主题更适合作为独立文档,而不是继续扩充 README。

原因:

- README 现在已经同时承担项目介绍、架构概览、目录约定、并发设计入口
- CLI 指南是“任务导向”的操作文档,读者和阅读时机都不同
- 单独文档更容易后续继续补“场景化示例”和“故障处理手册”

推荐做法:

1. README 保留 1 段 CLI 导航和链接
2. 把完整教程放在本文件
3. 后续如果命令继续增长,再拆出 `docs/cli-reference.md`
