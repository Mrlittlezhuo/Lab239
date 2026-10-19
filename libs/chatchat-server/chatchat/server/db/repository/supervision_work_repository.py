"""师生协作业务层（里程碑1：纯人工闭环）。

四类对象分别保存状态，页面标签由它们派生（§5.6）：

- 任务 ``draft → active → completed/cancelled/archived``；提交、预审、审核
  都不推动任务状态，只有主导师发布"确认通过"才让任务 ``completed``；
- 正式提交 ``submitted → withdrawn/superseded``，版本只读；
- 导师审核 ``drafting → published``，发布后只能追加意见；
- 派生标签（待导师审核 / 审核草稿中 / 退回修改 / 等待重审 / 已通过 /
  已拒绝 / 审核暂缓）每次现算，不落库，避免三个状态互相污染。

审计与通知在业务写入的同一个事务里完成：一次业务动作只产生一个 ``EV-``
事件编号，审计一条、通知按接收者各一条（§12）。查看动作不写审计、不改
状态（§5.1）。
"""
from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from chatchat.server.db.models.supervision_model import SupervisionProjectModel
from chatchat.server.db.models.supervision_work_model import (
    CODE_ATTACHMENT,
    CODE_MILESTONE,
    CODE_PROGRESS,
    CODE_REVIEW,
    CODE_SUBMISSION,
    CODE_TASK,
    SupervisionAttachmentModel,
    SupervisionMilestoneModel,
    SupervisionProgressNoteModel,
    SupervisionProgressUpdateModel,
    SupervisionReviewAcModel,
    SupervisionReviewModel,
    SupervisionReviewNoteModel,
    SupervisionSubmissionModel,
    SupervisionTaskAcModel,
    SupervisionTaskModel,
)
from chatchat.server.db.repository.supervision_repository import (
    ROLE_MENTOR,
    ROLE_STUDENT,
    active_member_ids,
    new_id,
    next_code,
    notify,
    record_audit,
    require_member,
    resolve_project,
)

# 附件与解析白名单（AI预审方案 §5.5.1）。上传允许的范围比可解析范围宽：
# 二进制材料可以作为人工下载材料入库，但不能被当成"已解析"。
TEXT_EXTENSIONS = {
    ".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".toml", ".ini", ".log",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".c", ".cpp", ".h", ".m",
    ".tex", ".bib",
}
BINARY_ALLOWED_EXTENSIONS = {
    ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".zip", ".mat", ".npy",
    ".pkl", ".docx", ".xlsx",
}
DENIED_EXTENSIONS = {
    ".exe", ".dll", ".so", ".dylib", ".sh", ".bat", ".cmd", ".ps1", ".jar",
    ".docm", ".xlsm", ".pptm", ".apk", ".msi",
}
DEFAULT_MAX_UPLOAD_BYTES = 20 * 1024 * 1024

AC_STATEMENT_MIN_CHARS = 8  # 禁止"完成算法""效果较好"这类不可判断的标准


class WorkError(ValueError):
    """状态机或业务规则不满足；路由层收敛成 409。"""


class WorkValidationError(WorkError):
    """字段或前置条件不合法；路由层收敛成 400。"""


def _now() -> datetime:
    return datetime.now()


def max_upload_bytes() -> int:
    raw = os.environ.get("CHATCHAT_SUPERVISION_MAX_UPLOAD_BYTES")
    try:
        return int(raw) if raw else DEFAULT_MAX_UPLOAD_BYTES
    except ValueError:
        return DEFAULT_MAX_UPLOAD_BYTES


# ---------------------------------------------------------------------------
# 派生标签（§5.6）
# ---------------------------------------------------------------------------


def derive_task_label(
    task: SupervisionTaskModel,
    submissions: list[SupervisionSubmissionModel],
    reviews: list[SupervisionReviewModel],
    has_progress: bool = False,
) -> str:
    """把三套状态折算成页面上那一个标签。

    提交状态、预审状态和审核状态各自独立展示，这里只负责"当前该看哪一步"，
    不参与任何状态写入。
    """
    if task.status == "draft":
        return "任务草稿"
    if task.status == "cancelled":
        return "已取消"
    if task.status == "archived":
        return "已归档"

    valid = [s for s in submissions if s.status != "withdrawn"]
    if not valid:
        # "待开始"是 active 且尚无进展记录的派生标签，不是单独状态（§5.1）。
        return "进行中" if has_progress else "待开始"
    latest = max(valid, key=lambda s: (s.version, s.create_time or datetime.min))
    review = next((r for r in reviews if r.submission_id == latest.id), None)

    if review is None:
        # 最新有效提交还没有审核。若上一版已被退回且还没有新审核，就是"等待
        # 重审"；否则才是"待导师审核"（§5.6）。
        published_earlier = [
            r for r in reviews
            if r.status == "published" and r.submission_id != latest.id
        ]
        if published_earlier:
            newest_review = max(
                published_earlier, key=lambda r: r.published_at or datetime.min
            )
            if newest_review.overall_decision == "revision_required":
                return "等待重审"
        return "待导师审核"
    if review.status == "drafting":
        return "审核草稿中"
    if review.overall_decision == "approved":
        return "已通过"
    if review.overall_decision == "rejected":
        return "已拒绝"
    if review.overall_decision == "paused":
        return "审核暂缓"
    if review.overall_decision == "revision_required":
        return "退回修改"
    return "进行中"


def completeness_warnings(
    task: SupervisionTaskModel,
    acs: list[SupervisionTaskAcModel],
    self_assessment: list[dict],
    materials: list[dict],
    payload: dict,
) -> list[dict]:
    """提交前完整性检查：只提醒，不替代导师审核，也不阻塞提交（§6.4）。"""
    assessed = {str(item.get("ac_code", "")) for item in self_assessment}
    attachment_kinds = {str(item.get("kind", "")) for item in materials}
    checks: list[dict] = []

    missing_ac = [ac.ac_code for ac in acs if ac.ac_code not in assessed]
    checks.append({
        "key": "self_assessment",
        "passed": not missing_ac,
        "message": "已逐条填写自评" if not missing_ac
        else "以下验收标准还没有自评：" + "、".join(missing_ac),
    })
    required_materials = [
        m for m in (task.required_materials or []) if m.get("required")
    ]
    missing_materials = [
        m.get("label") or m.get("key")
        for m in required_materials
        if not any(
            str(item.get("kind", "")) == str(m.get("key", "")) for item in materials
        )
    ]
    checks.append({
        "key": "required_materials",
        "passed": not missing_materials,
        "message": "必填材料已关联" if not missing_materials
        else "仍缺少必填材料：" + "、".join(str(x) for x in missing_materials),
    })
    if task.requires_code_review:
        has_commit = bool((payload.get("git_commit") or "").strip())
        has_code_file = bool({"code", "diff", "archive"} & attachment_kinds)
        checks.append({
            "key": "code_material",
            "passed": has_commit and has_code_file,
            "message": "已填写 commit 并上传代码材料"
            if has_commit and has_code_file
            else "任务要求代码检查：需要填写 commit，并上传平台内的代码、差异或归档文件",
        })
    if task.requires_runtime:
        has_experiment = bool((payload.get("experiment_notes") or "").strip())
        has_output = bool({"log", "result", "report"} & attachment_kinds)
        checks.append({
            "key": "runtime_material",
            "passed": has_experiment and has_output,
            "message": "已填写实验配置与结果说明"
            if has_experiment and has_output
            else "任务要求实际运行：需要填写配置/命令/环境，并上传日志或结果文件",
        })
    checks.append({
        "key": "unfinished_items",
        "passed": bool((payload.get("unfinished_items") or "").strip()),
        "message": "已列出未完成项"
        if (payload.get("unfinished_items") or "").strip()
        else "请明确列出未完成项，导师按此核对范围",
    })
    return checks


