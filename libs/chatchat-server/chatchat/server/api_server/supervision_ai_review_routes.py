"""AI 静态预审接口（《AI预审功能详细设计方案》里程碑4）。

与 ``supervision_routes`` / ``supervision_work_routes`` 同一个 ``/supervision``
前缀。错误口径完全一致：非成员与不存在统一 404，角色不足 403，状态不允许 409，
字段非法 400，**模型能力未接入 503**。当前用户只来自服务端认证上下文
（``require_password_changed``），绝不相信请求体里的 user_id。

三个入口（§1、§6、§7）：

- 学生手动自检：``POST .../tasks/{task_ref}/ai-review/self-check``；
- 导师手动预审 / 重新预审：``POST .../submissions/{submission_ref}/ai-review/run``
  与 ``.../rerun``；
- 提交后自动预审：不在任何 HTTP 入口里触发，由
  ``supervision_ai_review_repository.schedule_auto_review`` 在正式提交成功后
  异步创建（默认关闭）。

模型调用是**注入**的：``get_ai_review_llm`` / ``get_ai_review_capacity`` 是集成
点，里程碑4 里默认返回 ``None``，此时执行类接口返回 503 + 中文说明，绝不假装
已经跑过。创建类接口只排队，不调模型，因此仍然返回 201/200 + ``queued``。

路由顺序：``/ai-review/runs``、``/ai-review/settings`` 必须声明在
``/ai-review/{run_ref}`` 之前，否则会被路径参数吞掉。
"""
from __future__ import annotations

from typing import Any, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from chatchat.server import ai_review_runtime as ai_runtime
from chatchat.server.api_server.supervision_work_routes import call
from chatchat.server.auth.deps import require_password_changed
from chatchat.server.db.models.supervision_ai_review_model import (
    SupervisionAIReviewFindingModel,
    SupervisionAIReviewRunModel,
)
from chatchat.server.db.models.user_model import UserModel
from chatchat.server.db.repository import supervision_ai_review_repository as ai
from chatchat.server.db.repository import supervision_work_repository as work
from chatchat.server.db.session import get_db

supervision_ai_review_router = APIRouter(
    prefix="/supervision", tags=["Supervision - AI 静态预审"],
    dependencies=[Depends(require_password_changed)],
)


# ---------------------------------------------------------------------------
# 集成点：模型与容量闸门注入
# ---------------------------------------------------------------------------


def get_ai_review_llm() -> Any:
    """返回**已被统一容量闸门覆盖**的生成模型对象（有 ``.invoke(messages)``）。

    接线在 ``chatchat/server/ai_review_runtime.py``：模型以
    ``capacity_mode="external"`` 构造，容量由 ``get_ai_review_capacity()`` 的
    适配器在仓储层的每个 AC 批次前后取放（AI预审方案 §12.2.1）。模型自己不再
    取租约（上限 1 且不可重入，取了两次必然自锁），但会在发请求前确认租约确实
    在，缺了就抛 ``ModelCapacityLeaseNotHeldError``。

    模型没配好时返回 ``None``，执行类接口据此返回 503 + 中文说明，绝不假装跑过。
    测试通过 ``app.dependency_overrides`` 注入假模型，绝不联网。
    """
    return ai_runtime.get_ai_review_model()


def get_ai_review_capacity() -> Any:
    """容量适配器：``acquire(consumer=..., business_ref=..., user_id=...)`` /
    ``release(token, reason=...)``，内部取放 ``model_capacity`` 的租约。"""
    return ai_runtime.capacity_adapter


# ---------------------------------------------------------------------------
# 错误语义
# ---------------------------------------------------------------------------


def _unavailable(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail
    )


MODEL_NOT_WIRED = "模型能力尚未接入：预审已进入队列，接入统一模型队列后才能执行"


def _ts(value) -> Optional[str]:
    return value.isoformat() if value is not None else None


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------


