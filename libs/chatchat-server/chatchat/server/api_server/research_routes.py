"""论文研究 API 路由（任务 017）。

所有接口要求登录（``require_password_changed``），owner 只取自服务端
认证上下文；非本人/不存在的研究资源统一 404（不区分原因，避免枚举），
状态不允许的操作返回 409，字段非法返回 400。

- 项目：``POST/GET/PATCH/DELETE /research/projects``；
- 运行：``POST /research/projects/{pid}/runs`` 创建；
  ``POST /research/runs/{rid}/plan`` 生成计划（挂起等待确认）；
  ``PUT  /research/runs/{rid}/plan`` 确认前编辑计划；
  ``POST /research/runs/{rid}/confirm`` 确认并执行（服务端状态转换 +
  继续研究图）；``POST .../resume`` 中断恢复；``POST .../cancel`` 取消；
  ``GET  /research/runs/{rid}`` 查看运行（计划/阶段/来源统计/摘要）；
- 论文：``GET /research/projects/{pid}/papers`` 查看候选论文；
  ``PATCH /research/papers/{ppid}/screening`` 标记待审/纳入/排除。

任务 018 阶段 A 追加（原文与证据，全部 owner 校验）：

- ``POST /research/projects/{pid}/papers/{paper_id}/fulltext`` 解析并入库
  该论文在本项目内的原文单元（幂等；取不到全文自动降级为摘要级单元）；
- ``GET  /research/projects/{pid}/papers/{paper_id}/units`` 查看原文单元；
- ``POST /research/projects/{pid}/papers/{paper_id}/evidence`` 按子问题提取
  证据（原文逐字核对，核对不上的条目被拒绝并如实返回原因）；
- ``GET  /research/projects/{pid}/evidence`` 证据列表（可按论文/子问题/
  审核状态过滤）；
- ``PATCH /research/evidence/{eid}`` 确认/编辑/驳回（编辑原文必须仍能在
  该论文原文中核对）；``DELETE /research/evidence/{eid}`` 删除；
- ``POST /research/projects/{pid}/evidence/reverify`` 按当前原文重新核对。

任务 018 阶段 B 追加（比较、冲突、覆盖与有界补充检索）：

- ``GET/PUT /research/projects/{pid}/dimensions`` 查看/设置比较维度；
- ``POST /research/projects/{pid}/comparisons`` 按维度生成比较单元并检测
  冲突（只以真实证据为依据，无效引用被拒绝）；
- ``GET  /research/projects/{pid}/comparisons`` 比较矩阵；
- ``GET  /research/projects/{pid}/conflicts`` 冲突列表（两侧都可反查证据）；
- ``PATCH /research/conflicts/{cid}`` 人工裁决；``DELETE`` 删除；
- ``GET  /research/projects/{pid}/coverage`` 证据覆盖与补充检索建议；
- ``POST /research/runs/{rid}/supplement`` 基于上一轮创建补充检索新运行
  （仍需确认执行，轮次有上限）。

任务 018 阶段 C 追加（版本化报告与导出）：

- ``POST /research/projects/{pid}/reports`` 生成报告新版本；
- ``GET  /research/projects/{pid}/reports`` 报告版本列表；
- ``GET  /research/reports/{rid}`` 报告详情（含引用映射，可反查证据）；
- ``PATCH /research/reports/{rid}`` 保存编辑 → 新版本（旧版本不覆盖）；
- ``DELETE /research/reports/{rid}`` 删除单个版本；
- ``GET /research/reports/{rid}/export.md`` 导出 Markdown（引用渲染为脚注）；
- ``GET /research/reports/{rid}/export.bib`` 导出 BibTeX（仅真实论文元数据）。

任务 018 阶段 E 追加（证据闭环串联，复用同一研究运行）：

- ``POST /research/runs/{rid}/artifacts`` 用户纳入论文后，基于该运行继续
  读取原文→提取证据→等待证据复核→覆盖/冲突分析→比较→报告→等待报告
  复核；检索与入库按阶段跳过，不重复联网检索、不重复入库；
- 两个人工复核门禁复用 ``awaiting_confirmation`` 状态（``stage`` 表明复核
  什么），继续执行仍走 ``POST /research/runs/{rid}/confirm``。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from chatchat.server.auth.deps import require_password_changed
from chatchat.server.db.models.user_model import UserModel
from chatchat.server.db.repository import research_repository as repo
from chatchat.server.db.session import get_db

research_router = APIRouter(
    prefix="/research",
    tags=["论文研究"],
    dependencies=[Depends(require_password_changed)],
)


class ProjectCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200, description="项目名称")
    research_question: str = Field(..., min_length=1, max_length=2000, description="研究问题")
    scope: str = Field("", max_length=2000, description="研究范围/约束")


class ProjectUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    research_question: Optional[str] = Field(None, min_length=1, max_length=2000)
    scope: Optional[str] = Field(None, max_length=2000)
    status: Optional[str] = Field(None, description="active/archived")


class PlanUpdate(BaseModel):
    plan: Dict[str, Any] = Field(..., description="研究计划（summary/sub_questions/sources）")


class ScreeningUpdate(BaseModel):
    screening_status: str = Field(..., description="pending_review/included/excluded")
    reason: str = Field("", max_length=1000, description="纳入/排除原因")


class FullTextRequest(BaseModel):
    run_id: Optional[str] = Field(None, description="关联的研究运行（可空）")
    kb_names: Optional[List[str]] = Field(
        None, description="限定用于取全文的公共知识库（默认全部）"
    )


class EvidenceExtractRequest(BaseModel):
    sub_question_id: str = Field(..., min_length=1, max_length=64, description="子问题ID")
    sub_question: str = Field(..., min_length=1, max_length=500, description="子问题文本")
    question: Optional[str] = Field(None, max_length=2000, description="研究问题（默认取项目）")
    run_id: Optional[str] = Field(None, description="关联的研究运行（可空）")
    model: Optional[str] = Field(None, max_length=200, description="提取用模型（默认默认 LLM）")


class EvidenceReview(BaseModel):
    review_status: str = Field(..., description="confirmed/edited/rejected")
    summary: Optional[str] = Field(None, max_length=2000, description="修改后的概括")
    quote: Optional[str] = Field(None, max_length=4000, description="修改后的原文")
    note: str = Field("", max_length=1000, description="审核备注")


class DimensionItem(BaseModel):
    id: str = Field(..., min_length=1, max_length=64, description="维度ID")
    label: str = Field("", max_length=200, description="维度显示名")


class DimensionsUpdate(BaseModel):
    dimensions: List[DimensionItem] = Field(
        default_factory=list, description="比较维度列表（空=恢复默认）"
    )


class ComparisonGenerateRequest(BaseModel):
    run_id: Optional[str] = Field(None, description="关联的研究运行（可空）")
    dimension_ids: Optional[List[str]] = Field(None, description="限定生成的维度")
    model: Optional[str] = Field(None, max_length=200, description="生成用模型")
    detect_conflicts: bool = Field(True, description="同时检测结论冲突")


class ConflictResolve(BaseModel):
    resolution: str = Field(..., min_length=1, max_length=1000, description="裁决说明")


class ReportGenerateRequest(BaseModel):
    run_id: Optional[str] = Field(None, description="依据的研究运行（默认最近一次）")
    model: Optional[str] = Field(None, max_length=200, description="生成用模型")


class ReportEdit(BaseModel):
    content: str = Field(..., min_length=1, description="编辑后的报告正文（Markdown）")
    title: Optional[str] = Field(None, max_length=500, description="可选的新标题")


def _iso(value) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _project_out(p) -> dict:
    return {
        "project_id": p.id,
        "name": p.name,
        "research_question": p.research_question,
        "scope": p.scope,
        "status": p.status,
        "current_stage": p.current_stage,
        "create_time": _iso(p.create_time),
        "update_time": _iso(p.update_time),
    }


def _run_out(r) -> dict:
    def _loads(value, default):
        try:
            return json.loads(value) if value else default
        except (json.JSONDecodeError, TypeError):
            return default

    return {
        "run_id": r.id,
        "project_id": r.project_id,
        "status": r.status,
        "stage": r.stage,
        "plan": _loads(r.plan_json, {}),
        "source_stats": _loads(r.sources_json, []),
        "summary": _loads(r.summary_json, {}),
        "agent_run_id": r.agent_run_id,
        "error": r.error,
        "create_time": _iso(r.create_time),
        "start_time": _iso(r.start_time),
        "end_time": _iso(r.end_time),
    }


def _paper_out(pp, paper) -> dict:
    try:
        sources = json.loads(paper.sources_json or "[]")
    except (json.JSONDecodeError, TypeError):
        sources = []
    return {
        "project_paper_id": pp.id,
        "paper_id": paper.id,
        "title": paper.title,
        "authors": paper.authors,
        "year": paper.year,
        "abstract": paper.abstract,
        "doi": paper.doi,
        "arxiv_id": paper.arxiv_id,
        "url": paper.url,
        "source": pp.source or paper.source,
        "sources": sources,
        "fulltext_status": paper.fulltext_status,
        "relevance": pp.relevance,
        "screening_status": pp.screening_status,
        "screening_reason": pp.screening_reason,
        "first_run_id": pp.first_run_id,
        # 本项目内该论文的原文可得性（私有，不影响公开 paper_record）
        "fulltext_state": pp.fulltext_state,
        "unit_count": pp.unit_count,
        "fulltext_time": _iso(pp.fulltext_time),
        "create_time": _iso(pp.create_time),
    }


def _unit_out(u) -> dict:
    return {
        "unit_id": u.id,
        "paper_id": u.paper_id,
        "unit_kind": u.unit_kind,
        "page": u.page,
        "section": u.section,
        "block_index": u.block_index,
        "text": u.text,
        "char_count": u.char_count,
        "source_kind": u.source_kind,
        "create_time": _iso(u.create_time),
    }


def _evidence_out(c) -> dict:
    try:
        locator = json.loads(c.locator_json) if c.locator_json else {}
    except (json.JSONDecodeError, TypeError):
        locator = {}
    return {
        "evidence_id": c.id,
        "project_id": c.project_id,
        "paper_id": c.paper_id,
        "run_id": c.run_id,
        "sub_question_id": c.sub_question_id,
        "evidence_type": c.evidence_type,
        "summary": c.summary,
        "quote": c.quote,
        "level": c.level,
        "quote_verified": bool(c.quote_verified),
        "unit_id": c.unit_id,
        "locator": locator,
        "source_kind": c.source_kind,
        "extraction_model": c.extraction_model,
        "extraction_mode": c.extraction_mode,
        "review_status": c.review_status,
        "review_note": c.review_note,
        "reviewed_at": _iso(c.reviewed_at),
        "version": c.version,
        "create_time": _iso(c.create_time),
        "update_time": _iso(c.update_time),
    }


def _comparison_out(c) -> dict:
    return {
        "comparison_id": c.id,
        "project_id": c.project_id,
        "paper_id": c.paper_id,
        "dimension_id": c.dimension_id,
        "content": c.content,
        "evidence_ids": _loads(c.evidence_ids_json, []),
        "level": c.level,
        "run_id": c.run_id,
        "version": c.version,
        "update_time": _iso(c.update_time),
    }


def _conflict_out(c) -> dict:
    return {
        "conflict_id": c.id,
        "project_id": c.project_id,
        "run_id": c.run_id,
        "sub_question_id": c.sub_question_id,
        "dimension_id": c.dimension_id,
        "topic": c.topic,
        "description": c.description,
        "side_a": _loads(c.side_a_json, []),
        "side_b": _loads(c.side_b_json, []),
        "status": c.status,
        "resolution": c.resolution,
        "version": c.version,
        "create_time": _iso(c.create_time),
        "update_time": _iso(c.update_time),
    }


def _loads(value, default):
    try:
        return json.loads(value) if value else default
    except (json.JSONDecodeError, TypeError):
        return default


def _not_found(detail: str = "研究资源不存在或无权访问") -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


# ---------------------------------------------------------------------------
# 研究项目 CRUD
# ---------------------------------------------------------------------------


@research_router.post("/projects", summary="创建研究项目（owner 来自当前用户）")
def create_project(
    body: ProjectCreate,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> dict:
    try:
        project = repo.create_project(
            session, user.id, body.name, body.research_question, body.scope
        )
    except ValueError as exc:
        raise _bad_request(str(exc))
    return _project_out(project)


@research_router.get("/projects", summary="当前用户的研究项目列表")
def list_projects(
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
    status_filter: Optional[str] = Query(None, alias="status", description="active/archived"),
) -> dict:
    return {
        "projects": [_project_out(p) for p in repo.list_projects(session, user.id, status_filter)]
    }


@research_router.get("/projects/{project_id}", summary="研究项目详情")
def get_project(
    project_id: str,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> dict:
    project = repo.get_owned_project(session, project_id, user.id)
    if project is None:
        raise _not_found("研究项目不存在或无权访问")
    return _project_out(project)


@research_router.patch("/projects/{project_id}", summary="修改研究项目")
def update_project(
    project_id: str,
    body: ProjectUpdate,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> dict:
    try:
        project = repo.update_owned_project(
            session,
            project_id,
            user.id,
            name=body.name,
            research_question=body.research_question,
            scope=body.scope,
            status=body.status,
        )
    except repo.ResearchResourceNotFound:
        raise _not_found("研究项目不存在或无权访问")
    except ValueError as exc:
        raise _bad_request(str(exc))
    return _project_out(project)


@research_router.delete("/projects/{project_id}", summary="删除研究项目及其私有产物")
def delete_project(
    project_id: str,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> dict:
    try:
        repo.delete_owned_project(session, project_id, user.id)
    except repo.ResearchResourceNotFound:
        raise _not_found("研究项目不存在或无权访问")
    return {"deleted": True, "project_id": project_id}


# ---------------------------------------------------------------------------
# 研究运行
# ---------------------------------------------------------------------------


@research_router.post("/projects/{project_id}/runs", summary="创建研究运行")
def create_run(
    project_id: str,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> dict:
    try:
        run = repo.create_run(session, user.id, project_id)
    except repo.ResearchResourceNotFound:
        raise _not_found("研究项目不存在或无权访问")
    return _run_out(run)


@research_router.get("/projects/{project_id}/runs", summary="项目的研究运行列表")
def list_runs(
    project_id: str,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
    limit: int = Query(20, ge=1, le=100),
) -> dict:
    if repo.get_owned_project(session, project_id, user.id) is None:
        raise _not_found("研究项目不存在或无权访问")
    return {"runs": [_run_out(r) for r in repo.list_runs(session, user.id, project_id=project_id, limit=limit)]}


@research_router.get("/runs/{run_id}", summary="研究运行详情（计划/阶段/来源统计/摘要）")
def get_run(
    run_id: str,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> dict:
    run = repo.get_owned_run(session, run_id, user.id)
    if run is None:
        raise _not_found("研究运行不存在或无权访问")
    return _run_out(run)


@research_router.post("/runs/{run_id}/plan", summary="生成研究计划（生成后挂起等待确认）")
async def generate_plan(
    run_id: str,
    user: UserModel = Depends(require_password_changed),
) -> dict:
    from chatchat.server.research.service import generate_research_plan

    try:
        return await generate_research_plan(run_id, user.id)
    except LookupError:
        raise _not_found("研究运行不存在或无权访问")
    except repo.ResearchConflict as exc:
        raise _conflict(str(exc))


@research_router.put("/runs/{run_id}/plan", summary="确认前编辑研究计划")
def update_plan(
    run_id: str,
    body: PlanUpdate,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> dict:
    from chatchat.server.research.plan import parse_research_plan
    from chatchat.server.research.sources import build_default_sources

    try:
        # 用户提交的计划必须仍是结构化子问题 + 检索式
        available = set(build_default_sources().keys())
        plan = parse_research_plan(body.plan, available)
    except repo.ResearchResourceNotFound:
        raise _not_found("研究运行不存在或无权访问")
    except Exception as exc:
        raise _bad_request(f"计划不符合结构要求：{exc}")
    try:
        run = repo.update_plan(session, run_id, user.id, plan.to_json())
    except repo.ResearchResourceNotFound:
        raise _not_found("研究运行不存在或无权访问")
    except repo.ResearchConflict as exc:
        raise _conflict(str(exc))
    return _run_out(run)


@research_router.post("/runs/{run_id}/confirm", summary="确认计划并执行研究运行")
async def confirm_run(
    run_id: str,
    user: UserModel = Depends(require_password_changed),
) -> dict:
    from chatchat.server.research.service import confirm_and_execute_research_run

    try:
        return await confirm_and_execute_research_run(run_id, user.id)
    except LookupError:
        raise _not_found("研究运行不存在或无权访问")
    except repo.ResearchConflict as exc:
        raise _conflict(str(exc))
    except RuntimeError as exc:
        raise _conflict(str(exc))


@research_router.post("/runs/{run_id}/resume", summary="恢复中断/重启前的研究运行")
async def resume_run(
    run_id: str,
    user: UserModel = Depends(require_password_changed),
) -> dict:
    from chatchat.server.research.service import resume_research_run

    try:
        return await resume_research_run(run_id, user.id)
    except LookupError:
        raise _not_found("研究运行不存在或无权访问")
    except RuntimeError as exc:
        raise _conflict(str(exc))


@research_router.post("/runs/{run_id}/cancel", summary="取消研究运行")
def cancel_run(
    run_id: str,
    user: UserModel = Depends(require_password_changed),
) -> dict:
    from chatchat.server.research.service import cancel_research_run

    summary = cancel_research_run(run_id, user.id)
    if summary is None:
        raise _not_found("研究运行不存在或无权访问")
    return summary


@research_router.post(
    "/runs/{run_id}/artifacts",
    summary="基于已纳入的论文继续产出证据/比较/报告（任务 018 阶段 E）",
)
async def run_artifacts(
    run_id: str,
    user: UserModel = Depends(require_password_changed),
) -> dict:
    from chatchat.server.research.service import run_research_artifacts

    try:
        return await run_research_artifacts(run_id, user.id)
    except LookupError:
        raise _not_found("研究运行不存在或无权访问")
    except repo.ResearchConflict as exc:
        raise _conflict(str(exc))
    except RuntimeError as exc:
        raise _conflict(str(exc))


# ---------------------------------------------------------------------------
# 项目论文与筛选
# ---------------------------------------------------------------------------


@research_router.get("/projects/{project_id}/papers", summary="项目候选论文列表")
def list_papers(
    project_id: str,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
    screening_status: Optional[str] = Query(
        None, description="pending_review/included/excluded"
    ),
) -> dict:
    if screening_status and screening_status not in repo.SCREENING_STATUSES:
        raise _bad_request("非法筛选状态")
    rows = repo.list_project_papers(session, project_id, user.id, screening_status=screening_status)
    if repo.get_owned_project(session, project_id, user.id) is None:
        raise _not_found("研究项目不存在或无权访问")
    return {"papers": [_paper_out(pp, paper) for pp, paper in rows]}


@research_router.patch("/papers/{project_paper_id}/screening", summary="标记论文待审/纳入/排除")
def set_screening(
    project_paper_id: str,
    body: ScreeningUpdate,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> dict:
    try:
        pp = repo.set_screening(
            session, project_paper_id, user.id, body.screening_status, body.reason
        )
    except repo.ResearchResourceNotFound:
        raise _not_found("项目论文不存在或无权访问")
    except ValueError as exc:
        raise _bad_request(str(exc))
    paper = session.get(repo.PaperRecordModel, pp.paper_id)
    return _paper_out(pp, paper)


# ---------------------------------------------------------------------------
# 原文单元与证据卡片（任务 018 阶段 A）
# ---------------------------------------------------------------------------


@research_router.post(
    "/projects/{project_id}/papers/{paper_id}/fulltext",
    summary="解析并入库该论文在本项目内的原文单元（幂等，可降级）",
)
def parse_fulltext(
    project_id: str,
    paper_id: str,
    body: FullTextRequest = Body(FullTextRequest()),
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> dict:
    from chatchat.server.research.evidence_service import ingest_paper_fulltext

    try:
        return ingest_paper_fulltext(
            session,
            project_id=project_id,
            owner_id=user.id,
            paper_id=paper_id,
            run_id=body.run_id,
            kb_names=body.kb_names,
        )
    except repo.ResearchResourceNotFound:
        raise _not_found("研究项目或项目论文不存在或无权访问")


@research_router.get(
    "/projects/{project_id}/papers/{paper_id}/units",
    summary="查看该论文在本项目内的原文单元",
)
def list_units(
    project_id: str,
    paper_id: str,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> dict:
    if repo.get_owned_project(session, project_id, user.id) is None:
        raise _not_found("研究项目不存在或无权访问")
    units = repo.list_paper_units(session, project_id, user.id, paper_id)
    return {"units": [_unit_out(u) for u in units], "total": len(units)}


@research_router.post(
    "/projects/{project_id}/papers/{paper_id}/evidence",
    summary="按子问题提取该论文的证据（原文逐字核对 + 幂等）",
)
def extract_evidence(
    project_id: str,
    paper_id: str,
    body: EvidenceExtractRequest,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> dict:
    from chatchat.server.research.evidence_service import extract_paper_evidence

    project = repo.get_owned_project(session, project_id, user.id)
    if project is None:
        raise _not_found("研究项目不存在或无权访问")
    try:
        result = extract_paper_evidence(
            session,
            project_id=project_id,
            owner_id=user.id,
            paper_id=paper_id,
            sub_question_id=body.sub_question_id,
            question=(body.question or project.research_question),
            sub_question=body.sub_question,
            model_name=body.model or "",
            run_id=body.run_id,
        )
    except repo.ResearchResourceNotFound:
        raise _not_found("研究项目或项目论文不存在或无权访问")
    except Exception as exc:  # 提取失败如实回报，不静默产出空证据
        raise _bad_request(f"证据提取失败：{exc}")
    return result


@research_router.get("/projects/{project_id}/evidence", summary="项目证据列表")
def list_evidence(
    project_id: str,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
    paper_id: Optional[str] = Query(None),
    sub_question_id: Optional[str] = Query(None),
    review_status: Optional[str] = Query(
        None, description="pending/confirmed/edited/rejected"
    ),
    include_rejected: bool = Query(True),
) -> dict:
    if review_status and review_status not in repo.REVIEW_STATUSES:
        raise _bad_request("非法审核状态")
    if repo.get_owned_project(session, project_id, user.id) is None:
        raise _not_found("研究项目不存在或无权访问")
    cards = repo.list_evidence(
        session,
        project_id,
        user.id,
        paper_id=paper_id,
        sub_question_id=sub_question_id,
        review_status=review_status,
        include_rejected=include_rejected,
    )
    titles = {}
    for card in cards:
        paper = session.get(repo.PaperRecordModel, card.paper_id)
        titles[card.paper_id] = paper.title if paper is not None else ""
    out = [_evidence_out(c) for c in cards]
    for item in out:
        item["paper_title"] = titles.get(item["paper_id"], "")
    return {"evidence": out, "total": len(out)}


@research_router.patch("/evidence/{evidence_id}", summary="确认/编辑/驳回证据")
def review_evidence(
    evidence_id: str,
    body: EvidenceReview,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> dict:
    from chatchat.server.research.evidence_service import get_paper_units
    from chatchat.server.research.evidence import find_quote

    card = repo.get_owned_evidence(session, evidence_id, user.id)
    if card is None:
        raise _not_found("证据不存在或无权访问")
    # 编辑后的原文必须仍能在该论文的原文中逐字核对，否则拒绝该次编辑
    if body.quote is not None and body.quote.strip() and body.quote.strip() != card.quote:
        units = get_paper_units(session, card.project_id, user.id, card.paper_id)
        if find_quote(body.quote, units) is None:
            raise _bad_request("修改后的原文在该论文的原文中找不到，无法保存")
    try:
        card = repo.review_evidence(
            session,
            evidence_id,
            user.id,
            review_status=body.review_status,
            summary=body.summary,
            quote=body.quote,
            note=body.note,
        )
    except repo.ResearchResourceNotFound:
        raise _not_found("证据不存在或无权访问")
    except ValueError as exc:
        raise _bad_request(str(exc))
    return _evidence_out(card)


@research_router.delete("/evidence/{evidence_id}", summary="删除证据卡片")
def delete_evidence(
    evidence_id: str,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> dict:
    try:
        repo.delete_evidence(session, evidence_id, user.id)
    except repo.ResearchResourceNotFound:
        raise _not_found("证据不存在或无权访问")
    return {"deleted": True, "evidence_id": evidence_id}


@research_router.post(
    "/projects/{project_id}/evidence/reverify",
    summary="按当前原文重新核对证据原文",
)
def reverify_evidence(
    project_id: str,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> dict:
    from chatchat.server.research.evidence_service import reverify_evidence as _reverify

    try:
        return _reverify(session, project_id=project_id, owner_id=user.id)
    except repo.ResearchResourceNotFound:
        raise _not_found("研究项目不存在或无权访问")


# ---------------------------------------------------------------------------
# 比较维度、比较单元、冲突与覆盖（任务 018 阶段 B）
# ---------------------------------------------------------------------------


@research_router.get("/projects/{project_id}/dimensions", summary="查看项目比较维度")
def get_dimensions(
    project_id: str,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> dict:
    try:
        return {"dimensions": repo.get_project_dimensions(session, project_id, user.id)}
    except repo.ResearchResourceNotFound:
        raise _not_found("研究项目不存在或无权访问")


@research_router.put("/projects/{project_id}/dimensions", summary="设置项目比较维度")
def set_dimensions(
    project_id: str,
    body: DimensionsUpdate,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> dict:
    try:
        dimensions = repo.set_project_dimensions(
            session,
            project_id,
            user.id,
            [d.model_dump() for d in body.dimensions],
        )
    except repo.ResearchResourceNotFound:
        raise _not_found("研究项目不存在或无权访问")
    return {"dimensions": dimensions}


@research_router.post(
    "/projects/{project_id}/comparisons",
    summary="按维度生成比较单元并检测冲突（只以真实证据为依据）",
)
def generate_comparisons(
    project_id: str,
    body: ComparisonGenerateRequest = Body(ComparisonGenerateRequest()),
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> dict:
    from chatchat.server.research.comparison_service import generate_project_comparisons

    try:
        return generate_project_comparisons(
            session,
            project_id=project_id,
            owner_id=user.id,
            run_id=body.run_id,
            model_name=body.model or "",
            dimension_ids=body.dimension_ids,
            detect_conflicts=body.detect_conflicts,
        )
    except repo.ResearchResourceNotFound:
        raise _not_found("研究项目不存在或无权访问")
    except Exception as exc:  # 生成失败如实回报
        raise _bad_request(f"比较生成失败：{exc}")


@research_router.get("/projects/{project_id}/comparisons", summary="项目比较矩阵")
def list_comparisons(
    project_id: str,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
    paper_id: Optional[str] = Query(None),
    dimension_id: Optional[str] = Query(None),
) -> dict:
    if repo.get_owned_project(session, project_id, user.id) is None:
        raise _not_found("研究项目不存在或无权访问")
    units = repo.list_comparison_units(
        session, project_id, user.id, paper_id=paper_id, dimension_id=dimension_id
    )
    titles = {}
    for unit in units:
        if unit.paper_id not in titles:
            paper = session.get(repo.PaperRecordModel, unit.paper_id)
            titles[unit.paper_id] = paper.title if paper is not None else ""
    out = [_comparison_out(u) for u in units]
    for item in out:
        item["paper_title"] = titles.get(item["paper_id"], "")
    return {"comparisons": out, "total": len(out)}


@research_router.get("/projects/{project_id}/conflicts", summary="项目冲突列表")
def list_conflicts(
    project_id: str,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
    status_filter: Optional[str] = Query(None, alias="status", description="open/resolved"),
) -> dict:
    if status_filter and status_filter not in repo.CONFLICT_STATUSES:
        raise _bad_request("非法冲突状态")
    if repo.get_owned_project(session, project_id, user.id) is None:
        raise _not_found("研究项目不存在或无权访问")
    conflicts = repo.list_conflicts(session, project_id, user.id, status_filter=status_filter)
    return {"conflicts": [_conflict_out(c) for c in conflicts], "total": len(conflicts)}


@research_router.patch("/conflicts/{conflict_id}", summary="人工裁决冲突")
def resolve_conflict(
    conflict_id: str,
    body: ConflictResolve,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> dict:
    try:
        conflict = repo.resolve_conflict(session, conflict_id, user.id, body.resolution)
    except repo.ResearchResourceNotFound:
        raise _not_found("冲突记录不存在或无权访问")
    return _conflict_out(conflict)


@research_router.delete("/conflicts/{conflict_id}", summary="删除冲突记录")
def delete_conflict(
    conflict_id: str,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> dict:
    try:
        repo.delete_conflict(session, conflict_id, user.id)
    except repo.ResearchResourceNotFound:
        raise _not_found("冲突记录不存在或无权访问")
    return {"deleted": True, "conflict_id": conflict_id}


@research_router.get(
    "/projects/{project_id}/coverage",
    summary="证据覆盖、缺失维度、冲突与补充检索建议（可重现计算）",
)
def get_coverage(
    project_id: str,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> dict:
    from chatchat.server.research.comparison_service import project_coverage

    try:
        return project_coverage(session, project_id=project_id, owner_id=user.id)
    except repo.ResearchResourceNotFound:
        raise _not_found("研究项目不存在或无权访问")


@research_router.post(
    "/runs/{run_id}/supplement",
    summary="基于上一轮运行创建补充检索的新运行（需确认，轮次有上限）",
)
def supplement_run(
    run_id: str,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> dict:
    from chatchat.server.research.comparison_service import start_supplementary_run

    run = repo.get_owned_run(session, run_id, user.id)
    if run is None:
        raise _not_found("研究运行不存在或无权访问")
    try:
        return start_supplementary_run(
            session, project_id=run.project_id, owner_id=user.id, parent_run_id=run.id
        )
    except repo.ResearchResourceNotFound:
        raise _not_found("研究运行不存在或无权访问")
    except repo.ResearchConflict as exc:
        raise _conflict(str(exc))


# ---------------------------------------------------------------------------
# 研究报告：版本化、引用可反查、导出（任务 018 阶段 C）
# ---------------------------------------------------------------------------


@research_router.post("/projects/{project_id}/reports", summary="生成研究报告新版本")
def generate_report(
    project_id: str,
    body: ReportGenerateRequest = Body(ReportGenerateRequest()),
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> dict:
    from chatchat.server.research.report_service import generate_project_report

    try:
        return generate_project_report(
            session,
            project_id=project_id,
            owner_id=user.id,
            run_id=body.run_id,
            model_name=body.model or "",
        )
    except repo.ResearchResourceNotFound:
        raise _not_found("研究项目或研究运行不存在或无权访问")
    except Exception as exc:  # 生成失败如实回报，不产出空报告
        raise _bad_request(f"报告生成失败：{exc}")


@research_router.get("/projects/{project_id}/reports", summary="报告版本列表")
def list_reports(
    project_id: str,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
    include_versions: bool = Query(False, description="是否返回全部版本（默认只返回最新版）"),
) -> dict:
    if repo.get_owned_project(session, project_id, user.id) is None:
        raise _not_found("研究项目不存在或无权访问")
    from chatchat.server.research.report_service import _report_out

    if include_versions:
        reports = repo.list_project_reports(session, project_id, user.id)
    else:
        reports = repo.latest_project_reports(session, project_id, user.id)
    return {"reports": [_report_out(r) for r in reports], "total": len(reports)}


@research_router.get("/reports/{report_id}", summary="报告详情（含引用映射）")
def get_report(
    report_id: str,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> dict:
    from chatchat.server.research.report_service import _report_out

    report = repo.get_owned_report(session, report_id, user.id)
    if report is None:
        raise _not_found("研究报告不存在或无权访问")
    return _report_out(report)


@research_router.patch("/reports/{report_id}", summary="保存报告编辑（生成新版本）")
def edit_report(
    report_id: str,
    body: ReportEdit,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> dict:
    from chatchat.server.research.report_service import save_report_edit

    try:
        return save_report_edit(
            session,
            report_id=report_id,
            owner_id=user.id,
            content=body.content,
            title=body.title,
        )
    except repo.ResearchResourceNotFound:
        raise _not_found("研究报告不存在或无权访问")
    except ValueError as exc:
        raise _bad_request(str(exc))


@research_router.delete("/reports/{report_id}", summary="删除报告单个版本")
def delete_report(
    report_id: str,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> dict:
    try:
        repo.delete_report(session, report_id, user.id)
    except repo.ResearchResourceNotFound:
        raise _not_found("研究报告不存在或无权访问")
    return {"deleted": True, "report_id": report_id}


@research_router.get("/reports/{report_id}/export.md", summary="导出报告 Markdown")
def export_report_markdown(
    report_id: str,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> Response:
    from chatchat.server.research.report_service import render_report_markdown

    try:
        markdown = render_report_markdown(session, report_id=report_id, owner_id=user.id)
    except repo.ResearchResourceNotFound:
        raise _not_found("研究报告不存在或无权访问")
    return Response(
        content=markdown,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="research-report-{report_id}.md"'},
    )


@research_router.get("/reports/{report_id}/export.bib", summary="导出 BibTeX（仅真实论文元数据）")
def export_report_bibtex(
    report_id: str,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> Response:
    from chatchat.server.research.report_service import render_report_bibtex

    try:
        bib = render_report_bibtex(session, report_id=report_id, owner_id=user.id)
    except repo.ResearchResourceNotFound:
        raise _not_found("研究报告不存在或无权访问")
    return Response(
        content=bib,
        media_type="application/x-bibtex; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="research-report-{report_id}.bib"'},
    )


__all__ = ["research_router"]