# ---------------------------------------------------------------------------
# 阶段目标
# ---------------------------------------------------------------------------


def create_milestone(
    session: Session,
    *,
    project_ref: str,
    actor_id: str,
    title: str,
    description: str = "",
    success_criteria: str = "",
    planned_start: Optional[datetime] = None,
    planned_end: Optional[datetime] = None,
    owner_user_id: str = "",
) -> SupervisionMilestoneModel:
    project, role = require_member(session, project_ref, actor_id)
    if not title.strip():
        raise WorkValidationError("阶段目标必须有标题")
    now = _now()
    milestone = SupervisionMilestoneModel(
        id=new_id(),
        code=next_code(session, SupervisionMilestoneModel,
                       SupervisionMilestoneModel.code, CODE_MILESTONE),
        project_id=project.id,
        title=title.strip()[:200],
        description=description or "",
        success_criteria=success_criteria or "",
        planned_start=planned_start,
        planned_end=planned_end,
        status="planning",
        owner_user_id=owner_user_id or actor_id,
        create_time=now,
        update_time=now,
    )
    session.add(milestone)
    session.flush()
    record_audit(
        session, project_id=project.id, object_type="milestone",
        object_code=milestone.code, actor_id=actor_id, actor_role=role,
        action="创建阶段目标", detail=milestone.title,
    )
    session.flush()
    return milestone


def list_milestones(
    session: Session, *, project_ref: str, actor_id: str
) -> list[SupervisionMilestoneModel]:
    project, _ = require_member(session, project_ref, actor_id)
    return list(
        session.execute(
            select(SupervisionMilestoneModel)
            .where(SupervisionMilestoneModel.project_id == project.id)
            .order_by(SupervisionMilestoneModel.create_time)
        )
        .scalars()
        .all()
    )


def get_milestone(
    session: Session, *, project_ref: str, milestone_ref: str, actor_id: str
) -> SupervisionMilestoneModel:
    project, _ = require_member(session, project_ref, actor_id)
    milestone = session.execute(
        select(SupervisionMilestoneModel).where(
            SupervisionMilestoneModel.project_id == project.id,
            SupervisionMilestoneModel.id == milestone_ref,
        )
    ).scalar_one_or_none()
    if milestone is None:
        raise LookupError("阶段目标不存在")
    return milestone


def update_milestone(
    session: Session, *, project_ref: str, milestone_ref: str, actor_id: str,
    fields: dict[str, Any],
) -> SupervisionMilestoneModel:
    milestone = get_milestone(
        session, project_ref=project_ref, milestone_ref=milestone_ref, actor_id=actor_id
    )
    project, role = require_member(session, project_ref, actor_id)
    for key in ("title", "description", "success_criteria", "status"):
        if key in fields and fields[key] is not None:
            setattr(milestone, key, fields[key])
    for key in ("planned_start", "planned_end"):
        if key in fields:
            setattr(milestone, key, fields[key])
    if "owner_user_id" in fields and fields["owner_user_id"]:
        milestone.owner_user_id = fields["owner_user_id"]
    milestone.update_time = _now()
    record_audit(
        session, project_id=project.id, object_type="milestone",
        object_code=milestone.code, actor_id=actor_id, actor_role=role,
        action="修改阶段目标", detail=milestone.title,
    )
    session.flush()
    return milestone


def confirm_milestone(
    session: Session, *, project_ref: str, milestone_ref: str, actor_id: str,
    confirmation: str, note: str = "",
) -> SupervisionMilestoneModel:
    """导师对阶段目标的最终确认（§4.6）。"""
    if confirmation not in ("confirmed", "partially_confirmed", "not_confirmed"):
        raise WorkValidationError("确认结果只能是 confirmed / partially_confirmed / not_confirmed")
    milestone = get_milestone(
        session, project_ref=project_ref, milestone_ref=milestone_ref, actor_id=actor_id
    )
    project, role = require_member(session, project_ref, actor_id, roles=(ROLE_MENTOR,))
    milestone.mentor_confirmation = confirmation
    milestone.mentor_confirmation_note = note or ""
    milestone.mentor_confirmed_by = actor_id
    milestone.mentor_confirmed_at = _now()
    milestone.update_time = _now()
    event_code = record_audit(
        session, project_id=project.id, object_type="milestone",
        object_code=milestone.code, actor_id=actor_id, actor_role=role,
        action="确认阶段目标", detail=f"{confirmation}：{note or milestone.title}",
    )
    notify(
        session, event_code=event_code,
        recipients=[m for m in active_member_ids(session, project_id=project.id)
                    if m != actor_id],
        project_id=project.id, object_type="milestone", object_code=milestone.code,
        title=f"阶段目标 {milestone.code} 已确认", event_type="milestone.confirmed",
        body=note or f"主导师确认结果：{confirmation}",
    )
    session.flush()
    return milestone


# ---------------------------------------------------------------------------
# 任务与验收标准
# ---------------------------------------------------------------------------


def _validate_acs(acs: list[dict]) -> list[dict]:
    if not acs:
        raise WorkValidationError("任务至少要有一条验收标准")
    cleaned = []
    for index, ac in enumerate(acs, start=1):
        statement = (ac.get("statement") or "").strip()
        if len(statement) < AC_STATEMENT_MIN_CHARS:
            raise WorkValidationError(
                f"第 {index} 条验收标准过于笼统，要写成可核验的条件"
                "（例如\"固定随机种子下中位定位误差小于 1 m\"）"
            )
        check_type = ac.get("check_type") or "document"
        if check_type not in ("code", "experiment", "document", "analysis", "runtime"):
            raise WorkValidationError(f"第 {index} 条验收标准的检查类型不合法")
        cleaned.append({
            "ac_code": (ac.get("ac_code") or f"AC-{index}").strip(),
            "statement": statement,
            "evidence_requirement": (ac.get("evidence_requirement") or "").strip(),
            "required": bool(ac.get("required", True)),
            "check_type": check_type,
            "order_index": index,
        })
    return cleaned


