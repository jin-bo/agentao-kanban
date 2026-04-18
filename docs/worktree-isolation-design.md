# Per-Card Git Worktree Isolation Design

> Graduated default — 2026-04-16. 行为在 kanban ≥ 0.1.4-dev 默认启用
> （见下文 §6 的 tri-state 语义）。

## Context

当前所有 card 的 executor 共享同一个 `working_directory`（即项目根目录）。当多个 worker 并发执行时，agent 对文件系统的写入会互相冲突。需要为每张卡创建独立的 Git worktree，使每个 card 在隔离的分支上工作，最终通过 diff/PR 合并回主分支。

### 同类项目参考

| 项目 | 隔离方式 |
|------|----------|
| [BloopAI/vibe-kanban](https://github.com/BloopAI/vibe-kanban) | 每 issue 独立 worktree + branch + dev server |
| [nwiizo/ccswarm](https://github.com/nwiizo/ccswarm) | Git worktree per agent |
| [ComposioHQ/agent-orchestrator](https://github.com/ComposioHQ/agent-orchestrator) | 每 agent 独立 worktree → 自动 PR |

---

## 核心设计决策

### 1. Worktree 生命周期：跟随 Card 而非 Claim

```
INBOX → READY → DOING ─────────── REVIEW ─── VERIFY ─── DONE
                  │                  │          │          │
           [create worktree]         │          │    [detach worktree]
                  │                  │          │    [branch preserved]
                  ↑                  │          │
                  └── BLOCKED ◄──────┘──────────┘
                        │
                  [detach worktree]
                  [branch preserved]
```

- **创建时机**：Card 首次进入 DOING（第一个 worker claim）时创建
- **保留策略**：worktree 跨 DOING/REVIEW/VERIFY 全程保留，reviewer/verifier 在同一 worktree 中审查
- **清理时机**：分为两步操作（见下文 §2）
- **Planner 不需要 worktree**：planner 只输出 acceptance_criteria，不操作文件系统

### 2. Detach vs Delete：两阶段清理

**问题**：v1 设计在 DONE/BLOCKED 时同时删除 worktree 目录和分支（`git worktree remove` + `git branch -d`），但 `branch -d` 对未合并分支会失败，`branch -D` 则丢失 PR/merge handle。

**修正**：终态清理拆为 **detach**（立即）和 **prune**（延迟/手动）。

| 操作 | 触发时机 | 行为 |
|------|----------|------|
| **detach** | Card → DONE 或 BLOCKED | `git worktree remove <path>` 释放目录；**分支保留** |
| **prune** | 手动 `kanban worktree prune` 或 daemon idle GC | 清理已 detach 且满足条件的分支 |

**Prune 条件**（必须全部满足才删分支）：

- Card 状态为 DONE 或 BLOCKED
- Worktree 已 detach（目录不存在）
- 分支已 merge 到 base（`git branch --merged <base_commit>` 包含它），**或** card 在 BLOCKED 超过 retention 天数（默认 7，可配置）

这样 DONE 卡的分支在 merge/PR 前安全保留，BLOCKED 卡的分支在 retention 窗口后自动清除。

```python
class WorktreeManager:
    def detach(self, card_id: str) -> None:
        """git worktree remove <path> — 释放目录，保留分支"""

    def prune_branch(self, card_id: str, *, force: bool = False) -> bool:
        """删分支。force=False 时仅删已 merge 分支；force=True 用 -D"""

    def prune_stale(
        self,
        card_statuses: dict[str, CardStatus],
        retention_days: int = 7,
    ) -> list[str]:
        """批量清理：DONE+merged → delete，BLOCKED+expired → force delete"""
```

### 3. 持久化 base commit（fork-point）

**问题**：v1 用 `base_ref="HEAD"` 创建和 diff，但 HEAD 是移动目标。main 前进后 `git diff HEAD...kanban/<id>` 不再表示"此卡引入的变更"。

**修正**：创建 worktree 时记录精确的 fork-point commit SHA。

```python
@dataclass
class WorktreeInfo:
    card_id: str
    path: Path          # worktree 目录（detach 后为 None）
    branch: str         # "kanban/<card_id>"
    base_commit: str    # 创建时的精确 commit SHA（不可变）
    head_commit: str    # 当前分支 HEAD
```

- `create()` 内部：`base_commit = git rev-parse HEAD`，然后 `git worktree add ... -b kanban/<id> <base_commit>`
- Card 持久化新增 `worktree_base_commit: str | None` 字段（TOML front-matter）
- `diff_summary()` 使用 `git diff <base_commit>...kanban/<card_id> --stat`

### 4. Reviewer rejection 是 BLOCKED 而非 loop-back（不改现有语义）

**问题**：v1 假设 reviewer 拒绝会让 card 回退到 DOING 并复用 worktree。但当前代码中 reviewer/verifier 的拒绝是 `FailureCategory.FUNCTIONAL`（`models.py:321`），retry budget = 0（`models.py:334`），直接进入 BLOCKED（`orchestrator.py:434`）。这是测试覆盖的核心行为（`test_retry_matrix.py:109`）。

**修正**：本设计**不改变现有 rejection → BLOCKED 语义**。Worktree 在 BLOCKED 时 detach（目录释放），分支保留以供人工审查和手动 unblock。

如果未来需要 reject-and-rework loop，那是一个独立 RFC，涉及：
- 新增 `FailureCategory.REWORK`（retry budget > 0）
- Reviewer executor contract 新增 `rework_hint` 字段
- Scheduler 的 `_retry_or_block` 对 REWORK 走 retry 路径
- 在该 RFC 中再处理 worktree 在 DOING↔REVIEW loop 中的保留

**本设计仅处理已有的 BLOCKED 终态**：detach worktree，保留分支。

### 5. worktree_path 的运行时注入路径

**问题**：v1 声称 `claim.worktree_path` 会"通过 card 对象自动携带"到 executor，但实际调用链中 `WorkerDaemon.run_once()`（`daemon.py:461-473`）只做 `card = store.get_card(claim.card_id)` 然后 `executor.run(claim.role, card)`，claim 对象不传给 executor。两个 executor 也只读 `self.working_directory`（`agentao_multi.py:81`，`multi_backend.py:251`）。

**修正**：不改 executor protocol 签名（保持 `run(role, card) → AgentResult`），改为在 **worker 调用 executor 前注入**：

```python
# daemon.py — WorkerDaemon.run_once() 修改
card = self.orchestrator.store.get_card(claim.card_id)

# NEW: 如果 claim 带 worktree_path，覆盖 executor 的 working_directory
if claim.worktree_path is not None:
    prev_wd = self.orchestrator.executor.working_directory
    self.orchestrator.executor.working_directory = Path(claim.worktree_path)
    try:
        result = self.orchestrator.executor.run(claim.role, card)
    finally:
        self.orchestrator.executor.working_directory = prev_wd
else:
    result = self.orchestrator.executor.run(claim.role, card)
```

**为什么不加 Card.worktree_path 字段？**
- Card 是业务模型，worktree path 是运行时基础设施细节，不应持久化到 card TOML
- Claim 已经是运行时对象，path 放在 claim 上语义正确
- Worker 是唯一持有 claim 引用的消费者，注入点明确

**为什么不改 executor protocol？**
- `CardExecutor.run(role, card)` 是公共接口，MockExecutor 和未来第三方 executor 不应被迫处理 worktree
- `working_directory` 已是 executor 实例属性，临时覆盖最小侵入

**线程安全**：当前 worker 是独立进程（每个 WorkerDaemon 持有自己的 executor 实例），临时覆盖 `working_directory` 不存在竞争。如果未来改为线程池 worker，需要引入 per-call context 或改 protocol。

### 6. 开关与配置（tri-state）

`--worktree` 是三态 flag：

| 传入 | 语义 | 非 git 仓库时的行为 |
|------|------|----------------------|
| *（不传）* | auto：探测到 git 仓库就启用 | stderr 打印一行 warning 后自动降级关闭 |
| `--worktree` | 显式要求启用 | `SystemExit` — "requires a Git repository" |
| `--no-worktree` | 显式关闭 | 无影响（本来就关） |

```bash
uv run kanban tick                      # auto：git repo 内默认启用
uv run kanban --no-worktree tick        # 显式关闭（回到 v0.1.3 行为）
uv run kanban --worktree tick           # 硬要求 git repo
```

**为什么是 tri-state 而不是简单 `default=True`**：`_find_git_root`
在非 git 仓库时会 `SystemExit`。若一刀切改成默认 True，任何在临时
scratch 目录下跑 mock executor 的用户都会被意外打爆。auto 让显式
用户享受 fail-fast，而非 git 用户享受静默降级。

---

## 新增组件

### A. `kanban/worktree.py` — WorktreeManager

单一职责：管理 Git worktree 的创建、查询、detach、prune。不涉及 kanban 业务逻辑。

```python
@dataclass
class WorktreeManager:
    project_root: Path          # 主仓库根目录（.git 所在目录）
    worktrees_root: Path        # workspace/worktrees/
    base_ref: str = "HEAD"      # 解析起点（实际记录的是解析后的 commit SHA）

    def create(self, card_id: str) -> WorktreeInfo:
        """
        1. base_commit = git rev-parse <base_ref>
        2. git worktree add workspace/worktrees/<card_id> -b kanban/<card_id> <base_commit>
        3. return WorktreeInfo(base_commit=base_commit, ...)
        """

    def get(self, card_id: str) -> WorktreeInfo | None:
        """查询已有 worktree，返回路径+分支+base_commit+HEAD commit"""

    def detach(self, card_id: str) -> None:
        """git worktree remove workspace/worktrees/<card_id> --force
        分支保留，仅释放目录。"""

    def prune_branch(self, card_id: str, *, force: bool = False) -> bool:
        """git branch -d (or -D if force) kanban/<card_id>
        Returns True if branch was deleted."""

    def prune_stale(
        self,
        card_statuses: dict[str, CardStatus],
        retention_days: int = 7,
    ) -> list[str]:
        """批量清理满足条件的已 detach 分支"""

    def diff_summary(self, card_id: str, base_commit: str) -> str:
        """git diff <base_commit>...kanban/<card_id> --stat
        调用方须提供 base_commit（从 card 字段读取）"""

    def list_active(self) -> list[WorktreeInfo]:
        """git worktree list --porcelain → 过滤 kanban/ 分支"""

@dataclass
class WorktreeInfo:
    card_id: str
    path: Path | None       # detach 后为 None
    branch: str             # "kanban/<card_id>"
    base_commit: str        # fork-point SHA（不可变）
    head_commit: str        # 当前分支 HEAD
```

底层全部调用 `subprocess.run(["git", ...], cwd=self.project_root)`，不依赖 gitpython。

### B. Model 扩展

**Card** (`kanban/models.py`):
```python
worktree_branch: str | None = None        # e.g. "kanban/abc123"
worktree_base_commit: str | None = None   # fork-point SHA, immutable once set
```

**ExecutionClaim** (`kanban/models.py`):
```python
worktree_path: str | None = None     # e.g. "workspace/worktrees/abc123"
```

**CardEvent** (`kanban/models.py`):
```python
worktree_branch: str | None = None   # present on worktree.* events
```

### C. 持久化扩展（allowlist 协调）

当前 CardEvent、ExecutionClaim 的序列化是显式 allowlist，新字段必须在所有 encode/decode 点同步添加。

**Card TOML** (`store_markdown.py:_card_to_toml_dict / _card_from_toml_dict`):
- `worktree_branch`: 可选 string
- `worktree_base_commit`: 可选 string

**Claim JSON** (`store_markdown.py:_claim_to_json / _claim_from_json`，当前 L893-924):
- `worktree_path`: 可选 string（encode 时 `if claim.worktree_path is not None`）

**Event JSON** (`store_markdown.py` event encode + `cli.py:_event_to_json` L858):
- `worktree_branch`: 加入 `_event_to_json` 的条件输出 block（与现有 `backend_metadata` 同级）

**Event text format** (`cli.py:_format_event_line` L886):
- worktree.* events 在 extras 中追加 `wt=kanban/<card_id>`

---

## 修改清单

### 1. `kanban/models.py`
- Card dataclass 增加 `worktree_branch: str | None = None`，`worktree_base_commit: str | None = None`
- ExecutionClaim dataclass 增加 `worktree_path: str | None = None`
- CardEvent dataclass 增加 `worktree_branch: str | None = None`

### 2. `kanban/worktree.py`（新文件）
- WorktreeManager + WorktreeInfo 实现

### 3. `kanban/orchestrator.py`
- `__init__` 接受可选 `worktree_mgr: WorktreeManager | None`
- `select_and_claim()`: 当 role == WORKER 且 card 无 `worktree_branch` 时，调用 `worktree_mgr.create(card_id)` 创建 worktree，将 branch + base_commit 写入 card、path 写入 claim
- `select_and_claim()`: 当 role in {WORKER(retry), REVIEWER, VERIFIER} 且 card 有 `worktree_branch` 时，用 `worktree_mgr.get()` 获取 path，写入 claim
- `_apply_result()`: 当 next_status == DONE 时，调用 `worktree_mgr.detach(card_id)`（**不删分支**）
- `_retry_or_block()`: 当 card 进入 BLOCKED 时，调用 `worktree_mgr.detach(card_id)`（**不删分支**）

### 4. `kanban/daemon.py`
- `WorkerDaemon.run_once()`（L461-473）: 在 `executor.run()` 前后临时覆盖 `executor.working_directory` 为 `claim.worktree_path`
- `SchedulerDaemon`: 在空闲 tick 调用 `worktree_mgr.prune_stale()` 清理过期分支
- daemon 需持有 `worktree_mgr` 引用（从 CLI 注入）

### 5. `kanban/store_markdown.py`
- `_card_to_toml_dict()` / `_card_from_toml_dict()`: 增加 `worktree_branch`、`worktree_base_commit`
- `_claim_to_json()` (L893) / `_claim_from_json()` (L910): 增加 `worktree_path`
- Event encode 路径：增加 `worktree_branch`

### 6. `kanban/cli.py`
- `_event_to_json()` (L858): 增加 `worktree_branch` 到条件输出
- `_format_event_line()` (L886): worktree events 追加 `wt=` extra
- 顶层 parser 增加 `--worktree / --no-worktree` flag
- 当 `--worktree` 时，构造 `WorktreeManager` 并注入 orchestrator + daemon
- 新增子命令：
  - `kanban worktree list` — 列出活跃 worktree（包括已 detach 但分支存在的）
  - `kanban worktree prune` — 手动触发过期分支清理
  - `kanban worktree diff <card_id>` — 使用持久化的 base_commit diff

### 7. 事件类型
- `worktree.created` — worktree 创建成功
- `worktree.detached` — worktree 目录释放，分支保留
- `worktree.pruned` — 分支删除

---

## 数据流图

```
Scheduler tick
│
├─ select_and_claim(card in READY)
│   ├─ role = WORKER
│   ├─ card.worktree_branch is None?
│   │   ├─ YES → worktree_mgr.create(card_id)
│   │   │         ├─ base_commit = git rev-parse HEAD
│   │   │         ├─ git worktree add ... -b kanban/<card_id> <base_commit>
│   │   │         ├─ card.worktree_branch = "kanban/<card_id>"
│   │   │         ├─ card.worktree_base_commit = base_commit
│   │   │         ├─ claim.worktree_path = "workspace/worktrees/<card_id>"
│   │   │         └─ emit worktree.created event
│   │   └─ NO  → worktree_mgr.get(card_id)  (retry 场景)
│   │             └─ claim.worktree_path = existing path
│   ├─ move card → DOING
│   └─ persist claim + card
│
├─ select_and_claim(card in REVIEW or VERIFY)
│   ├─ role = REVIEWER or VERIFIER
│   ├─ card.worktree_branch exists → worktree_mgr.get(card_id)
│   │   └─ claim.worktree_path = existing path
│   └─ persist claim
│
Worker acquires claim
│
├─ load card from store
├─ claim.worktree_path is not None?
│   ├─ YES → executor.working_directory = claim.worktree_path (临时覆盖)
│   └─ NO  → use default self.working_directory
├─ executor.run(role, card)
├─ restore executor.working_directory
├─ submit result envelope
│
Scheduler commit_pending_results
│
├─ _apply_result()
│   ├─ next_status = REVIEW → worktree preserved
│   ├─ next_status = VERIFY → worktree preserved
│   ├─ next_status = DONE   → worktree_mgr.detach(card_id)
│   │                         emit worktree.detached
│   │                         (branch preserved for merge/PR)
│   └─ (reviewer/verifier rejection → BLOCKED, see below)
│
├─ _retry_or_block()
│   ├─ retry budget remaining → new claim, worktree preserved
│   └─ budget exhausted → BLOCKED
│       → worktree_mgr.detach(card_id)
│       → emit worktree.detached
│       → (branch preserved for manual review)
│
Scheduler (idle ticks)
│
└─ prune_stale(): for each detached branch:
    ├─ DONE + merged to base → git branch -d (safe delete)
    └─ BLOCKED + age > retention_days → git branch -D (force delete)
```

---

## 边界情况处理

| 场景 | 处理方式 |
|------|----------|
| Worker crash mid-execution | Worktree 保留；claim 超时后 scheduler 重试，新 claim 复用同一 worktree |
| Daemon SIGTERM | Worktree 不在 shutdown 路径清理；下次启动 prune_stale 处理 |
| 卡有 depends_on，依赖卡的产出需要可见 | Worktree 基于 `base_commit` 创建；如果依赖卡已 merge 到 base_commit 之前的 main，其改动自然包含 |
| Worktree 创建失败（磁盘满、git 异常） | Card 直接 BLOCKED，failure_category = INFRASTRUCTURE |
| 主仓库非 git repo（如纯 workspace 场景） | `--worktree` flag 启动时检查 `.git` 存在，不存在则报错退出 |
| 并发 worker 抢同一张卡 | CAS claim 机制已保证唯一，worktree 创建在 scheduler（单线程），无竞争 |
| Reviewer/Verifier 需要运行测试 | 在同一 worktree 中执行，与 worker 共享文件系统状态 |
| DONE 卡的分支何时删除 | prune_stale 仅在 `git branch --merged` 确认后 `-d` 删除 |
| BLOCKED 卡的分支何时删除 | prune_stale 在超过 retention_days (默认 7) 后 `-D` 强制删除 |
| base_commit 在创建后 main 前进 | 无影响：diff 始终用 card.worktree_base_commit 而非 HEAD |
| 未来多线程 worker 共享 executor | 需改为 per-call context 或 ContextVar；当前单进程 worker 无此问题 |

---

## Reject-and-Rework (Out of Scope)

当前 reviewer/verifier rejection 的语义是 `FailureCategory.FUNCTIONAL` → retry budget 0 → 直接 BLOCKED（`orchestrator.py:403-439`，`models.py:321,334`，`test_retry_matrix.py:109`）。

如果未来需要 reject → rework loop（卡从 REVIEW 回到 DOING 而不 BLOCK），需要独立 RFC 涉及：

1. **新 FailureCategory**: `REWORK`（budget > 0，e.g. 1-2 次）
2. **Reviewer contract 扩展**: `AgentResult` 新增 `rework_hint: str | None`（告诉 worker 具体要改什么）
3. **Scheduler 语义**: `_retry_or_block()` 对 REWORK 走 retry 路径，但目标状态是 DOING 而非重新 claim 同角色
4. **Worktree 保留**: rework retry 复用已有 worktree（此时才需要 v1 中描述的 "rejection 保留" 行为）
5. **测试更新**: `test_retry_matrix.py` 等需要覆盖 REWORK 路径

本设计不预设此行为，仅处理 BLOCKED 终态下的 worktree detach + 分支保留。

---

## 测试验证

1. **单元测试** (`tests/test_worktree.py`):
   - `WorktreeManager.create`: 验证 worktree 目录存在、分支名正确、base_commit 是 SHA 而非 ref
   - `WorktreeManager.get`: 查询存在/不存在的 worktree
   - `WorktreeManager.detach`: 目录释放但分支存在
   - `WorktreeManager.prune_branch`: `-d` 对已 merge 分支成功、对未 merge 分支失败；`force=True` 时成功
   - `WorktreeManager.prune_stale`: 批量清理逻辑
   - `WorktreeManager.diff_summary`: 使用 base_commit 而非 HEAD

2. **集成测试** (`tests/test_worktree_integration.py`):
   - 完整 card 生命周期：add → tick(planner, no worktree) → tick(worker, worktree created) → tick(reviewer) → tick(verifier) → DONE → worktree detached, branch exists
   - Worker retry (mock infra failure) → 新 claim 复用同一 worktree
   - Reviewer rejection → BLOCKED → worktree detached, branch preserved
   - prune_stale 对 DONE+merged 分支执行 -d 成功

3. **Serialization round-trip 测试**:
   - Card TOML 含 `worktree_branch` + `worktree_base_commit` 字段的 encode/decode
   - Claim JSON 含 `worktree_path` 字段的 encode/decode
   - CardEvent JSON 含 `worktree_branch` 字段通过 `_event_to_json` 输出
   - `_format_event_line` 对 worktree.* events 包含 `wt=` extra

4. **Working directory injection 测试**:
   - WorkerDaemon with claim.worktree_path → executor.working_directory 被临时覆盖
   - WorkerDaemon with claim.worktree_path=None → executor.working_directory 不变
   - Executor 异常时 working_directory 恢复到原值

5. **手动验证**（在 git 仓库内跑，auto 默认生效）:
   ```bash
   uv run kanban card add --title "Test" --goal "Write hello.py"
   uv run kanban tick              # planner (no worktree created)
   uv run kanban tick              # worker (worktree created)
   uv run kanban worktree list     # should show 1 active
   uv run kanban worktree diff <card_id>
   uv run kanban tick              # reviewer
   uv run kanban tick              # verifier
   uv run kanban worktree list     # should show 0 active worktrees, 1 detached branch
   # after merge:
   uv run kanban worktree prune    # branch deleted
   ```
