"""师生协作论文检索与文献结论数据模型（《学生研究与导师审核功能重新设计方案》里程碑3）。

七张表，各自回答一件事实，互不越界（§15.3、§16.4）：

- ``supervision_literature_search_task``（``LS-001``）：论文检索任务。创建时
  **必须**绑定一个已确认并共享的 ``ResearchBrief`` 版本，并把那一版的结构化内容
  复制进 ``brief_snapshot``（§4.4「原检索任务保留原依据」）。研究定义后来出了新
  版本，本任务绑定的仍是当时那一版；是否重新检索由学生决定，因此这里存的是
  ``brief_version`` + 快照，不是「跟着研究定义走」的外键。
- ``supervision_literature_subtask``：``LS`` 内部可单独调度、保存、取消和重试的
  子任务（检索方案 / 搜索与去重 / 低成本排序 / 摘要级分析 / 单篇深度提取 /
  多篇比较 / 文献结论）。每个子任务有自己的状态与结果计数，**单个失败不影响
  其它子任务与整个任务**（§15.3 第3、6条）；失败原因与历史留在行里，不静默清掉。
- ``supervision_literature_result``（``P-001``）：项目内论文记录。三种幂等键
  （规范化 DOI、规范化 arXiv ID、规范化标题+年份）都建了项目内部分唯一索引，
  重复入库只产生一条记录（§6.3 第4条）。关联度是**可解释的规则结果**：等级 +
  命中理由 + 命中的研究定义要素 + 缺失要素 + 规则版本，没有模型分数时
  ``relevance_score`` 恒为 ``NULL``，不伪造「95/91/94」这类伪精确数值（§6.3.B）。
  模型语义分析单独写在 ``semantic_analysis``，与规则字段并列展示，不覆盖它们。
- ``supervision_literature_extraction``：单篇深度提取结果，按版本追加
  （``version`` 递增，旧版只读），必须带原文定位；只有带定位的原文片段才可能
  被标成「已核对证据」（§16.4 第5条）。
- ``supervision_literature_evidence_card``（``EC-001``）：证据卡片，保存可核对
  原文、定位、支持关系与幂等键；不静默覆盖（§4.0、§15.3 第8条）。
- ``supervision_literature_conclusion``（``LC-001`` + ``version``）：文献研究结论。
  必须区分「论文原文明确支持的事实」「学生综合判断」「尚未验证的假设」三类
  （§4.5），并绑定它依据的文献集合快照与其哈希。文献集合之后变化**不改写旧
  结论**：由快照哈希现算出「依据已变化」标记（见仓储层 ``literature_changed``），
  重新形成结论是同一 ``code`` 的新版本，旧版本标 ``superseded`` 保留。
- ``supervision_literature_citation``（``CS-001``）：引用快照。按**具体成果**
  授权（一篇论文记录 / 一张证据卡片 / 一条结论），把授权当时可见的字段与内容
  哈希复制固定下来（§9.1）。导师只能读到这份快照，不能顺着对象关系浏览学生
  其它私人搜索；来源之后变化不回改历史快照，学生撤销授权只关闭后续引用，
  已进入审核历史的快照仍在审计里（§13）。

刻意没有建的东西：

- **没有「自动深度提取全部候选」的字段或计数器**：深度提取只能由学生对单篇
  发起，且受 ``deep_extract_limit`` 上限约束（§16.4 第3条）。
- **没有把私人论文研究项目的整库挂进来**：私人材料只有 ``CS`` 快照这一条入口，
  本模块不写 ``research_project`` / ``paper_record`` / ``evidence_card`` 任何一行。
- **没有把 AI 结论写进导师结论字段**：``mentor_state`` 只记录导师对文献结论本身
  的确认/质疑/要求补充证据，与里程碑1 的 ``supervision_review_ac`` 无关。

取值全部写成 ``CHECK``，与 ``supervision_work_model`` / ``supervision_research_model``
同一风格：服务层漏判时库层仍然兜住。
"""
from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)

from chatchat.server.db.base import Base

# 可读编号前缀（§4.0 的 ID 表：检索运行 LS-，项目论文记录 P-，证据卡片 EC-，
# 文献结论 LC-，引用快照 CS-）。
CODE_SEARCH_TASK = "LS"
CODE_RESULT = "P"
CODE_EVIDENCE_CARD = "EC"
CODE_CONCLUSION = "LC"
CODE_CITATION = "CS"

