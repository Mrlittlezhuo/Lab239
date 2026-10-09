"""单 Agent 任务闭环（任务 011 / 014）。

独立于现有 Agent 执行器的编排/控制层：

- ``schema``：结构化计划/判断的数据模型、校验与截断工具；
- ``planner``：规划器协议与基于现有 LLM 的生产实现；
- ``runner``：既有手写运行器（任务 011 主循环，现由 ``LegacyRuntime``
  包装为回退路径）；
- ``common``：两种运行时共享的低层行为（工具调用、步骤载荷、计划落库、
  尽力收敛）；
- ``runtime``：统一运行时边界（任务 014）——``LangGraphRuntime``
  （默认，LangGraph 状态图 + 持久化 checkpoint）/ ``LegacyRuntime``
  （回退）与 ``create_agent_runtime`` 工厂。

轨迹只持久化结构化计划、工具行为、观察摘要与用户可见状态，
不保存模型隐藏思维链。
"""
from chatchat.server.agent_run.planner import LLMPlanner
from chatchat.server.agent_run.runner import (
    DEFAULT_MAX_STEPS,
    DEFAULT_TIMEOUT_SECONDS,
    AgentRunner,
)
from chatchat.server.agent_run.runtime import (
    LangGraphRuntime,
    LegacyRuntime,
    create_agent_runtime,
    runtime_name,
)

__all__ = [
    "LLMPlanner",
    "AgentRunner",
    "DEFAULT_MAX_STEPS",
    "DEFAULT_TIMEOUT_SECONDS",
    "LegacyRuntime",
    "LangGraphRuntime",
    "create_agent_runtime",
    "runtime_name",
]