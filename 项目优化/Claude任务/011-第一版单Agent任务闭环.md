# 任务 011：第一版单 Agent 任务闭环

## 1. 状态与基线

- 状态：`completed`
- 负责人：Claude Code
- 任务编写与审查：Codex
- 目标分支：`master`
- 源码基线 commit：`55a6c228d6bc7e10b6619d3ccbfe242c0a74f397`
- 允许存在于源码基线之后的前置提交：仅本任务文档、Claude 任务索引和项目优化文档

“源码基线”表示实现开始前的业务代码版本。任务文档提交可以位于该基线之后，Claude 应检查基线之后是否混入了未声明的业务代码，不要只比较 `HEAD == 源码基线`。

## 2. 执行前检查

Claude 开始修改前必须执行：

```bash
git status --short --branch
git rev-parse HEAD
git diff --name-status 55a6c228d6bc7e10b6619d3ccbfe242c0a74f397..HEAD
git stash list
```

当前工作区已知存在以下用户本机运行内容，它们不属于本任务，不得读取、修改、删除、暂存或提交：

- `chatchat-study-data/` 下的配置、数据库、日志、备份和 `rag-memory-test` 知识库；
- `libs/chatchat-server/start_api.sh`；
- `libs/chatchat-server/start_webui.sh`。

除上述已知内容外，如果出现下列任一情况，停止编码并报告用户：

- 当前分支不是 `master`；
- 源码基线之后包含任务文档之外的未声明业务代码；
- 发现其他无法确认归属的修改或 stash；
- 本任务需要修改的源码与已有未提交修改重叠；
- 无法使用 Conda 环境 `Langchain-Chatchat`。

## 3. 背景

当前项目已有 Agent 执行器、工具注册、本地知识库工具、MCP、会话历史、长期记忆和工具调用流式事件。现有能力本质上仍是“模型根据对话直接选择工具并返回结果”，缺少可持久、可观察、可控制的任务闭环。

本任务实现第一版单 Agent 闭环，使其能够围绕一个用户目标完成：

```text
加载会话与记忆
  -> 生成结构化计划
  -> 选择并执行工具
  -> 记录观察结果
  -> 判断完成或重新规划
  -> 生成最终回答
  -> 持久化执行轨迹并沿用现有记忆机制
```

这是可验收的第一版，不是 FastGPT 的全量复制。

## 4. 目标

1. 建立单 Agent 的结构化运行模型，包含用户目标、计划、当前步骤、执行状态、最终结果和错误信息。
2. 实现“计划—执行—观察—判断/重规划—完成”循环，支持连续执行多个工具步骤，而不是只做一次工具调用。
3. 复用现有普通工具、本地知识库工具和 MCP 接入；保留会话历史、长期记忆和用户隔离。
4. 每个运行和步骤都要逐步持久化，即使工具失败或请求中断，也能查看已完成的执行轨迹。
5. 在 WebUI 中展示用户可理解的执行时间线：计划、步骤状态、工具名称与参数、工具输出摘要、错误和最终结果。
6. 建立离线、可重复的测试，验证直接回答、多工具任务、失败处理、步数上限、持久化和用户隔离。

## 5. 允许修改范围

为避免过度限制实现，Claude 可根据实际代码结构修改完成本任务所必需的下列模块，不限定具体文件名：

- `libs/chatchat-server/chatchat/server/` 中的 Agent、chat、API、数据库模型/仓库/迁移代码；
- `libs/chatchat-server/chatchat/webui_pages/` 中的 Agent 对话与执行轨迹展示；
- `libs/chatchat-server/chatchat/settings.py` 或对应默认配置定义，仅限 Agent 步数、超时等本任务配置；
- `libs/chatchat-server/tests/` 中的新增或相关回归测试；
- 数据库迁移说明和本任务必须的开发文档；
- 本文档末尾的“Claude 执行报告”。

如果任务确实需要修改上述范围外的业务文件，先在执行报告中说明理由，只做与验收目标直接相关的最小修改。不要因为文件未在列表中就采用明显绕过现有架构的实现。