class DraftBody(BaseModel):
    """学生当前草稿内容（§7.1）。

    里程碑1 的库里没有"草稿提交"实体，草稿由页面在点检查按钮时随请求带来；
    点击按钮这一刻才生成固定快照，运行期间页面继续编辑不会改动该快照。
    ``self_assessment`` 每项形如 ``{ac_code, self_assessment, evidence, note}``，
    取值合法性由仓储层按提交快照里的验收标准校验（不信请求体里的身份）。
    """

    summary: str = Field(default="", max_length=8000)
    self_assessment: List[dict] = Field(default_factory=list)
    git_repo: str = Field(default="", max_length=500)
    git_branch: str = Field(default="", max_length=200)
    git_commit: str = Field(default="", max_length=200)
    experiment_notes: str = Field(default="", max_length=8000)
    known_issues: str = Field(default="", max_length=8000)
    unfinished_items: str = Field(default="", max_length=8000)
    attachment_codes: List[str] = Field(default_factory=list)
    core_attachment_codes: List[str] = Field(default_factory=list)
    force: bool = False


class MentorRunBody(BaseModel):
    core_attachment_codes: List[str] = []


class CancelBody(BaseModel):
    reason: str = ""


class AttachBody(BaseModel):
    submission_ref: str


class AdoptBody(BaseModel):
    ac_code: str
    reason: str = ""


class SettingsBody(BaseModel):
    task_ref: str = ""
    mode: str = ""
    enabled: Optional[bool] = None


# ---------------------------------------------------------------------------
# 响应模型
# ---------------------------------------------------------------------------


class RunOut(BaseModel):
    run_id: str
    code: str
    display_code: str
    revision: int
    project_id: str
    task_id: str
    task_code: str
    submission_id: Optional[str] = None
    submission_code: str = ""
    submission_version: int = 0
    submission_display: str = ""
    trigger_source: str
    triggered_by: str
    triggered_role: str
    status: str
    summary_state: str
    summary_note: str
    ac_version: int
    total_batches: int
    completed_batches: int
    unchecked_acs: List[str] = []
    partial_results: bool
    call_plan: List[dict] = []
    model_calls: List[dict] = []
    input_budget: dict = {}
    material_manifest: List[dict] = []
    unread_materials: List[dict] = []
    partially_read_materials: List[dict] = []
    security_flags: List[str] = []
    finding_counts: dict = {}
    visible_to_mentor: bool
    attached_to_submission: bool
    # 不可变检查快照（§5.3、§17）：页面要能回答"这次到底基于哪一份内容检查的"。
    check_snapshot: dict = {}
    # 快照输入指纹：前端把当前草稿送到 staleness 接口后，用返回值与此比对判断
    # "草稿已变化"（§11.2）。
    draft_fingerprint: str = ""
    model_name: str = ""
    rules_version: str = ""
    parser_version: str = ""
    whitelist_version: str = ""
    budget_config_version: str = ""
    # 时效标记（§11）：不是运行状态，是结果相对当前草稿/当前提交的新鲜度。
    stale: bool = False
    stale_reasons: List[str] = []
    stale_label: str = ""
    attach_allowed: bool = False
    queue_status: dict = {}
    model_available: bool = False
    failure_reason: str = ""
    cancel_reason: str = ""
    cancelled_by: str = ""
    queued_at: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    cancelled_at: Optional[str] = None
    create_time: Optional[str] = None


class FindingOut(BaseModel):
    finding_id: str
    run_id: str
    run_code: str
    ac_code: str
    ac_statement: str
    ac_version: int
    required: bool
    check_type: str
    verdict: str
    evidence_depth: str
    support_relation: str
    reason: str
    satisfied_parts: str
    missing_parts: str
    evidence_refs: List[dict] = []
    missing_materials: List[str] = []
    risks: List[dict] = []
    mentor_focus: str
    injection_suspected: bool
    injection_note: str
    runtime_claim_flag: bool
    downgrade_reason: str
    validated: bool
    batch_index: int


class StalenessOut(BaseModel):
    run_code: str
    stale: bool
    reasons: List[str] = []
    based_on: str
    snapshot_fingerprint: str
    attach_allowed: bool
    rerun_allowed: bool
    label: str


class SettingsOut(BaseModel):
    project_id: str
    task_id: str
    project_mode: str
    task_mode: str
    effective_mode: str
    auto_review_enabled: bool
    default_is_off: bool
    priority_note: str