def create_task(
    session: Session,
    *,
    project_ref: str,
    actor_id: str,
    title: str,
    purpose: str,
    description: str = "",
    acs: Optional[list[dict]] = None,
    required_materials: Optional[list[dict]] = None,
    due_at: Optional[datetime] = None,
    priority: str = "normal",
    requires_code_review: bool = False,
    requires_runtime: bool = False,
    milestone_id: str = "",
    publish: bool = False,
) -> SupervisionTaskModel:
    """创建任务。首期正式任务只由主导师下发（§15.1）。"""
    project, role = require_member(
        session, project_ref, actor_id, roles=(ROLE_MENTOR,)
    )
    if not title.strip():
        raise WorkValidationError("任务必须有标题")
    if not purpose.strip():
        raise WorkValidationError("任务必须写明为什么要做")
    if priority not in ("low", "normal", "high"):
        raise WorkValidationError("优先级只能是 low / normal / high")
    cleaned_acs = _validate_acs(acs or [])
    if milestone_id:
        get_milestone(
            session, project_ref=project.id, milestone_ref=milestone_id, actor_id=actor_id
        )

    now = _now()
    task = SupervisionTaskModel(
        id=new_id(),
        code=next_code(session, SupervisionTaskModel, SupervisionTaskModel.code, CODE_TASK),
        project_id=project.id,
        milestone_id=milestone_id or None,
        title=title.strip()[:200],
        purpose=purpose.strip(),
        description=description or "",
        required_materials=required_materials or [],
        due_at=due_at,
        priority=priority,
        requires_code_review=requires_code_review,
        requires_runtime=requires_runtime,
        status="draft",
        version=1,
        created_by=actor_id,
        created_role=role,
        create_time=now,
        update_time=now,
    )
    session.add(task)
    session.flush()
    for ac in cleaned_acs:
        session.add(SupervisionTaskAcModel(
            id=new_id(), task_id=task.id, version=1, create_time=now, **ac
        ))
    if publish:
        _publish_task(session, project, task, actor_id, role)
    else:
        record_audit(
            session, project_id=project.id, object_type="task", object_code=task.code,
            actor_id=actor_id, actor_role=role, action="创建任务草稿", detail=task.title,
        )
    session.flush()
    return task


def _publish_task(
    session: Session, project: SupervisionProjectModel, task: SupervisionTaskModel,
    actor_id: str, role: str,
) -> None:
    acs = list_task_acs(session, task_id=task.id)
    if not acs:
        raise WorkError("发布前至少要有一条验收标准")
    if task.status not in ("draft", "active"):
        raise WorkError(f"当前状态不能发布：{task.status}")
    task.status = "active"
    task.published_at = task.published_at or _now()
    task.update_time = _now()
    event_code = record_audit(
        session, project_id=project.id, object_type="task", object_code=task.code,
        actor_id=actor_id, actor_role=role, action="发布任务",
        detail=f"{task.title}（{len(acs)} 条验收标准）",
    )
    notify(
        session, event_code=event_code,
        recipients=active_member_ids(session, project_id=project.id, role=ROLE_STUDENT),
        project_id=project.id, object_type="task", object_code=task.code,
        title=f"新任务 {task.code}：{task.title}", event_type="task.published",
        body="导师已发布任务，请在\"执行与提交\"里查看验收标准。",
        level="warn" if task.priority == "high" else "info",
    )


def publish_task(
    session: Session, *, project_ref: str, task_ref: str, actor_id: str
) -> SupervisionTaskModel:
    task = get_task(session, project_ref=project_ref, task_ref=task_ref, actor_id=actor_id)
    project, role = require_member(
        session, project_ref, actor_id, roles=(ROLE_MENTOR,)
    )
    _publish_task(session, project, task, actor_id, role)
    session.flush()
    return task


def update_task(
    session: Session, *, project_ref: str, task_ref: str, actor_id: str,
    fields: dict[str, Any], acs: Optional[list[dict]] = None,
) -> SupervisionTaskModel:
    """修改任务。已发布任务改动会升版本并保留旧版验收标准（§7.2）。"""
    task = get_task(session, project_ref=project_ref, task_ref=task_ref, actor_id=actor_id)
    project, role = require_member(
        session, project_ref, actor_id, roles=(ROLE_MENTOR,)
    )
    if task.status in ("completed", "cancelled", "archived"):
        raise WorkError("已结束的任务不能修改，请创建后续任务")

    replaced_acs = False
    if acs is not None:
        cleaned = _validate_acs(acs)
        if cleaned != [
            {
                "ac_code": ac.ac_code, "statement": ac.statement,
                "evidence_requirement": ac.evidence_requirement,
                "required": ac.required, "check_type": ac.check_type,
                "order_index": ac.order_index,
            }
            for ac in list_task_acs(session, task_id=task.id)
        ]:
            replaced_acs = True

    for key in (
        "title", "purpose", "description", "priority", "requires_code_review",
        "requires_runtime", "required_materials", "milestone_id",
    ):
        if key in fields and fields[key] is not None:
            setattr(task, key, fields[key])
    if "due_at" in fields:
        task.due_at = fields["due_at"]

    detail = f"修改任务 {task.title}"
    if replaced_acs:
        task.version += 1
        now = _now()
        for ac in _validate_acs(acs):
            session.add(SupervisionTaskAcModel(
                id=new_id(), task_id=task.id, version=task.version, create_time=now, **ac
            ))
        submitted = session.execute(
            select(func.count(SupervisionSubmissionModel.id)).where(
                SupervisionSubmissionModel.task_id == task.id
            )
        ).scalar() or 0
        detail += (
            f"；验收标准升到 v{task.version}，已有 {submitted} 份提交仍绑定各自快照，"
            "不会被这次改动追溯改写"
        )
    task.update_time = _now()
    record_audit(
        session, project_id=project.id, object_type="task", object_code=task.code,
        actor_id=actor_id, actor_role=role,
        action="修改验收标准" if replaced_acs else "修改任务", detail=detail,
    )
    session.flush()
    return task


def cancel_task(
    session: Session, *, project_ref: str, task_ref: str, actor_id: str, reason: str
) -> SupervisionTaskModel:
    if not reason.strip():
        raise WorkValidationError("取消任务必须填写原因")
    task = get_task(session, project_ref=project_ref, task_ref=task_ref, actor_id=actor_id)
    project, role = require_member(
        session, project_ref, actor_id, roles=(ROLE_MENTOR,)
    )
    if task.status in ("completed", "cancelled", "archived"):
        raise WorkError("任务已经是终态")
    task.status = "cancelled"
    task.cancelled_at = _now()
    task.cancel_reason = reason
    task.update_time = _now()
    event_code = record_audit(
        session, project_id=project.id, object_type="task", object_code=task.code,
        actor_id=actor_id, actor_role=role, action="取消任务", detail=reason,
    )
    notify(
        session, event_code=event_code,
        recipients=active_member_ids(session, project_id=project.id, role=ROLE_STUDENT),
        project_id=project.id, object_type="task", object_code=task.code,
        title=f"任务 {task.code} 已取消", event_type="task.cancelled", body=reason,
    )
    session.flush()
    return task


def get_task(
    session: Session, *, project_ref: str, task_ref: str, actor_id: str
) -> SupervisionTaskModel:
    project, _ = require_member(session, project_ref, actor_id)
    task = session.execute(
        select(SupervisionTaskModel).where(
            SupervisionTaskModel.project_id == project.id,
            (SupervisionTaskModel.id == task_ref)
            | (SupervisionTaskModel.code == task_ref),
        )
    ).scalar_one_or_none()
    if task is None:
        raise LookupError("任务不存在")
    return task


