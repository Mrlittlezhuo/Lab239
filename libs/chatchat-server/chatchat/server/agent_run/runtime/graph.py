"""生产单 Agent 闭环的 LangGraph 状态图（任务 014 §4/§5/§7）。

真实的"计划—执行—观察—判断—终态"闭环被拆分为五个节点与条件边
（不是把旧循环整体塞进一个节点的"形式迁移"）：

```text
prepare_plan ──► gate ──► step_start ──► step_run ──► (queue 非空? gate : decide)
     │(失败/直接回答→END)   │(取消/步数/超时/已终态→END)  │(残态→END)  │(残态→END)
     └──────────────────────┴───────────────────────────────┴──► decide ──► (replan? gate : END)
```

- ``prepare_plan``：加载上下文并规划；直接回答在此一步收敛为 completed；
  规划输出非法/异常 → failed（可控失败）。
- ``gate``：每一步开始前的门禁——运行是否已被取消/终态（直读数据库，
  跨连接可见）、是否达到最大步数、是否超过总超时（``deadline_ts``
  存于状态中，恢复后仍有效）。
- ``step_start``：执行前创建步骤行并持久化（时间线立即可见）。
- ``step_run``：调用工具并立即持久化成功/失败观察；恢复时若该步骤
  在上一个进程已留下记录（非本次运行刚创建），则复用记录而不重复
  调用工具，记录仍为 ``running``（上次中断在步骤执行中）时可控失败。
- ``decide``：判断下一动作——answer（写入答案步骤并 completed）/
  fail（failed）/ replan（补充步骤回到 gate，循环门禁保证有界）。

Run/Step（``agent_run`` / ``agent_step`` 表）在每个节点开始/结束时
同步持久化，继续作为用户可见轨迹与权限查询来源；LangGraph checkpoint
只保存编排状态与恢复位置（``run_id == thread_id``）。两类状态不一致时
一律可控失败，绝不静默重复执行工具。

状态字段全部 JSON 可序列化（checkpoint 需要），不保存模型隐藏思维链。
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph

from chatchat.server.agent_run import schema as S
from chatchat.server.agent_run.common import invoke_tool, set_plan_text
from chatchat.server.db.models.agent_run_model import AgentRunModel
from chatchat.server.db.models.agent_step_model import AgentStepModel
from chatchat.server.db.repository import agent_run_repository as repo


class AgentRunState(TypedDict, total=False):
    """LangGraph 状态（``run_id`` 即 ``thread_id``）。"""

    run_id: str  # 稳定运行标识 = LangGraph thread_id
    owner_id: str  # 当前用户（仅来自服务端认证上下文）
    goal: str  # 用户目标
    max_steps: int  # 步数上限（有界循环）
    deadline_ts: float  # 总超时截止时间（wall clock，跨进程/恢复后仍有效）
    model_name: str  # 规划/判断所用模型名（仅轨迹展示）
    plan: dict  # 结构化计划（plan.to_dict()）
    queue: list  # 待执行步骤（PlanStep.to_dict() 列表）
    steps_done: int  # 已执行（工具）步骤数
    results: list  # 已执行步骤观察（StepResult 字典列表）
    replan_steps: list  # 临时字段：decide 节点重规划补充的步骤（供 SSE）
    reused_step: int  # 临时字段：step_run 复用上进程残留观察的步骤 seq（0=否）
    status: str  # pending/running/completed/failed/cancelled
    final_result: str  # 最终答案
    error: str  # 失败原因


def _step_row(session: Any, run_id: str, owner_id: str, seq: int):
    """按 (run_id, seq) 查询步骤行（校验所有者），不存在时返回 None。"""
    return (
        session.query(AgentStepModel)
        .join(AgentRunModel, AgentStepModel.run_id == AgentRunModel.id)
        .filter(
            AgentStepModel.run_id == run_id,
            AgentStepModel.seq == int(seq),
            AgentRunModel.owner_id == owner_id,
        )
        .one_or_none()
    )


def _fail_terminal(session: Any, run_id: str, owner_id: str, error: str) -> Dict[str, Any]:
    """把运行收敛为 failed（可控失败），返回状态更新。"""
    msg = S.truncate_text(error, 1000)
    try:
        repo.finalize_run(session, run_id, owner_id, "failed", error=msg)
    except Exception:
        try:
            session.rollback()
        except Exception:
            pass
    return {"status": "failed", "error": msg, "queue": []}


def _is_terminal(state: AgentRunState) -> bool:
    return state.get("status") in repo.RUN_TERMINAL_STATUSES


def build_agent_graph(rt: Any, checkpointer: Any):
    """构建并编译生产单 Agent 闭环状态图。

    ``rt`` 为 ``LangGraphRuntime`` 实例（节点通过闭包使用其
    session_factory / planner / tools / history / memory_messages /
    max_steps / timeout_seconds / goal）；``checkpointer`` 为持久化
    checkpointer（生产为 ``AsyncSqliteSaver``）。

    ``fresh_seqs`` 记录本次进程内 ``step_start`` 刚创建的步骤行 seq：
    恢复运行时据此区分"上一个进程遗留的步骤行"（只能可控失败或复用
    观察）与"本次运行新创建、应当正常执行的步骤行"。
    """

    fresh_seqs: set = set()

    def _ctx(state: AgentRunState) -> S.PlanContext:
        return S.PlanContext(
            goal=state.get("goal") or rt.goal,
            tools=rt.tools,
            history=list(rt.history),
            memory_messages=list(rt.memory_messages),
            max_steps=rt.max_steps,
        )

    # -- 节点 ------------------------------------------------------------

    async def prepare_plan_node(state: AgentRunState) -> Dict[str, Any]:
        """准备上下文并规划；直接回答在此收敛为 completed。"""
        session = rt.session_factory()
        run_id, owner = state["run_id"], state["owner_id"]
        try:
            try:
                plan = rt.planner.plan(_ctx(state))
            except S.PlanError as exc:
                return _fail_terminal(
                    session, run_id, owner, f"计划生成失败（模型输出无效）：{exc}"
                )
            except Exception as exc:
                return _fail_terminal(session, run_id, owner, f"计划生成异常：{exc}")
            set_plan_text(session, run_id, owner, plan)
            if plan.is_direct_answer:
                step = repo.add_step(
                    session, run_id, owner, 1,
                    stype="answer", goal="直接回答", tool_name=None,
                )
                if step is not None:
                    repo.update_step(
                        session, step.id, owner, status="completed",
                        output_summary=S.truncate_text(plan.direct_answer, 2000),
                        end_time_now=True,
                    )
                repo.finalize_run(
                    session, run_id, owner, "completed",
                    final_result=S.truncate_text(plan.direct_answer, 8000),
                )
                return {
                    "plan": plan.to_dict(),
                    "queue": [],
                    "status": "completed",
                    "final_result": plan.direct_answer,
                }
            return {
                "plan": plan.to_dict(),
                "queue": [s.to_dict() for s in plan.steps],
                "status": "running",
            }
        finally:
            session.close()

    async def gate_node(state: AgentRunState) -> Dict[str, Any]:
        """每一步开始前的门禁：取消/已终态/最大步数/总超时/恢复残态。"""
        session = rt.session_factory()
        run_id, owner = state["run_id"], state["owner_id"]
        try:
            current = repo.run_status(session, run_id, owner)
            if current is None:
                return _fail_terminal(session, run_id, owner, "运行记录不可用")
            if current in repo.RUN_TERMINAL_STATUSES:
                # 运行已被取消 API 或其他进程收敛：停止，不再开始新步骤
                update: Dict[str, Any] = {"status": current, "queue": []}
                if current == "completed":
                    update["final_result"] = state.get("final_result", "")
                return update
            # 恢复场景防御：若仍有步骤停留在 running（上一次运行中断），
            # 不静默重跑工具，直接可控失败
            steps = repo.get_owned_steps(session, run_id, owner)
            if any(s.status == "running" for s in steps):
                return _fail_terminal(
                    session, run_id, owner,
                    "恢复时检测到未完成的步骤（上次运行中断于步骤执行中），"
                    "为避免重复执行工具已停止",
                )
            if state.get("steps_done", 0) + 1 > rt.max_steps:
                return _fail_terminal(
                    session, run_id, owner,
                    f"达到最大步数（{rt.max_steps}），停止继续调用工具，任务未完成",
                )
            if time.time() > state.get("deadline_ts", float("inf")):
                return _fail_terminal(
                    session, run_id, owner,
                    f"运行超时（超过 {int(rt.timeout_seconds)} 秒），停止执行",
                )
            return {}
        finally:
            session.close()

    async def step_start_node(state: AgentRunState) -> Dict[str, Any]:
        """执行前创建步骤行并持久化（时间线立即可见）。"""
        if not state.get("queue"):
            return {}
        ps = state["queue"][0]
        seq = state.get("steps_done", 0) + 1
        session = rt.session_factory()
        run_id, owner = state["run_id"], state["owner_id"]
        try:
            existing = _step_row(session, run_id, owner, seq)
            if existing is not None:
                if existing.status == "running":
                    # 恢复场景残留（上次 step_start 已落库但未完成图步骤）：
                    # 无法安全判断工具是否已执行，可控失败而非重跑
                    return _fail_terminal(
                        session, run_id, owner,
                        f"步骤 {seq} 存在未收敛的残留记录，为避免重复执行工具已停止",
                    )
                # 上一个进程已把该步骤执行到 completed/failed（checkpoint
                # 落后于步骤表）：跳过建行，step_run 复用既有观察
                return {}
            step = repo.add_step(
                session, run_id, owner, seq,
                stype="tool_call", goal=ps.get("goal", ""),
                tool_name=ps.get("tool") or None,
                tool_input=S.truncate_text(
                    json.dumps(ps.get("args") or {}, ensure_ascii=False), 2000
                ),
            )
            if step is None:
                # 与取消 API 的竞态：门禁检查后运行已被置为终态
                current = repo.run_status(session, run_id, owner) or "cancelled"
                return {"status": current, "queue": []}
            repo.update_step(session, step.id, owner, start_time_now=True)
            fresh_seqs.add(seq)
            return {}
        finally:
            session.close()

    async def step_run_node(
        state: AgentRunState, config: Optional[RunnableConfig] = None
    ) -> Dict[str, Any]:
        """调用当前步骤的工具并立即持久化观察（成功/失败）。

        恢复语义（``__pregel_resuming`` 标记本次调用为恢复）：
        - 步骤行在上一个进程已存在（非本次 ``step_start`` 创建）且为
          completed/failed：复用其记录，不重复调用工具；
        - 步骤行在上一个进程已存在且仍为 running：上次运行中断于该步骤
          （无法确定工具是否已执行），可控失败；
        - 步骤行由本次运行 ``step_start`` 刚创建（恢复中也可能出现）：
          正常执行工具；
        - 无步骤行：checkpoint 与 Run/Step 不一致，可控失败。

        复用上进程残留观察（completed/failed）时在状态更新中带回
        ``reused_step=seq``，供运行时抑制重复的 ``step_finished`` 事件。
        """
        if not state.get("queue"):
            return {}
        ps = state["queue"][0]
        seq = state.get("steps_done", 0) + 1
        resuming = bool(
            ((config or {}).get("configurable") or {}).get("__pregel_resuming")
        )
        session = rt.session_factory()
        run_id, owner = state["run_id"], state["owner_id"]
        try:
            row = _step_row(session, run_id, owner, seq)
            # 恢复场景：仅当该步骤行"早于本次进程存在"（不在 fresh_seqs
            # 中）时，才视为上一个进程遗留的记录；本次 step_start 刚
            # 创建的行（恢复运行中同样会出现）走正常执行路径。
            residual = resuming and row is not None and seq not in fresh_seqs
            if residual:
                if row.status == "running":
                    return _fail_terminal(
                        session, run_id, owner,
                        f"步骤 {seq} 上次运行中断于工具执行中途，"
                        "为避免重复执行工具已停止",
                    )
                if row.status == "completed":
                    output = row.output_summary or ""
                    error = ""
                else:
                    output = ""
                    error = row.error_summary or "工具步骤上次执行失败"
            else:
                if row is None:
                    return _fail_terminal(
                        session, run_id, owner,
                        f"步骤 {seq} 记录缺失，为避免不一致执行已停止",
                    )
                output, error = "", ""
                tool = rt.tools.get(ps.get("tool") or "")
                if tool is None:
                    error = f"工具 '{ps.get('tool')}' 当前不可用"
                else:
                    try:
                        result = await invoke_tool(tool, ps.get("args") or {})
                        output = result if isinstance(result, str) else str(result)
                    except Exception as exc:
                        error = str(exc)
                if error:
                    repo.update_step(
                        session, row.id, owner, status="failed",
                        error_summary=S.truncate_text(error, 1000),
                        end_time_now=True,
                    )
                else:
                    repo.update_step(
                        session, row.id, owner, status="completed",
                        output_summary=S.truncate_text(output, 2000),
                        end_time_now=True,
                    )
            results: List[Dict[str, Any]] = list(state.get("results") or [])
            results.append(
                {
                    "seq": seq,
                    "goal": ps.get("goal", ""),
                    "tool": ps.get("tool", ""),
                    "args": ps.get("args") or {},
                    "output": output,
                    "error": error,
                }
            )
            queue = list(state.get("queue") or [])[1:]
            # 复用上进程残留观察时标记 seq：运行时据此在恢复流中不再重复
            # 发该步骤的 step_finished 事件（上一段流已发过）
            return {
                "queue": queue,
                "steps_done": seq,
                "results": results,
                "reused_step": seq if residual else 0,
            }
        finally:
            session.close()

    async def decide_node(state: AgentRunState) -> Dict[str, Any]:
        """判断下一动作：answer / fail / replan。"""
        session = rt.session_factory()
        run_id, owner = state["run_id"], state["owner_id"]
        try:
            results = [S.StepResult(**r) for r in state.get("results") or []]
            try:
                decision = rt.planner.decide(_ctx(state), results)
            except S.PlanError as exc:
                return _fail_terminal(
                    session, run_id, owner, f"执行判断失败（模型输出无效）：{exc}"
                )
            except Exception as exc:
                return _fail_terminal(session, run_id, owner, f"执行判断异常：{exc}")
            if decision.action == "answer":
                seq = state.get("steps_done", 0) + 1
                answer_step = repo.add_step(
                    session, run_id, owner, seq,
                    stype="answer", goal="综合观察形成最终答案", tool_name=None,
                )
                if answer_step is not None:
                    repo.update_step(
                        session, answer_step.id, owner, status="completed",
                        output_summary=S.truncate_text(decision.answer, 2000),
                        end_time_now=True,
                    )
                repo.finalize_run(
                    session, run_id, owner, "completed",
                    final_result=S.truncate_text(decision.answer, 8000),
                )
                return {
                    "status": "completed",
                    "final_result": decision.answer,
                    "queue": [],
                }
            if decision.action == "fail":
                reason = decision.reason or "Agent 判定无法完成任务"
                return _fail_terminal(session, run_id, owner, reason)
            # replan：补充步骤（gate 门禁保证有界）
            new_steps = [s.to_dict() for s in decision.steps]
            return {"queue": new_steps, "replan_steps": new_steps}
        finally:
            session.close()

    # -- 图结构 ----------------------------------------------------------

    graph = StateGraph(AgentRunState)
    graph.add_node("prepare_plan", prepare_plan_node)
    graph.add_node("gate", gate_node)
    graph.add_node("step_start", step_start_node)
    graph.add_node("step_run", step_run_node)
    graph.add_node("decide", decide_node)
    graph.set_entry_point("prepare_plan")

    def _to_end_or(node: str):
        return lambda s: END if _is_terminal(s) else node

    graph.add_conditional_edges("prepare_plan", _to_end_or("gate"),
                                {END: END, "gate": "gate"})
    graph.add_conditional_edges("gate", _to_end_or("step_start"),
                                {END: END, "step_start": "step_start"})
    graph.add_conditional_edges("step_start", _to_end_or("step_run"),
                                {END: END, "step_run": "step_run"})

    def _after_step_run(s: AgentRunState) -> str:
        if _is_terminal(s):
            return END
        return "gate" if s.get("queue") else "decide"

    graph.add_conditional_edges("step_run", _after_step_run,
                                {END: END, "gate": "gate", "decide": "decide"})
    graph.add_conditional_edges("decide", _to_end_or("gate"),
                                {END: END, "gate": "gate"})

    return graph.compile(checkpointer=checkpointer)


__all__ = ["AgentRunState", "build_agent_graph"]