class SuggestionOut(BaseModel):
    ac_code: str
    verdict: str
    suggested_conclusion: str
    suggested_label: str
    reason: str
    note: str


class AdoptOut(BaseModel):
    run_code: str
    ac_code: str
    verdict: str
    conclusion: str
    ai_relation: str
    review_code: str


class MentorDiffOut(BaseModel):
    ac_code: str
    verdict: str
    suggested_conclusion: str
    mentor_conclusion: str
    ai_relation: str
    consistent: bool


# ---------------------------------------------------------------------------
# 序列化
# ---------------------------------------------------------------------------


def _run_out(
    session: Session, run: SupervisionAIReviewRunModel, *, model_available: bool = False
) -> RunOut:
    stale = ai.run_staleness(session, run=run)
    return RunOut(
        run_id=run.id, code=run.code, display_code=f"{run.code}-rev{run.revision}",
        revision=run.revision, project_id=run.project_id, task_id=run.task_id,
        task_code=run.task_code, submission_id=run.submission_id,
        submission_code=run.submission_code, submission_version=run.submission_version,
        submission_display=(
            f"{run.submission_code}-v{run.submission_version}"
            if run.submission_id else ""
        ),
        trigger_source=run.trigger_source, triggered_by=run.triggered_by,
        triggered_role=run.triggered_role, status=run.status,
        summary_state=run.summary_state, summary_note=run.summary_note,
        ac_version=run.ac_version, total_batches=run.total_batches,
        completed_batches=run.completed_batches,
        unchecked_acs=list(run.unchecked_acs or []),
        partial_results=run.partial_results,
        call_plan=list(run.call_plan or []), model_calls=list(run.model_calls or []),
        input_budget=dict(run.input_budget or {}),
        material_manifest=list(run.material_manifest or []),
        unread_materials=list(run.unread_materials or []),
        partially_read_materials=list(run.partially_read_materials or []),
        security_flags=list(run.security_flags or []),
        finding_counts=dict(run.finding_counts or {}),
        visible_to_mentor=run.visible_to_mentor,
        attached_to_submission=run.attached_to_submission,
        check_snapshot=dict(run.check_snapshot or {}),
        draft_fingerprint=run.draft_fingerprint,
        model_name=run.model_name, rules_version=run.rules_version,
        parser_version=run.parser_version,
        whitelist_version=run.whitelist_version,
        budget_config_version=run.budget_config_version,
        stale=stale["stale"], stale_reasons=stale["reasons"],
        stale_label=stale["label"], attach_allowed=stale["attach_allowed"],
        queue_status=ai.queue_position(session, run),
        model_available=model_available,
        failure_reason=run.failure_reason, cancel_reason=run.cancel_reason,
        cancelled_by=run.cancelled_by,
        queued_at=_ts(run.queued_at), started_at=_ts(run.started_at),
        finished_at=_ts(run.finished_at), cancelled_at=_ts(run.cancelled_at),
        create_time=_ts(run.create_time),
    )


def _finding_out(
    run: SupervisionAIReviewRunModel, f: SupervisionAIReviewFindingModel
) -> FindingOut:
    return FindingOut(
        finding_id=f.id, run_id=f.run_id, run_code=run.code, ac_code=f.ac_code,
        ac_statement=f.ac_statement, ac_version=f.ac_version, required=f.required,
        check_type=f.check_type, verdict=f.verdict, evidence_depth=f.evidence_depth,
        support_relation=f.support_relation, reason=f.reason,
        satisfied_parts=f.satisfied_parts, missing_parts=f.missing_parts,
        evidence_refs=list(f.evidence_refs or []),
        missing_materials=list(f.missing_materials or []),
        risks=list(f.risks or []), mentor_focus=f.mentor_focus,
        injection_suspected=f.injection_suspected, injection_note=f.injection_note,
        runtime_claim_flag=f.runtime_claim_flag,
        downgrade_reason=f.downgrade_reason, validated=f.validated,
        batch_index=f.batch_index,
    )


