from datetime import datetime
from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Integer,
    String,
    UniqueConstraint,
    func,
)

from chatchat.server.db.base import Base


class AgentStepModel(Base):
    """
    单 Agent 运行的执行步骤（任务 011）。

    步骤在执行前创建（状态 running）、执行后立即持久化结果或错误，
    保证运行中任何时点都能看到已完成的执行轨迹。``output_summary`` /
    ``error_summary`` 为截断后的用户可见摘要；完整工具结果仅传给后续
    步骤使用，不入库。
    """

    __tablename__ = "agent_step"
    __table_args__ = (
        UniqueConstraint("run_id", "seq", name="uq_agent_step_run_seq"),
        CheckConstraint(
            "type IN ('tool_call', 'answer')", name="ck_agent_step_type"
        ),
        CheckConstraint(
            "status IN ('running', 'completed', 'failed', 'cancelled')",
            name="ck_agent_step_status",
        ),
        CheckConstraint("seq >= 1", name="ck_agent_step_seq"),
    )

    id = Column(String(32), primary_key=True, comment="步骤ID(UUID hex，服务端生成)")
    run_id = Column(String(32), nullable=False, index=True, comment="所属运行ID")
    seq = Column(Integer, nullable=False, comment="运行内顺序号（从 1 开始）")
    type = Column(
        String(16),
        nullable=False,
        default="tool_call",
        comment="步骤类型：tool_call/answer",
    )
    status = Column(
        String(16),
        nullable=False,
        default="running",
        comment="状态：running/completed/failed/cancelled",
    )
    goal = Column(
        String(500), nullable=False, default="", comment="用户可理解的步骤目标"
    )
    tool_name = Column(String(128), nullable=True, comment="工具名称（answer 步骤为空）")
    tool_input = Column(
        String(2000), nullable=True, comment="结构化输入（JSON 文本，截断）"
    )
    output_summary = Column(
        String(2000), nullable=True, comment="观察结果摘要（截断，不影响传给后续步骤的完整结果）"
    )
    error_summary = Column(String(1000), nullable=True, comment="错误摘要（截断）")
    create_time = Column(DateTime, default=func.now(), comment="创建时间")
    start_time = Column(DateTime, nullable=True, comment="开始执行时间")
    end_time = Column(DateTime, nullable=True, comment="结束时间")

    def __repr__(self):
        return (
            f"<AgentStep(id='{self.id}', run_id='{self.run_id}', seq={self.seq}, "
            f"tool='{self.tool_name}', status='{self.status}')>"
        )


__all__ = ["AgentStepModel"]