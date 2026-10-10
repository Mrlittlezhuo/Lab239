"""论文研究领域模型（任务 017 阶段 A）。

归属边界（任务 017 §5.1/§6）：

- ``research_project`` / ``research_run`` / ``project_paper`` /
  ``evidence_card`` / ``research_report`` 是**用户私有**资源：所有读写
  同时按 ``id + owner_id`` 过滤，owner 只来自服务端认证上下文；
  子资源（项目论文、证据、报告）经所属项目继承所有权，与
  message 经 conversation 继承的模式一致。
- ``paper_record`` 是**公开论文元数据**的规范记录，可在项目间复用；
  只保存公开可溯源信息（标题/作者/年份/摘要/DOI/ArXiv ID/稳定 URL/
  来源列表），不保存任何用户私有文件路径或私有上传内容；项目内的
  筛选状态、纳入/排除原因与笔记全部挂在 ``project_paper``，不共享。

研究运行（``research_run``）的 ``id`` 同时是研究 LangGraph 的
``thread_id``（复用任务 014 的 checkpoint 存储），并通过
``agent_run_id`` 关联现有 Agent Run 时间线（任务 017 §5.3）。
"""
from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)

from chatchat.server.db.base import Base


class ResearchProjectModel(Base):
    """研究项目：用户私有的长期研究对象（名称、研究问题、范围）。"""

    __tablename__ = "research_project"
    __table_args__ = (
        Index("ix_research_project_owner_update", "owner_id", "update_time"),
        CheckConstraint(
            "status IN ('active', 'archived')", name="ck_research_project_status"
        ),
    )

    id = Column(String(32), primary_key=True, comment="项目ID(UUID hex，服务端生成)")
    owner_id = Column(
        String(32),
        nullable=False,
        index=True,
        comment="所属用户ID（服务端写入，来自认证上下文）",
    )
    name = Column(String(200), nullable=False, comment="项目名称")
    research_question = Column(
        String(2000), nullable=False, comment="研究问题"
    )
    scope = Column(
        String(2000), nullable=False, default="", comment="研究范围/约束（可空）"
    )
    status = Column(
        String(16), nullable=False, default="active", comment="active/archived"
    )
    current_stage = Column(
        String(32), nullable=False, default="", comment="最近一次运行的当前阶段（展示用）"
    )
    # --- 任务 018 阶段 B：用户可增删/调整的比较维度（空=用默认维度） ---
    dimensions_json = Column(
        String(4000),
        nullable=False,
        default="",
        comment="比较维度列表（JSON：[{id,label}]；空表示使用默认维度）",
    )
    create_time = Column(DateTime, default=func.now(), comment="创建时间")
    update_time = Column(DateTime, default=func.now(), onupdate=func.now(), comment="更新时间")

    def __repr__(self):
        return (
            f"<ResearchProject(id='{self.id}', owner_id='{self.owner_id}', "
            f"name='{self.name}', status='{self.status}')>"
        )


