"""跨会话记忆召回的辅助表（智能聊天记忆第三期，v15）。

两张表都按 ``owner_id`` 隔离，且都不保存原始聊天全文：

- ``memory_revision``：记忆被编辑/替代前的旧值快照，用于"编辑保留修订历史"
  与"查看完整修订链"（设计方案 §4.2、§4.5）。删除记忆时一并删除其修订行。
- ``conversation_memory_ignore``：界面上的"本次不使用"——只让该记忆在**当前
  会话**里不被召回，原记录保留，其他会话不受影响（设计方案 §4.2 / P3-04）。
  放在服务端而不是浏览器里，是为了让普通聊天、知识库对话与 Agent 任务三条
  链路都自动遵守同一条规则。
"""
from datetime import datetime

from sqlalchemy import Column, DateTime, Index, String, func

from chatchat.server.db.base import Base


class MemoryRevisionModel(Base):
    """一条记忆的历史版本（编辑或替代前的内容）。"""

    __tablename__ = "memory_revision"
    __table_args__ = (
        # 与 v15 迁移里手工建的索引同名同列（迁移用 IF NOT EXISTS，
        # 新库由本声明创建，两边不冲突）
        Index("ix_memory_revision_owner_memory", "owner_id", "memory_id"),
    )

    id = Column(String(32), primary_key=True, comment="修订记录ID(UUID hex)")
    memory_id = Column(
        String(32), nullable=False, comment="所属记忆ID（memory.id）"
    )
    owner_id = Column(
        String(32),
        nullable=False,
        index=True,
        comment="所属用户ID（服务端写入，来自认证上下文）",
    )
    content = Column(String(2000), nullable=False, comment="修改前的内容")
    kind = Column(String(32), nullable=False, default="", comment="修改前的类别")
    scope = Column(String(16), nullable=False, default="", comment="修改前的范围")
    project_id = Column(String(32), nullable=True, comment="修改前的项目ID")
    project_name = Column(String(200), nullable=True, comment="修改前的项目名称")
    expires_at = Column(DateTime, nullable=True, comment="修改前的有效期")
    reason = Column(
        String(32),
        nullable=False,
        default="edit",
        comment="变更原因：edit（编辑）/ supersede（被新决策替代）",
    )
    create_time = Column(
        DateTime, nullable=False, default=datetime.utcnow, comment="快照时间"
    )

    def __repr__(self):
        return (
            f"<MemoryRevision(id='{self.id}', memory_id='{self.memory_id}', "
            f"reason='{self.reason}')>"
        )


class ConversationMemoryIgnoreModel(Base):
    """会话内"本次不使用"的记忆指针（不复制内容，原记忆不受影响）。"""

    __tablename__ = "conversation_memory_ignore"
    __table_args__ = (
        Index("ix_conversation_memory_ignore_owner", "owner_id"),
    )

    conversation_id = Column(
        String(32), primary_key=True, comment="会话ID（越权写入被 repository 拦住）"
    )
    memory_id = Column(String(32), primary_key=True, comment="被忽略的记忆ID")
    owner_id = Column(
        String(32),
        nullable=False,
        index=True,
        comment="所属用户ID（服务端写入，来自认证上下文）",
    )
    create_time = Column(
        DateTime, nullable=False, default=datetime.utcnow, comment="忽略时间"
    )

    def __repr__(self):
        return (
            f"<ConversationMemoryIgnore(conversation_id='{self.conversation_id}', "
            f"memory_id='{self.memory_id}')>"
        )


__all__ = ["MemoryRevisionModel", "ConversationMemoryIgnoreModel"]
