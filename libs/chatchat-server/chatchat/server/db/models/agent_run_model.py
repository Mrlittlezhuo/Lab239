from datetime import datetime
from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Integer,
    String,
    func,
)

from chatchat.server.db.base import Base


class AgentRunModel(Base):
    """
    单 Agent 闭环运行记录（任务 011）。

    一次运行 = 围绕一个用户目标的"计划—执行—观察—判断/重规划—完成"
    闭环。``owner_id`` 来自服务端认证上下文，运行与步骤的查询、取消、
    读取一律按所有者限定，不接受请求体自报身份。

    持久化内容仅为：用户目标、结构化计划文本、当前步骤序号、状态、
    最终结果、错误摘要与起止时间；不保存模型隐藏思维链。
    """

    __tablename__ = "agent_run"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', 'cancelled')",
            name="ck_agent_run_status",
        ),
        CheckConstraint("current_step >= 0", name="ck_agent_run_current_step"),
        CheckConstraint("max_steps >= 1", name="ck_agent_run_max_steps"),
    )

    id = Column(String(32), primary_key=True, comment="运行ID(UUID hex，服务端生成)")
    owner_id = Column(
        String(32),
        nullable=False,
        index=True,
        comment="所属用户ID（服务端写入，来自认证上下文）",
    )
    conversation_id = Column(
        String(32), nullable=True, comment="来源会话ID（可为空）"
    )
    source_message_id = Column(
        String(32), nullable=True, comment="来源消息ID（用户输入消息）"
    )
    goal = Column(
        String(2000), nullable=False, comment="用户目标（本轮用户输入）"
    )
    plan_text = Column(
        String(8000),
        nullable=False,
        default="",
        comment="结构化计划文本（JSON，含步骤目标与工具）",
    )
    status = Column(
        String(16),
        nullable=False,
        default="pending",
        comment="状态：pending/running/completed/failed/cancelled",
    )
    current_step = Column(
        Integer, nullable=False, default=0, comment="当前步骤序号（已创建的最大 seq）"
    )
    max_steps = Column(
        Integer,
        nullable=False,
        default=8,
        comment="本运行允许的最大步骤数（达到后收敛为 failed）",
    )
    final_result = Column(String(8000), nullable=True, comment="最终回答（截断存储）")
    error = Column(
        String(1000), nullable=True, comment="失败/取消错误摘要"
    )
    create_time = Column(DateTime, default=func.now(), comment="创建时间")
    start_time = Column(DateTime, nullable=True, comment="开始执行时间")
    end_time = Column(DateTime, nullable=True, comment="结束时间（终态）")

    def __repr__(self):
        return (
            f"<AgentRun(id='{self.id}', owner_id='{self.owner_id}', "
            f"status='{self.status}', step={self.current_step})>"
        )


__all__ = ["AgentRunModel"]