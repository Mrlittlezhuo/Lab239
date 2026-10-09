"""统一 Agent 运行时的共享底层行为（任务 014）。

``LegacyRuntime``（手写 AgentRunner 循环）与 ``LangGraphRuntime``（LangGraph
状态图）复用这里同一套低层语义，保证两种运行时在工具调用、步骤载荷、计划文本
持久化和终态收敛上完全一致：

- ``invoke_tool``：普通工具（同步 ``invoke``）与异步工具（``ainvoke``）统一调用入口；
- ``step_payload``：SSE 事件与轨迹查询共用的步骤载荷；
- ``set_plan_text``：计划文本写回 ``agent_run.plan_text``；
- ``converge_run_best_effort``：请求中断/异常退出时的尽力终态收敛，
  绝不让一次运行停留在永久 ``running``。
"""

from __future__ import annotations

from typing import Any, Callable, Dict

from chatchat.server.agent_run import schema as S
from chatchat.server.db.repository import agent_run_repository as repo


def step_payload(step: Any) -> Dict[str, Any]:
    """步骤轨迹载荷（与 AgentRunner._step_payload 语义一致）。"""
    return {
        "id": step.id,
        "seq": step.seq,
        "type": step.type,
        "status": step.status,
        "goal": step.goal,
        "tool_name": step.tool_name,
        "tool_input": step.tool_input,
        "output_summary": step.output_summary,
        "error_summary": step.error_summary,
    }


async def invoke_tool(tool: Any, args: Dict[str, Any]) -> str:
    """统一调用普通工具或异步工具，异常向上抛出由调用方收敛。

    注意：故意不使用 ``asyncio.to_thread`` 包装同步 ``invoke`` —— 该包装会在
    部分离线测试环境（事件循环被嵌套/独占）中死锁，与任务 011 的既有行为一致。
    """
    ainvoke = getattr(tool, "ainvoke", None)
    if callable(ainvoke):
        return await ainvoke(args)
    invoke = getattr(tool, "invoke", None)
    if not callable(invoke):
        raise TypeError(f"工具 {getattr(tool, 'name', repr(tool))!r} 不可调用")
    return invoke(args)


def set_plan_text(session: Any, run_id: str, owner_id: str, plan: S.Plan) -> None:
    """把计划文本（JSON 序列化）写回运行记录；失败仅回滚不抛出。"""
    try:
        from sqlalchemy import text as _text

        session.execute(
            _text("UPDATE agent_run SET plan_text = :plan WHERE id = :id AND owner_id = :owner"),
            {"plan": S.truncate_text(plan.to_json(), 8000), "id": run_id, "owner": owner_id},
        )
        session.commit()
    except Exception:
        try:
            session.rollback()
        except Exception:
            pass


def converge_run_best_effort(
    session_factory: Callable[[], Any], run_id: str | None, owner_id: str | None
) -> bool:
    """在独立 session 上做尽力终态收敛；吞掉一切异常。

    运行仍处于 pending/running 时置为 failed（"运行中断，未正常结束"）；
    已终态或记录不存在时不做任何事。返回是否发生了 failed 收敛。
    """
    if not run_id or not owner_id:
        return False
    fresh = session_factory()
    try:
        status = repo.run_status(fresh, run_id, owner_id)
        if status not in (None, *repo.RUN_TERMINAL_STATUSES):
            repo.ensure_terminal(fresh, run_id, owner_id, error="运行中断，未正常结束")
            return True
    except Exception:
        pass
    finally:
        try:
            fresh.close()
        except Exception:
            pass
    return False