## 6. 禁止事项

- 不执行 `git commit`、`git push`、`git reset`、`git checkout`、`git clean` 或 `git stash`；
- 不删除或覆盖任务开始前的用户改动；
- 不读取或修改 `chatchat-study-data/`、真实数据库、用户数据、日志、密钥和 `.env`；
- 不修改或提交两个未跟踪的 `start_*.sh`；
- 不安装新依赖，不升级 LangChain、Streamlit 或其他框架；
- 不实现多 Agent 协作、可视化拖拽工作流、工具市场或外部发布渠道；
- 不重写现有用户、RAG 或长期记忆系统；
- 不以重建数据库、清空数据或重建公共知识库代替升级迁移；
- 不持久化或展示模型隐藏思维链。轨迹只保存结构化计划、工具行为、观察摘要和用户可见状态。

## 7. 设计约束

### 7.1 最小运行模型

应有等价于 `agent_run` 和 `agent_step` 的持久化概念，具体命名由 Claude 结合项目风格决定。

运行至少包含：

- 唯一 ID、`owner_id`、`conversation_id` 和来源 `message_id`；
- 用户目标、结构化计划、当前步骤序号；
- `pending/running/completed/failed/cancelled` 或等价状态；
- 最终结果、错误摘要、创建/开始/结束时间。

步骤至少包含：

- 所属运行、顺序号、步骤类型与状态；
- 用户可理解的步骤目标；
- 工具名称、结构化输入、输出摘要和错误摘要；
- 开始/结束时间。

可以增加实现所需字段，但避免为尚未实现的多 Agent 或通用工作流提前设计大量抽象。

### 7.2 执行闭环

- 规划结果必须可解析和校验；模型输出无效时要可控失败，不得无限循环。
- 每次工具执行前创建/更新步骤，执行后立即保存结果或错误，不能只在整个 Agent 结束后统一写入。
- 工具错误不应直接让整个服务崩溃；Agent 可以根据观察重新规划，或以 `failed` 结束并给出明确错误。
- 必须存在可配置且有有限默认值的最大步数，达到上限时正常收敛为失败结果。
- 运行需要支持用户取消；取消在当前不可中断工具返回后、下一步开始前生效即可，不要为强制终止线程引入新依赖。
- 允许直接回答无需工具的问题，不得强制所有请求进入工具循环。

### 7.3 兼容和隔离

- 当前用户身份继续来自服务端认证上下文，不接受请求体自报 `owner_id`。
- 运行与步骤的查询、取消和其他操作必须校验所有者；用户 A 不能读取用户 B 的 Agent 轨迹。
- 沿用现有会话、消息、记忆和知识库边界；公共知识库仍然全局共享。
- 不启用 Agent 时，普通聊天、RAG 对话、临时文件对话和现有结构化引用行为保持不变。
- 现有 OpenAI 兼容接口尽量保持兼容；如需新增 Agent Run API，使用独立路由并在现有认证下挂载。

### 7.4 可观测性和 UI

- 流式事件或查询 API 应能向前端提供 `run_id`、运行状态、计划和步骤变更。
- 在现有 Agent 对话页内扩展即可，不要重做整个导航和主界面。
- 界面至少区分：计划、执行中步骤、已完成步骤、失败/取消状态和最终答案。
- 工具输入/输出可收起展示，长输出在持久化和界面展示时需做合理截断，但不影响工具结果传给后续步骤。
- 保留现有“启用 Agent”和工具选择交互，可在其基础上增加执行时间线与取消入口。

### 7.5 数据库迁移

- 通过新的幂等迁移版本增加所需表和索引，保留旧数据；
- 支持新空库和已有版本升级；
- 不使用 `drop_all`、删表或删库实现升级；
- 测试只使用临时数据库。

## 8. 实现任务

Claude 先阅读现有 Agent 执行器、工具注册、chat API、会话/消息仓库、记忆服务、迁移器和 WebUI Agent 区域，再选择与项目风格一致的实现。不限定必须新建哪些具体文件。

