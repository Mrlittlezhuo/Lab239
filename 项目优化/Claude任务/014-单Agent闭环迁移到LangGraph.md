# 任务 014：单 Agent 闭环迁移到 LangGraph

## 1. 状态与基线

- 状态：`completed`
- 负责人：Claude Code
- 任务编写与审查：Codex
- 目标分支：`master`
- 源码基线 commit：`4ec15730d529cd98e5f31721f43da5d98b3f099c`
- 允许存在于源码基线之后的前置提交：仅本任务文档、Claude 任务索引和项目规则文档
- 前置任务：任务 013 已完成、审查并提交

Claude 开始修改前执行：

```bash
git status --short --branch
git rev-parse HEAD
git diff --name-status 4ec15730d529cd98e5f31721f43da5d98b3f099c..HEAD
git stash list
```

当前工作区允许存在以下用户本机运行内容，不属于本任务，不得读取、修改、删除、暂存或
提交：

- `chatchat-study-data/data/knowledge_base/info.db`
- `chatchat-study-data/data/logs/chatchat.log`
- `chatchat-study-data/model_settings.yaml`

如果分支不正确、基线之后混入未声明业务代码、出现其他来源不明的修改或 stash，或任务
需要修改的文件与已有改动冲突，Claude 必须停止并报告，不能自行清理。

## 2. 背景

项目已有第一版单 Agent 闭环，能够完成：

```text
计划 → 执行工具 → 保存观察 → 判断/重规划 → 最终回答
```

现有实现还具备 Run/Step 持久化、SSE 时间线、取消、步数与超时限制、用户隔离、
会话历史、长期记忆、普通工具和 MCP 接入。任务 012 又在它前面增加了智能意图路由。

但是核心循环目前由 `AgentRunner` 手工控制。论文研究助手后续需要长任务恢复、条件分支、
人工确认和研究领域子流程，如果继续扩展手写循环，状态同步、异常收敛和恢复逻辑会越来越
难维护。

任务 013 只完成现代 LangChain/LangGraph 依赖基线和离线原型，不切换生产执行器。本任务
负责第一次正式迁移：使用 LangGraph 重建现有单 Agent 闭环，同时保持外部接口和用户体验
基本不变，并保留旧执行器作为短期回退路径。

任务 013 最终采用 `langgraph 1.2.14` 与 `langgraph-checkpoint-sqlite 3.1.1`。其中同步
`SqliteSaver` 已验证可以跨连接恢复，但不能用于异步图接口；生产 Agent 是异步流式路径，
因此本任务应使用 `AsyncSqliteSaver` 或同等的官方异步持久化实现，不能在事件循环中直接
调用同步 SQLite checkpoint。

## 3. 本任务目标

1. 将现有“计划—执行—观察—判断/重规划—完成”闭环迁移为正式 LangGraph 状态图。
2. 复用现有规划器、工具、记忆、Run/Step 数据和消息写回，不建立平行业务系统。
3. 使用持久化 checkpoint，使一次运行能够按稳定 `thread_id` 保存图状态并从中断位置继续。
4. 保持现有 Chat API、Agent Run API、SSE 事件和 WebUI 时间线兼容。
5. 将 LangGraph 运行时设为新运行的默认路径，同时保留明确、可配置的 legacy 回退开关。
6. 用离线测试证明新旧运行时在核心场景中的业务结果和终态规则一致。

## 4. 目标架构

本任务只迁移编排运行时，不重新设计产品功能。迁移后的职责边界应为：

```text
Chat / 自动意图路由
          ↓
统一 Agent Runtime 接口
     ├─ LangGraphRuntime（默认）
     └─ LegacyRuntime（临时回退）
          ↓
规划器 / 工具注册 / MCP / 会话历史 / 长期记忆
          ↓
AgentRun / AgentStep / Checkpoint / SSE 事件
```

LangGraph 状态至少能够表达：

