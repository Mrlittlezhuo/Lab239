# 任务 016：Phoenix 本地可观测性

## 1. 状态、基线与执行顺序

- 状态：ready_for_claude
- 负责人：Claude Code
- 任务编写与审查：Codex
- 目标分支：master
- 任务文档基线 commit：4b5a3b25ad71bab3d020af2f27234e5b23e5b664
- 未审查业务代码基线：b85373b2b46dcd09f30a442e79d8294a0160469b
- 前置任务：任务 015 必须先在同一工作区完成

本任务是任务 015 的串联后续任务。Claude 开始本任务时，工作区预期包含任务 015
未提交的源码、测试和任务报告，不应因此停止，也不得将其清理、stash 或提交。

开始前执行：

    git status --short --branch
    git rev-parse HEAD
    git diff --stat
    git stash list

然后确认：

- 任务 015 状态已改为 `ready_for_codex_review`，执行报告已填写；
- 任务 015 的核心定向测试已经运行；
- 除任务 015 的改动和下列用户本机运行文件外，没有新的来源不明改动。

以下文件始终不属于任务，不得读取、修改、删除、暂存或提交：

- chatchat-study-data/data/knowledge_base/info.db
- chatchat-study-data/data/logs/chatchat.log
- chatchat-study-data/model_settings.yaml

当前还存在未跟踪的 `libs/chatchat-server/data/` 运行目录。将它视为待 Codex
处理的任务 015 现场，本任务不读取、不继续写入也不提交；后续测试必须使用临时
`CHATCHAT_ROOT`。

若任务 015 尚未完成，先回到任务 015 收尾。若发现其他不明改动、未声明 stash 或与
当前实现相冲突的修改，停止并报告，不自行清理。

## 2. 背景

任务 015 将直接回答、知识库、单工具和多步 Agent 纳入统一 LangGraph 智能对话总图。
现在需要补上开发期可观测性，让路由、图节点、LLM、RAG 和工具调用能够在本机查看完整
Trace，便于后续论文研究工作流调试。

技术方案已确定为 Arize Phoenix，不是 `phoenixframework/phoenix` 这个 Elixir Web 框架。
本任务使用 Phoenix 的本地服务、OpenTelemetry 和 OpenInference，不接入 LangSmith 或云端托管服务。

官方参考：

- https://github.com/Arize-ai/phoenix
- https://arize.com/docs/phoenix/self-hosting/deployment-options
- https://arize.com/docs/phoenix/tracing/how-to-tracing/setup-tracing

## 3. 目标

1. 在 Conda 环境 `Langchain-Chatchat` 中安装并声明 Phoenix 本地服务与 Trace 接入所需的最小依赖。
2. 为服务端增加可开关、可配置、不影响业务启动的可观测性初始化。
3. 让一次智能对话能在 Phoenix 中关联看到路由、执行分支、LangGraph/Agent、工具、RAG
   和 LLM 调用。
4. 正确处理流式输出：Trace 必须在响应流实际结束或异常后结束。
5. Phoenix 未启动、网络不通或 Trace 导出失败时，聊天与 Agent 功能仍正常工作。
6. 提供简明的本地启动、配置和验证说明。

## 4. 实现边界

### 4.1 依赖与本地服务

允许 Claude 使用以下固定 Python 安装相容稳定版本，并将项目运行必需的依赖正确声明在
`pyproject.toml`/锁文件中：

    /home/lab239/anaconda3/envs/Langchain-Chatchat/bin/python -m pip ...

预期使用的官方包是：

- `arize-phoenix`：本地 Phoenix 服务和 UI；
- `arize-phoenix-otel`：OpenTelemetry Trace 注册与导出；
- `openinference-instrumentation-langchain`：LangChain/LangGraph 自动埋点。

只有在实际调用链存在自动埋点无法覆盖的原生 OpenAI 兼容客户端时，才允许增加
`openinference-instrumentation-openai`，并在执行报告中说明原因。本任务不引入评测、云服务或
其他可观测平台依赖。