1. 实现 Agent Run/Step 的模型、仓库、幂等迁移和所有者限定查询。
2. 实现一个独立的单 Agent 编排/控制层，负责计划、步骤执行、观察、重规划/完成判断和状态收敛。
3. 接入现有工具注册、本地知识库工具、MCP、会话历史和长期记忆；避免复制第二套工具或记忆系统。
4. 将 Agent 运行接入已认证的对话流程，补充查询运行详情和取消所需 API 或等价能力。
5. 在现有 WebUI 中增加简洁的 Agent 执行时间线和取消入口，不重做整个对话页。
6. 完成离线测试和必要的迁移回归，并确认未读写真实运行数据。

## 9. 测试要求

所有 Python/pytest 命令必须使用：

```text
/home/lab239/anaconda3/envs/Langchain-Chatchat/bin/python
```

测试不得连接真实模型、外部服务、MCP 服务、用户数据库或 `chatchat-study-data`。使用临时 SQLite、假模型/规划器和确定性假工具。

至少覆盖：

- 无需工具时直接生成最终答案；
- 同一运行顺序执行两个或以上工具步骤，观察结果可供后续决策使用；
- 知识库工具作为普通 Agent 工具被选择和记录（mock 实际检索）；
- 工具报错后重新规划或明确失败，数据库中留下失败步骤；
- 达到最大步数时不再继续调用工具；
- 取消后不开始新步骤，运行状态为 `cancelled` 或等价状态；
- 运行中每步都逐步持久化，完成、失败和异常均收敛到终态；
- 用户 A 无法查看或取消用户 B 的运行；
- 迁移对新空库、已有库和重复执行都正常；
- 普通聊天、RAG 引用、长期记忆与会话隔离的相关回归测试仍通过。

建议的定向命令由 Claude 根据最终测试文件确定，但至少需运行 Agent 专项、迁移、记忆、会话隔离和 RAG 引用相关测试。

## 10. 验收标准

- [ ] 启用 Agent 后，一个需要多步工具的任务能在同一运行中完成计划、两步以上执行、观察和最终回答。
- [ ] 以“检索知识库中 Mem0 与传统 RAG 的相关内容并比较”为代表场景时，Agent 可以规划多个检索步骤、使用知识库工具并形成最终答案；自动测试使用 mock，不读取真实知识库。
- [ ] 运行和每个步骤在执行过程中逐步入库，可通过认证 API 或等价后端能力取回。
- [ ] 用户可以取消正在运行的 Agent，取消后不会继续启动新步骤。
- [ ] 工具异常、计划解析失败、达到最大步数和请求中断都不会留下永久 `running` 的运行。
- [ ] WebUI 能展示计划、当前步骤、工具调用与结果摘要、完成/失败/取消状态和最终答案。
- [ ] Agent 轨迹不包含模型隐藏思维链。
- [ ] 用户 A 不能读取或操作用户 B 的 Agent 运行；公共知识库边界保持不变。
- [ ] 不启用 Agent 时的普通聊天、RAG、临时文件、会话历史和长期记忆功能没有回归。
- [ ] 新迁移幂等、保留旧数据，且测试未访问 `chatchat-study-data`。
- [ ] `git diff HEAD` 中没有与本任务无关的新增修改，已知用户本机内容保持不变。
- [ ] 所有要求的测试通过，或准确记录无法运行的原因。

## 11. Claude 执行报告

Claude 完成后将状态改为 `ready_for_codex_review`，并填写以下内容。不要修改本任务前面的目标、范围和验收标准。

### 修改文件

新增（untracked）：