#: 检索任务状态（§4.4：草稿、待执行、检索中、待筛选、已形成文献结论、已关闭）。
TASK_STATUS_VALUES = ("draft", "pending", "searching", "triage", "concluded", "closed")
#: 检索目标性质（§4.4「要验证、补充或反驳什么」）。
TASK_PURPOSE_VALUES = ("verify", "supplement", "refute", "explore")
#: 子任务种类（§15.3 第3条拆出来的六类动作 + 形成结论的记账）。
SUBTASK_KIND_VALUES = (
    "plan",               # 检索方案（一次短调用，学生确认后才搜索）
    "search",             # 搜索与去重（按来源 × 关键词批次）
    "rank",               # 低成本规则排序
    "abstract_analysis",  # 摘要级关联分析（有上限）
    "deep_extract",       # 单篇深度提取（学生选篇后才跑）
    "compare",            # 多篇比较
    "conclusion",         # 形成文献结论（记账，不调模型）
)
#: 子任务状态。``queued`` 留给后续后台 worker，首版由请求同步执行。
SUBTASK_STATUS_VALUES = ("pending", "queued", "running", "completed", "failed", "cancelled")
#: 关联等级（§6.3.B：高相关、相关、可参考、低相关；不展示伪精确五维分数）。
RELEVANCE_LEVEL_VALUES = ("high", "relevant", "reference", "low")
#: 关联度分数来源。``rule_keyword`` 是词面规则；只有真正跑过模型任务才允许
#: 出现 ``model_semantic``，且必须与规则结果分开展示。
RELEVANCE_SOURCE_VALUES = ("rule_keyword", "model_semantic")
#: 推荐动作（§6.3.B：优先阅读、可参考、低相关）。
RECOMMENDED_ACTION_VALUES = ("priority_read", "reference", "low_relevant")
#: 学生对候选论文的决定（§4.4 纳入、排除、稍后阅读；pending 是尚未决定）。
DECISION_VALUES = ("pending", "included", "excluded", "read_later")
#: 单篇深度提取状态（与子任务状态同步，冗余在结果上便于列表展示）。
EXTRACT_STATE_VALUES = ("none", "queued", "running", "completed", "failed", "cancelled")
#: 全文可用性（与现有 ``paper_record.fulltext_status`` 同一套取值）。
FULLTEXT_STATUS_VALUES = ("unknown", "abstract", "url", "pdf")
#: 文献结论状态：草稿、已确认、被新版本替代（§4.0「确认后版本化」）。
CONCLUSION_STATUS_VALUES = ("draft", "confirmed", "superseded")
#: 导师对文献结论的态度（§4.5：确认、质疑、要求补充证据；空串=未处理）。
MENTOR_REVIEW_STATE_VALUES = ("", "confirmed", "questioned", "evidence_required")
#: 引用快照的来源种类（§9.1 按具体成果授权，不按整个私人项目授权）。
CITATION_SOURCE_KIND_VALUES = (
    "project_result",        # 本项目内的论文记录
    "conclusion",            # 本项目内的文献结论
    "private_paper",         # 学生私人论文研究项目里的论文记录
    "private_evidence_card", # 学生私人论文研究项目里的证据卡片
)
#: 引用快照的用途（导师审核 / 研究方案 / 提交材料 / 进展）。
CITATION_PURPOSE_VALUES = ("mentor_review", "research_plan", "submission", "progress")
#: 证据卡片的支持关系（与里程碑4 的 AI 支持关系轴同一思路，独立于等级）。
SUPPORT_RELATION_VALUES = (
    "",
    "supports",
    "partially_supports",
    "contradicts",
    "conflicting",
    "background",
)
#: 证据卡片与提取证据的层级。摘要级证据没有页码，不能冒充全文定位。
EVIDENCE_LEVEL_VALUES = ("fulltext", "abstract")
#: 证据类型（与现有 ``evidence_card.evidence_type`` 对齐，便于将来复用）。
EVIDENCE_TYPE_VALUES = (
    "",
    "objective",
    "method",
    "dataset",
    "metric",
    "result",
    "conclusion",
    "limitation",
    "assumption",
)


