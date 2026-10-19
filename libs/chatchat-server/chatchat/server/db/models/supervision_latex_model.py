"""LaTeX 稿件静态分析的持久化对象（《学生研究与导师审核功能重新设计方案》里程碑5 路由层）。

三张表，各自只存自己那一层的状态：

- ``supervision_latex_snapshot``：一次**分析快照**。绑定某个附件版本（code +
  sha256 + version），存结构大纲、计数、静态检查发现、主文件判定与实际生效的
  安全/解析上限。快照只追加：重新分析是**新行**，旧结论不被覆盖（升级解析器
  版本后也不得静默改掉历史结论）。
- ``supervision_latex_progress_draft``：两个版本差异生成的**进度草稿**。
  ``status`` 只有 ``draft/baseline/confirmed/discarded``；``published`` 恒为
  ``False``、``requires_student_confirmation`` 恒为 ``True``（库层 CHECK 兜底），
  因为草稿本身永不发布——学生确认后写出去的是 ``supervision_progress_update``
  里另一条独立进展行，草稿行只被标记为已确认。
- ``supervision_latex_draft_confirmation``：草稿 → 正式进展的**确认记录**。
  一个草稿最多一条（``draft_id`` 唯一约束），重复确认由库层挡住。

钉死的口径（§6.6、§16.6）：

1. ``compiled`` 恒为 ``False``：系统从不编译 LaTeX，快照里没有任何"编译通过"
   字段，只有静态证据（E1 文档证据）；
2. 快照不存临时解压目录路径、附件磁盘路径或稿件原文，只存归档内相对路径、
   哈希与结构；
3. 超预算的文件以 ``partially_read`` / ``not_read`` 原样记录，不静默截断；
4. 首次上传（无上一版本）只落 ``status="baseline"`` 的空条目草稿，不推断
   "本周期完成了什么"。
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
)

from chatchat.server.db.base import Base

# 可读编号前缀（§4.0 的 ID 表）。快照 ``LX-001``、进度草稿 ``PD-001``。
CODE_LATEX_SNAPSHOT = "LX"
CODE_PROGRESS_DRAFT = "PD"

#: 主文件判定，与解析库 ``MainFileResolution.status`` 同一套取值。
MAIN_STATUS_VALUES = (
    "resolved",
    "user_specified",
    "ambiguous",
    "not_found",
    "invalid_selection",
)
#: 草稿状态机：``draft/baseline → confirmed``，或 ``draft/baseline → discarded``。
DRAFT_STATUS_VALUES = ("draft", "baseline", "confirmed", "discarded")
#: 可选模型摘要的状态。``unavailable`` = 模型能力未接入（不是错误）。
MODEL_SUMMARY_STATUS_VALUES = ("", "generated", "unavailable", "failed")


class SupervisionLatexSnapshotModel(Base):
    """一次 LaTeX 稿件静态分析快照（附件版本 + 解析器版本 + 结论）。"""

    __tablename__ = "supervision_latex_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "code", name="uq_supervision_latex_snapshot_code"
        ),
        Index(
            "ix_supervision_latex_snapshot_project_time",
            "project_id",
            "create_time",
        ),
        Index(
            "ix_supervision_latex_snapshot_attachment",
            "attachment_id",
            "create_time",
        ),
        CheckConstraint(
            "main_status IN ('resolved', 'user_specified', 'ambiguous', "
            "'not_found', 'invalid_selection')",
            name="ck_supervision_latex_snapshot_main_status",
        ),
        # 恒 False：系统绝不调用 LaTeX 编译器，库层直接钉住。
        CheckConstraint("compiled = 0", name="ck_supervision_latex_snapshot_compiled"),
    )

    id = Column(String(32), primary_key=True)
    code = Column(String(16), nullable=False)
    project_id = Column(String(32), nullable=False)

    # --- 绑定的附件版本（快照对"哪一份稿件"负责）---------------------------
    attachment_id = Column(String(32), nullable=False)
    attachment_code = Column(String(16), nullable=False, default="")
    attachment_name = Column(String(300), nullable=False, default="")
    attachment_sha256 = Column(String(64), nullable=False, default="")
    attachment_version = Column(Integer, nullable=False, default=1)

    # --- 主文件判定（不确定时必须把候选交给用户，不替用户猜）--------------
    main_file = Column(String(500), nullable=False, default="")  # 用户指定，空=自动
    main_status = Column(String(32), nullable=False, default="not_found")
    main_path = Column(String(500), nullable=False, default="")
    # 解析库 MainFileResolution.as_dict()：含 status/reason/candidates。
    main_json = Column(JSON, nullable=False, default=dict)

    # --- 版本可追溯（升级解析器不得静默改变旧结论）------------------------
    parser_version = Column(String(32), nullable=False, default="")
    checker_version = Column(String(32), nullable=False, default="")
    zip_safety_version = Column(String(32), nullable=False, default="")
    # 实际生效的安全上限与解析预算（快照里存的是生效值，不是默认常量）。
    limits_json = Column(JSON, nullable=False, default=dict)
    parse_budget_json = Column(JSON, nullable=False, default=dict)

    # --- 结构与检查结论 ---------------------------------------------------
    document_present = Column(Boolean, nullable=False, default=False)
    # 解析库 LatexDocument.as_dict()（不含稿件原文，只有结构与 read_states）。
    document_json = Column(JSON, nullable=True)
    # 解析库 LatexCheckReport.as_dict()：ok/compiled/findings/counts/checked_files。
    check_json = Column(JSON, nullable=True)
    outline = Column(JSON, nullable=False, default=list)
    counts = Column(JSON, nullable=False, default=dict)
    findings = Column(JSON, nullable=False, default=list)
    finding_counts = Column(JSON, nullable=False, default=dict)
    # 危险命令命中单独留一份：页面必须原样呈现，不能被"没问题"盖过去。
    dangerous_findings = Column(JSON, nullable=False, default=list)
    # 超预算如实透出：部分读取 / 完全没读的文件清单（含原因）。
    partially_read = Column(JSON, nullable=False, default=list)
    not_read = Column(JSON, nullable=False, default=list)
    unreferenced_tex = Column(JSON, nullable=False, default=list)
    available_files = Column(JSON, nullable=False, default=list)
    # 安全解压落盘的清单（entry/extension/size/sha256），不含磁盘路径。
    extracted_files = Column(JSON, nullable=False, default=list)

    file_count = Column(Integer, nullable=False, default=0)
    total_bytes = Column(Integer, nullable=False, default=0)
    word_count = Column(Integer, nullable=False, default=0)
    compiled = Column(Boolean, nullable=False, default=False)

    created_by = Column(String(32), nullable=False)
    created_role = Column(String(16), nullable=False, default="")
    create_time = Column(DateTime, nullable=True)


class SupervisionLatexProgressDraftModel(Base):
    """两个稿件版本的静态差异 → 进度草稿（必须学生确认，§6.6 第9条）。"""

    __tablename__ = "supervision_latex_progress_draft"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "code", name="uq_supervision_latex_draft_code"
        ),
        Index(
            "ix_supervision_latex_draft_project_time", "project_id", "create_time"
        ),
        Index("ix_supervision_latex_draft_project_status", "project_id", "status"),
        Index("ix_supervision_latex_draft_attachment", "attachment_id"),
        CheckConstraint(
            "status IN ('draft', 'baseline', 'confirmed', 'discarded')",
            name="ck_supervision_latex_draft_status",
        ),
        # 草稿本身永不发布：确认动作写的是独立进展行。
        CheckConstraint("published = 0", name="ck_supervision_latex_draft_published"),
        CheckConstraint(
            "requires_student_confirmation = 1",
            name="ck_supervision_latex_draft_confirm",
        ),
        CheckConstraint(
            "model_summary_status IN ('', 'generated', 'unavailable', 'failed')",
            name="ck_supervision_latex_draft_model_summary",
        ),
    )

    id = Column(String(32), primary_key=True)
    code = Column(String(16), nullable=False)
    project_id = Column(String(32), nullable=False)
    # 解析库自己生成的草稿标识（``PD-<digest>``），与本平台可读编号分开存。
    library_draft_id = Column(String(80), nullable=False, default="")
    draft_version = Column(String(32), nullable=False, default="")
    parser_version = Column(String(32), nullable=False, default="")
    checker_version = Column(String(32), nullable=False, default="")
    zip_safety_version = Column(String(32), nullable=False, default="")

    status = Column(String(16), nullable=False, default="draft")
    published = Column(Boolean, nullable=False, default=False)
    requires_student_confirmation = Column(Boolean, nullable=False, default=True)
    baseline_only = Column(Boolean, nullable=False, default=False)

    # --- 两侧版本（快照 code + 附件 code/sha256）--------------------------
    snapshot_id = Column(String(32), nullable=False)
    snapshot_code = Column(String(16), nullable=False, default="")
    attachment_id = Column(String(32), nullable=False)
    attachment_code = Column(String(16), nullable=False, default="")
    attachment_sha256 = Column(String(64), nullable=False, default="")
    attachment_version = Column(Integer, nullable=False, default=1)
    main_file = Column(String(500), nullable=False, default="")
    # 首次上传时 base_* 全为空：只有基线，没有"进展"。
    base_snapshot_id = Column(String(32), nullable=True)
    base_snapshot_code = Column(String(16), nullable=False, default="")
    base_attachment_id = Column(String(32), nullable=True)
    base_attachment_code = Column(String(16), nullable=False, default="")
    base_attachment_sha256 = Column(String(64), nullable=False, default="")
    base_attachment_version = Column(Integer, nullable=False, default=0)
    base_main_file = Column(String(500), nullable=False, default="")

    # --- 草稿内容（解析库 ProgressDraft 的忠实落库）-----------------------
    summary = Column(Text, nullable=False, default="")
    caveat = Column(Text, nullable=False, default="")
    items = Column(JSON, nullable=False, default=list)
    item_count = Column(Integer, nullable=False, default=0)
    truncated = Column(Boolean, nullable=False, default=False)
    dropped_items = Column(Integer, nullable=False, default=0)
    # 解析库 ProgressDraft.as_dict() 全量，供审计与页面回放。
    draft_json = Column(JSON, nullable=False, default=dict)
    # 差异统计（章节增删改、资源变化、字数变化）。
    diff_counts = Column(JSON, nullable=False, default=dict)
    word_count_old = Column(Integer, nullable=False, default=0)
    word_count_new = Column(Integer, nullable=False, default=0)
    word_count_delta = Column(Integer, nullable=False, default=0)

    # --- 可选模型摘要（纯静态草稿不依赖模型，模型缺席也照常生成）----------
    model_summary = Column(Text, nullable=False, default="")
    model_summary_status = Column(String(32), nullable=False, default="")
    model_summary_note = Column(Text, nullable=False, default="")
    model_name = Column(String(120), nullable=False, default="")

    # --- 确认（转成正式进展）与丢弃 --------------------------------------
    confirmed_progress_id = Column(String(32), nullable=True)
    confirmed_progress_code = Column(String(16), nullable=False, default="")
    confirmed_by = Column(String(32), nullable=False, default="")
    confirmed_role = Column(String(16), nullable=False, default="")
    confirmed_at = Column(DateTime, nullable=True)
    discarded_by = Column(String(32), nullable=False, default="")
    discarded_role = Column(String(16), nullable=False, default="")
    discarded_at = Column(DateTime, nullable=True)
    discard_reason = Column(Text, nullable=False, default="")

    created_by = Column(String(32), nullable=False)
    created_role = Column(String(16), nullable=False, default="")
    create_time = Column(DateTime, nullable=True)
    update_time = Column(DateTime, nullable=True)


class SupervisionLatexDraftConfirmationModel(Base):
    """草稿确认记录：谁在什么时候把哪份草稿转成了哪条正式进展。

    只追加。``draft_id`` 唯一 → 一个草稿只能确认一次；重复确认由库层兜底
    （业务层先查一次给出中文提示，并发下这里保证不会生成两条进展）。
    """

    __tablename__ = "supervision_latex_draft_confirmation"
    __table_args__ = (
        UniqueConstraint(
            "draft_id", name="uq_supervision_latex_draft_confirmation"
        ),
        Index(
            "ix_supervision_latex_draft_confirmation_project",
            "project_id",
            "created_at",
        ),
        Index("ix_supervision_latex_draft_confirmation_progress", "progress_id"),
    )

    id = Column(String(32), primary_key=True)
    draft_id = Column(String(32), nullable=False)
    draft_code = Column(String(16), nullable=False, default="")
    project_id = Column(String(32), nullable=False)
    progress_id = Column(String(32), nullable=False)
    progress_code = Column(String(16), nullable=False, default="")
    # 确认时草稿处于 draft 还是 baseline（基线确认同样生成进展）。
    draft_status = Column(String(16), nullable=False, default="")
    actor_id = Column(String(32), nullable=False)
    actor_role = Column(String(16), nullable=False, default="")
    # 学生确认时填写/编辑的正文（进展行里是拼装后的结果，这里存原始输入）。
    statement = Column(Text, nullable=False, default="")
    item_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=True)


__all__ = [
    "CODE_LATEX_SNAPSHOT",
    "CODE_PROGRESS_DRAFT",
    "DRAFT_STATUS_VALUES",
    "MAIN_STATUS_VALUES",
    "MODEL_SUMMARY_STATUS_VALUES",
    "SupervisionLatexDraftConfirmationModel",
    "SupervisionLatexProgressDraftModel",
    "SupervisionLatexSnapshotModel",
]