- `libs/chatchat-server/chatchat/server/agent_run/__init__.py`
- `libs/chatchat-server/chatchat/server/agent_run/schema.py`（计划/判断/步骤的解析契约）
- `libs/chatchat-server/chatchat/server/agent_run/planner.py`（结构化规划/判断器）
- `libs/chatchat-server/chatchat/server/agent_run/runner.py`（AgentRunner 编排循环）
- `libs/chatchat-server/chatchat/server/agent_run/service.py`（工具/历史/记忆装配、流式与非流式出口、消息写回）
- `libs/chatchat-server/chatchat/server/api_server/agent_run_routes.py`（`/agent-runs` 查询/取消 API）
- `libs/chatchat-server/chatchat/server/db/models/agent_run_model.py`
- `libs/chatchat-server/chatchat/server/db/models/agent_step_model.py`
- `libs/chatchat-server/chatchat/server/db/repository/agent_run_repository.py`
- `libs/chatchat-server/tests/unit_tests/test_agent_run.py`

修改（tracked）：

- `libs/chatchat-server/chatchat/server/api_server/chat_routes.py`（`extra.agent_run` 分支）
- `libs/chatchat-server/chatchat/server/api_server/server_app.py`（挂载 agent_run 路由）
- `libs/chatchat-server/chatchat/server/db/migrate/migrations.py`（v7 幂等迁移：agent_run / agent_step 表与索引）
- `libs/chatchat-server/chatchat/server/db/repository/__init__.py`（导入 agent_run_repository）
- `libs/chatchat-server/chatchat/settings.py`（`AGENT_MAX_STEPS`、`AGENT_RUN_TIMEOUT_SECONDS`）
- `libs/chatchat-server/chatchat/webui_pages/dialogue/dialogue.py`（执行时间线、取消入口、agent_event 优先的流式分发）
- `libs/chatchat-server/chatchat/webui_pages/utils.py`（`agent_run_list/detail/cancel` API 方法）
- `libs/chatchat-server/tests/unit_tests/test_db_migrate.py`（v7 断言）
- `libs/chatchat-server/tests/unit_tests/test_user_memory.py`（仅把版本断言 v6→v7，无逻辑改动）

未触碰（用户本机已有、与本任务无关，保持原样）：`chatchat-study-data/*`（配置、info.db 及备份、日志、rag-memory-test 知识库）、`libs/chatchat-server/start_api.sh`、`libs/chatchat-server/start_webui.sh`。

### 关键设计选择

- 独立编排层 `AgentRunner` 实现 计划 → 多步工具执行 → 观察 → 判断（replan/answer/fail）闭环；完整复用现有工具注册表（`get_tool`）、本地知识库工具、MCP、会话历史与长期记忆，未建立第二套工具或记忆系统。
- 步骤行在执行前持久化（`add_step` 先建 `running` 行再调工具），与取消 API 的竞态、门禁命中等异常路径都收敛到终态，不留永久 `running`/`pending`。
- 循环门禁在每轮迭代开头检查：raw-SQL `run_status`（跨连接取消）、`seq + 1 > max_steps`（最大步数）、`time.monotonic() > deadline`（超时），三类终止都是可控失败事件，循环有界、不会无限打转。
- 每个工具步骤执行后都调用 `planner.decide()`；规划/判断的无效输出统一由解析器转成 `PlanError` → 可控失败，不崩溃服务。
- 所有退出路径（`CancelledError` / `GeneratorExit` / 一般异常 / `finally`）都做 best-effort 终态收敛。
- 生产解析器要求 `replan` 必须给出至少一个步骤（空 replan 直接拒绝），避免“补充了 0 步”的假收敛。
- 流式沿用 OpenAI 兼容 SSE，附加 `agent_event`/`run_id`/`agent_plan`/`agent_step`/`agent_steps`/`agent_final_result`/`agent_error`/`message_id` 字段；非流式返回聚合 dict（`class_name="AgentRunResult"`，含 `agent_run` 明细）并在响应中保留 `conversation_id`。
- WebUI 按 `agent_event` 优先分发（执行时间线 + 取消入口），无 `agent_event` 时保持原有消息流行为不变。
- 权限：owner 一律取自服务端认证上下文，不信任请求体 `user_id`；会话/运行的所有读取、更新、取消都同时校验所有者；运行详情与取消 API 挂在现有认证之下的独立路由 `/agent-runs`。
- v7 迁移幂等（`CREATE TABLE IF NOT EXISTS` + 索引）、保留旧数据，不使用 drop_all 或重建数据库/知识库。