class SupervisionLiteratureSearchTaskModel(Base):
    """论文检索任务 ``LS-001``：研究定义与论文流水线之间的正式连接（§4.4）。"""

    __tablename__ = "supervision_literature_search_task"
    __table_args__ = (
        UniqueConstraint("project_id", "code", name="uq_supervision_lit_task_code"),
        Index("ix_supervision_lit_task_project_status", "project_id", "status"),
        Index("ix_supervision_lit_task_brief", "brief_id"),
        CheckConstraint(
            "status IN ('draft', 'pending', 'searching', 'triage', 'concluded', "
            "'closed')",
            name="ck_supervision_lit_task_status",
        ),
        CheckConstraint(
            "purpose IN ('verify', 'supplement', 'refute', 'explore')",
            name="ck_supervision_lit_task_purpose",
        ),
        CheckConstraint(
            "result_limit >= 1 AND result_limit <= 200",
            name="ck_supervision_lit_task_result_limit",
        ),
        # 深度提取上限是硬闸门：一次误操作不能造出无上限的后台任务（§6.3.1）。
        CheckConstraint(
            "deep_extract_limit >= 0 AND deep_extract_limit <= 20",
            name="ck_supervision_lit_task_deep_limit",
        ),
        CheckConstraint(
            "brief_version >= 1", name="ck_supervision_lit_task_brief_version"
        ),
    )

    id = Column(String(32), primary_key=True)
    # 对外可读编号 LS-001；审计、通知与界面引用它，不引用内部 id。
    code = Column(String(16), nullable=False)
    project_id = Column(String(32), nullable=False)

    # --- 绑定的研究定义依据（§4.3.1 确认门槛、§4.4）-----------------------
    brief_id = Column(String(32), nullable=False)
    brief_code = Column(String(16), nullable=False, default="")
    # 创建时固定的那一版；研究定义后来出新版本也不跟着走。
    brief_version = Column(Integer, nullable=False)
    brief_kind = Column(String(32), nullable=False, default="")
    brief_confirmed_by = Column(String(32), nullable=False, default="")
    brief_confirmed_at = Column(DateTime, nullable=True)
    # 依据内容快照（结构化字段原样复制）：页面顶部必须显示依据与确认时间，
    # 且历史任务始终能回答「当时按什么检索」。
    brief_snapshot = Column(JSON, nullable=False, default=dict)

    # --- 检索方案输入（§6.3.A）-------------------------------------------
    # 检索目标：要验证、补充或反驳什么（自由文本 + 性质标签）。
    purpose = Column(String(16), nullable=False, default="verify")
    goal = Column(Text, nullable=False, default="")
    keywords = Column(JSON, nullable=False, default=list)
    synonyms = Column(JSON, nullable=False, default=list)
    exclude_terms = Column(JSON, nullable=False, default=list)
    sources = Column(JSON, nullable=False, default=list)
    year_from = Column(Integer, nullable=True)
    year_to = Column(Integer, nullable=True)
    require_fulltext = Column(Boolean, nullable=False, default=False)
    result_limit = Column(Integer, nullable=False, default=30)
    deep_extract_limit = Column(Integer, nullable=False, default=3)

    # --- 模型生成、学生确认后的检索方案（§6.3.1 第1步）-------------------
    # {queries:[{query,source,rationale}], exclude_terms, year_from, year_to,
    #  note}；启动检索时固定下来，之后改研究定义不影响本次方案。
    search_plan = Column(JSON, nullable=False, default=dict)
    search_plan_note = Column(Text, nullable=False, default="")
    search_plan_model = Column(String(120), nullable=False, default="")
    search_plan_prompt_version = Column(String(32), nullable=False, default="")
    search_plan_generated_at = Column(DateTime, nullable=True)
    search_plan_confirmed_at = Column(DateTime, nullable=True)
    search_plan_confirmed_by = Column(String(32), nullable=False, default="")

    status = Column(String(16), nullable=False, default="pending")

    # --- 检索过程可见性（§6.3.A「不能只显示最终结果而隐藏检索过程」）----
    hits_total = Column(Integer, nullable=False, default=0)
    deduped_total = Column(Integer, nullable=False, default=0)
    stored_total = Column(Integer, nullable=False, default=0)
    # [{source, query, reason}]：失败来源必须显示，不隐藏。
    source_failures = Column(JSON, nullable=False, default=list)
    searched_at = Column(DateTime, nullable=True)

    concluded_at = Column(DateTime, nullable=True)
    closed_at = Column(DateTime, nullable=True)
    close_reason = Column(Text, nullable=False, default="")

    created_by = Column(String(32), nullable=False)
    created_role = Column(String(16), nullable=False, default="")
    create_time = Column(DateTime, nullable=True)
    update_time = Column(DateTime, nullable=True)
    # 软删除：已产生结果或结论的任务作为项目审计资产保留（§13）。
    deleted_at = Column(DateTime, nullable=True)
    delete_reason = Column(Text, nullable=False, default="")