class ResearchRunModel(Base):
    """研究运行：一次"计划→确认→检索→去重→筛选→持久化"的可恢复执行。

    ``id`` 即研究 LangGraph 的 ``thread_id``；``agent_run_id`` 关联现有
    ``agent_run`` 行（时间线/取消/Trace 复用，不建第二套执行器）。

    状态机::

        pending -> awaiting_confirmation -> running -> completed | failed | cancelled
        （pending/awaiting_confirmation/running -> cancelled）

    ``stage`` 记录持久化的阶段位置（恢复与展示的事实来源之一）：
    ``draft -> plan -> retrieval -> dedup -> screening -> persist ->
    fulltext -> evidence -> evidence_review -> coverage -> comparison ->
    report -> report_review -> done``（任务 018 阶段 E 扩展；两个人工复核点
    复用 ``awaiting_confirmation`` 状态，由 ``stage`` 表明复核对象）。
    """

    __tablename__ = "research_run"
    __table_args__ = (
        Index("ix_research_run_project", "project_id"),
        Index("ix_research_run_owner_create", "owner_id", "create_time"),
        CheckConstraint(
            "status IN ('pending', 'awaiting_confirmation', 'running', "
            "'completed', 'failed', 'cancelled')",
            name="ck_research_run_status",
        ),
        CheckConstraint(
            "stage IN ('draft', 'plan', 'retrieval', 'dedup', 'screening', "
            "'persist', 'fulltext', 'evidence', 'evidence_review', 'coverage', "
            "'comparison', 'report', 'report_review', 'done')",
            name="ck_research_run_stage",
        ),
    )

    id = Column(String(32), primary_key=True, comment="运行ID(= LangGraph thread_id)")
    project_id = Column(String(32), nullable=False, comment="所属研究项目")
    owner_id = Column(
        String(32), nullable=False, comment="所属用户（冗余存储，便于直接鉴权）"
    )
    plan_json = Column(
        String(8000),
        nullable=False,
        default="",
        comment="研究计划（JSON：summary/sub_questions/queries/sources）",
    )
    status = Column(
        String(16),
        nullable=False,
        default="pending",
        comment="pending/awaiting_confirmation/running/completed/failed/cancelled",
    )
    stage = Column(
        String(32), nullable=False, default="draft", comment="当前/最后阶段"
    )
    agent_run_id = Column(
        String(32), nullable=True, comment="关联的 agent_run.id（时间线/取消/Trace）"
    )
    sources_json = Column(
        String(4000),
        nullable=False,
        default="[]",
        comment="各来源命中/失败统计（JSON 列表）",
    )
    summary_json = Column(
        String(8000),
        nullable=False,
        default="",
        comment="完成后的结构化摘要（各来源命中数、去重数、候选数）",
    )
    error = Column(String(1000), nullable=True, comment="失败/取消错误摘要")
    # --- 任务 018 阶段 B：补充检索是有界的新运行，不是无限自循环 ---
    parent_run_id = Column(
        String(32), nullable=True, comment="补充检索运行的父运行（首轮为空）"
    )
    round_no = Column(
        Integer, nullable=False, default=1, comment="第几轮检索（含补充检索，上限受控）"
    )
    create_time = Column(DateTime, default=func.now(), comment="创建时间")
    start_time = Column(DateTime, nullable=True, comment="确认并开始执行时间")
    end_time = Column(DateTime, nullable=True, comment="终态时间")
    update_time = Column(DateTime, default=func.now(), onupdate=func.now(), comment="更新时间")

    def __repr__(self):
        return (
            f"<ResearchRun(id='{self.id}', project_id='{self.project_id}', "
            f"status='{self.status}', stage='{self.stage}')>"
        )


class PaperRecordModel(Base):
    """公开论文元数据的规范记录（跨项目复用，非用户私有）。

    只保存公开可溯源信息；``sources_json`` 保留全部来源（去重合并时
    不丢失可追溯性）。用户私有上传文件不得进入本表（任务 017 §5.1）。
    """

    __tablename__ = "paper_record"
    __table_args__ = (
        # 稳定标识的部分唯一索引：同一 DOI / ArXiv ID 只有一条规范记录
        Index(
            "ux_paper_record_doi",
            "doi",
            unique=True,
            sqlite_where=text("doi IS NOT NULL AND doi <> ''"),
        ),
        Index(
            "ux_paper_record_arxiv_id",
            "arxiv_id",
            unique=True,
            sqlite_where=text("arxiv_id IS NOT NULL AND arxiv_id <> ''"),
        ),
        Index("ix_paper_record_title_key", "title_key"),
    )

    id = Column(String(32), primary_key=True, comment="论文ID(UUID hex，服务端生成)")
    title = Column(String(1000), nullable=False, comment="标题")
    authors = Column(String(2000), nullable=False, default="", comment="作者（分号连接）")
    year = Column(Integer, nullable=True, comment="年份")
    abstract = Column(String(8000), nullable=False, default="", comment="摘要（截断存储）")
    doi = Column(String(200), nullable=True, comment="规范化 DOI（小写、去 URL 前缀）")
    arxiv_id = Column(
        String(100), nullable=True, comment="规范化 ArXiv ID（去版本后缀/URL）"
    )
    url = Column(String(1000), nullable=True, comment="主稳定来源 URL")
    source = Column(String(50), nullable=False, default="", comment="首次发现来源名")
    sources_json = Column(
        String(4000),
        nullable=False,
        default="[]",
        comment="合并后的来源列表（JSON：[{source,url,year}]）",
    )
    fulltext_status = Column(
        String(16),
        nullable=False,
        default="unknown",
        comment="全文可用状态：unknown/abstract/url/pdf",
    )
    title_key = Column(
        String(200), nullable=True, comment="规范化标题+年份（去重兜底键）"
    )
    create_time = Column(DateTime, default=func.now(), comment="创建时间")
    update_time = Column(DateTime, default=func.now(), onupdate=func.now(), comment="更新时间")

    def __repr__(self):
        return (
            f"<PaperRecord(id='{self.id}', doi='{self.doi}', "
            f"arxiv_id='{self.arxiv_id}', title='{self.title[:40]}')>"
        )