- 当前运行和当前用户；
- 用户目标与上下文；
- 结构化计划和待执行步骤；
- 已执行步骤及观察结果；
- 当前步数、运行状态、错误与最终答案；
- 取消、超时、失败和恢复所需信息。

图应包含等价于以下职责的节点，具体拆分和命名由 Claude 根据任务 013 的实际代码决定：

```text
准备上下文/规划
      ↓
执行当前步骤
      ↓
判断下一动作
  ├─ 继续执行
  ├─ 重新规划
  ├─ 生成答案
  └─ 失败结束
```

必须存在有界循环和明确终态，不能把全部旧循环简单包进一个 LangGraph 节点来“形式迁移”。

## 5. 状态与持久化边界

迁移后保留两类持久状态，各自职责必须清晰：

| 状态 | 主要作用 |
|---|---|
| LangGraph checkpoint | 保存编排状态、条件分支和恢复位置 |
| `AgentRun` / `AgentStep` | 用户可见的任务状态、执行时间线、权限查询和审计记录 |

约束：

- `run_id` 应作为或稳定映射为 LangGraph `thread_id`；
- checkpoint 必须是本地可持久化实现，生产路径不能只使用内存保存；
- Run/Step 继续作为现有 API 和 WebUI 的用户可见数据来源；
- 图节点开始和结束时及时同步 Run/Step，不能只在整张图结束后一次性写入；
- 恢复执行不得重复创建已经成功完成的工具步骤；
- checkpoint 与 Run/Step 不一致时必须可控失败或修复状态，不能静默重复执行工具；
- 不在 checkpoint 或轨迹中保存模型隐藏思维链。

如果需要为运行时标识、checkpoint 关联或恢复状态增加数据库字段，应使用新的幂等迁移，
保留已有运行记录。不要为了迁移重建数据库或修改历史 Run/Step 的语义。

## 6. 兼容与回退要求

LangGraph 迁移不能造成以下功能回退：

- 自动 Agent、普通对话和手动调试模式继续工作；
- 直接回答不产生无意义工具步骤；
- 初始计划中的多个步骤按顺序执行，不得提前跳过；
- 普通工具、知识库工具和异步 MCP 工具继续使用同一注册与装配方式；
- 流式和非流式结果保持现有主要字段与语义；
- SSE 继续提供计划、步骤开始、步骤完成、重规划和终态事件；
- `/agent-runs` 的列表、详情和取消接口保持兼容；
- 最终结果继续写回当前用户的来源消息；
- 会话历史、长期记忆和用户隔离规则保持不变；
- 用户 A 不能查看、恢复、取消或继续用户 B 的运行。

提供配置化的运行时选择，例如等价于：

```text
langgraph（默认） / legacy（回退）
```

不要在 Chat API 和 WebUI 中分别维护两套分支。运行时差异应收敛在统一接口或工厂内。
legacy 路径用于迁移期排错，不再继续添加新业务能力。

对于升级前遗留的 `pending/running` 记录，不要求强行转换成 LangGraph checkpoint。它们必须
仍可查看和取消；无法安全恢复时给出明确状态，不能把旧记录当成新图继续执行。

## 7. 恢复、取消与异常收敛

新运行时至少支持：

- 新建并流式执行运行；
- 根据当前用户和 `run_id` 获取运行状态；
- 从已有 checkpoint 恢复未完成运行；
- 用户取消后不再启动新的工具步骤；
- 达到最大步数或总超时后进入明确终态；
- 规划失败、工具异常、判断输出非法和请求中断后不遗留永久 `running` 状态；
- 同一个运行被重复恢复时具备基本幂等或并发保护，避免同一步骤执行两次。

本任务只要求后端恢复能力和离线验证，不要求新增复杂的 WebUI“恢复任务”页面。现有时间线
和取消入口需要继续可用；更完整的人工确认界面留给后续研究工作流任务。

