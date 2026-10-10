# 任务 018：证据驱动论文研究闭环与 WebUI

## 1. 状态、基线与执行顺序

- 状态：`ready_for_codex_review`（Claude 已按 §9 顺序完成阶段 A–E，
  执行报告见 §13）
- 负责人：Claude Code
- 任务编写与审查：Codex
- 目标分支：`master`
- 未审查业务代码基线：`b9a6cfbd8748df0a1addd2a7c4f8367859d55851`
- 任务 017 文档提交：`5f3ef248`
- 前置任务：任务 017 必须先完成

本任务是任务 017 的串联后续任务。Claude 可在同一工作区按下列顺序连续执行：

1. 先按任务 017 完成研究数据基础、论文采集标准化和检索工作流；
2. 运行任务 017 的定向测试，填写其执行报告，将状态改为
   `ready_for_codex_review`；
3. 不提交、不推送、不清理任务 017 的改动，直接继续本任务；
4. 两个任务都完成后，由用户通知 Codex 联合审查。

如果任务 017 未完成、数据边界尚未稳定或其核心测试仍失败，不得跳过问题
直接实现本任务。

## 2. 执行前检查

Claude 进入本任务前执行：

```bash
git status --short --branch
git rev-parse HEAD
git diff --stat
git stash list
```

并确认：

- 任务 017 状态已为 `ready_for_codex_review`，执行报告已填写；
- 工作区中的未提交业务代码都来自任务 017；
- 任务 017 的迁移、所有者隔离、论文去重和研究图定向测试已通过。

当前本机运行内容始终不属于任务，不得读取、修改、删除、暂存或提交：

- `chatchat-study-data/data/knowledge_base/info.db`
- `chatchat-study-data/data/logs/chatchat.log`
- `chatchat-study-data/model_settings.yaml`
- `libs/chatchat-server/data/`

如果除任务 017 和上述运行内容外还有来源不明的修改或 stash，必须停止并报告，
不得自行清理。

## 3. 背景与目标

任务 017 完成后，用户将能建立研究项目、生成并确认计划、检索多来源论文、
去重并筛选候选论文。但仅有论文列表还不能构成真正的论文研究助手：系统还需要
把原文转化为可检查证据，再基于证据比较论文并生成可追溯报告。

本任务将后续功能合并为一个“候选论文到可追溯研究报告”业务闭环：

1. 论文原文解析和带位置的证据卡片；
2. 基于证据的论文比较、覆盖与冲突检查；
3. 带可追溯引用的 Markdown 研究报告、版本和 BibTeX 导出；
4. 能够完整操作上述流程的论文研究 WebUI。

## 4. 完成后的业务闭环

从已有研究项目的入选论文开始，用户应能完成：

```text
选择要深入阅读的论文
  -> 解析可用原文并保留页码/章节/文本块位置
  -> 按研究子问题提取结构化证据卡片
  -> 用户查看、确认、修改或否决证据
  -> 按统一维度比较论文
  -> 检查子问题覆盖、证据不足和结论冲突
  -> 必要时返回任务 017 补充检索
  -> 生成带证据引用的 Markdown 报告和 BibTeX
  -> 用户审阅、修改或再次生成新版本
```

刷新页面、重新登录或服务重启后，项目、论文、证据状态、比较结果、报告版本和
运行进度仍必须存在。

## 5. 实施阶段

### 5.1 阶段 A：论文阅读与可定位证据

复用现有 PDF/知识库文档解析和任务 017 的论文记录，为入选论文建立可定位的
原文单元。实现方式应与现有文档加载、分块和 metadata 结构兼容，不重写第二套
PDF 框架。

一张证据卡片至少表达：

- 所属项目、子问题和论文；
- 证据类型：研究目标、方法、数据集、指标、实验结果、结论或局限；
- 可核对的原文片段和简短概括；
- 页码、章节、文本块/文档 ID 或等价的稳定来源定位；
- 提取时间、模型/方式、用户确认状态和必要的来源校验信息。

