"""论文研究领域 Repository（owner-aware，任务 017）。

鉴权规则（与 conversation/agent_run 一致）：

- 所有读写同时校验 ``id + owner_id``（子资源经所属项目/关系继承），
  不满足一律视为"不可见"（返回 None / 抛 ``ResearchResourceNotFound``，
  调用方映射 404，不区分原因，避免资源枚举）；
- owner 只来自服务端认证上下文，请求体中的 ``owner_id``/``user_id``
  绝不参与授权。

状态机：

- 运行：``pending -> awaiting_confirmation -> running ->
  completed | failed | cancelled``；``pending/awaiting_confirmation/
  running`` 均可取消；终态不再被更新。
- 确认是服务端状态转换：``awaiting_confirmation -> running`` 原子
  UPDATE；未确认的运行绝不进入 running，也就不会开始检索。
- 论文入库幂等：``upsert_paper`` 按 DOI → ArXiv ID → 规范 URL →
  规范化标题+年份 的优先级查重合并；``link_project_paper`` 按
  ``(project_id, paper_id)`` 唯一，重试/恢复不产生重复记录或关系。

状态读取用原生 SQL 直读数据库（绕过 ORM 身份映射缓存），保证能观察到
其他连接（取消 API、其他进程）的提交。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

import hashlib

from chatchat.server.db.models.research_model import (
    ComparisonUnitModel,
    EvidenceCardModel,
    PaperRecordModel,
    PaperUnitModel,
    ProjectPaperModel,
    ResearchConflictModel,
    ResearchProjectModel,
    ResearchReportModel,
    ResearchRunModel,
)
from chatchat.utils import build_logger

logger = build_logger()

PROJECT_STATUSES = ("active", "archived")
RUN_STATUSES = (
    "pending",
    "awaiting_confirmation",
    "running",
    "completed",
    "failed",
    "cancelled",
)
RUN_TERMINAL_STATUSES = ("completed", "failed", "cancelled")
RUN_ACTIVE_STATUSES = ("pending", "awaiting_confirmation", "running")
RUN_STAGES = (
    "draft",
    "plan",
    "retrieval",
    "dedup",
    "screening",
    "persist",
    # 任务 018 阶段 E：证据闭环的后续阶段（select/evidence_review/
    # report_review 是人工确认点，挂起时 status=awaiting_confirmation）
    "fulltext",
    "evidence",
    "evidence_review",
    "coverage",
    "comparison",
    "report",
    "report_review",
    "done",
)
#: 阶段先后顺序（恢复重放时据此判断某阶段是否已完成，避免重复调用模型/来源）
STAGE_ORDER = {stage: index for index, stage in enumerate(RUN_STAGES)}
SCREENING_STATUSES = ("pending_review", "included", "excluded")

# --- 任务 018 阶段 A：原文单元与证据卡片 ---
UNIT_KINDS = ("block", "abstract")
FULLTEXT_STATES = ("unknown", "none", "abstract", "fulltext")
EVIDENCE_TYPES = (
    "objective",
    "method",
    "dataset",
    "metric",
    "result",
    "conclusion",
    "limitation",
)
EVIDENCE_LEVELS = ("fulltext", "abstract")
REVIEW_STATUSES = ("pending", "confirmed", "edited", "rejected")
# 用户已审核（非 pending）的证据在重跑时不被静默覆盖
REVIEWED_STATUSES = ("confirmed", "edited", "rejected")

# --- 任务 018 阶段 B：比较维度、比较单元、冲突与有界补充检索 ---
#: 默认比较维度（id 与证据类型同名，便于覆盖检查直接按证据类型统计）
DEFAULT_DIMENSIONS = (
    {"id": "objective", "label": "研究目标"},
    {"id": "method", "label": "核心方法"},
    {"id": "dataset", "label": "数据集"},
    {"id": "metric", "label": "评价指标"},
    {"id": "result", "label": "主要结果"},
    {"id": "conclusion", "label": "结论与优点"},
    {"id": "limitation", "label": "局限"},
)
MAX_DIMENSIONS = 12
CONFLICT_STATUSES = ("open", "resolved")
#: 补充检索的最大轮次（含首轮）：可见的新运行，绝不无限自循环
MAX_RESEARCH_ROUNDS = 5


class ResearchResourceNotFound(Exception):
    """研究资源不存在或不属于指定用户（调用方映射 404）。"""


class ResearchConflict(Exception):
    """当前状态不允许该操作（调用方映射 409）。"""


def _now():
    return datetime.now()


# ---------------------------------------------------------------------------
# 研究项目
# ---------------------------------------------------------------------------


def create_project(
    session: Session,
    owner_id: str,
    name: str,
    research_question: str,
    scope: str = "",
) -> ResearchProjectModel:
    """新增私有研究项目；owner 由服务端写入。"""
    if not owner_id:
        raise ValueError("owner_id 不能为空")
    name = (name or "").strip()
    question = (research_question or "").strip()
    if not name:
        raise ValueError("项目名称不能为空")
    if not question:
        raise ValueError("研究问题不能为空")
    project = ResearchProjectModel(
        id=uuid.uuid4().hex,
        owner_id=owner_id,
        name=name[:200],
        research_question=question[:2000],
        scope=(scope or "")[:2000],
        status="active",
        current_stage="",
        create_time=_now(),
        update_time=_now(),
    )
    session.add(project)
    session.commit()
    session.refresh(project)
    return project


def list_projects(
    session: Session, owner_id: str, status: str | None = None
) -> list[ResearchProjectModel]:
    """当前用户的项目列表（update_time DESC）。"""
    q = session.query(ResearchProjectModel).filter(
        ResearchProjectModel.owner_id == owner_id
    )
    if status:
        q = q.filter(ResearchProjectModel.status == status)
    return (
        q.order_by(
            ResearchProjectModel.update_time.desc(),
            ResearchProjectModel.id.desc(),
        )
        .all()
    )


def get_owned_project(
    session: Session, project_id: str, owner_id: str
) -> ResearchProjectModel | None:
    if not project_id or not owner_id:
        return None
    return (
        session.query(ResearchProjectModel)
        .filter(
            ResearchProjectModel.id == project_id,
            ResearchProjectModel.owner_id == owner_id,
        )
        .one_or_none()
    )


def update_owned_project(
    session: Session,
    project_id: str,
    owner_id: str,
    *,
    name: str | None = None,
    research_question: str | None = None,
    scope: str | None = None,
    status: str | None = None,
) -> ResearchProjectModel:
    """修改自己的项目（仅提供的字段）；不可见抛 ResearchResourceNotFound。"""
    project = get_owned_project(session, project_id, owner_id)
    if project is None:
        raise ResearchResourceNotFound(project_id)
    if name is not None:
        name = name.strip()
        if not name:
            raise ValueError("项目名称不能为空")
        project.name = name[:200]
    if research_question is not None:
        research_question = research_question.strip()
        if not research_question:
            raise ValueError("研究问题不能为空")
        project.research_question = research_question[:2000]
    if scope is not None:
        project.scope = scope[:2000]
    if status is not None:
        if status not in PROJECT_STATUSES:
            raise ValueError(f"非法项目状态：{status}")
        project.status = status
    project.update_time = _now()
    session.commit()
    session.refresh(project)
    return project


def delete_owned_project(session: Session, project_id: str, owner_id: str) -> None:
    """删除项目及其全部私有产物（运行、项目论文、原文单元、证据、比较、冲突、报告）。

    级联只作用于私有关系表；**绝不删除/修改公开 ``paper_record``**
    （其他项目可能复用同一规范记录）。用户私有的 ``paper_unit`` 原文块
    随项目一并清除，不残留在库中。不可见抛 ResearchResourceNotFound。
    """
    project = get_owned_project(session, project_id, owner_id)
    if project is None:
        raise ResearchResourceNotFound(project_id)
    session.query(ResearchRunModel).filter(
        ResearchRunModel.project_id == project_id,
        ResearchRunModel.owner_id == owner_id,
    ).delete(synchronize_session=False)
    session.query(ProjectPaperModel).filter(
        ProjectPaperModel.project_id == project_id,
        ProjectPaperModel.owner_id == owner_id,
    ).delete(synchronize_session=False)
    session.query(PaperUnitModel).filter(
        PaperUnitModel.project_id == project_id,
        PaperUnitModel.owner_id == owner_id,
    ).delete(synchronize_session=False)
    session.query(EvidenceCardModel).filter(
        EvidenceCardModel.project_id == project_id,
        EvidenceCardModel.owner_id == owner_id,
    ).delete(synchronize_session=False)
    session.query(ComparisonUnitModel).filter(
        ComparisonUnitModel.project_id == project_id,
        ComparisonUnitModel.owner_id == owner_id,
    ).delete(synchronize_session=False)
    session.query(ResearchConflictModel).filter(
        ResearchConflictModel.project_id == project_id,
        ResearchConflictModel.owner_id == owner_id,
    ).delete(synchronize_session=False)
    session.query(ResearchReportModel).filter(
        ResearchReportModel.project_id == project_id,
        ResearchReportModel.owner_id == owner_id,
    ).delete(synchronize_session=False)
    session.delete(project)
    session.commit()


# ---------------------------------------------------------------------------
# 研究运行
# ---------------------------------------------------------------------------


def create_run(
    session: Session, owner_id: str, project_id: str, *, run_id: str | None = None
) -> ResearchRunModel:
    """在项目下创建研究运行（pending/draft）；项目不可见抛 404 异常。"""
    if not owner_id:
        raise ValueError("owner_id 不能为空")
    if get_owned_project(session, project_id, owner_id) is None:
        raise ResearchResourceNotFound(project_id)
    run = ResearchRunModel(
        id=run_id or uuid.uuid4().hex,
        project_id=project_id,
        owner_id=owner_id,
        plan_json="",
        status="pending",
        stage="draft",
        sources_json="[]",
        summary_json="",
        create_time=_now(),
        update_time=_now(),
    )
    session.add(run)
    # 项目展示字段同步（尽力而为）
    project = get_owned_project(session, project_id, owner_id)
    if project is not None:
        project.current_stage = "draft"
        project.update_time = _now()
    session.commit()
    session.refresh(run)
    return run


def get_owned_run(
    session: Session, run_id: str, owner_id: str
) -> ResearchRunModel | None:
    if not run_id or not owner_id:
        return None
    return (
        session.query(ResearchRunModel)
        .filter(
            ResearchRunModel.id == run_id, ResearchRunModel.owner_id == owner_id
        )
        .one_or_none()
    )


def list_runs(
    session: Session,
    owner_id: str,
    *,
    project_id: str | None = None,
    limit: int = 20,
) -> list[ResearchRunModel]:
    q = session.query(ResearchRunModel).filter(ResearchRunModel.owner_id == owner_id)
    if project_id:
        q = q.filter(ResearchRunModel.project_id == project_id)
    return (
        q.order_by(ResearchRunModel.create_time.desc(), ResearchRunModel.id.desc())
        .limit(max(1, min(int(limit or 20), 100)))
        .all()
    )


def run_status(session: Session, run_id: str, owner_id: str) -> str | None:
    """直读数据库中的运行状态（绕过身份映射缓存）。"""
    if not run_id or not owner_id:
        return None
    row = session.execute(
        text("SELECT status FROM research_run WHERE id = :id AND owner_id = :owner"),
        {"id": run_id, "owner": owner_id},
    ).one_or_none()
    return row[0] if row is not None else None


def set_plan(
    session: Session, run_id: str, owner_id: str, plan_json: str
) -> ResearchRunModel | None:
    """写入生成的研究计划：``pending -> awaiting_confirmation``（stage=plan）。

    仅 pending/awaiting_confirmation 可写（幂等重写计划）；running/终态
    返回 None（调用方按状态冲突处理）。
    """
    result = session.execute(
        text(
            "UPDATE research_run SET plan_json = :plan, status = 'awaiting_confirmation', "
            "stage = 'plan', update_time = CURRENT_TIMESTAMP "
            "WHERE id = :id AND owner_id = :owner "
            "AND status IN ('pending', 'awaiting_confirmation')"
        ),
        {"plan": (plan_json or "")[:8000], "id": run_id, "owner": owner_id},
    )
    session.commit()
    if not result.rowcount:
        return None
    run = get_owned_run(session, run_id, owner_id)
    if run is not None:
        session.refresh(run)
    return run


def update_plan(
    session: Session, run_id: str, owner_id: str, plan_json: str
) -> ResearchRunModel:
    """用户编辑计划；仅 ``awaiting_confirmation`` 状态允许（确认前可改）。

    - 不可见：ResearchResourceNotFound；
    - 状态不符：ResearchConflict。
    """
    current = run_status(session, run_id, owner_id)
    if current is None:
        raise ResearchResourceNotFound(run_id)
    if current != "awaiting_confirmation":
        raise ResearchConflict(
            f"当前状态 {current} 不允许修改计划（仅待确认状态可修改）"
        )
    result = session.execute(
        text(
            "UPDATE research_run SET plan_json = :plan, update_time = CURRENT_TIMESTAMP "
            "WHERE id = :id AND owner_id = :owner AND status = 'awaiting_confirmation'"
        ),
        {"plan": (plan_json or "")[:8000], "id": run_id, "owner": owner_id},
    )
    session.commit()
    if not result.rowcount:
        # 与确认/取消 API 的竞态：重读后按当前状态给冲突
        fresh = run_status(session, run_id, owner_id)
        raise ResearchConflict(f"计划状态已变化（当前 {fresh}），修改未生效")
    run = get_owned_run(session, run_id, owner_id)
    if run is not None:
        session.refresh(run)
    return run


def confirm_run(session: Session, run_id: str, owner_id: str) -> ResearchRunModel:
    """确认计划：``awaiting_confirmation -> running``（服务端状态转换）。

    - 不可见：ResearchResourceNotFound；
    - 非待确认状态：ResearchConflict（pending 需先生成计划；
      running/终态不可重复确认）。
    """
    # 前置检查直读数据库（同一会话内 set_plan 的原生 SQL 更新不会
    # 反映到 ORM 身份映射）
    current = run_status(session, run_id, owner_id)
    if current is None:
        raise ResearchResourceNotFound(run_id)
    if current != "awaiting_confirmation":
        raise ResearchConflict(
            f"当前状态 {current} 不可确认（仅待确认状态可确认执行）"
        )
    result = session.execute(
        text(
            "UPDATE research_run SET status = 'running', start_time = CURRENT_TIMESTAMP, "
            "update_time = CURRENT_TIMESTAMP "
            "WHERE id = :id AND owner_id = :owner AND status = 'awaiting_confirmation'"
        ),
        {"id": run_id, "owner": owner_id},
    )
    session.commit()
    if not result.rowcount:
        fresh = run_status(session, run_id, owner_id)
        raise ResearchConflict(f"确认未生效（当前状态 {fresh}）")
    run = get_owned_run(session, run_id, owner_id)
    if run is not None:
        session.refresh(run)
    return run


def pause_run_for_review(session: Session, run_id: str, owner_id: str) -> bool:
    """人工复核门禁：``running -> awaiting_confirmation``（任务 018 阶段 E）。

    复用任务 017 的确认状态机（不新增状态值）：挂起语义与计划确认一致，
    区别只在 ``stage`` 表明正在复核什么（证据/报告）。已终态或已挂起时
    返回 ``False``，调用方据此不再推进。
    """
    result = session.execute(
        text(
            "UPDATE research_run SET status = 'awaiting_confirmation', "
            "update_time = CURRENT_TIMESTAMP "
            "WHERE id = :id AND owner_id = :owner AND status = 'running'"
        ),
        {"id": run_id, "owner": owner_id},
    )
    session.commit()
    # 原生 SQL 改动不会反映到 ORM 身份映射：过期后同会话再读才拿到新状态
    session.expire_all()
    return bool(result.rowcount)


def reopen_run_for_artifacts(
    session: Session, run_id: str, owner_id: str
) -> ResearchRunModel | None:
    """用户筛选论文后要求产出产物：``completed -> running``（任务 018 阶段 E）。

    - 阶段回到 ``persist``（证据闭环入口），后续节点按 ``stage`` 跳过已
      完成的检索/入库，因此不会重复检索或重复创建论文；
    - 仅 ``completed`` 可重开：``failed`` 走 ``retry_run``，``cancelled``
      是用户意图，不自动复活；
    - 证据/比较幂等，报告版本只追加，因此重复执行不覆盖用户产物。
    """
    current = run_status(session, run_id, owner_id)
    if current is None:
        raise ResearchResourceNotFound(run_id)
    if current != "completed":
        raise ResearchConflict(
            f"当前状态 {current} 不能重新打开生成产物（仅 completed 可，需先纳入论文）"
        )
    session.execute(
        text(
            "UPDATE research_run SET status = 'running', stage = 'persist', "
            "end_time = NULL, error = NULL, update_time = CURRENT_TIMESTAMP "
            "WHERE id = :id AND owner_id = :owner AND status = 'completed'"
        ),
        {"id": run_id, "owner": owner_id},
    )
    session.commit()
    session.expire_all()  # 原生 SQL 改动不进身份映射，重读才拿到新状态
    return get_owned_run(session, run_id, owner_id)


def retry_run(session: Session, run_id: str, owner_id: str) -> ResearchRunModel | None:
    """重试：``failed -> running``，从已持久化阶段（checkpoint 链尾）继续。

    任务 017 §4.7：执行失败后可从已持久化阶段继续，不重复创建同一批
    论文（入库幂等兜底）。``cancelled`` 是用户意图，不自动重试；
    已终态的其它状态返回 None。
    """
    result = session.execute(
        text(
            "UPDATE research_run SET status = 'running', end_time = NULL, "
            "update_time = CURRENT_TIMESTAMP "
            "WHERE id = :id AND owner_id = :owner AND status = 'failed'"
        ),
        {"id": run_id, "owner": owner_id},
    )
    session.commit()
    if not result.rowcount:
        return None
    run = get_owned_run(session, run_id, owner_id)
    if run is not None:
        session.refresh(run)
    return run


def cancel_run(session: Session, run_id: str, owner_id: str) -> ResearchRunModel | None:
    """取消运行（原子 UPDATE）。已终态保持原状并返回当前运行。"""
    current = run_status(session, run_id, owner_id)
    if current is None:
        return None
    if current in RUN_TERMINAL_STATUSES:
        return get_owned_run(session, run_id, owner_id)
    session.execute(
        text(
            "UPDATE research_run SET status = 'cancelled', end_time = CURRENT_TIMESTAMP, "
            "error = COALESCE(error, '用户取消'), update_time = CURRENT_TIMESTAMP "
            "WHERE id = :id AND owner_id = :owner "
            "AND status IN ('pending', 'awaiting_confirmation', 'running')"
        ),
        {"id": run_id, "owner": owner_id},
    )
    session.commit()
    run = get_owned_run(session, run_id, owner_id)
    if run is not None:
        session.refresh(run)
    return run


def set_stage(session: Session, run_id: str, owner_id: str, stage: str) -> None:
    """持久化阶段位置（恢复与展示的事实来源之一）；失败仅回滚不抛出。

    **阶段只前进不后退**：恢复重放时已完成的阶段仍会被访问（幂等跳过），
    若允许把标记倒回，后续节点的"已完成"判断会失真并重复产生副作用
    （重复挂起人工门禁、重复出版本等）。用户显式重开产物阶段由
    ``reopen_run_for_artifacts`` 直接改写，不走本函数。
    """
    if stage not in RUN_STAGES:
        return
    try:
        current = session.execute(
            text(
                "SELECT stage FROM research_run WHERE id = :id AND owner_id = :owner"
            ),
            {"id": run_id, "owner": owner_id},
        ).first()
        current_stage = (current[0] if current and current[0] else "") or ""
        if STAGE_ORDER.get(stage, -1) < STAGE_ORDER.get(current_stage, -1):
            return
        session.execute(
            text(
                "UPDATE research_run SET stage = :stage, update_time = CURRENT_TIMESTAMP "
                "WHERE id = :id AND owner_id = :owner "
                "AND status IN ('pending', 'awaiting_confirmation', 'running')"
            ),
            {"stage": stage, "id": run_id, "owner": owner_id},
        )
        session.commit()
        session.expire_all()  # 同会话后续 ORM 读取必须看到新阶段
    except Exception:
        # 阶段写入失败不阻断业务，但必须可见：阶段是恢复与重放的事实来源
        logger.warning("写入研究运行阶段失败：run=%s stage=%s", run_id, stage, exc_info=True)
        try:
            session.rollback()
        except Exception:
            pass


def set_source_stats(
    session: Session, run_id: str, owner_id: str, stats: list[dict]
) -> None:
    """持久化各来源命中/失败统计；失败仅回滚不抛出。"""
    try:
        session.execute(
            text(
                "UPDATE research_run SET sources_json = :stats, "
                "update_time = CURRENT_TIMESTAMP "
                "WHERE id = :id AND owner_id = :owner"
            ),
            {"stats": json.dumps(stats, ensure_ascii=False)[:4000], "id": run_id,
             "owner": owner_id},
        )
        session.commit()
    except Exception:
        try:
            session.rollback()
        except Exception:
            pass


def merge_run_summary(
    session: Session, run_id: str, owner_id: str, patch: dict
) -> None:
    """把某阶段的业务产物统计并入 ``summary_json``（阶段先落库再推进）。

    只合并、不覆盖已有键；失败仅回滚不抛出（统计缺失不能拖垮研究图）。
    """
    if not patch:
        return
    try:
        row = session.execute(
            text("SELECT summary_json FROM research_run WHERE id = :id AND owner_id = :owner"),
            {"id": run_id, "owner": owner_id},
        ).first()
        current: dict = {}
        if row and row[0]:
            try:
                loaded = json.loads(row[0])
                if isinstance(loaded, dict):
                    current = loaded
            except (json.JSONDecodeError, TypeError):
                current = {}
        current.update(patch)
        session.execute(
            text(
                "UPDATE research_run SET summary_json = :summary, "
                "update_time = CURRENT_TIMESTAMP WHERE id = :id AND owner_id = :owner"
            ),
            {"summary": json.dumps(current, ensure_ascii=False)[:8000], "id": run_id,
             "owner": owner_id},
        )
        session.commit()
    except Exception:
        try:
            session.rollback()
        except Exception:
            pass


def link_agent_run(session: Session, run_id: str, owner_id: str, agent_run_id: str) -> None:
    """关联现有 Agent Run（时间线/取消/Trace 复用）。"""
    try:
        session.execute(
            text(
                "UPDATE research_run SET agent_run_id = :ar, update_time = CURRENT_TIMESTAMP "
                "WHERE id = :id AND owner_id = :owner"
            ),
            {"ar": agent_run_id, "id": run_id, "owner": owner_id},
        )
        session.commit()
    except Exception:
        try:
            session.rollback()
        except Exception:
            pass


def finalize_run(
    session: Session,
    run_id: str,
    owner_id: str,
    status: str,
    *,
    summary_json: str | None = None,
    error: str | None = None,
    stage: str | None = None,
) -> ResearchRunModel | None:
    """把运行收敛到终态（原子 UPDATE，仅非终态可迁移）。已终态返回 None。"""
    if status not in RUN_TERMINAL_STATUSES:
        raise ValueError(f"非法终态：{status}")
    result = session.execute(
        text(
            "UPDATE research_run SET status = :status, end_time = CURRENT_TIMESTAMP, "
            "summary_json = COALESCE(:summary, summary_json), "
            "error = COALESCE(:err, error), "
            "stage = COALESCE(:stage, stage), "
            "update_time = CURRENT_TIMESTAMP "
            "WHERE id = :id AND owner_id = :owner "
            "AND status IN ('pending', 'awaiting_confirmation', 'running')"
        ),
        {
            "status": status,
            "summary": summary_json,
            "err": error,
            "stage": stage,
            "id": run_id,
            "owner": owner_id,
        },
    )
    session.commit()
    if not result.rowcount:
        return None
    run = get_owned_run(session, run_id, owner_id)
    if run is not None:
        session.refresh(run)
    return run


def converge_run_best_effort(
    session_factory, run_id: str | None, owner_id: str | None,
    error: str = "运行中断，未正常结束",
) -> bool:
    """尽力终态收敛：仍 active 的运行置 failed；吞掉一切异常。"""
    if not run_id or not owner_id:
        return False
    fresh = session_factory()
    try:
        status = run_status(fresh, run_id, owner_id)
        # awaiting_confirmation 是人工门禁（计划确认 / 证据复核 / 报告复核）
        # 的正常挂起点，不是崩溃，绝不能被收敛成 failed
        if status not in (None, *RUN_TERMINAL_STATUSES, "awaiting_confirmation"):
            finalize_run(fresh, run_id, owner_id, "failed", error=error[:1000])
            return True
    except Exception:
        pass
    finally:
        try:
            fresh.close()
        except Exception:
            pass
    return False


# ---------------------------------------------------------------------------
# 论文记录（公开元数据）与项目论文（私有关系）
# ---------------------------------------------------------------------------


def find_paper_by_key(
    session: Session,
    *,
    doi: str | None = None,
    arxiv_id: str | None = None,
    url: str | None = None,
    title_key: str | None = None,
) -> PaperRecordModel | None:
    """按稳定标识查规范论文记录（优先级由调用方保证：doi > arxiv > url > title_key）。"""
    if doi:
        row = (
            session.query(PaperRecordModel)
            .filter(PaperRecordModel.doi == doi)
            .one_or_none()
        )
        if row is not None:
            return row
    if arxiv_id:
        row = (
            session.query(PaperRecordModel)
            .filter(PaperRecordModel.arxiv_id == arxiv_id)
            .one_or_none()
        )
        if row is not None:
            return row
    if url:
        row = (
            session.query(PaperRecordModel)
            .filter(PaperRecordModel.url == url)
            .one_or_none()
        )
        if row is not None:
            return row
    if title_key:
        row = (
            session.query(PaperRecordModel)
            .filter(PaperRecordModel.title_key == title_key)
            .all()
        )
        # title_key 非唯一（不同版本/预印本可能同键）：取首个可合并记录
        return row[0] if row else None
    return None


def merge_paper_sources(record: PaperRecordModel, source: str, url: str | None) -> bool:
    """把新来源并入 ``sources_json``；返回是否发生变化（幂等）。"""
    try:
        sources = json.loads(record.sources_json or "[]")
    except (json.JSONDecodeError, TypeError):
        sources = []
    entry = {"source": source or "", "url": url or ""}
    for s in sources:
        if (s.get("source"), s.get("url")) == (entry["source"], entry["url"]):
            return False
    if (not entry["source"]) and (not entry["url"]):
        return False
    sources.append(entry)
    record.sources_json = json.dumps(sources, ensure_ascii=False)[:4000]
    return True


def create_paper(session: Session, paper: dict) -> PaperRecordModel:
    """按规范化字段新建规范论文记录（查重由 upsert_paper 负责）。"""
    record = PaperRecordModel(
        id=uuid.uuid4().hex,
        title=(paper.get("title") or "")[:1000],
        authors=(paper.get("authors") or "")[:2000],
        year=paper.get("year"),
        abstract=(paper.get("abstract") or "")[:8000],
        doi=paper.get("doi"),
        arxiv_id=paper.get("arxiv_id"),
        url=(paper.get("url") or None),
        source=(paper.get("source") or "")[:50],
        sources_json=json.dumps(
            paper.get("sources") or [{"source": paper.get("source") or "",
                                      "url": paper.get("url") or ""}],
            ensure_ascii=False,
        )[:4000],
        fulltext_status=paper.get("fulltext_status") or "unknown",
        title_key=paper.get("title_key"),
        create_time=_now(),
        update_time=_now(),
    )
    session.add(record)
    return record


def upsert_paper(session: Session, paper: dict) -> tuple[PaperRecordModel, bool]:
    """幂等入库规范论文：命中稳定标识则合并来源/补齐缺失字段，否则新建。

    查重优先级：doi → arxiv_id → url → title_key。
    返回 ``(record, created)``；不提交（由调用方统一提交，保证与
    ``link_project_paper`` 同事务的幂等性）。
    """
    record = find_paper_by_key(
        session,
        doi=paper.get("doi") or None,
        arxiv_id=paper.get("arxiv_id") or None,
        url=paper.get("url") or None,
        title_key=paper.get("title_key") or None,
    )
    if record is not None:
        changed = False
        # 合并候选携带的全部来源（含批内去重合并的来源），不丢失可追溯性
        for entry in paper.get("sources") or []:
            if isinstance(entry, dict):
                if merge_paper_sources(record, entry.get("source") or "", entry.get("url")):
                    changed = True
        if not (paper.get("sources") or []):
            if merge_paper_sources(record, paper.get("source") or "", paper.get("url")):
                changed = True
        # 补齐缺失字段（不覆盖已有值；摘要只在为空时补）
        if not record.doi and paper.get("doi"):
            record.doi = paper["doi"]
            changed = True
        if not record.arxiv_id and paper.get("arxiv_id"):
            record.arxiv_id = paper["arxiv_id"]
            changed = True
        if not record.url and paper.get("url"):
            record.url = paper["url"]
            changed = True
        if not record.year and paper.get("year"):
            record.year = paper["year"]
            changed = True
        if not (record.abstract or "").strip() and paper.get("abstract"):
            record.abstract = (paper["abstract"] or "")[:8000]
            changed = True
        if not (record.authors or "").strip() and paper.get("authors"):
            record.authors = (paper["authors"] or "")[:2000]
            changed = True
        if record.fulltext_status == "unknown" and paper.get("fulltext_status"):
            record.fulltext_status = paper["fulltext_status"]
            changed = True
        if changed:
            record.update_time = _now()
        return record, False
    record = create_paper(session, paper)
    return record, True


def link_project_paper(
    session: Session,
    project_id: str,
    owner_id: str,
    paper_id: str,
    *,
    source: str = "",
    relevance: float | None = None,
    run_id: str | None = None,
) -> ProjectPaperModel:
    """幂等挂接项目论文关系；已存在只更新相关度/来源（不重置筛选状态）。"""
    existing = (
        session.query(ProjectPaperModel)
        .filter(
            ProjectPaperModel.project_id == project_id,
            ProjectPaperModel.paper_id == paper_id,
        )
        .one_or_none()
    )
    if existing is not None:
        if relevance is not None and (existing.relevance or 0) < relevance:
            existing.relevance = relevance
            existing.update_time = _now()
        return existing
    pp = ProjectPaperModel(
        id=uuid.uuid4().hex,
        project_id=project_id,
        paper_id=paper_id,
        owner_id=owner_id,
        source=(source or "")[:50],
        relevance=relevance,
        screening_status="pending_review",
        screening_reason="",
        first_run_id=run_id,
        create_time=_now(),
        update_time=_now(),
    )
    session.add(pp)
    return pp


def list_project_papers(
    session: Session,
    project_id: str,
    owner_id: str,
    *,
    screening_status: str | None = None,
) -> list[tuple[ProjectPaperModel, PaperRecordModel]]:
    """项目论文列表（含论文元数据）；项目不可见返回 []。"""
    if get_owned_project(session, project_id, owner_id) is None:
        return []
    q = (
        session.query(ProjectPaperModel, PaperRecordModel)
        .join(PaperRecordModel, ProjectPaperModel.paper_id == PaperRecordModel.id)
        .filter(
            ProjectPaperModel.project_id == project_id,
            ProjectPaperModel.owner_id == owner_id,
        )
    )
    if screening_status:
        q = q.filter(ProjectPaperModel.screening_status == screening_status)
    return q.order_by(
        ProjectPaperModel.relevance.desc(), ProjectPaperModel.create_time.asc()
    ).all()


def get_owned_project_paper(
    session: Session, project_paper_id: str, owner_id: str
) -> ProjectPaperModel | None:
    if not project_paper_id or not owner_id:
        return None
    return (
        session.query(ProjectPaperModel)
        .filter(
            ProjectPaperModel.id == project_paper_id,
            ProjectPaperModel.owner_id == owner_id,
        )
        .one_or_none()
    )


def set_screening(
    session: Session,
    project_paper_id: str,
    owner_id: str,
    screening_status: str,
    reason: str = "",
) -> ProjectPaperModel:
    """修改论文筛选状态（pending_review/included/excluded）+ 原因。"""
    if screening_status not in SCREENING_STATUSES:
        raise ValueError(f"非法筛选状态：{screening_status}")
    pp = get_owned_project_paper(session, project_paper_id, owner_id)
    if pp is None:
        raise ResearchResourceNotFound(project_paper_id)
    pp.screening_status = screening_status
    pp.screening_reason = (reason or "")[:1000]
    pp.update_time = _now()
    session.commit()
    session.refresh(pp)
    return pp


# ---------------------------------------------------------------------------
# 论文原文单元（任务 018 阶段 A）
# ---------------------------------------------------------------------------


def _sha256(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def get_project_paper(
    session: Session, project_id: str, paper_id: str, owner_id: str
) -> ProjectPaperModel:
    """项目内论文关系（不可见抛 ResearchResourceNotFound）。"""
    pp = (
        session.query(ProjectPaperModel)
        .filter(
            ProjectPaperModel.project_id == project_id,
            ProjectPaperModel.paper_id == paper_id,
            ProjectPaperModel.owner_id == owner_id,
        )
        .one_or_none()
    )
    if pp is None:
        raise ResearchResourceNotFound(f"{project_id}:{paper_id}")
    return pp


def replace_paper_units(
    session: Session,
    *,
    project_id: str,
    owner_id: str,
    paper_id: str,
    run_id: str | None = None,
    units: list[dict],
) -> dict:
    """幂等写入某论文在**本项目内**的原文单元（用户私有）。

    ``units`` 每项为 ``{unit_kind, page, section, block_index, text, source_kind}``。

    - 先算集合哈希；与已记录的 ``fulltext_hash`` 一致且单元数一致时
      **完全不写**：重跑解析不产生新行、不刷新时间，也不影响已有证据。
    - 需要更新时在同一事务内先删旧单元再插新单元，任一时刻都不会出现
      半新半旧的混合定位。
    - 只作用于本项目的私有单元；公开 ``paper_record`` 不写入任何私有内容。
    - 返回 ``{changed, created, state, fulltext_hash}``。
    """
    pp = get_project_paper(session, project_id, paper_id, owner_id)

    prepared = []
    for item in units:
        kind = item.get("unit_kind") or "block"
        if kind not in UNIT_KINDS:
            raise ValueError(f"非法原文单元类型：{kind}")
        body = (item.get("text") or "").strip()
        if not body:
            continue
        page = item.get("page")
        if kind == "abstract":
            # 摘要级单元绝不携带页码，避免下游伪造定位
            page = None
        prepared.append(
            {
                "unit_kind": kind,
                "page": page,
                "section": (item.get("section") or "")[:200],
                "block_index": int(item.get("block_index") or 0),
                "text": body[:8000],
                "source_kind": (item.get("source_kind") or "")[:32],
            }
        )

    state = "none"
    if any(u["unit_kind"] == "block" for u in prepared):
        state = "fulltext"
    elif prepared:
        state = "abstract"

    collection_hash = _sha256(
        "|".join(
            f"{u['unit_kind']}:{u['block_index']}:{_sha256(u['text'])}" for u in prepared
        )
    )
    existing_count = (
        session.query(PaperUnitModel)
        .filter(
            PaperUnitModel.project_id == project_id,
            PaperUnitModel.paper_id == paper_id,
            PaperUnitModel.owner_id == owner_id,
        )
        .count()
    )
    if prepared and existing_count == len(prepared) and pp.fulltext_hash == collection_hash:
        # 内容完全一致：幂等跳过
        return {
            "changed": False,
            "created": 0,
            "state": pp.fulltext_state or state,
            "fulltext_hash": collection_hash,
        }

    session.query(PaperUnitModel).filter(
        PaperUnitModel.project_id == project_id,
        PaperUnitModel.paper_id == paper_id,
        PaperUnitModel.owner_id == owner_id,
    ).delete(synchronize_session=False)
    now = _now()
    for u in prepared:
        session.add(
            PaperUnitModel(
                id=uuid.uuid4().hex,
                project_id=project_id,
                paper_id=paper_id,
                owner_id=owner_id,
                run_id=run_id,
                unit_kind=u["unit_kind"],
                page=u["page"],
                section=u["section"],
                block_index=u["block_index"],
                text=u["text"],
                char_count=len(u["text"]),
                content_hash=_sha256(u["text"]),
                source_kind=u["source_kind"],
                create_time=now,
                update_time=now,
            )
        )
    pp.fulltext_state = state
    pp.unit_count = len(prepared)
    pp.fulltext_hash = collection_hash
    pp.fulltext_time = now
    pp.update_time = now
    session.commit()
    return {
        "changed": True,
        "created": len(prepared),
        "state": state,
        "fulltext_hash": collection_hash,
    }


def list_paper_units(
    session: Session, project_id: str, owner_id: str, paper_id: str | None = None
) -> list[PaperUnitModel]:
    """本项目某论文的原文单元（按 kind + 槽位排序）；项目不可见返回 []。"""
    if get_owned_project(session, project_id, owner_id) is None:
        return []
    q = session.query(PaperUnitModel).filter(
        PaperUnitModel.project_id == project_id,
        PaperUnitModel.owner_id == owner_id,
    )
    if paper_id:
        q = q.filter(PaperUnitModel.paper_id == paper_id)
    return q.order_by(
        PaperUnitModel.paper_id.asc(),
        PaperUnitModel.unit_kind.asc(),
        PaperUnitModel.block_index.asc(),
    ).all()


def get_owned_unit(
    session: Session, unit_id: str, owner_id: str
) -> PaperUnitModel | None:
    if not unit_id or not owner_id:
        return None
    return (
        session.query(PaperUnitModel)
        .filter(PaperUnitModel.id == unit_id, PaperUnitModel.owner_id == owner_id)
        .one_or_none()
    )


# ---------------------------------------------------------------------------
# 证据卡片（任务 018 阶段 A）
# ---------------------------------------------------------------------------


def evidence_dedup_key(
    project_id: str,
    paper_id: str,
    sub_question_id: str,
    evidence_type: str,
    quote: str,
) -> str:
    """提取重试的幂等键：同一项目/论文/子问题/类型/原文只保留一张卡片。"""
    return _sha256(
        "\x1f".join([project_id, paper_id, sub_question_id, evidence_type, quote or ""])
    )


def get_owned_evidence(
    session: Session, evidence_id: str, owner_id: str
) -> EvidenceCardModel | None:
    if not evidence_id or not owner_id:
        return None
    return (
        session.query(EvidenceCardModel)
        .filter(EvidenceCardModel.id == evidence_id, EvidenceCardModel.owner_id == owner_id)
        .one_or_none()
    )


def list_evidence(
    session: Session,
    project_id: str,
    owner_id: str,
    *,
    paper_id: str | None = None,
    sub_question_id: str | None = None,
    review_status: str | None = None,
    include_rejected: bool = True,
) -> list[EvidenceCardModel]:
    """项目证据列表；项目不可见返回 []。默认含 rejected（UI 需要过滤展示）。"""
    if get_owned_project(session, project_id, owner_id) is None:
        return []
    q = session.query(EvidenceCardModel).filter(
        EvidenceCardModel.project_id == project_id,
        EvidenceCardModel.owner_id == owner_id,
    )
    if paper_id:
        q = q.filter(EvidenceCardModel.paper_id == paper_id)
    if sub_question_id:
        q = q.filter(EvidenceCardModel.sub_question_id == sub_question_id)
    if review_status:
        q = q.filter(EvidenceCardModel.review_status == review_status)
    elif not include_rejected:
        q = q.filter(EvidenceCardModel.review_status != "rejected")
    return q.order_by(
        EvidenceCardModel.paper_id.asc(),
        EvidenceCardModel.sub_question_id.asc(),
        EvidenceCardModel.create_time.asc(),
    ).all()


def upsert_evidence(
    session: Session,
    *,
    project_id: str,
    owner_id: str,
    paper_id: str,
    sub_question_id: str,
    evidence_type: str,
    quote: str,
    summary: str = "",
    unit: PaperUnitModel | None = None,
    run_id: str | None = None,
    level: str = "fulltext",
    source_kind: str = "",
    extraction_model: str = "",
    extraction_mode: str = "llm",
    quote_verified: bool = True,
) -> tuple[EvidenceCardModel, str]:
    """写入一张证据卡片，返回 ``(卡片, 动作)``。

    动作取值：

    - ``created``：新建（``review_status=pending``，等待用户审核）；
    - ``updated``：命中同一幂等键且旧卡片**尚未审核** → 刷新提取结果，
      内容确有变化时 ``version + 1``；
    - ``unchanged``：命中且内容完全一致，不做任何写入；
    - ``kept_reviewed``：命中但旧卡片**已被用户审核**（confirmed/edited/
      rejected）→ **绝不覆盖**，返回原卡片。

    ``quote`` 必须是提供给模型的原文片段（调用方已逐字校验），
    ``quote_verified`` 只在校验通过时为 1；定位以 ``locator_json`` 快照
    保存，单元被替换后仍可核对。
    """
    if evidence_type and evidence_type not in EVIDENCE_TYPES:
        raise ValueError(f"非法证据类型：{evidence_type}")
    if level not in EVIDENCE_LEVELS:
        raise ValueError(f"非法证据级别：{level}")
    if not (quote or "").strip():
        raise ValueError("证据必须包含可核对原文 quote")

    quote = quote.strip()[:4000]
    summary = (summary or "").strip()[:2000]
    dedup = evidence_dedup_key(project_id, paper_id, sub_question_id, evidence_type, quote)
    existing = (
        session.query(EvidenceCardModel)
        .filter(
            EvidenceCardModel.project_id == project_id,
            EvidenceCardModel.dedup_key == dedup,
        )
        .one_or_none()
    )
    locator = {
        "unit_id": unit.id if unit else None,
        "unit_kind": unit.unit_kind if unit else None,
        "page": unit.page if unit else None,
        "section": unit.section if unit else None,
        "block_index": unit.block_index if unit else None,
        "char_count": unit.char_count if unit else None,
        "source_kind": source_kind or (unit.source_kind if unit else ""),
    }
    locator_json = json.dumps(locator, ensure_ascii=False)[:2000]

    if existing is not None:
        if existing.review_status in REVIEWED_STATUSES:
            return existing, "kept_reviewed"
        same = (
            existing.summary == summary
            and existing.quote == quote
            and existing.level == level
            and existing.locator_json == locator_json
            and bool(existing.quote_verified) == bool(quote_verified)
        )
        if same:
            return existing, "unchanged"
        existing.summary = summary
        existing.quote = quote
        existing.content = (quote or summary)[:8000]
        existing.level = level
        existing.unit_id = unit.id if unit else None
        existing.locator_json = locator_json
        existing.quote_verified = 1 if quote_verified else 0
        existing.source_kind = (source_kind or locator["source_kind"] or "")[:32]
        existing.extraction_model = (extraction_model or "")[:200]
        existing.extraction_mode = extraction_mode[:16]
        existing.run_id = run_id
        existing.version = (existing.version or 1) + 1
        existing.update_time = _now()
        session.commit()
        session.refresh(existing)
        return existing, "updated"

    card = EvidenceCardModel(
        id=uuid.uuid4().hex,
        project_id=project_id,
        paper_id=paper_id,
        owner_id=owner_id,
        run_id=run_id,
        content=(quote or summary)[:8000],
        locator_json=locator_json,
        version=1,
        sub_question_id=sub_question_id,
        evidence_type=evidence_type,
        summary=summary,
        quote=quote,
        unit_id=unit.id if unit else None,
        quote_verified=1 if quote_verified else 0,
        level=level,
        source_kind=(source_kind or locator["source_kind"] or "")[:32],
        extraction_model=(extraction_model or "")[:200],
        extraction_mode=extraction_mode[:16],
        review_status="pending",
        review_note="",
        dedup_key=dedup,
        create_time=_now(),
        update_time=_now(),
    )
    session.add(card)
    session.commit()
    session.refresh(card)
    return card, "created"


def review_evidence(
    session: Session,
    evidence_id: str,
    owner_id: str,
    *,
    review_status: str,
    summary: str | None = None,
    quote: str | None = None,
    note: str = "",
) -> EvidenceCardModel:
    """用户审核证据卡片（confirmed/edited/rejected）。

    - 带 ``summary``/``quote`` 的修改视为编辑：``review_status`` 强制为
      ``edited``，``version + 1``，旧值不静默丢弃（版本号可追溯）；
    - 只允许操作自己的卡片，不可见抛 ResearchResourceNotFound；
    - 编辑原文时必须仍能在其定位单元中核对（由调用方校验后传入），
      校验结果同步写入 ``quote_verified``。
    """
    if review_status not in REVIEW_STATUSES or review_status == "pending":
        raise ValueError(f"非法审核状态：{review_status}")
    card = get_owned_evidence(session, evidence_id, owner_id)
    if card is None:
        raise ResearchResourceNotFound(evidence_id)

    edited = False
    if summary is not None and summary.strip() != (card.summary or ""):
        card.summary = summary.strip()[:2000]
        edited = True
    if quote is not None and quote.strip() != (card.quote or ""):
        new_quote = quote.strip()[:4000]
        if not new_quote:
            raise ValueError("证据原文不能清空")
        card.quote = new_quote
        card.content = new_quote[:8000]
        card.dedup_key = evidence_dedup_key(
            card.project_id,
            card.paper_id,
            card.sub_question_id,
            card.evidence_type,
            new_quote,
        )
        edited = True
    if edited:
        review_status = "edited"
        card.version = (card.version or 1) + 1

    card.review_status = review_status
    card.review_note = (note or "")[:1000]
    card.reviewed_at = _now()
    card.update_time = _now()
    session.commit()
    session.refresh(card)
    return card


def set_evidence_verified(
    session: Session, evidence_id: str, owner_id: str, verified: bool
) -> EvidenceCardModel:
    """回写原文核对结果（重新核对时调用）。"""
    card = get_owned_evidence(session, evidence_id, owner_id)
    if card is None:
        raise ResearchResourceNotFound(evidence_id)
    card.quote_verified = 1 if verified else 0
    card.update_time = _now()
    session.commit()
    session.refresh(card)
    return card


def delete_evidence(session: Session, evidence_id: str, owner_id: str) -> None:
    card = get_owned_evidence(session, evidence_id, owner_id)
    if card is None:
        raise ResearchResourceNotFound(evidence_id)
    session.delete(card)
    session.commit()


# ---------------------------------------------------------------------------
# 比较维度（任务 018 阶段 B）
# ---------------------------------------------------------------------------


def get_project_dimensions(session: Session, project_id: str, owner_id: str) -> list[dict]:
    """项目的比较维度；未自定义时返回默认维度。"""
    project = get_owned_project(session, project_id, owner_id)
    if project is None:
        raise ResearchResourceNotFound(project_id)
    return parse_dimensions_json(project.dimensions_json)


def parse_dimensions_json(raw: str | None) -> list[dict]:
    """解析维度 JSON；为空或非法时回落到默认维度。"""
    if not raw:
        return [dict(d) for d in DEFAULT_DIMENSIONS]
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return [dict(d) for d in DEFAULT_DIMENSIONS]
    if not isinstance(data, list):
        return [dict(d) for d in DEFAULT_DIMENSIONS]
    dims = _normalize_dimensions(data)
    return dims or [dict(d) for d in DEFAULT_DIMENSIONS]


def _normalize_dimensions(items: list) -> list[dict]:
    out: list[dict] = []
    seen: set = set()
    for item in items[:MAX_DIMENSIONS]:
        if isinstance(item, str):
            dim_id, label = item, item
        elif isinstance(item, dict):
            dim_id = str(item.get("id") or "").strip()
            label = str(item.get("label") or dim_id).strip()
        else:
            continue
        dim_id = (dim_id or label)[:64]
        if not dim_id or dim_id in seen:
            continue
        seen.add(dim_id)
        out.append({"id": dim_id, "label": label[:200]})
    return out


def set_project_dimensions(
    session: Session, project_id: str, owner_id: str, dimensions: list
) -> list[dict]:
    """设置项目比较维度（用户可增删/调整）；空列表表示恢复默认。"""
    project = get_owned_project(session, project_id, owner_id)
    if project is None:
        raise ResearchResourceNotFound(project_id)
    dims = _normalize_dimensions(dimensions or [])
    project.dimensions_json = json.dumps(dims, ensure_ascii=False)[:4000] if dims else ""
    project.update_time = _now()
    session.commit()
    return parse_dimensions_json(project.dimensions_json)


# ---------------------------------------------------------------------------
# 比较单元（必须引用具体证据卡片）
# ---------------------------------------------------------------------------


def list_comparison_units(
    session: Session,
    project_id: str,
    owner_id: str,
    *,
    paper_id: str | None = None,
    dimension_id: str | None = None,
) -> list[ComparisonUnitModel]:
    if get_owned_project(session, project_id, owner_id) is None:
        return []
    q = session.query(ComparisonUnitModel).filter(
        ComparisonUnitModel.project_id == project_id,
        ComparisonUnitModel.owner_id == owner_id,
    )
    if paper_id:
        q = q.filter(ComparisonUnitModel.paper_id == paper_id)
    if dimension_id:
        q = q.filter(ComparisonUnitModel.dimension_id == dimension_id)
    return q.order_by(
        ComparisonUnitModel.dimension_id.asc(), ComparisonUnitModel.paper_id.asc()
    ).all()


def replace_comparison_units(
    session: Session,
    *,
    project_id: str,
    owner_id: str,
    run_id: str | None = None,
    units: list[dict],
) -> dict:
    """幂等写入比较单元，返回 ``{written, skipped, rejected}``。

    每个单元必须带 ``paper_id``/``dimension_id``/``content`` 和
    ``evidence_ids``；引用的证据卡片必须属于本项目且属于该用户，
    否则整条被拒绝（比较结论永远可以反查到真实原文证据）。
    """
    if get_owned_project(session, project_id, owner_id) is None:
        raise ResearchResourceNotFound(project_id)
    owned_ids = {
        card.id
        for card in session.query(EvidenceCardModel)
        .filter(
            EvidenceCardModel.project_id == project_id,
            EvidenceCardModel.owner_id == owner_id,
        )
        .all()
    }
    written = skipped = 0
    rejected: list[str] = []
    now = _now()
    for pos, item in enumerate(units):
        paper_id = str(item.get("paper_id") or "")
        dimension_id = str(item.get("dimension_id") or "").strip()[:64]
        content = str(item.get("content") or "").strip()[:4000]
        evidence_ids = [str(e) for e in (item.get("evidence_ids") or []) if str(e)]
        valid = [e for e in evidence_ids if e in owned_ids]
        if not paper_id or not dimension_id or not content:
            rejected.append(f"第 {pos + 1} 条比较单元缺少 paper_id/dimension_id/content")
            continue
        if not valid:
            rejected.append(
                f"第 {pos + 1} 条比较单元（{dimension_id}）没有引用本项目内真实存在的证据卡片，已丢弃"
            )
            continue
        if len(valid) != len(evidence_ids):
            rejected.append(
                f"第 {pos + 1} 条比较单元引用了不属于本项目的证据，已按有效引用保留"
            )
        levels = {
            card.level
            for card in session.query(EvidenceCardModel)
            .filter(EvidenceCardModel.id.in_(valid))
            .all()
        }
        level = "fulltext" if "fulltext" in levels else "abstract"
        existing = (
            session.query(ComparisonUnitModel)
            .filter(
                ComparisonUnitModel.project_id == project_id,
                ComparisonUnitModel.paper_id == paper_id,
                ComparisonUnitModel.dimension_id == dimension_id,
            )
            .one_or_none()
        )
        payload = (content, json.dumps(valid, ensure_ascii=False)[:4000], level)
        if existing is not None:
            if (
                existing.content,
                existing.evidence_ids_json,
                existing.level,
            ) == payload:
                skipped += 1
                continue
            existing.content, existing.evidence_ids_json, existing.level = payload
            existing.run_id = run_id
            existing.version = (existing.version or 1) + 1
            existing.update_time = now
        else:
            session.add(
                ComparisonUnitModel(
                    id=uuid.uuid4().hex,
                    project_id=project_id,
                    paper_id=paper_id,
                    dimension_id=dimension_id,
                    owner_id=owner_id,
                    run_id=run_id,
                    content=payload[0],
                    evidence_ids_json=payload[1],
                    level=payload[2],
                    version=1,
                    create_time=now,
                    update_time=now,
                )
            )
        written += 1
    session.commit()
    return {"written": written, "skipped": skipped, "rejected": rejected}


# ---------------------------------------------------------------------------
# 冲突记录（两侧都必须引用证据）
# ---------------------------------------------------------------------------


def conflict_dedup_key(
    project_id: str, sub_question_id: str, dimension_id: str, side_a: list, side_b: list
) -> str:
    """冲突幂等键：两侧证据集合（与顺序无关）+ 维度 + 子问题。"""
    def norm(ids):
        return ",".join(sorted(set(ids)))

    a, b = norm(side_a), norm(side_b)
    if b < a:  # 保证 A/B 互换不产生第二条记录
        a, b = b, a
    return _sha256("\x1f".join([project_id, sub_question_id, dimension_id, a, b]))


def get_owned_conflict(
    session: Session, conflict_id: str, owner_id: str
) -> ResearchConflictModel | None:
    if not conflict_id or not owner_id:
        return None
    return (
        session.query(ResearchConflictModel)
        .filter(
            ResearchConflictModel.id == conflict_id,
            ResearchConflictModel.owner_id == owner_id,
        )
        .one_or_none()
    )


def list_conflicts(
    session: Session, project_id: str, owner_id: str, *, status_filter: str | None = None
) -> list[ResearchConflictModel]:
    if get_owned_project(session, project_id, owner_id) is None:
        return []
    q = session.query(ResearchConflictModel).filter(
        ResearchConflictModel.project_id == project_id,
        ResearchConflictModel.owner_id == owner_id,
    )
    if status_filter:
        q = q.filter(ResearchConflictModel.status == status_filter)
    return q.order_by(ResearchConflictModel.create_time.asc()).all()


def record_conflict(
    session: Session,
    *,
    project_id: str,
    owner_id: str,
    side_a: list[dict],
    side_b: list[dict],
    topic: str = "",
    description: str = "",
    sub_question_id: str = "",
    dimension_id: str = "",
    run_id: str | None = None,
) -> tuple[ResearchConflictModel, str]:
    """记录一条冲突；两侧都必须引用本项目真实存在的证据卡片。

    返回 ``(记录, 动作)``，动作为 ``created``/``unchanged``；同一对证据
    集合重复检测不产生第二条记录（幂等）。已被人工裁决（resolved）的记录
    不被自动检测覆盖。
    """
    if get_owned_project(session, project_id, owner_id) is None:
        raise ResearchResourceNotFound(project_id)
    owned = {
        card.id: card
        for card in session.query(EvidenceCardModel)
        .filter(
            EvidenceCardModel.project_id == project_id,
            EvidenceCardModel.owner_id == owner_id,
        )
        .all()
    }

    def _side(items: list[dict]) -> list[dict]:
        out = []
        for item in items:
            evidence_id = str(item.get("evidence_id") or "")
            card = owned.get(evidence_id)
            if card is None:
                continue
            out.append(
                {
                    "evidence_id": card.id,
                    "paper_id": card.paper_id,
                    "quote": (item.get("quote") or card.quote)[:400],
                }
            )
        return out

    a, b = _side(side_a), _side(side_b)
    if not a or not b:
        raise ValueError("冲突两侧都必须引用本项目内真实存在的证据卡片")
    ids_a = {x["evidence_id"] for x in a}
    ids_b = {x["evidence_id"] for x in b}
    if ids_a & ids_b:
        raise ValueError("冲突两侧不能引用同一张证据卡片")
    if {x["paper_id"] for x in a} == {x["paper_id"] for x in b} and len(ids_a | ids_b) < 2:
        raise ValueError("冲突必须涉及至少两条不同证据")

    key = conflict_dedup_key(
        project_id, sub_question_id, dimension_id, sorted(ids_a), sorted(ids_b)
    )
    existing = (
        session.query(ResearchConflictModel)
        .filter(
            ResearchConflictModel.project_id == project_id,
            ResearchConflictModel.dedup_key == key,
        )
        .one_or_none()
    )
    if existing is not None:
        return existing, "unchanged"

    conflict = ResearchConflictModel(
        id=uuid.uuid4().hex,
        project_id=project_id,
        owner_id=owner_id,
        run_id=run_id,
        sub_question_id=sub_question_id[:64],
        dimension_id=dimension_id[:64],
        topic=(topic or "")[:500],
        description=(description or "")[:4000],
        side_a_json=json.dumps(a, ensure_ascii=False)[:4000],
        side_b_json=json.dumps(b, ensure_ascii=False)[:4000],
        status="open",
        resolution="",
        dedup_key=key,
        version=1,
        create_time=_now(),
        update_time=_now(),
    )
    session.add(conflict)
    session.commit()
    session.refresh(conflict)
    return conflict, "created"


def resolve_conflict(
    session: Session, conflict_id: str, owner_id: str, resolution: str
) -> ResearchConflictModel:
    """人工裁决冲突（写入结论并把状态置为 resolved）。"""
    conflict = get_owned_conflict(session, conflict_id, owner_id)
    if conflict is None:
        raise ResearchResourceNotFound(conflict_id)
    conflict.status = "resolved"
    conflict.resolution = (resolution or "")[:1000]
    conflict.version = (conflict.version or 1) + 1
    conflict.update_time = _now()
    session.commit()
    session.refresh(conflict)
    return conflict


def delete_conflict(session: Session, conflict_id: str, owner_id: str) -> None:
    conflict = get_owned_conflict(session, conflict_id, owner_id)
    if conflict is None:
        raise ResearchResourceNotFound(conflict_id)
    session.delete(conflict)
    session.commit()


# ---------------------------------------------------------------------------
# 有界补充检索（复用任务 017 的运行入口）
# ---------------------------------------------------------------------------


def create_supplementary_run(
    session: Session, owner_id: str, project_id: str, parent_run_id: str
) -> ResearchRunModel:
    """基于上一轮运行创建补充检索的新运行（可见、需确认、轮次有界）。

    - 复用 ``create_run`` 与任务 017 的检索/去重/筛选流程，不复制检索逻辑；
    - ``round_no = 父运行 round_no + 1``，超过 ``MAX_RESEARCH_ROUNDS`` 直接
      409 语义（``ResearchConflict``），杜绝无限自循环。
    """
    parent = get_owned_run(session, parent_run_id, owner_id)
    if parent is None or parent.project_id != project_id:
        raise ResearchResourceNotFound(parent_run_id)
    round_no = int(parent.round_no or 1) + 1
    if round_no > MAX_RESEARCH_ROUNDS:
        raise ResearchConflict(
            f"补充检索已达最大轮次（{MAX_RESEARCH_ROUNDS}），请新建研究运行重新开始"
        )
    run = create_run(session, owner_id, project_id)
    run.parent_run_id = parent.id
    run.round_no = round_no
    session.commit()
    session.refresh(run)
    return run


# ---------------------------------------------------------------------------
# 研究报告：版本链 + 引用映射（任务 018 阶段 C）
# ---------------------------------------------------------------------------


def get_owned_report(
    session: Session, report_id: str, owner_id: str
) -> ResearchReportModel | None:
    if not report_id or not owner_id:
        return None
    return (
        session.query(ResearchReportModel)
        .filter(ResearchReportModel.id == report_id, ResearchReportModel.owner_id == owner_id)
        .one_or_none()
    )


def list_project_reports(
    session: Session, project_id: str, owner_id: str, *, limit: int = 50
) -> list[ResearchReportModel]:
    """项目全部报告版本（新版本在前）；项目不可见返回 []。"""
    if get_owned_project(session, project_id, owner_id) is None:
        return []
    return (
        session.query(ResearchReportModel)
        .filter(
            ResearchReportModel.project_id == project_id,
            ResearchReportModel.owner_id == owner_id,
        )
        .order_by(ResearchReportModel.version.desc(), ResearchReportModel.create_time.desc())
        .limit(max(1, min(200, int(limit))))
        .all()
    )


def latest_project_reports(
    session: Session, project_id: str, owner_id: str
) -> list[ResearchReportModel]:
    """每个逻辑报告（report_key）的最新版本。"""
    rows = list_project_reports(session, project_id, owner_id)
    seen: set = set()
    out: list[ResearchReportModel] = []
    for row in rows:
        key = row.report_key or row.id
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def create_report_version(
    session: Session,
    *,
    project_id: str,
    owner_id: str,
    title: str,
    content: str,
    citations: list[dict],
    evidence_ids: list[str],
    report_key: str | None = None,
    run_id: str | None = None,
    unsupported: str = "",
    generated_by: str = "",
    status: str = "generated",
) -> ResearchReportModel:
    """插入报告的新版本（同一 ``report_key`` 版本号递增，不覆盖旧版本）。"""
    if get_owned_project(session, project_id, owner_id) is None:
        raise ResearchResourceNotFound(project_id)
    key = report_key or uuid.uuid4().hex
    latest = (
        session.query(ResearchReportModel)
        .filter(
            ResearchReportModel.report_key == key,
            ResearchReportModel.owner_id == owner_id,
        )
        .order_by(ResearchReportModel.version.desc())
        .first()
    )
    version = int(latest.version or 0) + 1 if latest is not None else 1
    now = _now()
    report = ResearchReportModel(
        id=uuid.uuid4().hex,
        project_id=project_id,
        owner_id=owner_id,
        title=(title or "")[:500],
        content=(content or "")[:30000],
        version=version,
        report_key=key,
        run_id=run_id,
        status=status,
        citations_json=json.dumps(citations or [], ensure_ascii=False)[:8000],
        evidence_ids_json=json.dumps(evidence_ids or [], ensure_ascii=False)[:8000],
        unsupported=(unsupported or "")[:4000],
        generated_by=(generated_by or "")[:200],
        create_time=now,
        update_time=now,
    )
    session.add(report)
    session.commit()
    session.refresh(report)
    return report


def delete_report(session: Session, report_id: str, owner_id: str) -> None:
    """删除单个报告版本（其他版本不受影响）。"""
    report = get_owned_report(session, report_id, owner_id)
    if report is None:
        raise ResearchResourceNotFound(report_id)
    session.delete(report)
    session.commit()


__all__ = [
    "PROJECT_STATUSES",
    "RUN_STATUSES",
    "RUN_TERMINAL_STATUSES",
    "RUN_ACTIVE_STATUSES",
    "RUN_STAGES",
    "SCREENING_STATUSES",
    "UNIT_KINDS",
    "FULLTEXT_STATES",
    "EVIDENCE_TYPES",
    "EVIDENCE_LEVELS",
    "REVIEW_STATUSES",
    "REVIEWED_STATUSES",
    "ResearchResourceNotFound",
    "ResearchConflict",
    "create_project",
    "list_projects",
    "get_owned_project",
    "update_owned_project",
    "delete_owned_project",
    "create_run",
    "get_owned_run",
    "list_runs",
    "run_status",
    "set_plan",
    "update_plan",
    "confirm_run",
    "pause_run_for_review",
    "reopen_run_for_artifacts",
    "retry_run",
    "cancel_run",
    "set_stage",
    "set_source_stats",
    "merge_run_summary",
    "STAGE_ORDER",
    "link_agent_run",
    "finalize_run",
    "converge_run_best_effort",
    "find_paper_by_key",
    "merge_paper_sources",
    "create_paper",
    "upsert_paper",
    "link_project_paper",
    "list_project_papers",
    "get_owned_project_paper",
    "get_project_paper",
    "set_screening",
    "replace_paper_units",
    "list_paper_units",
    "get_owned_unit",
    "evidence_dedup_key",
    "get_owned_evidence",
    "list_evidence",
    "upsert_evidence",
    "review_evidence",
    "set_evidence_verified",
    "delete_evidence",
    "DEFAULT_DIMENSIONS",
    "MAX_DIMENSIONS",
    "MAX_RESEARCH_ROUNDS",
    "CONFLICT_STATUSES",
    "parse_dimensions_json",
    "get_project_dimensions",
    "set_project_dimensions",
    "list_comparison_units",
    "replace_comparison_units",
    "conflict_dedup_key",
    "get_owned_conflict",
    "list_conflicts",
    "record_conflict",
    "resolve_conflict",
    "delete_conflict",
    "create_supplementary_run",
    "get_owned_report",
    "list_project_reports",
    "latest_project_reports",
    "create_report_version",
    "delete_report",
]
