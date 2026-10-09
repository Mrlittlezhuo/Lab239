"""智能对话总图（任务 015）。

一次智能对话请求的真实 LangGraph 状态图，统一路由与分发：

    route
      ├─ direct_answer      （复用现有普通聊天流程，不创建 AgentRun）
      ├─ knowledge_task     （一步 Agent Run，只提供知识库检索工具）
      ├─ single_tool_task   （一步 Agent Run，只提供路由选中的单个有效工具）
      └─ multi_step_agent   （复用任务 014 的多步骤 LangGraph Agent Runtime）
              ↓
        normalize_result

总图是一次请求的短生命周期编排层：不创建第二套持久 checkpoint，需要恢复的
多步骤执行仍由任务 014 的 Agent Runtime checkpoint 负责。路由 / 执行准备阶段
的失败在工具执行开始之前可控回退为直接回答；工具或 Agent 已经开始后的失败
返回明确失败状态，不再切换路径，避免工具重复执行。

任务 016 扩展点：未来"论文研究"分支只需新增一个执行节点、在
``_resolve_execution`` 增加一条映射，并复用 ``normalize_result`` 统一输出；
本研究业务不在本任务实现。
"""
from __future__ import annotations

from typing import Any, Dict, Optional, TypedDict

from langgraph.graph import END, START, StateGraph
from opentelemetry.trace import StatusCode

# 任务 016：手工业务 span（可观测性关闭时 OTel API 自动退化为 no-op）
from chatchat.server.observability import (
    finish_span,
    set_span_attributes,
    start_business_span,
)

# 复用现有模块；分支节点在调用时通过模块属性解析可调用对象（``chat``、
# ``agent_run_chat``、``route_auto_agent``、``get_tool_config``），保证测试
# 对 chat_routes / intent_router / agent_run.service 命名空间的打桩生效。
import chatchat.server.agent_run.service as agent_service
import chatchat.server.api_server.chat_routes as chat_routes_mod
import chatchat.server.intent_router as intent_router_pkg
from chatchat.server.api_server.openai_routes import OpenAIChatOutput
from chatchat.server.intent_router.schemas import (
    COMPLEXITY_DIRECT,
    COMPLEXITY_MULTI,
    COMPLEXITY_SINGLE,
    INTENT_COMPLEX,
    INTENT_GENERAL,
    INTENT_KB_SEARCH,
    INTENT_SINGLE_TOOL,
    INTENT_UNKNOWN,
    PATH_AGENT_RUN,
    PATH_DIRECT,
    RouteResult,
)
import uuid

# ---------------------------------------------------------------------------
# 执行分支常量（兼容扩展字段 execution_mode / execution_mode_label，
# 不改动任务 012 RouteResult 的 path / intent / complexity / candidate_tools）
# ---------------------------------------------------------------------------

EXEC_DIRECT_ANSWER = "direct_answer"
EXEC_KNOWLEDGE_TASK = "knowledge_task"
EXEC_SINGLE_TOOL_TASK = "single_tool_task"
EXEC_MULTI_STEP_AGENT = "multi_step_agent"

EXECUTION_MODE_LABELS = {
    EXEC_DIRECT_ANSWER: "直接回答",
    EXEC_KNOWLEDGE_TASK: "知识库任务",
    EXEC_SINGLE_TOOL_TASK: "单工具任务",
    EXEC_MULTI_STEP_AGENT: "多步骤 Agent",
}

# 一步 Agent 任务的工具步数上限（知识库 / 单工具任务最多执行一个工具步骤）
ONE_STEP_MAX_STEPS = 1

# 知识库任务固定使用的现有公共知识库检索工具
KB_TOOL_NAME = "search_local_knowledgebase"


class SmartConversationState(TypedDict, total=False):
    """智能对话总图状态。

    请求身份（owner_id / conversation_id / message_id / session）一律来自
    服务端认证上下文，由 Chat API 分发层在调用前注入。
    """

    # —— 请求标识与参数 ——
    query: str
    conversation_id: str
    message_id: Optional[str]
    owner_id: str
    session: Any
    stream: bool
    model_name: Optional[str]
    max_tokens: Optional[int]
    history_len: Optional[int]
    metadata: Dict[str, Any]
    chat_model_config: Dict[str, Any]
    auto_use_mcp: bool
    available_tools: Dict[str, Any]
    # —— 路由与执行分支（route 节点写入） ——
    route_result: Optional[RouteResult]
    execution_mode: str
    execution_mode_label: str
    # —— 分支结果与统一输出（分支节点 / normalize_result 写入） ——
    branch_result: Any
    result: Any


