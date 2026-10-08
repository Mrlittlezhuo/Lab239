# 任务 012：智能意图路由与自动 Agent

## 1. 状态与基线

- 状态：`completed`
- 负责人：Claude Code
- 任务编写与审查：Codex
- 目标分支：`master`
- 源码基线 commit：`c7068b9a7de4cc5f9bdce721910d05f10222fb71`
- 允许存在于源码基线之后的前置提交：仅本任务文档、Claude 任务索引和项目优化文档

“源码基线”表示实现开始前的业务代码版本。任务文档提交可以位于该基线之后，Claude 应检查基线之后是否混入未声明的业务代码，不要只比较 `HEAD == 源码基线`。

## 2. 执行前检查

Claude 开始修改前必须执行：

```bash
git status --short --branch
git rev-parse HEAD
git diff --name-status c7068b9a7de4cc5f9bdce721910d05f10222fb71..HEAD
git stash list
```

当前工作区已知存在以下用户本机运行内容，它们不属于本任务，不得读取、修改、删除、暂存或提交：

- `chatchat-study-data/data/knowledge_base/info.db`
- `chatchat-study-data/data/logs/chatchat.log`
- `chatchat-study-data/data/knowledge_base/info.db.bak-20261007T154749-3ee95b98.db`

除上述已知内容外，如果出现下列任一情况，停止编码并报告用户：

- 当前分支不是 `master`；
- 源码基线之后包含任务文档和索引之外的业务代码；
- 工作区出现来源不明的修改或 stash；
- 本任务需要修改的文件与已有改动重叠且无法安全保留。

## 3. 背景

任务 011 已实现持久化的单 Agent 任务闭环，能够完成计划、多步工具执行、观察、重规划、取消和轨迹展示。但当前 WebUI 仍要求用户手动勾选“启用 Agent”、手动选择工具，并决定是否启用 Agent Run；当只选择一个工具时还会强制该工具。普通用户需要理解内部执行方式，系统尚未形成真正的自动 Agent 入口。

本任务增加一层轻量、可替换的意图路由：根据用户请求判断是直接回答还是进入 Agent Run，并为 Agent 缩小候选工具集合。知识库检索继续复用现有 `search_local_knowledgebase` 工具，复杂任务继续复用任务 011 的 Agent Run，不建立第二套执行器。

FastJev 的本地模型仍在准备中，本任务不安装、不启动也不依赖 FastJev。意图路由接口应方便后续增加 FastJev provider，但当前版本必须在没有 FastJev 和外部新依赖时完整可用。

## 4. 目标

1. 增加默认可用的“自动 Agent”模式：用户只输入问题，系统自动选择直接回答或 Agent Run。
2. 自动 Agent 能识别普通对话、知识库检索、明确工具任务和复杂多步骤任务，并选择合理的候选工具。
3. 保留普通对话和现有手动工具/Agent 调试能力，不能因自动模式而删除旧功能。
4. 路由失败、超时、低置信度或结果无效时可控回退，不能阻断用户对话。
5. 建立可插拔路由边界，为后续接入本地 FastJev 留出清晰位置，但不提前伪造 FastJev 实现。

## 5. 允许修改范围

Claude 可根据现有代码结构修改完成本任务所必需的模块，不限定具体文件名，主要包括：

- `libs/chatchat-server/chatchat/server/` 中的 chat API、Agent Run、工具装配和新增的意图路由代码；
- `libs/chatchat-server/chatchat/webui_pages/` 中的对话模式和自动路由结果展示；
- `libs/chatchat-server/chatchat/settings.py` 或对应配置定义中的自动模式配置；
- `libs/chatchat-server/tests/` 中的相关测试；
- 本任务所需的开发说明；
- 本文档末尾的“Claude 执行报告”。

如确实需要修改其他业务文件，可做与验收目标直接相关的最小修改，并在执行报告中解释原因。不要因为文件未列出就绕开已有架构。

## 6. 禁止事项