def list_tasks(
    session: Session, *, project_ref: str, actor_id: str, status: str = ""
) -> list[SupervisionTaskModel]:
    project, _ = require_member(session, project_ref, actor_id)
    stmt = (
        select(SupervisionTaskModel)
        .where(SupervisionTaskModel.project_id == project.id)
        .order_by(SupervisionTaskModel.create_time.desc())
    )
    if status:
        stmt = stmt.where(SupervisionTaskModel.status == status)
    return list(session.execute(stmt).scalars().all())


def list_task_acs(
    session: Session, *, task_id: str, version: Optional[int] = None
) -> list[SupervisionTaskAcModel]:
    """默认取任务当前版本的验收标准。"""
    if version is None:
        version = int(session.execute(
            select(func.max(SupervisionTaskAcModel.version)).where(
                SupervisionTaskAcModel.task_id == task_id
            )
        ).scalar() or 1)
    return list(
        session.execute(
            select(SupervisionTaskAcModel)
            .where(
                SupervisionTaskAcModel.task_id == task_id,
                SupervisionTaskAcModel.version == version,
            )
            .order_by(SupervisionTaskAcModel.order_index)
        )
        .scalars().all()
    )


def task_label(
    session: Session, task: SupervisionTaskModel
) -> str:
    submissions = list(
        session.execute(
            select(SupervisionSubmissionModel).where(
                SupervisionSubmissionModel.task_id == task.id
            )
        ).scalars().all()
    )
    reviews = list(
        session.execute(
            select(SupervisionReviewModel).where(
                SupervisionReviewModel.task_id == task.id
            )
        ).scalars().all()
    )
    has_progress = bool(session.execute(
        select(func.count(SupervisionProgressUpdateModel.id)).where(
            SupervisionProgressUpdateModel.task_id == task.id
        )
    ).scalar())
    return derive_task_label(task, submissions, reviews, has_progress)


# ---------------------------------------------------------------------------
# 进展记录
# ---------------------------------------------------------------------------


def create_progress(
    session: Session, *, project_ref: str, actor_id: str,
    task_ref: str = "", milestone_ref: str = "",
    done: str = "", results: str = "", materials: str = "", problems: str = "",
    next_steps: str = "", links: Optional[list[dict]] = None,
) -> SupervisionProgressUpdateModel:
    """进展记录：不触发正式审核，也不会被当成正式提交（§4.8）。"""
    project, role = require_member(session, project_ref, actor_id)
    if not any((done, results, materials, problems, next_steps)):
        raise WorkValidationError("进展记录至少要写一项内容")
    task_id = None
    if task_ref:
        task = get_task(
            session, project_ref=project.id, task_ref=task_ref, actor_id=actor_id
        )
        task_id = task.id
    milestone_id = None
    if milestone_ref:
        milestone_id = get_milestone(
            session, project_ref=project.id, milestone_ref=milestone_ref,
            actor_id=actor_id,
        ).id

    progress = SupervisionProgressUpdateModel(
        id=new_id(),
        code=next_code(session, SupervisionProgressUpdateModel,
                       SupervisionProgressUpdateModel.code, CODE_PROGRESS),
        project_id=project.id,
        task_id=task_id,
        milestone_id=milestone_id,
        done=done or "",
        results=results or "",
        materials=materials or "",
        problems=problems or "",
        next_steps=next_steps or "",
        links=links or [],
        author_id=actor_id,
        author_role=role,
        create_time=_now(),
    )
    session.add(progress)
    session.flush()
    record_audit(
        session, project_id=project.id, object_type="progress",
        object_code=progress.code, actor_id=actor_id, actor_role=role,
        action="记录进展", detail=(done or results)[:200],
    )
    session.flush()
    return progress


def add_progress_note(
    session: Session, *, project_ref: str, progress_id: str, actor_id: str, note: str
) -> SupervisionProgressNoteModel:
    """补充说明只追加，原始进展不改写（§4.8）。"""
    if not note.strip():
        raise WorkValidationError("补充说明不能为空")
    project, role = require_member(session, project_ref, actor_id)
    progress = session.execute(
        select(SupervisionProgressUpdateModel).where(
            SupervisionProgressUpdateModel.id == progress_id,
            SupervisionProgressUpdateModel.project_id == project.id,
        )
    ).scalar_one_or_none()
    if progress is None:
        raise LookupError("进展记录不存在")
    row = SupervisionProgressNoteModel(
        id=new_id(), progress_id=progress.id, project_id=project.id,
        note=note, author_id=actor_id, author_role=role, create_time=_now(),
    )
    session.add(row)
    record_audit(
        session, project_id=project.id, object_type="progress",
        object_code=progress.code, actor_id=actor_id, actor_role=role,
        action="补充进展说明", detail=note[:200],
    )
    session.flush()
    return row


def list_progress(
    session: Session, *, project_ref: str, actor_id: str, task_ref: str = ""
) -> list[SupervisionProgressUpdateModel]:
    project, _ = require_member(session, project_ref, actor_id)
    stmt = (
        select(SupervisionProgressUpdateModel)
        .where(SupervisionProgressUpdateModel.project_id == project.id)
        .order_by(SupervisionProgressUpdateModel.create_time.desc())
    )
    if task_ref:
        task = get_task(
            session, project_ref=project.id, task_ref=task_ref, actor_id=actor_id
        )
        stmt = stmt.where(SupervisionProgressUpdateModel.task_id == task.id)
    return list(session.execute(stmt).scalars().all())


def list_progress_notes(
    session: Session, *, project_ref: str, progress_id: str, actor_id: str
) -> list[SupervisionProgressNoteModel]:
    project, _ = require_member(session, project_ref, actor_id)
    return list(
        session.execute(
            select(SupervisionProgressNoteModel)
            .where(
                SupervisionProgressNoteModel.progress_id == progress_id,
                SupervisionProgressNoteModel.project_id == project.id,
            )
            .order_by(SupervisionProgressNoteModel.create_time)
        )
        .scalars().all()
    )


# ---------------------------------------------------------------------------
# 正式提交
# ---------------------------------------------------------------------------


