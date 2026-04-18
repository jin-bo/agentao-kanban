# MCP Server: Expose kanban board to MCP clients

## Context

Kanban 目前只能通过 `uv run kanban …` CLI 操作。要把它当成 Claude Code / Codex / 任意 MCP client 的"调度中心",必须以 MCP 协议暴露同一套能力。本方案新增一个 stdio MCP server (`kanban-mcp`),把 card CRUD、events.log 尾流、orchestrator 触发、board 快照原样映射成 MCP tools / resources,直接调用 `BoardStore` API(不走 subprocess),并复用 CLI 已有的 daemon-lock 守卫语义,确保和 daemon、CLI 三者并存时不破坏数据一致性。

## Architecture

- **传输**: stdio,每个 client 一个进程。Claude Code 通过 `claude mcp add` 注册;Codex 通过其 `mcp_servers` 配置启动同一个二进制。
- **SDK**: 官方 `mcp` Python SDK 的 `FastMCP`(`from mcp.server.fastmcp import FastMCP`)。装饰器风格,自动从类型注解生成 JSONSchema。
- **后端调用**: 进程内构造一个 `MarkdownBoardStore(board_dir)`,所有 tool 直接调它,**不走 subprocess**。这样可以把异常映射成 MCP tool error,且无需重新解析 stdout。
- **板路径来源**: argv `--board` > env `KANBAN_BOARD` > 默认 `workspace/board`(与 CLI 行为一致)。
- **写守卫**: 写类 tool 在调 store 前先 `assert_no_daemon(board_dir)`(`kanban/daemon.py:146`)。`DaemonLockError` 直接变成 tool error,信息里复刻 CLI 提示("Stop the daemon or restart MCP with --force")。
- **force 模式**: server 启动时可加 `--force` 一次性禁用守卫(对应 CLI `--force`,仅做 recovery)。**不做 per-tool force 参数**,避免 client 误用。

## File changes

新增:
- `kanban/mcp.py` — MCP server 入口,所有 tool / resource / `main()`。
- `tests/test_mcp_server.py` — 单元测试,直接构造 `FastMCP` 后用 `call_tool` / `read_resource` in-process 调,断言行为。

修改:
- `pyproject.toml`:
  - `dependencies` 加 `mcp>=1.0`
  - `[project.scripts]` 加 `kanban-mcp = "kanban.mcp:main"`
- `README.md` — 加 "Use as MCP server" 段(注册命令 + tool 列表)。
- `CHANGELOG.md` — 记一行。

不动:`kanban/cli.py`、`kanban/store_markdown.py`、`kanban/daemon.py`、模型层 — MCP 是新外壳,不改既有契约。

## MCP surface

### Tools (写 + 触发)

| Tool 名 | 参数 | 调用 | 说明 |
|---|---|---|---|
| `card_add` | `title:str, goal:str, priority="MEDIUM", acceptance:list[str]=[], depends:list[str]=[]` | `store.add_card(Card(...))` | 复刻 `cmd_card_add` (`kanban/cli.py:1006`)。返回 card dict。 |
| `card_move` | `card_id:str, status:str` | `store.move_card(card_id, CardStatus(status), note)` | `status` 校验走 `CardStatus(...)`。返回 card dict。 |
| `card_block` | `card_id:str, reason:str` | `store.update_card` + `move_card` 到 `BLOCKED` | 复刻 `cmd_block` (`kanban/cli.py:1071`)。 |
| `card_unblock` | `card_id:str, to:str="INBOX"` | 清 `blocked_reason` + `move_card` | 复刻 `cmd_unblock` (`kanban/cli.py:1085`)。 |
| `tick` | — | `KanbanOrchestrator(store, executor).tick()` | 写守卫;executor 默认 mock(server 启动 flag 决定)。 |
| `run` | `max_steps:int=100` | `orchestrator.run_until_idle(max_steps)` | 写守卫;返回执行步数。 |
| `card_list` | `status:str|None=None` | `store.list_by_status` 或 `list_cards` | 读,无守卫。返回 `[card_dict]`。 |
| `card_show` | `card_id:str` | `store.get_card(card_id)` | 读,`KeyError → ToolError "card not found"`。 |
| `events_tail` | `limit:int=50, card_id:str|None=None, role:str|None=None, execution_only:bool=False` | 选择性调 `list_events` / `list_execution_events` | 复用 `BoardStore` 现成方法(`kanban/store.py:60-67`)。 |

