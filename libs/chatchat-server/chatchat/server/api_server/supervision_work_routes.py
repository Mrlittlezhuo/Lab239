"""师生协作业务接口（里程碑1：纯人工闭环）。

与 ``supervision_routes`` 同一个 ``/supervision`` 前缀，覆盖阶段目标、任务与
验收标准、进展记录、不可变正式提交、导师逐条审核、待审核队列与项目附件。

错误口径与 0A 一致：非成员与不存在统一 404，角色不足 403，状态不允许 409，
字段非法 400。当前用户只来自服务端认证上下文。

页面标签由 ``label`` 字段现算（§5.6），库里不存"待审核"这类混合状态；
查看类接口不写审计、不改任何状态（§5.1）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional

from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from chatchat.server.auth.deps import require_password_changed
from chatchat.server.db.models.supervision_work_model import (
    SupervisionAttachmentModel,
    SupervisionMilestoneModel,
    SupervisionProgressNoteModel,
    SupervisionProgressUpdateModel,
    SupervisionReviewModel,
    SupervisionReviewNoteModel,
    SupervisionSubmissionModel,
    SupervisionTaskModel,
)
from chatchat.server.db.models.user_model import UserModel
from chatchat.server.db.repository import supervision_work_repository as work
from chatchat.server.db.repository.supervision_repository import ROLE_MENTOR
from chatchat.server.db.session import get_db
from chatchat.utils import build_logger

logger = build_logger(__name__)

supervision_work_router = APIRouter(
    prefix="/supervision", tags=["Supervision - 研究任务与审核"],
    dependencies=[Depends(require_password_changed)],
)


def _not_found(detail: str = "资源不存在或无权访问") -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _forbidden(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def _ts(value) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise _bad_request(f"时间格式不合法：{value}")


def call(
    session: Session, action, *args, **kwargs
):
    """把仓储层异常收敛成统一 HTTP 语义，并在失败时回滚。"""
    try:
        result = action(session, *args, **kwargs)
        session.commit()
        return result
    except LookupError as exc:
        session.rollback()
        raise _not_found(str(exc) or "资源不存在或无权访问")
    except PermissionError as exc:
        session.rollback()
        message = str(exc)
        raise _forbidden(message) if "角色" in message else _not_found(message)
    except work.WorkValidationError as exc:
        session.rollback()
        raise _bad_request(str(exc))
    except work.WorkError as exc:
        session.rollback()
        raise _conflict(str(exc))


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------


class AcIn(BaseModel):
    ac_code: str = Field(default="", max_length=16)
    statement: str = Field(min_length=1, max_length=4000)
    evidence_requirement: str = Field(default="", max_length=4000)
    required: bool = True
    check_type: str = Field(default="document", max_length=16)


class MaterialIn(BaseModel):
    key: str = Field(min_length=1, max_length=32)
    label: str = Field(default="", max_length=100)
    required: bool = True


class MilestoneBody(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=8000)
    success_criteria: str = Field(default="", max_length=8000)
    planned_start: Optional[str] = None
    planned_end: Optional[str] = None
    owner_user_id: str = Field(default="", max_length=32)


class MilestonePatchBody(BaseModel):
    title: Optional[str] = Field(default=None, max_length=200)
    description: Optional[str] = Field(default=None, max_length=8000)
    success_criteria: Optional[str] = Field(default=None, max_length=8000)
    planned_start: Optional[str] = None
    planned_end: Optional[str] = None
    status: Optional[str] = Field(default=None, max_length=16)
    owner_user_id: Optional[str] = Field(default=None, max_length=32)


class MilestoneConfirmBody(BaseModel):
    confirmation: str = Field(min_length=1, max_length=32)
    note: str = Field(default="", max_length=4000)


class TaskBody(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    purpose: str = Field(min_length=1, max_length=8000)
    description: str = Field(default="", max_length=8000)
    acs: List[AcIn] = Field(default_factory=list)
    required_materials: List[MaterialIn] = Field(default_factory=list)
    due_at: Optional[str] = None
    priority: str = Field(default="normal", max_length=16)
    requires_code_review: bool = False
    requires_runtime: bool = False
    milestone_id: str = Field(default="", max_length=32)
    publish: bool = False


class TaskPatchBody(BaseModel):
    title: Optional[str] = Field(default=None, max_length=200)
    purpose: Optional[str] = Field(default=None, max_length=8000)
    description: Optional[str] = Field(default=None, max_length=8000)
    acs: Optional[List[AcIn]] = None
    required_materials: Optional[List[MaterialIn]] = None
    due_at: Optional[str] = None
    priority: Optional[str] = Field(default=None, max_length=16)
    requires_code_review: Optional[bool] = None
    requires_runtime: Optional[bool] = None
    milestone_id: Optional[str] = Field(default=None, max_length=32)


class ReasonBody(BaseModel):
    reason: str = Field(min_length=1, max_length=4000)


class ProgressBody(BaseModel):
    task_ref: str = Field(default="", max_length=32)
    milestone_ref: str = Field(default="", max_length=32)
    done: str = Field(default="", max_length=8000)
    results: str = Field(default="", max_length=8000)
    materials: str = Field(default="", max_length=8000)
    problems: str = Field(default="", max_length=8000)
    next_steps: str = Field(default="", max_length=8000)
    links: List[dict] = Field(default_factory=list)


class NoteBody(BaseModel):
    note: str = Field(min_length=1, max_length=8000)
    kind: str = Field(default="amendment", max_length=16)


class SelfAssessmentIn(BaseModel):
    ac_code: str = Field(min_length=1, max_length=16)
    self_assessment: str = Field(min_length=1, max_length=32)
    evidence: str = Field(default="", max_length=4000)
    note: str = Field(default="", max_length=4000)


class SubmissionBody(BaseModel):
    summary: str = Field(default="", max_length=8000)
    self_assessment: List[SelfAssessmentIn] = Field(default_factory=list)
    git_repo: str = Field(default="", max_length=500)
    git_branch: str = Field(default="", max_length=200)
    git_commit: str = Field(default="", max_length=200)
    experiment_notes: str = Field(default="", max_length=8000)
    known_issues: str = Field(default="", max_length=8000)
    unfinished_items: str = Field(default="", max_length=8000)
    attachment_codes: List[str] = Field(default_factory=list)


class AcConclusionIn(BaseModel):
    ac_code: str = Field(min_length=1, max_length=16)
    conclusion: str = Field(min_length=1, max_length=32)
    reason: str = Field(default="", max_length=4000)
    ai_relation: str = Field(default="none", max_length=16)


class ReviewDraftBody(BaseModel):
    conclusions: List[AcConclusionIn] = Field(default_factory=list)
    feedback: str = Field(default="", max_length=8000)
    must_fix: Optional[List[str]] = None
    suggestions: Optional[List[str]] = None
    next_steps: str = Field(default="", max_length=8000)


class ReviewPublishBody(ReviewDraftBody):
    decision: str = Field(min_length=1, max_length=32)
    reason: str = Field(default="", max_length=4000)


# ---------------------------------------------------------------------------
# 响应模型
# ---------------------------------------------------------------------------


class AcOut(BaseModel):
    ac_code: str
    statement: str
    evidence_requirement: str
    required: bool
    check_type: str
    version: int


class MilestoneOut(BaseModel):
    milestone_id: str
    code: str
    title: str
    description: str
    success_criteria: str
    planned_start: Optional[str] = None
    planned_end: Optional[str] = None
    status: str
    owner_user_id: str
    mentor_confirmation: str
    mentor_confirmation_note: str
    mentor_confirmed_at: Optional[str] = None
    create_time: Optional[str] = None


class TaskOut(BaseModel):
    task_id: str
    code: str
    title: str
    purpose: str
    description: str
    status: str
    label: str
    priority: str
    version: int
    due_at: Optional[str] = None
    requires_code_review: bool
    requires_runtime: bool
    required_materials: List[dict] = Field(default_factory=list)
    milestone_id: Optional[str] = None
    acs: List[AcOut] = Field(default_factory=list)
    published_at: Optional[str] = None
    cancel_reason: str = ""
    create_time: Optional[str] = None


class ProgressNoteOut(BaseModel):
    note_id: str
    note: str
    author_id: str
    author_role: str
    create_time: Optional[str] = None


class ProgressOut(BaseModel):
    progress_id: str
    code: str
    task_id: Optional[str] = None
    milestone_id: Optional[str] = None
    done: str
    results: str
    materials: str
    problems: str
    next_steps: str
    links: List[dict] = Field(default_factory=list)
    author_id: str
    author_role: str
    create_time: Optional[str] = None
    notes: List[ProgressNoteOut] = Field(default_factory=list)


class SubmissionOut(BaseModel):
    submission_id: str
    code: str
    display_code: str
    version: int
    task_id: str
    status: str
    summary: str
    git_repo: str
    git_branch: str
    git_commit: str
    experiment_notes: str
    known_issues: str
    unfinished_items: str
    self_assessment: List[dict] = Field(default_factory=list)
    ac_snapshot: List[dict] = Field(default_factory=list)
    materials: List[dict] = Field(default_factory=list)
    completeness_check: List[dict] = Field(default_factory=list)
    author_id: str
    create_time: Optional[str] = None
    withdrawn_at: Optional[str] = None
    withdraw_reason: str = ""


class ReviewAcOut(BaseModel):
    ac_code: str
    conclusion: str
    reason: str
    ai_relation: str


class ReviewOut(BaseModel):
    review_id: str
    code: str
    submission_id: str
    task_id: str
    status: str
    overall_decision: str
    decision_reason: str
    feedback: str
    must_fix: List[str] = Field(default_factory=list)
    suggestions: List[str] = Field(default_factory=list)
    next_steps: str
    reviewer_id: str
    first_touched_at: Optional[str] = None
    published_at: Optional[str] = None
    conclusions: List[ReviewAcOut] = Field(default_factory=list)
    notes: List[ProgressNoteOut] = Field(default_factory=list)


class AttachmentOut(BaseModel):
    attachment_id: str
    code: str
    name: str
    ext: str
    mime_type: str
    size: int
    sha256: str
    version: int
    kind: str
    status: str
    task_id: Optional[str] = None
    submission_id: Optional[str] = None
    uploaded_by: str
    create_time: Optional[str] = None


# ---------------------------------------------------------------------------
# 序列化
# ---------------------------------------------------------------------------


def _milestone_out(m: SupervisionMilestoneModel) -> MilestoneOut:
    return MilestoneOut(
        milestone_id=m.id, code=m.code, title=m.title, description=m.description,
        success_criteria=m.success_criteria, planned_start=_ts(m.planned_start),
        planned_end=_ts(m.planned_end), status=m.status, owner_user_id=m.owner_user_id,
        mentor_confirmation=m.mentor_confirmation,
        mentor_confirmation_note=m.mentor_confirmation_note,
        mentor_confirmed_at=_ts(m.mentor_confirmed_at), create_time=_ts(m.create_time),
    )


def _task_out(session: Session, t: SupervisionTaskModel) -> TaskOut:
    return TaskOut(
        task_id=t.id, code=t.code, title=t.title, purpose=t.purpose,
        description=t.description, status=t.status, label=work.task_label(session, t),
        priority=t.priority, version=t.version, due_at=_ts(t.due_at),
        requires_code_review=t.requires_code_review,
        requires_runtime=t.requires_runtime,
        required_materials=list(t.required_materials or []),
        milestone_id=t.milestone_id, published_at=_ts(t.published_at),
        cancel_reason=t.cancel_reason, create_time=_ts(t.create_time),
        acs=[
            AcOut(
                ac_code=ac.ac_code, statement=ac.statement,
                evidence_requirement=ac.evidence_requirement, required=ac.required,
                check_type=ac.check_type, version=ac.version,
            )
            for ac in work.list_task_acs(session, task_id=t.id)
        ],
    )


def _submission_out(s: SupervisionSubmissionModel) -> SubmissionOut:
    return SubmissionOut(
        submission_id=s.id, code=s.code, display_code=f"{s.code}-v{s.version}",
        version=s.version, task_id=s.task_id, status=s.status, summary=s.summary,
        git_repo=s.git_repo, git_branch=s.git_branch, git_commit=s.git_commit,
        experiment_notes=s.experiment_notes, known_issues=s.known_issues,
        unfinished_items=s.unfinished_items,
        self_assessment=list(s.self_assessment or []),
        ac_snapshot=list(s.ac_snapshot or []),
        materials=list(s.materials or []),
        completeness_check=list(s.completeness_check or []),
        author_id=s.author_id, create_time=_ts(s.create_time),
        withdrawn_at=_ts(s.withdrawn_at), withdraw_reason=s.withdraw_reason,
    )


def _review_out(
    session: Session, r: SupervisionReviewModel, project_ref: str, actor_id: str
) -> ReviewOut:
    return ReviewOut(
        review_id=r.id, code=r.code, submission_id=r.submission_id, task_id=r.task_id,
        status=r.status, overall_decision=r.overall_decision,
        decision_reason=r.decision_reason, feedback=r.feedback,
        must_fix=list(r.must_fix or []), suggestions=list(r.suggestions or []),
        next_steps=r.next_steps, reviewer_id=r.reviewer_id,
        first_touched_at=_ts(r.first_touched_at), published_at=_ts(r.published_at),
        conclusions=[
            ReviewAcOut(
                ac_code=row.ac_code, conclusion=row.conclusion, reason=row.reason,
                ai_relation=row.ai_relation,
            )
            for row in work.list_review_acs(session, review_id=r.id)
        ],
        notes=[
            ProgressNoteOut(
                note_id=n.id, note=n.note, author_id=n.author_id,
                author_role=n.author_role, create_time=_ts(n.create_time),
            )
            for n in work.list_review_notes(
                session, project_ref=project_ref, review_ref=r.id, actor_id=actor_id
            )
        ],
    )


def _progress_out(
    session: Session,
    p: SupervisionProgressUpdateModel,
    project_ref: str,
    actor_id: str,
) -> ProgressOut:
    return ProgressOut(
        progress_id=p.id, code=p.code, task_id=p.task_id, milestone_id=p.milestone_id,
        done=p.done, results=p.results, materials=p.materials, problems=p.problems,
        next_steps=p.next_steps, links=list(p.links or []), author_id=p.author_id,
        author_role=p.author_role, create_time=_ts(p.create_time),
        notes=[
            ProgressNoteOut(
                note_id=n.id, note=n.note, author_id=n.author_id,
                author_role=n.author_role, create_time=_ts(n.create_time),
            )
            for n in work.list_progress_notes(
                session, project_ref=project_ref, progress_id=p.id, actor_id=actor_id,
            )
        ],
    )


def _attachment_out(a: SupervisionAttachmentModel) -> AttachmentOut:
    return AttachmentOut(
        attachment_id=a.id, code=a.code, name=a.name, ext=a.ext,
        mime_type=a.mime_type, size=a.size, sha256=a.sha256, version=a.version,
        kind=a.kind, status=a.status, task_id=a.task_id,
        submission_id=a.submission_id, uploaded_by=a.uploaded_by,
        create_time=_ts(a.create_time),
    )


# ---------------------------------------------------------------------------
# 阶段目标
# ---------------------------------------------------------------------------


@supervision_work_router.post(
    "/projects/{project_ref}/milestones", response_model=MilestoneOut
)
def create_milestone(
    project_ref: str,
    body: MilestoneBody = Body(...),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    milestone = call(
        session, work.create_milestone, project_ref=project_ref, actor_id=user.id,
        title=body.title, description=body.description,
        success_criteria=body.success_criteria,
        planned_start=_parse_dt(body.planned_start),
        planned_end=_parse_dt(body.planned_end), owner_user_id=body.owner_user_id,
    )
    return _milestone_out(milestone)


@supervision_work_router.get(
    "/projects/{project_ref}/milestones", response_model=List[MilestoneOut]
)
def list_milestones(
    project_ref: str,
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    rows = call(session, work.list_milestones, project_ref=project_ref, actor_id=user.id)
    return [_milestone_out(m) for m in rows]


@supervision_work_router.patch(
    "/projects/{project_ref}/milestones/{milestone_ref}", response_model=MilestoneOut
)
def update_milestone(
    project_ref: str,
    milestone_ref: str,
    body: MilestonePatchBody = Body(...),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if "planned_start" in fields:
        fields["planned_start"] = _parse_dt(fields["planned_start"])
    if "planned_end" in fields:
        fields["planned_end"] = _parse_dt(fields["planned_end"])
    milestone = call(
        session, work.update_milestone, project_ref=project_ref,
        milestone_ref=milestone_ref, actor_id=user.id, fields=fields,
    )
    return _milestone_out(milestone)


@supervision_work_router.post(
    "/projects/{project_ref}/milestones/{milestone_ref}/confirm",
    response_model=MilestoneOut,
)
def confirm_milestone(
    project_ref: str,
    milestone_ref: str,
    body: MilestoneConfirmBody = Body(...),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    milestone = call(
        session, work.confirm_milestone, project_ref=project_ref,
        milestone_ref=milestone_ref, actor_id=user.id,
        confirmation=body.confirmation, note=body.note,
    )
    return _milestone_out(milestone)


# ---------------------------------------------------------------------------
# 任务与验收标准
# ---------------------------------------------------------------------------


@supervision_work_router.post("/projects/{project_ref}/tasks", response_model=TaskOut)
def create_task(
    project_ref: str,
    body: TaskBody = Body(...),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    task = call(
        session, work.create_task, project_ref=project_ref, actor_id=user.id,
        title=body.title, purpose=body.purpose, description=body.description,
        acs=[ac.model_dump() for ac in body.acs],
        required_materials=[m.model_dump() for m in body.required_materials],
        due_at=_parse_dt(body.due_at), priority=body.priority,
        requires_code_review=body.requires_code_review,
        requires_runtime=body.requires_runtime, milestone_id=body.milestone_id,
        publish=body.publish,
    )
    return _task_out(session, task)


@supervision_work_router.get(
    "/projects/{project_ref}/tasks", response_model=List[TaskOut]
)
def list_tasks(
    project_ref: str,
    status_filter: str = Query(default="", alias="status"),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    rows = call(
        session, work.list_tasks, project_ref=project_ref, actor_id=user.id,
        status=status_filter,
    )
    return [_task_out(session, t) for t in rows]


@supervision_work_router.get(
    "/projects/{project_ref}/tasks/{task_ref}", response_model=TaskOut
)
def get_task(
    project_ref: str,
    task_ref: str,
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    task = call(
        session, work.get_task, project_ref=project_ref, task_ref=task_ref,
        actor_id=user.id,
    )
    return _task_out(session, task)


@supervision_work_router.post(
    "/projects/{project_ref}/tasks/{task_ref}/publish", response_model=TaskOut
)
def publish_task(
    project_ref: str,
    task_ref: str,
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    task = call(
        session, work.publish_task, project_ref=project_ref, task_ref=task_ref,
        actor_id=user.id,
    )
    return _task_out(session, task)


@supervision_work_router.patch(
    "/projects/{project_ref}/tasks/{task_ref}", response_model=TaskOut
)
def update_task(
    project_ref: str,
    task_ref: str,
    body: TaskPatchBody = Body(...),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    fields = {
        k: v for k, v in body.model_dump().items()
        if k != "acs" and v is not None
    }
    if "due_at" in fields:
        fields["due_at"] = _parse_dt(fields["due_at"])
    acs = None if body.acs is None else [ac.model_dump() for ac in body.acs]
    task = call(
        session, work.update_task, project_ref=project_ref, task_ref=task_ref,
        actor_id=user.id, fields=fields, acs=acs,
    )
    return _task_out(session, task)


@supervision_work_router.post(
    "/projects/{project_ref}/tasks/{task_ref}/cancel", response_model=TaskOut
)
def cancel_task(
    project_ref: str,
    task_ref: str,
    body: ReasonBody = Body(...),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    task = call(
        session, work.cancel_task, project_ref=project_ref, task_ref=task_ref,
        actor_id=user.id, reason=body.reason,
    )
    return _task_out(session, task)


# ---------------------------------------------------------------------------
# 进展记录
# ---------------------------------------------------------------------------


@supervision_work_router.post(
    "/projects/{project_ref}/progress", response_model=ProgressOut
)
def create_progress(
    project_ref: str,
    body: ProgressBody = Body(...),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    progress = call(
        session, work.create_progress, project_ref=project_ref, actor_id=user.id,
        task_ref=body.task_ref, milestone_ref=body.milestone_ref, done=body.done,
        results=body.results, materials=body.materials, problems=body.problems,
        next_steps=body.next_steps, links=body.links,
    )
    return _progress_out(session, progress, project_ref, user.id)


@supervision_work_router.get(
    "/projects/{project_ref}/progress", response_model=List[ProgressOut]
)
def list_progress(
    project_ref: str,
    task_ref: str = Query(default=""),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    rows = call(
        session, work.list_progress, project_ref=project_ref, actor_id=user.id,
        task_ref=task_ref,
    )
    return [_progress_out(session, p, project_ref, user.id) for p in rows]


@supervision_work_router.post(
    "/projects/{project_ref}/progress/{progress_id}/notes", response_model=ProgressOut
)
def add_progress_note(
    project_ref: str,
    progress_id: str,
    body: NoteBody = Body(...),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    call(
        session, work.add_progress_note, project_ref=project_ref,
        progress_id=progress_id, actor_id=user.id, note=body.note,
    )
    progress = session.get(SupervisionProgressUpdateModel, progress_id)
    return _progress_out(session, progress, project_ref, user.id)


# ---------------------------------------------------------------------------
# 正式提交
# ---------------------------------------------------------------------------


@supervision_work_router.post(
    "/projects/{project_ref}/tasks/{task_ref}/submissions", response_model=SubmissionOut
)
def create_submission(
    project_ref: str,
    task_ref: str,
    body: SubmissionBody = Body(...),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    submission = call(
        session, work.create_submission, project_ref=project_ref, task_ref=task_ref,
        actor_id=user.id, summary=body.summary,
        self_assessment=[a.model_dump() for a in body.self_assessment],
        git_repo=body.git_repo, git_branch=body.git_branch,
        git_commit=body.git_commit, experiment_notes=body.experiment_notes,
        known_issues=body.known_issues, unfinished_items=body.unfinished_items,
        attachment_codes=body.attachment_codes,
    )
    # 提交后自动预审（AI预审方案 §7.2）：开关默认关闭，打开时也只创建 queued
    # 运行并交给后台线程，模型调用按 AC 批次经过统一容量闸门。这里任何失败都
    # 只写日志，绝不回滚或改写已经成功的提交（P-06、P-11）。
    try:
        from chatchat.server import ai_review_runtime

        ai_review_runtime.queue_auto_review(
            project_id=submission.project_id,
            submission_id=submission.id,
            actor_id=user.id,
        )
    except Exception:
        logger.warning("自动预审创建失败，提交不受影响", exc_info=True)
    return _submission_out(submission)


@supervision_work_router.get(
    "/projects/{project_ref}/tasks/{task_ref}/submissions",
    response_model=List[SubmissionOut],
)
def list_submissions(
    project_ref: str,
    task_ref: str,
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    rows = call(
        session, work.list_submissions, project_ref=project_ref, task_ref=task_ref,
        actor_id=user.id,
    )
    return [_submission_out(s) for s in rows]


@supervision_work_router.get(
    "/projects/{project_ref}/submissions/{submission_ref}", response_model=SubmissionOut
)
def get_submission(
    project_ref: str,
    submission_ref: str,
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    return _submission_out(
        call(session, work.get_submission, project_ref=project_ref,
             submission_ref=submission_ref, actor_id=user.id)
    )


@supervision_work_router.post(
    "/projects/{project_ref}/submissions/{submission_ref}/withdraw",
    response_model=SubmissionOut,
)
def withdraw_submission(
    project_ref: str,
    submission_ref: str,
    body: ReasonBody = Body(...),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    return _submission_out(
        call(session, work.withdraw_submission, project_ref=project_ref,
             submission_ref=submission_ref, actor_id=user.id, reason=body.reason)
    )


@supervision_work_router.get(
    "/projects/{project_ref}/review-queue", response_model=List[SubmissionOut]
)
def review_queue(
    project_ref: str,
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    rows = call(session, work.review_queue, project_ref=project_ref, actor_id=user.id)
    return [_submission_out(s) for s in rows]


# ---------------------------------------------------------------------------
# 导师审核
# ---------------------------------------------------------------------------


@supervision_work_router.get(
    "/projects/{project_ref}/submissions/{submission_ref}/review",
    response_model=Optional[ReviewOut],
)
def get_review(
    project_ref: str,
    submission_ref: str,
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    submission = call(
        session, work.get_submission, project_ref=project_ref,
        submission_ref=submission_ref, actor_id=user.id,
    )
    review = work.get_review(
        session, project_ref=submission.project_id, submission_id=submission.id,
        actor_id=user.id,
    )
    session.commit()
    if review is None:
        return None
    return _review_out(session, review, submission.project_id, user.id)


@supervision_work_router.post(
    "/projects/{project_ref}/submissions/{submission_ref}/review/draft",
    response_model=ReviewOut,
)
def save_review_draft(
    project_ref: str,
    submission_ref: str,
    body: ReviewDraftBody = Body(...),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    review = call(
        session, work.save_review_draft, project_ref=project_ref,
        submission_ref=submission_ref, actor_id=user.id,
        conclusions=[c.model_dump() for c in body.conclusions], feedback=body.feedback,
        must_fix=body.must_fix, suggestions=body.suggestions,
        next_steps=body.next_steps,
    )
    return _review_out(session, review, review.project_id, user.id)


@supervision_work_router.post(
    "/projects/{project_ref}/submissions/{submission_ref}/review/publish",
    response_model=ReviewOut,
)
def publish_review(
    project_ref: str,
    submission_ref: str,
    body: ReviewPublishBody = Body(...),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    review = call(
        session, work.publish_review, project_ref=project_ref,
        submission_ref=submission_ref, actor_id=user.id, decision=body.decision,
        conclusions=[c.model_dump() for c in body.conclusions], feedback=body.feedback,
        must_fix=body.must_fix, suggestions=body.suggestions,
        next_steps=body.next_steps, reason=body.reason,
    )
    return _review_out(session, review, review.project_id, user.id)


@supervision_work_router.post(
    "/projects/{project_ref}/reviews/{review_ref}/notes", response_model=ReviewOut
)
def add_review_note(
    project_ref: str,
    review_ref: str,
    body: NoteBody = Body(...),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    call(
        session, work.add_review_note, project_ref=project_ref, review_ref=review_ref,
        actor_id=user.id, note=body.note, kind=body.kind,
    )
    review = call(
        session, work.get_review_by_ref, project_ref=project_ref,
        review_ref=review_ref, actor_id=user.id,
    )
    return _review_out(session, review, review.project_id, user.id)


# ---------------------------------------------------------------------------
# 项目附件
# ---------------------------------------------------------------------------


@supervision_work_router.post(
    "/projects/{project_ref}/attachments", response_model=AttachmentOut
)
def upload_attachment(
    project_ref: str,
    file: UploadFile = File(...),
    kind: str = Form(default="other"),
    task_ref: str = Form(default=""),
    submission_ref: str = Form(default=""),
    progress_ref: str = Form(default=""),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    data = file.file.read()
    attachment = call(
        session, work.save_attachment, project_ref=project_ref, actor_id=user.id,
        filename=file.filename or "", data=data, kind=kind, task_ref=task_ref,
        submission_ref=submission_ref, progress_ref=progress_ref,
        mime_type=file.content_type or "",
    )
    return _attachment_out(attachment)


@supervision_work_router.get(
    "/projects/{project_ref}/attachments", response_model=List[AttachmentOut]
)
def list_attachments(
    project_ref: str,
    task_ref: str = Query(default=""),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    rows = call(
        session, work.list_attachments, project_ref=project_ref, actor_id=user.id,
        task_ref=task_ref,
    )
    return [_attachment_out(a) for a in rows]


@supervision_work_router.get(
    "/projects/{project_ref}/attachments/{attachment_ref}", response_model=AttachmentOut
)
def get_attachment(
    project_ref: str,
    attachment_ref: str,
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    return _attachment_out(
        call(session, work.get_attachment, project_ref=project_ref,
             attachment_ref=attachment_ref, actor_id=user.id)
    )


@supervision_work_router.get("/projects/{project_ref}/attachments/{attachment_ref}/download")
def download_attachment(
    project_ref: str,
    attachment_ref: str,
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    """下载鉴权：只有当前项目成员能取文件本体（§16.1 第3条）。"""
    attachment = call(
        session, work.get_attachment, project_ref=project_ref,
        attachment_ref=attachment_ref, actor_id=user.id,
    )
    try:
        path = work.attachment_path(attachment)
    except LookupError:
        raise _not_found("附件文件已丢失")
    except work.WorkError as exc:
        session.rollback()
        raise _conflict(str(exc))
    return FileResponse(
        path=str(path), filename=attachment.name,
        media_type=attachment.mime_type or "application/octet-stream",
    )


@supervision_work_router.delete(
    "/projects/{project_ref}/attachments/{attachment_ref}", response_model=AttachmentOut
)
def remove_attachment(
    project_ref: str,
    attachment_ref: str,
    body: ReasonBody = Body(...),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    return _attachment_out(
        call(session, work.remove_attachment, project_ref=project_ref,
             attachment_ref=attachment_ref, actor_id=user.id, reason=body.reason)
    )


__all__ = ["supervision_work_router"]