def create_submission(
    session: Session, *, project_ref: str, task_ref: str, actor_id: str,
    summary: str = "", self_assessment: Optional[list[dict]] = None,
    git_repo: str = "", git_branch: str = "", git_commit: str = "",
    experiment_notes: str = "", known_issues: str = "", unfinished_items: str = "",
    attachment_codes: Optional[list[str]] = None,
) -> SupervisionSubmissionModel:
    """学生正式提交：生成不可变版本快照（§4.9、§5.2）。"""
    project, role = require_member(
        session, project_ref, actor_id, roles=(ROLE_STUDENT,)
    )
    task = get_task(session, project_ref=project.id, task_ref=task_ref, actor_id=actor_id)
    if task.status != "active":
        raise WorkError("只有进行中的任务可以正式提交")

    acs = list_task_acs(session, task_id=task.id)
    if not acs:
        raise WorkError("任务没有验收标准，不能提交")

    attachments = _attachments_for_codes(
        session, project_id=project.id, codes=attachment_codes or []
    )
    materials = [
        {
            "attachment_code": a.code, "name": a.name, "sha256": a.sha256,
            "size": a.size, "kind": a.kind, "version": a.version,
        }
        for a in attachments
    ]
    assessment = _clean_self_assessment(self_assessment or [], acs)
    payload = {
        "git_commit": git_commit, "experiment_notes": experiment_notes,
        "unfinished_items": unfinished_items,
    }
    warnings = completeness_warnings(task, acs, assessment, materials, payload)

    now = _now()
    previous = list(
        session.execute(
            select(SupervisionSubmissionModel)
            .where(
                SupervisionSubmissionModel.task_id == task.id,
                SupervisionSubmissionModel.status == "submitted",
            )
        ).scalars().all()
    )
    version = int(session.execute(
        select(func.max(SupervisionSubmissionModel.version)).where(
            SupervisionSubmissionModel.task_id == task.id
        )
    ).scalar() or 0) + 1
    code = previous[0].code if previous else next_code(
        session, SupervisionSubmissionModel, SupervisionSubmissionModel.code,
        CODE_SUBMISSION,
    )
    for old in previous:
        old.status = "superseded"
        old.superseded_at = now
        old.superseded_by = ""

    submission = SupervisionSubmissionModel(
        id=new_id(),
        code=code,
        version=version,
        project_id=project.id,
        task_id=task.id,
        status="submitted",
        summary=summary or "",
        git_repo=git_repo or "",
        git_branch=git_branch or "",
        git_commit=git_commit or "",
        experiment_notes=experiment_notes or "",
        known_issues=known_issues or "",
        unfinished_items=unfinished_items or "",
        self_assessment=assessment,
        ac_snapshot=[
            {
                "ac_code": ac.ac_code, "statement": ac.statement,
                "evidence_requirement": ac.evidence_requirement,
                "required": ac.required, "check_type": ac.check_type,
                "version": ac.version,
            }
            for ac in acs
        ],
        materials=materials,
        completeness_check=warnings,
        author_id=actor_id,
        author_role=role,
        create_time=now,
    )
    session.add(submission)
    session.flush()

    display = f"{code}-v{version}"
    event_code = record_audit(
        session, project_id=project.id, object_type="submission",
        object_code=display, actor_id=actor_id, actor_role=role,
        action="正式提交", detail=f"{task.code} 第 {version} 版；"
        f"{len(materials)} 份材料",
    )
    notify(
        session, event_code=event_code,
        recipients=active_member_ids(session, project_id=project.id, role=ROLE_MENTOR),
        project_id=project.id, object_type="submission", object_code=display,
        title=f"{task.code} 有新提交 {display}", event_type="submission.created",
        body=summary[:200] or "学生已正式提交，等待审核。",
        level="warn",
    )
    session.flush()
    return submission


def _clean_self_assessment(assessment: list[dict], acs: list[SupervisionTaskAcModel]) -> list[dict]:
    known = {ac.ac_code for ac in acs}
    cleaned = []
    for item in assessment:
        ac_code = str(item.get("ac_code", "")).strip()
        if ac_code not in known:
            raise WorkValidationError(f"自评里的 {ac_code} 不属于本任务的验收标准")
        value = item.get("self_assessment") or ""
        if value not in ("done", "partially_done", "not_done", "not_applicable"):
            raise WorkValidationError(f"{ac_code} 的自评取值不合法")
        cleaned.append({
            "ac_code": ac_code,
            "self_assessment": value,
            "evidence": str(item.get("evidence") or "")[:2000],
            "note": str(item.get("note") or "")[:2000],
        })
    return cleaned


def _attachments_for_codes(
    session: Session, *, project_id: str, codes: list[str]
) -> list[SupervisionAttachmentModel]:
    if not codes:
        return []
    rows = list(
        session.execute(
            select(SupervisionAttachmentModel).where(
                SupervisionAttachmentModel.project_id == project_id,
                SupervisionAttachmentModel.code.in_(codes),
                SupervisionAttachmentModel.status == "active",
            )
        ).scalars().all()
    )
    found = {r.code for r in rows}
    missing = [c for c in codes if c not in found]
    if missing:
        raise WorkValidationError("以下附件不存在或已失效：" + "、".join(missing))
    return sorted(rows, key=lambda r: r.code)


def withdraw_submission(
    session: Session, *, project_ref: str, submission_ref: str, actor_id: str, reason: str
) -> SupervisionSubmissionModel:
    """撤回规则（§5.2）：只有提交学生本人、导师尚未发布正式审核前可以撤回。"""
    if not reason.strip():
        raise WorkValidationError("撤回必须填写原因")
    project, role = require_member(session, project_ref, actor_id)
    submission = get_submission(
        session, project_ref=project.id, submission_ref=submission_ref, actor_id=actor_id
    )
    if submission.author_id != actor_id:
        raise WorkError("只有提交本人可以撤回")
    if submission.status != "submitted":
        raise WorkError(f"当前提交状态不能撤回：{submission.status}")
    review = session.execute(
        select(SupervisionReviewModel).where(
            SupervisionReviewModel.submission_id == submission.id
        )
    ).scalar_one_or_none()
    if review is not None and review.status == "published":
        raise WorkError("已发布正式审核的提交不能撤回")

    submission.status = "withdrawn"
    submission.withdrawn_at = _now()
    submission.withdraw_reason = reason
    display = f"{submission.code}-v{submission.version}"
    event_code = record_audit(
        session, project_id=project.id, object_type="submission",
        object_code=display, actor_id=actor_id, actor_role=role,
        action="撤回提交",
        detail=f"{reason}；当时审核状态："
        f"{review.status if review else '未开始'}；快照继续只读保留",
    )
    notify(
        session,
        event_code=event_code,
        recipients=active_member_ids(session, project_id=project.id, role=ROLE_MENTOR),
        project_id=project.id, object_type="submission", object_code=display,
        title=f"{display} 已被撤回", event_type="submission.withdrawn", body=reason,
    )
    session.flush()
    return submission


_SUBMISSION_DISPLAY = re.compile(r"^(?P<code>[A-Z]+-\d+)-v(?P<version>\d+)$")


def get_submission(
    session: Session, *, project_ref: str, submission_ref: str, actor_id: str
) -> SupervisionSubmissionModel:
    """按内部 id、``S-001-v2`` 显示编号或 ``S-001``（取最新版本）定位提交。

    同一任务的各版本共用 code，所以只给 code 时必须明确取哪一版：这里取
    版本号最大的那一版，避免调用方误拿旧快照做审核。
    """
    project, _ = require_member(session, project_ref, actor_id)
    match = _SUBMISSION_DISPLAY.match(submission_ref)
    if match:
        row = session.execute(
            select(SupervisionSubmissionModel).where(
                SupervisionSubmissionModel.project_id == project.id,
                SupervisionSubmissionModel.code == match.group("code"),
                SupervisionSubmissionModel.version == int(match.group("version")),
            )
        ).scalar_one_or_none()
    else:
        row = session.execute(
            select(SupervisionSubmissionModel)
            .where(
                SupervisionSubmissionModel.project_id == project.id,
                (SupervisionSubmissionModel.id == submission_ref)
                | (SupervisionSubmissionModel.code == submission_ref),
            )
            .order_by(SupervisionSubmissionModel.version.desc())
            .limit(1)
        ).scalar_one_or_none()
    if row is None:
        raise LookupError("提交不存在")
    return row


