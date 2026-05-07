# 安装路径详解

按你想跑哪个 executor 挑一条路径。**不要混装**:ACP 路径依赖外部 CLI 和
API key,而纯 mock 路径什么都不需要。

如果只想看怎么 5 分钟跑起来,先回 [README 的安装段](../README.md#安装)。

## 先决条件(所有模式共用)

- Python ≥ 3.12
- [`uv`](https://docs.astral.sh/uv/) (包管理)
- Git clone 后在仓库根目录 `uv sync` 一次

```bash
git clone https://github.com/jin-bo/agentao-kanban.git
cd agentao-kanban
uv sync
```

> `agentao` 已发布到 PyPI(≥ 0.4.2),`uv sync` 默认直接从 PyPI 拉,
> 不需要同级 `../agentao` 源码。只有同时在改 agentao 的开发者才需要在
> 当前虚拟环境里手动覆盖为本地 editable:
>
> ```bash
> uv sync
> uv pip install --editable ../agentao
> ```
>
> `uv` 0.11.x 不再接受把项目依赖 `sources` 写进 `uv.toml`。上面的做法只
> 覆盖当前 `.venv`,不会改仓库里的 `pyproject.toml`/`uv.lock`。

## 路径 A:纯 mock 模式(默认,推荐首次上手)

`MockAgentaoExecutor` 是离线状态机,不调用任何 LLM,也不读 `.agentao/`。
装完 `uv sync` 即可跑:

```bash
uv run python main.py                           # 内存态演示,最短闭环
uv run kanban card add --title T --goal G --priority HIGH
uv run kanban list
uv run kanban run                               # 跑到 idle
uv run kanban daemon                            # 前台 dispatcher,Ctrl-C 退出
uv run kanban daemon --once --poll-interval 0.2 # 单轮调试
uv run kanban daemon --detach                   # 后台,日志写 <board>/daemon.log
```

到这一步就完了,不要继续装下面的东西。

## 路径 B:agentao sub-agent 模式

用 `--executor agentao` 调四个本地 sub-agent(planner / worker / reviewer /
verifier)。额外要求:

1. `agentao` 包可用。`uv sync` 已经从 PyPI 装好(≥ 0.4.2);只有同时在改
   agentao 源码的开发者才需要按先决条件里的说明再执行一次
   `uv pip install --editable ../agentao` 覆盖当前 `.venv`。
2. sub-agent 定义:默认从 `kanban/defaults/*.md` 读,仓库已打包。若要本地
   改 prompt:

   ```bash
   mkdir -p .agentao/agents
   cp kanban/defaults/*.md .agentao/agents/
   ```

   `.agentao/` 已 `.gitignore`,不会被提交。

3. 跑:

   ```bash
   uv run kanban --executor agentao run
   uv run kanban --executor agentao daemon
   ```

## 路径 C:multi-backend + ACP 远端模式

`--executor multi-backend` 走 profile 路由,默认每个角色仍落到 `subagent`
backend(等同路径 B)。**只有**把卡上 `agent_profile` 手动 pin 到 `gemini-*`
profile,或让 router 选中,才会真正调 ACP 后端。此时额外要求:

1. 准备 ACP 服务器配置。仓库的 `docs/acp.sample.json` 是样板,拷到运行
   目录:

   ```bash
   mkdir -p .agentao
   cp docs/acp.sample.json .agentao/acp.json
   ```

   默认样板里的 `gemini-worker` / `gemini-reviewer` 都是 `npx
   @google/gemini-cli@latest --acp`。

2. 准备外部 CLI 与凭据:

   - Node.js + `npx` 可用(让 `npx @google/gemini-cli@latest` 可跑)。
   - 导出 `GEMINI_API_KEY`(`acp.json` 里的 `env` 用 `$GEMINI_API_KEY`
     占位符,进程启动时展开)。

   ```bash
   export GEMINI_API_KEY=...
   npx @google/gemini-cli@latest --version   # 自检
   ```

3. (可选)调整 profile 路由。默认配置 `kanban/defaults/agent_profiles.yaml`
   已经声明了 `gemini-worker` / `gemini-reviewer` profile 但**不**作为默认;
   要按项目改,复制一份本地覆盖:

   ```bash
   mkdir -p .kanban
   cp docs/agent_profiles.sample.yaml .kanban/agent_profiles.yaml
   ```

4. 跑:

   ```bash
   uv run kanban --executor multi-backend daemon
   ```

   若一张卡被路由到 ACP profile 但 `.agentao/acp.json` 缺失 / 外部命令
   起不来,该卡会以 `FailureCategory=infrastructure` 进入重试/BLOCKED;
   其它仍走 subagent 的卡不受影响。

设计细节:[`agent-router-design.md`](agent-router-design.md)、
[`agent-profile-acp-design.md`](agent-profile-acp-design.md)。

## 自检

装完推荐跑一次:

```bash
uv run pytest -q                                # 单元+集成测试
uv run kanban doctor                            # 板健康体检
```
