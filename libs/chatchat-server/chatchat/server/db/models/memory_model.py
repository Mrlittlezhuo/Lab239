from datetime import datetime
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)

from chatchat.server.db.base import Base


class MemoryModel(Base):
    """
    用户级长期记忆（独立于 conversation/message）。

    每条记忆属于且仅属于一个用户：读取、更新、删除都必须同时校验
    ``owner_id``（owner 来自服务端认证上下文，不信任请求体）。记忆
    不进入公共知识库、公共向量索引，不能被其他用户读取或使用。

    - ``type``：内容类别（preference / fact / goal / important）。
    - ``category``：个人偏好类别（answer_style / technical_background /
      work_style / long_term_goal），空表示"不是用户管理的个人偏好"
      （例如自动提取的长期事实）。取值由应用层白名单校验：SQLite 无法对
      已有表追加 ``CHECK``，加约束需要整表重建，代价与收益不匹配。
    - ``source``：来源（manual / auto_extract / chat）。
    - ``importance``：1-5，供召回排序使用。
    - ``enabled``：用户可停用/启用；停用的记忆不参与召回。
    """

    __tablename__ = "memory"
    __table_args__ = (
        UniqueConstraint("owner_id", "memory_key", name="uq_memory_owner_key"),
        CheckConstraint(
            "type IN ('preference', 'fact', 'goal', 'important')",
            name="ck_memory_type",
        ),
        CheckConstraint(
            "importance >= 1 AND importance <= 5",
            name="ck_memory_importance",
        ),
        # 与 v6/v13 迁移里手工建的索引同名同列（迁移用 IF NOT EXISTS，
        # 新库由本声明创建，两边不冲突）
        Index("ix_memory_owner_type", "owner_id", "type"),
        Index("ix_memory_owner_category", "owner_id", "category"),
    )

    id = Column(String(32), primary_key=True, comment="记忆ID(UUID hex，服务端生成)")
    owner_id = Column(
        String(32),
        nullable=False,
        index=True,
        comment="所属用户ID（服务端写入，来自认证上下文）",
    )
    memory_key = Column(
        String(128),
        nullable=False,
        comment="稳定主题键，用于同一事实/偏好的冲突更新",
    )
    type = Column(
        String(16),
        nullable=False,
        default="fact",
        comment="内容类别：preference/fact/goal/important",
    )
    category = Column(
        String(32),
        nullable=True,
        comment="个人偏好类别；空表示非用户管理的个人偏好（如自动提取的事实）",
    )
    content = Column(String(2000), nullable=False, comment="记忆内容（非完整聊天记录）")
    source = Column(
        String(16),
        nullable=False,
        default="chat",
        comment="来源：manual/auto_extract/chat",
    )
    source_conversation_id = Column(
        String(32), nullable=True, comment="来源会话ID"
    )
    source_message_id = Column(
        String(32), nullable=True, comment="来源消息ID"
    )
    importance = Column(
        Integer,
        nullable=False,
        default=1,
        comment="重要性 1-5，用于召回排序",
    )
    enabled = Column(Boolean, nullable=False, default=True, comment="是否启用")
    create_time = Column(DateTime, default=func.now(), comment="创建时间")
    update_time = Column(DateTime, default=func.now(), onupdate=func.now(), comment="更新时间")

    def __repr__(self):
        return (
            f"<Memory(id='{self.id}', owner_id='{self.owner_id}', "
            f"type='{self.type}', enabled={self.enabled})>"
        )


__all__ = ["MemoryModel"]
