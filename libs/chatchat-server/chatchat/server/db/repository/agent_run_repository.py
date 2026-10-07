"""Agent 运行/步骤 Repository（owner-aware，任务 011）。

所有读写都同时校验 ``run_id/step_id`` 与 ``owner_id``；owner 不匹配或
记录不存在时一律视为"不可见"（返回 None），不区分原因，避免运行/步骤
枚举。owner 来自服务端认证上下文，不接受客户端指定。

状态机：

- 运行：``pending -> running -> completed | failed | cancelled``；
  终态（completed/failed/cancelled）不再被更新。
- 取消：API 直接把 pending/running 的运行置为 ``cancelled``（原子
  UPDATE）；运行器在"启动新步骤前"重新读取数据库状态，发现
  ``cancelled`` 即停止，不再开始新步骤——满足"当前不可中断工具返回
  后、下一步开始前生效"，不引入线程强制终止。
- 步骤：执行前创建（``running``）、执行后立即持久化结果或错误；
  运行到达终态后不再新增步骤。

状态读取用原生 SQL 直读数据库（绕过 ORM 身份映射缓存），保证能观察到
其他连接（取消 API、其他进程）的提交。
"""
from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from chatchat.server.db.models.agent_run_model import AgentRunModel
from chatchat.server.db.models.agent_step_model import AgentStepModel

RUN_STATUSES = ("pending", "running", "completed", "failed", "cancelled")
RUN_TERMINAL_STATUSES = ("completed", "failed", "cancelled")
RUN_ACTIVE_STATUSES = ("pending", "running")
STEP_STATUSES = ("running", "completed", "failed", "cancelled")
STEP_TYPES = ("tool_call", "answer")