def list_submissions(
    session: Session, *, project_ref: str, task_ref: str, actor_id: str
) -> list[SupervisionSubmissionModel]:
    project, _ = require_member(session, project_ref, actor_id)
    task = get_task(
        session, project_ref=project.id, task_ref=task_ref, actor_id=actor_id
    )
    return list(
        session.execute(
            select(SupervisionSubmissionModel)
            .where(SupervisionSubmissionModel.task_id == task.id)
            .order_by(SupervisionSubmissionModel.version.desc())
        )
        .scalars().all()
    )


def review_queue(
    session: Session, *, project_ref: str, actor_id: str
) -> list[SupervisionSubmissionModel]:
    """导师待审核队列：有效提交且没有已发布审核（§5.6）。"""
    project, _ = require_member(session, project_ref, actor_id)
    rows = list(
        session.execute(
            select(SupervisionSubmissionModel)
            .where(
                SupervisionSubmissionModel.project_id == project.id,
                SupervisionSubmissionModel.status == "submitted",
            )
            .order_by(SupervisionSubmissionModel.create_time)
        ).scalars().all()
    )
    pending = []
    for submission in rows:
        review = session.execute(
            select(SupervisionReviewModel).where(
                SupervisionReviewModel.submission_id == submission.id
            )
        ).scalar_one_or_none()
        if review is None or review.status != "published":
            pending.append(submission)
    return pending


# ---------------------------------------------------------------------------
# 导师审核
# ---------------------------------------------------------------------------


def get_review(
    session: Session, *, project_ref: str, submission_id: str, actor_id: str
) -> Optional[SupervisionReviewModel]:
    return session.execute(
        select(SupervisionReviewModel).where(
            SupervisionReviewModel.project_id == project_ref,
            SupervisionReviewModel.submission_id == submission_id,
        )
    ).scalar_one_or_none()


def get_review_for_reader(
    session: Session, *, project_ref: str, submission_id: str, actor_id: str
) -> Optional[SupervisionReviewModel]:
    """按读取者返回审核记录：**未发布的草稿只对写它的导师可见**。

    ``drafting`` 是导师的私人工作状态：逐条结论可能只填了一半、理由还是
    写给自己的备忘。把它端给学生等于替导师提前表态，也会让"退回修改"
    之类的派生标签在学生侧凭空出现（§5.4、§5.6）。

    对学生返回 ``None`` 而不是 404：提交本身可见，只是还没有已发布结论，
    界面据此显示"导师尚未给出结论"。
    """
    review = get_review(
        session, project_ref=project_ref, submission_id=submission_id, actor_id=actor_id
    )
    if review is None:
        return None
    if review.status != "published" and review.reviewer_id != actor_id:
        return None
    return review


def _ensure_draft(
    session: Session, project: SupervisionProjectModel, submission: SupervisionSubmissionModel,
    actor_id: str, role: str,
) -> SupervisionReviewModel:
    review = get_review(
        session, project_ref=project.id, submission_id=submission.id, actor_id=actor_id
    )
    if review is not None:
        if review.status == "published":
            raise WorkError("审核已发布，不能覆盖；请追加补充意见")
        return review
    review = SupervisionReviewModel(
        id=new_id(),
        code=next_code(session, SupervisionReviewModel, SupervisionReviewModel.code,
                       CODE_REVIEW),
        project_id=project.id,
        task_id=submission.task_id,
        submission_id=submission.id,
        status="drafting",
        reviewer_id=actor_id,
        reviewer_role=role,
        first_touched_at=_now(),
        create_time=_now(),
        update_time=_now(),
    )
    session.add(review)
    session.flush()
    return review


def _write_ac_conclusions(
    session: Session, review: SupervisionReviewModel, conclusions: list[dict],
    known_acs: set[str],
) -> None:
    for item in conclusions:
        ac_code = str(item.get("ac_code", "")).strip()
        if ac_code not in known_acs:
            raise WorkValidationError(f"{ac_code} 不属于本次提交快照里的验收标准")
        conclusion = item.get("conclusion") or ""
        if conclusion not in (
            "met", "partially_met", "not_met", "more_evidence_required", "not_applicable"
        ):
            raise WorkValidationError(f"{ac_code} 的导师结论取值不合法")
        if conclusion == "not_applicable" and not str(item.get("reason") or "").strip():
            raise WorkValidationError(f"{ac_code} 标记为不适用时必须填写理由")
        existing = session.execute(
            select(SupervisionReviewAcModel).where(
                SupervisionReviewAcModel.review_id == review.id,
                SupervisionReviewAcModel.ac_code == ac_code,
            )
        ).scalar_one_or_none()
        if existing is None:
            existing = SupervisionReviewAcModel(
                id=new_id(), review_id=review.id, project_id=review.project_id,
                ac_code=ac_code,
            )
            session.add(existing)
        existing.conclusion = conclusion
        existing.reason = str(item.get("reason") or "")
        existing.ai_relation = str(item.get("ai_relation") or "none")
        existing.updated_at = _now()
    session.flush()


def save_review_draft(
    session: Session, *, project_ref: str, submission_ref: str, actor_id: str,
    conclusions: Optional[list[dict]] = None, feedback: str = "",
    must_fix: Optional[list[str]] = None, suggestions: Optional[list[str]] = None,
    next_steps: str = "",
) -> SupervisionReviewModel:
    """保存审核草稿。首次保存才记录"首次处理时间"，只打开页面不算（§5.4）。"""
    project, role = require_member(
        session, project_ref, actor_id, roles=(ROLE_MENTOR,)
    )
    submission = get_submission(
        session, project_ref=project.id, submission_ref=submission_ref, actor_id=actor_id
    )
    if submission.status == "withdrawn":
        raise WorkError("提交已撤回，不在待审核队列里")
    review = _ensure_draft(session, project, submission, actor_id, role)
    known = {str(item["ac_code"]) for item in (submission.ac_snapshot or [])}
    if conclusions:
        _write_ac_conclusions(session, review, conclusions, known)
    if feedback:
        review.feedback = feedback
    if must_fix is not None:
        review.must_fix = [str(x) for x in must_fix]
    if suggestions is not None:
        review.suggestions = [str(x) for x in suggestions]
    if next_steps:
        review.next_steps = next_steps
    review.update_time = _now()
    record_audit(
        session, project_id=project.id, object_type="review", object_code=review.code,
        actor_id=actor_id, actor_role=role, action="保存审核草稿",
        detail=f"针对 {submission.code}-v{submission.version}",
    )
    session.flush()
    return review