### Resources (读快照,可被 client 缓存)

| URI | 内容 | 调用 |
|---|---|---|
| `kanban://board/snapshot` | `{status: [titles]}` JSON | `store.board_snapshot()` |
| `kanban://card/{card_id}` | 单卡完整 dict | `store.get_card(card_id)` |
| `kanban://events/recent?limit=N` | 最近 N 条事件 JSON 数组 | `store.list_events(limit=N)` |

### 序列化

`Card` / `CardEvent` 都是 `@dataclass(slots=True)`(`kanban/models.py:150,246`)。在 `kanban/mcp.py` 写两个小 helper:
```python
def _card_to_dict(c: Card) -> dict: ...   # 含 datetime → ISO8601
def _event_to_dict(e: CardEvent) -> dict: ...
```
不动 `cli.py` 的 `--json` 路径,以防互相耦合。

## Lock policy & error mapping

| 异常 | 触发场景 | MCP 响应 |
|---|---|---|
| `DaemonLockError` (`kanban/daemon.py:40`) | 写类 tool + daemon 在跑 | tool error,文案复刻 CLI |
| `KeyError` | `get_card` / `move_card` 找不到 id | tool error "card {id} not found" |
| `ValueError` | `CardStatus("BAD")` / 依赖环 | tool error,原文案 |
| 其它 `Exception` | 未预期 | tool error,带 traceback 摘要 |

读类 tool / resource **不**调 `assert_no_daemon`,daemon 写盘是原子的(逐文件 + JSONL append),并发读不会脏读。

## Dependencies & registration

```toml
# pyproject.toml
dependencies = ["agentao", "pyyaml>=6.0", "mcp>=1.0"]

[project.scripts]
kanban     = "kanban.cli:main"
kanban-mcp = "kanban.mcp:main"
```

`uv sync` 后,Claude Code 注册:
```bash
claude mcp add kanban --scope project -- uv run --directory /path/to/agentao-kanban kanban-mcp --board workspace/board
```

Codex / 其它 client 同理,只改命令前缀。

## Verification

1. **单元测试** (`tests/test_mcp_server.py`,`uv run pytest tests/test_mcp_server.py`):
   - `card_add` → `card_show` → `card_move(READY)` → 状态转移正确
   - `card_block` 后 `card_show` 看到 `blocked_reason`,`card_unblock(to=DOING)` 正确清空
   - `events_tail(limit=3, execution_only=True)` 只返回 execution 事件
   - 启动一个假的 daemon 锁文件(直接写 `.daemon.lock` + 当前 pid),`card_add` 抛 `DaemonLockError` → MCP tool error
   - `kanban://board/snapshot` resource 返回 `dict[str, list[str]]`

2. **手动联调**:
   ```bash
   uv sync
   uv run kanban card add --title smoke --goal smoke   # 准备一张卡
   claude mcp add kanban-local -- uv run kanban-mcp --board workspace/board
   ```
   在 Claude Code 里:`/mcp` 看到 kanban-local;让它调 `card_list`、`card_show <id>`、`events_tail limit=10`,核对输出和 `uv run kanban list / show / events.log` 一致。

3. **lock 守卫端到端**:
   ```bash
   uv run kanban daemon &
   # MCP 里调 card_add → 应得到 tool error,文案含 "Daemon (pid=...) is running"
   kill %1
   # 再调 card_add → 成功
   ```

## Out of scope (本期不做)

- HTTP/SSE transport(用户已确认 stdio 即可)。
- `daemon start/stop` MCP tool — daemon 是长进程,不适合 MCP request/response;client 自己用 shell。
- v0.1.2 runtime surface(claims / results / workers)— 暴露给外部 client 风险高,留给后续。
- per-tool `force` 参数 — 仅在启动 flag 提供。
- Resource subscriptions(实时事件推送)— stdio 下 FastMCP 支持有限,先用 `events_tail` 轮询。
