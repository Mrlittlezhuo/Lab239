"""LegacyRuntime：既有手写 AgentRunner 循环的统一接口包装（任务 014 §4/§6）。

与 ``LangGraphRuntime`` 暴露完全相同的属性面（``run_id`` / ``status`` /
``final_result`` / ``error`` / ``max_steps`` / ``model_name`` /
``source_message_id``）与 ``run()`` 异步事件生成器，上层
（``agent_run_chat`` / SSE 映射 / 消息写回）不需要区分运行时。

legacy 路径仅用于迁移期回退排错，不再承载新业务能力；它没有
checkpoint，因此不支持从断点恢复（升级前遗留的 pending/running 记录
只能查看与取消，恢复接口会给出明确状态）。
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, Optional

from chatchat.server.agent_run.runner import AgentRunner


class LegacyRuntime:
    """包装 ``AgentRunner``，使其满足统一 Agent Runtime 接口。"""

    def __init__(
        self,
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
    ):
        self._runner = AgentRunner(
            session_factory,
            owner_id,
            planner,
            tools,
            goal,
            conversation_id=conversation_id,
            source_message_id=source_message_id,
            history=history,
            memory_messages=memory_messages,
            max_steps=max_steps,
            timeout_seconds=timeout_seconds,
            model_name=model_name,
        )

    # -- 统一属性面（实时委托给 AgentRunner） -----------------------------

    @property
    def run_id(self) -> Optional[str]:
        return self._runner.run_id

    @property
    def status(self) -> str:
        return self._runner.status

    @property
    def final_result(self) -> Optional[str]:
        return self._runner.final_result

    @property
    def error(self) -> Optional[str]:
        return self._runner.error

    @property
    def max_steps(self) -> int:
        return self._runner.max_steps

    @property
    def timeout_seconds(self) -> float:
        return self._runner.timeout_seconds

    @property
    def model_name(self) -> str:
        return self._runner.model_name

    @property
    def source_message_id(self) -> Optional[str]:
        return self._runner.source_message_id

    # -- 统一事件流 -------------------------------------------------------

    async def run(self) -> AsyncIterator[dict]:
        async for ev in self._runner.run():
            yield ev


__all__ = ["LegacyRuntime"]