- 不执行 `git commit`、`git push`、`git reset`、`git checkout`、`git clean` 或 `git stash`；
- 不删除或覆盖任务开始前的用户改动；
- 不读取或修改 `chatchat-study-data/`、真实数据库、日志、密钥和 `.env`；
- 不安装新依赖，不升级 LangChain、Streamlit 或其他框架；
- 不下载、安装或模拟 FastJev；
- 不新增第二套 Agent 执行器、工具注册表、RAG 系统或长期记忆系统；
- 不实现多 Agent、拖拽工作流、RAG 批量评测中心或大规模界面重构；
- 不把模型隐藏思维链作为意图识别理由展示或持久化；
- 不改变现有用户隔离和公共知识库共享边界。

## 7. 核心行为

### 7.1 对话模式

界面应提供清晰的三类使用方式，具体控件和命名可结合现有页面调整：

- **自动 Agent（推荐）**：系统自动判断是否使用工具和 Agent Run；
- **普通对话**：不进行自动工具路由，保持直接聊天；
- **手动调试**：保留现有工具多选、MCP 和 Agent Run 手动控制能力。

自动模式应成为容易发现且无需配置工具即可使用的主入口。不要移除现有侧边栏、RAG 对话入口或任务 011 的执行时间线与取消能力。

### 7.2 意图路由结果

路由结果应是可校验的结构化数据，至少表达：

- 处理路径：直接回答或 Agent Run；
- 识别出的意图类型，例如普通对话、知识库检索、单工具任务、复杂任务或未知；
- 复杂度：直接、单步骤或多步骤；
- 候选工具名称列表；
- 可选的置信度和简短、面向用户的判断摘要。

具体类名和字段名由 Claude 决定。所有工具名称必须来自当前实际可用的工具集合，未知工具、非法结构和空结果必须被校验并进入回退流程。

### 7.3 路由策略

当前版本可以组合简单确定性规则与现有本地 LLM 完成判断，具体实现由 Claude 根据项目结构选择。应满足：

- 问候、闲聊、改写、解释等明显不需要工具的请求直接回答；
- 明确涉及本地资料、论文语料或知识库内容时，把 `search_local_knowledgebase` 纳入候选工具；
- 天气、计算、论文检索等明确能力，在对应工具实际存在时只提供相关候选工具；
- 需要检索、比较、汇总等连续动作的请求进入 Agent Run；
- 无法可靠判断时允许进入 Agent Run 由规划器继续判断，或按统一策略回退普通对话；
- 自动路由不能仅依赖中文关键词硬编码，规则只能作为明显意图的快速路径，未命中时应有通用判断或可靠回退。

候选工具的来源必须是现有工具注册与配置，避免维护一份与实际工具脱节的完整副本。自动模式不要求默认启用 MCP；MCP 是否进入自动候选应有明确配置，并保持现有手动开关可用。

### 7.4 自动执行与回退

- 路由为直接回答时，复用现有普通聊天流程，不创建无意义的 Agent Run。
- 路由为工具或复杂任务时，复用任务 011 的 `agent_run_chat`/`AgentRunner`，只把候选工具交给规划器。
- 知识库需求优先作为现有本地知识库工具进入 Agent Run，由工具参数和动态知识库选项选择实际公共知识库，不复制 `kb_chat` 检索实现。
- 路由异常、模型输出解析失败、超时或无候选工具时，返回普通聊天流程；不能留下空消息、永久运行状态或直接向用户抛出内部异常。
- 自动模式、普通模式和手动模式都继续使用服务端认证用户和当前用户会话，不能接受客户端自报 `owner_id`。
- 流式与非流式行为应保持现有接口风格；自动路由后的 Agent Run 继续展示任务 011 的轨迹和取消入口。

### 7.5 可观测性与后续 FastJev 接入

- 前端至少能看到本次请求选择了“直接回答”还是“Agent 执行”，以及简短意图名称；不要展示隐藏推理过程。
- 后端日志或响应扩展字段应足以定位路由 provider、结果和回退原因，但不要记录密钥或不必要的完整用户隐私数据。
- 路由器应有清晰的 provider/接口边界。当前实现可以是规则加现有 LLM；以后接入 FastJev 时应能替换决策来源，而无需重写 chat API、Agent Run 或 WebUI 主流程。
- 本任务不要求新增数据库表，也不要求持久化完整路由历史；如基于现有消息 metadata 记录少量路由结果，应保持兼容且无需破坏性迁移。