class ProjectPaperModel(Base):
    """论文在某项目内的私有关系（来源、相关度、筛选状态）。"""

    __tablename__ = "project_paper"
    __table_args__ = (
        UniqueConstraint("project_id", "paper_id", name="uq_project_paper"),
        Index("ix_project_paper_owner", "owner_id"),
        Index("ix_project_paper_project_screen", "project_id", "screening_status"),
        CheckConstraint(
            "screening_status IN ('pending_review', 'included', 'excluded')",
            name="ck_project_paper_screening_status",
        ),
    )

    id = Column(String(32), primary_key=True, comment="关系ID(UUID hex)")
    project_id = Column(String(32), nullable=False, comment="所属项目（所有权继承）")
    paper_id = Column(String(32), nullable=False, comment="指向 paper_record")
    owner_id = Column(
        String(32), nullable=False, comment="所属用户（与项目一致，冗余便于鉴权）"
    )
    source = Column(String(50), nullable=False, default="", comment="本项目中的发现来源")
    relevance = Column(Float, nullable=True, comment="相关度分数（筛选节点计算）")
    screening_status = Column(
        String(16),
        nullable=False,
        default="pending_review",
        comment="pending_review/included/excluded",
    )
    screening_reason = Column(
        String(1000), nullable=False, default="", comment="纳入/排除原因"
    )
    first_run_id = Column(String(32), nullable=True, comment="首次引入该论文的运行")

    # --- 任务 018 阶段 A：本项目内该论文的全文可得性（私有，不写公开表） ---
    fulltext_state = Column(
        String(16),
        nullable=False,
        default="unknown",
        comment="unknown/none/abstract/fulltext（原文单元可得性）",
    )
    unit_count = Column(Integer, nullable=False, default=0, comment="已入库原文单元数")
    fulltext_hash = Column(
        String(64), nullable=False, default="", comment="单元集合哈希（幂等重解析）"
    )
    fulltext_time = Column(DateTime, nullable=True, comment="最近一次解析时间")

    create_time = Column(DateTime, default=func.now(), comment="创建时间")
    update_time = Column(DateTime, default=func.now(), onupdate=func.now(), comment="更新时间")

    def __repr__(self):
        return (
            f"<ProjectPaper(id='{self.id}', project_id='{self.project_id}', "
            f"paper_id='{self.paper_id}', screening='{self.screening_status}', "
            f"fulltext='{self.fulltext_state}')>"
        )


class PaperUnitModel(Base):
    """论文原文单元（任务 018 阶段 A）：带稳定定位的可核对文本块。

    - **私有**：随项目/用户隔离，用户私有全文绝不进入 ``paper_record``；
    - ``unit_kind``：``block``（全文块，带 page/section）或 ``abstract``
      （仅有摘要时的降级单元，``page`` 必须为 NULL，绝不伪造页码）；
    - ``(project_id, paper_id, unit_kind, block_index)`` 唯一：重跑解析
      按内容哈希幂等替换，不产生重复单元；
    - ``page``/``section``/``block_index``/``content_hash`` 一起构成可重现
      的来源定位，证据卡片快照这些值后即使单元被替换仍可核对。
    """

    __tablename__ = "paper_unit"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "paper_id", "unit_kind", "block_index", name="uq_paper_unit_slot"
        ),
        Index("ix_paper_unit_project_paper", "project_id", "paper_id"),
        Index("ix_paper_unit_owner", "owner_id"),
        CheckConstraint(
            "unit_kind IN ('block', 'abstract')", name="ck_paper_unit_kind"
        ),
    )

    id = Column(String(32), primary_key=True, comment="单元ID(UUID hex)")
    project_id = Column(String(32), nullable=False, comment="所属项目（所有权继承）")
    paper_id = Column(String(32), nullable=False, comment="指向 paper_record")
    owner_id = Column(String(32), nullable=False, comment="所属用户（冗余便于鉴权）")
    run_id = Column(String(32), nullable=True, comment="产生该单元的解析运行/调用")
    unit_kind = Column(
        String(16), nullable=False, default="block", comment="block/abstract"
    )
    page = Column(Integer, nullable=True, comment="页码（摘要级单元必须为 NULL）")
    section = Column(String(200), nullable=False, default="", comment="章节标题（可空）")
    block_index = Column(
        Integer, nullable=False, default=0, comment="文档内顺序号（稳定槽位）"
    )
    text = Column(String(8000), nullable=False, default="", comment="原文文本块")
    char_count = Column(Integer, nullable=False, default=0, comment="字符数")
    content_hash = Column(String(64), nullable=False, default="", comment="文本哈希（幂等）")
    source_kind = Column(
        String(32), nullable=False, default="", comment="来源方式：pdf/local_kb/abstract/manual"
    )
    create_time = Column(DateTime, default=func.now(), comment="创建时间")
    update_time = Column(DateTime, default=func.now(), onupdate=func.now(), comment="更新时间")

    def __repr__(self):
        return (
            f"<PaperUnit(id='{self.id}', paper_id='{self.paper_id}', "
            f"kind='{self.unit_kind}', page={self.page}, idx={self.block_index})>"
        )