class SupervisionLiteratureSubtaskModel(Base):
    """``LS`` 子任务：可单独调度、保存、取消与重试，失败不外溢（§15.3 第3、6条）。"""

    __tablename__ = "supervision_literature_subtask"
    __table_args__ = (
        UniqueConstraint("search_task_id", "code", name="uq_supervision_lit_subtask_code"),
        Index("ix_supervision_lit_subtask_task_kind", "search_task_id", "kind"),
        Index("ix_supervision_lit_subtask_project_status", "project_id", "status"),
        CheckConstraint(
            "kind IN ('plan', 'search', 'rank', 'abstract_analysis', 'deep_extract', "
            "'compare', 'conclusion')",
            name="ck_supervision_lit_subtask_kind",
        ),
        CheckConstraint(
            "status IN ('pending', 'queued', 'running', 'completed', 'failed', "
            "'cancelled')",
            name="ck_supervision_lit_subtask_status",
        ),
        CheckConstraint(
            "attempt >= 1", name="ck_supervision_lit_subtask_attempt",
        ),
    )

    id = Column(String(32), primary_key=True)
    # 形如 ``LS-001-SE01``：可读、可按种类数数，审计里能直接定位到子任务。
    code = Column(String(32), nullable=False)
    project_id = Column(String(32), nullable=False)
    search_task_id = Column(String(32), nullable=False)
    search_task_code = Column(String(16), nullable=False, default="")

    kind = Column(String(32), nullable=False)
    status = Column(String(16), nullable=False, default="pending")
    # 第几次尝试；重试不删历史，旧失败进 ``failure_history``。
    attempt = Column(Integer, nullable=False, default=1)

    # 子任务输入：search 是 {source, query, limit}；deep_extract 是
    # {result_code}；compare 是 {result_codes}；plan 是 {note}。
    source = Column(String(50), nullable=False, default="")
    query = Column(Text, nullable=False, default="")
    payload = Column(JSON, nullable=False, default=dict)

    # 结果计数：命中、新入库、去重合并（§6.3.A 要求显示批次/来源/去重数量）。
    result_total = Column(Integer, nullable=False, default=0)
    new_total = Column(Integer, nullable=False, default=0)
    duplicate_total = Column(Integer, nullable=False, default=0)

    error_code = Column(String(64), nullable=False, default="")
    error_message = Column(Text, nullable=False, default="")
    # [{attempt, error_code, error_message, finished_at}]
    failure_history = Column(JSON, nullable=False, default=list)

    # 每次实际模型调用的取用/释放记录（§8.2「容量控制的最小单位是一次实际
    # 生成模型调用」的业务侧证据）；真正的并发上限证据由 model_capacity 侧输出。
    model_calls = Column(JSON, nullable=False, default=list)
    # 已登记的消费者类型：research_plan / paper_extract / paper_compare。
    consumer = Column(String(32), nullable=False, default="")
    model_name = Column(String(120), nullable=False, default="")
    prompt_version = Column(String(32), nullable=False, default="")

    queued_at = Column(DateTime, nullable=True)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    cancelled_by = Column(String(32), nullable=False, default="")
    cancelled_at = Column(DateTime, nullable=True)
    cancel_reason = Column(Text, nullable=False, default="")

    created_by = Column(String(32), nullable=False)
    created_role = Column(String(16), nullable=False, default="")
    create_time = Column(DateTime, nullable=True)
    update_time = Column(DateTime, nullable=True)