# ---------------------------------------------------------------------------
# route 节点：调用一次现有意图路由，并确定实际执行分支
# ---------------------------------------------------------------------------


def _demote(route: RouteResult, reason: str) -> RouteResult:
    """把 agent_run 路由结果受控降级为直接回答（保留原意图与置信度）。

    与任务 012 IntentRouter 的低置信度降级形状保持一致：path 改为 direct、
    source 标记 fallback、fallback_reason 说明降级原因。
    """
    return RouteResult(
        path=PATH_DIRECT,
        intent=route.intent,
        complexity=COMPLEXITY_DIRECT,
        candidate_tools=[],
        confidence=route.confidence,
        summary="满足回退条件，已按普通对话回答",
        provider=route.provider,
        source="fallback",
        fallback_reason=reason,
    )


def _resolve_execution(route: RouteResult, available_tools: Dict[str, Any]):
    """按路由结果确定实际执行分支；工具执行开始前的受控回退都发生在这里。

    映射（任务 015 §5.1）：
    - general / unknown / 统一回退（path=direct）→ direct_answer；
    - kb_search → knowledge_task（要求知识库检索工具仍在注册表中，否则回退）；
    - single_tool 或 single → single_tool_task（取首个有效候选工具）；
    - complex 或 multi → multi_step_agent（保留全部有效候选工具）；
    - agent_run 但意图不一致 / 无有效候选工具 → 回退 direct_answer。
    """
    available = available_tools or {}
    if route.path != PATH_AGENT_RUN:
        return route, EXEC_DIRECT_ANSWER

    # 意图为 general / unknown 的 agent_run 路由不一致（普通意图只应走直接
    # 回答），受控回退。
    if route.intent in (INTENT_GENERAL, INTENT_UNKNOWN):
        return (
            _demote(route, "路由结果不一致（意图不匹配 Agent 路径）"),
            EXEC_DIRECT_ANSWER,
        )

    if route.intent == INTENT_KB_SEARCH:
        if KB_TOOL_NAME in available:
            return route, EXEC_KNOWLEDGE_TASK
        return (
            _demote(route, f"知识库检索工具不可用：{KB_TOOL_NAME}"),
            EXEC_DIRECT_ANSWER,
        )

    valid = [t for t in route.candidate_tools if t in available]
    if not valid:
        return _demote(route, "无可用候选工具"), EXEC_DIRECT_ANSWER

    if route.intent == INTENT_SINGLE_TOOL or route.complexity == COMPLEXITY_SINGLE:
        return route, EXEC_SINGLE_TOOL_TASK
    if route.intent == INTENT_COMPLEX or route.complexity == COMPLEXITY_MULTI:
        return route, EXEC_MULTI_STEP_AGENT

    # agent_run 但意图与复杂度均无法识别 → 受控回退
    return _demote(route, "路由结果不一致（意图无法识别）"), EXEC_DIRECT_ANSWER


async def route_node(state: SmartConversationState) -> dict:
    """调用一次现有意图路由，保存校验后的 RouteResult 与实际执行分支。

    任务 016：路由过程挂在 `intent_route` span 下（意图、置信度、实际
    分支与回退原因等标识属性）；可观测性关闭时为 no-op。
    """
    route_span = start_business_span("intent_route")
    try:
        # route_auto_agent 为同步接口（内部自带线程池与超时），永不抛出异常；
        # 这里仍保留兜底，防御性处理外部实现变化。
        route_fn = intent_router_pkg.route_auto_agent
        try:
            route = route_fn(
                state["query"],
                available_tools=state.get("available_tools") or {},
                use_mcp=bool(state.get("auto_use_mcp", False)),
            )
        except Exception as exc:  # pragma: no cover - 防御分支
            route = _fallback_route(f"路由意外异常：{exc}")

        if not isinstance(route, RouteResult):
            route = _fallback_route("路由结果非法")

        route, mode = _resolve_execution(
            route, state.get("available_tools") or {}
        )
        set_span_attributes(
            route_span,
            {
                "chatchat.route.path": route.path,
                "chatchat.route.intent": route.intent,
                "chatchat.route.complexity": route.complexity,
                "chatchat.route.confidence": route.confidence,
                "chatchat.route.provider": route.provider,
                "chatchat.route.source": route.source,
                "chatchat.route.fallback_reason": route.fallback_reason,
                "chatchat.route.candidate_tools": list(route.candidate_tools),
                "chatchat.execution_mode": mode,
            },
        )
        finish_span(route_span, status=StatusCode.OK)
    except BaseException as exc:
        finish_span(route_span, error=exc)
        raise
    return {
        "route_result": route,
        "execution_mode": mode,
        "execution_mode_label": EXECUTION_MODE_LABELS[mode],
    }