class EvidenceCardModel(Base):
    """证据卡片（任务 017 建最小归属，任务 018 阶段 A 填充可核对内容）。

    一张卡片必须同时表达：所属项目/子问题/论文、证据类型、可核对原文
    ``quote`` 与简短概括 ``summary``、来源定位（``unit_id`` +
    ``locator_json`` 快照）、提取方式与用户审核状态。

    - ``quote_verified``：1 表示 ``quote`` 在提供给模型的原文单元中逐字
      校验通过；模型自行改写的文本绝不标记为已核对（提取时直接拒绝）。
    - ``level``：``fulltext`` 或 ``abstract``（摘要级证据，无页码）。
    - ``review_status``：``pending/confirmed/edited/rejected``；已被用户
      审核（非 pending）的卡片在重跑时**不被静默覆盖**。
    - ``dedup_key``：``(project, paper, sub_question, evidence_type, quote)``
      的哈希，保证提取重试幂等。
    """

    __tablename__ = "evidence_card"
    __table_args__ = (
        Index("ix_evidence_card_project", "project_id"),
        Index("ix_evidence_card_paper", "paper_id"),
        Index("ix_evidence_card_project_subq", "project_id", "sub_question_id"),
        Index("ix_evidence_card_review", "project_id", "review_status"),
        Index(
            "ux_evidence_card_dedup",
            "dedup_key",
            unique=True,
            sqlite_where=text("dedup_key IS NOT NULL AND dedup_key <> ''"),
        ),
        CheckConstraint(
            "evidence_type IN ('', 'objective', 'method', 'dataset', 'metric', "
            "'result', 'conclusion', 'limitation')",
            name="ck_evidence_card_type",
        ),
        CheckConstraint(
            "level IN ('fulltext', 'abstract')", name="ck_evidence_card_level"
        ),
        CheckConstraint(
            "review_status IN ('pending', 'confirmed', 'edited', 'rejected')",
            name="ck_evidence_card_review",
        ),
    )

    id = Column(String(32), primary_key=True, comment="证据ID(UUID hex)")
    project_id = Column(String(32), nullable=False, comment="所属项目（所有权继承）")
    paper_id = Column(String(32), nullable=False, comment="指向 paper_record")
    owner_id = Column(String(32), nullable=False, comment="所属用户（冗余便于鉴权）")
    run_id = Column(String(32), nullable=True, comment="产生该证据的研究运行")
    content = Column(String(8000), nullable=False, default="", comment="证据文本快照（兼容旧字段）")
    locator_json = Column(
        String(2000), nullable=False, default="", comment="页码/章节/文本块定位（JSON 快照）"
    )
    version = Column(Integer, nullable=False, default=1, comment="版本号")

    # --- 任务 018 阶段 A：可定位、可核对、可审核 ---
    sub_question_id = Column(
        String(64), nullable=False, default="", comment="所属研究子问题ID"
    )
    evidence_type = Column(
        String(32),
        nullable=False,
        default="",
        comment="objective/method/dataset/metric/result/conclusion/limitation",
    )
    summary = Column(String(2000), nullable=False, default="", comment="简短概括（模型）")
    quote = Column(String(4000), nullable=False, default="", comment="可核对原文片段")
    unit_id = Column(String(32), nullable=True, comment="指向 paper_unit")
    quote_verified = Column(
        Integer, nullable=False, default=0, comment="1=原文逐字校验通过"
    )
    level = Column(
        String(16), nullable=False, default="fulltext", comment="fulltext/abstract"
    )
    source_kind = Column(
        String(32), nullable=False, default="", comment="pdf/local_kb/abstract/manual"
    )
    extraction_model = Column(String(200), nullable=False, default="", comment="提取模型")
    extraction_mode = Column(
        String(16), nullable=False, default="llm", comment="llm/manual"
    )
    review_status = Column(
        String(16), nullable=False, default="pending", comment="pending/confirmed/edited/rejected"
    )
    review_note = Column(String(1000), nullable=False, default="", comment="审核备注")
    reviewed_at = Column(DateTime, nullable=True, comment="审核时间")
    dedup_key = Column(
        String(64), nullable=False, default="", comment="幂等键哈希（重试不重复）"
    )

    create_time = Column(DateTime, default=func.now(), comment="创建时间")
    update_time = Column(DateTime, default=func.now(), onupdate=func.now(), comment="更新时间")

    def __repr__(self):
        return (
            f"<EvidenceCard(id='{self.id}', project_id='{self.project_id}', "
            f"paper_id='{self.paper_id}', type='{self.evidence_type}', "
            f"review='{self.review_status}', version={self.version})>"
        )


