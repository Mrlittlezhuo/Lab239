# Phoenix 本地可观测性使用说明（任务 016）

本项目的开发期 Trace 能力基于 Arize Phoenix（本地自托管，非 Elixir Phoenix，
也不是 LangSmith 等云服务）。默认关闭；开启后一次智能对话的路由、执行分支、
LangGraph/Agent、工具与 LLM 调用会作为一条 Trace 上报到本机 Phoenix UI。

依赖已声明在 `libs/chatchat-server/pyproject.toml`：
`arize-phoenix==12.6.1`、`arize-phoenix-otel==0.17.1`、
`openinference-instrumentation-langchain==0.1.78`（另有兼容钉版
`arize-phoenix-evals==2.13.0`、`graphql-core==3.2.7`）。

## 1. 启动 Phoenix 本地服务

在 Conda 环境 `Langchain-Chatchat` 中（持久化目录自行选择，勿放仓库内）：

```bash
conda activate Langchain-Chatchat
export PHOENIX_WORKING_DIR=~/phoenix-data   # 用户自选的持久化目录
phoenix serve                                # 默认 UI: http://localhost:6006
```

验证服务：浏览器打开 <http://localhost:6006>，或
`curl -s http://localhost:6006/healthz`。

## 2. 配置本项目

配置项在 `CHATCHAT_ROOT/basic_settings.yaml` 的 `basic_settings` 段
（或直接改代码默认值，见 `chatchat/settings.py`）：

```yaml
basic_settings:
  TRACE_ENABLED: true                  # 默认 false：关闭时完全 no-op
  TRACE_PHOENIX_ENDPOINT: http://localhost:6006
  TRACE_PROJECT_NAME: chatchat
  TRACE_CAPTURE_CONTENT: false         # 是否记录 prompt/response/工具内容，默认不记录
  TRACE_SAMPLE_RATIO: 1.0              # 0~1 采样比例
```

## 3. 启动 Chatchat 并查看 Trace

正常 `chatchat start -a`（或分别启动 API/WebUI）。API 进程启动时会在
lifespan 中一次性注册 OTel provider 与 LangChain/LangGraph 自动埋点；
在 WebUI「智能对话」发起请求后，打开 Phoenix UI 选择
`TRACE_PROJECT_NAME` 对应项目即可看到 Trace。

## 4. 用两类请求确认 Trace

1. 直接回答：发送“你好！” → 应看到根 span `smart_conversation`
   （属性含 `chatchat.conversation_id`、`chatchat.execution_mode=direct_answer`）
   与子 span `intent_route`（意图、置信度、回退原因），以及自动埋点的
   LLM 调用 span。
2. Agent/RAG：发送知识库或复杂任务问题 → 根 span 上还有
   `chatchat.agent_run_id`、`chatchat.agent_run_status`；工具调用
   （如 `search_local_knowledgebase`）与 LLM span 挂在同一 Trace 下。
   需要查看检索条数、知识库名、引用与工具输入输出时，把
   `TRACE_CAPTURE_CONTENT` 设为 `true`（会把内容上报到本机 Phoenix，
   仅调试使用）。

流式请求的根 span 覆盖整个输出过程（首事件为路由事件，直到流结束/
取消才收尾），不会在响应对象返回时提前结束。

## 5. Phoenix 未启动时的行为与排查

- Phoenix 未启动 / endpoint 不通：API 启动日志出现一条
  “可观测性初始化失败（已忽略，对话功能不受影响）”告警；对话与 Agent
  功能完全正常。运行中导出失败由 OTel 批量导出器静默丢弃，不重试。
- 常见排查：
  - `phoenix --version`、`phoenix serve --help` 确认包可用；
  - `curl -s http://localhost:6006/healthz` 确认 collector 在监听；
  - 确认 `TRACE_ENABLED: true` 且 endpoint 与 `phoenix serve` 端口一致；
  - 确认修改 `basic_settings.yaml` 后重启了 API 进程（配置在启动时读取）；
  - Phoenix UI 左上角切换 project（默认 `chatchat`）。
- 关闭可观测性：`TRACE_ENABLED: false` 后重启，不产生任何网络请求。
