# Kanban Web 只读看板实现计划

> Status: implemented in v0.1.5. This document is retained as the design and
> release checklist for the read-only `kanban web` board.

## Context

现有项目为纯 CLI + MCP 的 Python 看板系统。需要新增只读 Web 服务，供本地/内网实时观察看板状态，不引入写操作、不改现有 CLI/MCP 语义。

---

## 代码库关键发现（探索结果）

- **CLI**: `kanban/cli.py` 使用 **argparse**（非 Click），全局 `--board` 选项已存在，dispatch dict 在 `main()` 末尾（约 1707 行）
- **序列化**: `card_to_dict()` 和 `event_to_dict()` 已在 `kanban/mcp.py:77` 和 `:109` 定义，直接复用
- **存储读取**: `store.list_cards()`, `store.list_events(limit=N)`, `store.list_execution_events(card_id, role, limit)`, `store.list_claims()`, `store.list_workers()`, `store.refresh()` 均已存在
- **`board_snapshot()`** 只返回 `{status: [title]}` 最简结构，不适合前端，需自行聚合
- **构建系统**: hatchling，包数据配置在 `[tool.hatch.build.targets.wheel]` 的 `include` 列表
- **测试模式**: 无 TestClient 使用先例；MCP 测试直接调纯函数；CLI 测试调 `main()`
- **FastAPI 同步处理**: 路由 handler 用 `def`（非 `async def`）时 FastAPI 自动放入线程池，**无需** `anyio.to_thread.run_sync()`

---

## 修改文件

| 文件 | 操作 |
|---|---|
| `kanban/web.py` | 新建 — FastAPI app + service 层 |
| `kanban/web_assets/index.html` | 新建 — 单页看板 |
| `kanban/web_assets/app.js` | 新建 — 轮询 + 渲染逻辑 |
| `kanban/web_assets/styles.css` | 新建 — 样式 |
| `kanban/cli.py` | 新增 `web` 子命令（argparse + dispatch） |
| `pyproject.toml` | 增加依赖 + hatchling include |
| `README.md` | 新增章节 |

---

## 实现细节

### 1. 依赖与打包（pyproject.toml）

```toml
[project.dependencies]
# 追加：
"fastapi>=0.110",
"uvicorn[standard]>=0.29",

[dependency-groups]
dev = ["pytest>=9.0.3", "httpx>=0.27"]  # httpx for TestClient

[tool.hatch.build.targets.wheel]
include = [
    # 现有条目保留，追加：
    "kanban/web_assets/**",
]
# sdist 同理追加
```

### 2. kanban/web.py — 整体结构

```python
from contextlib import asynccontextmanager
from importlib import resources
import fastapi, uvicorn
from kanban.store_markdown import MarkdownBoardStore
from kanban.mcp import card_to_dict, event_to_dict   # 直接复用
from kanban.models import CardStatus, AgentRole

COLUMN_TITLES = {
    CardStatus.INBOX: "Inbox", CardStatus.READY: "Ready",
    CardStatus.DOING: "Doing", CardStatus.REVIEW: "Review",
    CardStatus.VERIFY: "Verify", CardStatus.DONE: "Done",
    CardStatus.BLOCKED: "Blocked",
}

@asynccontextmanager
async def lifespan(app):
    app.state.store = MarkdownBoardStore(app.state.board_dir)
    yield

def create_app(board_dir, poll_interval_ms=5000):
    app = fastapi.FastAPI(lifespan=lifespan)
    app.state.board_dir = board_dir
    app.state.poll_interval_ms = poll_interval_ms
    # 注册路由…
    return app

def main(board_dir, host="127.0.0.1", port=8000, poll_interval_ms=5000):
    app = create_app(board_dir, poll_interval_ms)
    uvicorn.run(app, host=host, port=port)
```

**关键设计决策**：
- handler 全部用同步 `def`，FastAPI 自动放入线程池，无需 anyio
- lifespan 创建 store 单例；每个 handler 调用 `store.refresh()` 后再读，保证外部写入可见
- runtime 目录缺失时 `list_claims()`/`list_workers()` 统一返回空列表（store 已处理），不需要额外 try/except

### 3. HTTP API

**GET /healthz**
```json
{"status": "ok", "board_dir": "workspace/board"}
```