def _fallback_route(reason: str) -> RouteResult:
    """路由调用本身失败时的兜底结果（与 IntentRouter._fallback_result 一致）。"""
    return RouteResult(
        path=PATH_DIRECT,
        intent=INTENT_GENERAL,
        complexity=COMPLEXITY_DIRECT,
        candidate_tools=[],
        confidence=None,
        summary="自动判断未通过，已按普通对话回答",
        provider=None,
        source="fallback",
        fallback_reason=reason,
    )


# ---------------------------------------------------------------------------
# 执行分支节点
# ---------------------------------------------------------------------------


def _tool_config_for(tool_names) -> Dict[str, Any]:
    """按工具名从现有工具配置装配 tool_config（保持与 012 相同的形状）。"""
    get_config = chat_routes_mod.get_tool_config
    return {name: get_config(name) for name in tool_names}


async def direct_answer_node(state: SmartConversationState) -> dict:
    """直接回答：复用现有普通聊天流程，不装配工具、不创建 AgentRun。"""
    result = await chat_routes_mod.chat(
        query=state["query"],
        metadata=state.get("metadata") or {},
        conversation_id=state["conversation_id"],
        message_id=state.get("message_id"),
        history_len=state.get("history_len"),
        stream=bool(state.get("stream")),
        chat_model_config=state.get("chat_model_config") or {},
        tool_config={},
        use_mcp=False,
        max_tokens=state.get("max_tokens"),
        session=state["session"],
        owner_id=state["owner_id"],
    )
    return {"branch_result": result}


async def knowledge_task_node(state: SmartConversationState) -> dict:
    """知识库任务：只向 Agent Runtime 提供知识库检索工具，限制为一步工具任务。"""
    # route 节点已校验 KB_TOOL_NAME 在当前工具注册表中可用。
    result = await agent_service.agent_run_chat(
        query=state["query"],
        conversation_id=state["conversation_id"],
        message_id=state.get("message_id"),
        session=state["session"],
        owner_id=state["owner_id"],
        stream=bool(state.get("stream")),
        tool_config=_tool_config_for([KB_TOOL_NAME]),
        use_mcp=False,
        model_name=state.get("model_name"),
        history_len=state.get("history_len"),
        max_steps=ONE_STEP_MAX_STEPS,
    )
    return {"branch_result": result}


async def single_tool_task_node(state: SmartConversationState) -> dict:
    """单工具任务：只提供路由选中的单个有效工具，限制为一步工具任务。"""
    route: RouteResult = state["route_result"]
    available = state.get("available_tools") or {}
    valid = [t for t in route.candidate_tools if t in available]
    if not valid:
        raise ValueError("单工具任务无可用候选工具")
    tool_name = valid[0]
    result = await agent_service.agent_run_chat(
        query=state["query"],
        conversation_id=state["conversation_id"],
        message_id=state.get("message_id"),
        session=state["session"],
        owner_id=state["owner_id"],
        stream=bool(state.get("stream")),
        tool_config=_tool_config_for([tool_name]),
        use_mcp=False,
        model_name=state.get("model_name"),
        history_len=state.get("history_len"),
        max_steps=ONE_STEP_MAX_STEPS,
    )
    return {"branch_result": result}