# ---------------------------------------------------------------------------
# 入口 1：学生手动自检（§6.1、§7.1）
# ---------------------------------------------------------------------------


@supervision_ai_review_router.post(
    "/projects/{project_ref}/tasks/{task_ref}/ai-review/self-check",
    response_model=RunOut,
)
def run_self_check(
    project_ref: str,
    task_ref: str,
    body: DraftBody = Body(...),
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
    llm=Depends(get_ai_review_llm),
) -> RunOut:
    """学生对当前任务草稿运行 AI 检查。只排队，不调模型（模型未接入时也不报错）。

    结果默认只对学生可见（P-03）；草稿变化后旧结果显示"基于旧快照"（P-02）。
    """
    run, _reused = call(
        session, ai.create_run,
        project_ref=project_ref, actor_id=user.id, task_ref=task_ref,
        trigger_source="student_manual",
        draft=body.model_dump(), force=body.force,
    )
    return _run_out(session, run, model_available=llm is not None)


# ---------------------------------------------------------------------------
# 入口 2：导师手动预审 / 重新预审（§6.3、§7.3、§13.2）
# ---------------------------------------------------------------------------


@supervision_ai_review_router.post(
    "/projects/{project_ref}/submissions/{submission_ref}/ai-review/run",
    response_model=RunOut,
)
def run_mentor_review(
    project_ref: str,
    submission_ref: str,
    body: MentorRunBody = Body(default_factory=MentorRunBody),
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
    llm=Depends(get_ai_review_llm),
) -> RunOut:
    """导师对某个正式提交运行预审。已有同快照完成结果时复用（§7.3 第2条）。"""
    run, _reused = call(
        session, ai.create_run,
        project_ref=project_ref, actor_id=user.id,
        task_ref=_task_ref_of_submission(session, project_ref, submission_ref, user.id),
        trigger_source="mentor_manual", submission_ref=submission_ref,
        draft={"core_attachment_codes": body.core_attachment_codes},
    )
    return _run_out(session, run, model_available=llm is not None)


@supervision_ai_review_router.post(
    "/projects/{project_ref}/submissions/{submission_ref}/ai-review/rerun",
    response_model=RunOut,
)
def rerun_submission_review(
    project_ref: str,
    submission_ref: str,
    body: MentorRunBody = Body(default_factory=MentorRunBody),
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
    llm=Depends(get_ai_review_llm),
) -> RunOut:
    """明确"重新预审"：生成新 run 版本，旧版本继续保留可查（P-21、§13.2）。"""
    run, _reused = call(
        session, ai.create_run,
        project_ref=project_ref, actor_id=user.id,
        task_ref=_task_ref_of_submission(session, project_ref, submission_ref, user.id),
        trigger_source="mentor_manual", submission_ref=submission_ref,
        draft={"core_attachment_codes": body.core_attachment_codes},
        force=True,
    )
    return _run_out(session, run, model_available=llm is not None)


def _task_ref_of_submission(
    session: Session, project_ref: str, submission_ref: str, actor_id: str
) -> str:
    submission = call(
        session, work.get_submission,
        project_ref=project_ref, submission_ref=submission_ref, actor_id=actor_id,
    )
    return submission.task_id


# ---------------------------------------------------------------------------
# 执行、取消、附带
# ---------------------------------------------------------------------------


@supervision_ai_review_router.post(
    "/projects/{project_ref}/ai-review/{run_ref}/execute", response_model=RunOut
)
def execute_run(
    project_ref: str,
    run_ref: str,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
    llm=Depends(get_ai_review_llm),
    capacity=Depends(get_ai_review_capacity),
) -> RunOut:
    """执行一个已排队的预审。模型未接入时返回 503，绝不伪造结果（§14.2）。"""
    if llm is None:
        raise _unavailable(MODEL_NOT_WIRED)
    run = call(
        session, ai.run_review,
        project_ref=project_ref, run_ref=run_ref, actor_id=user.id,
        llm=llm, capacity=capacity,
    )
    return _run_out(session, run, model_available=True)


