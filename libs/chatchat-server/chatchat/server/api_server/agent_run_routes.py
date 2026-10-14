"""Agent 运行查询与取消接口（任务 011）。

独立于 ``/chat`` 路由的 Agent 运行管理接口，沿用现有 Bearer 认证：

- ``GET  /agent-runs``           当前用户的运行列表（可按会话过滤）；
- ``GET  /agent-runs/{run_id}``  运行详情（含结构化计划与步骤轨迹）；
- ``POST /agent-runs/{run_id}/cancel``  取消运行（pending/running 生效；
  已终态保持原状）；
- ``POST /agent-runs/{run_id}/resume``  从 checkpoint 断点恢复中断的运行
  （任务 014；仅 langgraph 运行时支持，legacy 回退给出明确状态）。

所有接口的 owner 只取自服务端认证上下文（``user.id``），不读取请求体中的
任何用户身份；非本人/不存在的运行一律 404，不区分原因。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from chatchat.server.auth.deps import require_password_changed
from chatchat.server.db.models.user_model import UserModel
from chatchat.server.db.session import get_db
from chatchat.server.db.repository import agent_run_repository as agent_run_repo

agent_run_router = APIRouter(
    prefix="/agent-runs",
    tags=["Agent 运行"],
    dependencies=[Depends(require_password_changed)],
)


def _iso(value) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _run_to_dict(run) -> dict:
    return {
        "run_id": run.id,
        "conversation_id": run.conversation_id,
        "source_message_id": run.source_message_id,
        "goal": run.goal,
        "plan_text": run.plan_text,
        "status": run.status,
        "current_step": run.current_step,
        "max_steps": run.max_steps,
        "final_result": run.final_result,
        "error": run.error,
        "create_time": _iso(run.create_time),
        "start_time": _iso(run.start_time),
        "end_time": _iso(run.end_time),
    }


def _step_to_dict(step) -> dict:
    return {
        "step_id": step.id,
        "seq": step.seq,
        "type": step.type,
        "status": step.status,
        "goal": step.goal,
        "tool_name": step.tool_name,
        "tool_input": step.tool_input,
        "output_summary": step.output_summary,
        "error_summary": step.error_summary,
        "create_time": _iso(step.create_time),
        "start_time": _iso(step.start_time),
        "end_time": _iso(step.end_time),
    }


@agent_run_router.get("", summary="查询当前用户的 Agent 运行列表")
async def list_agent_runs(
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
    conversation_id: Optional[str] = Query(
        None, description="按会话过滤；传入的会话不属于当前用户时返回空列表"
    ),
    limit: int = Query(20, ge=1, le=100, description="返回条数上限"),
) -> dict:
    if conversation_id:
        from chatchat.server.db.repository import get_owned_conversation

        if get_owned_conversation(session, conversation_id, user.id) is None:
            return {"runs": []}
    runs = agent_run_repo.list_runs(
        session, user.id, conversation_id=conversation_id, limit=limit
    )
    return {"runs": [_run_to_dict(run) for run in runs]}


@agent_run_router.get("/{run_id}", summary="查询 Agent 运行详情（含步骤轨迹）")
async def get_agent_run(
    run_id: str,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> dict:
    run = agent_run_repo.get_owned_run(session, run_id, user.id)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="运行不存在或无权访问",
        )
    steps = agent_run_repo.get_owned_steps(session, run_id, user.id)
    return {"run": _run_to_dict(run), "steps": [_step_to_dict(s) for s in steps]}


@agent_run_router.post("/{run_id}/cancel", summary="取消运行中的 Agent")
async def cancel_agent_run(
    run_id: str,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> dict:
    """把 pending/running 的运行置为 cancelled（原子更新）。

    运行器在开始新步骤前重新读取数据库状态，取消后不再开始新步骤；
    已终态的运行保持原状并原样返回。
    """
    run = agent_run_repo.cancel_run(session, run_id, user.id)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="运行不存在或无权访问",
        )
    return {"run": _run_to_dict(run)}


@agent_run_router.post("/{run_id}/resume", summary="从 checkpoint 断点恢复中断的运行（任务 014）")
async def resume_agent_run(
    run_id: str,
    user: UserModel = Depends(require_password_changed),
    stream: bool = Query(False, description="True 返回 SSE 事件流；False 返回聚合结果"),
    tool_config: Optional[Dict[str, Any]] = Body(
        None, description="工具配置；缺省时使用完整工具注册表"
    ),
    use_mcp: bool = Body(False, description="是否加载启用的 MCP 工具"),
    use_memory: bool = Body(True, description="本轮是否使用个人记忆（个人偏好/长期记忆）"),
):
    """恢复必须通过认证上下文校验运行归属（任务 014 §5/§7）。

    - 非本人/不存在的运行：404；
    - 当前运行时为 legacy（迁移期回退）：409 并给出明确状态
      （旧记录仅可查看/取消，绝不当作新图执行）；
    - 已终态运行：幂等，原样返回终态结果。
    """
    from chatchat.server.agent_run.service import agent_run_resume

    try:
        return await agent_run_resume(
            run_id=run_id,
            owner_id=user.id,
            stream=stream,
            tool_config=tool_config,
            use_mcp=use_mcp,
            use_memory=use_memory,
        )
    except LookupError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="运行不存在或无权访问",
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


__all__ = ["agent_run_router"]