本地服务应能够使用当前 Conda 环境的 `phoenix serve` 启动，默认 UI 地址是
`http://localhost:6006`。持久化目录通过 `PHOENIX_WORKING_DIR` 由用户选择，不在仓库内硬编码
本机绝对路径，测试时使用临时目录。

### 4.2 配置与生命周期

至少支持以下等价配置，名称可与项目现有 Settings 风格统一：

- 启用/关闭可观测性，默认关闭；
- Phoenix/OTLP collector endpoint；
- project/service name；
- 是否记录 prompt、response 和工具内容，默认关闭；
- 可选的 Trace 开关或采样配置。

初始化应放在合理的应用生命周期中，同一进程只注册一次，测试、热重载和重复创建应用时
不能反复包装 instrumentor。关闭时不产生网络请求；启用但 Phoenix 不可用时只记录简洁告警，
不中断应用启动或对话。

### 4.3 Trace 与业务关联

优先使用 OpenInference 的 LangChain/LangGraph 自动埋点，再用少量手工 span 补齐业务语义。
一次智能对话至少应能关联：

- `conversation_id`、`message_id`、`user_id`或等价内部标识；
- 意图、置信度、`execution_mode` 和实际选中分支；
- 模型名称、是否流式输出、耗时和成功/失败状态；
- Agent `run_id`、计划/执行阶段、步骤和恢复/取消结果；
- 工具名、调用状态与耗时；
- RAG 检索耗时、返回条数、知识库名和引用数。

不得记录访问令牌、密码、密钥、数据库连接串、隐藏思维链或未经配置开启的完整用户内容。

对流式响应，根 span 不能在创建 `EventSourceResponse`/响应对象时提前结束，必须覆盖异步
迭代器的真实消费过程，并在正常完成、生成异常和客户端取消时留下正确状态。

### 4.4 运行说明

增加一份面向本项目的简明说明，至少包含：

1. 在 `Langchain-Chatchat` Conda 环境中启动 Phoenix；
2. 配置本项目的 endpoint、project name 和开关；
3. 启动 Chatchat 并访问 Phoenix UI；
4. 如何用一次直接回答和一次 Agent/RAG 请求确认 Trace；
5. Phoenix 未启动时的预期降级行为和常见排查命令。

本任务不要开发 Phoenix 的替代 UI，也不要把 Phoenix 强绑到 `chatchat start`。两个进程可以分别启动。

## 5. 允许修改范围

可按现有架构修改：

- 服务端配置、启动生命周期和可观测性封装；
- 任务 015 的智能对话总图、任务 014 的 Agent Runtime、工具和 RAG 调用处的埋点；
- 项目依赖声明与锁文件；
- 离线测试、少量开发说明和本文档的 Claude 执行报告。

不限制具体文件名。如果为了埋点需要调整任务 015 的新模块，可以做最小调整，但不得重写其
路由和执行语义。

## 6. 禁止事项

- 不执行 `git commit`、`git push`、`git reset`、`git checkout`、`git clean` 或 `git stash`；
- 不读取或修改 `chatchat-study-data`、真实数据库、日志、模型文件、密钥和 `.env`；
- 不使用 base 环境或系统 Python；
- 不升级或降级 LangChain、LangGraph、FastAPI、Streamlit 等无关框架；
- 不引入 LangSmith、Jaeger、Grafana 或其他第二套可观测系统；
- 不改变用户隔离、公共知识库、长期记忆、路由或 Agent 业务语义；
- 不新增论文研究数据模型、评测中心、云部署、Docker/Kubernetes 编排或大规模 UI 改造；
- 不将 Trace 失败变成对话失败，不在导出路径中无限重试。

## 7. 实施要求