class SupervisionLiteratureResultModel(Base):
    """项目论文记录 ``P-001``：去重后的候选论文 + 可解释关联度 + 学生决定。"""

    __tablename__ = "supervision_literature_result"
    __table_args__ = (
        UniqueConstraint("project_id", "code", name="uq_supervision_lit_result_code"),
        # 三种幂等键都在库层兜住：服务层漏判时也不会出现同一篇论文两条记录。
        UniqueConstraint("project_id", "dedup_key", name="uq_supervision_lit_result_dedup"),
        Index(
            "uq_supervision_lit_result_doi",
            "project_id",
            "doi",
            unique=True,
            sqlite_where=text("doi IS NOT NULL AND doi <> ''"),
            postgresql_where=text("doi IS NOT NULL AND doi <> ''"),
        ),
        Index(
            "uq_supervision_lit_result_arxiv",
            "project_id",
            "arxiv_id",
            unique=True,
            sqlite_where=text("arxiv_id IS NOT NULL AND arxiv_id <> ''"),
            postgresql_where=text("arxiv_id IS NOT NULL AND arxiv_id <> ''"),
        ),
        Index(
            "uq_supervision_lit_result_title",
            "project_id",
            "title_key",
            unique=True,
            sqlite_where=text("title_key IS NOT NULL AND title_key <> ''"),
            postgresql_where=text("title_key IS NOT NULL AND title_key <> ''"),
        ),
        Index("ix_supervision_lit_result_project_decision", "project_id", "decision"),
        Index("ix_supervision_lit_result_task", "search_task_id"),
        Index("ix_supervision_lit_result_project_level", "project_id", "relevance_level"),
        CheckConstraint(
            "decision IN ('pending', 'included', 'excluded', 'read_later')",
            name="ck_supervision_lit_result_decision",
        ),
        CheckConstraint(
            "relevance_level IN ('high', 'relevant', 'reference', 'low')",
            name="ck_supervision_lit_result_level",
        ),
        CheckConstraint(
            "recommended_action IN ('priority_read', 'reference', 'low_relevant')",
            name="ck_supervision_lit_result_action",
        ),
        CheckConstraint(
            "relevance_score_source IN ('rule_keyword', 'model_semantic')",
            name="ck_supervision_lit_result_score_source",
        ),
        CheckConstraint(
            "extract_state IN ('none', 'queued', 'running', 'completed', 'failed', "
            "'cancelled')",
            name="ck_supervision_lit_result_extract_state",
        ),
        CheckConstraint(
            "fulltext_status IN ('unknown', 'abstract', 'url', 'pdf')",
            name="ck_supervision_lit_result_fulltext",
        ),
    )

    id = Column(String(32), primary_key=True)
    code = Column(String(16), nullable=False)
    project_id = Column(String(32), nullable=False)
    # 首次发现它的检索任务与搜索子任务（§6.3.A 要求能回到批次与来源）。
    search_task_id = Column(String(32), nullable=False)
    subtask_id = Column(String(32), nullable=False, default="")

    # --- 幂等键（§6.3 第4条：DOI / arXiv ID / 规范化标题三种）------------
    # 最强键的字符串形式，例如 ``doi:10.1/abc`` / ``arxiv:2101.00001`` /
    # ``title:xxx:2021``；同一项目内唯一。
    dedup_key = Column(String(200), nullable=False)
    doi = Column(String(200), nullable=True)
    arxiv_id = Column(String(100), nullable=True)
    title_key = Column(String(200), nullable=True)

    # --- 元数据快照（来源变化不回改已入库记录，历史可回溯）--------------
    title = Column(String(1000), nullable=False)
    authors = Column(String(2000), nullable=False, default="")
    year = Column(Integer, nullable=True)
    abstract = Column(String(8000), nullable=False, default="")
    url = Column(String(1000), nullable=True)
    source = Column(String(50), nullable=False, default="")
    # 去重合并进来的其它来源：[{source,url}]，去重不丢可追溯性。
    sources = Column(JSON, nullable=False, default=list)
    fulltext_status = Column(String(16), nullable=False, default="unknown")

    # --- 可解释关联度（§6.3.B：等级 + 理由 + 命中与缺失，不展示黑盒分数）
    relevance_level = Column(String(16), nullable=False, default="reference")
    relevance_reason = Column(Text, nullable=False, default="")
    # [{field,label,term,where}]：命中了研究定义里的哪些要素。
    relevance_matched = Column(JSON, nullable=False, default=list)
    # [{field,label,note}]：哪些要素还没确认（例如「评价指标尚未确认」）。
    relevance_missing = Column(JSON, nullable=False, default=list)
    relevance_score_source = Column(String(32), nullable=False, default="rule_keyword")
    # 只有真正跑过模型语义评分才有值；规则排序下恒为 NULL。
    relevance_score = Column(Integer, nullable=True)
    relevance_rules_version = Column(String(32), nullable=False, default="")
    recommended_action = Column(String(24), nullable=False, default="reference")
    # 摘要级模型关联分析的结果，单独存、单独展示为「AI语义分析」，
    # 不覆盖规则字段（§6.3.B 最后一段）。
    # {level, reason, matched, missing, model, prompt_version, subtask_code, at}
    semantic_analysis = Column(JSON, nullable=False, default=dict)

    # --- 学生决定（§4.4、§16.4 第4条；导师可见但不是决策方）-------------
    decision = Column(String(16), nullable=False, default="pending")
    decision_reason = Column(Text, nullable=False, default="")
    decided_by = Column(String(32), nullable=False, default="")
    decided_role = Column(String(16), nullable=False, default="")
    decided_at = Column(DateTime, nullable=True)
    # 纳入状态与理由留历史（§4.0 版本规则）。
    decision_history = Column(JSON, nullable=False, default=list)

    extract_state = Column(String(16), nullable=False, default="none")

    first_seen_at = Column(DateTime, nullable=True)
    create_time = Column(DateTime, nullable=True)
    update_time = Column(DateTime, nullable=True)


