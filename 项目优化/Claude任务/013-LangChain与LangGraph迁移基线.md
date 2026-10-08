# 任务 013：LangChain 1.x 与 LangGraph 1.x 迁移基线

## 1. 状态与基线

- 状态：`ready_for_claude`
- 负责人：Claude Code
- 任务编写与审查：Codex
- 目标分支：`master`
- 源码基线 commit：`2efa2eb8b76d2b75034869ffbf49cf14ebd0553d`
- 允许存在于源码基线之后的前置提交：仅本任务文档、Claude任务索引和项目优化导航文档

源码基线表示依赖迁移前的业务代码版本。任务文档提交可以位于该基线之后，Claude
应确认基线之后没有未声明的业务代码提交，不要求 `HEAD` 与源码基线完全相等。

## 2. 执行前检查

Claude 开始修改前执行：

```bash
git status --short --branch
git rev-parse HEAD
git diff --name-status 2efa2eb8b76d2b75034869ffbf49cf14ebd0553d..HEAD
git stash list
```

当前工作区已知存在以下本机运行内容，不属于本任务，不得读取、修改、删除、暂存或
提交：

- `chatchat-study-data/data/knowledge_base/info.db`
- `chatchat-study-data/data/logs/chatchat.log`
- `chatchat-study-data/model_settings.yaml`

如果分支不正确、出现其他来源不明的改动、存在未知 stash，或基线之后混入未声明的
业务代码，停止编码并报告用户。

## 3. 背景

项目当前依赖：

- Python 3.10，Conda环境 `Langchain-Chatchat`；
- `langchain 0.1.17`；
- `langchain-core 0.1.53`；
- `langchain-community 0.0.36`；
- `langchain-openai 0.0.6`；
- 尚未安装LangGraph。

现有 `AgentRunner` 手工实现了计划、工具执行、观察、重规划、超时、取消、Run/Step
持久化和SSE事件。这套实现已经可用，但论文研究助手后续需要更多条件分支、长任务
恢复、人工确认和领域子流程，继续扩展手写循环会增加维护成本。

目标架构将采用LangGraph 1.x作为Agent编排运行时。不过，稳定版LangGraph 1.x依赖
现代版 `langchain-core`，不能直接安装到当前0.1.x依赖栈。本任务先完成依赖迁移基线
和最小LangGraph能力验证，不在同一任务中重写现有Agent业务流程。

参考资料：

- <https://docs.langchain.com/oss/python/langgraph/overview>
- <https://docs.langchain.com/oss/python/langgraph/persistence>
- <https://docs.langchain.com/oss/python/langgraph/interrupts>
- <https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/pyproject.toml>

## 4. 本任务目标

1. 将项目的LangChain相关依赖迁移到与稳定版LangGraph 1.x兼容的现代版本组合。
2. 修复依赖升级造成的导入、类型和调用兼容问题，使现有主要功能保持可导入、可测试。
3. 增加一个独立、最小、离线的LangGraph兼容性原型，验证状态图、条件分支、异步流式
   事件和本地checkpoint能力可用于后续Agent迁移。
4. 建立清晰的Agent运行时迁移边界，但本任务不替换现有 `AgentRunner` 默认实现。
5. 记录最终依赖版本、主要兼容修改和仍需后续任务处理的问题。

## 5. 任务边界

Claude可以根据实际依赖关系修改完成迁移所必需的代码，不逐文件限制，通常会涉及：

- `libs/chatchat-server/pyproject.toml`及实际使用的依赖声明；
- 受LangChain 1.x API变化影响的模型、工具、Retriever、Document、Callback和Agent相关代码；
- 新增的LangGraph兼容性原型与离线测试；
- 必要的开发说明；
- 本文档末尾的“Claude执行报告”。

允许修改范围以“让现有项目在现代LangChain依赖下恢复等价行为”为准。可以修复真实的
兼容问题，不要求为了遵守文件清单而保留已废弃API；但不要顺便重构无关业务功能或
改变产品行为。

## 6. 明确禁止事项

- 不执行 `git commit`、`git push`、`git reset`、`git checkout`、`git clean` 或
  `git stash`；
- 不读取或修改 `chatchat-study-data/`、真实数据库、日志、模型文件、密钥和 `.env`；
- 不使用base环境或系统Python，固定使用Conda环境 `Langchain-Chatchat`；
- 不安装LangGraph 0.x或其他旧版本来迁就LangChain 0.1.x；
- 不从Git分支、未发布wheel或不明镜像安装框架；
- 不在本任务中删除现有 `AgentRunner`，也不切换生产默认执行器；
- 不在本任务中实现论文研究工作流、Multi-Agent或大规模WebUI改版；
- 不重建数据库、知识库和向量索引来解决兼容问题；
- 不为了测试连接真实LLM、FastJev、外部API、MCP服务或用户数据库。

## 7. 依赖迁移要求

### 7.1 版本选择

- 使用执行任务时可获得的稳定版LangChain 1.x和LangGraph 1.x；
- LangGraph必须是1.x稳定版，不能使用alpha、beta、rc或开发版；
- `langchain-core`、`langchain-community`、`langchain-openai`、
  `langchain-experimental`等相关包选择彼此兼容的稳定版本；
- 优先使用官方PyPI包和官方包元数据确认依赖，不凭经验硬凑版本；
- 在执行报告中记录最终安装版本和选择理由。

