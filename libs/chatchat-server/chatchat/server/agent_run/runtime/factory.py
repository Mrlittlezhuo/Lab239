"""统一 Agent Runtime 工厂（任务 014 §4/§6）。

上层（``agent_run_chat`` / ``agent_run_resume``）不直接依赖"手写循环还是
LangGraph"，只依赖本工厂产出的统一运行时接口：

- ``langgraph``（默认）：``LangGraphRuntime``——LangGraph 状态图 +
  持久化 ``AsyncSqliteSaver`` checkpoint，支持从断点恢复；
- ``legacy``（回退）：``LegacyRuntime``——既有手写 ``AgentRunner``
  循环，仅用于迁移期排错；无 checkpoint，不支持断点恢复（对
  ``existing_run_id`` 直接给出明确错误）。

运行时由 ``Settings.model_settings.AGENT_RUNTIME`` 配置；未知取值
告警后按 ``legacy`` 处理（回退开关必须始终可用，不能成为摆设）。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from chatchat.settings import Settings
from chatchat.utils import build_logger

logger = build_logger()

_RUNTIMES = ("langgraph", "legacy")


def runtime_name() -> str:
    """当前配置的运行时（``langgraph`` / ``legacy``）。"""
    name = str(
        getattr(Settings.model_settings, "AGENT_RUNTIME", "langgraph") or "langgraph"
    ).strip().lower()
    if name not in _RUNTIMES:
        logger.warning(
            "未知的 AGENT_RUNTIME=%r，按 legacy 回退处理", name
        )
        return "legacy"
    return name


def create_agent_runtime(
    session_factory,
    owner_id: str,
    planner: Any,
    tools: Dict[str, Any],
    goal: str,
    *,
    conversation_id: Optional[str] = None,
    source_message_id: Optional[str] = None,
    history: Optional[list] = None,
    memory_messages: Optional[list] = None,
    max_steps: Optional[int] = None,
    timeout_seconds: Optional[float] = None,
    model_name: str = "",
    checkpointer: Any = None,
    existing_run_id: Optional[str] = None,
    forced: Optional[str] = None,
):
    """按配置创建统一 Agent 运行时实例。

    ``forced`` 可显式指定运行时（测试用）；``existing_run_id`` 仅
    ``langgraph`` 支持（断点恢复），``legacy`` 会抛出明确错误。
    """
    name = (forced or runtime_name()).strip().lower()
    common = dict(
        session_factory=session_factory,
        owner_id=owner_id,
        planner=planner,
        tools=tools,
        goal=goal,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
        history=history,
        memory_messages=memory_messages,
        max_steps=max_steps,
        timeout_seconds=timeout_seconds,
        model_name=model_name,
    )
    if name != "langgraph":
        from chatchat.server.agent_run.runtime.legacy import LegacyRuntime

        if name != "legacy":
            logger.warning(
                "未知的 AGENT_RUNTIME=%r，按 legacy 回退处理", name
            )
        if existing_run_id:
            raise ValueError(
                "legacy 运行时（回退）不支持从 checkpoint 断点恢复；"
                "该运行无法安全恢复，仅可查看或取消"
            )
        return LegacyRuntime(**common)

    from chatchat.server.agent_run.runtime.langgraph_runtime import (
        LangGraphRuntime,
    )

    return LangGraphRuntime(
        **common,
        checkpointer=checkpointer,
        existing_run_id=existing_run_id,
    )


__all__ = ["create_agent_runtime", "runtime_name"]