from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)

from chatchat.server.db.base import Base


class SessionSummaryModel(Base):
    """
    会话私有上下文（每会话一行）：较早历史的压缩摘要 + 本会话的结构化槽位。

    智能聊天记忆第二期把"摘要"扩展为"会话上下文"（设计方案 §3）：主题、
    目标、已确认事项、限制条件、待解决问题 + 较早内容摘要。仍复用同一张
    每会话一行的表，不另建表。

    - 短期上下文仍由最近若干轮原始消息构成，本表不替代它；模型注入时两者
      并存（上下文段在前、近期轮次在后），最近的关键消息始终以完整原文
      参与回答（设计方案 §4.1）。
    - **摘要覆盖范围与最近原文窗口必须无缝衔接**：分界由统一上下文构建器
      （``memory.context_package``）按字符预算算出，本表的
      ``summarized_message_id`` 必须正好落在分界之前，不允许出现"既没进
      摘要、也不在最近原文"的中间消息（设计方案 §6.2）。
    - ``summarized_message_id`` / ``summarized_count`` 记录摘要覆盖到的
      消息位置；后续只对新增消息做增量摘要，避免重复总结。
    - ``context_start_after_message_id`` 是重置起点：重置只清字段不够，必须
      记下从哪条消息之后重新建立上下文，否则下一轮重扫会把旧结论找回来
      （设计方案 §7.7）。
    - ``version`` 是乐观锁版本：并发更新同一会话时，只有版本仍一致才能
      原子写入摘要、覆盖位置和新版本号（设计方案 §7.5）。
    - ``summary_type`` / ``summary_status`` / ``summary_error`` /
      ``summary_updated_at`` 让界面能如实区分规则摘要与模型摘要，并让失败
      可见而不是静默（设计方案 §7.2、§15.1）。
    - ``slots_scanned_through_message_id`` / ``slots_scanned_count`` 是结构化
      槽位的增量游标：每轮只扫描新增消息，长会话不再平方级重扫
      （设计方案 §4.9）。
    - ``pinned_json`` 保存用户手工编辑或明确确认过的条目（含来源消息位置）：
      自动更新只补充新条目，不静默覆盖这些结论（设计方案 §4.2）。
    - 会话上下文不是长期事实，也不跨会话：长期召回只读 ``memory`` 表；
      临时对话（``conversation.temporary``）不写入本表（设计方案 §4.4）。
    - 上下文按会话隔离（会话已按用户隔离），owner_id 冗余便于按用户直查。
    """

    __tablename__ = "session_summary"
    __table_args__ = (
        UniqueConstraint("conversation_id", name="uq_session_summary_conversation"),
        CheckConstraint("summarized_count >= 0", name="ck_session_summary_count"),
    )

    id = Column(String(32), primary_key=True, comment="摘要记录ID")
    owner_id = Column(
        String(32),
        nullable=False,
        index=True,
        comment="所属用户ID（会话属主的冗余记录）",
    )
    conversation_id = Column(
        String(32),
        nullable=False,
        index=True,
        comment="会话ID",
    )
    summary = Column(String(4000), nullable=False, default="", comment="压缩后的较早内容摘要")
    # --- 会话私有上下文的结构化槽位（第二期）---
    # server_default 与 v14 迁移的 DDL 一致：既有行以及绕过 ORM 的写入
    # （迁移脚本、运维 SQL）也拿得到默认值。
    topic = Column(
        String(200), nullable=False, default="", server_default="", comment="会话主题"
    )
    goal = Column(
        String(500), nullable=False, default="", server_default="", comment="本会话目标"
    )
    confirmed = Column(
        Text, nullable=False, default="", server_default="", comment="已确认事项（每行一条）"
    )
    constraints = Column(
        Text, nullable=False, default="", server_default="", comment="本会话限制条件（每行一条）"
    )
    open_questions = Column(
        Text, nullable=False, default="", server_default="", comment="待解决问题（每行一条）"
    )
    pinned_json = Column(
        Text,
        nullable=False,
        default="[]",
        server_default="[]",
        comment="用户手工编辑/明确确认过的条目（JSON：text/slot/message_index）",
    )
    completed_items = Column(
        Text,
        nullable=False,
        default="",
        server_default="",
        comment="已完成事项（每行一条；与仍生效的已确认事项分开）",
    )
    summarized_message_id = Column(
        String(32), nullable=True, comment="摘要覆盖到的最后一条消息ID（位置标记）"
    )
    summarized_count = Column(
        Integer, nullable=False, default=0, comment="摘要覆盖的消息数"
    )
    # --- 第四期第一阶段 A：上下文正确性（设计方案 §7.1/§7.5/§7.7、§15.1）---
    context_start_after_message_id = Column(
        String(32),
        nullable=True,
        comment="重置起点：只从这条消息之后重新建立上下文（NULL 表示从头）",
    )
    version = Column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        comment="乐观锁版本：并发更新摘要/覆盖位置时校验",
    )
    summary_type = Column(
        String(16),
        nullable=False,
        default="rule",
        server_default="rule",
        comment="摘要来源：rule（确定性规则摘要）/ llm（模型语义摘要）",
    )
    summary_status = Column(
        String(16),
        nullable=False,
        default="idle",
        server_default="idle",
        comment="最近一次摘要更新结果：idle / succeeded / failed",
    )
    summary_error = Column(
        String(500), nullable=True, comment="最近一次摘要失败原因（面向管理员）"
    )
    summary_updated_at = Column(
        DateTime, nullable=True, comment="最近一次摘要正文更新时间"
    )
    needs_rebuild = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
        comment="旧摘要文本存在但覆盖位置不可确认，需按规则安全重建（v16 判据）",
    )
    slots_scanned_through_message_id = Column(
        String(32),
        nullable=True,
        comment="结构化槽位增量游标：只扫描这条消息之后新增的消息",
    )
    slots_scanned_count = Column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        comment="已扫描过的消息数（保证条目 message_index 跨轮稳定）",
    )
    create_time = Column(DateTime, default=func.now(), comment="创建时间")
    update_time = Column(DateTime, default=func.now(), onupdate=func.now(), comment="更新时间")

    def __repr__(self):
        return (
            f"<SessionSummary(conversation_id='{self.conversation_id}', "
            f"owner_id='{self.owner_id}', summarized_count={self.summarized_count})>"
        )


__all__ = ["SessionSummaryModel"]
