"""师生协作 API 路由（《学生研究与导师审核功能重新设计方案》里程碑0A）。

所有接口要求登录（``require_password_changed``），当前用户只取自服务端
认证上下文，**不接受请求体里的 user_id**。项目可见性只由成员关系决定：

- 非成员与不存在的项目统一 404，不区分原因，避免枚举项目是否存在；
- 需要特定项目角色的操作返回 403；
- 状态不允许的操作返回 409，字段非法返回 400。

0A 覆盖的对象与动作：

- ``POST /supervision/projects`` 新建协作项目（创建者作为学生入册）；
- ``GET  /supervision/projects`` 我的项目列表；
- ``GET  /supervision/projects/{pid}`` 项目详情与成员；
- ``POST /supervision/projects/{pid}/activate`` 筹备中 → 进行中；
- ``POST /supervision/projects/{pid}/members`` 加入成员（学生/主导师）；
- ``DELETE /supervision/projects/{pid}/members/{user_id}`` 移出成员；
- ``POST /supervision/projects/{pid}/archive/request`` 学生申请归档；
- ``POST /supervision/projects/{pid}/archive/decide`` 主导师同意/拒绝；
- ``DELETE /supervision/projects/{pid}`` 软删除（仅空白筹备项目）；
- ``GET  /supervision/projects/{pid}/audit`` 项目审计记录；
- ``GET  /supervision/notifications`` 我的通知；
- ``POST /supervision/notifications/read`` 标记单条或全部已读。

管理员查看不属于自己项目的记录时放行，但这次查看本身会另起一条审计
事件（§13）；管理员拿不到项目角色，因此不能据此执行学生或导师动作。
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from chatchat.server.auth.deps import require_password_changed
from chatchat.server.db.models.user_model import UserModel
from chatchat.server.db.repository import supervision_repository as repo
from chatchat.server.db.session import get_db

supervision_router = APIRouter(
    prefix="/supervision", tags=["Supervision - 师生科研协作"],
    dependencies=[Depends(require_password_changed)],
)


def _not_found(detail: str = "项目不存在或无权访问") -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _forbidden(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def _is_admin(user: UserModel) -> bool:
    return getattr(user, "role", "") == "admin"


def _resolve_or_404(
    session: Session, project_ref: str, user: UserModel
) -> tuple[object, Optional[str]]:
    resolved = repo.resolve_project(
        session, project_ref, user.id, is_admin=_is_admin(user)
    )
    if resolved is None:
        raise _not_found()
    return resolved


# ---------------------------------------------------------------------------
# 响应模型
# ---------------------------------------------------------------------------


class ProjectMemberOut(BaseModel):
    user_id: str
    role: str
    status: str
    joined_at: Optional[str] = None
    removed_at: Optional[str] = None


class ProjectOut(BaseModel):
    project_id: str
    code: str
    name: str
    description: str
    status: str
    my_role: Optional[str] = None
    archive_requested_at: Optional[str] = None
    archive_request_reason: str = ""
    archived_at: Optional[str] = None
    archive_reject_reason: str = ""
    create_time: Optional[str] = None
    update_time: Optional[str] = None


class ProjectDetailOut(ProjectOut):
    members: List[ProjectMemberOut] = Field(default_factory=list)


class AuditEventOut(BaseModel):
    event_code: str
    object_type: str
    object_code: str
    actor_id: str
    actor_role: str
    action: str
    detail: str
    created_at: Optional[str] = None


class NotificationOut(BaseModel):
    notification_id: str
    event_code: str
    event_type: str
    project_id: str
    object_type: str
    object_code: str
    title: str
    body: str
    level: str
    read: bool
    created_at: Optional[str] = None
    read_at: Optional[str] = None


class CreateProjectBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)


class AddMemberBody(BaseModel):
    user_id: str = Field(min_length=1, max_length=32)
    role: str = Field(pattern="^(student|mentor)$")


class ArchiveRequestBody(BaseModel):
    reason: str = Field(default="", max_length=1000)


class ArchiveDecideBody(BaseModel):
    approve: bool
    reason: str = Field(default="", max_length=1000)


class DeleteProjectBody(BaseModel):
    reason: str = Field(default="", max_length=1000)


class ReadNotificationsBody(BaseModel):
    notification_id: str = Field(default="", max_length=32)
    mark_all: bool = False


def _ts(value) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _project_out(project, role: Optional[str]) -> ProjectOut:
    return ProjectOut(
        project_id=project.id,
        code=project.code,
        name=project.name,
        description=project.description,
        status=project.status,
        my_role=role,
        archive_requested_at=_ts(project.archive_requested_at),
        archive_request_reason=project.archive_request_reason,
        archived_at=_ts(project.archived_at),
        archive_reject_reason=project.archive_reject_reason,
        create_time=_ts(project.create_time),
        update_time=_ts(project.update_time),
    )


# ---------------------------------------------------------------------------
# 项目
# ---------------------------------------------------------------------------


@supervision_router.post("/projects", response_model=ProjectDetailOut)
def create_project(
    body: CreateProjectBody = Body(...),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    """新建协作项目；创建者作为学生入册，状态为筹备中。"""
    project = repo.create_project(
        session, name=body.name.strip(), description=body.description, creator_id=user.id
    )
    event_code = repo.record_audit(
        session,
        project_id=project.id,
        object_type="project",
        object_code=project.code,
        actor_id=user.id,
        action="创建协作项目",
        detail=f"项目 {project.code} 已创建",
        actor_role=repo.ROLE_STUDENT,
    )
    repo.notify(
        session,
        event_code=event_code,
        recipients=repo.active_member_ids(session, project_id=project.id),
        project_id=project.id,
        title=f"研究项目 {project.code} 已创建",
        event_type="project.created",
        body=f"{project.name} 处于筹备中，需要一名主导师后才能开始。",
        object_type="project",
        object_code=project.code,
    )
    session.commit()
    return _project_detail_out(session, project, repo.ROLE_STUDENT)


@supervision_router.get("/projects", response_model=List[ProjectOut])
def list_projects(
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    rows = repo.list_projects_for_user(session, user.id)
    return [_project_out(project, role) for project, role in rows]


@supervision_router.get("/projects/{project_ref}", response_model=ProjectDetailOut)
def get_project(
    project_ref: str,
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    project, role = _resolve_or_404(session, project_ref, user)
    return _project_detail_out(session, project, role)


@supervision_router.post("/projects/{project_ref}/activate", response_model=ProjectDetailOut)
def activate_project(
    project_ref: str,
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    try:
        project, role = repo.require_member(session, project_ref, user.id)
    except PermissionError:
        raise _not_found()
    try:
        repo.activate_project(session, project_ref=project.id, actor_id=user.id)
    except ValueError as exc:
        raise _conflict(str(exc))
    session.commit()
    return _project_detail_out(session, project, role)


@supervision_router.post("/projects/{project_ref}/members", response_model=ProjectDetailOut)
def add_member(
    project_ref: str,
    body: AddMemberBody = Body(...),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    """加入成员。首期每项目只允许一名在册学生和一名在册主导师。"""
    try:
        project, _ = repo.require_member(session, project_ref, user.id)
    except PermissionError:
        raise _not_found()
    if body.user_id == user.id and body.role == repo.ROLE_STUDENT:
        raise _bad_request("创建者已经是本项目的学生")
    try:
        repo.add_member(
            session,
            project_ref=project.id,
            user_id=body.user_id,
            role=body.role,
            actor_id=user.id,
        )
    except Exception as exc:  # 唯一索引兜住"第二个学生/第二个主导师"
        session.rollback()
        raise _conflict(f"加入成员失败：{exc}")
    event_code = repo.record_audit(
        session,
        project_id=project.id,
        object_type="project",
        object_code=project.code,
        actor_id=user.id,
        action="加入成员",
        detail=f"{body.user_id} 以 {body.role} 加入",
    )
    repo.notify(
        session,
        event_code=event_code,
        recipients=[body.user_id],
        project_id=project.id,
        title=f"你已被加入研究项目 {project.code}",
        event_type="member.added",
        body=f"你的项目角色是 {body.role}。",
        object_type="project",
        object_code=project.code,
    )
    session.commit()
    mine = repo.get_membership(session, project.id, user.id)
    return _project_detail_out(session, project, mine.role if mine else None)


@supervision_router.delete("/projects/{project_ref}/members/{member_user_id}", response_model=ProjectDetailOut)
def remove_member(
    project_ref: str,
    member_user_id: str,
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    """移出成员：关闭后续访问，历史业务记录与已共享内容保留（§13）。"""
    try:
        project, role = repo.require_member(session, project_ref, user.id)
    except PermissionError:
        raise _not_found()
    # 学生不能把主导师换掉；被移出者本人可以退出。
    if member_user_id != user.id and role != repo.ROLE_MENTOR:
        raise _forbidden("只有主导师能移出其他成员")
    try:
        repo.remove_member(
            session, project_ref=project.id, user_id=member_user_id, actor_id=user.id
        )
    except LookupError as exc:
        raise _bad_request(str(exc))
    repo.record_audit(
        session,
        project_id=project.id,
        object_type="project",
        object_code=project.code,
        actor_id=user.id,
        action="移出成员",
        detail=f"{member_user_id} 已不再是本项目在册成员；历史记录保留",
    )
    session.commit()
    return _project_detail_out(session, project, role)


@supervision_router.post("/projects/{project_ref}/archive/request", response_model=ProjectDetailOut)
def request_archive(
    project_ref: str,
    body: ArchiveRequestBody = Body(...),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    """学生只能申请归档；申请不关闭访问权限，需主导师处理（§10）。"""
    try:
        project, _ = repo.require_member(session, project_ref, user.id)
    except PermissionError:
        raise _not_found()
    try:
        repo.request_archive(
            session, project_ref=project.id, actor_id=user.id, reason=body.reason
        )
    except ValueError as exc:
        raise _conflict(str(exc))
    event_code = repo.record_audit(
        session,
        project_id=project.id,
        object_type="project",
        object_code=project.code,
        actor_id=user.id,
        action="提交归档申请",
        detail=body.reason or "等待主导师处理",
    )
    repo.notify(
        session,
        event_code=event_code,
        recipients=repo.active_member_ids(
            session, project_id=project.id, role=repo.ROLE_MENTOR
        ),
        project_id=project.id,
        title=f"研究项目 {project.code} 申请归档",
        event_type="archive.requested",
        body=body.reason or "学生已申请归档，等待主导师处理。",
        object_type="project",
        object_code=project.code,
        level="warn",
    )
    session.commit()
    return _project_detail_out(session, project, repo.ROLE_STUDENT)


@supervision_router.post("/projects/{project_ref}/archive/decide", response_model=ProjectDetailOut)
def decide_archive(
    project_ref: str,
    body: ArchiveDecideBody = Body(...),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    try:
        project, _ = repo.require_member(
            session, project_ref, user.id, roles=(repo.ROLE_MENTOR,)
        )
    except PermissionError as exc:
        message = str(exc)
        if "成员" in message or "不存在" in message:
            raise _not_found()
        raise _forbidden(message)
    if not body.approve and not body.reason.strip():
        raise _bad_request("拒绝归档必须填写原因")
    try:
        repo.decide_archive(
            session,
            project_ref=project.id,
            actor_id=user.id,
            approve=body.approve,
            reason=body.reason,
        )
    except ValueError as exc:
        raise _conflict(str(exc))
    event_code = repo.record_audit(
        session,
        project_id=project.id,
        object_type="project",
        object_code=project.code,
        actor_id=user.id,
        action="同意归档" if body.approve else "拒绝归档申请",
        detail=body.reason or ("访问关闭，数据保留" if body.approve else ""),
    )
    repo.notify(
        session,
        event_code=event_code,
        recipients=repo.active_member_ids(
            session, project_id=project.id, role=repo.ROLE_STUDENT
        ),
        project_id=project.id,
        title=f"研究项目 {project.code} {'已归档' if body.approve else '归档申请被拒绝'}",
        event_type="archive.approved" if body.approve else "archive.rejected",
        body=body.reason or ("项目已归档，历史记录保留。" if body.approve else "归档申请被拒绝。"),
        object_type="project",
        object_code=project.code,
    )
    session.commit()
    return _project_detail_out(session, project, repo.ROLE_MENTOR)


@supervision_router.delete("/projects/{project_ref}", response_model=ProjectOut)
def delete_project(
    project_ref: str,
    body: DeleteProjectBody = Body(...),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    """软删除：只允许删除空白筹备项目，已有内容只能归档（§13、§15.1）。"""
    try:
        project, role = repo.require_member(session, project_ref, user.id)
    except PermissionError:
        raise _not_found()
    try:
        repo.soft_delete_project(
            session, project_ref=project.id, actor_id=user.id, reason=body.reason
        )
    except ValueError as exc:
        raise _conflict(str(exc))
    repo.record_audit(
        session,
        project_id=project.id,
        object_type="project",
        object_code=project.code,
        actor_id=user.id,
        action="项目软删除",
        detail="访问关闭；数据与审计记录保留",
    )
    session.commit()
    return _project_out(project, role)


# ---------------------------------------------------------------------------
# 审计与通知
# ---------------------------------------------------------------------------


@supervision_router.get("/projects/{project_ref}/audit", response_model=List[AuditEventOut])
def list_audit(
    project_ref: str,
    object_code: str = Query(default=""),
    action: str = Query(default=""),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    project, role = _resolve_or_404(session, project_ref, user)
    if role is None:
        # 管理员查看项目历史要再形成一条审计记录（§13）。
        repo.record_audit(
            session,
            project_id=project.id,
            object_type="project",
            object_code=project.code,
            actor_id=user.id,
            action="管理员查看项目审计",
            detail="管理员全局查看",
        )
        session.commit()
    events = repo.list_audit_events(
        session,
        project_id=project.id,
        viewer_id=user.id,
        viewer_role=role,
        object_code=object_code,
        action=action,
    )
    return [
        AuditEventOut(
            event_code=e.event_code,
            object_type=e.object_type,
            object_code=e.object_code,
            actor_id=e.actor_id,
            actor_role=e.actor_role,
            action=e.action,
            detail=e.detail,
            created_at=_ts(e.created_at),
        )
        for e in events
    ]


@supervision_router.get("/notifications", response_model=List[NotificationOut])
def list_notifications(
    unread_only: bool = Query(default=False),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    rows = repo.list_notifications(session, user_id=user.id, unread_only=unread_only)
    return [
        NotificationOut(
            notification_id=n.id,
            event_code=n.event_code,
            event_type=n.event_type,
            project_id=n.project_id,
            object_type=n.object_type,
            object_code=n.object_code,
            title=n.title,
            body=n.body,
            level=n.level,
            read=n.read_at is not None,
            created_at=_ts(n.created_at),
            read_at=_ts(n.read_at),
        )
        for n in rows
    ]


@supervision_router.post("/notifications/read")
def mark_read(
    body: ReadNotificationsBody = Body(...),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    """标记已读只改该用户自己的通知状态，不改变任何业务状态（§12）。"""
    if not body.mark_all and not body.notification_id:
        raise _bad_request("需要 notification_id 或 mark_all")
    try:
        updated = repo.mark_notification_read(
            session,
            user_id=user.id,
            notification_id=body.notification_id,
            mark_all=body.mark_all,
        )
    except ValueError as exc:
        raise _bad_request(str(exc))
    session.commit()
    return {"updated": updated}


def _project_detail_out(
    session: Session, project, role: Optional[str]
) -> ProjectDetailOut:
    detail = ProjectDetailOut(**_project_out(project, role).model_dump())
    detail.members = [
        ProjectMemberOut(
            user_id=m.user_id,
            role=m.role,
            status=m.status,
            joined_at=_ts(m.joined_at),
            removed_at=_ts(m.removed_at),
        )
        for m in repo.list_members(session, project_id=project.id)
    ]
    return detail


__all__ = ["supervision_router"]
