# Langchain-Chatchat 项目工作约定

本文件随仓库版本控制，用于让新电脑或新会话中的执行方（目前是 Claude Code）恢复项目工作约定。它不覆盖系统指令、用户当前请求或更高优先级的安全规则。

## 开始工作前

- 先读取本文件和用户直接提供的当前任务文档；需要产品范围与历史设计背景时看
  `项目优化/README.md`。
- 执行 `git status --short --branch`、`git log -5 --oneline --decorate`、`git stash list`，确认分支、基线和遗留改动。
- 未确认归属前，不删除、覆盖、暂存或提交已有改动。

## 本机开发环境

- 当前机器上本项目固定使用 Conda 环境 `Langchain-Chatchat`。
- Python 路径为
  `/home/lab239/anaconda3/envs/Langchain-Chatchat/bin/python`。
- Python、pip 和 pytest 命令必须使用已激活的 `Langchain-Chatchat` 环境、
  `conda run -n Langchain-Chatchat ...` 或上述绝对路径，不得误用 `base`
  环境或系统 Python。
- 安装依赖时优先使用 `python -m pip`，确保 pip 与当前 Python 属于同一环境。
- 跑后端 pytest 之前，`CHATCHAT_ROOT` 指向的目录必须先建好数据目录：
  `python -c "from chatchat.settings import Settings; Settings.basic_settings.make_dirs()"`。
  否则 `test_supervision_project.py::test_router_is_mounted_on_the_real_server_app`
  会因 `data/media` 不存在而失败，看起来像代码 bug（2026-10-25 实测踩过）。
- `tests/unit_tests/test_research_workflow.py` 内部用 `pytest.raises(KeyboardInterrupt)`
  模拟进程崩溃，在全量会话里会打断整个 pytest（退出码 2，后面的测试根本不执行）。
  单独跑它是 11 passed。全量跑时先 `--ignore` 排除它，再单独补跑这一个文件。
- 依赖钉版集中在 `libs/chatchat-server/pyproject.toml`（仓库没有 poetry.lock /
  requirements.txt）。`pyarrow`、`protobuf`、`googleapis-common-protos` 都是
  **兼容性钉版**，改动前先看注释里的冲突原因：`streamlit 1.34.0` 要求 `protobuf<5`，
  与 `googleapis-common-protos 1.75+`（要 `protobuf>=6.33`）不可同时满足。

## 工作方式

- 2026-10-14 起不再走「Codex 写任务文档 → Claude 执行 → Codex 审查交付」的协作流程，
  协作工作流文档已删除。用户直接把任务文档交给 Claude Code，Claude Code 直接执行。
- 执行范围以任务文档为准：只改允许范围内的文件与功能；发现必须越界时先说明再动。
- 完成定义：实现 + 定向验证通过 + 按「Git 交付」提交并推送 + 报告修改文件、设计选择、
  测试结果和已知限制。
- 不擅自扩大范围，不清理工作区，不暂存或覆盖归属不明的改动。

## Git 交付

- **每轮改动完成即交付**：代码修改完成并通过定向验证后，立即创建提交并推送当前远端分支，
  不攒改动、不等用户再次开口。用户 2026-10-13 明确要求此为默认动作。
- 定向验证是提交的前置条件：后端跑相关 `tests/unit_tests`，前端跑 `npx vitest run` 与
  `npm run typecheck`，改了前端还要 `npm run build`（`dist` 不入库）。
- 只暂存当前任务文件；bugfix 与界面/功能改动分开提交，不混成一个巨型提交。
- 不把用户已有改动、其他任务改动、运行数据、日志、数据库、密钥或生成文件混入提交。
- 提交或推送失败时保留现场并如实报告，不能声称已经完成。
- 已推送的提交不回滚、不改写：之后发现问题用独立修复提交补上，不 force push、
  不 reset 远端分支。用户明确要求「先审查再提交」时，按用户当场指示。
- 跨电脑转移**尚未验证或尚未完成**的改动时，用 `wip(handoff): …` 临时分支推送，
  不推 `master`；不用 stash 当跨电脑传输手段。交接分支只是传输容器，不代表验收通过。

## 网络和代理

- 先直连。直连不通或明确需要外网时，按下面两档处理，不必每轮都问用户。
- **轻量外网访问**——`git push/fetch/clone`、依赖版本查询、`pip`/`npm` 解析与小包安装、
  `curl`、web 请求等，单次传输预计**不超过约 50 MB**：自动探测本机代理并用代理重试，
  **不需要先征求用户同意**。探测顺序，取第一个能连通的：
  1. 当前进程已有的 `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY`（大小写都查）；
  2. Windows 系统代理：`HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings`
     的 `ProxyEnable` 与 `ProxyServer`；
  3. 常见本地混合端口，依次探测 `127.0.0.1:7897`、`7890`、`10809`、`1080`。
- **下载较大文件**——模型权重、数据集、Docker 镜像、`torch`/`transformers` 这类大依赖，
  或任何预计**超过约 50 MB**、会明显占用带宽和时间的下载：**必须先问用户是否使用代理**，
  得到答复再动手。用户在本轮已经主动要求下载并提到代理时可直接用。
- 用户未指定代理地址时使用 `http://127.0.0.1:7897`。
- 代理只注入相关命令的进程环境或命令行参数，**不写入** Windows User/Machine 环境设置，
  **不改全局 git 配置**（Git 用 `git -c http.proxy=... -c https.proxy=...`）。
- 自动启用代理时必须在报告里写清：用了哪个地址、怎么探测到的、影响了哪些命令。
- 走代理仍失败时保留现场并如实报告，不能声称已经完成。

## 当前产品范围

- 第一轮只隔离用户聊天、会话、历史消息、反馈、临时文件和聊天附件。
- 永久知识库、知识文件、向量索引、模型、工具和 MCP 保持全局共享。
- 所有登录用户均可上传、编辑和删除公共知识库内容；相关界面必须提示其影响所有用户。
- 不给知识库增加 `owner_id`，不移动公共知识库目录，不重建公共向量索引。
- 未经用户明确要求，不读取或修改 `chatchat-study-data`、真实运行数据库、日志、密钥和用户数据。

## 数据库和安全底线

- 数据库迁移必须幂等、可验证、保留旧数据，并提供失败恢复路径。
- 不使用 `drop_all`、重建数据库或重建知识库代替升级迁移。
- 当前用户身份必须来自服务端认证上下文，不能相信请求体中的 `user_id`。
- 会话 ID 不是权限；读取、更新、删除和反馈都必须同时验证会话所有者。
- 未登录、令牌过期、禁用用户和跨用户访问必须有自动化测试。
