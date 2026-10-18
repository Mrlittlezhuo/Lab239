"""师生协作业务对象模型（《学生研究与导师审核功能重新设计方案》里程碑1）。

四类对象**分别保存状态**（§5）：任务、正式提交、AI预审、导师审核。页面上
的"待导师审核""退回修改"等文案由它们组合派生，任何一个字段的变化都不得
暗中推动另外两个字段（§5.6、§5.7）。

不可变性（§13）：

- 正式提交版本一经创建只读；学生改内容必须生成新版本，不能覆盖导师已经
  看过的版本；
- 导师正式审核不可静默覆盖，只能追加补充意见（``supervision_review_note``）；
- 验收标准在学生提交时快照进 ``ac_snapshot``，导师事后修改标准不影响已提交
  版本，审核始终绑定当时那份标准；
- 进展记录允许追加补充说明，原始内容不改写。

附件（§16.1 第3条）：正式项目附件带项目权限、版本、内容哈希与下载鉴权，
文件本体落在 ``DATA_PATH/supervision_attachments`` 下，库里只存相对路径与
sha256，不存可被外部直接访问的 URL。
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

# 可读编号前缀（§4.0 的 ID 表）。
CODE_MILESTONE = "M"
CODE_TASK = "T"
CODE_PROGRESS = "PR"
CODE_SUBMISSION = "S"
CODE_REVIEW = "MR"
CODE_ATTACHMENT = "AT"

TASK_STATUS_VALUES = ("draft", "active", "completed", "cancelled", "archived")
SUBMISSION_STATUS_VALUES = ("submitted", "withdrawn", "superseded")
REVIEW_STATUS_VALUES = ("not_started", "drafting", "published")
REVIEW_DECISION_VALUES = ("approved", "revision_required", "rejected", "paused")
AC_CONCLUSION_VALUES = (
    "met",
    "partially_met",
    "not_met",
    "more_evidence_required",
    "not_applicable",
)
SELF_ASSESSMENT_VALUES = ("done", "partially_done", "not_done", "not_applicable")
MILESTONE_STATUS_VALUES = ("planning", "active", "completed", "cancelled")
MILESTONE_CONFIRM_VALUES = ("", "confirmed", "partially_confirmed", "not_confirmed")


class SupervisionMilestoneModel(Base):
    """阶段目标：这一阶段最终要证明或交付什么（§4.6）。"""

    __tablename__ = "supervision_milestone"
    __table_args__ = (
        UniqueConstraint("project_id", "code", name="uq_supervision_milestone_code"),
        Index("ix_supervision_milestone_project", "project_id", "status"),
        CheckConstraint(
            "status IN ('planning', 'active', 'completed', 'cancelled')",
            name="ck_supervision_milestone_status",
        ),
        CheckConstraint(
            "mentor_confirmation IN ('', 'confirmed', 'partially_confirmed', "
            "'not_confirmed')",
            name="ck_supervision_milestone_confirmation",
        ),
    )

    id = Column(String(32), primary_key=True)
    code = Column(String(16), nullable=False)
    project_id = Column(String(32), nullable=False)
    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=False, default="")
    # 成功判据：阶段目标不能只写标题（§4.6）。
    success_criteria = Column(Text, nullable=False, default="")
    planned_start = Column(DateTime, nullable=True)
    planned_end = Column(DateTime, nullable=True)
    status = Column(String(16), nullable=False, default="planning")
    owner_user_id = Column(String(32), nullable=False)
    # 导师最终确认结果与确认说明；空串表示还没确认过。
    mentor_confirmation = Column(String(32), nullable=False, default="")
    mentor_confirmation_note = Column(Text, nullable=False, default="")
    mentor_confirmed_by = Column(String(32), nullable=False, default="")
    mentor_confirmed_at = Column(DateTime, nullable=True)
    create_time = Column(DateTime, nullable=True)
    update_time = Column(DateTime, nullable=True)


class SupervisionTaskModel(Base):
    """研究任务：师生日常协作的基本单位（§4.7）。"""

    __tablename__ = "supervision_task"
    __table_args__ = (
        UniqueConstraint("project_id", "code", name="uq_supervision_task_code"),
        Index("ix_supervision_task_project_status", "project_id", "status"),
        Index("ix_supervision_task_due", "status", "due_at"),
        CheckConstraint(
            "status IN ('draft', 'active', 'completed', 'cancelled', 'archived')",
            name="ck_supervision_task_status",
        ),
        CheckConstraint(
            "priority IN ('low', 'normal', 'high')", name="ck_supervision_task_priority"
        ),
    )

    id = Column(String(32), primary_key=True)
    code = Column(String(16), nullable=False)
    project_id = Column(String(32), nullable=False)
    milestone_id = Column(String(32), nullable=True)
    title = Column(String(200), nullable=False)
    # 为什么要做 / 需要交付什么 / 怎么判定完成（§7.2 的三问）
    purpose = Column(Text, nullable=False, default="")
    description = Column(Text, nullable=False, default="")
    # 要求提交的材料：[{key,label,required}]
    required_materials = Column(JSON, nullable=False, default=list)
    due_at = Column(DateTime, nullable=True)
    priority = Column(String(16), nullable=False, default="normal")
    requires_code_review = Column(Boolean, nullable=False, default=False)
    requires_runtime = Column(Boolean, nullable=False, default=False)
    status = Column(String(16), nullable=False, default="draft")
    # 已发布任务被修改时递增版本号并保留旧版验收标准（§7.2）。
    version = Column(Integer, nullable=False, default=1)
    created_by = Column(String(32), nullable=False)
    created_role = Column(String(16), nullable=False, default="")
    published_at = Column(DateTime, nullable=True)
    cancelled_at = Column(DateTime, nullable=True)
    cancel_reason = Column(Text, nullable=False, default="")
    completed_at = Column(DateTime, nullable=True)
    create_time = Column(DateTime, nullable=True)
    update_time = Column(DateTime, nullable=True)


class SupervisionTaskAcModel(Base):
    """结构化验收标准 AC-n（§4.7、AI预审方案 §5.2）。"""

    __tablename__ = "supervision_task_ac"
    __table_args__ = (
        UniqueConstraint("task_id", "version", "ac_code", name="uq_supervision_ac_code"),
        Index("ix_supervision_ac_task", "task_id", "version"),
        CheckConstraint(
            "check_type IN ('code', 'experiment', 'document', 'analysis', 'runtime')",
            name="ck_supervision_ac_check_type",
        ),
    )

    id = Column(String(32), primary_key=True)
    task_id = Column(String(32), nullable=False)
    # 验收标准自身的版本号；提交快照按版本绑定（§13）。
    version = Column(Integer, nullable=False, default=1)
    ac_code = Column(String(16), nullable=False)
    statement = Column(Text, nullable=False)
    evidence_requirement = Column(Text, nullable=False, default="")
    required = Column(Boolean, nullable=False, default=True)
    check_type = Column(String(16), nullable=False, default="document")
    order_index = Column(Integer, nullable=False, default=0)
    create_time = Column(DateTime, nullable=True)


class SupervisionProgressUpdateModel(Base):
    """进展记录：连续记录研究过程，不触发正式审核（§4.8）。"""

    __tablename__ = "supervision_progress_update"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "code", name="uq_supervision_progress_code"
        ),
        Index("ix_supervision_progress_project_time", "project_id", "create_time"),
        Index("ix_supervision_progress_task", "task_id"),
    )

    id = Column(String(32), primary_key=True)
    code = Column(String(16), nullable=False)
    project_id = Column(String(32), nullable=False)
    task_id = Column(String(32), nullable=True)
    milestone_id = Column(String(32), nullable=True)
    done = Column(Text, nullable=False, default="")
    results = Column(Text, nullable=False, default="")
    materials = Column(Text, nullable=False, default="")
    problems = Column(Text, nullable=False, default="")
    next_steps = Column(Text, nullable=False, default="")
    # 可选附件或外部链接：[{attachment_code,url,label}]
    links = Column(JSON, nullable=False, default=list)
    author_id = Column(String(32), nullable=False)
    author_role = Column(String(16), nullable=False, default="")
    create_time = Column(DateTime, nullable=True)


class SupervisionProgressNoteModel(Base):
    """进展的补充说明：只追加，不改写原始内容（§4.8）。"""

    __tablename__ = "supervision_progress_note"
    __table_args__ = (Index("ix_supervision_progress_note_progress", "progress_id"),)

    id = Column(String(32), primary_key=True)
    progress_id = Column(String(32), nullable=False)
    project_id = Column(String(32), nullable=False)
    note = Column(Text, nullable=False)
    author_id = Column(String(32), nullable=False)
    author_role = Column(String(16), nullable=False, default="")
    create_time = Column(DateTime, nullable=True)


class SupervisionSubmissionModel(Base):
    """学生正式提交：不可变版本快照，导师审核的唯一正式输入（§4.9、§5.2）。"""

    __tablename__ = "supervision_submission"
    __table_args__ = (
        UniqueConstraint("code", "version", name="uq_supervision_submission_version"),
        Index("ix_supervision_submission_task", "task_id", "version"),
        Index("ix_supervision_submission_project_status", "project_id", "status"),
        CheckConstraint(
            "status IN ('submitted', 'withdrawn', 'superseded')",
            name="ck_supervision_submission_status",
        ),
    )

    id = Column(String(32), primary_key=True)
    # 同一任务的各版本共用 code，version 递增；对外显示 S-001-v2。
    code = Column(String(16), nullable=False)
    version = Column(Integer, nullable=False, default=1)
    project_id = Column(String(32), nullable=False)
    task_id = Column(String(32), nullable=False)
    status = Column(String(16), nullable=False, default="submitted")
    summary = Column(Text, nullable=False, default="")
    # Git 只是声明与审计字段，首版不读远程仓库（AI预审方案 §5.3）。
    git_repo = Column(String(500), nullable=False, default="")
    git_branch = Column(String(200), nullable=False, default="")
    git_commit = Column(String(200), nullable=False, default="")
    experiment_notes = Column(Text, nullable=False, default="")
    known_issues = Column(Text, nullable=False, default="")
    unfinished_items = Column(Text, nullable=False, default="")
    # 学生逐条自评：[{ac_code, self_assessment, evidence, note}]
    self_assessment = Column(JSON, nullable=False, default=list)
    # 提交时的验收标准快照：[{ac_code, statement, evidence_requirement,
    # required, check_type, version}]
    ac_snapshot = Column(JSON, nullable=False, default=list)
    # 材料清单：[{attachment_code,name,sha256,size,kind}]
    materials = Column(JSON, nullable=False, default=list)
    # 提交前完整性检查结果（提醒用，不替代导师审核，§6.4）
    completeness_check = Column(JSON, nullable=False, default=list)
    author_id = Column(String(32), nullable=False)
    author_role = Column(String(16), nullable=False, default="")
    create_time = Column(DateTime, nullable=True)
    withdrawn_at = Column(DateTime, nullable=True)
    withdraw_reason = Column(Text, nullable=False, default="")
    superseded_at = Column(DateTime, nullable=True)
    superseded_by = Column(String(32), nullable=False, default="")


class SupervisionReviewModel(Base):
    """导师审核：绑定某一次提交版本，发布后不可静默覆盖（§4.11、§5.4）。"""

    __tablename__ = "supervision_review"
    __table_args__ = (
        UniqueConstraint("project_id", "code", name="uq_supervision_review_code"),
        UniqueConstraint("submission_id", name="uq_supervision_review_submission"),
        Index("ix_supervision_review_project_status", "project_id", "status"),
        CheckConstraint(
            "status IN ('not_started', 'drafting', 'published')",
            name="ck_supervision_review_status",
        ),
        CheckConstraint(
            "overall_decision IN ('', 'approved', 'revision_required', 'rejected', "
            "'paused')",
            name="ck_supervision_review_decision",
        ),
    )

    id = Column(String(32), primary_key=True)
    code = Column(String(16), nullable=False)
    project_id = Column(String(32), nullable=False)
    task_id = Column(String(32), nullable=False)
    submission_id = Column(String(32), nullable=False)
    status = Column(String(16), nullable=False, default="drafting")
    overall_decision = Column(String(32), nullable=False, default="")
    decision_reason = Column(Text, nullable=False, default="")
    feedback = Column(Text, nullable=False, default="")
    # 必须修改项 / 建议改进项 / 下一步安排
    must_fix = Column(JSON, nullable=False, default=list)
    suggestions = Column(JSON, nullable=False, default=list)
    next_steps = Column(Text, nullable=False, default="")
    reviewer_id = Column(String(32), nullable=False)
    reviewer_role = Column(String(16), nullable=False, default="")
    # 首次处理时间：导师第一次保存实际审核动作的时间，打开页面不算（§5.4）。
    first_touched_at = Column(DateTime, nullable=True)
    published_at = Column(DateTime, nullable=True)
    create_time = Column(DateTime, nullable=True)
    update_time = Column(DateTime, nullable=True)


class SupervisionReviewAcModel(Base):
    """导师逐条验收结论（§5.5），与 AI 的认识状态不是同一枚举。"""

    __tablename__ = "supervision_review_ac"
    __table_args__ = (
        UniqueConstraint("review_id", "ac_code", name="uq_supervision_review_ac"),
        CheckConstraint(
            "conclusion IN ('met', 'partially_met', 'not_met', "
            "'more_evidence_required', 'not_applicable')",
            name="ck_supervision_review_ac_conclusion",
        ),
    )

    id = Column(String(32), primary_key=True)
    review_id = Column(String(32), nullable=False)
    project_id = Column(String(32), nullable=False)
    ac_code = Column(String(16), nullable=False)
    conclusion = Column(String(32), nullable=False)
    reason = Column(Text, nullable=False, default="")
    # 导师是否采纳 AI 建议：adopted / different / none（AI 未给出建议时）
    ai_relation = Column(String(16), nullable=False, default="none")
    updated_at = Column(DateTime, nullable=True)


class SupervisionReviewNoteModel(Base):
    """审核发布后的补充意见与修订：只追加（§4.11、§13）。"""

    __tablename__ = "supervision_review_note"
    __table_args__ = (Index("ix_supervision_review_note_review", "review_id"),)

    id = Column(String(32), primary_key=True)
    review_id = Column(String(32), nullable=False)
    project_id = Column(String(32), nullable=False)
    kind = Column(String(16), nullable=False, default="amendment")
    note = Column(Text, nullable=False)
    author_id = Column(String(32), nullable=False)
    author_role = Column(String(16), nullable=False, default="")
    create_time = Column(DateTime, nullable=True)


class SupervisionAttachmentModel(Base):
    """正式项目附件：项目权限 + 版本 + 内容哈希 + 下载鉴权（§16.1 第3条）。"""

    __tablename__ = "supervision_attachment"
    __table_args__ = (
        UniqueConstraint("project_id", "code", name="uq_supervision_attachment_code"),
        Index("ix_supervision_attachment_project", "project_id", "status"),
        Index("ix_supervision_attachment_task", "task_id"),
        CheckConstraint(
            "status IN ('active', 'replaced', 'removed')",
            name="ck_supervision_attachment_status",
        ),
    )

    id = Column(String(32), primary_key=True)
    code = Column(String(16), nullable=False)
    project_id = Column(String(32), nullable=False)
    task_id = Column(String(32), nullable=True)
    submission_id = Column(String(32), nullable=True)
    progress_id = Column(String(32), nullable=True)
    name = Column(String(300), nullable=False)
    ext = Column(String(24), nullable=False, default="")
    mime_type = Column(String(120), nullable=False, default="")
    size = Column(Integer, nullable=False, default=0)
    sha256 = Column(String(64), nullable=False)
    # 相对 DATA_PATH/supervision_attachments 的存储路径，不存可直连的 URL。
    stored_path = Column(String(500), nullable=False)
    version = Column(Integer, nullable=False, default=1)
    kind = Column(String(32), nullable=False, default="other")
    status = Column(String(16), nullable=False, default="active")
    uploaded_by = Column(String(32), nullable=False)
    uploaded_role = Column(String(16), nullable=False, default="")
    create_time = Column(DateTime, nullable=True)
    removed_at = Column(DateTime, nullable=True)
    remove_reason = Column(Text, nullable=False, default="")


__all__ = [
    "AC_CONCLUSION_VALUES",
    "CODE_ATTACHMENT",
    "CODE_MILESTONE",
    "CODE_PROGRESS",
    "CODE_REVIEW",
    "CODE_SUBMISSION",
    "CODE_TASK",
    "MILESTONE_CONFIRM_VALUES",
    "MILESTONE_STATUS_VALUES",
    "REVIEW_DECISION_VALUES",
    "REVIEW_STATUS_VALUES",
    "SELF_ASSESSMENT_VALUES",
    "SUBMISSION_STATUS_VALUES",
    "SupervisionAttachmentModel",
    "SupervisionMilestoneModel",
    "SupervisionProgressNoteModel",
    "SupervisionProgressUpdateModel",
    "SupervisionReviewAcModel",
    "SupervisionReviewModel",
    "SupervisionReviewNoteModel",
    "SupervisionSubmissionModel",
    "SupervisionTaskAcModel",
    "SupervisionTaskModel",
    "TASK_STATUS_VALUES",
]
