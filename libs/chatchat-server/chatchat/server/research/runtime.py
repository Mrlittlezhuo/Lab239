"""研究运行时（任务 017 阶段 C）。

复用任务 014 的 checkpoint 基础设施（``AsyncSqliteSaver`` 全局存储，
``run_id == thread_id``），不建第二套执行器或任务队列：

- ``generate_plan()``：新运行进入图，``prepare_plan`` 生成计划后
  ``wait_for_confirmation`` 在门禁让本轮图正常结束（checkpoint 保存
  完整状态），返回时运行处于 ``awaiting_confirmation``；
- ``execute()``：调用方已在数据库完成确认状态转换（running）后，
  以初始输入重新进入图——计划已存库不重调模型，确认节点按数据库
  状态放行进入检索（Python 3.10 上 langgraph ``interrupt`` 对 async
  节点不可用，挂起/恢复用"图结束 + 重新进入"表达，语义等价）；
- ``resume()``：进程重启/中断后按 checkpoint 链尾继续（挂起在确认
  门禁且未确认：无副作用回报；崩溃于检索等节点：从链尾重放；上一轮
  走完未收敛（failed 重试）：重新进入；入库幂等保证不重复创建论文）；
- 任何路径收敛：仍 active 的运行尽力收敛为 failed，绝不留永久 running；
- Phoenix 开启时以业务 span 关联研究项目 ID / 研究运行 ID / Agent Run
  ID（LangGraph 节点由 OpenInference 自动埋点挂在同一 trace 下）；
  关闭或导出失败时无副作用。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from opentelemetry.trace import StatusCode

from chatchat.server.agent_run.runtime.checkpoint import (
    get_agent_checkpoint_store,
    initialize_agent_checkpoint_store,
)
from chatchat.server.db.repository import research_repository as repo
from chatchat.server.observability import finish_span, start_business_span, set_span_attributes
from chatchat.server.research.graph import build_research_graph
from chatchat.utils import build_logger

logger = build_logger()


class ResearchRuntime:
    def __init__(
        self,
        session_factory,
        owner_id: str,
        run_id: str,
        *,
        planner: Any,
        sources: Dict[str, Any],
        model_name: str = "",
        checkpointer: Any = None,
        # 任务 018 阶段 E：证据闭环的可注入协作者（None = 按现有模型/知识库
        # 配置构建默认实现；测试注入假件，绝不连真实模型或外部网络）
        extractor: Any = None,
        unit_providers: Any = None,
        kb_names: Optional[List[str]] = None,
        comparison_generator: Any = None,
        report_generator: Any = None,
    ):
        if not owner_id:
            raise ValueError("owner_id 不能为空（必须来自服务端认证上下文）")
        if not run_id:
            raise ValueError("run_id 不能为空")
        self.session_factory = session_factory
        self.owner_id = owner_id
        self.run_id = run_id
        self.planner = planner
        self.sources = dict(sources or {})
        self.model_name = model_name
        self.checkpointer = checkpointer
        self.extractor = extractor
        self.unit_providers = unit_providers
        self.kb_names = list(kb_names) if kb_names else None
        self.comparison_generator = comparison_generator
        self.report_generator = report_generator

    # -- 辅助 ---------------------------------------------------------------

    async def _store(self):
        store = self.checkpointer or get_agent_checkpoint_store()
        if store is None:
            # 生产由 lifespan 初始化；惰性自愈仍使用本地持久化文件
            store = await initialize_agent_checkpoint_store()
        return store

    def _cfg(self) -> Dict[str, Any]:
        return {"configurable": {"thread_id": self.run_id}}

    def _summary_from_db(self) -> Dict[str, Any]:
        """从数据库读取运行状态作为权威结果（不依赖图内存）。"""
        session = self.session_factory()
        try:
            run = repo.get_owned_run(session, self.run_id, self.owner_id)
            if run is None:
                return {"run_id": self.run_id, "status": "not_found"}
            try:
                plan = json.loads(run.plan_json) if run.plan_json else {}
            except (json.JSONDecodeError, TypeError):
                plan = {}
            try:
                source_stats = json.loads(run.sources_json or "[]")
            except (json.JSONDecodeError, TypeError):
                source_stats = []
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
                "plan": plan,
                "source_stats": source_stats,
                "summary": summary,
                "error": run.error,
            }
        finally:
            session.close()

    async def _stream(self, graph: Any, input_state: Any) -> None:
        async for _chunk in graph.astream(
            input_state, self._cfg(), stream_mode="updates", durability="sync"
        ):
            # 事件消费即可；权威状态在数据库（研究 API 非流式返回摘要）
            pass

    # -- 生命周期 -----------------------------------------------------------

    async def generate_plan(self) -> Dict[str, Any]:
        """进入图生成计划并在确认节点挂起（幂等：非 pending 直接回报）。"""
        session = self.session_factory()
        try:
            run = repo.get_owned_run(session, self.run_id, self.owner_id)
            if run is None:
                raise LookupError("研究运行不存在或无权访问")
            if run.status != "pending":
                # 已生成过计划/已终态：幂等回报当前状态
                return self._summary_from_db()
            project_id = run.project_id
        finally:
            session.close()

        span = start_business_span(
            "research_run",
            {
                "chatchat.research_run_id": self.run_id,
                "chatchat.research_project_id": project_id,
                "chatchat.research_phase": "plan",
            },
        )
        try:
            store = await self._store()
            graph = build_research_graph(self, store)
            await self._stream(graph, self._initial_state(project_id))
            summary = self._summary_from_db()
            set_span_attributes(span, {"chatchat.research_status": summary["status"]})
            finish_span(span, status=StatusCode.OK)
            return summary
        except BaseException as exc:
            finish_span(span, error=exc)
            repo.converge_run_best_effort(self.session_factory, self.run_id, self.owner_id)
            raise

    async def execute(self) -> Dict[str, Any]:
        """确认之后继续执行（调用方已完成 awaiting_confirmation -> running）。"""
        return await self._resume(phase="execute")

    async def resume(self) -> Dict[str, Any]:
        """中断/重启后恢复（未确认则重新挂起，已确认则继续执行）。"""
        return await self._resume(phase="resume")

    def _initial_state(self, project_id: str) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "project_id": project_id,
            "owner_id": self.owner_id,
            "status": "pending",
            "stage": "draft",
            "plan": {},
            "source_stats": [],
            "candidates": [],
            "deduped": [],
            "screened": [],
            "persist_stats": {},
            "papers_read": 0,
            "read_stats": [],
            "skip_artifacts": False,
            "evidence_counts": {},
            "coverage": {},
            "comparison_stats": {},
            "report_id": "",
            "artifacts": {},
            "error": "",
        }

    async def _resume(self, *, phase: str) -> Dict[str, Any]:
        session = self.session_factory()
        try:
            run = repo.get_owned_run(session, self.run_id, self.owner_id)
            if run is None:
                raise LookupError("研究运行不存在或无权访问")
            project_id = run.project_id
            status = run.status
            agent_run_id = run.agent_run_id
        finally:
            session.close()

        if status == "failed":
            # 重试：failed -> running，从已持久化阶段继续（任务 017 §4.7）；
            # 入库幂等保证不重复创建同一批论文
            conv = self.session_factory()
            try:
                retried_run = repo.retry_run(conv, self.run_id, self.owner_id)
            finally:
                try:
                    conv.close()
                except Exception:
                    pass
            if retried_run is None:
                return self._summary_from_db()
            status = "running"
        elif status in repo.RUN_TERMINAL_STATUSES:
            # 已终态（completed/cancelled）：幂等回报，不重放图
            return self._summary_from_db()
        if status == "pending":
            raise RuntimeError("计划尚未生成，无法执行/恢复该研究运行")
        if status == "awaiting_confirmation":
            # 仍未确认：挂起状态保持，resume 无副作用（不检索、不收敛）
            return self._summary_from_db()

        span = start_business_span(
            "research_run",
            {
                "chatchat.research_run_id": self.run_id,
                "chatchat.research_project_id": project_id,
                "chatchat.agent_run_id": agent_run_id,
                "chatchat.research_phase": phase,
            },
        )
        try:
            store = await self._store()
            graph = build_research_graph(self, store)
            state = await graph.aget_state(self._cfg())
            pending_nodes = tuple(state.next or ())
            if not pending_nodes:
                # checkpoint 显示图已走完：两种合法情形——
                # a) 确认门禁正常结束、用户已确认（execute/resume 时
                #    数据库已是 running）；
                # b) 上一轮走完但未收敛（failed 重试，retry_run 已置
                #    running）。
                # 均以初始输入重新进入图；各节点幂等（计划已存库则
                # 不重调模型，确认节点按数据库状态放行，入库幂等），
                # 从已持久化状态继续。
                await self._stream(graph, self._initial_state(project_id))
                summary = self._summary_from_db()
                set_span_attributes(
                    span, {"chatchat.research_status": summary["status"]}
                )
                finish_span(span, status=StatusCode.OK)
                return summary
            # 崩溃于检索/去重/筛选/持久化：从 checkpoint 链尾重放
            # 未完成节点；论文入库幂等保证不重复创建
            await self._stream(graph, None)
            summary = self._summary_from_db()
            set_span_attributes(
                span,
                {
                    "chatchat.research_status": summary["status"],
                    "chatchat.agent_run_id": summary.get("agent_run_id"),
                },
            )
            finish_span(span, status=StatusCode.OK)
            return summary
        except BaseException as exc:
            finish_span(span, error=exc)
            repo.converge_run_best_effort(self.session_factory, self.run_id, self.owner_id)
            raise
        finally:
            # 双重兜底：任何路径都不留下永久 active 的运行
            repo.converge_run_best_effort(self.session_factory, self.run_id, self.owner_id)


__all__ = ["ResearchRuntime"]
