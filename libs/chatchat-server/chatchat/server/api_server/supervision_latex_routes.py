"""LaTeX 稿件静态分析接口（《学生研究与导师审核功能重新设计方案》里程碑5 路由层）。

与 ``supervision_routes`` / ``supervision_work_routes`` /
``supervision_ai_review_routes`` 同一个 ``/supervision`` 前缀，全部
``Depends(require_password_changed)``：当前用户只来自服务端令牌，绝不相信
请求体里的 user_id。

接口一览：

- ``POST /projects/{project_ref}/latex/analyze``：分析一份 .zip 稿件附件 →
  分析快照（结构大纲、计数、静态检查发现、主文件判定、生效上限）；
- ``POST /projects/{project_ref}/latex/progress-draft``：两个版本 → 进度草稿
  （首次上传只出基线）；
- ``GET  /projects/{project_ref}/latex/snapshots``、
  ``GET  /projects/{project_ref}/latex/snapshots/{snapshot_ref}``；
- ``GET  /projects/{project_ref}/latex/progress-drafts``、
  ``GET  /projects/{project_ref}/latex/progress-drafts/{draft_ref}``；
- ``POST /projects/{project_ref}/latex/progress-drafts/{draft_ref}/confirm``：
  **只有创建该草稿的学生本人**能把草稿转成一条正式进展；
- ``POST /projects/{project_ref}/latex/progress-drafts/{draft_ref}/discard``：
  只标记状态，不删数据。

错误口径与既有 supervision 模块一致：非成员与不存在统一 404，角色不足 403，
状态不允许 409，字段非法 400，未登录 401（由依赖给出）。两类补充：

- **危险压缩包** → 409，``detail`` 是 ``{message, problems}``：路径穿越、
  压缩炸弹、嵌套归档、符号链接等命中项**原样列出**，不静默通过；
- **主文件不确定** → ``ambiguous`` 409 + ``candidates``（必须让用户选，
  系统不猜），``invalid_selection``/``not_found`` 400。

全程不编译 LaTeX、不执行稿件里的任何命令、不联网：响应里的 ``compiled``
恒为 ``false``，``caveat`` 固定说明"静态差异不等于实验完成"。模型摘要只是
可选附加字段：模型未接入时 ``model_summary_status="unavailable"`` 并留中文
说明，**不会**因此 503，草稿照常生成。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from chatchat.server.api_server.api_schemas import ResponseModel
from sqlalchemy.orm import Session

from chatchat.server.auth.deps import require_password_changed
from chatchat.server.db.models.supervision_latex_model import (
    SupervisionLatexProgressDraftModel,
    SupervisionLatexSnapshotModel,
)
from chatchat.server.db.models.supervision_work_model import (
    SupervisionProgressUpdateModel,
)
from chatchat.server.db.models.user_model import UserModel
from chatchat.server.db.repository import supervision_latex_repository as latex
from chatchat.server.db.repository import supervision_work_repository as work
from chatchat.server.db.session import get_db
from chatchat.server.model_capacity import (
    CAPACITY_MODE_EXTERNAL,
    CONSUMER_PROGRESS_DRAFT,
    CapacityLeaseAdapter,
)
from chatchat.server.supervision.zip_safety import ZipSafetyError
from chatchat.utils import build_logger

logger = build_logger(__name__)

supervision_latex_router = APIRouter(
    prefix="/supervision",
    tags=["Supervision - LaTeX 稿件静态分析"],
    dependencies=[Depends(require_password_changed)],
)

#: 摘要调用的容量适配器：无状态可复用；真正的消费者逐次传入
#: ``CONSUMER_PROGRESS_DRAFT``（已登记，通道由 consumers.py 推导）。
progress_capacity_adapter = CapacityLeaseAdapter(default_consumer=CONSUMER_PROGRESS_DRAFT)

#: 摘要模型按进程缓存；构造模型对象本身不持有任何容量。
_model_cache: dict = {}


# ---------------------------------------------------------------------------
# 集成点：模型与容量闸门注入
# ---------------------------------------------------------------------------


def get_latex_summary_llm() -> Any:
    """返回**已被统一容量闸门覆盖**的摘要模型；未接入时返回 ``None``。

    模型以 ``capacity_mode="external"`` 构造：容量由仓储层通过
    ``get_latex_summary_capacity()`` 的适配器取放，模型自己不能再取一次
    （上限 1 且不可重入）。构造失败不缓存，下次调用再试。

    与 AI 预审不同：这里 ``None`` **不**代表接口失败，只代表这次没有模型摘要
    ——草稿是纯静态解析产物，照常生成。测试用
    ``app.dependency_overrides`` 注入假模型，绝不联网。
    """
    cached = _model_cache.get("model")
    if cached is not None:
        return cached
    try:
        from chatchat.settings import Settings
        from chatchat.server.utils import get_ChatOpenAI, get_default_llm

        model = get_ChatOpenAI(
            model_name=get_default_llm(),
            temperature=Settings.model_settings.TEMPERATURE,
            max_tokens=Settings.model_settings.MAX_TOKENS,
            streaming=False,
            verbose=False,
            consumer=CONSUMER_PROGRESS_DRAFT,
            capacity_mode=CAPACITY_MODE_EXTERNAL,
        )
    except Exception:  # 模型没配好：摘要留空，草稿照常生成
        logger.exception("构造 LaTeX 进度草稿摘要模型失败（摘要留空，草稿不受影响）")
        return None
    if model is None:
        return None
    _model_cache["model"] = model
    return model


def get_latex_summary_capacity() -> Any:
    """容量适配器：``acquire(consumer=…, business_ref=…, user_id=…)`` /
    ``release(token, reason=…)``，内部取放 ``model_capacity`` 的租约。"""
    return progress_capacity_adapter


def reset_latex_summary_model_cache() -> None:
    """测试与运维用：清掉模型缓存，下次调用重新构造。"""
    _model_cache.clear()


# ---------------------------------------------------------------------------
# 错误语义
# ---------------------------------------------------------------------------


def _not_found(detail: str = "资源不存在或无权访问") -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _forbidden(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


def _conflict(detail: Any) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def call(session: Session, action, *args, **kwargs):
    """把仓储层异常收敛成统一 HTTP 语义，并在失败时回滚。

    与 ``supervision_work_routes.call`` 同一套口径，另外多两类 LaTeX 专属：
    ``ZipSafetyError`` → 409 + 全部安全检查命中项；
    ``MainFileSelectionRequired`` → 主文件有歧义 409 + 候选清单，
    指定错/找不到 400。拒绝归档的审计事件已在仓储层单独提交，
    这里的 rollback 只回滚未落库的业务改动。
    """
    try:
        result = action(session, *args, **kwargs)
        session.commit()
        return result
    except LookupError as exc:
        session.rollback()
        raise _not_found(str(exc) or "资源不存在或无权访问")
    except latex.LatexAccessDenied as exc:
        session.rollback()
        raise _forbidden(str(exc))
    except PermissionError as exc:
        # 非成员与不存在统一 404，避免枚举项目是否存在（§10）。
        session.rollback()
        raise _not_found(str(exc) or "资源不存在或无权访问")
    except latex.MainFileSelectionRequired as exc:
        session.rollback()
        if exc.status == "ambiguous":
            raise _conflict(
                {
                    "message": exc.message,
                    "attachment_code": exc.attachment_code,
                    "candidates": exc.candidates,
                }
            )
        raise _bad_request(exc.message)
    except ZipSafetyError as exc:
        session.rollback()
        raise _conflict(
            {
                "message": "压缩包未通过安全检查，已拒绝分析（未解压、未解析）",
                "problems": [problem.as_dict() for problem in exc.problems],
            }
        )
    except work.WorkValidationError as exc:
        session.rollback()
        raise _bad_request(str(exc))
    except work.WorkError as exc:
        session.rollback()
        raise _conflict(str(exc))


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------


class AnalyzeBody(BaseModel):
    attachment_ref: str = Field(min_length=1, max_length=64)
    main_file: str = Field(default="", max_length=500)


class ProgressDraftBody(BaseModel):
    attachment_ref: str = Field(min_length=1, max_length=64)
    base_attachment_ref: str = Field(default="", max_length=64)
    main_file: str = Field(default="", max_length=500)
    base_main_file: str = Field(default="", max_length=500)


class DraftConfirmBody(BaseModel):
    """学生确认时可编辑的正文；草稿条目由系统附加，不需要学生重抄。"""

    task_ref: str = Field(default="", max_length=32)
    milestone_ref: str = Field(default="", max_length=32)
    done: str = Field(default="", max_length=8000)
    results: str = Field(default="", max_length=8000)
    materials: str = Field(default="", max_length=8000)
    problems: str = Field(default="", max_length=8000)
    next_steps: str = Field(default="", max_length=8000)


class DraftDiscardBody(BaseModel):
    reason: str = Field(default="", max_length=4000)


# ---------------------------------------------------------------------------
# 响应模型
# ---------------------------------------------------------------------------


class ParseBudgetOut(ResponseModel):
    """解析预算。两个键与 `supervision_latex_repository.parse_budget()` 一一对应。

    超预算的文件会被如实标成 partially_read / not_read，所以这两个数前端要显示。
    """

    max_source_bytes: int
    max_source_files: int


class SnapshotLimitsOut(ResponseModel):
    """一次解压的全部上限。

    字段与 `supervision.zip_safety.ZipLimits.as_dict()` 一一对应：as_dict 只导出这
    7 个键（内部判定用的扩展名集合不外露）。改 as_dict 必须同步改这里，否则契约里
    会多出实际不会写的键，或丢掉前端在用的键。
    """

    max_entries: int
    max_total_bytes: int
    max_single_bytes: int
    max_compression_ratio: float
    ratio_min_bytes: int
    max_name_bytes: int
    allowed_extensions: List[str]


class SnapshotOut(ResponseModel):
    snapshot_id: str
    code: str
    project_id: str
    attachment_code: str
    attachment_name: str
    attachment_sha256: str
    attachment_version: int
    main_file: str
    main_path: str
    main_status: str
    main: Dict[str, Any]
    candidates: List[Dict[str, Any]]
    parser_version: str
    checker_version: str
    zip_safety_version: str
    limits: SnapshotLimitsOut
    parse_budget: ParseBudgetOut
    document_present: bool
    compiled: bool
    # document.counts() 的返回标注就是 Dict[str, int]
    counts: Dict[str, int]
    outline: List[str]
    word_count: int
    findings: List[Dict[str, Any]]
    # latex_check 里 counts 的声明就是 Dict[str, int]（按 severity 与 kind 计数）
    finding_counts: Dict[str, int]
    dangerous_findings: List[Dict[str, Any]]
    partially_read: List[Dict[str, Any]]
    not_read: List[Dict[str, Any]]
    unreferenced_tex: List[str]
    available_files: List[str]
    extracted_files: List[Dict[str, Any]]
    file_count: int
    total_bytes: int
    document: Optional[Dict[str, Any]] = None
    check: Optional[Dict[str, Any]] = None
    created_by: str
    created_role: str
    create_time: Optional[str] = None


class DraftOut(ResponseModel):
    draft_id: str
    code: str
    project_id: str
    library_draft_id: str
    status: str
    published: bool
    requires_student_confirmation: bool
    baseline_only: bool
    draft_version: str
    parser_version: str
    checker_version: str
    zip_safety_version: str
    snapshot_code: str
    base_snapshot_code: str
    attachment_code: str
    attachment_sha256: str
    attachment_version: int
    base_attachment_code: str
    base_attachment_version: int
    base_attachment_sha256: str
    main_file: str
    base_main_file: str
    summary: str
    caveat: str
    items: List[Dict[str, Any]]
    item_count: int
    truncated: bool
    dropped_items: int
    counts: Dict[str, Any]
    word_count_old: int
    word_count_new: int
    word_count_delta: int
    model_summary: str
    model_summary_status: str
    model_summary_note: str
    model_name: str
    confirmed_progress_code: str
    confirmed_by: str
    confirmed_role: str
    confirmed_at: Optional[str] = None
    discarded_by: str
    discarded_role: str
    discarded_at: Optional[str] = None
    discard_reason: str
    created_by: str
    created_role: str
    create_time: Optional[str] = None
    update_time: Optional[str] = None


class ProgressOut(ResponseModel):
    progress_id: str
    code: str
    project_id: str
    task_id: Optional[str] = None
    done: str
    results: str
    materials: str
    problems: str
    next_steps: str
    links: List[Dict[str, Any]]
    author_id: str
    author_role: str
    create_time: Optional[str] = None


class ConfirmOut(ResponseModel):
    draft: DraftOut
    progress: ProgressOut


def _ts(value) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _snapshot_out(snapshot: SupervisionLatexSnapshotModel) -> dict:
    main = dict(snapshot.main_json or {})
    document = snapshot.document_json if snapshot.document_present else None
    return {
        "snapshot_id": snapshot.id,
        "code": snapshot.code,
        "project_id": snapshot.project_id,
        "attachment_code": snapshot.attachment_code,
        "attachment_name": snapshot.attachment_name,
        "attachment_sha256": snapshot.attachment_sha256,
        "attachment_version": snapshot.attachment_version,
        "main_file": snapshot.main_file,
        "main_path": snapshot.main_path,
        "main_status": snapshot.main_status,
        "main": main,
        "candidates": list(main.get("candidates") or []),
        "parser_version": snapshot.parser_version,
        "checker_version": snapshot.checker_version,
        "zip_safety_version": snapshot.zip_safety_version,
        "limits": dict(snapshot.limits_json or {}),
        "parse_budget": dict(snapshot.parse_budget_json or {}),
        "document_present": bool(snapshot.document_present),
        "compiled": bool(snapshot.compiled),  # 恒 False：系统从不编译 LaTeX
        "counts": dict(snapshot.counts or {}),
        "outline": list(snapshot.outline or []),
        "word_count": snapshot.word_count,
        "findings": list(snapshot.findings or []),
        "finding_counts": dict(snapshot.finding_counts or {}),
        "dangerous_findings": list(snapshot.dangerous_findings or []),
        "partially_read": list(snapshot.partially_read or []),
        "not_read": list(snapshot.not_read or []),
        "unreferenced_tex": list(snapshot.unreferenced_tex or []),
        "available_files": list(snapshot.available_files or []),
        "extracted_files": list(snapshot.extracted_files or []),
        "file_count": snapshot.file_count,
        "total_bytes": snapshot.total_bytes,
        "document": document,
        "check": dict(snapshot.check_json) if snapshot.check_json else None,
        "created_by": snapshot.created_by,
        "created_role": snapshot.created_role,
        "create_time": _ts(snapshot.create_time),
    }


def _draft_out(draft: SupervisionLatexProgressDraftModel) -> dict:
    return {
        "draft_id": draft.id,
        "code": draft.code,
        "project_id": draft.project_id,
        "library_draft_id": draft.library_draft_id,
        "status": draft.status,
        "published": bool(draft.published),
        "requires_student_confirmation": bool(draft.requires_student_confirmation),
        "baseline_only": bool(draft.baseline_only),
        "draft_version": draft.draft_version,
        "parser_version": draft.parser_version,
        "checker_version": draft.checker_version,
        "zip_safety_version": draft.zip_safety_version,
        "snapshot_code": draft.snapshot_code,
        "base_snapshot_code": draft.base_snapshot_code,
        "attachment_code": draft.attachment_code,
        "attachment_sha256": draft.attachment_sha256,
        "attachment_version": draft.attachment_version,
        "base_attachment_code": draft.base_attachment_code,
        "base_attachment_version": draft.base_attachment_version,
        "base_attachment_sha256": draft.base_attachment_sha256,
        "main_file": draft.main_file,
        "base_main_file": draft.base_main_file,
        "summary": draft.summary,
        "caveat": draft.caveat,
        "items": list(draft.items or []),
        "item_count": draft.item_count,
        "truncated": bool(draft.truncated),
        "dropped_items": draft.dropped_items,
        "counts": dict(draft.diff_counts or {}),
        "word_count_old": draft.word_count_old,
        "word_count_new": draft.word_count_new,
        "word_count_delta": draft.word_count_delta,
        "model_summary": draft.model_summary,
        "model_summary_status": draft.model_summary_status,
        "model_summary_note": draft.model_summary_note,
        "model_name": draft.model_name,
        "confirmed_progress_code": draft.confirmed_progress_code,
        "confirmed_by": draft.confirmed_by,
        "confirmed_role": draft.confirmed_role,
        "confirmed_at": _ts(draft.confirmed_at),
        "discarded_by": draft.discarded_by,
        "discarded_role": draft.discarded_role,
        "discarded_at": _ts(draft.discarded_at),
        "discard_reason": draft.discard_reason,
        "created_by": draft.created_by,
        "created_role": draft.created_role,
        "create_time": _ts(draft.create_time),
        "update_time": _ts(draft.update_time),
    }


def _progress_out(progress: SupervisionProgressUpdateModel) -> dict:
    return {
        "progress_id": progress.id,
        "code": progress.code,
        "project_id": progress.project_id,
        "task_id": progress.task_id,
        "done": progress.done,
        "results": progress.results,
        "materials": progress.materials,
        "problems": progress.problems,
        "next_steps": progress.next_steps,
        "links": list(progress.links or []),
        "author_id": progress.author_id,
        "author_role": progress.author_role,
        "create_time": _ts(progress.create_time),
    }


# ---------------------------------------------------------------------------
# 分析快照
# ---------------------------------------------------------------------------


@supervision_latex_router.post(
    "/projects/{project_ref}/latex/analyze", response_model=SnapshotOut
)
def analyze_latex(
    project_ref: str,
    body: AnalyzeBody = Body(...),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    """安全解压 + 静态解析一份 .zip 稿件附件，落一份分析快照并返回。

    危险压缩包 → 409（全部命中项在 ``detail.problems`` 里）并留下拒绝审计；
    主文件有歧义 → 快照仍落，响应里 ``document_present=false`` +
    ``candidates``，页面必须让学生选主文件后重试。
    """
    snapshot = call(
        session, latex.analyze_attachment, project_ref=project_ref,
        actor_id=user.id, attachment_ref=body.attachment_ref,
        main_file=body.main_file,
    )
    return _snapshot_out(snapshot)


@supervision_latex_router.get(
    "/projects/{project_ref}/latex/snapshots", response_model=List[SnapshotOut]
)
def list_latex_snapshots(
    project_ref: str,
    attachment_ref: str = Query(default="", max_length=64),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    """分析历史（新→旧）。重新分析是新快照，旧结论不被覆盖。"""
    rows = call(
        session, latex.list_snapshots, project_ref=project_ref, actor_id=user.id,
        attachment_ref=attachment_ref,
    )
    return [_snapshot_out(row) for row in rows]


@supervision_latex_router.get(
    "/projects/{project_ref}/latex/snapshots/{snapshot_ref}",
    response_model=SnapshotOut,
)
def get_latex_snapshot(
    project_ref: str,
    snapshot_ref: str,
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    snapshot = call(
        session, latex.get_snapshot, project_ref=project_ref, actor_id=user.id,
        snapshot_ref=snapshot_ref,
    )
    return _snapshot_out(snapshot)


# ---------------------------------------------------------------------------
# 进度草稿
# ---------------------------------------------------------------------------


@supervision_latex_router.post(
    "/projects/{project_ref}/latex/progress-draft", response_model=DraftOut
)
def create_progress_draft(
    project_ref: str,
    body: ProgressDraftBody = Body(...),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
    llm=Depends(get_latex_summary_llm),
    capacity=Depends(get_latex_summary_capacity),
):
    """两个版本的结构差异 → 进度草稿（``published=false``、必须学生确认）。

    没有 ``base_attachment_ref`` 时是首次上传：只出基线
    （``baseline_only=true``、``status="baseline"``、无条目），不编造进展。
    模型未接入只影响 ``model_summary`` 字段，草稿照常生成。
    """
    draft = call(
        session, latex.build_progress_draft, project_ref=project_ref,
        actor_id=user.id, attachment_ref=body.attachment_ref,
        base_attachment_ref=body.base_attachment_ref, main_file=body.main_file,
        base_main_file=body.base_main_file, llm=llm, capacity=capacity,
    )
    return _draft_out(draft)


@supervision_latex_router.get(
    "/projects/{project_ref}/latex/progress-drafts", response_model=List[DraftOut]
)
def list_progress_drafts(
    project_ref: str,
    draft_status: str = Query(default="", alias="status", max_length=16),
    attachment_ref: str = Query(default="", max_length=64),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    """草稿列表（新→旧）：历史草稿全部保留，确认与丢弃只改状态。"""
    rows = call(
        session, latex.list_drafts, project_ref=project_ref, actor_id=user.id,
        status=draft_status, attachment_ref=attachment_ref,
    )
    return [_draft_out(row) for row in rows]


@supervision_latex_router.get(
    "/projects/{project_ref}/latex/progress-drafts/{draft_ref}",
    response_model=DraftOut,
)
def get_progress_draft(
    project_ref: str,
    draft_ref: str,
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    draft = call(
        session, latex.get_draft, project_ref=project_ref, actor_id=user.id,
        draft_ref=draft_ref,
    )
    return _draft_out(draft)


@supervision_latex_router.post(
    "/projects/{project_ref}/latex/progress-drafts/{draft_ref}/confirm",
    response_model=ConfirmOut,
)
def confirm_progress_draft(
    project_ref: str,
    draft_ref: str,
    body: DraftConfirmBody = Body(default_factory=DraftConfirmBody),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    """学生确认草稿 → 生成一条正式进展记录。

    只有创建该草稿的学生本人能确认（导师 → 403）；已确认过再确认 → 409
    （响应里给出已有的进展编号），已丢弃 → 409。进展正文 = 学生填写的说明
    + 草稿条目 + 固定口径说明，``links`` 回指草稿与两侧附件。
    """
    draft, progress = call(
        session, latex.confirm_draft, project_ref=project_ref, actor_id=user.id,
        draft_ref=draft_ref, done=body.done, results=body.results,
        materials=body.materials, problems=body.problems,
        next_steps=body.next_steps, task_ref=body.task_ref,
        milestone_ref=body.milestone_ref,
    )
    return {"draft": _draft_out(draft), "progress": _progress_out(progress)}


@supervision_latex_router.post(
    "/projects/{project_ref}/latex/progress-drafts/{draft_ref}/discard",
    response_model=DraftOut,
)
def discard_progress_draft(
    project_ref: str,
    draft_ref: str,
    body: DraftDiscardBody = Body(default_factory=DraftDiscardBody),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    """丢弃草稿：只标记 ``status="discarded"``，快照与草稿数据都留着。"""
    draft = call(
        session, latex.discard_draft, project_ref=project_ref, actor_id=user.id,
        draft_ref=draft_ref, reason=body.reason,
    )
    return _draft_out(draft)


__all__ = [
    "get_latex_summary_capacity",
    "get_latex_summary_llm",
    "progress_capacity_adapter",
    "reset_latex_summary_model_cache",
    "supervision_latex_router",
]