证据提取的模型输出必须经结构化校验。引用片段必须能在提供给模型的原文中找到
或通过可重现的定位方式校验；不能把模型自行改写的文本当成原文引用。全文不可用时
可使用摘要，但必须标明为摘要级证据，不得伪造页码或章节。

重试和恢复必须幂等；已被用户修改、确认或否决的证据不能在重跑时被静默覆盖。

### 5.2 阶段 B：证据覆盖、冲突和论文比较

以证据卡片作为比较和综合的唯一事实入口。默认比较维度包括研究目标、核心方法、
数据集、评价指标、主要结果、优点和局限。用户可以增删或调整项目的比较维度。

比较单元和冲突记录必须关联具体证据卡片，不能只保存一段无法追溯的模型总结。
覆盖检查至少能指出：

- 哪些子问题已有证据；
- 哪些问题仅有单篇/摘要级或低可信证据；
- 哪些关键维度缺失；
- 哪些论文结论存在冲突，且冲突两侧分别依据哪些证据。

证据不足时，可生成结构化的补充检索建议，并通过任务 017 的研究检索入口启动新一轮；
不得复制一套搜索和去重逻辑。补充检索是可见的新运行，不得无限自循环。

### 5.3 阶段 C：带引用的版本化报告

根据已确认或策略允许的证据生成 Markdown 研究报告。报告中的事实性结论必须通过
稳定引用标记关联真实论文和证据卡片，不能只保存显示顺序或模型自行生成的参考文献。

第一版至少支持：

- 研究问题与范围、检索/筛选说明、方向概览、代表论文、比较、综合结论、冲突、
  证据不足、后续建议和参考文献；
- 引用从报告返回论文和原文证据的稳定关联；
- Markdown 查看/导出和基于真实元数据的 BibTeX 导出；
- 报告版本、生成时间、使用的证据集合与生成状态；
- 用户编辑后保存新版本，不覆盖旧版本。

如果某个结论没有证据，必须明确标记为推测、建议或证据不足，不得伪装成论文事实。

### 5.4 阶段 D：论文研究 WebUI

在现有 Streamlit WebUI 中增加清晰的“论文研究”入口，但不进行与任务无关的整体换肤。
至少提供：

- 研究项目列表、新建、编辑、删除和继续研究；
- 项目概览、研究问题/范围和阶段进度；
- 计划编辑、确认、启动、取消、恢复与补充检索；
- 论文候选列表、纳入/排除、全文状态和来源信息；
- 证据卡片按子问题/论文/类型查看，显示原文定位并支持确认、修改和否决；
- 论文比较表、覆盖与冲突提示；
- 报告查看/编辑、版本切换、引用证据查看、Markdown 和 BibTeX 导出；
- 复用 Agent Run/Step 的执行时间线、错误提示和取消入口。

WebUI 必须通过后端 API 完成业务操作，不直接绕过服务层写数据库。登录恢复、用户隔离、
现有侧边栏、智能对话、RAG 问答、知识库管理和高级 Agent 调试不得因新页面消失或失效。

### 5.5 阶段 E：扩展研究 LangGraph

在任务 017 的研究子图后续扩展真实节点，至少包括：

```text
read_selected_papers
  -> extract_evidence
  -> wait_for_evidence_review
  -> analyze_coverage_and_conflicts
  -> build_comparison
  -> generate_report
  -> wait_for_report_review
```

节点命名可调整，但必须保留独立阶段和人工确认点。复用同一研究运行、Agent Run/Step、
checkpoint、取消/恢复和 Phoenix Trace，不新建第二套工作流执行器。每个阶段的业务产物
都必须先持久化，再推进下一阶段。

## 6. 数据、API 与所有者边界

- 优先扩展任务 017 已建立的领域模型；实际 schema 不足时追加下一版幂等迁移，
  不改写已执行迁移。
- 必要时可增加论文原文单元/定位、比较项、覆盖/冲突记录、报告引用和报告版本，
  但不为未实现的远期功能预建大量空表。
