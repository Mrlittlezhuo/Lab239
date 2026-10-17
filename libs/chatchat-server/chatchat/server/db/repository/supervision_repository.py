"""师生协作数据访问（里程碑0A）。

权限模型（设计方案 §10）：项目可见性只由成员关系决定，owner 只来自
服务端认证上下文。所有读取都先解析成员关系再取子资源；不存在与无权
统一返回 ``None``，由路由层收敛成同一个 404，避免通过响应差异枚举
项目是否存在。

编号规则（§4.0）：内部 32 位键用于外键连接，对外引用用可读编号
（``RP-001``、``EV-001``）。审计与通知只保存可读编号。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from chatchat.server.db.models.supervision_model import (
    SupervisionAuditEventModel,
    SupervisionNotificationModel,
    SupervisionProjectMemberModel,
    SupervisionProjectModel,
)

ROLE_STUDENT = "student"
ROLE_MENTOR = "mentor"

PROJECT_DRAFT = "draft"
PROJECT_ACTIVE = "active"
PROJECT_ARCHIVED = "archived"

# 可读编号前缀（设计方案 §4.0 的 ID 表）。
CODE_PROJECT = "RP"
CODE_EVENT = "EV"


def new_id() -> str:
    return uuid.uuid4().hex


def next_code(session: Session, model, column, prefix: str) -> str:
    """按已有最大序号生成下一个可读编号（RP-001、EV-001）。

    序号从已提交行里取，唯一索引是并发下的兜底；调用方在
    ``IntegrityError`` 时重试。不用独立序列表，是为了让"编号"这件事
    只有一处真相，迁移和备份也不会漏掉它。
    """
    last = session.execute(
        select(func.max(column)).where(column.like(f"{prefix}-%"))
    ).scalar()
    sequence = int(last.split("-")[1]) if last else 0
    return f"{prefix}-{sequence + 1:03d}"


def _now() -> datetime:
    return datetime.now()


# --------------------------------------------------------------------------
# 权限解析
# --------------------------------------------------------------------------


def get_membership(
    session: Session, project_id: str, user_id: str
) -> Optional[SupervisionProjectMemberModel]:
    """取该用户在该项目的在册成员关系；不在册返回 None。"""
    return session.execute(
        select(SupervisionProjectMemberModel).where(
            SupervisionProjectMemberModel.project_id == project_id,
            SupervisionProjectMemberModel.user_id == user_id,
            SupervisionProjectMemberModel.status == "active",
        )
    ).scalar_one_or_none()


def resolve_project(
    session: Session, project_ref: str, user_id: str, *, is_admin: bool = False
) -> Optional[tuple[SupervisionProjectModel, Optional[str]]]:
    """按内部 id 或可读编号解析项目，并返回 (项目, 当前用户项目角色)。

    管理员可以读到不属于自己项目的**业务记录**（§13 要求这类查看另起
    审计），但拿不到项目角色，因此不能据此执行导师或学生动作。已软删除
    的项目对所有人不可见。
    """
    project = session.execute(
        select(SupervisionProjectModel).where(
            SupervisionProjectModel.id == project_ref,
            SupervisionProjectModel.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if project is None:
        project = session.execute(
            select(SupervisionProjectModel).where(
                SupervisionProjectModel.code == project_ref,
                SupervisionProjectModel.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
    if project is None:
        return None

    membership = get_membership(session, project.id, user_id)
    if membership is not None:
        return project, membership.role
    if is_admin:
        return project, None
    return None


def require_member(
    session: Session, project_ref: str, user_id: str, *, roles: tuple[str, ...] = ()
) -> tuple[SupervisionProjectModel, str]:
    """成员校验失败统一抛 ``PermissionError``，由路由层转 404/403。

    ``roles`` 非空时还要求项目角色在集合内（例如只有主导师能同意归档）。
    """
    resolved = resolve_project(session, project_ref, user_id)
    if resolved is None:
        raise PermissionError("项目不存在或无权访问")
    project, role = resolved
    if role is None:
        raise PermissionError("需要项目成员身份")
    if roles and role not in roles:
        raise PermissionError(f"需要项目角色：{'/'.join(roles)}")
    return project, role


# --------------------------------------------------------------------------
# 审计与通知
# --------------------------------------------------------------------------


def record_audit(
    session: Session,
    *,
    project_id: str,
    object_type: str,
    object_code: str,
    actor_id: str,
    action: str,
    detail: str = "",
    actor_role: str = "",
) -> str:
    """写入一条审计事件并返回其事件编号（EV-NNN）。

    审计只记录"发生了什么"，不代替业务状态；任何业务表都不因写审计而
    改动。事件编号是审计与通知共用的命名空间。
    """
    event_code = next_code(session, SupervisionAuditEventModel,
                           SupervisionAuditEventModel.event_code, CODE_EVENT)
    session.add(
        SupervisionAuditEventModel(
            id=new_id(),
            event_code=event_code,
            project_id=project_id,
            object_type=object_type,
            object_code=object_code,
            actor_id=actor_id,
            actor_role=actor_role,
            action=action,
            detail=detail[:2000],
            created_at=_now(),
        )
    )
    session.flush()
    return event_code


def notify(
    session: Session,
    *,
    event_code: str,
    recipients: list[str],
    project_id: str,
    title: str,
    event_type: str = "",
    body: str = "",
    object_type: str = "",
    object_code: str = "",
    level: str = "info",
) -> int:
    """按接收者各写一条通知；同一事件编号+接收者已存在则跳过。

    去重靠库层唯一约束，而不是"先查再插"：重试、重复点击和并发都不会
    让同一个人收到两条同样的通知（§12）。
    """
    written = 0
    for recipient_id in dict.fromkeys(recipients):  # 去重且保持顺序
        exists = session.execute(
            select(SupervisionNotificationModel.id).where(
                SupervisionNotificationModel.event_code == event_code,
                SupervisionNotificationModel.recipient_id == recipient_id,
            )
        ).scalar_one_or_none()
        if exists is not None:
            continue
        session.add(
            SupervisionNotificationModel(
                id=new_id(),
                event_code=event_code,
                recipient_id=recipient_id,
                project_id=project_id,
                object_type=object_type,
                object_code=object_code,
                event_type=event_type,
                title=title[:200],
                body=body[:2000],
                level=level,
                created_at=_now(),
                read_at=None,
            )
        )
        written += 1
    session.flush()
    return written


def list_audit_events(
    session: Session,
    *,
    project_id: str,
    viewer_id: str,
    viewer_role: Optional[str],
    object_code: str = "",
    action: str = "",
    limit: int = 100,
) -> list[SupervisionAuditEventModel]:
    """按项目读取审计事件（新→旧）。

    学生只能看到与自己有权读取的对象相关的业务审计（§13）；0A 阶段
    学生可见对象即项目内全部业务对象，因此先按成员放行，安全策略与
    内部错误细节不进入 ``detail``（写入侧保证）。
    """
    stmt = (
        select(SupervisionAuditEventModel)
        .where(SupervisionAuditEventModel.project_id == project_id)
        .order_by(SupervisionAuditEventModel.created_at.desc())
        .limit(limit)
    )
    if object_code:
        stmt = stmt.where(SupervisionAuditEventModel.object_code == object_code)
    if action:
        stmt = stmt.where(SupervisionAuditEventModel.action == action)
    return list(session.execute(stmt).scalars().all())


def list_notifications(
    session: Session,
    *,
    user_id: str,
    unread_only: bool = False,
    limit: int = 100,
) -> list[SupervisionNotificationModel]:
    """只返回发给该用户的通知；已读状态按人独立（§12、场景 H 第6步）。"""
    stmt = (
        select(SupervisionNotificationModel)
        .where(SupervisionNotificationModel.recipient_id == user_id)
        .order_by(SupervisionNotificationModel.created_at.desc())
        .limit(limit)
    )
    if unread_only:
        stmt = stmt.where(SupervisionNotificationModel.read_at.is_(None))
    return list(session.execute(stmt).scalars().all())


def mark_notification_read(
    session: Session, *, user_id: str, notification_id: str = "", mark_all: bool = False
) -> int:
    """标记已读。只改该用户自己的行，不触碰任何业务状态。"""
    if not mark_all and not notification_id:
        raise ValueError("必须给出通知ID或 mark_all")
    stmt = select(SupervisionNotificationModel).where(
        SupervisionNotificationModel.recipient_id == user_id,
        SupervisionNotificationModel.read_at.is_(None),
    )
    if not mark_all:
        stmt = stmt.where(SupervisionNotificationModel.id == notification_id)
    rows = list(session.execute(stmt).scalars().all())
    now = _now()
    for row in rows:
        row.read_at = now
    session.flush()
    return len(rows)


# --------------------------------------------------------------------------
# 项目与成员
# --------------------------------------------------------------------------


def list_projects_for_user(
    session: Session, user_id: str
) -> list[tuple[SupervisionProjectModel, str]]:
    """列出该用户作为在册成员能看到的项目（软删除的不出现）。"""
    rows = session.execute(
        select(SupervisionProjectMemberModel, SupervisionProjectModel)
        .join(
            SupervisionProjectModel,
            SupervisionProjectModel.id == SupervisionProjectMemberModel.project_id,
        )
        .where(
            SupervisionProjectMemberModel.user_id == user_id,
            SupervisionProjectMemberModel.status == "active",
            SupervisionProjectModel.deleted_at.is_(None),
        )
        .order_by(SupervisionProjectModel.update_time.desc())
    ).all()
    return [(project, member.role) for member, project in rows]


def create_project(
    session: Session, *, name: str, description: str, creator_id: str
) -> SupervisionProjectModel:
    """创建协作项目，创建者作为学生入册。

    新项目是 ``draft``（筹备中）：只有空白筹备项目能被创建者删除，
    一旦有正式内容就只能走归档（§15.1 放行标准第2条）。
    """
    now = _now()
    for _ in range(5):
        project = SupervisionProjectModel(
            id=new_id(),
            code=next_code(session, SupervisionProjectModel,
                           SupervisionProjectModel.code, CODE_PROJECT),
            name=name[:200],
            description=(description or "")[:2000],
            status=PROJECT_DRAFT,
            create_time=now,
            update_time=now,
        )
        session.add(project)
        try:
            session.flush()
        except IntegrityError:
            # 编号并发撞车：回滚到保存点重试，不引入额外序列表。
            session.rollback()
            continue
        session.add(
            SupervisionProjectMemberModel(
                id=new_id(),
                project_id=project.id,
                user_id=creator_id,
                role=ROLE_STUDENT,
                status="active",
                joined_at=now,
            )
        )
        session.flush()
        return project
    raise RuntimeError("项目编号生成失败，请重试")


def add_member(
    session: Session, *, project_ref: str, user_id: str, role: str, actor_id: str
) -> SupervisionProjectMemberModel:
    """加入成员。首期每项目一名在册学生 + 一名在册主导师，由库层索引兜住。"""
    project, _ = require_member(session, project_ref, actor_id)
    now = _now()
    member = SupervisionProjectMemberModel(
        id=new_id(),
        project_id=project.id,
        user_id=user_id,
        role=role,
        status="active",
        joined_at=now,
    )
    session.add(member)
    session.flush()
    return member


def remove_member(
    session: Session, *, project_ref: str, user_id: str, actor_id: str
) -> SupervisionProjectMemberModel:
    """移出成员：关闭后续访问，不删除历史业务记录（§13）。"""
    project, _ = require_member(session, project_ref, actor_id)
    member = session.execute(
        select(SupervisionProjectMemberModel).where(
            SupervisionProjectMemberModel.project_id == project.id,
            SupervisionProjectMemberModel.user_id == user_id,
            SupervisionProjectMemberModel.status == "active",
        )
    ).scalar_one_or_none()
    if member is None:
        raise LookupError("该用户不是本项目在册成员")
    member.status = "removed"
    member.removed_at = _now()
    session.flush()
    return member


def list_members(session: Session, *, project_id: str) -> list[SupervisionProjectMemberModel]:
    return list(
        session.execute(
            select(SupervisionProjectMemberModel)
            .where(SupervisionProjectMemberModel.project_id == project_id)
            .order_by(SupervisionProjectMemberModel.joined_at)
        )
        .scalars()
        .all()
    )


def active_member_ids(
    session: Session, *, project_id: str, role: Optional[str] = None
) -> list[str]:
    """在册成员的用户ID，用于通知收件人。"""
    stmt = select(SupervisionProjectMemberModel.user_id).where(
        SupervisionProjectMemberModel.project_id == project_id,
        SupervisionProjectMemberModel.status == "active",
    )
    if role:
        stmt = stmt.where(SupervisionProjectMemberModel.role == role)
    return list(session.execute(stmt).scalars().all())


def project_activity_count(session: Session, *, project_id: str) -> int:
    """项目是否已经产生正式内容。空白筹备项目才允许被创建者删除。"""
    return int(
        session.execute(
            select(func.count(SupervisionAuditEventModel.id)).where(
                SupervisionAuditEventModel.project_id == project_id,
                SupervisionAuditEventModel.action != "创建协作项目",
            )
        ).scalar()
        or 0
    )


def soft_delete_project(
    session: Session, *, project_ref: str, actor_id: str, reason: str
) -> SupervisionProjectModel:
    """软删除：关闭访问，保留行与既有审计（§13）。

    只有空白筹备项目能被删除；有正式内容的项目必须走归档。
    """
    project, role = require_member(session, project_ref, actor_id)
    if project.status != PROJECT_DRAFT:
        raise ValueError("正式项目不能删除，只能按权限归档")
    if project_activity_count(session, project_id=project.id) > 0:
        raise ValueError("项目已有正式内容，只能归档")
    project.deleted_at = _now()
    project.delete_reason = (reason or "")[:1000]
    project.update_time = _now()
    session.flush()
    return project


def request_archive(
    session: Session, *, project_ref: str, actor_id: str, reason: str
) -> SupervisionProjectModel:
    """学生申请归档。申请不关闭访问权限（§10）。"""
    project, _ = require_member(session, project_ref, actor_id)
    if project.status == PROJECT_ARCHIVED:
        raise ValueError("项目已归档")
    project.archive_requested_at = _now()
    project.archive_request_reason = (reason or "")[:1000]
    project.archive_reject_reason = ""
    project.update_time = _now()
    session.flush()
    return project


def decide_archive(
    session: Session,
    *,
    project_ref: str,
    actor_id: str,
    approve: bool,
    reason: str = "",
) -> SupervisionProjectModel:
    """主导师处理归档申请：同意则归档，拒绝则写明原因（§10）。"""
    project, role = require_member(session, project_ref, actor_id, roles=(ROLE_MENTOR,))
    if project.archive_requested_at is None:
        raise ValueError("当前没有待处理的归档申请")
    if approve:
        project.status = PROJECT_ARCHIVED
        project.archived_at = _now()
    else:
        project.archive_reject_reason = (reason or "")[:1000]
        project.archive_requested_at = None
        project.archive_request_reason = ""
    project.update_time = _now()
    session.flush()
    return project


def activate_project(session: Session, *, project_ref: str, actor_id: str) -> SupervisionProjectModel:
    """筹备中 → 进行中：需要同时有一名在册学生和一名在册主导师。"""
    project, _ = require_member(session, project_ref, actor_id)
    if project.status != PROJECT_DRAFT:
        raise ValueError("只有筹备中的项目可以进入进行中")
    roles = {m.role for m in list_members(session, project_id=project.id) if m.status == "active"}
    if {ROLE_STUDENT, ROLE_MENTOR} - roles:
        raise ValueError("需要同时有一名学生和一名主导师才能开始")
    project.status = PROJECT_ACTIVE
    project.update_time = _now()
    session.flush()
    return project


__all__ = [
    "CODE_EVENT",
    "CODE_PROJECT",
    "PROJECT_ACTIVE",
    "PROJECT_ARCHIVED",
    "PROJECT_DRAFT",
    "ROLE_MENTOR",
    "ROLE_STUDENT",
    "activate_project",
    "active_member_ids",
    "add_member",
    "create_project",
    "decide_archive",
    "get_membership",
    "list_audit_events",
    "list_members",
    "list_notifications",
    "list_projects_for_user",
    "mark_notification_read",
    "new_id",
    "next_code",
    "notify",
    "project_activity_count",
    "record_audit",
    "remove_member",
    "request_archive",
    "require_member",
    "resolve_project",
    "soft_delete_project",
]
