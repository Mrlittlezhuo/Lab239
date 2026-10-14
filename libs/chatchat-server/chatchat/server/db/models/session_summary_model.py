from sqlalchemy import (
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

    - 短期上下文仍由最近若干轮原始消息（``filter_message``）构成，本表
      不替代它；模型注入时两者并存（上下文段在前、近期轮次在后），
      最近的关键消息始终以完整原文参与回答（设计方案 §4.1）。
    - ``summarized_message_id`` / ``summarized_count`` 记录摘要覆盖到的
      消息位置；后续只对新增消息做增量摘要，避免重复总结。
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
    summarized_message_id = Column(
        String(32), nullable=True, comment="摘要覆盖到的最后一条消息ID（位置标记）"
    )
    summarized_count = Column(
        Integer, nullable=False, default=0, comment="摘要覆盖的消息数"
    )
    create_time = Column(DateTime, default=func.now(), comment="创建时间")
    update_time = Column(DateTime, default=func.now(), onupdate=func.now(), comment="更新时间")

    def __repr__(self):
        return (
            f"<SessionSummary(conversation_id='{self.conversation_id}', "
            f"owner_id='{self.owner_id}', summarized_count={self.summarized_count})>"
        )


__all__ = ["SessionSummaryModel"]
