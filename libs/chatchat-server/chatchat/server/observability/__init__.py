"""Phoenix 本地可观测性（任务 016）。

默认关闭、幂等注册、失败安全降级；启用时通过 OpenInference 对
LangChain/LangGraph 自动埋点，并用少量手工 span 补充智能对话路由与
项目内部标识（conversation_id / message_id / user_id / execution_mode /
run_id）。详见 chatchat/server/observability/tracing.py。
"""
from chatchat.server.observability.tracing import (
    TRACER_NAME,
    finish_span,
    get_tracer,
    init_observability,
    is_observability_enabled,
    set_span_attributes,
    shutdown_observability,
    start_business_span,
)

__all__ = [
    "TRACER_NAME",
    "finish_span",
    "get_tracer",
    "init_observability",
    "is_observability_enabled",
    "set_span_attributes",
    "shutdown_observability",
    "start_business_span",
]
