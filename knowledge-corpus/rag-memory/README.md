# RAG 与长期记忆公开论文语料库

本目录是任务 009/010 的本地语料产物：围绕 **RAG 基础、检索/重排、高级 RAG、Agent 与 LLM 长期记忆**
采集的开放获取论文 PDF 与可追溯清单。仅用于在本机手动构建测试知识库、验证
Langchain-Chatchat 的入库/检索/引用与长期记忆效果，**不自动上传**，不连接真实数据库、
模型服务或知识库 API。

## 1. 数据集说明

- 来源：arXiv 开放获取论文（元数据来自 `https://export.arxiv.org/api`，非搜索页转载）。
- 数量：以 `catalog.jsonl` 中 `status: success` 的记录为准（目标 20–30 篇，实际见文末）。
- 主题分布（以 `seeds.jsonl` 与 `catalog.jsonl` 为准，共 25 篇候选）：
  - **RAG 基础（6）**：RAG 原文、RAG 综述、DPR、Contriever、HyDE、Lost in the Middle
  - **检索/重排（4）**：ColBERT、ColBERTv2、ChatGPT 重排器、Dense X Retrieval
  - **高级 RAG（6）**：Self-RAG、CRAG、RAPTOR、GraphRAG、FLARE、HippoRAG
  - **Agent（4）**：ReAct、MemGPT、Generative Agents、Toolformer
  - **长期记忆（5）**：MemoryBank、A-MEM、Mem0、From RAG to Memory、Agent 记忆机制综述
- 每篇成功记录包含：唯一 ID（`arxiv-<id>`）、arXiv 编号、标题、作者、年份、主题、
  abs/PDF 来源 URL、许可说明、本地相对路径、SHA-256、语言、关键词、中文概要、
  英文摘要（API 原文）。
- 去重：按 arXiv 编号与 SHA-256 双重去重。
- 校验：PDF 魔数 `%PDF`、非零大小、SHA-256 与文件一致；损坏或 HTML 伪装文件不纳入成功清单。

## 2. 目录结构

```
knowledge-corpus/rag-memory/
├── collect.py        # 采集/校验脚本（可重复执行）
├── seeds.jsonl       # 候选种子（arXiv ID、期望标题片段、主题、关键词、中文概要）
├── catalog.jsonl     # 最终清单（UTF-8 JSONL，每行一篇）
├── failures.jsonl    # 失败记录（每个 arXiv ID 最新一条）
├── documents/        # 下载的 PDF（被 .gitignore 忽略）
└── README.md
```

## 3. 再次执行方式

使用 Conda 环境 `Langchain-Chatchat`（不依赖额外包，仅标准库）：

```bash
# 采集/补齐（断点跳过已存在且校验正确的文件；失败项会被重试）
/home/lab239/anaconda3/envs/Langchain-Chatchat/bin/python knowledge-corpus/rag-memory/collect.py

# 只校验（清单可解析、必填字段、编号/SHA 无重复、PDF 存在非空且魔数正确、
# 成功数 >= 20 且 <= 30；失败返回非 0）
/home/lab239/anaconda3/envs/Langchain-Chatchat/bin/python knowledge-corpus/rag-memory/collect.py --validate

# 调试：本次最多处理 N 个候选
/home/lab239/anaconda3/envs/Langchain-Chatchat/bin/python knowledge-corpus/rag-memory/collect.py --limit 3
```

重复执行行为：已存在且校验一致 → 跳过不下载；本地文件缺失或与清单校验值不一致 →
重新下载；失败项每 ID 只保留最新一条，不会无限追加。

## 4. 手动入库步骤（Langchain-Chatchat）

1. 启动 WebUI 后进入「知识库」页（或 API `POST /kb/upload`）。
2. 新建测试知识库（例如 `rag-memory-test`），勾选需要验证的文档上传：
   `documents/` 下对应 `catalog.jsonl` 的 `local_path`。
3. 上传后等待切分与向量化完成，再进行下述检索/记忆测试。
4. 测试完可删除该知识库，不影响公共知识库；本目录不随测试自动清理。

## 5. 适合的测试场景

| 场景 | 建议文档 |
| --- | --- |
| 单文档精确检索 | `2005.11401`（RAG 原文）、`2112.09118`（Contriever） |
| 多文档综合/对比 | `2312.10997`（RAG 综述）+ `2005.11401` + `2310.11511`（Self-RAG） |
| 长上下文/位置偏置 | `2307.03172`（Lost in the Middle） |
| 重排/检索质量 | `2004.12832`（ColBERT）、`2112.01488`（ColBERTv2） |
| 高级 RAG/自反馈 | `2405.14831`（HippoRAG）、`2310.11511`（Self-RAG） |
| Agent 记忆/记忆流 | `2304.03442`（Generative Agents）、`2502.12110`（A-MEM） |
| 长期记忆/个性化 | `2504.19413`（Mem0）、`2502.14802`（From RAG to Memory）、`2404.13501`（记忆机制综述） |
| 引用准确性 | 以上任意 + 中文概要/关键词组合提问 |

> 具体可用文档以 `catalog.jsonl` 实际成功项为准；失败项见 `failures.jsonl`。

## 6. 建议验收问题（8–12 个）

1. RAG 原文提出的“参数化记忆 + 非参数化记忆”组合方式是什么？
2. RAG 综述把知识获取分为哪两大类？检索增强生成包含哪些环节？
3. DPR 的双塔密集检索相比 BM25 的改进点是什么？
4. Contriever 用含噪检索数据做对比预训练的目标是什么？
5. HyDE 如何不借助标注实现零样本密集检索？
6. Lost in the Middle 发现 LLM 对长上下文不同位置信息的利用有什么规律？
7. ColBERT 的“延迟交互”机制相比整句向量检索的优势是什么？
8. Self-RAG 的反思 token 用来评估检索与生成的哪些维度？
9. CRAG 在检索结果置信度低时采取什么修正策略？
10. RAPTOR 的层级摘要树如何支持多跳问答？
11. GraphRAG 如何回答“全局性”问题（社区发现+分层摘要）？
12. 比较 A-MEM 与 Mem0：它们分别如何组织、提炼和更新 Agent 的长期记忆？

## 7. .gitignore 说明

`documents/*.pdf` 与 `documents/*.part` 不纳入版本库（本机数据产物）；
脚本、种子、清单、失败记录与 README 保留为可审查文本。

## 8. 实际执行结果（任务 009/010 运行）

- 采集命令：`python collect.py`（见任务文档 Claude 执行报告）
- 校验命令：`python collect.py --validate`
- 成功/失败数量、总大小、重复执行跳过行为：见下方任务报告（本 README 不重复记录数值）。