- 研究项目、项目论文、原文、证据、比较、冲突和报告必须通过项目校验当前用户。
- 当前用户只能来自服务端认证上下文；项目 ID、论文 ID、证据 ID、报告 ID 都不是权限。
- 公开论文规范元数据可复用，但用户私有全文、项目证据、笔记、比较和报告不共享。
- API 组织方式遵循任务 017 的研究路由风格，提供 WebUI 所需的列表、详情、审核、执行、
  版本和导出能力，无需为了固定 URL 复制服务。

## 7. 允许修改范围

Claude 可按现有架构修改完成本任务所需的：

- 研究领域模型、后续迁移、仓储、业务服务、API 和导出；
- 论文解析适配、证据提取/校验、比较、覆盖/冲突检查和报告生成；
- 任务 017 研究 LangGraph 的后续节点、checkpoint、Agent Run/Step 和 Phoenix 埋点；
- Streamlit 论文研究入口、页面、API 客户端和必要的导出交互；
- 对应离线测试、必要开发说明和本文档的 Claude 执行报告。

允许范围按功能边界确定，不限制具体文件名。Claude 应根据任务 017 实际实现选择
合理位置，不要因任务文档未列出文件名而绕开现有架构。

## 8. 禁止事项

- 不执行 `git commit`、`git push`、`git reset`、`git checkout`、`git clean` 或 `git stash`；
- 不读取或修改 `chatchat-study-data`、真实数据库、日志、模型文件、密钥和 `.env`；
- 固定使用 Conda 环境 `Langchain-Chatchat`，不使用 `base` 或系统 Python；
- 不安装新依赖，不升级或降级 LangChain、LangGraph、Phoenix、FastAPI 或 Streamlit；
- 不重写 PDF 框架、知识库、搜索/去重、用户系统、Agent 执行器、checkpoint 或 Trace 系统；
- 不以重建数据库、清空表、重建向量索引或改写已发布迁移代替正式升级；
- 不引入 Multi-Agent、GraphRAG、自动代写/投稿、付费文献绕过、大规模界面重构或云端依赖；
- 不把模型隐藏思维链、密钥或完整私有论文内容写入日志/Phoenix；
- 不在自动测试中连接真实 LLM、ArXiv、Phoenix、外部网络或用户数据。

如果必须引入新依赖、改变任务 017 的核心数据边界、大幅改写现有 WebUI 或无法复用
现有解析/执行能力，先停止并交给 Codex 重新评估。

## 9. 实施顺序与阶段停止条件

1. 先根据任务 017 的真实 schema 完成阶段 A，用可重现的来源定位验证证据。
2. 证据持久化、幂等和所有者测试通过后，再完成阶段 B 的比较与覆盖/冲突分析。
3. 比较产物可追溯后，再完成阶段 C 的引用映射、版本化报告和导出。
4. 后端 API 和工作流定向测试通过后，再完成阶段 D 的 WebUI，避免界面先行依赖未稳定接口。
5. 最后串联阶段 E，运行任务 017/018 联合回归并填写执行报告。

如果原文无法定位、证据引用无法校验、跨用户访问未阻断、报告引用不能反查证据，
或任一阶段定向测试仍有核心失败，停在当前阶段并如实报告，不要用界面或模型输出
掩盖后端数据问题。

## 10. 测试要求

所有 Python/pip/pytest 命令使用：

```text
/home/lab239/anaconda3/envs/Langchain-Chatchat/bin/python
```

测试使用临时 `CHATCHAT_ROOT`、临时 SQLite、小型固定文档样例、假模型、假解析器、假检索器
和内存 Trace exporter。至少覆盖：

- 从任务 017 最新 schema 幂等升级、失败回滚与旧产物保留；
- 页码/章节/文本块定位保留，原文片段校验，摘要级证据正确降级；
- 证据提取结构校验、重试幂等、用户修改/确认/否决不被静默覆盖；
- 比较单元和冲突两侧均能追溯到证据，覆盖结果可重现；
- 补充检索复用任务 017，存在最大轮次/人工确认边界，不会无限循环；
- 报告事实引用、证据反查、版本不覆盖、Markdown 和 BibTeX 导出；
- 无证据结论被标记或拒绝，不产生伪造论文、引用或定位；
- 用户 A 无法访问用户 B 的原文、证据、比较、报告、导出和执行入口；
- 研究图后续节点、人工确认、失败、取消、恢复、进程重启和 Phoenix 关联；
- WebUI 请求参数、鉴权失效处理、空状态、错误状态和页面状态恢复；
- 现有登录、侧边栏、智能对话、RAG、知识库、记忆、Agent Run 和 Phoenix 关键回归。

