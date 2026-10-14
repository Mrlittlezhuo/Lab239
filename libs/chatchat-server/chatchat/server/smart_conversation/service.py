"""智能对话编排入口（任务 015）。

把一次对话请求交给智能对话总图：意图路由 → 执行分支（直接回答 / 知识库
任务 / 单工具任务 / 多步骤 Agent）→ 统一输出。流式响应首条事件为路由 /
执行分支事件，其后透传所选分支的内容与 Agent 事件；非流式响应包含
conversation_id、route_result、execution_mode 统一字段。

任务 016：一次请求挂在 `smart_conversation` 根 span 下，可关联路由、
执行分支、Agent run_id 与自动埋点的 LLM/工具调用。可观测性关闭时
OTel API 返回 NonRecordingSpan，本模块行为完全不变；流式根 span 在
异步迭代器真实消费完成 / 异常 / 客户端取消时才结束。
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import StatusCode
from sse_starlette import EventSourceResponse

from chatchat.server.observability import (
    finish_span,
    set_span_attributes,
    start_business_span,
)
from chatchat.server.smart_conversation.graph import (
    SmartConversationState,
    get_smart_conversation_graph,
)

__all__ = ["run_smart_conversation"]


def _route_span_attributes(state: Dict[str, Any]) -> Dict[str, Any]:
    """路由与执行分支的标识类属性（不含用户内容）。"""
    route = state.get("route_result")
    attrs: Dict[str, Any] = {
        "chatchat.execution_mode": state.get("execution_mode"),
        "chatchat.execution_mode_label": state.get("execution_mode_label"),
    }
    if route is not None:
        attrs.update(
            {
                "chatchat.route.path": route.path,
                "chatchat.route.intent": route.intent,
                "chatchat.route.complexity": route.complexity,
                "chatchat.route.confidence": route.confidence,
                "chatchat.route.provider": route.provider,
                "chatchat.route.source": route.source,
                "chatchat.route.fallback_reason": route.fallback_reason,
                "chatchat.route.candidate_tools": list(route.candidate_tools),
            }
        )
    return attrs


def _agent_run_span_attributes(result: Any) -> Dict[str, Any]:
    """非流式 Agent 分支结果中的 run 标识与状态。"""
    if not isinstance(result, dict):
        return {}
    run = result.get("agent_run")
    if not isinstance(run, dict):
        return {}
    return {
        "chatchat.agent_run_id": run.get("run_id"),
        "chatchat.agent_run_status": run.get("status"),
    }


def _capture_stream_run_id(chunk: Any, span: "trace.Span") -> None:
    """从流式 SSE chunk 中捕获 Agent run_id / 终态（尽力而为，忽略解析失败）。"""
    try:
        text = chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
        if not isinstance(text, str) or "run_id" not in text:
            return
        for line in text.split("\n"):
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if not payload:
                continue
            data = json.loads(payload)
            if isinstance(data, dict) and data.get("run_id"):
                attrs = {"chatchat.agent_run_id": data["run_id"]}
                status = data.get("agent_run_status") or data.get("status")
                if data.get("agent_event") in (
                    "run_completed",
                    "run_failed",
                    "run_cancelled",
                ):
                    attrs["chatchat.agent_run_status"] = status
                set_span_attributes(span, attrs)
                return
    except Exception:
        pass


async def run_smart_conversation(
    *,
    query: str,
    conversation_id: str,
    message_id: Optional[str],
    session: Any,
    owner_id: str,
    stream: bool,
    model_name: Optional[str] = None,
    max_tokens: Optional[int] = None,
    history_len: Optional[int] = None,
    metadata: Optional[Dict[str, Any]] = None,
    chat_model_config: Optional[Dict[str, Any]] = None,
    auto_use_mcp: bool = False,
    available_tools: Optional[Dict[str, Any]] = None,
    use_memory: bool = True,
):
    """运行一次智能对话总图并返回统一输出。

    请求身份（owner_id / conversation_id / message_id / session）必须由调用方
    从服务端认证上下文注入，本函数不接受请求体中的用户身份。

    ``use_memory=False`` 表示本轮不使用个人记忆（界面"本次不使用个人偏好"），
    由各执行分支透传给记忆注入层。
    """
    graph = get_smart_conversation_graph()
    # 根 span：覆盖路由、图执行与（流式时）响应迭代器的真实消费过程。
    # 可观测性关闭时为 NonRecordingSpan，不产生任何开销或网络请求。
    root_span = start_business_span(
        "smart_conversation",
        {
            "chatchat.conversation_id": conversation_id,
            "chatchat.message_id": message_id,
            "chatchat.user_id": owner_id,
            "chatchat.model": model_name,
            "chatchat.stream": bool(stream),
        },
    )
    try:
        with trace.use_span(root_span, end_on_exit=False):
            state = await graph.ainvoke(
                SmartConversationState(
                    query=query,
                    conversation_id=conversation_id,
                    message_id=message_id,
                    owner_id=owner_id,
                    session=session,
                    stream=bool(stream),
                    model_name=model_name,
                    max_tokens=max_tokens,
                    history_len=history_len,
                    metadata=metadata or {},
                    chat_model_config=chat_model_config or {},
                    auto_use_mcp=bool(auto_use_mcp),
                    available_tools=dict(available_tools or {}),
                    use_memory=bool(use_memory),
                )
            )
    except BaseException as exc:
        finish_span(root_span, error=exc)
        raise

    set_span_attributes(root_span, _route_span_attributes(state))
    result = state.get("result") or {}

    if state.get("stream"):
        route_event = result.get("route_event")
        body = result.get("body")

        async def _wrapped():
            # 根 span 上下文覆盖整个异步迭代过程：分支内容在消费时才真正
            # 执行（Agent 事件 / LLM / 工具调用挂在根 span 下），并在正常
            # 完成、生成异常或客户端取消时才结束 span 并落状态。
            token = None
            try:
                current_ctx = trace.set_span_in_context(root_span)
                token = otel_context.attach(current_ctx)
                if route_event:
                    yield route_event
                if body is not None and hasattr(body, "body_iterator"):
                    async for chunk in body.body_iterator:
                        _capture_stream_run_id(chunk, root_span)
                        yield chunk
                finish_span(root_span, status=StatusCode.OK)
            except GeneratorExit:
                # 客户端取消 / 响应流被关闭
                finish_span(
                    root_span,
                    status=StatusCode.ERROR,
                    description="stream cancelled",
                )
                raise
            except BaseException as exc:
                finish_span(root_span, error=exc)
                raise
            finally:
                if token is not None:
                    try:
                        otel_context.detach(token)
                    except Exception:
                        pass

        return EventSourceResponse(_wrapped())

    set_span_attributes(root_span, _agent_run_span_attributes(result))
    finish_span(root_span, status=StatusCode.OK)
    return result