def create_run(
    session: Session,
    owner_id: str,
    goal: str,
    *,
    conversation_id: str | None = None,
    source_message_id: str | None = None,
    max_steps: int = 8,
    plan_text: str = "",
    run_id: str | None = None,
) -> AgentRunModel:
    """新增一个待执行运行（pending）；owner 由服务端写入。"""
    if not owner_id:
        raise ValueError("owner_id 不能为空")
    goal = (goal or "").strip()
    if not goal:
        raise ValueError("goal 不能为空")
    max_steps = max(1, int(max_steps))
    run = AgentRunModel(
        id=run_id or uuid.uuid4().hex,
        owner_id=owner_id,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
        goal=goal[:2000],
        plan_text=(plan_text or "")[:8000],
        status="pending",
        current_step=0,
        max_steps=max_steps,
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


def start_run(session: Session, run_id: str, owner_id: str) -> AgentRunModel | None:
    """``pending -> running``（原子 UPDATE）；非本用户或已终态返回 None。"""
    result = session.execute(
        text(
            "UPDATE agent_run SET status = 'running', start_time = CURRENT_TIMESTAMP "
            "WHERE id = :id AND owner_id = :owner AND status = 'pending'"
        ),
        {"id": run_id, "owner": owner_id},
    )
    session.commit()
    if result.rowcount:
        run = get_owned_run(session, run_id, owner_id)
        if run is not None:
            session.refresh(run)
        return run
    return None


def get_owned_run(
    session: Session, run_id: str, owner_id: str
) -> AgentRunModel | None:
    """同时按 id + owner_id 过滤；不满足返回 None。"""
    if not run_id or not owner_id:
        return None
    return (
        session.query(AgentRunModel)
        .filter(AgentRunModel.id == run_id, AgentRunModel.owner_id == owner_id)
        .one_or_none()
    )


def list_runs(
    session: Session,
    owner_id: str,
    *,
    conversation_id: str | None = None,
    limit: int = 20,
) -> list[AgentRunModel]:
    """当前用户的运行列表（create_time DESC）。"""
    q = session.query(AgentRunModel).filter(AgentRunModel.owner_id == owner_id)
    if conversation_id:
        q = q.filter(AgentRunModel.conversation_id == conversation_id)
    return (
        q.order_by(AgentRunModel.create_time.desc(), AgentRunModel.id.desc())
        .limit(max(1, min(int(limit or 20), 100)))
        .all()
    )


def get_owned_steps(
    session: Session, run_id: str, owner_id: str
) -> list[AgentStepModel]:
    """当前用户某运行的全部步骤（seq ASC）；运行不存在/非本用户返回 []。"""
    if not run_id or not owner_id:
        return []
    return (
        session.query(AgentStepModel)
        .join(AgentRunModel, AgentStepModel.run_id == AgentRunModel.id)
        .filter(
            AgentStepModel.run_id == run_id,
            AgentRunModel.owner_id == owner_id,
        )
        .order_by(AgentStepModel.seq)
        .all()
    )


def run_status(session: Session, run_id: str, owner_id: str) -> str | None:
    """直读数据库中的运行状态（绕过身份映射缓存）；不存在/非本用户返回 None。"""
    if not run_id or not owner_id:
        return None
    row = session.execute(
        text(
            "SELECT status FROM agent_run WHERE id = :id AND owner_id = :owner"
        ),
        {"id": run_id, "owner": owner_id},
    ).one_or_none()
    return row[0] if row is not None else None


def add_step(
    session: Session,
    run_id: str,
    owner_id: str,
    seq: int,
    *,
    stype: str = "tool_call",
    goal: str = "",
    tool_name: str | None = None,
    tool_input: str | None = None,
) -> AgentStepModel | None:
    """在执行前创建步骤（running）；运行非本用户或已终态返回 None。

    终态检查直读数据库，保证其他连接发起的取消立即阻止新步骤。
    """
    status = run_status(session, run_id, owner_id)
    if status is None or status in RUN_TERMINAL_STATUSES:
        return None
    stype = stype if stype in STEP_TYPES else "tool_call"
    step = AgentStepModel(
        id=uuid.uuid4().hex,
        run_id=run_id,
        seq=int(seq),
        type=stype,
        status="running",
        goal=(goal or "")[:500],
        tool_name=(tool_name or "")[:128] if tool_name else None,
        tool_input=(tool_input or "")[:2000] if tool_input else None,
    )
    session.add(step)
    session.execute(
        text(
            "UPDATE agent_run SET current_step = :seq "
            "WHERE id = :id AND owner_id = :owner"
        ),
        {"seq": int(seq), "id": run_id, "owner": owner_id},
    )
    session.commit()
    session.refresh(step)
    return step


def update_step(
    session: Session,
    step_id: str,
    owner_id: str,
    *,
    status: str | None = None,
    output_summary: str | None = None,
    error_summary: str | None = None,
    start_time_now: bool = False,
    end_time_now: bool = False,
) -> AgentStepModel | None:
    """执行后立即持久化步骤结果/错误；非本用户步骤返回 None。"""
    step = (
        session.query(AgentStepModel)
        .join(AgentRunModel, AgentStepModel.run_id == AgentRunModel.id)
        .filter(
            AgentStepModel.id == step_id,
            AgentRunModel.owner_id == owner_id,
        )
        .one_or_none()
    )
    if step is None:
        return None
    if status is not None:
        step.status = status if status in STEP_STATUSES else "completed"
    if output_summary is not None:
        step.output_summary = output_summary[:2000]
    if error_summary is not None:
        step.error_summary = error_summary[:1000]
    if start_time_now:
        step.start_time = _now()
    if end_time_now:
        step.end_time = _now()
    session.commit()
    session.refresh(step)
    return step


def finalize_run(
    session: Session,
    run_id: str,
    owner_id: str,
    status: str,
    *,
    final_result: str | None = None,
    error: str | None = None,
) -> AgentRunModel | None:
    """把运行收敛到终态（原子 UPDATE，仅非终态可迁移）。

    已终态（含被取消 API 抢先置为 cancelled）返回 None，不覆盖。
    """
    if status not in RUN_TERMINAL_STATUSES:
        raise ValueError(f"非法终态：{status}")
    result = session.execute(
        text(
            "UPDATE agent_run SET status = :status, end_time = CURRENT_TIMESTAMP, "
            "final_result = COALESCE(:final, final_result), "
            "error = COALESCE(:err, error) "
            "WHERE id = :id AND owner_id = :owner AND status IN ('pending', 'running')"
        ),
        {
            "status": status,
            "final": final_result,
            "err": error,
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


def cancel_run(session: Session, run_id: str, owner_id: str) -> AgentRunModel | None:
    """取消运行（原子 UPDATE）。

    - 非本用户/不存在：返回 None（调用方映射 404）；
    - 已终态：保持原状，返回当前运行；
    - pending/running：置为 ``cancelled``。运行器在下一步开始前读到该状态
      后停止，不再启动新步骤。
    """
    run = get_owned_run(session, run_id, owner_id)
    if run is None:
        return None
    if run.status in RUN_TERMINAL_STATUSES:
        return run
    result = session.execute(
        text(
            "UPDATE agent_run SET status = 'cancelled', end_time = CURRENT_TIMESTAMP, "
            "error = COALESCE(error, '用户取消') "
            "WHERE id = :id AND owner_id = :owner AND status IN ('pending', 'running')"
        ),
        {"id": run_id, "owner": owner_id},
    )
    session.commit()
    run = get_owned_run(session, run_id, owner_id)
    if run is not None:
        session.refresh(run)
    return run


def ensure_terminal(
    session: Session, run_id: str, owner_id: str, error: str = "运行中断"
) -> AgentRunModel | None:
    """兜底收敛：仍处于 pending/running 的运行置为 failed。

    用于运行器进程异常退出/请求中断后，避免留下永久 running 的运行。
    已终态（含 cancelled）返回 None。
    """
    return finalize_run(session, run_id, owner_id, "failed", error=error[:1000])


def _now():
    from datetime import datetime

    return datetime.now()


__all__ = [
    "RUN_STATUSES",
    "RUN_TERMINAL_STATUSES",
    "RUN_ACTIVE_STATUSES",
    "STEP_STATUSES",
    "STEP_TYPES",
    "create_run",
    "start_run",
    "get_owned_run",
    "list_runs",
    "get_owned_steps",
    "run_status",
    "add_step",
    "update_step",
    "finalize_run",
    "cancel_run",
    "ensure_terminal",
]