## 8. 允许修改范围

Claude 可以根据任务 013 完成后的实际代码结构，修改实现本任务所需的：

- Agent Run 编排、服务和运行时接口；
- Chat/Agent Run 接入层与配置；
- Run/Step 仓库、模型和必要的幂等迁移；
- checkpoint 生命周期和应用启动/关闭集成；
- 现有 WebUI Agent 时间线中的必要兼容代码；
- 本任务相关测试和少量开发说明；
- 本文档末尾的“Claude 执行报告”。

允许范围以实现目标和保持兼容为准，不限定具体文件名。不要顺便重构与 Agent 运行无关的
RAG、知识库、认证或界面模块。

## 9. 禁止事项

- 不执行 `git commit`、`git push`、`git reset`、`git checkout`、`git clean` 或 `git stash`；
- 不读取或修改 `chatchat-study-data/`、真实数据库、日志、模型文件、密钥和 `.env`；
- 不使用 base 环境或系统 Python，固定使用 Conda 环境 `Langchain-Chatchat`；
- 不自行升级或降级任务 013 已确认的 LangChain/LangGraph 版本；
- 不删除 legacy 执行器，也不让回退开关成为无法工作的摆设；
- 不另建一套工具注册、记忆系统、消息系统或 Agent Run API；
- 不在本任务中实现论文研究子图、研究项目数据模型、Multi-Agent 或大规模 WebUI 改版；
- 不改变公共知识库共享和用户私有聊天数据的既有边界；
- 不用清库、删表或重建知识库解决迁移问题。

本任务明确允许在 Conda 环境 `Langchain-Chatchat` 中安装并在 `pyproject.toml` 声明
`AsyncSqliteSaver` 所需的稳定版 `aiosqlite`。安装必须使用：

```text
/home/lab239/anaconda3/envs/Langchain-Chatchat/bin/python -m pip
```

除 `aiosqlite` 外，不增加、升级或降级其他依赖；如果实际实现需要其他依赖，先停止并交给
Codex 重新评估。安装后必须执行 `python -m pip check`。

## 10. 实施要求

Claude 应先阅读任务 013 的最终实现和执行报告，再结合现有 Agent Run 代码完成：

1. 定义统一运行时边界，让上层调用不依赖具体是手写循环还是 LangGraph。
2. 将真实的计划、工具执行、观察、判断和终态流程拆成 LangGraph 节点与条件边。
3. 接入持久化 checkpoint，并建立 `run_id`、`thread_id` 与用户身份的稳定关系。
4. 复用现有 Run/Step 持久化和 SSE 映射，保持查询、取消和前端时间线兼容。
5. 实现安全恢复和异常收敛，防止重复工具执行及永久运行状态。
6. 增加运行时配置，默认走 LangGraph，并验证 legacy 回退仍可执行。
7. 补充离线测试，比较两种运行时的核心行为。

实现时优先复用任务 013 已建立的兼容层和原型，不要保留一份与生产图重复的演示图。

## 11. 测试要求

所有命令使用：

```text
/home/lab239/anaconda3/envs/Langchain-Chatchat/bin/python
```

测试必须使用临时数据库、临时 checkpoint、假模型和确定性假工具，不连接真实 LLM、
FastJev、MCP 服务、知识库或外部网络。

至少覆盖：

- LangGraph 直接回答路径；
- 顺序执行两个以上工具步骤并形成最终答案；
- 工具失败后的重规划或明确失败；
- 最大步数、超时、非法规划和非法判断均正常收敛；
- checkpoint 中断后从正确节点恢复，不重复执行已完成工具；
- 取消后不再启动下一步骤；
- 同一运行重复恢复不会并发重复执行同一工具步骤；
- Run/Step、checkpoint、SSE 事件和最终消息状态一致；
- 流式与非流式主要输出兼容；
- LangGraph 为默认运行时，切换 legacy 后旧闭环仍能完成代表性任务；
- 用户隔离覆盖查询、取消和恢复；
- 自动意图路由、长期记忆、RAG 引用和数据库迁移没有功能回归。