1. 先完成任务 015，并理解统一 Chat API、智能对话总图、Agent Runtime、工具和 RAG 的调用边界。
2. 安装与声明最小 Phoenix/OpenTelemetry/OpenInference 依赖，记录最终版本。
3. 实现集中的配置与幂等初始化，关闭时为 no-op，异常时安全降级。
4. 启用 LangChain/LangGraph 自动埋点，用少量手工 span 补充智能对话路由与项目内部标识。
5. 确保流式和非流式请求的 span 生命周期、错误状态和上下文关联正确。
6. 增加离线测试与本地运行说明，再运行任务 015 的核心回归。

## 8. 测试要求

所有 Python/pip/pytest 命令使用：

    /home/lab239/anaconda3/envs/Langchain-Chatchat/bin/python

自动化测试使用临时 `CHATCHAT_ROOT`、假 exporter/假 tracer、假模型、假工具和假检索器，不连接真实
Phoenix、LLM、FastJev、MCP、知识库、外部网络或用户数据。

至少覆盖：

- 可观测性关闭时是 no-op，无导出请求且不改变对话结果；
- 启用时按配置注册正确的 endpoint 和 project name；
- 重复初始化或重复创建应用不会重复埋点；
- exporter/Phoenix 不可用或初始化异常时，直接回答和 Agent 路径仍可用；
- 直接回答、知识库、单工具和多步 Agent 分支均能生成包含关键属性的 Trace；
- 工具异常、Agent 失败和路由回退能正确标记，不导致第二次执行；
- 非流式 span 在返回结果后结束；
- 流式 span 在迭代器消费完成、生成异常或取消时才结束；
- 内容捕获关闭时，prompt、response、工具输入输出不出现在 span 属性中；
- 任务 015 智能对话总图、任务 014 Agent Runtime、路由、长期记忆和 RAG 引用的关键回归通过。

另外执行以下不依赖真实服务的检查：

- Phoenix 与 OpenInference 相关包可从指定 Conda 环境导入；
- `phoenix serve --help` 可在该环境正常运行；
- 项目依赖自检没有因新依赖出现不兼容。

## 9. 验收标准

- [ ] 任务 015 已先完成并保留其未提交改动。
- [ ] Phoenix 本地服务可从 `Langchain-Chatchat` Conda 环境启动。
- [ ] 项目存在默认关闭的可观测性配置，开关、endpoint、project name 和内容捕获行为明确。
- [ ] 同一进程不重复注册 instrumentor，关闭或导出失败不影响业务功能。
- [ ] 一次智能对话的路由、图执行、Agent、工具、RAG 和 LLM span 可用关键 ID 关联。
- [ ] 四类执行分支均有可查看的 Trace 语义，错误与回退状态清晰。
- [ ] 流式 Trace 覆盖完整实际输出周期，不在返回响应对象时提前结束。
- [ ] 默认不记录完整对话内容，不记录密钥或隐藏思维链。
- [ ] 本地启动与验证文档可按步骤执行，不需要 LangSmith 或云端账号。
- [ ] 离线测试和任务 015/014 核心回归通过，未访问真实用户数据。
- [ ] 未修改与任务 015/016 无关的业务功能，未提交或推送代码。

## 10. 后续任务边界

本任务只建立本地 Trace 基线，不在本阶段开发数据集评测、Prompt 版本管理、在线评分、报表大盘
或完整的运维监控。完成后进入任务 017：论文研究助手的研究项目、论文、证据和报告最小数据模型与
CRUD。

## 11. Claude 执行报告

Claude 完成后将状态改为 `ready_for_codex_review`，并填写以下内容。不要改写本任务前面的
目标、范围和验收标准。

### 修改文件

<待填写>

### 可观测性与降级设计

<待填写>

### 依赖版本与本地启动方式

<待填写>

### 测试命令与结果

<待填写；失败、跳过或未运行必须如实说明>

### 未完成项与风险

<待填写>

## 12. Codex 审查记录

- 审查状态：pending
- 主要发现：<由 Codex 填写>
- 补充修改：<由 Codex 填写>
- 最终验证：<由 Codex 填写>
- 交付 commit：<由 Codex 在最终报告中记录；文档内可保留为空>