## 8. 实现任务

1. 阅读现有统一 chat 接口、WebUI 工具设置、工具注册、知识库工具和任务 011 Agent Run，再确定最小接入点。
2. 实现可测试、可替换的意图路由组件以及结构化结果校验和失败回退。
3. 在自动模式下根据路由结果调用普通聊天或现有 Agent Run，并只装配相关候选工具。
4. 调整 WebUI 模式选择：自动模式面向普通用户，现有手动工具/MCP/Agent Run 控件保留在手动调试路径。
5. 在界面和响应中提供简洁的路由结果提示，并保持 Agent Run 时间线、取消、会话历史和消息持久化正常。
6. 添加离线自动化测试，验证路由、工具筛选、执行分支、回退和原有模式兼容性。

## 9. 测试要求

所有 Python/pytest 命令必须使用：

```text
/home/lab239/anaconda3/envs/Langchain-Chatchat/bin/python
```

测试不得连接真实模型、FastJev、外部服务、MCP 服务、真实知识库、用户数据库或 `chatchat-study-data`。使用假路由器、假模型、假工具和临时数据库。

至少覆盖：

- 普通问候自动走直接回答，且不创建 Agent Run；
- 明确知识库问题选择本地知识库工具并进入 Agent Run；
- 明确单工具请求只提供匹配的候选工具；
- 检索并比较/汇总类请求进入多步骤 Agent Run；
- 路由返回未知工具、非法结构、异常或超时时回退普通聊天；
- 自动模式未选择任何手动工具时仍可工作；
- 普通对话模式不自动调用工具；
- 手动调试模式仍可选择工具、MCP 和 Agent Run；
- 自动 Agent 继续满足会话所有者隔离，并能沿用 Agent Run 轨迹和取消能力；
- 任务 011 Agent 专项、RAG 引用、长期记忆和会话隔离相关测试没有功能回归。

Claude 应运行本任务新增的定向测试，以及能够在当前环境正常结束的相关回归测试。若既有测试因已知环境问题卡住，应停止该组并在报告中准确记录，不要声称通过。

## 10. 验收标准

- [ ] 用户在自动模式下无需手动勾选 Agent 或选择工具即可发起请求。
- [ ] 普通聊天直接回答，不产生无意义的 Agent Run。
- [ ] 知识库、明确工具和复杂任务能够自动进入合适的 Agent Run，并且候选工具范围合理。
- [ ] 自动模式复用现有 Agent Run、工具、RAG、记忆和会话能力，没有平行实现。
- [ ] 路由器不可用、超时或结果非法时能够回退普通聊天，用户仍能获得回答。
- [ ] 手动调试与普通对话模式保留，现有侧边栏和 RAG 功能没有消失。
- [ ] WebUI 能简洁展示路由结果；Agent Run 仍能展示轨迹和取消。
- [ ] 路由接口可在后续加入 FastJev provider，当前任务不依赖 FastJev 或新依赖。
- [ ] 用户身份与会话隔离规则保持不变，公共知识库仍为全局共享资源。
- [ ] 测试不访问真实数据、模型和外部服务。
- [ ] `git diff HEAD` 中没有无关修改，已知用户本机运行文件保持原样。
- [ ] 所有要求的测试通过，或准确记录无法运行的原因。

## 11. Claude 执行报告

Claude 完成后将状态改为 `ready_for_codex_review`，并填写以下内容。不要修改本任务前面的目标、范围和验收标准。

### 修改文件

新增：

- `libs/chatchat-server/chatchat/server/intent_router/__init__.py`：意图路由包出口（re-export 全部公共接口）。
- `libs/chatchat-server/chatchat/server/intent_router/schemas.py`：`RouteResult` 结构化结果、意图/路径/复杂度常量与中文展示标签、`validate_route_result` 校验（候选工具必须来自当前可用工具集合、未知意图降级、`RouteError`）。
- `libs/chatchat-server/chatchat/server/intent_router/providers.py`：provider 边界——`IntentProvider` 接口、`rule_fast_path` 确定性快速路径（仅问候/纯符号/纯应答）、`LlmIntentProvider`（现有 `get_ChatOpenAI` 模型路径做通用判断，system prompt 携带实时工具清单）。
- `libs/chatchat-server/chatchat/server/intent_router/router.py`：`IntentRouter`（超时/异常/低置信度统一回退）与 `route_auto_agent` 自动模式唯一入口；`build_router` 为后续 FastJev 预留替换点。
- `libs/chatchat-server/tests/unit_tests/test_intent_routing.py`：40 个离线用例（临时 SQLite + 假工具/假规划器/假 provider/假 LLM）。

