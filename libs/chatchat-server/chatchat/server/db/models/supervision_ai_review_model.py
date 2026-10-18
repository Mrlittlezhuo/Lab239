"""AI 静态预审数据模型（《AI预审功能详细设计方案》里程碑4）。

三张表，**只存 AI 自己的认识状态**，绝不与导师结论混写：

- ``supervision_ai_review_run``：一次可追踪的预审运行（§5.4）。状态机
  ``queued → running → completed/failed/cancelled``（总体方案 §5.3）是独立
  状态域，不写入任务生命周期、正式提交状态或导师审核状态（§11）。
- ``supervision_ai_review_finding``：逐条验收标准的 AI 判断
  （``satisfied/partial/not_satisfied/unverifiable``）+ 证据深度 E0–E4 +
  独立的「支持关系」轴 + 证据定位 + 缺失材料（§8.3、§9、§10.1）。
- ``supervision_ai_review_setting``：自动预审开关，项目默认 + 任务级，
  **默认关闭**（§16.5 第2条）。刻意单独建表，不去改已经发布的
  ``supervision_task``/``supervision_project`` 结构。

不可变与版本（§5.3、§11、§13.2）：

- 每次运行绑定一份**不可变检查快照** ``check_snapshot``（任务目标、当版验收
  标准、提交说明/草稿说明、逐项自评、材料清单与哈希、Git 标识、创建时间与
  创建人）。运行期间草稿继续变化不会改动这份快照。
- 重新预审生成**新的 run 行**（新的 ``code`` 与更大的 ``revision``），旧结果
  永不覆盖或删除（§13.2、§17）。
- 「基于旧快照」不是运行状态，而是时效标记：由 ``draft_fingerprint``、
  ``ac_version`` 与提交版本现算（见仓储层 ``run_staleness``），历史行不被改写。

AI 结论与导师结论字段分离（总体方案 §8.1）：本模块没有任何字段写入
``supervision_review_ac``；导师主动采纳时才由 ``adopt_ai_suggestion`` 写那条
已有的导师结论行，并记 ``ai_relation=adopted``。
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

# 可读编号前缀（§4.0 的 ID 表）。运行编号形如 ``AR-r1``。
CODE_AI_REVIEW = "AR"

#: 运行状态机（总体方案 §5.3）。
RUN_STATUS_VALUES = ("queued", "running", "completed", "failed", "cancelled")
#: 触发方式（§4 权限表、§5.4）。
TRIGGER_SOURCE_VALUES = ("student_manual", "mentor_manual", "auto_on_submission")
#: 逐条判断（§9）。AI 的 ``unverifiable`` 是认识状态，不是导师结论。
VERDICT_VALUES = ("satisfied", "partial", "not_satisfied", "unverifiable")
#: 证据强度（§8.3）。E4 受控复现本期不提供。
EVIDENCE_DEPTH_VALUES = ("E0", "E1", "E2", "E3", "E4")
#: 独立的「支持关系」轴：与判断状态正交，回答"这份材料对该验收项是什么作用"。
SUPPORT_RELATION_VALUES = (
    "no_evidence",        # 没有可读材料涉及该项
    "supports",           # 材料支持该标准
    "partially_supports", # 只覆盖部分条件
    "contradicts",        # 明确反证
    "conflicting",        # 材料之间互相冲突（§8.4）
    "self_report_only",   # 只有学生自述（E0）
)
#: 整体摘要枚举（§10.2）。由逐条结果确定，不允许模型自由发挥相反结论。
SUMMARY_STATE_VALUES = ("证据较完整", "存在缺失项", "存在明确不满足项", "预审失败")
#: 材料读取状态（§5.5.2）。只有三种，不得静默截断。
MATERIAL_READ_STATE_VALUES = ("fully_read", "partially_read", "not_read")
#: 自动预审开关取值。项目行用 ``off``/``on``；任务行可 ``inherit``。
AUTO_REVIEW_MODE_VALUES = ("off", "on", "inherit")


class SupervisionAIReviewRunModel(Base):
    """一次 AI 静态预审运行（§5.4）。"""

    __tablename__ = "supervision_ai_review_run"
    __table_args__ = (
        UniqueConstraint("code", name="uq_supervision_ai_review_code"),
        # 去重兜底（§13.1）：同一提交版本 + 同一触发来源 + 同一修订号只有一行。
        UniqueConstraint(
            "submission_id", "trigger_source", "revision",
            name="uq_supervision_ai_review_lineage",
        ),
        Index("ix_supervision_ai_review_project_status", "project_id", "status"),
        Index("ix_supervision_ai_review_task", "task_id", "revision"),
        Index("ix_supervision_ai_review_submission", "submission_id"),
        Index("ix_supervision_ai_review_queue", "status", "create_time"),
        CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed', 'cancelled')",
            name="ck_supervision_ai_review_status",
        ),
        CheckConstraint(
            "trigger_source IN ('student_manual', 'mentor_manual', "
            "'auto_on_submission')",
            name="ck_supervision_ai_review_trigger",
        ),
        CheckConstraint(
            "summary_state IN ('', '证据较完整', '存在缺失项', "
            "'存在明确不满足项', '预审失败')",
            name="ck_supervision_ai_review_summary",
        ),
    )

    id = Column(String(32), primary_key=True)
    # 对外可读编号 ``AR-r1``；重新预审是新编号 + 更大的 revision，旧行保留。
    code = Column(String(16), nullable=False)
    # 同一（提交版本 或 草稿指纹）+ 同一触发来源内的版本号，从 1 递增。
    revision = Column(Integer, nullable=False, default=1)

    project_id = Column(String(32), nullable=False)
    task_id = Column(String(32), nullable=False)
    task_code = Column(String(16), nullable=False, default="")
    # 学生自检没有正式提交：submission_id 为空，绑定的是草稿指纹。
    submission_id = Column(String(32), nullable=True)
    submission_code = Column(String(16), nullable=False, default="")
    submission_version = Column(Integer, nullable=False, default=0)

    trigger_source = Column(String(32), nullable=False)
    triggered_by = Column(String(32), nullable=False)
    triggered_role = Column(String(16), nullable=False, default="")

    status = Column(String(16), nullable=False, default="queued")

    # --- 不可变检查快照（§5.3）------------------------------------------------
    # {task:{code,title,purpose,description,version}, acs:[{ac_code,statement,
    #  evidence_requirement,required,check_type,version}], submission:{summary,
    #  self_assessment,git_repo,git_branch,git_commit,experiment_notes,
    #  known_issues,unfinished_items}, materials:[{attachment_code,name,sha256,
    #  size,version,kind}], created_at, created_by}
    check_snapshot = Column(JSON, nullable=False, default=dict)
    # 快照输入的哈希：草稿变化后重算即可判定"基于旧快照"（§11.2），不改历史。
    draft_fingerprint = Column(String(64), nullable=False, default="")
    # 本次使用的验收标准版本（§11.4：旧提交继续用旧标准）。
    ac_version = Column(Integer, nullable=False, default=1)

    # --- 输入预算与材料清单（§5.5.1、§5.5.2）--------------------------------
    # 实际生效的预算值与配置版本，必须写进快照并在页面展示。
    input_budget = Column(JSON, nullable=False, default=dict)
    # 逐项材料：read_state / reason / 页码·行号·字符区间 / affected_acs。
    material_manifest = Column(JSON, nullable=False, default=list)
    # 未读取与部分读取的清单（§5.5.2 要求"完整输入清单"，不只是 toast）。
    unread_materials = Column(JSON, nullable=False, default=list)
    partially_read_materials = Column(JSON, nullable=False, default=list)

    # --- 调用计划与批次（§5.4、§12.2.1）------------------------------------
    # [{batch_index, ac_codes, status, model_call_index}]
    call_plan = Column(JSON, nullable=False, default=list)
    total_batches = Column(Integer, nullable=False, default=1)
    completed_batches = Column(Integer, nullable=False, default=0)
    unchecked_acs = Column(JSON, nullable=False, default=list)
    # 某批失败时保存部分结果，但不得伪装成完整预审（§12.2.1、P-18）。
    partial_results = Column(Boolean, nullable=False, default=False)
    # 每次实际模型调用的取用/释放记录（P-10 的运行时证据；真正的闸门证据由
    # chatchat/server/model_capacity 侧输出）。
    model_calls = Column(JSON, nullable=False, default=list)

    # --- 结果（§10.2）------------------------------------------------------
    summary_state = Column(String(32), nullable=False, default="")
    # 验证深度说明，例如"全部结论仅基于静态证据，未运行验证"（§9.1）。
    summary_note = Column(Text, nullable=False, default="")
    finding_counts = Column(JSON, nullable=False, default=dict)

    # --- 可追踪性（§17）----------------------------------------------------
    model_name = Column(String(120), nullable=False, default="")
    rules_version = Column(String(32), nullable=False, default="")
    parser_version = Column(String(32), nullable=False, default="")
    whitelist_version = Column(String(32), nullable=False, default="")
    budget_config_version = Column(String(32), nullable=False, default="")
    # 提示注入与输出校验告警（§8.5、P-19）。
    security_flags = Column(JSON, nullable=False, default=list)

    queued_at = Column(DateTime, nullable=True)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    failure_reason = Column(Text, nullable=False, default="")

    cancel_reason = Column(Text, nullable=False, default="")
    cancelled_by = Column(String(32), nullable=False, default="")
    cancelled_at = Column(DateTime, nullable=True)

    # 学生私人自检默认不向导师展示（§4 规则3、P-03）；主动附带后才可见（P-04）。
    visible_to_mentor = Column(Boolean, nullable=False, default=False)
    attached_to_submission = Column(Boolean, nullable=False, default=False)

    create_time = Column(DateTime, nullable=True)
    update_time = Column(DateTime, nullable=True)
    deleted_at = Column(DateTime, nullable=True)


class SupervisionAIReviewFindingModel(Base):
    """逐条验收标准的 AI 判断（§9、§10.1）。"""

    __tablename__ = "supervision_ai_review_finding"
    __table_args__ = (
        UniqueConstraint("run_id", "ac_code", name="uq_supervision_ai_review_finding"),
        Index("ix_supervision_ai_review_finding_project", "project_id", "ac_code"),
        CheckConstraint(
            "verdict IN ('satisfied', 'partial', 'not_satisfied', 'unverifiable')",
            name="ck_supervision_ai_review_finding_verdict",
        ),
        CheckConstraint(
            "evidence_depth IN ('', 'E0', 'E1', 'E2', 'E3', 'E4')",
            name="ck_supervision_ai_review_finding_depth",
        ),
        CheckConstraint(
            "support_relation IN ('', 'no_evidence', 'supports', "
            "'partially_supports', 'contradicts', 'conflicting', "
            "'self_report_only')",
            name="ck_supervision_ai_review_finding_support",
        ),
    )

    id = Column(String(32), primary_key=True)
    run_id = Column(String(32), nullable=False)
    project_id = Column(String(32), nullable=False)
    ac_code = Column(String(16), nullable=False)
    ac_statement = Column(Text, nullable=False, default="")
    ac_version = Column(Integer, nullable=False, default=1)
    required = Column(Boolean, nullable=False, default=True)
    check_type = Column(String(16), nullable=False, default="document")

    verdict = Column(String(16), nullable=False, default="unverifiable")
    # 证据深度与判断状态分开存：静态实现证据（E2）永远不能表述成运行成功。
    evidence_depth = Column(String(8), nullable=False, default="")
    # 独立的「支持关系」轴，与 verdict 正交。
    support_relation = Column(String(24), nullable=False, default="")

    reason = Column(Text, nullable=False, default="")
    # partial 必须写清"已满足部分"和"缺少部分"（§9.2）。
    satisfied_parts = Column(Text, nullable=False, default="")
    missing_parts = Column(Text, nullable=False, default="")

    # 证据定位：[{attachment_code,name,locator:{line_start,line_end,
    # page_start,page_end,char_start,char_end,section},quote,depth}]
    evidence_refs = Column(JSON, nullable=False, default=list)
    missing_materials = Column(JSON, nullable=False, default=list)
    # 风险与冲突（§8.4）：[{kind,description,locations}]
    risks = Column(JSON, nullable=False, default=list)
    # 建议导师检查的位置（§10.1）。
    mentor_focus = Column(Text, nullable=False, default="")

    # 输出校验告警：被材料内指令诱导的迹象（§8.5、P-19）。
    injection_suspected = Column(Boolean, nullable=False, default=False)
    injection_note = Column(Text, nullable=False, default="")
    # 静态证据被表述成运行成功 → 降级 unverifiable 并记录原因（P-12）。
    runtime_claim_flag = Column(Boolean, nullable=False, default=False)
    downgrade_reason = Column(Text, nullable=False, default="")
    # 该结论来自模型输出校验降级的标记（§22.1 第6条：缺证据不得判满足）。
    validated = Column(Boolean, nullable=False, default=False)

    batch_index = Column(Integer, nullable=False, default=0)
    create_time = Column(DateTime, nullable=True)


class SupervisionAIReviewSettingModel(Base):
    """自动预审开关：项目默认 + 任务级，默认关闭（§6.2、§16.5 第2条）。

    ``task_id`` 为空串表示项目默认；任务行 ``mode=inherit`` 时继承项目设置。
    优先级：任务明确设置 > 项目默认 > 关闭。项目设置变化不追溯修改已发布
    任务的历史配置（§6.2），因此这里只存"当前设置"，历史由审计回答。
    """

    __tablename__ = "supervision_ai_review_setting"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "task_id", name="uq_supervision_ai_review_setting"
        ),
        CheckConstraint(
            "mode IN ('off', 'on', 'inherit')",
            name="ck_supervision_ai_review_setting_mode",
        ),
        Index("ix_supervision_ai_review_setting_project", "project_id"),
    )

    id = Column(String(32), primary_key=True)
    project_id = Column(String(32), nullable=False)
    # 空串 = 项目级默认；否则为任务内部 id。
    task_id = Column(String(32), nullable=False, default="")
    mode = Column(String(16), nullable=False, default="off")
    updated_by = Column(String(32), nullable=False, default="")
    updated_role = Column(String(16), nullable=False, default="")
    create_time = Column(DateTime, nullable=True)
    update_time = Column(DateTime, nullable=True)


__all__ = [
    "AUTO_REVIEW_MODE_VALUES",
    "CODE_AI_REVIEW",
    "EVIDENCE_DEPTH_VALUES",
    "MATERIAL_READ_STATE_VALUES",
    "RUN_STATUS_VALUES",
    "SUMMARY_STATE_VALUES",
    "SUPPORT_RELATION_VALUES",
    "SupervisionAIReviewFindingModel",
    "SupervisionAIReviewRunModel",
    "SupervisionAIReviewSettingModel",
    "TRIGGER_SOURCE_VALUES",
    "VERDICT_VALUES",
]
