"""论文研究 LangGraph 子图（任务 017 阶段 C + 任务 018 阶段 E）。

真实多节点状态图（不是单节点包装或 API 长 if/else）::

    prepare_plan ──► wait_for_confirmation ──► retrieve_sources
        │(计划失败→END)    │(未确认→挂起；取消→END)   │(全部来源失败→END)
        └──────────────────┴──────────────────────────┴─► normalize_and_deduplicate
                                                          └─► screen_candidates
                                                              └─► persist_results
    ──► read_selected_papers ──► extract_evidence ──► wait_for_evidence_review
        │(没有纳入论文/无原文 → 直接收尾)        │(未复核 → 挂起)
        └─► analyze_coverage_and_conflicts ──► build_comparison ──► generate_report
            ──► wait_for_report_review ──► finalize_artifacts ──► END
                                              │(未复核 → 挂起)

任务 018 的后续节点保持独立阶段与人工确认点（证据复核、报告复核），复用同一条
研究运行、同一条 Agent Run/Step、同一 checkpoint 链与 Phoenix trace；每个阶段的
业务产物先落库再推进下一阶段。挂起复用任务 017 的确认状态机
（``running -> awaiting_confirmation``，由 ``stage`` 表明复核对象），恢复时按
``stage`` 顺序跳过已完成阶段，因此不会重复检索、重复调用模型或重复出版本。

关键语义（任务 017 §5.3）：

- **计划**：``prepare_plan`` 用注入的计划器生成并校验为结构化子问题与
  检索式；无效输出 → failed 可控失败。计划写入 ``research_run.plan_json``
  （恢复/重新进入时不重复调用模型）。
- **确认**：未确认时 ``wait_for_confirmation`` 让本轮图在门禁正常结束
  （checkpoint 保存完整状态；Python 3.10 上 langgraph 的 ``interrupt``
  对 async 节点不可用，见节点内注释）；确认是 **服务端状态转换**
  （``awaiting_confirmation -> running``，见仓储 ``confirm_run``），
  运行时随后以初始输入重新进入图（计划已存库不重调模型）。
  未确认的运行不会越过本节点，也就不会开始检索。
- **检索**：逐来源逐检索式调用来源适配器；单来源失败记录统计并继续，
  全部来源失败才 failed。每个来源步骤前重读数据库取消状态。
- **去重/筛选/持久化**：批内去重合并来源；确定性相关度打分（第一版
  不调模型，全部候选保持 pending_review 交用户标记）；入库幂等
  （DOI/ArXiv/URL/标题+年份查重 + ``(project_id, paper_id)`` 唯一），
  重试/恢复不重复创建论文或关系。
- 阶段与产物写入数据库（``research_run.stage/sources_json/summary_json``
  与 ``paper_record``/``project_paper``），不只保存在状态里；
  checkpoint 只保存编排位置（``run_id == thread_id``，复用任务 014
  的存储）。状态字段全部 JSON 可序列化。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, TypedDict

from langgraph.graph import END, StateGraph

from chatchat.server.db.repository import agent_run_repository as agent_run_repo
from chatchat.server.db.repository import research_repository as repo
from chatchat.server.research.papers import (
    PaperCandidate,
    dedup_candidates,
    paper_record_fields,
    standardize_candidate,
)
from chatchat.server.research.plan import ResearchPlan, parse_research_plan
from chatchat.server.research.sources import SourceError
from chatchat.utils import build_logger

logger = build_logger()

#: 进入持久化的候选论文上限（按相关度降序保留，保证有界）
MAX_PERSIST_CANDIDATES = 100
#: 每轮读取原文的纳入论文上限（有界，避免无限消耗）
MAX_FULLTEXT_PAPERS = 20
#: 每轮证据提取调用上限（论文 × 子问题，超出即停止并如实记录）
MAX_EVIDENCE_CALLS = 40

_TOKEN_RE = re.compile(r"[\w一-鿿]+", re.UNICODE)


def _evidence_service():
    """延迟导入：证据服务会拉起 PDF/知识库解析依赖，图构建时不需要。"""
    from chatchat.server.research import evidence_service

    return evidence_service


def _comparison_service():
    from chatchat.server.research import comparison_service

    return comparison_service


def _report_service():
    from chatchat.server.research import report_service

    return report_service


class ResearchGraphState(TypedDict, total=False):
    """研究子图状态（``run_id`` 即 LangGraph ``thread_id``）。"""

    run_id: str
    project_id: str
    owner_id: str
    agent_run_id: str
    status: str          # 与 research_run.status 同步
    stage: str           # 与 research_run.stage 同步
    plan: dict           # ResearchPlan.to_dict()
    source_stats: list   # [{source, query, hits, error}]
    candidates: list     # 标准化候选（PaperCandidate.to_dict()）
    deduped: list        # 批内去重合并后的候选
    screened: list       # 带相关度的候选
    persist_stats: dict  # 完成摘要
    # --- 任务 018 阶段 E：证据闭环 ---
    papers_read: int     # 本轮读取原文的纳入论文数
    read_stats: list     # 每篇原文获取结果（state/units/detail）
    skip_artifacts: bool # 没有可加工的材料 → 直接收尾（不假装产出）
    evidence_counts: dict
    coverage: dict       # 覆盖分析结果（纯计算）
    comparison_stats: dict
    report_id: str
    artifacts: dict      # 各阶段产物计数
    error: str


def _terminal_update(status: str, error: str = "") -> Dict[str, Any]:
    return {"status": status, "error": error}


def _fail_terminal(session: Any, run_id: str, owner_id: str, error: str) -> Dict[str, Any]:
    """收敛为 failed（可控失败）。"""
    msg = (error or "未知错误")[:1000]
    try:
        repo.finalize_run(session, run_id, owner_id, "failed", error=msg)
    except Exception:
        try:
            session.rollback()
        except Exception:
            pass
    return _terminal_update("failed", msg)


def _current_status(session: Any, run_id: str, owner_id: str) -> Optional[str]:
    return repo.run_status(session, run_id, owner_id)


def _is_terminal(state: ResearchGraphState) -> bool:
    return state.get("status") in repo.RUN_TERMINAL_STATUSES


def _sync_project_stage(session: Any, project_id: str, owner_id: str, stage: str) -> None:
    """项目展示字段 current_stage 同步（尽力而为）。"""
    try:
        from sqlalchemy import text as _text

        session.execute(
            _text(
                "UPDATE research_project SET current_stage = :s, "
                "update_time = CURRENT_TIMESTAMP WHERE id = :p AND owner_id = :o"
            ),
            {"s": stage, "p": project_id, "o": owner_id},
        )
        session.commit()
    except Exception:
        try:
            session.rollback()
        except Exception:
            pass


def _stage_of(session: Any, run_id: str, owner_id: str) -> str:
    """数据库里的权威阶段（恢复重放据此判断哪些阶段已完成）。"""
    run = repo.get_owned_run(session, run_id, owner_id)
    return (run.stage or "") if run is not None else ""


def _already_done(session: Any, run_id: str, owner_id: str, stage: str) -> bool:
    """该阶段是否已走完（严格大于：正在该阶段上的节点仍要执行）。"""
    return (
        repo.STAGE_ORDER.get(_stage_of(session, run_id, owner_id), 0)
        > repo.STAGE_ORDER.get(stage, 0)
    )


def _guard_running(session: Any, run_id: str, owner_id: str) -> Optional[Dict[str, Any]]:
    """每个阶段前重读数据库：已取消/终态则立即收敛，不再产生副作用。"""
    current = _current_status(session, run_id, owner_id)
    if current is None or current in repo.RUN_TERMINAL_STATUSES:
        return _terminal_update(current or "cancelled")
    return None


def _plan_sub_questions(plan: Dict[str, Any]) -> List[Dict[str, str]]:
    """计划里的子问题（id 规则与覆盖统计一致：缺省为 ``sq{序号}``）。"""
    out: List[Dict[str, str]] = []
    for index, item in enumerate(plan.get("sub_questions") or []):
        if not isinstance(item, dict):
            continue
        question = str(item.get("question") or "").strip()
        if not question:
            continue
        out.append(
            {
                "id": str(item.get("id") or f"sq{index + 1}"),
                "question": question,
            }
        )
    return out


def _relevance(candidate: Dict[str, Any], plan: Dict[str, Any]) -> float:
    """确定性相关度：候选标题/摘要与子问题/检索式的词元重合率。

    第一版不调模型做语义筛选（避免测试依赖与不可控输出）；全部候选
    保持 pending_review，纳入/排除由用户经 API 决定。
    """
    text_tokens = set(
        _TOKEN_RE.findall(
            f"{candidate.get('title') or ''} {candidate.get('abstract') or ''}".lower()
        )
    )
    if not text_tokens:
        return 0.0
    plan_tokens: set = set()
    for sq in plan.get("sub_questions") or []:
        plan_tokens.update(_TOKEN_RE.findall(str(sq.get("question") or "").lower()))
        for q in sq.get("queries") or []:
            plan_tokens.update(_TOKEN_RE.findall(str(q).lower()))
    if not plan_tokens:
        return 0.0
    overlap = len(text_tokens & plan_tokens) / len(plan_tokens)
    return round(min(1.0, overlap), 4)


def build_research_graph(rt: Any, checkpointer: Any):
    """构建并编译研究子图。

    ``rt`` 为 ``ResearchRuntime`` 实例（节点通过闭包使用其
    ``session_factory`` / ``planner`` / ``sources``）；``checkpointer``
    为持久化 checkpointer（生产复用任务 014 的 ``AsyncSqliteSaver``）。
    """

    # -- 节点 ---------------------------------------------------------------

    async def prepare_plan_node(state: ResearchGraphState) -> Dict[str, Any]:
        """生成/加载研究计划；无效计划 → failed 可控失败。"""
        session = rt.session_factory()
        run_id, owner, project_id = state["run_id"], state["owner_id"], state["project_id"]
        try:
            run = repo.get_owned_run(session, run_id, owner)
            if run is None:
                return _terminal_update("cancelled")
            # 恢复/重入：计划已生成（或用户已编辑）→ 不重复调用模型
            if (run.plan_json or "").strip():
                try:
                    plan = parse_research_plan(
                        json.loads(run.plan_json), set(rt.sources.keys())
                    )
                except Exception as exc:
                    return _fail_terminal(session, run_id, owner, f"已保存的计划不可用：{exc}")
                return {"plan": plan.to_dict(), "status": run.status or "awaiting_confirmation"}

            project = repo.get_owned_project(session, project_id, owner)
            if project is None:
                return _fail_terminal(session, run_id, owner, "研究项目不可用")
            try:
                plan = rt.planner.generate(
                    project.research_question, project.scope or "", set(rt.sources.keys())
                )
            except Exception as exc:
                return _fail_terminal(
                    session, run_id, owner, f"研究计划生成失败（输出无效）：{exc}"
                )
            updated = repo.set_plan(session, run_id, owner, plan.to_json())
            if updated is None:
                current = _current_status(session, run_id, owner) or "cancelled"
                return _terminal_update(current)
            _sync_project_stage(session, project_id, owner, "plan")
            return {"plan": plan.to_dict(), "status": "awaiting_confirmation", "stage": "plan"}
        finally:
            session.close()

    async def wait_for_confirmation_node(state: ResearchGraphState) -> Dict[str, Any]:
        """确认门禁：未确认时本轮图在门禁正常结束；确认是服务端状态转换。

        说明：本机 Python 3.10 上 langgraph 不为 async 节点注入 config
        上下文（``interrupt()`` 依赖它，仅 Python>=3.11 可用），因此挂起
        用"图正常走完 + 数据库 ``awaiting_confirmation``"表达：
        checkpoint 保存完整状态，确认/恢复时运行时以初始输入重新进入图
        （计划已存库不重调模型、入库幂等），语义与 interrupt 挂起等价。
        """
        session = rt.session_factory()
        run_id, owner = state["run_id"], state["owner_id"]
        try:
            current = _current_status(session, run_id, owner)
            if current is None:
                return _terminal_update("cancelled")
            if current in repo.RUN_TERMINAL_STATUSES:
                return _terminal_update(current)
            if current == "running":
                # 用户已通过 API 确认（awaiting_confirmation -> running）。
                # 确认前用户可能编辑过计划（只写库），这里以数据库中的
                # 最新计划覆盖状态通道，保证检索用的是编辑后的检索式。
                run = repo.get_owned_run(session, run_id, owner)
                if run is not None and (run.plan_json or "").strip():
                    try:
                        plan = parse_research_plan(
                            json.loads(run.plan_json), set(rt.sources.keys())
                        )
                    except Exception as exc:
                        return _fail_terminal(
                            session, run_id, owner, f"确认的计划不可用：{exc}"
                        )
                    return {"status": "running", "stage": "plan",
                            "plan": plan.to_dict()}
                return {"status": "running", "stage": "plan"}
            # 仍在等待确认：本轮图在门禁结束（conditional edge -> END），
            # 数据库保持 awaiting_confirmation，绝不开始检索
            return {"status": current or "awaiting_confirmation", "stage": "plan"}
        finally:
            session.close()

    async def retrieve_sources_node(state: ResearchGraphState) -> Dict[str, Any]:
        """逐来源逐检索式检索；单来源失败隔离，全部失败才 failed。"""
        session = rt.session_factory()
        run_id, owner = state["run_id"], state["owner_id"]
        try:
            current = _current_status(session, run_id, owner)
            if current in repo.RUN_TERMINAL_STATUSES or current is None:
                return _terminal_update(current or "cancelled")
            if _already_done(session, run_id, owner, "retrieval"):
                # 恢复/产物续跑：检索已完成，绝不重复调用外部来源
                run = repo.get_owned_run(session, run_id, owner)
                stats = json.loads(run.sources_json) if run and run.sources_json else []
                return {"stage": "retrieval", "source_stats": stats, "candidates": []}
            repo.set_stage(session, run_id, owner, "retrieval")
            _sync_project_stage(session, state["project_id"], owner, "retrieval")

            plan = state.get("plan") or {}
            queries: List[str] = []
            for sq in plan.get("sub_questions") or []:
                for q in sq.get("queries") or []:
                    if q and q not in queries:
                        queries.append(q)
            source_names = [s for s in (plan.get("sources") or []) if s in rt.sources]
            if not queries or not source_names:
                return _fail_terminal(session, run_id, owner, "计划没有有效的检索式或来源")

            stats: List[Dict[str, Any]] = []
            candidates: List[Dict[str, Any]] = []
            any_success = False
            for name in source_names:
                source = rt.sources[name]
                source_ok = False
                for query in queries:
                    if _current_status(session, run_id, owner) in repo.RUN_TERMINAL_STATUSES:
                        # 检索过程中被取消：停止，不再调用来源
                        current = _current_status(session, run_id, owner)
                        return _terminal_update(current or "cancelled")
                    try:
                        found = await source.search(query)
                        for cand in found:
                            candidates.append(
                                standardize_candidate(cand).to_dict()
                            )
                        stats.append(
                            {"source": name, "query": query,
                             "hits": len(found), "error": None}
                        )
                        source_ok = True
                    except SourceError as exc:
                        stats.append(
                            {"source": name, "query": query, "hits": 0,
                             "error": str(exc)[:300]}
                        )
                    except Exception as exc:
                        logger.warning("研究来源异常：%s", name, exc_info=True)
                        stats.append(
                            {"source": name, "query": query, "hits": 0,
                             "error": str(exc)[:300]}
                        )
                if source_ok:
                    any_success = True

            repo.set_source_stats(session, run_id, owner, stats)
            if not any_success:
                # 所有来源均失败（有失败记录且无任何成功检索）→ 本阶段失败
                errors = [s["error"] for s in stats if s.get("error")]
                return _fail_terminal(
                    session, run_id, owner,
                    f"所有来源均失败：{errors[:3]}",
                )
            return {"candidates": candidates, "source_stats": stats, "stage": "retrieval"}
        finally:
            session.close()

    async def normalize_and_dedup_node(state: ResearchGraphState) -> Dict[str, Any]:
        """批内标准化去重：稳定标识优先，合并来源不丢失可追溯性。"""
        session = rt.session_factory()
        run_id, owner = state["run_id"], state["owner_id"]
        try:
            current = _current_status(session, run_id, owner)
            if current in repo.RUN_TERMINAL_STATUSES or current is None:
                return _terminal_update(current or "cancelled")
            if _already_done(session, run_id, owner, "dedup"):
                return {"stage": "dedup"}
            raw = [PaperCandidate.from_dict(c) for c in state.get("candidates") or []]
            deduped, merged_count = dedup_candidates(raw)
            repo.set_stage(session, run_id, owner, "dedup")
            _sync_project_stage(session, state["project_id"], owner, "dedup")
            return {
                "deduped": [c.to_dict() for c in deduped],
                "stage": "dedup",
                "persist_stats": {"merged_count": merged_count},
            }
        finally:
            session.close()

    async def screen_candidates_node(state: ResearchGraphState) -> Dict[str, Any]:
        """确定性相关度打分并限量；纳入/排除由用户后续标记。"""
        session = rt.session_factory()
        run_id, owner = state["run_id"], state["owner_id"]
        try:
            current = _current_status(session, run_id, owner)
            if current in repo.RUN_TERMINAL_STATUSES or current is None:
                return _terminal_update(current or "cancelled")
            if _already_done(session, run_id, owner, "screening"):
                return {"stage": "screening"}
            plan = state.get("plan") or {}
            scored = []
            for cand in state.get("deduped") or []:
                c = dict(cand)
                c["relevance"] = _relevance(c, plan)
                scored.append(c)
            scored.sort(key=lambda c: c["relevance"], reverse=True)
            kept = scored[:MAX_PERSIST_CANDIDATES]
            repo.set_stage(session, run_id, owner, "screening")
            _sync_project_stage(session, state["project_id"], owner, "screening")
            stats = dict(state.get("persist_stats") or {})
            stats["screened_count"] = len(kept)
            stats["dropped_by_cap"] = max(0, len(scored) - len(kept))
            return {"screened": kept, "stage": "screening", "persist_stats": stats}
        finally:
            session.close()

    async def persist_results_node(state: ResearchGraphState) -> Dict[str, Any]:
        """幂等入库规范论文与项目关系；统计并入 summary（终态由收尾节点收敛）。"""
        session = rt.session_factory()
        run_id, owner, project_id = state["run_id"], state["owner_id"], state["project_id"]
        try:
            current = _current_status(session, run_id, owner)
            if current in repo.RUN_TERMINAL_STATUSES or current is None:
                # 已取消/终态：不持久化（取消后不重复创建同一批论文）
                return _terminal_update(current or "cancelled")
            if _already_done(session, run_id, owner, "persist") or (
                not (state.get("screened") or [])
                and repo.list_project_papers(session, project_id, owner)
            ):
                # 恢复/产物续跑：论文已入库，空候选集不得覆盖已落库的统计
                return {"stage": "persist"}
            repo.set_stage(session, run_id, owner, "persist")
            _sync_project_stage(session, project_id, owner, "persist")

            new_papers = 0
            linked = 0
            for cand in state.get("screened") or []:
                record, created = repo.upsert_paper(
                    session, paper_record_fields(PaperCandidate.from_dict(cand))
                )
                if created:
                    new_papers += 1
                repo.link_project_paper(
                    session, project_id, owner, record.id,
                    source=cand.get("source") or "",
                    relevance=cand.get("relevance"),
                    run_id=run_id,
                )
                linked += 1
            session.commit()

            stats = dict(state.get("persist_stats") or {})
            source_hits: Dict[str, int] = {}
            source_errors = 0
            for s in state.get("source_stats") or []:
                source_hits[s.get("source") or "?"] = (
                    source_hits.get(s.get("source") or "?", 0) + int(s.get("hits") or 0)
                )
                if s.get("error"):
                    source_errors += 1
            stats.update(
                {
                    "source_hits": source_hits,
                    "source_errors": source_errors,
                    "deduped_count": len(state.get("deduped") or []),
                    "candidate_count": linked,
                    "new_papers": new_papers,
                }
            )
            # 阶段产物先落库。任务 018 扩展后图还要走证据/比较/报告阶段，
            # 因此这里不再收敛终态（终态由 finalize_artifacts 负责）。
            repo.merge_run_summary(session, run_id, owner, stats)
            agent_run_id = state.get("agent_run_id") or _agent_run_id(session, run_id, owner)
            _record_phase_step(
                session,
                agent_run_id,
                owner,
                goal="阶段：候选论文入库",
                tool_name="research.persist_results",
                output=f"新增论文 {new_papers} 篇，挂接 {linked} 篇",
            )
            return {
                "persist_stats": stats,
                "status": "running",
                "stage": "persist",
                "agent_run_id": agent_run_id or "",
            }
        except Exception as exc:
            try:
                session.rollback()
            except Exception:
                pass
            return _fail_terminal(session, run_id, owner, f"结果持久化失败：{exc}")
        finally:
            session.close()

    def _agent_run_id(session: Any, run_id: str, owner_id: str) -> Optional[str]:
        run = repo.get_owned_run(session, run_id, owner_id)
        return run.agent_run_id if run is not None else None

    def _record_phase_step(
        session: Any,
        agent_run_id: Optional[str],
        owner_id: str,
        *,
        goal: str,
        tool_name: str,
        output: str,
    ) -> None:
        """把研究阶段记入既有 Agent Run 的步骤时间线（尽力而为）。

        复用任务 014 的 Agent Run/Step，不建第二套执行记录；Agent Run 已
        终态或写入失败时静默跳过，业务产物本身不受影响。
        """
        if not agent_run_id:
            return
        try:
            steps = agent_run_repo.get_owned_steps(session, agent_run_id, owner_id)
            seq = len(steps or []) + 1
            step = agent_run_repo.add_step(
                session,
                agent_run_id,
                owner_id,
                seq,
                stype="tool_call",
                goal=goal[:500],
                tool_name=tool_name[:128],
            )
            if step is not None:
                agent_run_repo.update_step(
                    session,
                    step.id,
                    owner_id,
                    status="completed",
                    output_summary=(output or "")[:2000],
                    start_time_now=True,
                    end_time_now=True,
                )
        except Exception:
            logger.debug("研究阶段步骤写入 Agent Run 失败（忽略）", exc_info=True)

    def _run_agent_run_id(session: Any, run_id: str, owner_id: str) -> Optional[str]:
        return _agent_run_id(session, run_id, owner_id)

    # -- 任务 018 阶段 E：证据闭环后续节点 ---------------------------------

    async def read_selected_papers_node(state: ResearchGraphState) -> Dict[str, Any]:
        """读取纳入论文的原文单元（幂等、逐篇失败隔离、如实降级）。"""
        session = rt.session_factory()
        run_id, owner, project_id = state["run_id"], state["owner_id"], state["project_id"]
        try:
            blocked = _guard_running(session, run_id, owner)
            if blocked:
                return blocked
            if _already_done(session, run_id, owner, "fulltext"):
                # 恢复重放：原文已入库，不重复解析
                return {"stage": "fulltext", "skip_artifacts": bool(state.get("skip_artifacts"))}
            repo.set_stage(session, run_id, owner, "fulltext")
            _sync_project_stage(session, project_id, owner, "fulltext")

            included = repo.list_project_papers(
                session, project_id, owner, screening_status="included"
            )
            if not included:
                # 用户尚未纳入任何论文：不猜、不自动纳入，直接收尾并如实说明
                repo.merge_run_summary(
                    session, run_id, owner,
                    {"papers_read": 0, "artifact_note": "没有已纳入的论文，未生成证据/比较/报告"},
                )
                _record_phase_step(
                    session, _run_agent_run_id(session, run_id, owner), owner,
                    goal="阶段：读取纳入论文原文",
                    tool_name="research.read_selected_papers",
                    output="没有已纳入的论文",
                )
                return {"stage": "fulltext", "papers_read": 0, "skip_artifacts": True}

            read_stats: List[Dict[str, Any]] = []
            for pp, paper in included[:MAX_FULLTEXT_PAPERS]:
                if _guard_running(session, run_id, owner):
                    blocked = _guard_running(session, run_id, owner)
                    return blocked or _terminal_update("cancelled")
                try:
                    result = _evidence_service().ingest_paper_fulltext(
                        session,
                        project_id=project_id,
                        owner_id=owner,
                        paper_id=paper.id,
                        run_id=run_id,
                        providers=rt.unit_providers,
                        kb_names=rt.kb_names,
                    )
                    read_stats.append(
                        {
                            "paper_id": paper.id,
                            "state": result["state"],
                            "units": result["unit_count"],
                            "detail": (result.get("detail") or "")[:200],
                        }
                    )
                except Exception as exc:
                    read_stats.append(
                        {
                            "paper_id": paper.id,
                            "state": "none",
                            "units": 0,
                            "detail": str(exc)[:200],
                        }
                    )
            states: Dict[str, int] = {}
            for item in read_stats:
                states[item["state"]] = states.get(item["state"], 0) + 1
            repo.merge_run_summary(
                session, run_id, owner,
                {"papers_read": len(read_stats), "fulltext_states": states},
            )
            _record_phase_step(
                session, _run_agent_run_id(session, run_id, owner), owner,
                goal="阶段：读取纳入论文原文",
                tool_name="research.read_selected_papers",
                output=f"读取 {len(read_stats)} 篇：{states}",
            )
            return {
                "stage": "fulltext",
                "papers_read": len(read_stats),
                "read_stats": read_stats,
            }
        finally:
            session.close()

    async def extract_evidence_node(state: ResearchGraphState) -> Dict[str, Any]:
        """按子问题 × 论文提取证据（原文逐字核对、幂等、不覆盖已审核）。"""
        session = rt.session_factory()
        run_id, owner, project_id = state["run_id"], state["owner_id"], state["project_id"]
        try:
            blocked = _guard_running(session, run_id, owner)
            if blocked:
                return blocked
            if _already_done(session, run_id, owner, "evidence"):
                return {"stage": "evidence"}
            repo.set_stage(session, run_id, owner, "evidence")
            _sync_project_stage(session, project_id, owner, "evidence")

            sub_questions = _plan_sub_questions(state.get("plan") or {})
            papers = [
                (pp, paper)
                for pp, paper in repo.list_project_papers(
                    session, project_id, owner, screening_status="included"
                )
                if (pp.unit_count or 0) > 0
            ]
            if not sub_questions or not papers:
                repo.merge_run_summary(
                    session, run_id, owner,
                    {"evidence_note": "没有带原文单元的纳入论文或计划没有子问题，未提取证据"},
                )
                return {"stage": "evidence", "skip_artifacts": True}

            project = repo.get_owned_project(session, project_id, owner)
            question = project.research_question if project is not None else ""
            counts = {"created": 0, "updated": 0, "unchanged": 0, "kept_reviewed": 0}
            rejected: List[str] = []
            calls = 0
            for pp, paper in papers:
                for sq in sub_questions:
                    if calls >= MAX_EVIDENCE_CALLS:
                        rejected.append(
                            f"证据提取达到上限 {MAX_EVIDENCE_CALLS} 次，剩余组合未执行"
                        )
                        break
                    if _guard_running(session, run_id, owner):
                        blocked = _guard_running(session, run_id, owner)
                        return blocked or _terminal_update("cancelled")
                    calls += 1
                    try:
                        result = _evidence_service().extract_paper_evidence(
                            session,
                            project_id=project_id,
                            owner_id=owner,
                            paper_id=paper.id,
                            sub_question_id=sq["id"],
                            question=question,
                            sub_question=sq["question"],
                            extractor=rt.extractor,
                            model_name=rt.model_name,
                            run_id=run_id,
                            providers=rt.unit_providers,
                        )
                    except Exception as exc:
                        rejected.append(f"{paper.id}/{sq['id']}：{str(exc)[:200]}")
                        continue
                    for key in counts:
                        counts[key] += int(result.get(key) or 0)
                    rejected.extend(str(x)[:200] for x in (result.get("rejected") or []))
                else:
                    continue
                break

            repo.merge_run_summary(
                session, run_id, owner,
                {"evidence_calls": calls, "evidence_counts": counts,
                 "evidence_rejected": rejected[:10]},
            )
            _record_phase_step(
                session, _run_agent_run_id(session, run_id, owner), owner,
                goal="阶段：提取并核对证据",
                tool_name="research.extract_evidence",
                output=f"调用 {calls} 次：{counts}，拒绝 {len(rejected)} 条",
            )
            total = sum(counts.values())
            if total == 0:
                # 没有任何可核对的证据：不假装成功，直接收尾并说明原因
                return {"stage": "evidence", "skip_artifacts": True}
            return {"stage": "evidence", "evidence_counts": counts}
        finally:
            session.close()

    async def wait_for_evidence_review_node(state: ResearchGraphState) -> Dict[str, Any]:
        """证据复核门禁：用户确认/修改/驳回证据后才继续。"""
        session = rt.session_factory()
        run_id, owner, project_id = state["run_id"], state["owner_id"], state["project_id"]
        try:
            blocked = _guard_running(session, run_id, owner)
            if blocked:
                return blocked
            if _stage_of(session, run_id, owner) == "evidence_review" or _already_done(
                session, run_id, owner, "evidence_review"
            ):
                # 用户已复核（confirm_run 已把状态转回 running），或崩溃发生在
                # 复核之后的阶段：重放时不再要求用户重复复核
                return {"status": "running", "stage": _stage_of(session, run_id, owner)}
            repo.set_stage(session, run_id, owner, "evidence_review")
            _sync_project_stage(session, project_id, owner, "evidence_review")
            if not repo.pause_run_for_review(session, run_id, owner):
                current = _current_status(session, run_id, owner) or "cancelled"
                return _terminal_update(current)
            _record_phase_step(
                session, _run_agent_run_id(session, run_id, owner), owner,
                goal="阶段：等待证据复核",
                tool_name="research.wait_for_evidence_review",
                output="已挂起，等待用户复核证据",
            )
            return {"status": "awaiting_confirmation", "stage": "evidence_review"}
        finally:
            session.close()

    async def analyze_coverage_node(state: ResearchGraphState) -> Dict[str, Any]:
        """覆盖/缺口分析（纯计算，可重现），结果先落库再推进。"""
        session = rt.session_factory()
        run_id, owner, project_id = state["run_id"], state["owner_id"], state["project_id"]
        try:
            blocked = _guard_running(session, run_id, owner)
            if blocked:
                return blocked
            if _already_done(session, run_id, owner, "coverage"):
                # 恢复重放：覆盖结论已落库（纯计算，证据未变则结果相同）
                return {"stage": _stage_of(session, run_id, owner)}
            repo.set_stage(session, run_id, owner, "coverage")
            _sync_project_stage(session, project_id, owner, "coverage")
            coverage = _comparison_service().project_coverage(
                session, project_id=project_id, owner_id=owner
            )
            repo.merge_run_summary(
                session, run_id, owner,
                {
                    "coverage_totals": coverage.get("totals", {}),
                    "missing_dimensions": [
                        d.get("label") or d.get("id")
                        for d in coverage.get("missing_dimensions") or []
                    ],
                },
            )
            _record_phase_step(
                session, _run_agent_run_id(session, run_id, owner), owner,
                goal="阶段：证据覆盖与冲突分析",
                tool_name="research.analyze_coverage_and_conflicts",
                output=f"覆盖统计：{coverage.get('totals', {})}",
            )
            return {"coverage": coverage, "stage": "coverage"}
        finally:
            session.close()

    async def build_comparison_node(state: ResearchGraphState) -> Dict[str, Any]:
        """按维度生成比较单元与冲突（两侧都必须引用真实证据）。"""
        session = rt.session_factory()
        run_id, owner, project_id = state["run_id"], state["owner_id"], state["project_id"]
        try:
            blocked = _guard_running(session, run_id, owner)
            if blocked:
                return blocked
            if _already_done(session, run_id, owner, "comparison"):
                return {"stage": "comparison"}
            repo.set_stage(session, run_id, owner, "comparison")
            _sync_project_stage(session, project_id, owner, "comparison")
            result = _comparison_service().generate_project_comparisons(
                session,
                project_id=project_id,
                owner_id=owner,
                run_id=run_id,
                generator=rt.comparison_generator,
                model_name=rt.model_name,
            )
            repo.merge_run_summary(
                session, run_id, owner,
                {
                    "comparisons_written": result.get("written", 0),
                    "comparisons_skipped": result.get("skipped", 0),
                    "conflicts_recorded": result.get("conflicts", 0),
                    "comparison_rejected": [str(x)[:200] for x in (result.get("rejected") or [])][:10],
                },
            )
            _record_phase_step(
                session, _run_agent_run_id(session, run_id, owner), owner,
                goal="阶段：生成比较与冲突",
                tool_name="research.build_comparison",
                output=(
                    f"写入 {result.get('written', 0)} 条比较，"
                    f"新增 {result.get('conflicts', 0)} 条冲突"
                ),
            )
            return {"stage": "comparison", "comparison_stats": result}
        except Exception as exc:
            return _fail_terminal(session, run_id, owner, f"比较生成失败：{exc}")
        finally:
            session.close()

    async def generate_report_node(state: ResearchGraphState) -> Dict[str, Any]:
        """生成版本化研究报告（引用可反查，无证据结论标注为推测）。"""
        session = rt.session_factory()
        run_id, owner, project_id = state["run_id"], state["owner_id"], state["project_id"]
        try:
            blocked = _guard_running(session, run_id, owner)
            if blocked:
                return blocked
            if _already_done(session, run_id, owner, "report"):
                # 恢复重放：不重复出版本（报告版本只追加，重放会制造噪声版本）
                return {"stage": "report"}
            repo.set_stage(session, run_id, owner, "report")
            _sync_project_stage(session, project_id, owner, "report")
            report = _report_service().generate_project_report(
                session,
                project_id=project_id,
                owner_id=owner,
                run_id=run_id,
                generator=rt.report_generator,
                model_name=rt.model_name,
            )
            repo.merge_run_summary(
                session, run_id, owner,
                {
                    "report_id": report.get("report_id"),
                    "report_version": report.get("version"),
                    "report_citations": len(report.get("citations") or []),
                    # 内容未变时不出版本（重放/重复执行不产生噪声版本）
                    "report_unchanged": bool(report.get("unchanged")),
                    "report_warnings": (report.get("warnings") or [])[:10],
                },
            )
            _record_phase_step(
                session, _run_agent_run_id(session, run_id, owner), owner,
                goal="阶段：生成研究报告",
                tool_name="research.generate_report",
                output=f"报告 v{report.get('version')}，引用 {len(report.get('citations') or [])} 条",
            )
            return {"stage": "report", "report_id": report.get("report_id")}
        except Exception as exc:
            return _fail_terminal(session, run_id, owner, f"报告生成失败：{exc}")
        finally:
            session.close()

    async def wait_for_report_review_node(state: ResearchGraphState) -> Dict[str, Any]:
        """报告复核门禁：用户审阅/编辑报告后才收敛终态。"""
        session = rt.session_factory()
        run_id, owner, project_id = state["run_id"], state["owner_id"], state["project_id"]
        try:
            blocked = _guard_running(session, run_id, owner)
            if blocked:
                return blocked
            if _stage_of(session, run_id, owner) == "report_review" or _already_done(
                session, run_id, owner, "report_review"
            ):
                # 用户已复核报告，或崩溃发生在收尾阶段：重放不再挂起
                return {"status": "running", "stage": _stage_of(session, run_id, owner)}
            repo.set_stage(session, run_id, owner, "report_review")
            _sync_project_stage(session, project_id, owner, "report_review")
            if not repo.pause_run_for_review(session, run_id, owner):
                current = _current_status(session, run_id, owner) or "cancelled"
                return _terminal_update(current)
            _record_phase_step(
                session, _run_agent_run_id(session, run_id, owner), owner,
                goal="阶段：等待报告复核",
                tool_name="research.wait_for_report_review",
                output="已挂起，等待用户复核报告",
            )
            return {"status": "awaiting_confirmation", "stage": "report_review"}
        finally:
            session.close()

    async def finalize_artifacts_node(state: ResearchGraphState) -> Dict[str, Any]:
        """汇总各阶段产物并收敛终态（研究运行 + 关联 Agent Run）。"""
        session = rt.session_factory()
        run_id, owner, project_id = state["run_id"], state["owner_id"], state["project_id"]
        try:
            current = _current_status(session, run_id, owner)
            if current is None or current in repo.RUN_TERMINAL_STATUSES:
                return _terminal_update(current or "cancelled")
            artifacts = {
                "papers": len(repo.list_project_papers(session, project_id, owner)),
                "evidence": len(repo.list_evidence(session, project_id, owner)),
                "comparisons": len(repo.list_comparison_units(session, project_id, owner)),
                "conflicts": len(repo.list_conflicts(session, project_id, owner)),
                "reports": len(repo.latest_project_reports(session, project_id, owner)),
            }
            repo.merge_run_summary(session, run_id, owner, {"artifacts": artifacts})
            finalized = repo.finalize_run(
                session, run_id, owner, "completed", stage="done"
            )
            agent_run_id = state.get("agent_run_id") or _agent_run_id(session, run_id, owner)
            if agent_run_id:
                try:
                    agent_run_repo.finalize_run(
                        session, agent_run_id, owner, "completed",
                        final_result=(
                            f"论文研究运行完成：证据 {artifacts['evidence']} 条，"
                            f"报告 {artifacts['reports']} 份"
                        ),
                    )
                except Exception:
                    pass
            _sync_project_stage(session, project_id, owner, "done")
            status = finalized.status if finalized is not None else "completed"
            return {"status": status, "stage": "done", "artifacts": artifacts}
        except Exception as exc:
            try:
                session.rollback()
            except Exception:
                pass
            return _fail_terminal(session, run_id, owner, f"产物收尾失败：{exc}")
        finally:
            session.close()

    # -- 图结构 -------------------------------------------------------------

    graph = StateGraph(ResearchGraphState)
    graph.add_node("prepare_plan", prepare_plan_node)
    graph.add_node("wait_for_confirmation", wait_for_confirmation_node)
    graph.add_node("retrieve_sources", retrieve_sources_node)
    graph.add_node("normalize_and_deduplicate", normalize_and_dedup_node)
    graph.add_node("screen_candidates", screen_candidates_node)
    graph.add_node("persist_results", persist_results_node)
    graph.add_node("read_selected_papers", read_selected_papers_node)
    graph.add_node("extract_evidence", extract_evidence_node)
    graph.add_node("wait_for_evidence_review", wait_for_evidence_review_node)
    graph.add_node("analyze_coverage_and_conflicts", analyze_coverage_node)
    graph.add_node("build_comparison", build_comparison_node)
    graph.add_node("generate_report", generate_report_node)
    graph.add_node("wait_for_report_review", wait_for_report_review_node)
    graph.add_node("finalize_artifacts", finalize_artifacts_node)
    graph.set_entry_point("prepare_plan")

    def _to_end_or(node: str):
        # awaiting_confirmation 也走 END：图在确认门禁正常结束，
        # 确认/恢复由运行时重新进入（见 runtime._resume）
        def route(s: ResearchGraphState) -> str:
            st = s.get("status")
            if st in repo.RUN_TERMINAL_STATUSES or st == "awaiting_confirmation":
                return END
            return node

        return route

    def _to_finalize_or(node: str):
        """挂起/终态 → END；没有可加工材料 → 直接收尾；否则进入下一节点。"""

        def route(s: ResearchGraphState) -> str:
            st = s.get("status")
            if st in repo.RUN_TERMINAL_STATUSES or st == "awaiting_confirmation":
                return END
            if s.get("skip_artifacts"):
                return "finalize_artifacts"
            return node

        return route

    graph.add_conditional_edges("prepare_plan", _to_end_or("wait_for_confirmation"),
                                {END: END, "wait_for_confirmation": "wait_for_confirmation"})
    graph.add_conditional_edges("wait_for_confirmation", _to_end_or("retrieve_sources"),
                                {END: END, "retrieve_sources": "retrieve_sources"})
    graph.add_conditional_edges("retrieve_sources", _to_end_or("normalize_and_deduplicate"),
                                {END: END, "normalize_and_deduplicate": "normalize_and_deduplicate"})
    graph.add_edge("normalize_and_deduplicate", "screen_candidates")
    graph.add_edge("screen_candidates", "persist_results")
    graph.add_edge("persist_results", "read_selected_papers")

    def _routes(node: str) -> Dict[str, str]:
        return {
            END: END,
            node: node,
            "finalize_artifacts": "finalize_artifacts",
        }

    graph.add_conditional_edges(
        "read_selected_papers", _to_finalize_or("extract_evidence"),
        _routes("extract_evidence"),
    )
    graph.add_conditional_edges(
        "extract_evidence", _to_finalize_or("wait_for_evidence_review"),
        _routes("wait_for_evidence_review"),
    )
    graph.add_conditional_edges(
        "wait_for_evidence_review", _to_finalize_or("analyze_coverage_and_conflicts"),
        _routes("analyze_coverage_and_conflicts"),
    )
    graph.add_conditional_edges(
        "analyze_coverage_and_conflicts", _to_finalize_or("build_comparison"),
        _routes("build_comparison"),
    )
    graph.add_conditional_edges(
        "build_comparison", _to_finalize_or("generate_report"),
        _routes("generate_report"),
    )
    graph.add_conditional_edges(
        "generate_report", _to_finalize_or("wait_for_report_review"),
        _routes("wait_for_report_review"),
    )
    graph.add_conditional_edges(
        "wait_for_report_review", _to_finalize_or("finalize_artifacts"),
        _routes("finalize_artifacts"),
    )
    graph.add_edge("finalize_artifacts", END)

    return graph.compile(checkpointer=checkpointer)


__all__ = [
    "MAX_EVIDENCE_CALLS",
    "MAX_FULLTEXT_PAPERS",
    "MAX_PERSIST_CANDIDATES",
    "ResearchGraphState",
    "build_research_graph",
]
