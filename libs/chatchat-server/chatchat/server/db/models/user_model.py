from datetime import datetime
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    text,
)

from chatchat.server.db.base import Base


class UserModel(Base):
    """
    最小用户账户模型。

    物理表使用 user_account，避免保留字 user。password_hash 禁止出现
    在 repr、API 响应或日志中。
    """

    __tablename__ = "user_account"
    __table_args__ = (
        CheckConstraint("role IN ('admin', 'user')", name="ck_user_account_role"),
        CheckConstraint(
            "status IN ('active', 'disabled')",
            name="ck_user_account_status",
        ),
        CheckConstraint(
            "auth_version >= 1",
            name="ck_user_account_auth_version",
        ),
    )

    id = Column(
        String(32),
        primary_key=True,
        comment="用户ID(UUID hex，服务端生成)",
    )
    username = Column(
        String(64),
        unique=True,
        index=True,
        nullable=False,
        comment="用户名（规范化小写，仅存 a-z0-9._-）",
    )
    display_name = Column(
        String(100),
        nullable=False,
        comment="显示名称（允许 Unicode）",
    )
    password_hash = Column(
        String(255),
        nullable=False,
        comment="scrypt 密码哈希（禁止外泄）",
    )
    role = Column(String(16), nullable=False, default="user", comment="admin / user")
    status = Column(
        String(16),
        nullable=False,
        default="active",
        comment="active / disabled",
    )
    must_change_password = Column(
        Boolean, nullable=False, default=True, comment="是否必须修改密码"
    )
    auth_version = Column(Integer, nullable=False, default=1, comment="认证版本")
    memory_auto_enabled = Column(
        Boolean,
        nullable=False,
        default=True,
        comment="是否允许自动提取长期记忆（用户可关闭）",
    )
    memory_preferences_enabled = Column(
        Boolean,
        nullable=False,
        default=True,
        comment="是否把已启用的个人偏好注入新对话（关闭不删除数据）",
    )
    # --- 智能聊天记忆第三期：跨会话相关记忆召回开关（v15）---
    memory_recall_enabled = Column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("1"),
        comment="跨会话相关记忆总开关（关闭只停止召回，不删除记忆；个人偏好不受影响）",
    )
    memory_recall_decisions_enabled = Column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("1"),
        comment="是否召回已确认的历史决策/背景/待办",
    )
    memory_recall_project_enabled = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("0"),
        comment="是否召回项目范围记忆（仅在会话选定该项目时生效）",
    )
    memory_recall_general_enabled = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("0"),
        comment="是否召回未确认的一般历史会话结论（默认关闭，需用户主动开启）",
    )
    create_time = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        comment="创建时间",
    )
    update_time = Column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow,
        comment="更新时间",
    )

    def __repr__(self):
        # 不含 password_hash / 明文密码
        return (
            f"<User(id={self.id!r}, username={self.username!r}, role={self.role!r}, "
            f"status={self.status!r}, "
            f"must_change_password={self.must_change_password}, "
            f"auth_version={self.auth_version})>"
        )


__all__ = ["UserModel", "OpenAIFileModel"]


class OpenAIFileModel(Base):
    """
    OpenAI 兼容 ``/v1/files`` 接口（聊天图片/附件）的归属记录。

    每张图片/附件由上传用户独占：读取、列表、删除都必须校验
    ``owner_id``，用户 A 无法获取用户 B 的附件。物理路径仍位于
    ``BASE_TEMP_DIR/openai_files`` 下按用户分子目录，但权限以本表为
    事实来源（owner_id 来自服务端认证上下文）。
    """

    __tablename__ = "openai_file"
    id = Column(String(128), primary_key=True, comment="文件ID")
    owner_id = Column(
        String(32),
        ForeignKey("user_account.id"),
        nullable=False,
        index=True,
        comment="所属用户ID（上传用户，服务端写入）",
    )
    filename = Column(String(255), nullable=False, comment="文件名（含扩展名）")
    purpose = Column(
        String(64), nullable=False, default="assistants", comment="用途（OpenAI purpose）"
    )
    created_at = Column(Integer, nullable=False, comment="上传时间戳（秒）")
    size = Column(Integer, nullable=True, comment="文件大小（字节）")

    def __repr__(self):
        return (
            f"<OpenAIFile(id='{self.id}', owner_id='{self.owner_id}', "
            f"filename='{self.filename}', purpose='{self.purpose}')>"
        )
