"""师生协作研究讨论与结构化研究定义（《学生研究与导师审核功能重新设计方案》里程碑2）。

对象与边界（§4.0、§4.2、§4.3）：

- **研究讨论会话不建新表**：复用既有 ``conversation`` 表，用
  ``conversation.supervision_project_id`` 绑定科研协作项目，用
  ``owner_id`` 决定归属。同一项目内学生与导师各有自己的会话，原始消息
  只有会话所有者读得到。``conversation.project_id`` 继续只指向个人论文
  研究项目（``research_project.id``），两个字段并存、分别鉴权。
- **结构化研究定义** ``RB-001`` 才是项目共享对象：草稿只属于提取人；
  确认后进入项目共享区，另一角色只能读到已确认的结构化版本，读不到
  来源会话、来源消息和未确认草稿（§4.2、§9.3）。
- **版本不覆盖**（§4.3.1）：重新提取或修改已确认内容都新增一行
  ``supervision_research_brief_version``，旧版本只读保留、可比较；
  ``supervision_research_brief.shared_version`` 记住"对外生效的那一版"，
  所以上游检索任务始终能绑定当时的依据（§4.4）。
- 会话的可读编号 ``CV-001`` 不入库：它由项目内会话的创建顺序现算，
  避免为"一个编号"再建一张表；审计与通知只写 ``CV-``/``RB-`` 编号。

条目类型、状态与种类的取值都写成 ``CHECK``，与 ``supervision_work_model``
风格一致：服务层漏判时库层仍然兜住。
"""
from sqlalchemy import (
    JSON,
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

# 可读编号前缀（§4.0 的 ID 表：私人研究会话 CV-，结构化研究定义 RB-）。
CODE_BRIEF = "RB"
CODE_DIALOGUE = "CV"

# 会话种类：研究讨论会话走既有智能聊天链路，默认与聊天主链路一致。
DIALOGUE_CHAT_TYPE = "agent_chat"

BRIEF_STATUS_VALUES = ("draft", "confirmed", "rejected", "superseded")
BRIEF_KIND_VALUES = ("research_brief", "mentor_guidance")
BRIEF_ENTRY_TYPE_VALUES = (
    "research_question",
    "hypothesis",
    "scope_constraint",
    "to_verify_judgement",
    "candidate_method",
    "evaluation_metric",
    "mixed",
)

# 结构化内容的字段顺序与中文标签（§4.3、§6.2.1 的卡片字段）。
# 仓储层的提取提示、版本比较和上下文装配都从这里取字段名，避免三处各写一套。
BRIEF_TEXT_FIELDS = (
    "background",
    "question",
    "objectives",
    "subjects",
    "scope",
    "prior_attempts",
    "hypotheses",
    "constraints",
    "methods",
    "metrics",
    "expected_outcomes",
    "to_verify",
)
BRIEF_LIST_FIELDS = ("keywords", "sub_questions")
BRIEF_CONTENT_FIELDS = BRIEF_TEXT_FIELDS + BRIEF_LIST_FIELDS

BRIEF_FIELD_LABELS = {
    "background": "研究背景与已知工作",
    "question": "核心研究问题",
    "objectives": "研究目标",
    "subjects": "研究对象与场景",
    "scope": "范围与边界",
    "prior_attempts": "已经尝试过什么",
    "hypotheses": "研究假设",
    "constraints": "约束条件",
    "methods": "候选方法",
    "metrics": "评价指标",
    "expected_outcomes": "预期成果",
    "to_verify": "待验证判断与还不确定之处",
    "keywords": "需要检索的关键词",
    "sub_questions": "需要拆分的子问题",
}


class SupervisionResearchBriefModel(Base):
    """结构化研究定义（RB-001）：草稿私有，确认后项目共享（§4.2、§4.3）。

    这一行是"研究定义"这个逻辑对象的身份与当前状态；真正对外生效的内容
    在 ``supervision_research_brief_version`` 里按版本各存一份。
    ``shared_version`` 指向已确认并共享的那一版，另一角色只读得到那一版。
    """

    __tablename__ = "supervision_research_brief"
    __table_args__ = (
        UniqueConstraint("project_id", "code", name="uq_supervision_research_brief_code"),
        Index("ix_supervision_research_brief_project_status", "project_id", "status"),
        Index("ix_supervision_research_brief_project_owner", "project_id", "owner_user_id"),
        CheckConstraint(
            "status IN ('draft', 'confirmed', 'rejected', 'superseded')",
            name="ck_supervision_research_brief_status",
        ),
        CheckConstraint(
            "kind IN ('research_brief', 'mentor_guidance')",
            name="ck_supervision_research_brief_kind",
        ),
        CheckConstraint(
            "owner_role IN ('student', 'mentor')",
            name="ck_supervision_research_brief_owner_role",
        ),
        CheckConstraint(
            "entry_type IN ('research_question', 'hypothesis', 'scope_constraint', "
            "'to_verify_judgement', 'candidate_method', 'evaluation_metric', 'mixed')",
            name="ck_supervision_research_brief_entry_type",
        ),
        CheckConstraint(
            "current_version >= 1", name="ck_supervision_research_brief_current_version"
        ),
    )

    id = Column(String(32), primary_key=True)
    # 对外可读编号 RB-001；审计、通知和检索任务引用它，不引用内部 id。
    code = Column(String(16), nullable=False)
    project_id = Column(String(32), nullable=False)
    # 提取人及其项目角色：草稿阶段的可见性只认这个字段。
    owner_user_id = Column(String(32), nullable=False)
    owner_role = Column(String(16), nullable=False, default="")
    # research_brief=学生研究定义；mentor_guidance=导师建议（§4.2 两种来源）。
    kind = Column(String(32), nullable=False, default="research_brief")
    title = Column(String(200), nullable=False, default="")
    # 条目类型（§4.3）；一份汇总的研究定义通常是 mixed。
    entry_type = Column(String(32), nullable=False, default="mixed")
    status = Column(String(16), nullable=False, default="draft")
    # 当前最新版本号；已共享的版本单独记在 shared_version。
    current_version = Column(Integer, nullable=False, default=1)
    shared_version = Column(Integer, nullable=True)
    confirmed_by = Column(String(32), nullable=False, default="")
    confirmed_at = Column(DateTime, nullable=True)
    # 来源会话：只用于本人回看来源消息，永不对另一角色暴露。
    source_conversation_id = Column(String(32), nullable=True)
    # 软删除：草稿可以丢弃；已共享的研究定义作为项目审计资产保留（§13）。
    deleted_at = Column(DateTime, nullable=True)
    delete_reason = Column(Text, nullable=False, default="")
    create_time = Column(DateTime, nullable=True)
    update_time = Column(DateTime, nullable=True)

    # --- 结构化内容（§4.3；字段名与版本表逐一对应，比较时按名对齐）---
    background = Column(Text, nullable=False, default="")
    question = Column(Text, nullable=False, default="")
    objectives = Column(Text, nullable=False, default="")
    subjects = Column(Text, nullable=False, default="")
    scope = Column(Text, nullable=False, default="")
    prior_attempts = Column(Text, nullable=False, default="")
    hypotheses = Column(Text, nullable=False, default="")
    constraints = Column(Text, nullable=False, default="")
    methods = Column(Text, nullable=False, default="")
    metrics = Column(Text, nullable=False, default="")
    expected_outcomes = Column(Text, nullable=False, default="")
    to_verify = Column(Text, nullable=False, default="")
    keywords = Column(JSON, nullable=False, default=list)
    sub_questions = Column(JSON, nullable=False, default=list)


class SupervisionResearchBriefVersionModel(Base):
    """研究定义的一个版本：一经写入只读，修改与重新提取都新增版本（§4.3.1）。

    - ``ai_content`` 保存模型整理出来的原始结构化内容，``edited_fields``
      记录本人改过哪些字段，界面上才能区分"AI整理"与"学生修订"；
    - ``source_message_ids`` 是来源消息范围，只服务于本人的"打开来源"，
      另一角色读不到（§9.3 最后一条）；
    - ``superseded_at`` 只标记"被更新版本替代"，不删行，历史审核与
      检索任务的依据始终可回溯（§4.4、§13）。
    """

    __tablename__ = "supervision_research_brief_version"
    __table_args__ = (
        UniqueConstraint("brief_id", "version", name="uq_supervision_research_brief_version"),
        Index("ix_supervision_research_brief_version_brief", "brief_id", "version"),
        Index("ix_supervision_research_brief_version_project", "project_id", "brief_id"),
        CheckConstraint(
            "status IN ('draft', 'confirmed', 'rejected', 'superseded')",
            name="ck_supervision_research_brief_version_status",
        ),
        CheckConstraint(
            "entry_type IN ('research_question', 'hypothesis', 'scope_constraint', "
            "'to_verify_judgement', 'candidate_method', 'evaluation_metric', 'mixed')",
            name="ck_supervision_research_brief_version_entry_type",
        ),
        CheckConstraint(
            "version >= 1", name="ck_supervision_research_brief_version_number"
        ),
    )

    id = Column(String(32), primary_key=True)
    brief_id = Column(String(32), nullable=False)
    project_id = Column(String(32), nullable=False)
    version = Column(Integer, nullable=False, default=1)
    # 该版本当时的状态；已共享的版本即使后来出了新版本也仍是 confirmed。
    status = Column(String(16), nullable=False, default="draft")
    entry_type = Column(String(32), nullable=False, default="mixed")
    # 模型整理内容（原样）与本人改过的字段名，用于区分 AI 与学生贡献。
    ai_content = Column(JSON, nullable=False, default=dict)
    edited_fields = Column(JSON, nullable=False, default=list)
    source_conversation_id = Column(String(32), nullable=True)
    source_message_ids = Column(JSON, nullable=False, default=list)
    created_by = Column(String(32), nullable=False)
    created_role = Column(String(16), nullable=False, default="")
    confirmed_by = Column(String(32), nullable=False, default="")
    confirmed_at = Column(DateTime, nullable=True)
    # 确认/发布时的说明，以及"影响哪些检索任务与提交"的范围说明。
    confirm_note = Column(Text, nullable=False, default="")
    superseded_at = Column(DateTime, nullable=True)
    create_time = Column(DateTime, nullable=True)

    # --- 结构化内容：与 brief 表逐字段同名，版本比较按字段名对齐 ---
    background = Column(Text, nullable=False, default="")
    question = Column(Text, nullable=False, default="")
    objectives = Column(Text, nullable=False, default="")
    subjects = Column(Text, nullable=False, default="")
    scope = Column(Text, nullable=False, default="")
    prior_attempts = Column(Text, nullable=False, default="")
    hypotheses = Column(Text, nullable=False, default="")
    constraints = Column(Text, nullable=False, default="")
    methods = Column(Text, nullable=False, default="")
    metrics = Column(Text, nullable=False, default="")
    expected_outcomes = Column(Text, nullable=False, default="")
    to_verify = Column(Text, nullable=False, default="")
    keywords = Column(JSON, nullable=False, default=list)
    sub_questions = Column(JSON, nullable=False, default=list)


__all__ = [
    "BRIEF_CONTENT_FIELDS",
    "BRIEF_ENTRY_TYPE_VALUES",
    "BRIEF_FIELD_LABELS",
    "BRIEF_KIND_VALUES",
    "BRIEF_LIST_FIELDS",
    "BRIEF_STATUS_VALUES",
    "BRIEF_TEXT_FIELDS",
    "CODE_BRIEF",
    "CODE_DIALOGUE",
    "DIALOGUE_CHAT_TYPE",
    "SupervisionResearchBriefModel",
    "SupervisionResearchBriefVersionModel",
]