def publish_review(
    session: Session, *, project_ref: str, submission_ref: str, actor_id: str,
    decision: str, conclusions: Optional[list[dict]] = None, feedback: str = "",
    must_fix: Optional[list[str]] = None, suggestions: Optional[list[str]] = None,
    next_steps: str = "", reason: str = "",
) -> SupervisionReviewModel:
    """发布导师结论（§5.4、§7.3）。"""
    if decision not in ("approved", "revision_required", "rejected", "paused"):
        raise WorkValidationError("总体结论只能是 approved / revision_required / rejected / paused")
    project, role = require_member(
        session, project_ref, actor_id, roles=(ROLE_MENTOR,)
    )
    submission = get_submission(
        session, project_ref=project.id, submission_ref=submission_ref, actor_id=actor_id
    )
    if submission.status == "withdrawn":
        raise WorkError("提交已撤回，不能发布审核结论")
    review = _ensure_draft(session, project, submission, actor_id, role)
    known = {str(item["ac_code"]) for item in (submission.ac_snapshot or [])}
    if conclusions:
        _write_ac_conclusions(session, review, conclusions, known)
    if feedback:
        review.feedback = feedback
    if must_fix is not None:
        review.must_fix = [str(x) for x in must_fix]
    if suggestions is not None:
        review.suggestions = [str(x) for x in suggestions]
    if next_steps:
        review.next_steps = next_steps

    saved = {
        row.ac_code: row for row in session.execute(
            select(SupervisionReviewAcModel).where(
                SupervisionReviewAcModel.review_id == review.id
            )
        ).scalars().all()
    }
    required_acs = [
        item for item in (submission.ac_snapshot or []) if item.get("required")
    ]
    if decision == "approved":
        missing = [
            item["ac_code"] for item in required_acs if item["ac_code"] not in saved
        ]
        if missing:
            raise WorkValidationError(
                "确认通过前，所有必选验收标准都要有导师结论；缺少："
                + "、".join(missing)
            )
    if decision == "revision_required" and not review.must_fix:
        raise WorkValidationError("要求修改时必须至少填写一条必须修改项")
    if decision in ("rejected", "paused") and not reason.strip():
        raise WorkValidationError("拒绝或暂缓审核必须填写原因")

    review.overall_decision = decision
    review.decision_reason = reason or ""
    review.status = "published"
    review.published_at = _now()
    review.update_time = _now()

    task = session.get(SupervisionTaskModel, submission.task_id)
    if task is not None:
        if decision == "approved":
            task.status = "completed"
            task.completed_at = _now()
        task.update_time = _now()

    display = f"{submission.code}-v{submission.version}"
    action_label = {
        "approved": "审核通过", "revision_required": "退回修改",
        "rejected": "拒绝提交", "paused": "暂缓审核",
    }[decision]
    event_code = record_audit(
        session, project_id=project.id, object_type="review", object_code=review.code,
        actor_id=actor_id, actor_role=role, action=action_label,
        detail=f"针对 {display}；{reason or feedback}"[:2000],
    )
    notify(
        session, event_code=event_code,
        recipients=active_member_ids(session, project_id=project.id, role=ROLE_STUDENT),
        project_id=project.id, object_type="review", object_code=review.code,
        title=f"{task.code if task else ''} 审核结果：{action_label}",
        event_type=f"review.{decision}",
        body=(feedback or reason or "见审核详情")[:2000],
        level="warn" if decision in ("revision_required", "rejected") else "info",
    )
    session.flush()
    return review


def add_review_note(
    session: Session, *, project_ref: str, review_ref: str, actor_id: str,
    note: str, kind: str = "amendment",
) -> SupervisionReviewNoteModel:
    """已发布审核只能追加意见或发起修订，不覆盖原结论（§4.11、§13）。"""
    if not note.strip():
        raise WorkValidationError("补充意见不能为空")
    if kind not in ("amendment", "clarification"):
        raise WorkValidationError("kind 只能是 amendment 或 clarification")
    project, role = require_member(
        session, project_ref, actor_id, roles=(ROLE_MENTOR,)
    )
    review = session.execute(
        select(SupervisionReviewModel).where(
            SupervisionReviewModel.project_id == project.id,
            (SupervisionReviewModel.id == review_ref)
            | (SupervisionReviewModel.code == review_ref),
        )
    ).scalar_one_or_none()
    if review is None:
        raise LookupError("审核记录不存在")
    if review.status != "published":
        raise WorkError("审核尚未发布，直接编辑草稿即可")
    row = SupervisionReviewNoteModel(
        id=new_id(), review_id=review.id, project_id=project.id, kind=kind,
        note=note, author_id=actor_id, author_role=role, create_time=_now(),
    )
    session.add(row)
    event_code = record_audit(
        session, project_id=project.id, object_type="review", object_code=review.code,
        actor_id=actor_id, actor_role=role,
        action="追加审核意见" if kind == "clarification" else "发起审核修订",
        detail=note[:2000],
    )
    notify(
        session, event_code=event_code,
        recipients=active_member_ids(session, project_id=project.id, role=ROLE_STUDENT),
        project_id=project.id, object_type="review", object_code=review.code,
        title=f"导师对 {review.code} 追加了意见", event_type="review.amended",
        body=note[:2000],
    )
    session.flush()
    return row


def get_review_by_ref(
    session: Session, *, project_ref: str, review_ref: str, actor_id: str
) -> SupervisionReviewModel:
    project, _ = require_member(session, project_ref, actor_id)
    review = session.execute(
        select(SupervisionReviewModel).where(
            SupervisionReviewModel.project_id == project.id,
            (SupervisionReviewModel.id == review_ref)
            | (SupervisionReviewModel.code == review_ref),
        )
    ).scalar_one_or_none()
    if review is None:
        raise LookupError("审核记录不存在")
    return review


def list_review_notes(
    session: Session, *, project_ref: str, review_ref: str, actor_id: str
) -> list[SupervisionReviewNoteModel]:
    review = get_review_by_ref(
        session, project_ref=project_ref, review_ref=review_ref, actor_id=actor_id
    )
    return list(
        session.execute(
            select(SupervisionReviewNoteModel)
            .where(SupervisionReviewNoteModel.review_id == review.id)
            .order_by(SupervisionReviewNoteModel.create_time)
        ).scalars().all()
    )


def list_review_acs(
    session: Session, *, review_id: str
) -> list[SupervisionReviewAcModel]:
    return list(
        session.execute(
            select(SupervisionReviewAcModel)
            .where(SupervisionReviewAcModel.review_id == review_id)
            .order_by(SupervisionReviewAcModel.ac_code)
        ).scalars().all()
    )


# ---------------------------------------------------------------------------
# 项目附件
# ---------------------------------------------------------------------------


def attachment_root() -> Path:
    """附件根目录。

    默认落在 ``DATA_PATH/supervision_attachments``；部署可以用
    ``CHATCHAT_SUPERVISION_ATTACHMENT_ROOT`` 指到独立卷（附件与数据库备份
    保留策略不同）。库里只存相对这个根的路径。
    """
    override = os.environ.get("CHATCHAT_SUPERVISION_ATTACHMENT_ROOT")
    if override:
        root = Path(override)
    else:
        from chatchat.settings import Settings

        root = Path(Settings.basic_settings.DATA_PATH) / "supervision_attachments"
    root.mkdir(parents=True, exist_ok=True)
    return root