WebUI 至少完成语法/导入和可离线的页面逻辑测试。若无法自动化真实浏览器交互，在执行
报告中列出需要 Codex/用户手工验证的最小清单，不得声称已完成界面实机验收。

## 11. 验收标准

- [ ] 任务 017 已完成并保留其未提交改动，本任务没有绕开其领域模型和工作流。
- [ ] 入选论文可生成结构化证据卡片，每张卡片有可核对原文和稳定来源定位。
- [ ] 全文不可用时只生成明确的摘要级证据，不伪造页码、章节或原文。
- [ ] 用户可确认、修改和否决证据，重跑不覆盖用户已审核产物。
- [ ] 论文比较、覆盖与冲突结果都能反查具体证据，而不是孤立模型总结。
- [ ] 证据不足时可以受控地复用任务 017 启动补充检索，不无限循环。
- [ ] 报告事实性结论引用真实论文与证据，引用可从报告反查到原文定位。
- [ ] 报告支持不覆盖的历史版本、Markdown 和基于真实元数据的 BibTeX 导出。
- [ ] 研究 LangGraph 已扩展阅读、证据、审核、分析、比较和报告节点，且可取消、恢复和观测。
- [ ] WebUI 可完成项目、计划、论文、证据、比较、报告和执行时间线的主要操作。
- [ ] 页面刷新、重新登录或服务重启后，研究状态和产物仍可继续。
- [ ] 新研究入口未破坏现有侧边栏、智能对话、RAG、知识库和手动 Agent 能力。
- [ ] 所有研究 API 和 WebUI 操作按当前用户隔离，跨用户访问被拒绝。
- [ ] 自动测试不连接真实模型、外部网络、Phoenix 或 `chatchat-study-data`。
- [ ] `git diff HEAD` 中没有超出任务 017/018 边界的修改。
- [ ] 所有要求的定向与关键回归测试通过，或准确记录无法运行的原因。

## 12. 本任务不包含

- Multi-Agent 研究团队和可视化低代码工作流编辑器；
- GraphRAG、独立学术搜索引擎和大规模离线索引；
- Word/PDF 排版、APA/GB/T 7714 等完整格式引擎；
- 云端协作、团队空间、复杂 ACL 和自动投稿；
- 大规模评测中心、Prompt 管理平台和生产部署硬化。

这些能力在第一版论文研究业务闭环经真实使用验证后再评估。

## 13. Claude 执行报告

Claude 完成后将状态改为 `ready_for_codex_review`，并填写以下内容。不要改写本任务
前面的目标、范围和验收标准。

### 修改文件

阶段 A（原文与可定位证据）：

- 新增 `libs/chatchat-server/chatchat/server/research/fulltext.py`：原文单元
  切分与定位（page/section/block_index/content_hash）、provider 链
  （远程 PDF → 本地知识库 → 摘要降级）与失败隔离；
- 新增 `chatchat/server/research/evidence.py`：`EvidenceDraft`/
  `EvidenceExtraction`、`find_quote` 逐字核对、`parse_evidence_payload`、
  默认提取器；
- 新增 `chatchat/server/research/evidence_service.py`：`ingest_paper_fulltext`、
  `extract_paper_evidence`、证据审核（确认/编辑/驳回/删除）、重新核对；
- `chatchat/server/db/models/research_model.py`：`PaperUnitModel`，
  `EvidenceCardModel` 追加 quote/locator/review_status/dedup_key 等，
  `ProjectPaperModel` 追加 `fulltext_state/unit_count/fulltext_hash`；
- `chatchat/server/db/migrate/migrations.py`：v9（`paper_unit` + 加列）。

