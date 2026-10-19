"""服务启动时的持久化运行状态收敛（《学生研究与导师审核功能重新设计方案》§15.0B 第 7 条）。

上游账号同一时间只能处理一个生成请求，服务重启会让正在跑或排队中的模型任务
失去执行方。它们如果继续显示"运行中"，用户既等不到结果也点不了重试，队列
观测也不可信。这里在应用启动时把它们收敛成明确状态：

- ``agent_run``：``pending`` / ``running`` → ``failed``，``error`` 写明是服务
  重启导致的中断，用户可以重新发起或恢复；
- ``research_run``：``running`` 同样处理；``awaiting_confirmation`` 是**人工
  等待点**，重启后必须原样保留，不能当成中断；
- AI 预审运行：由预审模块自己的 ``sweep_interrupted_runs`` 处理，本模块在
  它可用时调用（里程碑4 之后生效）。
- 论文检索子任务：由论文检索模块自己的 ``converge_interrupted_subtasks``
  处理，本模块在它可用时调用（里程碑3 之后生效）；已入库的候选论文不动。

收敛动作只改状态与错误摘要，不删除任何历史数据，并把结果写日志。
"""
from __future__ import annotations

from typing import Any, Dict

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from chatchat.server.db.models.agent_run_model import AgentRunModel
from chatchat.server.db.models.research_model import ResearchRunModel
from chatchat.server.db.session import SessionLocal
from chatchat.utils import build_logger

logger = build_logger(__name__)

#: 页面上要显示的原因，用户据此知道该重试而不是继续等
INTERRUPTED_REASON = "服务重启导致中断，可重新发起"


def sweep_agent_runs(session: Session) -> int:
    """把重启时仍在排队的 Agent Run 收敛为失败（可重试）。"""
    result = session.execute(
        update(AgentRunModel)
        .where(AgentRunModel.status.in_(("pending", "running")))
        .values(status="failed", error=INTERRUPTED_REASON)
    )
    return int(result.rowcount or 0)


def sweep_research_runs(session: Session) -> int:
    """收敛重启时仍在执行的论文研究运行；人工等待点不动。"""
    result = session.execute(
        update(ResearchRunModel)
        .where(ResearchRunModel.status == "running")
        .values(status="failed", error=INTERRUPTED_REASON)
    )
    return int(result.rowcount or 0)


def sweep_ai_review_runs(session: Session) -> int:
    """收敛重启时仍在排队/执行的 AI 预审运行（里程碑4 之后生效）。"""
    try:
        from chatchat.server.db.repository import supervision_ai_review_repository
    except ImportError:  # 预审模块尚未落地
        return 0
    hook = getattr(supervision_ai_review_repository, "sweep_interrupted_runs", None)
    if hook is None:
        return 0
    return int(hook(session) or 0)


def sweep_literature_subtasks(session: Session) -> int:
    """收敛重启时仍在排队/执行的论文检索子任务（里程碑3 之后生效）。

    检索、排序、摘要分析、深度提取与对比都在请求内同步执行，重启会让它们
    永远停在 ``queued``/``running``。仓储层自己知道每种 ``kind`` 收敛成什么
    状态、要不要保留已入库的候选，所以这里只转调，不自己改状态。
    """
    try:
        from chatchat.server.db.repository import supervision_literature_repository
    except ImportError:  # 论文检索模块尚未落地
        return 0
    hook = getattr(supervision_literature_repository, "converge_interrupted_subtasks", None)
    if hook is None:
        return 0
    return int(hook(session) or 0)


def sweep_interrupted_runs() -> Dict[str, Any]:
    """应用启动时调用；任何一项失败都不应该阻塞服务启动。"""
    summary: Dict[str, Any] = {}
    session = SessionLocal()
    try:
        summary["agent_run"] = sweep_agent_runs(session)
        summary["research_run"] = sweep_research_runs(session)
        summary["ai_review_run"] = sweep_ai_review_runs(session)
        summary["literature_subtask"] = sweep_literature_subtasks(session)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    total = sum(summary.values())
    if total:
        logger.warning(
            "启动收敛：{} 条持久化模型任务因服务重启被标记为中断（{}）",
            total,
            summary,
        )
    else:
        logger.info("启动收敛：没有遗留的运行中/排队中的模型任务")
    return summary


def list_interrupted_runs(session: Session) -> Dict[str, int]:
    """只读统计，供观测端点与测试使用。"""
    counts: Dict[str, int] = {}
    counts["agent_run"] = len(
        session.execute(
            select(AgentRunModel.id).where(
                AgentRunModel.status.in_(("pending", "running"))
            )
        )
        .scalars()
        .all()
    )
    counts["research_run"] = len(
        session.execute(
            select(ResearchRunModel.id).where(ResearchRunModel.status == "running")
        )
        .scalars()
        .all()
    )
    return counts


__all__ = [
    "INTERRUPTED_REASON",
    "list_interrupted_runs",
    "sweep_agent_runs",
    "sweep_ai_review_runs",
    "sweep_interrupted_runs",
    "sweep_literature_subtasks",
    "sweep_research_runs",
]