如果解析器表明升级必须同时更换Python主版本、删除当前核心功能，或存在无法在合理
范围内解决的硬冲突，不要强行安装一个表面可用的组合。保留现场、记录冲突链并停止，
交由Codex重新决定迁移方案。

### 7.2 环境与安装授权

本任务明确允许Claude在以下解释器对应的项目环境中安装、升级和卸载完成迁移所需的
Python依赖：

```text
/home/lab239/anaconda3/envs/Langchain-Chatchat/bin/python
```

使用 `python -m pip`，禁止使用base环境的pip。不要安装系统级软件，不要改动另一个
`FastJev` Conda环境。

安装前后分别记录关键包版本。完成后运行 `python -m pip check`。依赖声明必须同步写入
项目配置，不能只把包临时安装进当前机器而不更新源码中的依赖定义。

## 8. 兼容改造原则

1. 优先采用LangChain 1.x公开API，不通过复制旧包源码或长期兼容补丁伪装升级完成。
2. 保持现有API、数据库、用户隔离、公共知识库和WebUI行为不变。
3. 模型和工具工厂的对外接口尽量保持稳定，减少后续LangGraph迁移与业务代码耦合。
4. 可以增加小型兼容适配层集中处理新旧接口差异，避免在大量业务文件中重复判断版本。
5. 对已从LangChain拆分到独立provider包的功能，使用官方推荐包和导入路径。
6. 如果某个旧依赖已无兼容版本，只在确认项目没有使用时移除；仍被使用时选择等价替代，
   并补充针对行为的测试。
7. 不因依赖升级修改数据库数据或重新生成知识库索引。

## 9. 最小LangGraph原型

新增一个不接管生产请求的离线原型或测试夹具，用来验证后续迁移需要的能力。具体模块
位置和命名由Claude结合项目结构决定，但不得成为第二套正式Agent业务实现。

原型至少包含：

- 一个TypedDict或等价强类型Graph State；
- 三个以上节点；
- 至少一条条件边；
- 一个会回到前置节点的有界循环；
- 异步调用或异步事件流；
- 内存或临时SQLite Checkpointer；
- 使用稳定 `thread_id` 完成状态恢复验证；
- 全部节点使用假模型、假工具和临时数据。

建议用当前Agent闭环的最小语义表达：

```text
plan → execute → decide
                  ├─ replan → execute
                  ├─ answer → END
                  └─ fail   → END
```

原型的目的只是证明现代依赖和关键LangGraph机制可用，不要求接入Chat API、WebUI或真实
`AgentRun/AgentStep`。

## 10. 后续架构边界

本任务应为下一任务预留一个简单、明确的Agent运行接口方向，例如：

```python
class AgentRuntime:
    async def stream(...): ...
    async def resume(...): ...
    async def cancel(...): ...
    async def get_state(...): ...
```

本任务可以定义接口或设计说明，但不要为了“看起来已经迁移”而把未完成的LangGraph实现
接到生产路径。

后续计划为：

1. 任务014：用LangGraph迁移现有单Agent闭环并保留旧执行器回退；
2. 任务015：将普通对话与自动Agent合并为智能对话总图；
3. 任务016：实现论文研究领域子图。

## 11. 测试要求

所有命令使用：

```text
/home/lab239/anaconda3/envs/Langchain-Chatchat/bin/python
```

至少执行：

```bash
python -m pip check
python -m pytest libs/chatchat-server/tests/unit_tests/test_langgraph_compat.py -q
python -m pytest libs/chatchat-server/tests/unit_tests/test_agent_run.py \
  libs/chatchat-server/tests/unit_tests/test_intent_routing.py -q
```

如原型测试采用其他合理文件名，相应替换第一条pytest路径。

还应选择受兼容修改直接影响的RAG、长期记忆、会话隔离或工具测试进行定向回归。测试
必须使用临时数据库、假模型和假工具，不触发真实模型下载、GPU推理和外部网络请求。

如果既有测试因确认存在的环境问题不能结束，可以中止该组并准确记录，不能声称通过。

## 12. 验收标准

- [ ] 项目依赖声明使用相互兼容的稳定版LangChain 1.x与LangGraph 1.x。
- [ ] `Langchain-Chatchat`环境可以导入LangChain、LangGraph及项目关键模块。
- [ ] `python -m pip check`通过，或只有已解释且不影响运行的既有问题。
- [ ] 最小LangGraph原型覆盖状态、条件边、有界循环、异步流和checkpoint恢复。
- [ ] 现有AgentRunner仍是默认生产实现，API、SSE和WebUI行为没有被提前切换。
- [ ] Agent、意图路由及受影响模块的定向测试通过。
- [ ] 没有读取、修改或提交 `chatchat-study-data` 和其他本机运行数据。
- [ ] 依赖版本、兼容改动、测试结果和后续风险完整写入执行报告。
- [ ] `git diff HEAD`没有与本任务无关的修改。

## 13. Claude执行报告

Claude完成后将状态改为 `ready_for_codex_review`，并填写以下内容。不要改写本任务前面
的目标、范围和验收标准。

### 最终依赖版本

<待填写：LangChain、Core、Community、OpenAI、Experimental、LangGraph及checkpoint包>

### 修改文件

<待填写>

### 关键兼容改造

<待填写>

### 测试命令与结果

<待填写；失败、跳过或未运行必须如实说明>

### 未完成项与风险

<待填写>

## 14. Codex审查记录

- 审查状态：`pending`
- 主要发现：<由Codex填写>
- 补充修改：<由Codex填写>
- 最终验证：<由Codex填写>
- 交付commit：<由Codex填写>