阶段 B（覆盖、比较、冲突、有界补充检索）：

- 新增 `chatchat/server/research/comparison.py`（`EvidenceRef`、
  `ComparisonDraft`、`ConflictDraft`、payload 解析与引用校验）；
- 新增 `chatchat/server/research/comparison_service.py`（维度读写、
  `generate_project_comparisons`、冲突裁决、`start_supplementary_run`）；
- 新增 `chatchat/server/research/coverage.py`（covered/thin/missing 判定、
  缺失维度、补充检索建议，纯计算可重现）；
- `research_model.py`：`ComparisonUnitModel`、`ResearchConflictModel`，
  `ResearchProjectModel.dimensions_json`，`ResearchRunModel.parent_run_id/round_no`；
- `migrations.py`：v10。

阶段 C（版本化报告与导出）：

- 新增 `chatchat/server/research/report.py`（`[[e:<id>]]` 稳定引用标记、
  `validate_report_body`、`render_markdown` 脚注、BibTeX 导出、无证据结论
  降级为"证据不足与推测"）；
- 新增 `chatchat/server/research/report_service.py`（`generate_project_report`、
  `save_report_edit` 追加新版本、导出）；
- `research_model.py`：`ResearchReportModel` 追加 `report_key/run_id/status/
  citations_json/evidence_ids_json/unsupported/generated_by/error`；
- `migrations.py`：v11（旧报告 `report_key` 回填为自身 id）。

阶段 D（React SPA 研究界面，按用户决定不做 Streamlit 页面）：

- `frontend/src/api/research.ts`：原文/单元/证据/维度/比较/冲突/覆盖/
  补充检索/报告全部端点的类型化客户端；
- 新增 `frontend/src/components/research/{labels.ts,reportText.ts,
  EvidencePanel.tsx,ComparisonPanel.tsx,CoveragePanel.tsx,ReportPanel.tsx,
  PaperSourceActions.tsx}`；
- `frontend/src/pages/ResearchPage.tsx`（页签改为 URL 同步 `?tab=`，接入
  证据/比较/覆盖/报告面板与逐篇"取原文/看单元/提取证据"）、
  `frontend/src/pages/ResearchRunPage.tsx`（阶段徽章、两个人工复核门禁提示
  与"前往复核"跳转、"生成证据与报告"按钮、阶段产物摘要与告警）；
- `frontend/src/index.css`：`.markdown-body` 报告排版（Tailwind preflight
  会清空默认样式）；
- 新增 `frontend/src/components/research/reportText.test.ts`、
  扩充 `frontend/src/api/research.test.ts`。

阶段 E（研究 LangGraph 扩展与串联）：

- `chatchat/server/research/graph.py`：新增
  `read_selected_papers → extract_evidence → wait_for_evidence_review →
  analyze_coverage_and_conflicts → build_comparison → generate_report →
  wait_for_report_review → finalize_artifacts` 节点与条件边；017 既有节点
  加"按阶段跳过"守卫；阶段产物先落库再推进；每阶段写入 Agent Run 步骤；
- `chatchat/server/research/runtime.py`、`service.py`：注入
  `extractor/unit_providers/kb_names/comparison_generator/report_generator`；
  新增 `run_research_artifacts`（`completed → running` 重开产物阶段，
  新建关联 Agent Run 时间线）；
- `chatchat/server/db/repository/research_repository.py`：`RUN_STAGES` 扩展 +
  `STAGE_ORDER`、`merge_run_summary`、`pause_run_for_review`、
  `reopen_run_for_artifacts`；`set_stage` 改为**只前进不后退**并在失败时告警
  （原生 SQL 后 `expire_all`，避免同会话读到过期阶段）；
  `converge_run_best_effort` 不再把人工门禁挂起误判为 failed；
- `chatchat/server/db/models/research_model.py`：`ck_research_run_stage`
  取值扩展；
- `chatchat/server/db/migrate/migrations.py`：**新增 v12
  `research_run_stage_artifacts`**（SQLite 无法对已有表增删 CHECK，按官方
  做法重建 `research_run`：列交集拷贝全部行 → 改名 → 重建索引；幂等，
  遇到预期之外的列直接失败回滚，行 id = LangGraph thread_id 不变）；