class SupervisionLiteratureExtractionModel(Base):
    """单篇深度提取结果：按版本追加，必须带原文定位（§6.3.C、§16.4 第5条）。"""

    __tablename__ = "supervision_literature_extraction"
    __table_args__ = (
        UniqueConstraint("result_id", "version", name="uq_supervision_lit_extraction_version"),
        Index("ix_supervision_lit_extraction_project_result", "project_id", "result_id"),
        CheckConstraint(
            "level IN ('fulltext', 'abstract')", name="ck_supervision_lit_extraction_level"
        ),
        CheckConstraint(
            "version >= 1", name="ck_supervision_lit_extraction_version",
        ),
    )

    id = Column(String(32), primary_key=True)
    project_id = Column(String(32), nullable=False)
    result_id = Column(String(32), nullable=False)
    result_code = Column(String(16), nullable=False, default="")
    # 重跑深度提取是新版本，旧版本只读保留（§13「AI 结果版本化」）。
    version = Column(Integer, nullable=False, default=1)
    subtask_id = Column(String(32), nullable=False, default="")
    subtask_code = Column(String(32), nullable=False, default="")
    # 摘要级提取没有页码，不能声称全文定位。
    level = Column(String(16), nullable=False, default="abstract")

    summary = Column(Text, nullable=False, default="")
    research_problem = Column(Text, nullable=False, default="")
    method = Column(Text, nullable=False, default="")
    algorithm = Column(Text, nullable=False, default="")
    experiments = Column(Text, nullable=False, default="")
    results_metrics = Column(Text, nullable=False, default="")
    limitations = Column(Text, nullable=False, default="")
    # 与当前项目的差异（§6.3.D 的六个问题）：
    # {same, different, borrowable, usable_methods, baseline_methods,
    #  conflicting_assumptions, gaps, suggested_tasks}
    differences = Column(JSON, nullable=False, default=dict)
    # 原文定位证据：[{evidence_type, quote, locator:{section,page_start,
    #  page_end,line_start,line_end,char_start,char_end}, quote_verified, level}]
    # 只有 ``quote_verified`` 为真的片段算「已核对证据」。
    evidence = Column(JSON, nullable=False, default=list)

    model_name = Column(String(120), nullable=False, default="")
    prompt_version = Column(String(32), nullable=False, default="")
    created_by = Column(String(32), nullable=False)
    created_role = Column(String(16), nullable=False, default="")
    create_time = Column(DateTime, nullable=True)
    superseded_at = Column(DateTime, nullable=True)


class SupervisionLiteratureEvidenceCardModel(Base):
    """证据卡片 ``EC-001``：可核对原文 + 定位 + 支持关系，不静默覆盖（§15.3 第8条）。"""

    __tablename__ = "supervision_literature_evidence_card"
    __table_args__ = (
        UniqueConstraint("project_id", "code", name="uq_supervision_lit_card_code"),
        UniqueConstraint("project_id", "dedup_key", name="uq_supervision_lit_card_dedup"),
        Index("ix_supervision_lit_card_project_result", "project_id", "result_id"),
        CheckConstraint(
            "evidence_type IN ('', 'objective', 'method', 'dataset', 'metric', "
            "'result', 'conclusion', 'limitation', 'assumption')",
            name="ck_supervision_lit_card_type",
        ),
        CheckConstraint(
            "level IN ('fulltext', 'abstract')", name="ck_supervision_lit_card_level"
        ),
        CheckConstraint(
            "support_relation IN ('', 'supports', 'partially_supports', "
            "'contradicts', 'conflicting', 'background')",
            name="ck_supervision_lit_card_support",
        ),
    )

    id = Column(String(32), primary_key=True)
    code = Column(String(16), nullable=False)
    project_id = Column(String(32), nullable=False)
    result_id = Column(String(32), nullable=False)
    result_code = Column(String(16), nullable=False, default="")
    extraction_id = Column(String(32), nullable=True)
    extraction_version = Column(Integer, nullable=False, default=0)

    evidence_type = Column(String(32), nullable=False, default="")
    summary = Column(Text, nullable=False, default="")
    # 可核对原文片段；模型改写、没有原文定位的内容不能进这张表。
    quote = Column(Text, nullable=False)
    locator = Column(JSON, nullable=False, default=dict)
    # 1=引文在该结果已保存的文本里逐字校验通过；模型自述的改写不算已核对。
    quote_verified = Column(Boolean, nullable=False, default=False)
    level = Column(String(16), nullable=False, default="abstract")
    # 这条证据对当前研究问题是支持、部分支持、冲突还是背景。
    support_relation = Column(String(24), nullable=False, default="")
    # 关联到研究定义的哪个要素/子问题。
    brief_field = Column(String(32), nullable=False, default="")
    sub_question = Column(String(500), nullable=False, default="")

    # 幂等键：同一结果 + 同一类型 + 同一定位 + 同一引文只有一张卡片，
    # 重试与重复点击不会堆出重复卡片。
    dedup_key = Column(String(64), nullable=False, default="")

    created_by = Column(String(32), nullable=False)
    created_role = Column(String(16), nullable=False, default="")
    create_time = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=True)
    deleted_at = Column(DateTime, nullable=True)
    delete_reason = Column(Text, nullable=False, default="")