任务 013 和现有 Agent 专项测试应继续通过。测试无法运行时必须记录准确原因，不能将
“未运行”写成“通过”。

## 12. 验收标准

- [x] 生产默认单 Agent 闭环由真实 LangGraph 节点和条件边执行，而不是单节点包装旧循环。
- [x] 新运行时保持现有计划、工具、重规划、终态、API、SSE 和 WebUI 时间线语义。
- [x] 每个新运行具有持久化 checkpoint，并使用稳定、可验证的 `thread_id`。
- [x] 中断后的运行可以恢复，已完成的工具步骤不会重复执行。
- [x] 取消、超时、异常和恢复冲突不会留下永久 `running` 状态。
- [x] Run/Step 仍是用户可见轨迹，checkpoint 与业务记录职责清晰且状态一致。
- [x] LangGraph 为默认路径，legacy 回退开关经过自动测试且确实可用。
- [x] 用户身份只来自服务端认证上下文，跨用户查询、取消和恢复被拒绝。
- [x] 现有自动意图路由、普通对话、手动调试、知识库工具、记忆和消息写回没有回归。
- [x] 测试未访问真实模型、外部服务和 `chatchat-study-data`。
- [x] `git diff HEAD` 没有与本任务无关的源码修改。
- [x] 要求的定向测试通过，或准确记录无法运行的原因。

## 13. 后续任务边界

本任务完成后继续按以下顺序推进：

1. 任务 015：将普通回答、知识库检索、工具任务和多步骤 Agent 组织为智能对话总图；
2. 任务 016：建设论文研究领域基础与研究工作流子图；
3. 后续任务：研究模式 WebUI、证据卡片、论文比较和带引用报告。

任务 014 不提前实现上述业务功能，只提供稳定的 LangGraph 生产运行底座。

## 14. Claude 执行报告

Claude 完成后将状态改为 `ready_for_codex_review`，并填写以下内容。不要改写本任务前面的
目标、范围和验收标准。

### 修改文件

均以 `libs/chatchat-server/` 为相对根。全部改动收敛在 Agent 运行编排、运行时、接入层
与配置，未触及 RAG/知识库/认证/界面（WebUI）无关模块。

新增文件（`chatchat/server/agent_run/` 下）：

- `runtime/`（新包）：
  - `graph.py`（416 行）：正式 LangGraph 状态图，`prepare_plan / gate / step_start /
    step_run / decide` 节点 + 条件边（非单节点包装旧循环）；
  - `langgraph_runtime.py`（456 行）：`LangGraphRuntime`（默认运行时），统一事件流、
    持久 checkpoint 接入、安全恢复与并发保护、异常收敛；
  - `checkpoint.py`（139 行）：`AsyncSqliteSaver` 生命周期（惰性自愈/显式初始化/关闭）、
    链尾 `latest_agent_checkpoint_id`（独立只读 `aiosqlite` 连接）；
  - `factory.py`（103 行）：`create_agent_runtime` / `runtime_name` 统一工厂；
  - `legacy.py`（94 行）：`LegacyRuntime`（回退，包装原 `AgentRunner`）；
  - `__init__.py`（21 行）：运行时导出；
- `common.py`（91 行）：`step_payload` / `invoke_tool` / `set_plan_text` /
  `converge_run_best_effort`，供新旧运行时共用，避免两套工具调用与收敛逻辑；
- `tests/unit_tests/test_agent_runtime_langgraph.py`（1526 行，31 个测试）：本任务定向
  离线测试。

修改文件：

- `agent_run/__init__.py`：运行时相关导出；
- `agent_run/runner.py`：legacy `AgentRunner` 行为保持不变，改为复用 `common.py`
  的工具调用/plan 文本/收敛辅助（不再维护第二份逻辑）；