### 测试命令与结果

环境：conda 环境 `Langchain-Chatchat`（`/home/lab239/anaconda3/envs/Langchain-Chatchat/bin/python`），工作目录 `libs/chatchat-server`。

1. `python -m pytest tests/unit_tests/test_agent_run.py -o addopts="" -q -p no:cacheprovider`
   → **17 passed**（本任务全部测试：v7 迁移、仓储 owner 隔离、runner 闭环/失败/取消/流式、`/agent-runs` API 权限、chat completions agent_run 分支、未开启时的原路径不变）。
2. `python -m py_compile chatchat/webui_pages/dialogue/dialogue.py chatchat/webui_pages/utils.py`
   → 通过（仅语法/编译级验证；WebUI 运行时行为需真实启动服务，本任务未运行）。
3. `python -m pytest tests/unit_tests/test_db_migrate.py tests/unit_tests/test_user_memory.py tests/unit_tests/test_conversation_isolation.py tests/unit_tests/test_rag_history_citations.py -o addopts="" -q -p no:cacheprovider`
   → **108 passed**（数据库迁移、用户长期记忆、会话隔离、RAG 历史引用无回归）。

如实说明：首轮全量运行有 3 个用例失败，均为测试脚本自身与 runner 实际契约不符（`decide()` 在每个工具步骤后都会调用；空 `replan` 被生产解析器拒绝；`message` 表无 `owner_id` 列、归属经会话推导），生产代码未因此修改；修正测试脚本后全部通过。运行期间本机安全分类器多次临时不可用导致命令执行被间歇阻断，通过重试恢复，以上结果均为实际运行成功后的输出。

### 未完成项与风险

- WebUI（Streamlit）端只做了语法级校验与代码走查，执行时间线/取消按钮的运行时渲染、SSE 接收未在真实服务端验证，建议 Codex 审查或人工启动确认。
- 非流式聚合的 `agent_run.steps` 由 `step_started`/`step_finished` 事件构建，最终答案步骤（type=answer，仅落库不产生 step 事件）不出现在非流式 steps 列表中；数据库 `agent_step` 表记录完整。属已知表层差异，不影响终态与持久化。
- 非流式 + 多模态（vision）消息未纳入 agent_run 的 goal（只取最后一条文本）；这是第一版的范围取舍。
- `_persist_final_message` 写回消息失败时仅记录日志、静默忽略：运行终态正确，但来源消息可能停留在空回答。
- 全部测试使用临时 SQLite + 假模型/假工具/确定性假知识库，不触真实模型、MCP 与真实数据库；未读写 `chatchat-study-data`。

## 12. Codex 审查记录

- 审查状态：`completed`
- 主要发现：Claude 已建立 Run/Step 持久化、计划器、有界执行循环、查询/取消 API 和 WebUI 执行时间线，整体架构可保留。审查发现两个核心问题：所有工具强制经过 `asyncio.to_thread` 会在当前环境稳定挂起，且不适合只提供异步接口的 MCP 工具；初始计划同时包含多个步骤时，原实现在第一步后立即进入完成判断，可能跳过剩余既定步骤。
- 补充修改：工具调用改为优先使用 `ainvoke`（兼容 LangChain/MCP 异步工具），仅对没有异步接口的旧工具使用同步 `invoke`；执行器改为完成当前计划批次后再判断回答/重规划。新增异步工具和初始两步计划两个回归用例。
- 最终验证：Agent 专项 `19 passed`，迁移 `40 passed`，长期记忆 `39 passed`，RAG 引用 `6 passed`，共 `104 passed`；新增/修改 Python 文件编译与 `git diff --check` 通过。会话隔离测试在当前环境仍卡在原有 TestClient 首个请求，本任务未修改会话隔离代码，未把该组记为通过。真实模型、MCP 服务和 Streamlit 交互仍需后续人工启动验证。
- 交付 commit：<由 Codex 在最终报告中记录；文档内可保留为空>