class SupervisionLiteratureConclusionModel(Base):
    """文献研究结论 ``LC-001``：三类内容分开写，绑定文献快照（§4.5）。"""

    __tablename__ = "supervision_literature_conclusion"
    __table_args__ = (
        # 同一 code 的多个版本；旧版本只标 superseded，不改写内容（§13）。
        UniqueConstraint("code", "version", name="uq_supervision_lit_conclusion_version"),
        Index("ix_supervision_lit_conclusion_project_status", "project_id", "status"),
        Index("ix_supervision_lit_conclusion_project_code", "project_id", "code"),
        Index("ix_supervision_lit_conclusion_task", "search_task_id"),
        CheckConstraint(
            "status IN ('draft', 'confirmed', 'superseded')",
            name="ck_supervision_lit_conclusion_status",
        ),
        CheckConstraint(
            "mentor_state IN ('', 'confirmed', 'questioned', 'evidence_required')",
            name="ck_supervision_lit_conclusion_mentor_state",
        ),
        CheckConstraint(
            "version >= 1", name="ck_supervision_lit_conclusion_version",
        ),
        CheckConstraint(
            "literature_count >= 0",
            name="ck_supervision_lit_conclusion_count",
        ),
    )

    id = Column(String(32), primary_key=True)
    code = Column(String(16), nullable=False)
    version = Column(Integer, nullable=False, default=1)
    project_id = Column(String(32), nullable=False)
    search_task_id = Column(String(32), nullable=True)
    search_task_code = Column(String(16), nullable=False, default="")

    statement = Column(Text, nullable=False)
    # 三类内容必须分开（§4.5、场景 G 第8步）：
    # [{statement, result_code, evidence_card_code, locator}]
    supported_facts = Column(JSON, nullable=False, default=list)
    # [{statement, basis_result_codes}]
    student_judgements = Column(JSON, nullable=False, default=list)
    # [{statement, how_to_verify}]
    unverified_hypotheses = Column(JSON, nullable=False, default=list)
    # 研究空白（§15.3 第9条）
    research_gaps = Column(JSON, nullable=False, default=list)

    # --- 依据的文献快照版本（§4.5、§13）--------------------------------
    # [{result_code, title, dedup_key, decision, decision_reason, decided_at,
    #   update_time, relevance_level}]
    literature_snapshot = Column(JSON, nullable=False, default=list)
    # 快照内容哈希：与当前纳入集合现算的哈希不一致即「依据已变化」，
    # 由此把旧结论标成历史，而不是改写它。
    literature_snapshot_hash = Column(String(64), nullable=False, default="")
    literature_count = Column(Integer, nullable=False, default=0)
    # [{code, result_code, evidence_type, quote}]
    evidence_cards = Column(JSON, nullable=False, default=list)

    status = Column(String(16), nullable=False, default="draft")
    confirmed_by = Column(String(32), nullable=False, default="")
    confirmed_at = Column(DateTime, nullable=True)
    confirm_note = Column(Text, nullable=False, default="")

    # 导师对文献结论本身的态度（§4.5）；不写进里程碑1 的导师验收结论。
    mentor_state = Column(String(24), nullable=False, default="")
    mentor_note = Column(Text, nullable=False, default="")
    mentor_by = Column(String(32), nullable=False, default="")
    mentor_role = Column(String(16), nullable=False, default="")
    mentor_at = Column(DateTime, nullable=True)

    # 被新版本替代：只标记，不删行、不改内容。
    superseded_at = Column(DateTime, nullable=True)
    superseded_by_code = Column(String(16), nullable=False, default="")
    superseded_by_version = Column(Integer, nullable=False, default=0)

    created_by = Column(String(32), nullable=False)
    created_role = Column(String(16), nullable=False, default="")
    create_time = Column(DateTime, nullable=True)
    update_time = Column(DateTime, nullable=True)
    deleted_at = Column(DateTime, nullable=True)
    delete_reason = Column(Text, nullable=False, default="")