**GET /api/board**
```json
{
  "generated_at": "<iso>",
  "poll_interval_ms": 5000,
  "columns": [
    {"status": "inbox", "title": "Inbox", "count": 2,
     "cards": [{"id":…, "title":…, "status":…, "priority":…,
                "owner_role":…, "blocked_reason":…, "updated_at":…,
                "depends_on":…, "rework_iteration":…, "agent_profile":…}]}
  ],
  "recent_events": [...],
  "runtime": {"claims": [...], "workers": [...]}
}
```
列顺序固定按 `CardStatus` 枚举值顺序（INBOX→BLOCKED）。
聚合：`store.list_cards()` 按 status 分组；`store.list_events(limit=20)` 取最近事件；`store.list_claims()` 和 `store.list_workers()` 取运行时概览。

**GET /api/cards/{card_id}**
- 复用 `card_to_dict(store.get_card(card_id))`
- 追加 `recent_events: [event_to_dict(e) for e in store.events_for_card(card_id)[-20:]]`
- 未知卡返回 404

**GET /api/events**
参数：`limit=50`, `card_id`, `role`, `execution_only`
- `execution_only=true` → `store.list_execution_events(card_id=…, role=…, limit=…)`
- 否则 → `store.list_events(limit=…)` + 手动过滤 card_id/role
- 每条事件 `event_to_dict(e)` + 追加 `display_tag` 字段（优先 event_type，其次 role，兜底 "info"）

**GET / 和静态资源**
```python
@app.get("/", response_class=HTMLResponse)
def index():
    ref = resources.files("kanban.web_assets").joinpath("index.html")
    return ref.read_text()

app.mount("/static", StaticFiles(directory=str(
    resources.files("kanban.web_assets"))), name="static")
```

### 4. kanban/cli.py 修改

在 `build_parser()` 中注册（argparse 风格）：
```python
web_p = sub.add_parser("web", help="Run read-only web board")
web_p.add_argument("--host", default="127.0.0.1")
web_p.add_argument("--port", type=int, default=8000)
web_p.add_argument("--poll-interval-ms", type=int, default=5000, dest="poll_interval_ms")
```

在 `main()` dispatch dict 追加：
```python
"web": cmd_web,
```

Handler（注意：web 命令不检查 daemon lock，只读，不需要 `_require_writable`）：
```python
def cmd_web(args: argparse.Namespace) -> int:
    from kanban.web import main as web_main
    web_main(args.board, host=args.host, port=args.port, poll_interval_ms=args.poll_interval_ms)
    return 0
```

### 5. 前端（kanban/web_assets/）

- **index.html**: 骨架 + 引用 `/static/styles.css` + `/static/app.js`，内嵌 `window.KANBAN_CONFIG = {pollIntervalMs: __POLL_INTERVAL_MS__}` 由后端注入
- **app.js**: `fetchBoard()` → 渲染 7 列 + 事件尾 → `setInterval(fetchBoard, config.pollIntervalMs)`；点击卡片 `fetchCard(id)` → 更新详情侧栏；原生 `fetch` + DOM 操作，无框架依赖
- **styles.css**: 7 列 CSS Grid（桌面）+ 纵向 flex（移动，`max-width: 768px`）；详情侧栏桌面右侧固定，移动端 inline 展开

**轮询模型**：
- 主看板：固定轮询 `/api/board`，间隔 `pollIntervalMs`（后端注入，默认 5s）
- 详情：打开后每次主轮询同时请求 `/api/cards/{id}`，不额外增加频率

---

## 交付顺序

1. pyproject.toml 增依赖；cli.py 新增 `web` 子命令；web.py 跑出空 FastAPI + `/healthz`
2. `/api/events`（复用 event_to_dict，最简只读路径）
3. `/api/cards/{card_id}`（card_to_dict + events_for_card）
4. `/api/board`（列聚合 + runtime 概览）
5. 静态资源 + 页面轮询
6. 测试 + README + hatchling include

---

## 测试计划

```python
# 使用 fastapi.testclient.TestClient
from fastapi.testclient import TestClient
from kanban.web import create_app

@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / "board")
    return TestClient(app)

def test_healthz(client): assert client.get("/healthz").status_code == 200
def test_board_7_columns(client): ...  # 验证 len(data["columns"]) == 7
def test_card_detail_404(client): assert client.get("/api/cards/bad").status_code == 404
def test_events_execution_only(client): ...
def test_runtime_missing_returns_empty(client): ...  # runtime/ 不存在时
```

---

## 假设与范围限制

- 首版无缓存、无鉴权、无 SSE/WebSocket、无搜索/筛选/分页
- `--host 0.0.0.0` 允许内网暴露，不追加鉴权
- 移动端降级为纵向列列表 + inline 详情展开（非全屏抽屉）
- 不新增独立 `kanban-web` script 入口，统一用 `kanban web`