async def multi_step_agent_node(state: SmartConversationState) -> dict:
    """多步骤任务：复用任务 014 的默认 LangGraph Agent Runtime（含正常步数
    限制、重规划、取消、checkpoint 与恢复）。"""
    route: RouteResult = state["route_result"]
    available = state.get("available_tools") or {}
    valid = [t for t in route.candidate_tools if t in available]
    if not valid:
        raise ValueError("多步骤任务无可用候选工具")
    result = await agent_service.agent_run_chat(
        query=state["query"],
        conversation_id=state["conversation_id"],
        message_id=state.get("message_id"),
        session=state["session"],
        owner_id=state["owner_id"],
        stream=bool(state.get("stream")),
        tool_config=_tool_config_for(valid),
        use_mcp=bool(state.get("auto_use_mcp", False)),
        model_name=state.get("model_name"),
        history_len=state.get("history_len"),
    )
    return {"branch_result": result}


# ---------------------------------------------------------------------------
# normalize_result 节点：统一路由信息、执行路径、会话 ID 与错误语义
# ---------------------------------------------------------------------------


async def normalize_result_node(state: SmartConversationState) -> dict:
    route: RouteResult = state["route_result"]
    route_dict = route.to_dict()
    mode = state["execution_mode"]
    mode_label = EXECUTION_MODE_LABELS.get(mode, mode)
    branch = state["branch_result"]

    if state.get("stream"):
        # 流式：首条事件为路由 / 执行分支（RouteResult），其后由调用方
        # （run_smart_conversation）透传所选分支的内容与 Agent 事件。
        route_event = OpenAIChatOutput(
            id=f"route{uuid.uuid4().hex}",
            object="chat.completion.chunk",
            content="",
            role="assistant",
            class_name="RouteResult",
            route_result=route_dict,
            execution_mode=mode,
            execution_mode_label=mode_label,
            conversation_id=state["conversation_id"],
        ).model_dump_json()
        return {"result": {"route_event": route_event, "body": branch}}

    # 非流式：在分支结果上补齐统一字段（Agent 分支保留 agent_run / plan /
    # steps，直接回答分支保留现有聊天结果结构）。
    payload = branch.model_dump() if hasattr(branch, "model_dump") else dict(branch)
    payload["conversation_id"] = state["conversation_id"]
    payload["route_result"] = route_dict
    payload["execution_mode"] = mode
    payload["execution_mode_label"] = mode_label
    return {"result": payload}


# ---------------------------------------------------------------------------
# 图构建
# ---------------------------------------------------------------------------


def _choose_branch(state: SmartConversationState) -> str:
    """条件边：按 route 节点确定的 execution_mode 进入对应执行分支。"""
    return state["execution_mode"]


def build_smart_conversation_graph():
    """构建并编译智能对话总图。

    编译时不挂载 checkpointer：总图本身是一次请求的短生命周期编排层，
    不新增持久 checkpoint；多步骤执行的恢复由任务 014 的 Agent Runtime
    checkpoint 负责。
    """
    builder = StateGraph(SmartConversationState)
    builder.add_node("route", route_node)
    builder.add_node("direct_answer", direct_answer_node)
    builder.add_node("knowledge_task", knowledge_task_node)
    builder.add_node("single_tool_task", single_tool_task_node)
    builder.add_node("multi_step_agent", multi_step_agent_node)
    builder.add_node("normalize_result", normalize_result_node)

    builder.add_edge(START, "route")
    builder.add_conditional_edges(
        "route",
        _choose_branch,
        {
            EXEC_DIRECT_ANSWER: "direct_answer",
            EXEC_KNOWLEDGE_TASK: "knowledge_task",
            EXEC_SINGLE_TOOL_TASK: "single_tool_task",
            EXEC_MULTI_STEP_AGENT: "multi_step_agent",
        },
    )
    for branch in (
        "direct_answer",
        "knowledge_task",
        "single_tool_task",
        "multi_step_agent",
    ):
        builder.add_edge(branch, "normalize_result")
    builder.add_edge("normalize_result", END)
    return builder.compile()


_smart_conversation_graph = None


def get_smart_conversation_graph():
    """返回进程内复用的已编译总图（图结构不可变，可安全共享）。"""
    global _smart_conversation_graph
    if _smart_conversation_graph is None:
        _smart_conversation_graph = build_smart_conversation_graph()
    return _smart_conversation_graph