- `chatchat/server/api_server/research_routes.py`：A–E 全部端点
  （含 `POST /research/runs/{rid}/artifacts`），owner 只取自认证上下文，
  非本人统一 404、状态不允许 409、字段非法 400；
- 新增 `tests/unit_tests/test_research_artifact_loop.py`（14 项，含 v12 迁移
  保留数据/幂等/未知列中止三向验证）。

因新增 v12 而同步的既有测试期望（仅版本号断言）：
`test_db_migrate.py`、`test_research_project.py`、`test_research_papers.py`、
`test_research_workflow.py`、`test_research_evidence.py`、
`test_research_comparison.py`、`test_research_report.py`、`test_agent_run.py`、
`test_agent_runtime_langgraph.py`、`test_user_memory.py`、
`test_smart_conversation.py`、`test_intent_routing.py`。

### 关键设计选择

- 原文定位：`paper_unit` 保存用户私有的原文块与稳定定位，槽位
  `(project_id, paper_id, unit_kind, block_index)` 唯一，重解析按内容哈希
  幂等替换；摘要降级单元 `page=None`，UI 与脚注一律显示"摘要级证据"，
  绝不显示页码。
- 证据校验：模型输出的每条 `quote` 必须能在**实际喂给模型的单元**里
  逐字定位（忽略空白差异），核对不上直接拒绝并回传原因，不入库为已核对
  证据；`dedup_key` 部分唯一索引使重复提取只产生 `unchanged`；已人工审核
  （confirmed/edited/rejected）的卡片不被重跑覆盖。
- 比较与冲突：比较单元必须引用本项目真实证据且
  `(project_id, paper_id, dimension_id)` 唯一；冲突两侧各自引用证据卡片，
  `dedup_key` 去重，人工裁决写入结论；无效引用被拒绝并如实返回。
- 覆盖与补充检索：covered/thin/missing 判定纯计算、可重现；建议复用 017
  计划里的检索式；补充检索是**可见的新运行**（`parent_run_id/round_no`），
  轮次有上限（超限 409），不自循环。
- 引用与报告版本：报告正文用稳定标记 `[[e:<evidence_id>]]`，渲染成 `[^n]`
  脚注并把映射持久化在 `citations_json`，引用可反查证据卡片；无证据结论
  自动降级为"证据不足与推测"；BibTeX 只用真实论文元数据。版本按
  `report_key` 只追加，编辑保存新版本，旧版本不覆盖。
- 报告去噪（阶段 E 补强）：`generate_project_report` 在**正文与标题与现有
  最新版本完全一致**时不再出版本，返回现有版本并标 `unchanged: true`，
  使闭环重放/重复点击不会堆积噪声版本。
- 研究图：沿用 017 的单一 LangGraph 与 014 checkpoint（`run_id ==
  thread_id`），新增节点在同一图内；`stage` 是恢复与重放的事实来源，
  节点按 `STAGE_ORDER` 跳过已完成阶段，因此门禁恢复/崩溃重放不会重复
  联网检索、重复入库或重复出版本；`set_stage` 只前进不后退（重放已完成的
  阶段不得把标记倒回，否则人工门禁会反复挂起）。
- 人工确认点：证据复核与报告复核复用 017 的 `awaiting_confirmation`
  状态（不新增状态值），由 `stage=evidence_review/report_review` 表明复核
  对象；继续执行仍走 `POST /research/runs/{rid}/confirm`。崩溃发生在复核
  之后时，重放不再要求用户重复复核。
- 产物入口：`POST /research/runs/{rid}/artifacts` 仅允许 `completed` 重开
  （`failed` 走 resume 重试，`cancelled` 尊重用户意图不复活），每次产物执行
  关联一条新的 Agent Run 时间线，各阶段以 `tool_call` 步骤进入既有时间线。