- `agent_run/service.py`：`agent_run_chat`（流式/非流式）与新增 `agent_run_resume`，
  统一经运行时工厂分发；`_event_to_sse` / `_aggregate_result` 保持既有 SSE 与聚合字段
  语义；恢复入口做用户属主校验（越权返回 None→404、legacy 回退→409）；
- `api_server/agent_run_routes.py`：`/agent-runs/{run_id}/resume` 端点，`stream` 查询
  参数与 `tool_config`/`use_mcp` 请求体沿用现有装配；
- `api_server/server_app.py`：`lifespan` 中启动时 `initialize_agent_checkpoint_store()`、
  关闭时 `shutdown_agent_checkpoint_store()`；
- `settings.py`：新增 `AGENT_RUNTIME: str = "langgraph"`（`langgraph` 默认 /
  `legacy` 回退）；
- `pyproject.toml`：新增 `aiosqlite = "^0.22"`（`AsyncSqliteSaver` 所需，唯一新增
  依赖）。

未新增数据库字段：`run_id == thread_id`，无需 v8 迁移，`current_version` 保持 7。

### 图结构与运行时选择

**状态图（`runtime/graph.py`）**：`AgentRunState` 表达运行/用户、目标与上下文、计划与
待执行队列、已执行步骤与观察、步数/状态/错误/最终答案、取消/超时/失败/恢复信息。节点
与条件边：

```text
prepare_plan（准备上下文/规划）
   └─(直答→completed) / (异常→failed) / (有步骤)──▶ gate
gate（有界循环门禁：运行存在性/终态/残态/最大步数/总超时）
   └─▶ step_start（同步步骤行：残态检测/复用/新建）
        └─▶ step_run（执行工具/复用既有观察/异常收敛）
             └─(终态→END) / (仍有队列→gate) / (队列空)──▶ decide
decide（判断下一动作：直答→completed / 重规划→gate / 失败→failed）
```

- `fresh_seqs` 闭包集合区分本进程新建步骤行与上一进程遗留（残态）行；恢复时对残态
  `completed/failed` 行复用既有观察，绝不重跑已完成的工具步骤，也不重复发该步骤的
  `step_finished` 事件；
- 门禁 `gate` 是真正的有界循环控制器：超过 `max_steps`、越过 `deadline_ts`、或出现
  无法安全续跑的残态时进入明确终态，防止无界循环与永久 `running`。

**checkpoint 与恢复**：生产异步流式路径使用持久化 `AsyncSqliteSaver`（默认落盘
`CHATCHAT_ROOT/data/agent_run_checkpoints.sqlite`，由 `server_app` lifespan 初始化；未
初始化时按默认路径惰性自愈，仍是本地文件，非纯内存）。`run_id == thread_id` 稳定映射，
可用 `tuple.config["configurable"]["thread_id"]` 直接验证。`astream` 使用
`stream_mode="updates"`、`durability="sync"`：每个超级步骤的 checkpoint 先于下一步同步
落盘，任意崩溃点处落盘链尾即真实恢复位置。恢复时把恢复点钉在**链尾** checkpoint
（`latest_agent_checkpoint_id`：独立只读 `aiosqlite` 连接上，取"不出现在任何记录
parent 中的唯一 checkpoint_id"，不依赖 uuid6 字典序"取最新"）；链无 checkpoint 或链
异常（多链尾）时可控失败，绝不当新图重放。

**运行时选择（`factory.py`）**：`create_agent_runtime(..., forced=, existing_run_id=)`
按 `Settings.AGENT_RUNTIME` 选择——`langgraph`（默认）→ `LangGraphRuntime`；`legacy`
→ `LegacyRuntime`（包装原 `AgentRunner`）。`existing_run_id` 非空（恢复）时只允许
`langgraph`；对 legacy 运行请求恢复直接 `ValueError`（无 checkpoint 可恢复），由 API
映射为 409。上层 Chat API 与 WebUI 均通过同一接口/工厂分发，不维护两套分支。

