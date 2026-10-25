"""论文检索与文献结论接口（《学生研究与导师审核功能重新设计方案》里程碑3）。

与 ``supervision_routes`` / ``supervision_work_routes`` /
``supervision_research_routes`` 共用 ``/supervision`` 前缀，覆盖：检索任务的
创建与检索方案确认、按来源与关键词批次拆分的可重试检索子任务、候选论文的
去重与可解释关联度、学生的纳入/排除/稍后阅读、单篇深度提取与证据卡片、
有上限的摘要级关联分析与多篇比较、文献研究结论与导师复核，以及按具体成果
授权的引用快照。

鉴权与错误口径与既有 supervision 模块完全一致：

- 当前用户只来自 ``require_password_changed`` 的服务端认证上下文，
  请求体里的任何 user_id 都不被采信；
- 非成员、不存在、别人的未共享草稿一律 404（不区分原因，避免枚举）；
- 项目角色不足 403（决定与结论由学生做，文献结论复核由主导师做）；
- 状态机不允许 409（未确认研究定义就创建检索任务、未确认检索方案就搜索、
  超过深度提取上限、已关闭的任务等）；
- 字段非法 400，pydantic 形状不符 422；
- **模型或外部来源没接入 → 503 + 中文说明，绝不假装跑过**。

模型与来源都按依赖注入：``get_literature_llm()`` 返回统一容量闸门覆盖下、
``capacity_mode="external"`` 的生成模型（容量由 ``get_literature_capacity()``
的适配器在仓储层按**每次实际调用**取放，消费者类型 ``research_plan`` /
``paper_extract`` / ``paper_compare``）；``get_literature_provider()`` 返回
外部论文来源 provider，默认复用论文研究模块的来源适配层（arxiv / local_kb），
没有任何来源可注入时明确报告「尚未接入」→ 503，绝不造一批假候选。测试用
``dependency_overrides`` 注入假模型与假 provider。
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Union

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from chatchat.server.api_server.api_schemas import ResponseModel
from sqlalchemy.orm import Session

from chatchat.server.auth.deps import require_password_changed
from chatchat.server.db.models.supervision_literature_model import (
    SupervisionLiteratureCitationModel,
    SupervisionLiteratureConclusionModel,
    SupervisionLiteratureEvidenceCardModel,
    SupervisionLiteratureExtractionModel,
    SupervisionLiteratureResultModel,
    SupervisionLiteratureSearchTaskModel,
    SupervisionLiteratureSubtaskModel,
)
from chatchat.server.db.models.user_model import UserModel
from chatchat.server.db.repository import supervision_literature_repository as lit
from chatchat.server.db.session import get_db
from chatchat.server.model_capacity import (
    CAPACITY_MODE_EXTERNAL,
    CONSUMER_PAPER_EXTRACT,
    CapacityLeaseAdapter,
)
from chatchat.utils import build_logger

logger = build_logger(__name__)

supervision_literature_router = APIRouter(
    prefix="/supervision", tags=["Supervision - 论文检索与文献结论"],
    dependencies=[Depends(require_password_changed)],
)

#: 论文相关的模型调用按批次取放容量用的共享适配器（无状态，可复用）。
#: 真正的消费者类型逐次传入（research_plan / paper_extract / paper_compare），
#: 这里只是漏传时的兜底。
capacity_adapter = CapacityLeaseAdapter(default_consumer=CONSUMER_PAPER_EXTRACT)

#: 模型对象按进程缓存：构造与缓存模型对象不持有任何容量。
_model_cache: dict = {}


def get_literature_llm() -> Any:
    """返回**已被统一容量闸门覆盖**的生成模型；没配好时返回 ``None``（→ 503）。

    与 AI 预审同一套接线：模型用 ``capacity_mode="external"`` 构造，容量由
    仓储层在每次实际调用前后通过 ``get_literature_capacity()`` 取放。模型
    自己不再取租约（上限 1 且不可重入，取两次必然自锁），但会在发请求前
    确认租约确实在，缺了就抛 ``ModelCapacityLeaseNotHeldError``——所以
    「由业务层取容量」不会退化成「谁都没取容量」。
    """
    cached = _model_cache.get("model")
    if cached is not None:
        return cached
    try:
        from chatchat.server.utils import get_ChatOpenAI, get_default_llm
        from chatchat.settings import Settings

        model = get_ChatOpenAI(
            model_name=get_default_llm(),
            temperature=Settings.model_settings.TEMPERATURE,
            max_tokens=Settings.model_settings.MAX_TOKENS,
            streaming=False,
            verbose=False,
            consumer=CONSUMER_PAPER_EXTRACT,
            capacity_mode=CAPACITY_MODE_EXTERNAL,
        )
    except Exception:  # 模型没配好：接口返回 503，不伪造结果
        logger.exception("构造论文检索相关生成模型失败（相关接口将返回 503）")
        return None
    if model is None:
        return None
    _model_cache["model"] = model
    return model


def reset_literature_model_cache() -> None:
    """测试与运维用：清掉模型缓存，下次调用重新构造。"""
    _model_cache.clear()


def get_literature_capacity() -> Any:
    """容量适配器：``acquire(consumer=…, business_ref=…, user_id=…)`` /
    ``release(token, reason=…)``，内部取放 ``model_capacity`` 的租约。"""
    return capacity_adapter


class ResearchSourcesLiteratureProvider:
    """把论文研究模块已有的来源适配层包成检索契约（arxiv / local_kb）。

    来源适配器是 async 的，检索接口与仓储层是同步的，所以在**独立线程里的新
    事件循环**中跑一次，绝不在请求所在的事件循环里 ``asyncio.run``（那会直接
    报错）。这一步仍然阻塞当前工作线程，与整个模块"同步执行、没有后台 worker"
    的现状一致。

    单个来源失败只抛 ``LiteratureSourceError``：仓储层把它记成该来源的失败并
    继续其它来源，界面上逐条列出，不会把一次失败伪装成"搜到了 0 篇"。
    """

    name = "research_sources"

    def __init__(self, sources: Optional[Dict[str, Any]] = None) -> None:
        if sources is None:
            from chatchat.server.research.sources import build_default_sources

            sources = build_default_sources()
        self._sources = dict(sources or {})

    def available(self) -> tuple[bool, str]:
        if not self._sources:
            return False, "没有可用的论文来源适配器"
        return True, "可用来源：" + "、".join(sorted(self._sources))

    def search(self, *, query: str, source: str, limit: int) -> list:
        name = (source or "").strip().lower()
        adapter = self._sources.get(name)
        if adapter is None:
            raise lit.LiteratureSourceError(
                f"未接入来源「{source}」；当前可用来源："
                + ("、".join(sorted(self._sources)) or "无")
            )
        try:
            return _run_source_search(adapter, query=query, limit=limit)
        except lit.LiteratureSourceError:
            raise
        except Exception as exc:  # 来源自己的报错：只说原因，来源名由上层标注
            raise lit.LiteratureSourceError(str(exc) or type(exc).__name__) from exc


def _run_source_search(adapter: Any, *, query: str, limit: int) -> list:
    """在独立线程的事件循环里跑一次 async 来源检索。"""
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    async def _call() -> list:
        return await adapter.search(query, limit)

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="literature-source") as pool:
        return list(pool.submit(asyncio.run, _call()).result() or [])


def _sources_enabled() -> bool:
    return os.environ.get("CHATCHAT_LITERATURE_ENABLE_SOURCES", "1").strip() not in (
        "0",
        "false",
        "off",
        "no",
    )


def get_literature_provider() -> Any:
    """外部论文来源 provider。

    默认复用论文研究模块的来源适配层（``chatchat/server/research/sources.py``
    的 ArXiv 与实验室论文库），仓储层签名不变。来源本身失败时按来源逐条记
    失败并继续其它来源；没有任何来源可注入时退回
    ``UnconfiguredLiteratureProvider``，检索接口据此返回 503，而不是造一批
    假候选（§16.4 第2条要求展示来源与去重，但绝不伪造命中）。

    ``CHATCHAT_LITERATURE_ENABLE_SOURCES=0`` 可强制退回"未接入"（离线演示或
    排障时用），此时检索一律 503。
    """
    if not _sources_enabled():
        return lit.UnconfiguredLiteratureProvider()
    try:
        provider = ResearchSourcesLiteratureProvider()
    except Exception:  # 来源层导入失败也不能让接口 500：退回未接入并说明原因
        logger.exception("加载论文来源适配层失败（检索将返回 503）")
        return lit.UnconfiguredLiteratureProvider()
    ok, _ = provider.available()
    return provider if ok else lit.UnconfiguredLiteratureProvider()


# ---------------------------------------------------------------------------
# 错误语义
# ---------------------------------------------------------------------------


def _not_found(detail: str = "资源不存在或无权访问") -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _forbidden(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def _unavailable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail)


def call(session: Session, action, *args, **kwargs):
    """把仓储层异常收敛成统一 HTTP 语义，并在失败时回滚。

    ``PermissionError`` 里带「角色」字样的是权限不足（403），其余按不可见
    处理（404）；``LiteratureUnavailable`` 是能力未接入（503），与
    ``supervision_research_routes.call`` 同一套口径。
    """
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
    except lit.LiteratureUnavailable as exc:
        session.rollback()
        raise _unavailable(str(exc) or "模型或外部来源能力尚未接入")
    except lit.WorkValidationError as exc:
        session.rollback()
        raise _bad_request(str(exc))
    except lit.WorkError as exc:
        session.rollback()
        raise _conflict(str(exc))


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------

StrList = List[str]


class TaskBody(BaseModel):
    """创建检索任务：必须引用已确认的研究定义版本（§4.4）。"""

    brief_ref: str = Field(min_length=1, max_length=32)
    goal: str = Field(min_length=1, max_length=4000)
    purpose: str = Field(default="verify", max_length=16)
    keywords: StrList = Field(default_factory=list)
    synonyms: StrList = Field(default_factory=list)
    exclude_terms: StrList = Field(default_factory=list)
    sources: StrList = Field(default_factory=list)
    year_from: Optional[int] = Field(default=None, ge=1900, le=2100)
    year_to: Optional[int] = Field(default=None, ge=1900, le=2100)
    require_fulltext: bool = False
    result_limit: int = Field(default=30, ge=1, le=200)
    deep_extract_limit: Optional[int] = Field(default=None, ge=0, le=20)
    as_draft: bool = False


class TaskPatchBody(BaseModel):
    goal: Optional[str] = Field(default=None, max_length=4000)
    purpose: Optional[str] = Field(default=None, max_length=16)
    keywords: Optional[StrList] = None
    synonyms: Optional[StrList] = None
    exclude_terms: Optional[StrList] = None
    sources: Optional[StrList] = None
    year_from: Optional[int] = Field(default=None, ge=1900, le=2100)
    year_to: Optional[int] = Field(default=None, ge=1900, le=2100)
    require_fulltext: Optional[bool] = None
    result_limit: Optional[int] = Field(default=None, ge=1, le=200)
    deep_extract_limit: Optional[int] = Field(default=None, ge=0, le=20)


class ReasonBody(BaseModel):
    reason: str = Field(min_length=1, max_length=4000)


class NoteBody(BaseModel):
    note: str = Field(default="", max_length=4000)


class SearchBody(BaseModel):
    """本次检索可覆盖来源与关键词批次；不传则按任务里确认过的方案。"""

    sources: Optional[StrList] = None
    keyword_batches: Optional[List[Dict[str, Any]]] = None


class DecisionBody(BaseModel):
    decision: str = Field(min_length=1, max_length=16)
    reason: str = Field(min_length=1, max_length=4000)


class AbstractAnalysisBody(BaseModel):
    result_refs: StrList = Field(default_factory=list)
    limit: Optional[int] = Field(default=None, ge=1, le=50)


class CompareBody(BaseModel):
    result_refs: StrList = Field(default_factory=list, min_length=2)


class EvidenceCardBody(BaseModel):
    result_ref: str = Field(min_length=1, max_length=32)
    quote: str = Field(min_length=1, max_length=4000)
    evidence_type: str = Field(default="", max_length=32)
    summary: str = Field(default="", max_length=2000)
    locator: Dict[str, Any] = Field(default_factory=dict)
    support_relation: str = Field(default="", max_length=24)
    brief_field: str = Field(default="", max_length=32)
    sub_question: str = Field(default="", max_length=500)
    level: str = Field(default="", max_length=16)
    extraction_id: str = Field(default="", max_length=32)


class ConclusionBody(BaseModel):
    """文献结论：论文事实 / 学生判断 / 待验证假设三类必须分开（§4.5）。"""

    statement: str = Field(min_length=1, max_length=4000)
    supported_facts: List[Dict[str, Any]] = Field(default_factory=list)
    student_judgements: List[Dict[str, Any]] = Field(default_factory=list)
    unverified_hypotheses: List[Dict[str, Any]] = Field(default_factory=list)
    research_gaps: StrList = Field(default_factory=list)
    evidence_card_codes: StrList = Field(default_factory=list)
    task_ref: str = Field(default="", max_length=32)
    confirm: bool = False


class MentorReviewBody(BaseModel):
    state: str = Field(min_length=1, max_length=24)
    note: str = Field(default="", max_length=4000)


class CitationBody(BaseModel):
    source_kind: str = Field(min_length=1, max_length=32)
    source_ref: str = Field(min_length=1, max_length=32)
    purpose: str = Field(min_length=1, max_length=32)
    bound_object_type: str = Field(default="", max_length=32)
    bound_object_code: str = Field(default="", max_length=32)
    note: str = Field(default="", max_length=2000)


# ---------------------------------------------------------------------------
# 响应模型（字段与仓储层 payload 一一对应；多余键忽略）
# ---------------------------------------------------------------------------


class StrictOut(ResponseModel):
    model_config = ConfigDict(extra="ignore")


class SubtaskOut(StrictOut):
    subtask_id: str
    code: str
    kind: str
    status: str
    attempt: int = 1
    source: str = ""
    query: str = ""
    payload: Dict[str, Any] = Field(default_factory=dict)
    result_total: int = 0
    new_total: int = 0
    duplicate_total: int = 0
    error_code: str = ""
    error_message: str = ""
    failure_history: List[Dict[str, Any]] = Field(default_factory=list)
    model_calls: List[Dict[str, Any]] = Field(default_factory=list)
    consumer: str = ""
    model_name: str = ""
    prompt_version: str = ""
    queued_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    cancelled_by: str = ""
    cancelled_at: Optional[datetime] = None
    cancel_reason: str = ""
    created_by: str = ""
    created_role: str = ""
    create_time: Optional[datetime] = None
    update_time: Optional[datetime] = None


class TaskOut(StrictOut):
    task_id: str
    code: str
    project_id: str
    status: str
    purpose: str
    goal: str
    keywords: StrList = Field(default_factory=list)
    synonyms: StrList = Field(default_factory=list)
    exclude_terms: StrList = Field(default_factory=list)
    sources: StrList = Field(default_factory=list)
    year_from: Optional[int] = None
    year_to: Optional[int] = None
    require_fulltext: bool = False
    result_limit: int = 30
    deep_extract_limit: int = 0
    brief_id: str
    brief_code: str
    brief_version: int
    brief_kind: str = ""
    brief_confirmed_by: str = ""
    brief_confirmed_at: Optional[datetime] = None
    brief_snapshot: Dict[str, Any] = Field(default_factory=dict)
    brief_superseded: bool = False
    brief_shared_version: Optional[int] = None
    brief_latest_version: Optional[int] = None
    search_plan: Dict[str, Any] = Field(default_factory=dict)
    search_plan_note: str = ""
    search_plan_model: str = ""
    search_plan_prompt_version: str = ""
    search_plan_generated_at: Optional[datetime] = None
    search_plan_confirmed_at: Optional[datetime] = None
    search_plan_confirmed_by: str = ""
    plan_status: str = ""
    plan_error: str = ""
    hits_total: int = 0
    deduped_total: int = 0
    stored_total: int = 0
    source_failures: List[Dict[str, Any]] = Field(default_factory=list)
    searched_at: Optional[datetime] = None
    concluded_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    close_reason: str = ""
    created_by: str = ""
    created_role: str = ""
    create_time: Optional[datetime] = None
    update_time: Optional[datetime] = None
    result_total: int = 0
    included_total: int = 0
    excluded_total: int = 0
    read_later_total: int = 0
    undecided_total: int = 0
    deep_extract_used: int = 0
    conclusion_total: int = 0
    subtask_total: int = 0
    subtask_failed_total: int = 0
    recent_subtasks: List[SubtaskOut] = Field(default_factory=list)


class EvidenceCardOut(StrictOut):
    card_id: str
    code: str
    project_id: str
    result_id: str
    result_code: str
    extraction_id: Optional[str] = None
    extraction_version: int = 0
    evidence_type: str = ""
    summary: str = ""
    quote: str
    locator: Dict[str, Any] = Field(default_factory=dict)
    quote_verified: bool = False
    level: str = ""
    support_relation: str = ""
    brief_field: str = ""
    sub_question: str = ""
    created_by: str = ""
    created_role: str = ""
    create_time: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class ResultOut(StrictOut):
    result_id: str
    code: str
    project_id: str
    search_task_id: str
    subtask_id: str = ""
    dedup_key: str
    doi: Optional[str] = None
    arxiv_id: Optional[str] = None
    title_key: Optional[str] = None
    title: str
    authors: str = ""
    year: Optional[int] = None
    abstract: str = ""
    url: Optional[str] = None
    source: str = ""
    sources: List[Dict[str, Any]] = Field(default_factory=list)
    fulltext_status: str = "unknown"
    relevance_level: str = "reference"
    relevance_reason: str = ""
    relevance_matched: List[Dict[str, Any]] = Field(default_factory=list)
    relevance_missing: List[Dict[str, Any]] = Field(default_factory=list)
    relevance_score_source: str = "rule_keyword"
    relevance_score: Optional[int] = None
    relevance_rules_version: str = ""
    recommended_action: str = "reference"
    semantic_analysis: Dict[str, Any] = Field(default_factory=dict)
    decision: str = "pending"
    decision_reason: str = ""
    decided_by: str = ""
    decided_role: str = ""
    decided_at: Optional[datetime] = None
    decision_history: List[Dict[str, Any]] = Field(default_factory=list)
    extract_state: str = "none"
    evidence_cards: List[EvidenceCardOut] = Field(default_factory=list)
    latest_extraction_version: int = 0
    first_seen_at: Optional[datetime] = None
    create_time: Optional[datetime] = None
    update_time: Optional[datetime] = None


class ExtractionOut(StrictOut):
    extraction_id: str
    project_id: str
    result_id: str
    result_code: str
    version: int
    subtask_id: str = ""
    subtask_code: str = ""
    level: str = ""
    summary: str = ""
    research_problem: str = ""
    method: str = ""
    algorithm: str = ""
    experiments: str = ""
    results_metrics: str = ""
    limitations: str = ""
    differences: Dict[str, Any] = Field(default_factory=dict)
    evidence: List[Dict[str, Any]] = Field(default_factory=list)
    model_name: str = ""
    prompt_version: str = ""
    created_by: str = ""
    created_role: str = ""
    create_time: Optional[datetime] = None
    superseded_at: Optional[datetime] = None


class ConclusionOut(StrictOut):
    conclusion_id: str
    code: str
    version: int
    project_id: str
    search_task_id: Optional[str] = None
    search_task_code: str = ""
    statement: str
    supported_facts: List[Dict[str, Any]] = Field(default_factory=list)
    student_judgements: List[Dict[str, Any]] = Field(default_factory=list)
    unverified_hypotheses: List[Dict[str, Any]] = Field(default_factory=list)
    research_gaps: StrList = Field(default_factory=list)
    literature_snapshot: List[Dict[str, Any]] = Field(default_factory=list)
    literature_snapshot_hash: str = ""
    literature_count: int = 0
    evidence_cards: List[Dict[str, Any]] = Field(default_factory=list)
    status: str
    confirmed_by: str = ""
    confirmed_at: Optional[datetime] = None
    confirm_note: str = ""
    mentor_state: str = ""
    mentor_note: str = ""
    mentor_by: str = ""
    mentor_role: str = ""
    mentor_at: Optional[datetime] = None
    superseded_at: Optional[datetime] = None
    superseded_by_code: str = ""
    superseded_by_version: int = 0
    literature_changed: bool = False
    current_included_count: int = 0
    created_by: str = ""
    created_role: str = ""
    create_time: Optional[datetime] = None
    update_time: Optional[datetime] = None


class CitationOut(StrictOut):
    citation_id: str
    code: str
    project_id: str
    source_kind: str
    source_ref: str
    source_code: str = ""
    source_owner_id: str
    purpose: str
    bound_object_type: str = ""
    bound_object_code: str = ""
    snapshot: Dict[str, Any] = Field(default_factory=dict)
    source_version: str = ""
    content_hash: str = ""
    granted_by: str = ""
    granted_role: str = ""
    granted_at: Optional[datetime] = None
    note: str = ""
    revoked_at: Optional[datetime] = None
    revoked_by: str = ""
    revoke_reason: str = ""
    create_time: Optional[datetime] = None


# ---------------------------------------------------------------------------
# 序列化
# ---------------------------------------------------------------------------


def _task_out(session: Session, task: SupervisionLiteratureSearchTaskModel) -> TaskOut:
    return TaskOut(**lit.task_payload(session, task))


def _subtask_out(row: SupervisionLiteratureSubtaskModel) -> SubtaskOut:
    return SubtaskOut(**lit.subtask_payload(row))


def _result_out(session: Session, row: SupervisionLiteratureResultModel) -> ResultOut:
    return ResultOut(**lit.result_payload(session, row))


def _extraction_out(row: SupervisionLiteratureExtractionModel) -> ExtractionOut:
    return ExtractionOut(**lit.extraction_payload(row))


def _card_out(row: SupervisionLiteratureEvidenceCardModel) -> EvidenceCardOut:
    return EvidenceCardOut(**lit.evidence_card_payload(row))


def _conclusion_out(
    session: Session, row: SupervisionLiteratureConclusionModel
) -> ConclusionOut:
    return ConclusionOut(**lit.conclusion_payload(session, row))


def _citation_out(row: SupervisionLiteratureCitationModel) -> CitationOut:
    return CitationOut(**lit.citation_payload(row))


# ---------------------------------------------------------------------------
# 检索任务（LS）
# ---------------------------------------------------------------------------


@supervision_literature_router.post(
    "/projects/{project_ref}/literature/tasks", response_model=TaskOut
)
def create_task(
    project_ref: str,
    body: TaskBody = Body(...),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    """创建论文检索任务；**只有已确认并共享的研究定义版本**能作为依据。

    引用只有草稿的研究定义 → 409，错误信息说明为什么不能创建以及下一步该
    做什么（§4.3.1 确认门槛、§16.4 第1条）。
    """
    task = call(
        session, lit.create_task, project_ref, user.id,
        brief_ref=body.brief_ref, goal=body.goal, purpose=body.purpose,
        keywords=body.keywords, synonyms=body.synonyms,
        exclude_terms=body.exclude_terms, sources=body.sources,
        year_from=body.year_from, year_to=body.year_to,
        require_fulltext=body.require_fulltext, result_limit=body.result_limit,
        deep_extract_limit=body.deep_extract_limit, as_draft=body.as_draft,
    )
    return _task_out(session, task)


@supervision_literature_router.get(
    "/projects/{project_ref}/literature/tasks", response_model=List[TaskOut]
)
def list_tasks(
    project_ref: str,
    status_filter: str = Query(default="", alias="status", max_length=16),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    rows = call(session, lit.list_tasks, project_ref, user.id, status=status_filter)
    return [_task_out(session, row) for row in rows]


@supervision_literature_router.get(
    "/projects/{project_ref}/literature/tasks/{task_ref}", response_model=TaskOut
)
def get_task(
    project_ref: str,
    task_ref: str,
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    task = call(session, lit.get_task, project_ref, user.id, task_ref)
    return _task_out(session, task)


@supervision_literature_router.patch(
    "/projects/{project_ref}/literature/tasks/{task_ref}", response_model=TaskOut
)
def update_task(
    project_ref: str,
    task_ref: str,
    body: TaskPatchBody = Body(...),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    """修改检索输入。已经开始检索或已关闭的任务不能改（依据必须固定）。"""
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    task = call(session, lit.update_task, project_ref, user.id, task_ref, fields)
    return _task_out(session, task)


@supervision_literature_router.post(
    "/projects/{project_ref}/literature/tasks/{task_ref}/close", response_model=TaskOut
)
def close_task(
    project_ref: str,
    task_ref: str,
    body: ReasonBody = Body(...),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    task = call(session, lit.close_task, project_ref, user.id, task_ref, reason=body.reason)
    return _task_out(session, task)


# ---------------------------------------------------------------------------
# 检索方案：短调用生成 + 学生确认（§6.3.1 第1步）
# ---------------------------------------------------------------------------


@supervision_literature_router.post(
    "/projects/{project_ref}/literature/tasks/{task_ref}/plan", response_model=TaskOut
)
def generate_search_plan(
    project_ref: str,
    task_ref: str,
    body: NoteBody = Body(default_factory=NoteBody),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
    llm=Depends(get_literature_llm),
    capacity=Depends(get_literature_capacity),
):
    """用一次短调用生成检索方案（消费者 ``research_plan``，走统一容量闸门）。

    模型未接入 → 503；模型失败只把该子任务标 failed，任务与已有结果不受影响。
    重新生成会清掉上一次的确认，必须重新确认才能搜索。
    """
    task = call(
        session, lit.generate_search_plan, project_ref, user.id, task_ref,
        llm=llm, capacity=capacity, note=body.note,
    )
    return _task_out(session, task)


@supervision_literature_router.post(
    "/projects/{project_ref}/literature/tasks/{task_ref}/plan/confirm",
    response_model=TaskOut,
)
def confirm_search_plan(
    project_ref: str,
    task_ref: str,
    body: NoteBody = Body(default_factory=NoteBody),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    """学生确认检索方案；未确认不能开始搜索（409）。"""
    task = call(
        session, lit.confirm_search_plan, project_ref, user.id, task_ref, note=body.note,
    )
    return _task_out(session, task)


# ---------------------------------------------------------------------------
# 搜索与去重：按来源 × 关键词批次拆可独立重试的子任务
# ---------------------------------------------------------------------------


@supervision_literature_router.post(
    "/projects/{project_ref}/literature/tasks/{task_ref}/search", response_model=TaskOut
)
def run_search(
    project_ref: str,
    task_ref: str,
    body: SearchBody = Body(default_factory=SearchBody),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
    provider=Depends(get_literature_provider),
):
    """执行检索：显示批次、来源、命中、去重与失败来源，不隐藏检索过程。

    来源由注入的 provider 决定（默认复用论文研究模块的 arxiv / local_kb）；
    没有任何可用来源时 503，单个来源失败只标该来源。检索**不会**自动深度
    提取任何候选论文（§16.4 第3条）。
    """
    task = call(
        session, lit.run_search, project_ref, user.id, task_ref,
        provider=provider, sources=body.sources, keyword_batches=body.keyword_batches,
    )
    return _task_out(session, task)


@supervision_literature_router.get(
    "/projects/{project_ref}/literature/tasks/{task_ref}/subtasks",
    response_model=List[SubtaskOut],
)
def list_subtasks(
    project_ref: str,
    task_ref: str,
    kind: str = Query(default="", max_length=32),
    status_filter: str = Query(default="", alias="status", max_length=16),
    limit: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    rows = call(
        session, lit.list_subtasks, project_ref, user.id, task_ref,
        kind=kind, status=status_filter, limit=limit,
    )
    return [_subtask_out(row) for row in rows]


@supervision_literature_router.post(
    "/projects/{project_ref}/literature/tasks/{task_ref}/subtasks/{subtask_ref}/retry",
    response_model=SubtaskOut,
)
def retry_subtask(
    project_ref: str,
    task_ref: str,
    subtask_ref: str,
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
    provider=Depends(get_literature_provider),
    llm=Depends(get_literature_llm),
    capacity=Depends(get_literature_capacity),
):
    """重试单个失败/已取消的子任务；只重跑它自己，其它子任务状态不动。"""
    row = call(
        session, lit.retry_subtask, project_ref, user.id, task_ref, subtask_ref,
        provider=provider, llm=llm, capacity=capacity,
    )
    return _subtask_out(row)


@supervision_literature_router.post(
    "/projects/{project_ref}/literature/tasks/{task_ref}/subtasks/{subtask_ref}/cancel",
    response_model=SubtaskOut,
)
def cancel_subtask(
    project_ref: str,
    task_ref: str,
    subtask_ref: str,
    body: NoteBody = Body(default_factory=NoteBody),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    row = call(
        session, lit.cancel_subtask, project_ref, user.id, task_ref, subtask_ref,
        reason=body.note,
    )
    return _subtask_out(row)


# ---------------------------------------------------------------------------
# 候选论文：列表、决定、深度提取、证据卡片
# ---------------------------------------------------------------------------


@supervision_literature_router.get(
    "/projects/{project_ref}/literature/results", response_model=List[ResultOut]
)
def list_results(
    project_ref: str,
    task_ref: str = Query(default="", max_length=32),
    decision: str = Query(default="", max_length=16),
    relevance: str = Query(default="", max_length=16),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    """按关联等级列出候选论文，含来源、去重合并、全文可用性与可解释理由。"""
    rows = call(
        session, lit.list_results, project_ref, user.id, task_ref=task_ref,
        decision=decision, relevance=relevance, limit=limit, offset=offset,
    )
    return [_result_out(session, row) for row in rows]


@supervision_literature_router.get(
    "/projects/{project_ref}/literature/results/{result_ref}", response_model=ResultOut
)
def get_result(
    project_ref: str,
    result_ref: str,
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    row = call(session, lit.get_result, project_ref, user.id, result_ref)
    return _result_out(session, row)


@supervision_literature_router.post(
    "/projects/{project_ref}/literature/results/{result_ref}/decision",
    response_model=ResultOut,
)
def decide_result(
    project_ref: str,
    result_ref: str,
    body: DecisionBody = Body(...),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    """学生决定纳入 / 排除 / 稍后阅读，必须写理由；导师调用本接口 → 403。"""
    row = call(
        session, lit.decide_result, project_ref, user.id, result_ref,
        decision=body.decision, reason=body.reason,
    )
    return _result_out(session, row)


@supervision_literature_router.post(
    "/projects/{project_ref}/literature/results/{result_ref}/deep-extract",
    response_model=ResultOut,
)
def run_deep_extract(
    project_ref: str,
    result_ref: str,
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
    llm=Depends(get_literature_llm),
    capacity=Depends(get_literature_capacity),
):
    """学生选篇后才做深度提取（消费者 ``paper_extract``，受任务深度上限约束）。

    单篇失败只让这一篇 ``extract_state=failed``，不动其它论文（§15.3 第6条）。
    """
    row = call(
        session, lit.run_deep_extract, project_ref, user.id, result_ref,
        llm=llm, capacity=capacity,
    )
    return _result_out(session, row)


@supervision_literature_router.get(
    "/projects/{project_ref}/literature/results/{result_ref}/extraction",
    response_model=Optional[ExtractionOut],
)
def get_extraction(
    project_ref: str,
    result_ref: str,
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    """最新一版深度提取结果（含原文定位）；没提取过返回 ``null``。"""
    row = call(session, lit.get_extraction, project_ref, user.id, result_ref)
    return _extraction_out(row) if row else None


@supervision_literature_router.get(
    "/projects/{project_ref}/literature/results/{result_ref}/extractions",
    response_model=List[ExtractionOut],
)
def list_extractions(
    project_ref: str,
    result_ref: str,
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    """全部提取版本（新→旧）：重跑不覆盖旧版本（§13）。"""
    rows = call(session, lit.list_extractions, project_ref, user.id, result_ref)
    return [_extraction_out(row) for row in rows]


@supervision_literature_router.post(
    "/projects/{project_ref}/literature/evidence-cards", response_model=EvidenceCardOut
)
def create_evidence_card(
    project_ref: str,
    body: EvidenceCardBody = Body(...),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    """把一条带原文定位的证据加成证据卡片；重复提交同一证据不产生重复卡片。"""
    card = call(
        session, lit.create_evidence_card, project_ref, user.id,
        result_ref=body.result_ref, quote=body.quote,
        evidence_type=body.evidence_type, summary=body.summary,
        locator=body.locator, support_relation=body.support_relation,
        brief_field=body.brief_field, sub_question=body.sub_question,
        level=body.level, extraction_id=body.extraction_id,
    )
    return _card_out(card)


@supervision_literature_router.get(
    "/projects/{project_ref}/literature/evidence-cards", response_model=List[EvidenceCardOut]
)
def list_evidence_cards(
    project_ref: str,
    result_ref: str = Query(default="", max_length=32),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    rows = call(session, lit.list_evidence_cards, project_ref, user.id, result_ref=result_ref)
    return [_card_out(row) for row in rows]


# ---------------------------------------------------------------------------
# 摘要级关联分析与多篇比较（有上限，消费者 paper_compare）
# ---------------------------------------------------------------------------


@supervision_literature_router.post(
    "/projects/{project_ref}/literature/tasks/{task_ref}/abstract-analysis",
    response_model=SubtaskOut,
)
def run_abstract_analysis(
    project_ref: str,
    task_ref: str,
    body: AbstractAnalysisBody = Body(default_factory=AbstractAnalysisBody),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
    llm=Depends(get_literature_llm),
    capacity=Depends(get_literature_capacity),
):
    """有上限的摘要级关联分析：只对排序靠前的有限候选做（§6.3.1 第3步）。

    结果写进 ``semantic_analysis`` 并明确标「AI语义分析」，不覆盖规则排序
    字段，也不包装成五维精确分数（§6.3.B）。
    """
    row = call(
        session, lit.run_abstract_analysis, project_ref, user.id, task_ref,
        llm=llm, capacity=capacity, result_refs=body.result_refs, limit=body.limit,
    )
    return _subtask_out(row)


@supervision_literature_router.post(
    "/projects/{project_ref}/literature/tasks/{task_ref}/compare",
    response_model=SubtaskOut,
)
def compare_papers(
    project_ref: str,
    task_ref: str,
    body: CompareBody = Body(...),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
    llm=Depends(get_literature_llm),
    capacity=Depends(get_literature_capacity),
):
    """多篇比较：一次最多 6 篇，避免一次误操作堆出无上限后台任务。"""
    row = call(
        session, lit.compare_papers, project_ref, user.id, task_ref,
        result_refs=body.result_refs, llm=llm, capacity=capacity,
    )
    return _subtask_out(row)


# ---------------------------------------------------------------------------
# 文献研究结论（LC）
# ---------------------------------------------------------------------------


@supervision_literature_router.post(
    "/projects/{project_ref}/literature/conclusions", response_model=ConclusionOut
)
def create_conclusion(
    project_ref: str,
    body: ConclusionBody = Body(...),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    """从当前已纳入集合形成文献研究结论，绑定文献快照与哈希。"""
    row = call(
        session, lit.create_conclusion, project_ref, user.id,
        statement=body.statement, supported_facts=body.supported_facts,
        student_judgements=body.student_judgements,
        unverified_hypotheses=body.unverified_hypotheses,
        research_gaps=body.research_gaps,
        evidence_card_codes=body.evidence_card_codes,
        task_ref=body.task_ref, confirm=body.confirm,
    )
    return _conclusion_out(session, row)


@supervision_literature_router.get(
    "/projects/{project_ref}/literature/conclusions", response_model=List[ConclusionOut]
)
def list_conclusions(
    project_ref: str,
    status_filter: str = Query(default="", alias="status", max_length=16),
    include_superseded: bool = Query(default=False),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    rows = call(
        session, lit.list_conclusions, project_ref, user.id, status=status_filter,
        include_superseded=include_superseded,
    )
    return [_conclusion_out(session, row) for row in rows]


@supervision_literature_router.get(
    "/projects/{project_ref}/literature/conclusions/{conclusion_ref}",
    response_model=ConclusionOut,
)
def get_conclusion(
    project_ref: str,
    conclusion_ref: str,
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    row = call(session, lit.get_conclusion, project_ref, user.id, conclusion_ref)
    return _conclusion_out(session, row)


@supervision_literature_router.post(
    "/projects/{project_ref}/literature/conclusions/{conclusion_ref}/revise",
    response_model=ConclusionOut,
)
def revise_conclusion(
    project_ref: str,
    conclusion_ref: str,
    body: ConclusionBody = Body(...),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    """修订文献结论：新版本 + 新快照，旧版本标 ``superseded`` 保留（§13）。"""
    row = call(
        session, lit.revise_conclusion, project_ref, user.id, conclusion_ref,
        statement=body.statement, supported_facts=body.supported_facts,
        student_judgements=body.student_judgements,
        unverified_hypotheses=body.unverified_hypotheses,
        research_gaps=body.research_gaps,
        evidence_card_codes=body.evidence_card_codes, confirm=body.confirm,
    )
    return _conclusion_out(session, row)


@supervision_literature_router.post(
    "/projects/{project_ref}/literature/conclusions/{conclusion_ref}/mentor-review",
    response_model=ConclusionOut,
)
def mentor_review_conclusion(
    project_ref: str,
    conclusion_ref: str,
    body: MentorReviewBody = Body(...),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    """导师确认 / 质疑 / 要求补充证据；学生调用 → 403。"""
    row = call(
        session, lit.mentor_review_conclusion, project_ref, user.id, conclusion_ref,
        state=body.state, note=body.note,
    )
    return _conclusion_out(session, row)


# ---------------------------------------------------------------------------
# 引用快照（CS）：按具体成果授权，固定当时字段
# ---------------------------------------------------------------------------


@supervision_literature_router.post(
    "/projects/{project_ref}/literature/citations", response_model=CitationOut
)
def create_citation(
    project_ref: str,
    body: CitationBody = Body(...),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    """授权引用快照：复制或固定授权当时可见的字段与内容哈希（§9.1）。

    私人论文研究成果必须证明该成果属于当前用户，别人的私人材料一律 404；
    导师只读得到这份快照，不能顺着对象关系浏览学生其它私人搜索。
    """
    row = call(
        session, lit.create_citation, project_ref, user.id,
        source_kind=body.source_kind, source_ref=body.source_ref,
        purpose=body.purpose, bound_object_type=body.bound_object_type,
        bound_object_code=body.bound_object_code, note=body.note,
    )
    return _citation_out(row)


@supervision_literature_router.get(
    "/projects/{project_ref}/literature/citations", response_model=List[CitationOut]
)
def list_citations(
    project_ref: str,
    purpose: str = Query(default="", max_length=32),
    source_kind: str = Query(default="", max_length=32),
    include_revoked: bool = Query(default=True),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    rows = call(
        session, lit.list_citations, project_ref, user.id, purpose=purpose,
        source_kind=source_kind, include_revoked=include_revoked,
    )
    return [_citation_out(row) for row in rows]


@supervision_literature_router.get(
    "/projects/{project_ref}/literature/citations/{citation_ref}",
    response_model=CitationOut,
)
def get_citation(
    project_ref: str,
    citation_ref: str,
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    row = call(session, lit.get_citation, project_ref, user.id, citation_ref)
    return _citation_out(row)


@supervision_literature_router.post(
    "/projects/{project_ref}/literature/citations/{citation_ref}/revoke",
    response_model=CitationOut,
)
def revoke_citation(
    project_ref: str,
    citation_ref: str,
    body: ReasonBody = Body(...),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    """撤销未来的引用权限；已进入审核历史的快照与审计记录继续保留（§9.1）。"""
    row = call(
        session, lit.revoke_citation, project_ref, user.id, citation_ref,
        reason=body.reason,
    )
    return _citation_out(row)


__all__ = [
    "get_literature_capacity",
    "get_literature_llm",
    "get_literature_provider",
    "reset_literature_model_cache",
    "supervision_literature_router",
]
