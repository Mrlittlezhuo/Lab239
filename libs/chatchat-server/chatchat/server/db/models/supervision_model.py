"""师生科研协作模型（《学生研究与导师审核功能重新设计方案》里程碑0A）。

归属边界（设计方案 §3.1/§9.0/§10）：

- ``supervision_project`` 是**师生协作项目**，与用户私有的
  ``research_project``（个人论文研究工作区）是两套对象。现有
  ``conversation.project_id`` 继续只指向 ``research_project.id``，
  协作项目一律使用独立外键 ``supervision_project_id``，两个字段可以
  并存但分别鉴权；绝不把协作项目ID写进 ``project_id``，也不借用
  项目范围记忆字段绕过成员关系。
- 项目可见性由 ``supervision_project_member`` 决定，不由项目上的单个
  owner 决定。首期一个项目恰好一名学生 + 一名主导师（§3.2），用两个
  部分唯一索引在数据库层钉住，避免服务层漏判。
- 所有跨用户读写必须先解析成员关系再读子资源；只凭资源ID查询后在
  前端隐藏是不允许的（§10）。

ID 规则（设计方案 §4.0）：内部 ``id`` 是 32 位十六进制键，对外引用一律
用可读编号（``RP-001``、``EV-001``）。审计与通知保存并引用**可读编号**，
不保存内部键，否则导出后无法定位对象。``EV-`` 事件编号是审计与通知
共用的一套命名空间：一次业务事件产生一个事件编号，发给各接收者的
通知引用同一个编号（§12）。
"""
from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Index,
    String,
    UniqueConstraint,
    text,
)

from chatchat.server.db.base import Base


class SupervisionProjectModel(Base):
    """科研协作项目：学生与主导师共享的研究协作容器。"""

    __tablename__ = "supervision_project"
    __table_args__ = (
        Index("ix_supervision_project_status_update", "status", "update_time"),
        CheckConstraint(
            "status IN ('draft', 'active', 'archived')",
            name="ck_supervision_project_status",
        ),
    )

    id = Column(String(32), primary_key=True)
    # 对外可读编号（RP-001）。审计、通知和界面引用它，不引用内部 id。
    code = Column(String(16), nullable=False, unique=True)
    name = Column(String(200), nullable=False)
    description = Column(String(2000), nullable=False, default="")
    # draft=筹备中（还没有师生闭环内容），active=进行中，archived=已归档。
    status = Column(String(16), nullable=False, default="draft")
    # 归档走"学生申请 → 主导师处理"两条时点（§10 权限矩阵），
    # 申请本身不关闭访问权限。
    archive_requested_at = Column(DateTime, nullable=True)
    archive_request_reason = Column(String(1000), nullable=False, default="")
    archived_at = Column(DateTime, nullable=True)
    archive_reject_reason = Column(String(1000), nullable=False, default="")
    # 软删除：关闭访问，不物理删除行，已有审核记录必须保留（§13）。
    deleted_at = Column(DateTime, nullable=True)
    delete_reason = Column(String(1000), nullable=False, default="")
    create_time = Column(DateTime, nullable=True)
    update_time = Column(DateTime, nullable=True)


class SupervisionProjectMemberModel(Base):
    """项目成员关系：项目可见性的唯一依据。"""

    __tablename__ = "supervision_project_member"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "user_id", name="uq_supervision_member_project_user"
        ),
        Index("ix_supervision_member_user", "user_id", "status"),
        CheckConstraint(
            "role IN ('student', 'mentor')", name="ck_supervision_member_role"
        ),
        CheckConstraint(
            "status IN ('active', 'removed')", name="ck_supervision_member_status"
        ),
        # 首期一个项目只允许一名在册学生和一名在册主导师（§3.2）。
        # 放在数据库层是因为服务层漏判会直接变成越权读取。
        Index(
            "uq_supervision_member_active_student",
            "project_id",
            unique=True,
            sqlite_where=text("role = 'student' AND status = 'active'"),
            postgresql_where=text("role = 'student' AND status = 'active'"),
        ),
        Index(
            "uq_supervision_member_active_mentor",
            "project_id",
            unique=True,
            sqlite_where=text("role = 'mentor' AND status = 'active'"),
            postgresql_where=text("role = 'mentor' AND status = 'active'"),
        ),
    )

    id = Column(String(32), primary_key=True)
    project_id = Column(String(32), nullable=False)
    user_id = Column(String(32), nullable=False)
    role = Column(String(16), nullable=False)
    status = Column(String(16), nullable=False, default="active")
    joined_at = Column(DateTime, nullable=True)
    removed_at = Column(DateTime, nullable=True)


class SupervisionAuditEventModel(Base):
    """项目审计事件：谁在何时对哪个固定版本做了什么。

    ``event_code``（EV-001）是审计与通知共用的事件编号。同一业务事件
    发给多个接收者时审计只有一条记录，通知按接收者各一条并引用同一个
    ``event_code``（§12）。
    """

    __tablename__ = "supervision_audit_event"
    __table_args__ = (
        Index("ix_supervision_audit_project_time", "project_id", "created_at"),
        Index("ix_supervision_audit_object", "object_type", "object_code"),
    )

    id = Column(String(32), primary_key=True)
    event_code = Column(String(16), nullable=False, unique=True)
    project_id = Column(String(32), nullable=False)
    # 对象用可读编号引用（提交用 S-001-v2、预审用 AR-001-r2），
    # 不用内部 id，保证导出记录可回溯。
    object_type = Column(String(32), nullable=False)
    object_code = Column(String(32), nullable=False, default="")
    actor_id = Column(String(32), nullable=False)
    actor_role = Column(String(16), nullable=False, default="")
    action = Column(String(64), nullable=False)
    detail = Column(String(2000), nullable=False, default="")
    created_at = Column(DateTime, nullable=True)


class SupervisionNotificationModel(Base):
    """站内通知：按接收者独立保存读取状态。

    唯一约束 ``(event_code, recipient_id)`` 直接实现 §12 的
    "同一业务事件对同一接收者只生成一条通知，重试不得重复发送"。
    标记已读只改本行的 ``read_at``，不触碰任何业务状态。
    """

    __tablename__ = "supervision_notification"
    __table_args__ = (
        UniqueConstraint(
            "event_code", "recipient_id", name="uq_supervision_notification_event_recipient"
        ),
        Index("ix_supervision_notification_recipient", "recipient_id", "read_at"),
        Index("ix_supervision_notification_project", "project_id"),
    )

    id = Column(String(32), primary_key=True)
    event_code = Column(String(16), nullable=False)
    recipient_id = Column(String(32), nullable=False)
    project_id = Column(String(32), nullable=False)
    object_type = Column(String(32), nullable=False, default="")
    object_code = Column(String(32), nullable=False, default="")
    event_type = Column(String(64), nullable=False, default="")
    title = Column(String(200), nullable=False)
    body = Column(String(2000), nullable=False, default="")
    level = Column(String(16), nullable=False, default="info")
    created_at = Column(DateTime, nullable=True)
    read_at = Column(DateTime, nullable=True)


__all__ = [
    "SupervisionAuditEventModel",
    "SupervisionNotificationModel",
    "SupervisionProjectMemberModel",
    "SupervisionProjectModel",
]
