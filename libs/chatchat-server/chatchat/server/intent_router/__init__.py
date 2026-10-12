"""意图路由（任务 012）：决策来源可插拔，失败统一回退普通对话。

- ``route_auto_agent``：chat API 自动模式唯一入口；
- ``IntentRouter`` + ``IntentProvider``：可测试、可替换的路由组件边界，
  后续接入 FastJev 只需实现 ``IntentProvider`` 接口；
- ``RouteResult`` / ``validate_route_result``：结构化、可校验的路由结果。
"""

from .providers import (
    FallbackIntentProvider,
    FastJevIntentProvider,
    IntentProvider,
    LlmIntentProvider,
    RouteContext,
    rule_fast_path,
)
from .router import IntentRouter, build_router, route_auto_agent
from .schemas import (
    INTENT_COMPLEX,
    INTENT_GENERAL,
    INTENT_KB_SEARCH,
    INTENT_LABELS,
    INTENT_SINGLE_TOOL,
    INTENT_UNKNOWN,
    MCP_CANDIDATE,
    PATH_AGENT_RUN,
    PATH_DIRECT,
    PATH_LABELS,
    RouteError,
    RouteResult,
    validate_route_result,
)

__all__ = [
    "IntentProvider",
    "LlmIntentProvider",
    "FastJevIntentProvider",
    "FallbackIntentProvider",
    "RouteContext",
    "rule_fast_path",
    "IntentRouter",
    "build_router",
    "route_auto_agent",
    "INTENT_GENERAL",
    "INTENT_KB_SEARCH",
    "INTENT_SINGLE_TOOL",
    "INTENT_COMPLEX",
    "INTENT_UNKNOWN",
    "INTENT_LABELS",
    "PATH_DIRECT",
    "PATH_AGENT_RUN",
    "MCP_CANDIDATE",
    "PATH_LABELS",
    "RouteError",
    "RouteResult",
    "validate_route_result",
]