def validate_upload_name(name: str) -> tuple[str, str]:
    """返回 (安全文件名, 小写扩展名)。拒绝穿越路径与危险类型。"""
    safe = os.path.basename((name or "").replace("\\", "/")).strip()
    if not safe or safe in (".", ".."):
        raise WorkValidationError("文件名不合法")
    if any(part in ("", "..") for part in safe.split("/")) or "/" in safe:
        raise WorkValidationError("文件名不合法")
    ext = Path(safe).suffix.lower()
    if ext in DENIED_EXTENSIONS:
        raise WorkValidationError(f"不允许上传该类型文件：{ext}")
    if ext not in TEXT_EXTENSIONS | BINARY_ALLOWED_EXTENSIONS:
        raise WorkValidationError(f"暂不支持的文件类型：{ext or '（无扩展名）'}")
    return safe, ext


def save_attachment(
    session: Session, *, project_ref: str, actor_id: str, filename: str,
    data: bytes, kind: str = "other", task_ref: str = "", submission_ref: str = "",
    progress_ref: str = "", mime_type: str = "",
) -> SupervisionAttachmentModel:
    """上传项目附件：项目权限 + 版本 + sha256 + 存储路径（§16.1 第3条）。"""
    project, role = require_member(session, project_ref, actor_id)
    safe_name, ext = validate_upload_name(filename)
    if len(data) > max_upload_bytes():
        raise WorkValidationError(
            f"文件超过上限 {max_upload_bytes()} 字节"
        )
    if not data:
        raise WorkValidationError("文件内容为空")

    task_id = None
    if task_ref:
        task_id = get_task(
            session, project_ref=project.id, task_ref=task_ref, actor_id=actor_id
        ).id
    submission_id = None
    if submission_ref:
        submission_id = get_submission(
            session, project_ref=project.id, submission_ref=submission_ref,
            actor_id=actor_id,
        ).id
    progress_id = None
    if progress_ref:
        progress_id = progress_ref

    digest = hashlib.sha256(data).hexdigest()
    attachment_id = new_id()
    relative = Path(project.id) / f"{attachment_id}{ext}"
    target = attachment_root() / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    # 先落盘再写库：库里那份永远指着一个已经存在、哈希对得上的文件。
    target.write_bytes(data)

    attachment = SupervisionAttachmentModel(
        id=attachment_id,
        code=next_code(session, SupervisionAttachmentModel,
                       SupervisionAttachmentModel.code, CODE_ATTACHMENT),
        project_id=project.id,
        task_id=task_id,
        submission_id=submission_id,
        progress_id=progress_id,
        name=safe_name[:300],
        ext=ext,
        mime_type=(mime_type or "")[:120],
        size=len(data),
        sha256=digest,
        stored_path=str(relative),
        version=1,
        kind=(kind or "other")[:32],
        status="active",
        uploaded_by=actor_id,
        uploaded_role=role,
        create_time=_now(),
    )
    session.add(attachment)
    record_audit(
        session, project_id=project.id, object_type="attachment",
        object_code=attachment.code, actor_id=actor_id, actor_role=role,
        action="上传附件", detail=f"{safe_name}（{len(data)} 字节，sha256 {digest[:12]}…）",
    )
    session.flush()
    return attachment


def get_attachment(
    session: Session, *, project_ref: str, attachment_ref: str, actor_id: str
) -> SupervisionAttachmentModel:
    project, _ = require_member(session, project_ref, actor_id)
    attachment = session.execute(
        select(SupervisionAttachmentModel).where(
            SupervisionAttachmentModel.project_id == project.id,
            (SupervisionAttachmentModel.id == attachment_ref)
            | (SupervisionAttachmentModel.code == attachment_ref),
        )
    ).scalar_one_or_none()
    if attachment is None or attachment.status == "removed":
        raise LookupError("附件不存在")
    return attachment


def attachment_path(attachment: SupervisionAttachmentModel) -> Path:
    root = attachment_root().resolve()
    target = (root / attachment.stored_path).resolve()
    # 双重保险：库里的路径必须仍在附件根目录内。
    if not str(target).startswith(str(root)):
        raise WorkError("附件路径不合法")
    if not target.is_file():
        raise LookupError("附件文件已丢失")
    return target


def list_attachments(
    session: Session, *, project_ref: str, actor_id: str, task_ref: str = ""
) -> list[SupervisionAttachmentModel]:
    project, _ = require_member(session, project_ref, actor_id)
    stmt = (
        select(SupervisionAttachmentModel)
        .where(
            SupervisionAttachmentModel.project_id == project.id,
            SupervisionAttachmentModel.status != "removed",
        )
        .order_by(SupervisionAttachmentModel.create_time.desc())
    )
    if task_ref:
        task = get_task(
            session, project_ref=project.id, task_ref=task_ref, actor_id=actor_id
        )
        stmt = stmt.where(SupervisionAttachmentModel.task_id == task.id)
    return list(session.execute(stmt).scalars().all())


def remove_attachment(
    session: Session, *, project_ref: str, attachment_ref: str, actor_id: str, reason: str
) -> SupervisionAttachmentModel:
    """软删除：已绑定正式提交的附件不能移除（提交快照不可变，§13）。"""
    if not reason.strip():
        raise WorkValidationError("移除附件必须填写原因")
    attachment = get_attachment(
        session, project_ref=project_ref, attachment_ref=attachment_ref, actor_id=actor_id
    )
    project, role = require_member(session, project_ref, actor_id)
    referenced = any(
        item.get("attachment_code") == attachment.code
        for row in session.execute(
            select(SupervisionSubmissionModel).where(
                SupervisionSubmissionModel.project_id == project.id
            )
        ).scalars().all()
        for item in (row.materials or [])
    )
    if referenced:
        raise WorkError("附件已被正式提交引用，不能移除；请提交新版本替换它")
    attachment.status = "removed"
    attachment.removed_at = _now()
    attachment.remove_reason = reason
    record_audit(
        session, project_id=project.id, object_type="attachment",
        object_code=attachment.code, actor_id=actor_id, actor_role=role,
        action="移除附件", detail=f"{attachment.name}：{reason}",
    )
    session.flush()
    return attachment


__all__ = [
    "BINARY_ALLOWED_EXTENSIONS",
    "DENIED_EXTENSIONS",
    "TEXT_EXTENSIONS",
    "WorkError",
    "WorkValidationError",
    "add_progress_note",
    "add_review_note",
    "attachment_path",
    "attachment_root",
    "cancel_task",
    "completeness_warnings",
    "confirm_milestone",
    "create_milestone",
    "create_progress",
    "create_submission",
    "create_task",
    "derive_task_label",
    "get_attachment",
    "get_milestone",
    "get_review",
    "get_review_for_reader",
    "get_review_by_ref",
    "get_submission",
    "get_task",
    "list_attachments",
    "list_milestones",
    "list_progress",
    "list_progress_notes",
    "list_review_acs",
    "list_review_notes",
    "list_submissions",
    "list_task_acs",
    "list_tasks",
    "max_upload_bytes",
    "publish_review",
    "publish_task",
    "remove_attachment",
    "review_queue",
    "save_attachment",
    "save_review_draft",
    "task_label",
    "update_milestone",
    "update_task",
    "validate_upload_name",
    "withdraw_submission",
]
