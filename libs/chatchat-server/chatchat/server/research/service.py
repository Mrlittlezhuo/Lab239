"""论文研究服务层（任务 017）。

把研究仓储、LangGraph 运行时与现有 Agent Run/认证体系接入：

- 计划生成/执行/恢复走 ``ResearchRuntime``（复用任务 014 checkpoint）；
- 确认执行时创建并关联一条 ``agent_run`` 行（时间线/取消/Trace 复用，
  不建第二套执行器），研究终态同步到该 Agent Run 行；
- 取消研究运行时同步取消关联的 Agent Run 行；
- 计划器与来源可注入（测试注入假件），默认按现有模型/工具配置构建。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from chatchat.server.db.repository import agent_run_repository as agent_run_repo
from chatchat.server.db.repository import research_repository as repo
from chatchat.server.research.runtime import ResearchRuntime
from chatchat.utils import build_logger

logger = build_logger()


def _session_factory() -> Session:
    """持久化使用的会话工厂；测试中 monkeypatch 指向临时数据库。"""
    from chatchat.server.db.session import SessionLocal

    return SessionLocal()


def _build_planner(model_name: Optional[str] = None):
    from chatchat.server.research.plan import build_default_planner

    return build_default_planner(model_name)


def _build_sources() -> Dict[str, Any]:
    from chatchat.server.research.sources import build_default_sources

    return build_default_sources()


def _runtime(
    run_id: str,
    owner_id: str,
    *,
    planner: Any = None,
    sources: Optional[Dict[str, Any]] = None,
    model_name: Optional[str] = None,
    extractor: Any = None,
    unit_providers: Any = None,
    kb_names: Optional[list] = None,
    comparison_generator: Any = None,
    report_generator: Any = None,
) -> ResearchRuntime:
    return ResearchRuntime(
        _session_factory,
        owner_id,
        run_id,
        planner=planner or _build_planner(model_name),
        sources=sources if sources is not None else _build_sources(),
        model_name=model_name or "",
        extractor=extractor,
        unit_providers=unit_providers,
        kb_names=kb_names,
        comparison_generator=comparison_generator,
        report_generator=report_generator,
    )


# ---------------------------------------------------------------------------
# 计划生成 / 确认执行 / 恢复 / 取消
# ---------------------------------------------------------------------------


async def generate_research_plan(
    run_id: str,
    owner_id: str,
    *,
    planner: Any = None,
    sources: Optional[Dict[str, Any]] = None,
    model_name: Optional[str] = None,
) -> dict:
    """生成研究计划并在确认节点挂起；返回运行状态摘要。"""
    return await _runtime(
        run_id, owner_id, planner=planner, sources=sources, model_name=model_name
    ).generate_plan()


async def confirm_and_execute_research_run(
    run_id: str,
    owner_id: str,
    *,
    planner: Any = None,
    sources: Optional[Dict[str, Any]] = None,
    model_name: Optional[str] = None,
    extractor: Any = None,
    unit_providers: Any = None,
    kb_names: Optional[list] = None,
    comparison_generator: Any = None,
    report_generator: Any = None,
) -> dict:
    """确认计划（服务端状态转换）并继续执行到完成/失败/取消。

    - 非本人/不存在：``LookupError``（路由层 404）；
    - 状态不允许确认：``ResearchConflict``（路由层 409）；
    - 确认成功后创建并关联 Agent Run 行，再恢复研究图执行。
    """
    session = _session_factory()
    try:
        run = repo.get_owned_run(session, run_id, owner_id)
        if run is None:
            raise LookupError("研究运行不存在或无权访问")
        project = repo.get_owned_project(session, run.project_id, owner_id)
        run = repo.confirm_run(session, run_id, owner_id)
        project_name = project.name if project is not None else ""
        plan_json = run.plan_json if run is not None else ""
        existing_agent_run_id = run.agent_run_id if run is not None else None
    finally:
        session.close()

    # 关联现有 Agent Run（时间线/取消/Trace 复用，任务 017 §5.3）
    agent_run_id = existing_agent_run_id
    if not agent_run_id:
        session = _session_factory()
        try:
            ar = agent_run_repo.create_run(
                session,
                owner_id,
                f"论文研究：{project_name}"[:2000],
                conversation_id=None,
                max_steps=1,
                plan_text=plan_json,
            )
            agent_run_repo.start_run(session, ar.id, owner_id)
            agent_run_id = ar.id
        except Exception:
            logger.warning("创建研究关联 Agent Run 失败（忽略）", exc_info=True)
        finally:
            session.close()
        if agent_run_id:
            session = _session_factory()
            try:
                repo.link_agent_run(session, run_id, owner_id, agent_run_id)
            finally:
                session.close()

    try:
        return await _runtime(
            run_id,
            owner_id,
            planner=planner,
            sources=sources,
            model_name=model_name,
            extractor=extractor,
            unit_providers=unit_providers,
            kb_names=kb_names,
            comparison_generator=comparison_generator,
            report_generator=report_generator,
        ).execute()
    finally:
        _sync_agent_run_terminal(run_id, owner_id, agent_run_id)


async def resume_research_run(
    run_id: str,
    owner_id: str,
    *,
    planner: Any = None,
    sources: Optional[Dict[str, Any]] = None,
    model_name: Optional[str] = None,
    extractor: Any = None,
    unit_providers: Any = None,
    kb_names: Optional[list] = None,
    comparison_generator: Any = None,
    report_generator: Any = None,
) -> dict:
    """中断/恢复研究运行（含任务 018 的证据/报告复核门禁）。

    - ``awaiting_confirmation``：未确认则重新挂起（无副作用）；
    - ``running``：从 checkpoint 链尾继续（崩溃恢复）；
    - ``failed``：重试语义 ``failed -> running``，从已持久化阶段继续，
      并新建一条关联 Agent Run 行（旧的保持 failed）；
    - ``completed/cancelled``：幂等回报终态。
    """
    session = _session_factory()
    try:
        run = repo.get_owned_run(session, run_id, owner_id)
        if run is None:
            raise LookupError("研究运行不存在或无权访问")
        agent_run_id = run.agent_run_id
        is_retry = run.status == "failed"
        project = repo.get_owned_project(session, run.project_id, owner_id)
        project_name = project.name if project is not None else ""
        plan_json = run.plan_json
    finally:
        session.close()

    if is_retry:
        # 重试使用新的 Agent Run 关联（一次执行一条时间线）
        session = _session_factory()
        try:
            ar = agent_run_repo.create_run(
                session, owner_id, f"论文研究重试：{project_name}"[:2000],
                conversation_id=None, max_steps=1, plan_text=plan_json,
            )
            agent_run_repo.start_run(session, ar.id, owner_id)
            agent_run_id = ar.id
        except Exception:
            logger.warning("重试创建关联 Agent Run 失败（忽略）", exc_info=True)
        finally:
            session.close()
        if agent_run_id:
            session = _session_factory()
            try:
                repo.link_agent_run(session, run_id, owner_id, agent_run_id)
            finally:
                session.close()

    try:
        return await _runtime(
            run_id,
            owner_id,
            planner=planner,
            sources=sources,
            model_name=model_name,
            extractor=extractor,
            unit_providers=unit_providers,
            kb_names=kb_names,
            comparison_generator=comparison_generator,
            report_generator=report_generator,
        ).resume()
    finally:
        _sync_agent_run_terminal(run_id, owner_id, agent_run_id)


async def run_research_artifacts(
    run_id: str,
    owner_id: str,
    *,
    planner: Any = None,
    sources: Optional[Dict[str, Any]] = None,
    model_name: Optional[str] = None,
    extractor: Any = None,
    unit_providers: Any = None,
    kb_names: Optional[list] = None,
    comparison_generator: Any = None,
    report_generator: Any = None,
) -> dict:
    """用户筛选论文后，基于既有运行产出证据/比较/报告（任务 018 阶段 E）。

    - 非本人/不存在：``LookupError``（路由层 404）；
    - 状态不允许：``ResearchConflict``（路由层 409，仅 ``completed`` 可重开，
      ``failed`` 走 resume 重试，``cancelled`` 尊重用户意图不复活）；
    - 重开只把状态回到 ``running``、阶段回到 ``persist``，检索/去重/筛选/
      入库节点按 ``stage`` 跳过已完成工作，因此不会重复联网检索或重复入库；
    - 一次产物执行关联一条新的 Agent Run 时间线（旧的保持 completed）。
    """
    session = _session_factory()
    try:
        run = repo.get_owned_run(session, run_id, owner_id)
        if run is None:
            raise LookupError("研究运行不存在或无权访问")
        project = repo.get_owned_project(session, run.project_id, owner_id)
        project_name = project.name if project is not None else ""
        plan_json = run.plan_json
    finally:
        session.close()

    # completed -> running（证据闭环入口；仓储层负责状态机与拒绝理由）
    session = _session_factory()
    try:
        repo.reopen_run_for_artifacts(session, run_id, owner_id)
    finally:
        session.close()

    agent_run_id = None
    session = _session_factory()
    try:
        ar = agent_run_repo.create_run(
            session,
            owner_id,
            f"论文证据与报告：{project_name}"[:2000],
            conversation_id=None,
            max_steps=1,
            plan_text=plan_json,
        )
        agent_run_repo.start_run(session, ar.id, owner_id)
        agent_run_id = ar.id
    except Exception:
        logger.warning("产物执行创建关联 Agent Run 失败（忽略）", exc_info=True)
    finally:
        session.close()
    if agent_run_id:
        session = _session_factory()
        try:
            repo.link_agent_run(session, run_id, owner_id, agent_run_id)
        finally:
            session.close()

    try:
        return await _runtime(
            run_id,
            owner_id,
            planner=planner,
            sources=sources,
            model_name=model_name,
            extractor=extractor,
            unit_providers=unit_providers,
            kb_names=kb_names,
            comparison_generator=comparison_generator,
            report_generator=report_generator,
        ).execute()
    finally:
        _sync_agent_run_terminal(run_id, owner_id, agent_run_id)


def cancel_research_run(run_id: str, owner_id: str) -> Optional[dict]:
    """取消研究运行（含挂起等待确认的运行）；同步取消关联 Agent Run。

    非本人/不存在返回 None（路由层 404）。
    """
    session = _session_factory()
    try:
        run = repo.cancel_run(session, run_id, owner_id)
        if run is None:
            return None
        agent_run_id = run.agent_run_id
        summary = _run_summary(run)
    finally:
        session.close()
    if agent_run_id:
        session = _session_factory()
        try:
            agent_run_repo.cancel_run(session, agent_run_id, owner_id)
        finally:
            session.close()
    return summary


def _sync_agent_run_terminal(run_id: str, owner_id: str, agent_run_id: Optional[str]) -> None:
    """把研究运行终态同步到关联 Agent Run 行（幂等，失败仅日志）。"""
    if not agent_run_id:
        return
    session = _session_factory()
    try:
        run = repo.get_owned_run(session, run_id, owner_id)
        if run is None or run.status not in repo.RUN_TERMINAL_STATUSES:
            return
        if run.status == "completed":
            agent_run_repo.finalize_run(
                session, agent_run_id, owner_id, "completed",
                final_result="论文研究运行完成",
            )
        elif run.status == "cancelled":
            agent_run_repo.cancel_run(session, agent_run_id, owner_id)
        else:
            agent_run_repo.finalize_run(
                session, agent_run_id, owner_id, "failed",
                error=(run.error or "研究运行失败")[:1000],
            )
    except Exception:
        logger.warning("同步研究终态到 Agent Run 失败（忽略）", exc_info=True)
    finally:
        try:
            session.close()
        except Exception:
            pass


def _run_summary(run) -> dict:
    import json

    try:
        summary = json.loads(run.summary_json) if run.summary_json else {}
    except (json.JSONDecodeError, TypeError):
        summary = {}
    return {
        "run_id": run.id,
        "project_id": run.project_id,
        "status": run.status,
        "stage": run.stage,
        "agent_run_id": run.agent_run_id,
        "summary": summary,
        "error": run.error,
    }


__all__ = [
    "_session_factory",
    "_build_planner",
    "_build_sources",
    "generate_research_plan",
    "confirm_and_execute_research_run",
    "resume_research_run",
    "run_research_artifacts",
    "cancel_research_run",
]