- WebUI 状态：研究页五个页签写进 URL，运行页门禁卡片可"前往复核"直达对应
  页签；运行页显示阶段产物计数、缺证据维度与被拒绝/告警条目；报告经
  `react-markdown + remark-gfm` 渲染（不启用 rehype-raw，模型输出不注入
  HTML）。

### 测试命令与结果

后端（固定 Conda 环境，临时 SQLite + 临时 `CHATCHAT_ROOT`，不触碰
`chatchat-study-data`）：

```bash
cd libs/chatchat-server
CHATCHAT_ROOT=<临时目录> \
/home/lab239/anaconda3/envs/Langchain-Chatchat/bin/python -m pytest tests/unit_tests -q
# 562 passed（018 开始前基线 476；新增 018 测试：阶段 A/B/C/E 共 86 项）
```

阶段 E 定向：`pytest tests/unit_tests/test_research_artifact_loop.py -q` →
**14 passed**，覆盖：未纳入论文时如实收尾（不调模型）、完整闭环在
`evidence_review`/`report_review` 两处挂起后完成、Agent Run 步骤时间线、
Phoenix 业务 span 关联 run/project/agent_run、重放不重复检索/入库/出版本、
门禁中取消后不能再打开、状态不允许 409、跨用户 404、报告失败 → failed
且既有产物保留并可 resume 完成、原文取不到时降级为摘要级且不编造页码、
v12 迁移保留旧行与索引 + 幂等 + 未知列失败回滚。

任务 017 联合回归：`pytest tests/unit_tests/test_research_workflow.py -q` →
**21 passed**（图扩展后行为不变）。

前端：

```bash
cd frontend
npm run typecheck   # 通过，无错误
npm test            # 58 passed（含新增 runArtifacts 契约与 reportText 用例）
npm run build       # 成功（dist 已重建；仅有 chunk >500kB 的既有体积告警）
```

未运行的测试：`tests/api/`（需要真实服务进程与模型，离线不可跑）；
`pytest tests/unit_tests` 之外的集成/端到端套件本任务未新增。

### 未完成项与风险

- 真实模型/真实原文未验证：全部证据/比较/报告测试用假提取器与假生成器，
  未连真实 LLM、ArXiv、PDF 下载、本地知识库或 Phoenix。真实提取质量、
  PDF 解析成功率、摘要降级的实际比例需在配置好模型与网络后手工验证。
- 浏览器手工验收未做：SPA 研究页（取原文→提取证据→复核→比较→覆盖→
  报告→导出）只由 vitest 契约测试与 typecheck/build 覆盖，未在浏览器里
  跑通完整流程。
- v12 迁移在真实库上未执行：本次只在临时库验证升级/幂等/失败回滚；
  真实 `chatchat-study-data` 数据库未读取也未升级（按任务禁止项）。首次
  启动真实服务时会执行 v12 重建 `research_run`，建议 Codex 在审查后按
  协作流程先备份再升级验证。
- 重放语义的取舍：用户显式再次"生成证据与报告"会重跑原文读取与证据提取
  （幂等，计数为 `unchanged`），但报告在内容完全一致时不出版本；如果
  期望"重跑也一定产生新版本"，需要产品层面确认。
- 阶段跳过依赖 `stage` 单调：若将来新增阶段或允许回退阶段，必须同步
  调整 `RUN_STAGES`/`STAGE_ORDER` 与守卫，否则会重复副作用；v12 的
  CHECK 取值与 `RUN_STAGES` 是两处定义，已由测试钉住但需人工保持同步。
- 报告内容一致性判据是"正文+标题完全相同"：极长标题（>500 字符被截断）
  或仅元数据变化会被视为新内容并出版本。
- 覆盖分析在每次进入该阶段时重算（阶段已完成则跳过）：若用户在报告复核
  门禁回头改证据，需要重新走一次产物阶段才会刷新覆盖统计。

## 14. Codex 审查记录

- 审查状态：`pending`
- 主要发现：<由 Codex 填写>
- 补充修改：<由 Codex 填写>
- 最终验证：<由 Codex 填写>
- 交付 commit：<由 Codex 在最终报告中记录；文档内可保留为空>