class ComparisonUnitModel(Base):
    """论文在某比较维度上的比较单元（任务 018 阶段 B）。

    **必须关联具体证据卡片**：``evidence_ids_json`` 保存该结论所依据的
    ``evidence_card`` id 列表，空引用或引用不属于本项目/用户的卡片一律
    被拒绝，因此比较结果永远可以反查到原文。

    ``(project_id, paper_id, dimension_id)`` 唯一：重新生成按维度幂等
    替换，不产生重复比较行。
    """

    __tablename__ = "comparison_unit"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "paper_id", "dimension_id", name="uq_comparison_unit"
        ),
        Index("ix_comparison_unit_project", "project_id"),
        Index("ix_comparison_unit_owner", "owner_id"),
    )

    id = Column(String(32), primary_key=True, comment="比较单元ID(UUID hex)")
    project_id = Column(String(32), nullable=False, comment="所属项目（所有权继承）")
    paper_id = Column(String(32), nullable=False, comment="被比较的论文")
    dimension_id = Column(
        String(64), nullable=False, comment="比较维度ID（默认与证据类型同名）"
    )
    owner_id = Column(String(32), nullable=False, comment="所属用户（冗余便于鉴权）")
    run_id = Column(String(32), nullable=True, comment="产生该比较的运行")
    content = Column(String(4000), nullable=False, default="", comment="该维度下的比较结论")
    evidence_ids_json = Column(
        String(4000), nullable=False, default="[]", comment="依据的证据卡片ID列表（JSON）"
    )
    level = Column(
        String(16), nullable=False, default="fulltext", comment="fulltext/abstract（依据证据级别）"
    )
    version = Column(Integer, nullable=False, default=1, comment="版本号")
    create_time = Column(DateTime, default=func.now(), comment="创建时间")
    update_time = Column(DateTime, default=func.now(), onupdate=func.now(), comment="更新时间")

    def __repr__(self):
        return (
            f"<ComparisonUnit(project_id='{self.project_id}', paper_id='{self.paper_id}', "
            f"dimension='{self.dimension_id}')>"
        )


class ResearchConflictModel(Base):
    """论文间/论文内的结论冲突记录（任务 018 阶段 B）。

    冲突两侧都必须引用具体证据卡片（``side_a_json`` / ``side_b_json``
    各为 ``[{"evidence_id", "paper_id", "quote"}]``），否则不构成冲突记录；
    两侧不得引用同一张卡片。``status`` 为 ``open/resolved``，人工裁决写入
    ``resolution``。
    """

    __tablename__ = "research_conflict"
    __table_args__ = (
        Index("ix_research_conflict_project", "project_id"),
        Index("ix_research_conflict_owner", "owner_id"),
        Index(
            "ux_research_conflict_dedup",
            "dedup_key",
            unique=True,
            sqlite_where=text("dedup_key IS NOT NULL AND dedup_key <> ''"),
        ),
        CheckConstraint(
            "status IN ('open', 'resolved')", name="ck_research_conflict_status"
        ),
    )

    id = Column(String(32), primary_key=True, comment="冲突记录ID(UUID hex)")
    project_id = Column(String(32), nullable=False, comment="所属项目（所有权继承）")
    owner_id = Column(String(32), nullable=False, comment="所属用户（冗余便于鉴权）")
    run_id = Column(String(32), nullable=True, comment="产生该冲突的运行")
    sub_question_id = Column(
        String(64), nullable=False, default="", comment="相关子问题（可空）"
    )
    dimension_id = Column(String(64), nullable=False, default="", comment="相关比较维度")
    topic = Column(String(500), nullable=False, default="", comment="冲突主题")
    description = Column(String(4000), nullable=False, default="", comment="冲突说明")
    side_a_json = Column(
        String(4000), nullable=False, default="[]", comment="甲方证据（JSON：evidence_id/paper_id/quote）"
    )
    side_b_json = Column(
        String(4000), nullable=False, default="[]", comment="乙方证据（JSON：evidence_id/paper_id/quote）"
    )
    status = Column(String(16), nullable=False, default="open", comment="open/resolved")
    resolution = Column(String(1000), nullable=False, default="", comment="人工裁决说明")
    dedup_key = Column(String(64), nullable=False, default="", comment="幂等键哈希")
    version = Column(Integer, nullable=False, default=1, comment="版本号")
    create_time = Column(DateTime, default=func.now(), comment="创建时间")
    update_time = Column(DateTime, default=func.now(), onupdate=func.now(), comment="更新时间")

    def __repr__(self):
        return (
            f"<ResearchConflict(id='{self.id}', project_id='{self.project_id}', "
            f"topic='{self.topic[:40]}', status='{self.status}')>"
        )