@supervision_ai_review_router.post(
    "/projects/{project_ref}/ai-review/{run_ref}/cancel", response_model=RunOut
)
def cancel_run(
    project_ref: str,
    run_ref: str,
    body: CancelBody = Body(default_factory=CancelBody),
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> RunOut:
    """取消排队中或运行中的预审；不影响正式提交与人工审核（P-26/P-27、§13.3）。"""
    run = call(
        session, ai.cancel_run,
        project_ref=project_ref, run_ref=run_ref, actor_id=user.id,
        reason=body.reason,
    )
    return _run_out(session, run)


@supervision_ai_review_router.post(
    "/projects/{project_ref}/ai-review/{run_ref}/attach", response_model=RunOut
)
def attach_self_check(
    project_ref: str,
    run_ref: str,
    body: AttachBody = Body(...),
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> RunOut:
    """把已完成的自检快照附到正式提交上，导师这才能看到它（P-04、§4 规则4）。

    附带的是固定快照；过期结果不能附带（§7.1 第4条）。
    """
    run = call(
        session, ai.attach_self_check,
        project_ref=project_ref, run_ref=run_ref,
        submission_ref=body.submission_ref, actor_id=user.id,
    )
    return _run_out(session, run)


@supervision_ai_review_router.post(
    "/projects/{project_ref}/ai-review/{run_ref}/retry", response_model=RunOut
)
def retry_run(
    project_ref: str,
    run_ref: str,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
    llm=Depends(get_ai_review_llm),
) -> RunOut:
    """失败/取消后重新运行：生成新版本，不在旧运行上静默补齐（§12.4、§14.2）。"""
    run = call(
        session, ai.retry_run,
        project_ref=project_ref, run_ref=run_ref, actor_id=user.id,
    )
    return _run_out(session, run, model_available=llm is not None)


# ---------------------------------------------------------------------------
# 查询：列表、详情、逐条结论、时效
# ---------------------------------------------------------------------------


@supervision_ai_review_router.get(
    "/projects/{project_ref}/ai-review/runs", response_model=List[RunOut]
)
def list_runs(
    project_ref: str,
    task_ref: str = Query(default="", max_length=32),
    submission_ref: str = Query(default="", max_length=32),
    status_filter: str = Query(default="", alias="status", max_length=16),
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> List[RunOut]:
    """run 列表（新→旧）。私人自检只对本人出现，导师看不到未附带的自检。"""
    runs = call(
        session, ai.list_runs,
        project_ref=project_ref, actor_id=user.id, task_ref=task_ref,
        submission_ref=submission_ref, status=status_filter,
    )
    return [_run_out(session, run) for run in runs]


@supervision_ai_review_router.get(
    "/projects/{project_ref}/tasks/{task_ref}/ai-review/latest",
    response_model=Optional[RunOut],
)
def latest_run(
    project_ref: str,
    task_ref: str,
    submission_ref: str = Query(default="", max_length=32),
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
    llm=Depends(get_ai_review_llm),
) -> Optional[RunOut]:
    """页面默认展示的最新版本；旧版本仍在列表里可查（§13.2）。"""
    run = call(
        session, ai.latest_run_for_task,
        project_ref=project_ref, actor_id=user.id, task_ref=task_ref,
        submission_ref=submission_ref,
    )
    if run is None:
        return None
    return _run_out(session, run, model_available=llm is not None)


@supervision_ai_review_router.get(
    "/projects/{project_ref}/ai-review/settings", response_model=SettingsOut
)
def get_settings(
    project_ref: str,
    task_ref: str = Query(default="", max_length=32),
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> SettingsOut:
    """自动预审开关（项目默认 + 任务级）。默认关闭（P-05、§16.5 第2条）。"""
    return SettingsOut(**call(
        session, ai.get_auto_review_setting,
        project_ref=project_ref, actor_id=user.id, task_ref=task_ref,
    ))


@supervision_ai_review_router.put(
    "/projects/{project_ref}/ai-review/settings", response_model=SettingsOut
)
def put_settings(
    project_ref: str,
    body: SettingsBody = Body(...),
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> SettingsOut:
    """导师配置自动预审：``task_ref`` 为空写项目默认，否则写任务级。"""
    return SettingsOut(**call(
        session, ai.set_auto_review_setting,
        project_ref=project_ref, actor_id=user.id, task_ref=body.task_ref,
        mode=body.mode, enabled=body.enabled,
    ))


@supervision_ai_review_router.get(
    "/projects/{project_ref}/ai-review/{run_ref}", response_model=RunOut
)
def get_run(
    project_ref: str,
    run_ref: str,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
    llm=Depends(get_ai_review_llm),
) -> RunOut:
    """run 详情：完整输入清单、预算实际值、批次计划、材料读取状态、告警。"""
    run = call(
        session, ai.get_run,
        project_ref=project_ref, run_ref=run_ref, actor_id=user.id,
    )
    return _run_out(session, run, model_available=llm is not None)


@supervision_ai_review_router.get(
    "/projects/{project_ref}/ai-review/{run_ref}/findings",
    response_model=List[FindingOut],
)
def get_findings(
    project_ref: str,
    run_ref: str,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> List[FindingOut]:
    """逐条验收矩阵（§6.3、§10.1）：状态、深度、理由、证据定位、缺失材料。"""
    run = call(
        session, ai.get_run,
        project_ref=project_ref, run_ref=run_ref, actor_id=user.id,
    )
    return [
        _finding_out(run, f)
        for f in call(session, ai.list_findings, run_id=run.id)
    ]


@supervision_ai_review_router.post(
    "/projects/{project_ref}/ai-review/{run_ref}/staleness",
    response_model=StalenessOut,
)
def check_staleness(
    project_ref: str,
    run_ref: str,
    body: Optional[DraftBody] = Body(default=None),
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> StalenessOut:
    """过期状态查询（§11.2）。带上页面当前草稿即可判断"基于旧快照"（P-02）。"""
    run = call(
        session, ai.get_run,
        project_ref=project_ref, run_ref=run_ref, actor_id=user.id,
    )
    draft = body.model_dump() if body is not None else None
    return StalenessOut(run_code=run.code, **call(
        session, ai.run_staleness, run=run, draft=draft
    ))


@supervision_ai_review_router.get(
    "/projects/{project_ref}/ai-review/{run_ref}/suggested-conclusions",
    response_model=List[SuggestionOut],
)
def get_suggested_conclusions(
    project_ref: str,
    run_ref: str,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> List[SuggestionOut]:
    """AI 的"建议导师结论"（§15.2）。只读；不会自动写入任何导师结论。"""
    rows = call(
        session, ai.suggested_conclusions,
        project_ref=project_ref, run_ref=run_ref, actor_id=user.id,
    )
    return [SuggestionOut(**row) for row in rows]


@supervision_ai_review_router.get(
    "/projects/{project_ref}/ai-review/{run_ref}/mentor-diff",
    response_model=List[MentorDiffOut],
)
def get_mentor_diff(
    project_ref: str,
    run_ref: str,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> List[MentorDiffOut]:
    """AI 建议与导师最终结论的一致性（P-30）。只读，不限制导师选择。"""
    rows = call(
        session, ai.ai_vs_mentor_diff,
        project_ref=project_ref, run_ref=run_ref, actor_id=user.id,
    )
    return [MentorDiffOut(**row) for row in rows]


@supervision_ai_review_router.post(
    "/projects/{project_ref}/ai-review/{run_ref}/adopt", response_model=AdoptOut
)
def adopt_suggestion(
    project_ref: str,
    run_ref: str,
    body: AdoptBody = Body(...),
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> AdoptOut:
    """导师逐项**主动**采纳 AI 映射建议，才写导师结论（P-20/P-22、§15.2）。

    产品不提供含义模糊的"否定 AI"；导师要选不同结论走原有审核草稿接口，
    ``ai_relation`` 记 ``different``，AI 原始建议保留。
    """
    row = call(
        session, ai.adopt_ai_suggestion,
        project_ref=project_ref, run_ref=run_ref, actor_id=user.id,
        ac_code=body.ac_code, reason=body.reason,
    )
    return AdoptOut(**row)


__all__ = [
    "supervision_ai_review_router",
    "get_ai_review_llm",
    "get_ai_review_capacity",
]