class SupervisionLiteratureCitationModel(Base):
    """引用快照 ``CS-001``：按具体成果授权，固定授权当时的字段与内容哈希（§9.1）。"""

    __tablename__ = "supervision_literature_citation"
    __table_args__ = (
        UniqueConstraint("project_id", "code", name="uq_supervision_lit_citation_code"),
        Index("ix_supervision_lit_citation_project_purpose", "project_id", "purpose"),
        Index("ix_supervision_lit_citation_project_kind", "project_id", "source_kind"),
        Index("ix_supervision_lit_citation_owner", "source_owner_id"),
        Index("ix_supervision_lit_citation_bound", "bound_object_type", "bound_object_code"),
        CheckConstraint(
            "source_kind IN ('project_result', 'conclusion', 'private_paper', "
            "'private_evidence_card')",
            name="ck_supervision_lit_citation_source_kind",
        ),
        CheckConstraint(
            "purpose IN ('mentor_review', 'research_plan', 'submission', 'progress')",
            name="ck_supervision_lit_citation_purpose",
        ),
    )

    id = Column(String(32), primary_key=True)
    code = Column(String(16), nullable=False)
    # 被授权的项目。快照不默认跨项目复用，每增加一个项目都要重新授权（§9.1）。
    project_id = Column(String(32), nullable=False)

    source_kind = Column(String(32), nullable=False)
    source_ref = Column(String(32), nullable=False)
    source_code = Column(String(32), nullable=False, default="")
    # 成果的所有者（私人材料的所有人），也是授权的实质来源。
    source_owner_id = Column(String(32), nullable=False)

    purpose = Column(String(32), nullable=False)
    # 引用落点：导师审核 MR-001 / 研究方案 / 提交 S-001-v1 / 进展 PR-001。
    bound_object_type = Column(String(32), nullable=False, default="")
    bound_object_code = Column(String(32), nullable=False, default="")

    # 授权当时可见的字段副本（标题、作者、年份、来源、摘要、URL、定位等）。
    snapshot = Column(JSON, nullable=False, default=dict)
    # 内容版本与哈希：来源后来变化时，历史快照仍然能证明「当时看到的是什么」。
    source_version = Column(String(64), nullable=False, default="")
    content_hash = Column(String(64), nullable=False, default="")

    granted_by = Column(String(32), nullable=False)
    granted_role = Column(String(16), nullable=False, default="")
    granted_at = Column(DateTime, nullable=True)
    note = Column(Text, nullable=False, default="")

    # 撤销只关闭后续引用，历史快照不消失（§9.1、§13）。
    revoked_at = Column(DateTime, nullable=True)
    revoked_by = Column(String(32), nullable=False, default="")
    revoke_reason = Column(Text, nullable=False, default="")

    create_time = Column(DateTime, nullable=True)


__all__ = [
    "CITATION_PURPOSE_VALUES",
    "CITATION_SOURCE_KIND_VALUES",
    "CODE_CITATION",
    "CODE_CONCLUSION",
    "CODE_EVIDENCE_CARD",
    "CODE_RESULT",
    "CODE_SEARCH_TASK",
    "CONCLUSION_STATUS_VALUES",
    "DECISION_VALUES",
    "EVIDENCE_LEVEL_VALUES",
    "EVIDENCE_TYPE_VALUES",
    "EXTRACT_STATE_VALUES",
    "FULLTEXT_STATUS_VALUES",
    "MENTOR_REVIEW_STATE_VALUES",
    "RECOMMENDED_ACTION_VALUES",
    "RELEVANCE_LEVEL_VALUES",
    "RELEVANCE_SOURCE_VALUES",
    "SUBTASK_KIND_VALUES",
    "SUBTASK_STATUS_VALUES",
    "SUPPORT_RELATION_VALUES",
    "SupervisionLiteratureCitationModel",
    "SupervisionLiteratureConclusionModel",
    "SupervisionLiteratureEvidenceCardModel",
    "SupervisionLiteratureExtractionModel",
    "SupervisionLiteratureResultModel",
    "SupervisionLiteratureSearchTaskModel",
    "SupervisionLiteratureSubtaskModel",
    "TASK_PURPOSE_VALUES",
    "TASK_STATUS_VALUES",
]