class ResearchReportModel(Base):
    """研究报告（任务 017 建最小归属，任务 018 阶段 C 填充可追溯内容）。

    版本语义：**永不覆盖**。同一逻辑报告用 ``report_key`` 关联，每次生成
    或用户编辑都插入 ``version + 1`` 的新行，旧版本保持可读。

    引用语义：正文里的 ``[[e:<evidence_id>]]`` 标记就是稳定引用（内嵌真实
    证据 id，不是显示顺序）；``citations_json`` 保存标记 → 证据/论文/定位
    的映射，因此报告里的每条事实都能反查论文与原文。无证据支持的结论只能
    出现在"证据不足/推测"小节，并被显式标注。
    """

    __tablename__ = "research_report"
    __table_args__ = (
        Index("ix_research_report_project", "project_id"),
        Index("ix_research_report_owner", "owner_id"),
        Index("ix_research_report_key_version", "report_key", "version"),
        UniqueConstraint("report_key", "version", name="uq_research_report_version"),
        CheckConstraint(
            "status IN ('generated', 'edited', 'failed')", name="ck_research_report_status"
        ),
    )

    id = Column(String(32), primary_key=True, comment="报告版本ID(UUID hex)")
    project_id = Column(String(32), nullable=False, comment="所属项目（所有权继承）")
    owner_id = Column(String(32), nullable=False, comment="所属用户（冗余便于鉴权）")
    title = Column(String(500), nullable=False, default="", comment="报告标题")
    content = Column(String(30000), nullable=False, default="", comment="报告正文（Markdown）")
    version = Column(Integer, nullable=False, default=1, comment="版本号")

    # --- 任务 018 阶段 C：版本链、引用映射与生成依据 ---
    report_key = Column(
        String(32), nullable=False, default="", comment="逻辑报告ID（同一报告的多版本共用）"
    )
    run_id = Column(String(32), nullable=True, comment="依据的研究运行")
    status = Column(
        String(16), nullable=False, default="generated", comment="generated/edited/failed"
    )
    citations_json = Column(
        String(8000),
        nullable=False,
        default="[]",
        comment="引用映射（JSON：marker/evidence_id/paper_id/title/locator）",
    )
    evidence_ids_json = Column(
        String(8000), nullable=False, default="[]", comment="生成时使用的证据集合"
    )
    unsupported = Column(
        String(4000), nullable=False, default="", comment="无证据支持而被标注的结论/原因"
    )
    generated_by = Column(String(200), nullable=False, default="", comment="生成模型")
    error = Column(String(1000), nullable=True, comment="失败原因")
    create_time = Column(DateTime, default=func.now(), comment="创建时间")
    update_time = Column(DateTime, default=func.now(), onupdate=func.now(), comment="更新时间")

    def __repr__(self):
        return (
            f"<ResearchReport(id='{self.id}', project_id='{self.project_id}', "
            f"key='{self.report_key}', version={self.version}, status='{self.status}')>"
        )


__all__ = [
    "ResearchProjectModel",
    "ResearchRunModel",
    "PaperRecordModel",
    "ProjectPaperModel",
    "PaperUnitModel",
    "EvidenceCardModel",
    "ComparisonUnitModel",
    "ResearchConflictModel",
    "ResearchReportModel",
]