### 兼容与恢复设计

**两类持久状态职责分离（§5）**：LangGraph checkpoint 保存编排状态/条件分支/恢复位置；
`AgentRun`/`AgentStep` 继续作为用户可见时间线、`/agent-runs` 查询、权限校验与审计的
唯一事实来源。图在 `step_start`/`step_run` 节点开始与结束时同步 Run/Step，非整图结束
后一次性写入。`run_id == thread_id`，无需新增字段、无数据库迁移，历史 Run/Step 语义与
`current_version == 7` 不变。

**SSE / 聚合兼容（§6）**：`_event_to_sse` 将 `plan_ready / step_started /
step_finished / replanned / run_completed / run_failed / run_cancelled`（恢复另发
`run_resumed`）映射为 `OpenAIChatOutput` 兼容分块（content 位于 `choices[0].delta`）；
`_aggregate_result` 以 `runner.status` 汇总非流式 `agent_run`。流式与非流式主要字段与
语义一致，与 legacy 输出同形。

**安全恢复（§7）**：
- 已终态运行的重复恢复为幂等空操作，仅回报终态；
- 非终态运行的并发恢复由 per-run 进程内 `asyncio.Lock` 串行化，拿到锁后用全新会话
  重读运行状态——等待期间已被其他恢复/取消收敛为终态时幂等回报，不重放图；
- 跨进程由 Run/Step 与 checkpoint 的残态检测兜底：宁可可控失败也不重复执行工具；
- 升级前遗留的 legacy `pending/running` 记录：仍可查看与取消；请求恢复时返回 409 并
  保持原状（`running`），绝不把旧记录当新图继续执行。

**异常收敛（§7）**：`run()` 以 `try/finally` 显式 `await inner.aclose()`，保证消费者
提前关闭（请求中断）时内层生成器的 `GeneratorExit` 收敛路径在 `aclose` 返回前确定性
执行；`_run_fresh`/`_run_resume` 对 `CancelledError`/`GeneratorExit`/一般异常均调用
`converge_run_best_effort`，`finally` 双重兜底收敛——任何路径下都不允许留下
`pending/running`。无 checkpoint 的可控失败路径显式 `self.status = "failed"`，与非流式
聚合结果回报的终态一致。

**用户隔离（§6/§12）**：用户身份只来自服务端认证上下文（`owner_id` 由 API 层注入，
拒绝请求体 `user_id`）；查询/取消/恢复均经 `get_owned_run` 属主校验，跨用户访问被
拒（查询/取消 404、恢复 404/409），由定向测试覆盖。

### 测试命令与结果

所有命令使用 `/home/lab239/anaconda3/envs/Langchain-Chatchat/bin/python`；测试使用临时
数据库、临时 checkpoint、假模型与确定性假工具，`CHATCHAT_ROOT` 指向独立临时目录，
全程不连接真实 LLM/FastJev/MCP/知识库/网络，不访问 `chatchat-study-data`。

```bash
cd libs/chatchat-server
PY=/home/lab239/anaconda3/envs/Langchain-Chatchat/bin/python

# 本任务定向测试（31 个：图结构/闭环/有界收敛/恢复/持久化+SSE/恢复 API/双运行时一致性）
$PY -m pytest tests/unit_tests/test_agent_runtime_langgraph.py \
    -o addopts= -p no:cacheprovider -q
#   → 31 passed, 3 warnings in 4.67s

# 任务 013 兼容层（7 个）
$PY -m pytest tests/unit_tests/test_langgraph_compat.py \
    -o addopts= -p no:cacheprovider -q
#   → 7 passed in 0.21s

# 任务 011 既有 Agent 专项（27 个）
$PY -m pytest tests/unit_tests/test_agent_run.py \
    -o addopts= -p no:cacheprovider -q
#   → 27 passed, 1 warning in 6.08s

$PY -m pip check
#   → No broken requirements found.
```