修改：

- `libs/chatchat-server/chatchat/settings.py`：`ApiModelSettings` 新增 `AUTO_ROUTE_TIMEOUT_SECONDS`（默认 20）、`AUTO_ROUTE_MIN_CONFIDENCE`（默认 0.5，None 关闭）、`AUTO_ROUTE_USE_MCP`（默认 False，自动模式默认不启用 MCP）。
- `libs/chatchat-server/chatchat/server/api_server/chat_routes.py`：新增 `auto_agent` 分支（`extra.agent_run` 手动调试优先）；`_attach_route_result` 把路由结果附加到非流式结果 dict 或流式 SSE 首条 `RouteResult` 事件；直接回答复用 `chat(..., tool_config={})`，Agent Run 复用 `agent_run_chat` 且只传候选工具。
- `libs/chatchat-server/chatchat/webui_pages/dialogue/dialogue.py`：侧栏改为「自动 Agent（推荐）/ 普通对话 / 手动调试」三模式 radio（默认自动，无需手动选工具）；`extra_body` 增加 `auto_agent`；流式/非流式均展示简短路由提示（路径 + 意图名 + 摘要），时间线/取消/MCP/手动 Agent Run 控件保持不变。

### 关键设计选择

1. **可插拔 provider 边界**：`IntentProvider.route(ctx) -> RouteResult` 是决策来源的唯一接口；当前默认 `LlmIntentProvider`（与任务 011 规划器同一模型来源）。后续 FastJev 只需实现该接口并在 `build_router` 注册，chat API、Agent Run、WebUI 均无需改动；本任务未下载/安装/模拟 FastJev，也未引入任何新外部依赖。
2. **确定性快速路径只覆盖明显意图**：问候/纯符号/纯应答直接短路（大小写/标点归一，不依赖中文关键词硬编码做工具判断）；其余请求一律交给 LLM 通用判断，避免关键词表覆盖不了的真实请求。
3. **结构化结果 + 严格校验 + 统一回退**：候选工具名必须来自当前实际可用工具注册表（调用方传入，不维护副本；未知名称剔除）；Agent Run 无有效候选、结构非法、provider 异常、输出解析失败、超时、低置信度 → 一律回退「直接回答」，绝不阻断对话、不向用户抛内部异常。
4. **超时不走 `asyncio.to_thread`**：该路径在本运行环境会永久等待，改用 `concurrent.futures` 线程池 + `future.result(timeout=...)`。
5. **不建第二执行器**：直接回答复用现有 `chat`（零工具）；Agent Run 复用任务 011 `agent_run_chat`/`AgentRunner`，只把候选工具交给规划器；知识库检索走 Agent Run 内的现有 `search_local_knowledgebase` 工具，未复制 `kb_chat`；时间线、取消、步骤持久化、消息写回、所有者隔离全部沿用任务 011。
6. **分支优先级**：`extra.agent_run`（手动调试）先于 `extra.auto_agent` 判定，任务 011 行为完全不变；自动模式默认不启用 MCP（由 `AUTO_ROUTE_USE_MCP` 显式控制）。
7. **前端最小侵入**：三种对话模式 radio；路由提示只展示「直接回答 / Agent 执行 · 意图名（+ 简短摘要）」，不展示、不持久化模型隐藏推理过程；非流式在结果 dict 附加 `route_result`，流式在 SSE 首条插入 `RouteResult` 事件（`OpenAIChatOutput` extra 字段透传，与 `AgentRunEvent` 风格一致）。
8. **身份与安全**：自动模式全程使用服务端认证用户的 `owner_id` 与既有会话归属校验，未接受请求体 `user_id`；未新增数据库表、未持久化路由历史。

### 测试命令与结果

