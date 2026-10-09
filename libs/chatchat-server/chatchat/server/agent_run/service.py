"""单 Agent 任务闭环服务层（任务 011）。

把运行器接入现有对话流程：

- 复用现有工具注册表（``get_tool``）与 MCP 连接（``get_enabled_mcp_connections``
  + 本项目自带的 ``MultiServerMCPClient``），不引入第二套工具/记忆系统；
- 复用现有会话历史（``filter_message``）与长期记忆服务（``memory_service``
  的自动提取 / 会话摘要 / 记忆注入）；
- 规划器由现有 ``get_ChatOpenAI`` 构建（``LLMPlanner``），测试中以假规划器
  替换（``_build_planner``）；
- 运行轨迹持久化走独立会话（``_session_factory``），与路由注入的会话互不
  干扰；测试中可替换为临时库会话。

流式输出沿用 ``OpenAIChatOutput``（``extra="allow"`` 透传 ``agent_event`` /
``run_id`` / ``agent_plan`` / ``agent_step`` 等字段），前端无需更换客户端。
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from chatchat.server.agent_run.planner import LLMPlanner
from chatchat.server.agent_run.runtime import create_agent_runtime, runtime_name
from chatchat.server.api_server.api_schemas import OpenAIChatOutput
from chatchat.server.db.repository import agent_run_repository as agent_run_repo
from chatchat.server.db.repository import update_message
from chatchat.server.db.repository.message_repository import filter_message
from chatchat.server.memory import memory_service
from chatchat.server.utils import get_ChatOpenAI, get_default_llm, get_tool
from chatchat.settings import Settings
from chatchat.utils import build_logger

logger = build_logger()


def _session_factory() -> Session:
    """运行器/收尾持久化使用的会话工厂。

    测试中 monkeypatch 本函数以指向临时数据库。
    """
    from chatchat.server.db.session import SessionLocal

    return SessionLocal()


def _build_planner(model_name: Optional[str] = None) -> LLMPlanner:
    """用现有模型配置构建规划器（非流式）；测试中替换为假规划器。

    必须使用 ``local_wrap=False``（直连模型平台，如 ollama），不能用
    ``local_wrap=True``：后者会把 LLM 指向 chatchat 自己的 ``/v1`` 接口，
    而规划器在事件循环线程里同步调用 ``llm.invoke()``，会形成"事件循环
    阻塞等待本服务 /v1、/v1 又只能靠事件循环处理"的自引用死锁，导致
    Agent Run 永远停在"已启动"。与意图路由器（router.py 用默认
    ``local_wrap=False`` 直连 ollama）保持一致。
    """
    name = model_name or get_default_llm()
    llm = get_ChatOpenAI(
        model_name=name,
        streaming=False,
        local_wrap=False,
        max_tokens=Settings.model_settings.MAX_TOKENS,
    )
    return LLMPlanner(llm)


def _build_tools(tool_config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """按工具配置从现有注册表取工具；未知名称跳过（不报错）。"""
    tools: Dict[str, Any] = {}
    for name in (tool_config or {}):
        if tool := get_tool(name):
            tools[name] = tool
    return tools


async def _load_mcp_tools() -> Tuple[Dict[str, Any], Optional[Any]]:
    """加载启用的 MCP 工具。

    返回 ``(tools, client)``：client 在运行期间必须保持存活（工具调用依赖
    其会话），调用方负责在运行结束后关闭。
    """
    from chatchat.server.db.repository.mcp_connection_repository import (
        get_enabled_mcp_connections,
    )
    from langchain_chatchat.agents.platform_tools.base import PlatformToolsRunnable

    connections = get_enabled_mcp_connections() or []
    mcp_connections: Dict[str, Any] = {}
    for conn in connections:
        if conn["transport"] == "stdio":
            mcp_connections[conn["server_name"]] = {
                "transport": "stdio",
                "command": conn["config"].get(
                    "command", conn["args"][0] if conn["args"] else ""
                ),
                "args": conn["args"][1:] if len(conn["args"]) > 1 else [],
                "env": conn["env"],
                "encoding": "utf-8",
                "encoding_error_handler": "strict",
            }
        elif conn["transport"] == "sse":
            mcp_connections[conn["server_name"]] = {
                "transport": "sse",
                "url": conn["config"].get("url", ""),
                "headers": conn["config"].get("headers", {}),
                "timeout": conn.get("timeout", 30.0),
                "sse_read_timeout": conn.get("sse_read_timeout", 60.0),
            }
    if not mcp_connections:
        return {}, None
    client = await PlatformToolsRunnable.create_mcp_client(mcp_connections)
    tools = {t.name: t for t in client.get_tools()}
    return tools, client


async def _close_mcp_client_async(client: Optional[Any]) -> None:
    if client is None:
        return
    try:
        await client.__aexit__(None, None, None)
    except Exception:
        logger.warning("关闭 MCP 客户端失败（忽略）", exc_info=True)


# 失败/取消的 Agent 轮持久化文本（``_final_message_text`` 生成）。这类文本
# 进入规划上下文后，小模型会把"失败"当作事实复述（如直接回答"计划生成
# 失败"），甚至诱发空计划；读取历史时替换为中性占位。
_AGENT_FAILED_MARKERS = (
    "（Agent 运行失败",
    "（Agent 运行已取消）",
    "Agent 运行失败",
    "Agent 运行已取消",
)


def _neutralize_agent_result(text: Optional[str]) -> str:
    """把历史中失败/取消的 Agent 轮替换为中性占位，避免污染后续规划。"""
    t = (text or "").strip()
    if not t or not t.startswith(_AGENT_FAILED_MARKERS):
        return text or ""
    return (
        "（该次 Agent 运行失败或已取消，以上结果不可用；"
        "请只依据当前用户目标重新规划，不要复述该失败）"
    )


def _build_history(
    session: Session,
    conversation_id: str,
    owner_id: str,
    history_len: Optional[int],
) -> List[Dict[str, str]]:
    """从当前用户会话读取历史（时间正序），仅取可渲染的轮次。"""
    if session is None or not owner_id or not conversation_id:
        return []
    try:
        messages = filter_message(
            session=session,
            conversation_id=conversation_id,
            owner_id=owner_id,
            limit=history_len or Settings.model_settings.HISTORY_LEN,
        )
    except Exception:
        logger.warning("读取会话历史失败（忽略）", exc_info=True)
        return []
    history: List[Dict[str, str]] = []
    for message in reversed(messages or []):
        query = message.get("query") or ""
        response = _neutralize_agent_result(message.get("response"))
        if not query and not response:
            continue
        history.append({"role": "user", "content": query})
        history.append({"role": "assistant", "content": response or ""})
    return history


def _build_memory_messages(
    session: Session,
    owner_id: str,
    query: str,
    conversation_id: str,
    message_id: Optional[str],
) -> List[Dict[str, str]]:
    """沿用现有记忆机制：自动提取 + 会话摘要 + 记忆注入；失败不阻断运行。"""
    if session is None or not owner_id:
        return []
    try:
        memory_service.auto_extract(
            session,
            owner_id,
            query,
            source_conversation_id=conversation_id,
            source_message_id=message_id,
        )
    except Exception:
        pass
    try:
        if conversation_id:
            memory_service.summarize_conversation(session, owner_id, conversation_id)
    except Exception:
        pass
    try:
        return [
            {"role": m.get("role", "system"), "content": m.get("content", "")}
            for m in (
                memory_service.memory_system_messages(
                    session, owner_id, query, conversation_id
                )
                or []
            )
        ]
    except Exception:
        logger.warning("读取长期记忆失败（忽略）", exc_info=True)
        return []


def _event_to_sse(ev: dict, message_id: Optional[str], model_name: str) -> str:
    """把运行器事件映射为 OpenAI 兼容的 SSE 块（额外字段经 extra 透传）。"""
    etype = ev["type"]
    content = ""
    if etype == "run_completed":
        content = ev.get("final_result") or ""
    elif etype == "run_failed":
        content = f"Agent 运行失败：{ev.get('error') or '未知原因'}"
    elif etype == "run_cancelled":
        content = "Agent 运行已取消"
    elif etype == "plan_ready" and isinstance(ev.get("plan"), dict):
        content = ev["plan"].get("summary", "")
    out = OpenAIChatOutput(
        id=f"agentrun{uuid.uuid4()}",
        object="chat.completion.chunk",
        content=content,
        model=model_name or None,
        message_id=message_id,
        class_name="AgentRunEvent",
        agent_event=etype,
        run_id=ev.get("run_id"),
        agent_plan=ev.get("plan"),
        agent_step=ev.get("step"),
        agent_steps=ev.get("steps"),
        agent_final_result=ev.get("final_result"),
        agent_error=ev.get("error"),
    )
    return out.model_dump_json()


def _final_message_text(runner: Any) -> str:
    if runner.status == "completed":
        return runner.final_result or ""
    if runner.status == "cancelled":
        return "（Agent 运行已取消）"
    return f"（Agent 运行失败：{runner.error or '未知原因'}）"


def _persist_final_message(
    message_id: Optional[str],
    owner_id: str,
    runner: Any,
) -> None:
    """运行收敛后把最终可见文本写入来源消息；失败只记日志。"""
    if not message_id or not owner_id:
        return
    fresh = _session_factory()
    try:
        update_message(
            session=fresh,
            message_id=message_id,
            owner_id=owner_id,
            response=_final_message_text(runner),
            metadata={"agent_run_id": runner.run_id, "agent_status": runner.status},
        )
    except Exception:
        logger.warning("写入 Agent 运行结果到消息失败（忽略）", exc_info=True)
    finally:
        try:
            fresh.close()
        except Exception:
            pass


def _aggregate_result(
    events: List[dict], runner: Any, conversation_id: str
) -> dict:
    """非流式模式：把事件聚合为一份运行结果。"""
    plan: Optional[dict] = None
    steps: Dict[int, dict] = {}
    for ev in events:
        if ev["type"] == "plan_ready":
            plan = ev.get("plan")
        elif ev["type"] == "step_started":
            step = ev.get("step") or {}
            steps[step.get("seq", len(steps) + 1)] = dict(step)
        elif ev["type"] == "step_finished":
            step = ev.get("step") or {}
            seq = step.get("seq")
            steps[seq if seq is not None else len(steps) + 1] = dict(step)
    run_detail = {
        "run_id": runner.run_id,
        "status": runner.status,
        "current_step": max(steps.keys()) if steps else 0,
        "max_steps": runner.max_steps,
        "plan": plan,
        "steps": [steps[k] for k in sorted(steps)],
        "final_result": runner.final_result,
        "error": runner.error,
    }
    model_name = runner.model_name or ""
    out = OpenAIChatOutput(
        id=f"agentrun{runner.run_id or uuid.uuid4().hex}",
        object="chat.completion",
        content=_final_message_text(runner),
        role="assistant",
        finish_reason="stop" if runner.status == "completed" else "error",
        model=model_name or None,
        message_id=runner.source_message_id,
        class_name="AgentRunResult",
        agent_event=f"run_{runner.status}",
        run_id=runner.run_id,
        agent_run=run_detail,
    )
    result = out.model_dump()
    result["conversation_id"] = conversation_id
    return result


async def agent_run_chat(
    *,
    query: str,
    conversation_id: str,
    message_id: Optional[str],
    session: Session,
    owner_id: str,
    stream: bool = True,
    tool_config: Optional[Dict[str, Any]] = None,
    use_mcp: bool = False,
    model_name: Optional[str] = None,
    history_len: Optional[int] = None,
    max_steps: Optional[int] = None,
) -> Any:
    """执行一次单 Agent 任务闭环。

    流式返回 ``EventSourceResponse``（SSE 事件带 ``agent_event`` / ``run_id``
    等额外字段）；非流式返回聚合结果 dict。所有路径收敛到运行终态，并把
    最终可见文本写回来源消息。
    """
    if not owner_id:
        raise ValueError("owner_id 不能为空（必须来自服务端认证上下文）")

    tools = _build_tools(tool_config)
    mcp_client = None
    try:
        if use_mcp:
            mcp_tools, mcp_client = await _load_mcp_tools()
            tools.update(mcp_tools)

        history = _build_history(session, conversation_id, owner_id, history_len)
        memory_messages = _build_memory_messages(
            session, owner_id, query, conversation_id, message_id
        )

        planner = _build_planner(model_name)
        # 任务 014：运行时由统一工厂创建（默认 LangGraph，legacy 仅回退）；
        # Chat API / WebUI 不感知具体运行时，差异收敛在此处。
        runner = create_agent_runtime(
            session_factory=_session_factory,
            owner_id=owner_id,
            planner=planner,
            tools=tools,
            goal=query,
            conversation_id=conversation_id,
            source_message_id=message_id,
            history=history,
            memory_messages=memory_messages,
            max_steps=max_steps,
            model_name=model_name or "",
        )

        if not stream:
            events: List[dict] = []
            async for ev in runner.run():
                events.append(ev)
            _persist_final_message(message_id, owner_id, runner)
            return _aggregate_result(events, runner, conversation_id)

        from sse_starlette import EventSourceResponse

        async def _stream() -> Any:
            try:
                async for ev in runner.run():
                    yield _event_to_sse(ev, message_id, model_name or "")
            finally:
                await _close_mcp_client_async(mcp_client)
                _persist_final_message(message_id, owner_id, runner)

        return EventSourceResponse(_stream())
    except Exception:
        await _close_mcp_client_async(mcp_client)
        raise


async def agent_run_resume(
    *,
    run_id: str,
    owner_id: str,
    model_name: Optional[str] = None,
    tool_config: Optional[Dict[str, Any]] = None,
    use_mcp: bool = False,
    stream: bool = True,
) -> Any:
    """从 checkpoint 断点恢复一个中断的运行（任务 014 §7，仅后端）。

    - ``run_id`` / ``owner_id`` 由服务端校验归属，非本人/不存在的运行
      抛 ``LookupError``（路由层转 404）；
    - 已终态运行：幂等空操作，只回报终态；
    - 恢复后从上次 checkpoint 的下一节点继续，已完成工具步骤绝不重跑；
    - 规划器/工具/历史/记忆按当前用户上下文重建（``tool_config`` 为
      ``None`` 时使用完整工具注册表）；
    - ``legacy`` 运行时（迁移期回退）不支持断点恢复，抛 ``RuntimeError``
      由路由层给出明确状态（旧记录仅可查看/取消，绝不当作新图执行）。

    流式返回 ``EventSourceResponse``；非流式返回聚合结果 dict。
    """
    if not owner_id:
        raise ValueError("owner_id 不能为空（必须来自服务端认证上下文）")
    if not run_id:
        raise ValueError("run_id 不能为空")
    if runtime_name() != "langgraph":
        raise RuntimeError(
            "当前运行时为 legacy（迁移期回退），不支持从 checkpoint 断点"
            "恢复；该运行仅可查看或取消"
        )

    session = _session_factory()
    try:
        run = agent_run_repo.get_owned_run(session, run_id, owner_id)
        if run is None:
            # 跨用户/不存在的运行：统一 LookupError（路由层转 404，
            # 与"不存在"不可区分）
            raise LookupError("运行不存在或无权访问")
        goal = run.goal or ""
        conversation_id = run.conversation_id or ""
        source_message_id = run.source_message_id
        max_steps = run.max_steps
    finally:
        session.close()

    tools: Dict[str, Any]
    if tool_config:
        tools = _build_tools(tool_config)
    else:
        # 未显式给出工具配置时恢复完整注册表（与运行当时一致的能力面）
        tools = {name: t for name, t in get_tool().items() if t}
    mcp_client = None
    try:
        if use_mcp:
            mcp_tools, mcp_client = await _load_mcp_tools()
            tools.update(mcp_tools)

        history: List[Dict[str, str]] = []
        memory_messages: List[Dict[str, str]] = []
        ctx_session = _session_factory()
        try:
            if conversation_id:
                history = _build_history(ctx_session, conversation_id, owner_id, None)
            try:
                memory_messages = [
                    {"role": m.get("role", "system"), "content": m.get("content", "")}
                    for m in (
                        memory_service.memory_system_messages(
                            ctx_session, owner_id, goal, conversation_id
                        )
                        or []
                    )
                ]
            except Exception:
                logger.warning("恢复时读取长期记忆失败（忽略）", exc_info=True)
                memory_messages = []
        finally:
            try:
                ctx_session.close()
            except Exception:
                pass

        planner = _build_planner(model_name)
        runner = create_agent_runtime(
            session_factory=_session_factory,
            owner_id=owner_id,
            planner=planner,
            tools=tools,
            goal=goal,
            conversation_id=conversation_id or None,
            source_message_id=source_message_id,
            history=history,
            memory_messages=memory_messages,
            max_steps=max_steps,
            model_name=model_name or "",
            existing_run_id=run_id,
            forced="langgraph",
        )

        if not stream:
            events: List[dict] = []
            async for ev in runner.run():
                events.append(ev)
            _persist_final_message(source_message_id, owner_id, runner)
            return _aggregate_result(events, runner, conversation_id)

        from sse_starlette import EventSourceResponse

        async def _stream() -> Any:
            try:
                async for ev in runner.run():
                    yield _event_to_sse(ev, source_message_id, model_name or "")
            finally:
                await _close_mcp_client_async(mcp_client)
                _persist_final_message(source_message_id, owner_id, runner)

        return EventSourceResponse(_stream())
    except Exception:
        await _close_mcp_client_async(mcp_client)
        raise


__all__ = [
    "agent_run_chat",
    "agent_run_resume",
    "_session_factory",
    "_build_planner",
    "_build_tools",
    "_load_mcp_tools",
]