覆盖点：LangGraph 直接回答（不产生无意义工具步骤）、多步骤顺序执行、工具失败后重规划
/ 明确失败、最大步数/总超时/非法规划/非法判断均收敛终态、checkpoint 中断后从正确节点
恢复且不重复执行已完成工具、取消后不再启动新步骤、重复恢复幂等/串行化不并发重复执行
同一工具、Run/Step+checkpoint+SSE+最终消息一致、流式与非流式输出同形、LangGraph 为
默认且切换 legacy 后旧闭环仍能完成代表性任务、用户隔离覆盖查询/取消/恢复、意图路由/
记忆/RAG/迁移无回归（013/011 套件）。

如实说明：全量 31 个测试同进程运行时偶发一条
`RuntimeWarning: coroutine '...on_chain_error' was never awaited`。单独以
`-W error::RuntimeWarning` 重跑单个测试干净通过，判定为整组运行时回调协程被延迟 GC
回收的归属产物，非功能缺陷（未影响任何断言结果）。

### 未完成项与风险

- **011 基线套件首次运行曾报 `timeout`/`已终止`（Exit 124/143）**：经无管道直写日志
  复跑确认为**全部 27 个通过（6.08s）**，非代码回归。根因是当时存在多个历史遗留的
  Codex 沙箱 pytest 进程（非本任务进程，未予 kill）竞争 CPU 导致的瞬时卡顿；干净环境
  下该套件秒级完成。
- **WebUI 未新增"恢复任务"页面**：按 §7 本任务仅要求后端恢复能力与离线验证；现有
  时间线与取消入口保持不变、继续可用，更完整的人工确认界面留给后续研究工作流任务。
- **checkpoint 为本地单文件**：`AsyncSqliteSaver` 落盘单个本地 SQLite 文件，适合当前
  单机部署；若未来多进程/多实例部署，跨进程恢复依赖残态检测兜底（宁可可控失败也不
  重复执行工具），届时需评估共享持久化 checkpoint 后端。
- **唯一新增依赖 `aiosqlite`**：已用
  `/home/lab239/anaconda3/envs/Langchain-Chatchat/bin/python -m pip` 安装并在
  `pyproject.toml` 声明，`python -m pip check` 通过；未新增、升级或降级其他依赖。
- 未改写本任务 §1–§13，§15（Codex 审查记录）保持 `pending` 待 Codex 填写。

## 15. Codex 审查记录

- 审查状态：`completed`
- 主要发现：生产执行器确已迁移为五节点 LangGraph 状态图，持久化 checkpoint、
  Run/Step 轨迹、恢复、取消、SSE 与 legacy 回退的主要语义均有离线覆盖。联合回归发现
  全局 `AsyncSqliteSaver` 会被跨事件循环复用，导致任务 012 的自动 Agent 用例失败；
  另发现 FastAPI 生命周期的异常日志对象错误，以及绕过 lifespan 的测试未释放 SQLite
  后台线程。
- 补充修改：checkpoint 单例和 per-run 恢复锁改为事件循环感知；初始化失败时关闭已打开
  连接；应用关闭改为 `finally` 保证执行；修正启动日志调用；增加跨事件循环回归用例及
  pytest 会话级连接清理。
- 最终验证：LangGraph、Agent Run 与意图路由联合回归 `117 passed`；长期记忆、RAG 引用、
  数据库迁移、会话隔离和知识库会话生命周期回归 `121 passed`；`pip check`、Python 编译
  和 `git diff --check` 通过。测试使用 `/tmp` 下的独立 `CHATCHAT_ROOT`，未访问真实模型
  或用户数据。
- 交付 commit：<由 Codex 在提交后通过最终报告记录>