均在 `libs/chatchat-server` 下执行，Python 为 `/home/lab239/anaconda3/envs/Langchain-Chatchat/bin/python`（任务要求的 Conda 环境）。全部用例离线：临时 SQLite（迁移 v7）+ 假工具/假规划器/假 provider/假 LLM，不触真实模型、FastJev、外部服务、MCP、真实知识库、用户数据库与 `chatchat-study-data`。

```bash
# 1) 新增意图路由套件（40 用例：快速路径/校验/LLM provider/回退/自动模式 API/
#    普通对话不路由/手动调试不变/越权 404/所有者隔离与取消复用/流式 RouteResult 首条）
python -m pytest tests/unit_tests/test_intent_routing.py -o addopts="" -q -p no:cacheprovider
# 结果：40 passed（4.06s）

# 2) 新套件 + 任务 011 单 Agent 套件（确认无回归）
python -m pytest tests/unit_tests/test_intent_routing.py tests/unit_tests/test_agent_run.py -o addopts="" -q -p no:cacheprovider
# 结果：59 passed（9.25s）

# 3) 数据库迁移 / 用户长期记忆 / 会话隔离回归
python -m pytest tests/unit_tests/test_db_migrate.py tests/unit_tests/test_user_memory.py tests/unit_tests/test_conversation_isolation.py -o addopts="" -q -p no:cacheprovider
# 结果：102 passed（10.14s）

# 4) RAG 历史引用回归
python -m pytest tests/unit_tests/test_rag_history_citations.py -o addopts="" -q -p no:cacheprovider
# 结果：6 passed（0.85s）
```

全部通过，无失败、无跳过、无超时；未运行其他测试组。

### 未完成项与风险

- **FastJev 未接入**（符合任务范围）：只交付了干净的 `IntentProvider` 边界与 `build_router` 替换点，未实现/注册 FastJev provider。
- **路由调用同步阻塞事件循环**：`route_auto_agent` 内部用线程池执行 provider，但 `future.result(timeout=...)` 会同步阻塞 FastAPI 请求协程最长 `AUTO_ROUTE_TIMEOUT_SECONDS`（默认 20 秒）。这是为避免本运行环境中 `asyncio.to_thread` 永久挂起而做的取舍；对本项目本地/低并发部署可接受，高并发场景后续可改为 `loop.run_in_executor` 的非阻塞写法（需先验证该路径在本环境同样不挂起）。
- **LLM 自报置信度仅为参考值**：`AUTO_ROUTE_MIN_CONFIDENCE=0.5` 的阈值基于现有 LLM 的经验值；若换用 FastJev 或其他模型，应重新标定该阈值（或置 None 关闭）。
- **快速路径词表是保守集合**：仅覆盖问候/纯应答/纯符号，用于直接回答而非工具判断；扩展时需防止把真实任务误判为问候。
- **WebUI 为三模式 radio**：默认落在「自动 Agent（推荐）」，普通用户无需配置；若后续希望按用户记忆默认模式，需另开任务（本任务不改用户偏好存储）。

## 12. Codex 审查记录

- 审查状态：`completed`
- 主要发现：自动模式、普通对话和手动调试三条路径清晰，自动模式复用了现有普通聊天与 Agent Run，流式路由事件、候选工具收窄、失败回退和所有者隔离均有离线测试。审查发现 provider 返回值的工具校验原本只由默认 LLM provider 执行，后续 FastJev 等自定义 provider 可以绕过统一校验并返回不存在的工具名。
- 补充修改：在 `IntentRouter` provider 边界统一调用 `validate_route_result`，超时时尝试取消尚未开始的 future；增加“自定义 provider 返回未知工具时回退普通对话”的回归测试。
- 最终验证：意图路由与 Agent 专项 `60 passed`；数据库迁移、长期记忆和 RAG 引用回归 `85 passed`；相关 Python 文件编译及 `git diff --check` 通过。会话隔离测试与其他回归组合运行时在当前环境再次出现既有挂起，已中止，未把该组记为通过。真实模型路由与 Streamlit 三模式交互仍需后续人工启动验证。
- 交付 commit：<由 Codex 在最终报告中记录；文档内可保留为空>
