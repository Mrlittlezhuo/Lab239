"""单 Agent 任务闭环服务层（任务 011）。

把运行器接入现有对话流程：

- 复用现有工具注册表（``get_tool``）与 MCP 连接（``mcp_connections.build_mcp_connections``
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
from collections.abc import Mapping
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from chatchat.server.agent_run.planner import LLMPlanner
from chatchat.server.agent_run.runtime import create_agent_runtime, runtime_name
from chatchat.server.api_server.api_schemas import OpenAIChatOutput
from chatchat.server.db.repository import agent_run_repository as agent_run_repo
from chatchat.server.db.repository import merge_message_metadata
from chatchat.server.memory import context_package, memory_service, usage_manifest
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
    """用现有模型配置构建规划器；测试中替换为假规划器。

    必须使用 ``local_wrap=False``（直连模型平台，如 ollama），不能用
    ``local_wrap=True``：后者会把 LLM 指向 chatchat 自己的 ``/v1`` 接口，
    而规划器同步调用模型，会形成"事件循环阻塞等待本服务 /v1、/v1 又只能
    靠事件循环处理"的自引用死锁，导致 Agent Run 永远停在"已启动"。与意图
    路由器（router.py 用默认 ``local_wrap=False`` 直连 ollama）保持一致。

    ``streaming=True`` 是为了让 ``llm.stream()`` 真的逐片返回：langchain
    在 ``streaming=False`` 时把 ``stream()`` 退化成一次性返回整段（实测
    只出 1 片），那样 Agent 任务就没有正文预览可言。解析与校验仍在收完
    之后做，流式只改变"什么时候能看到字"。
    """
    name = model_name or get_default_llm()
    llm = get_ChatOpenAI(
        model_name=name,
        streaming=True,
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
    from chatchat.server.mcp_connections import prepare_mcp_connections
    from langchain_chatchat.agents.platform_tools.base import PlatformToolsRunnable

    # 连接字典的构造收敛在 mcp_connections（含 MCP 通用配置），两条路径共用；
    # 浏览器类 MCP 依赖的专用浏览器不在时，prepare 会先把它拉起来
    mcp_connections = await prepare_mcp_connections()
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


@asynccontextmanager
async def _mcp_session(use_mcp: bool):
    """在**消费这条运行的那个任务里**建立并关闭 MCP 连接。

    MCP 客户端 ``__aenter__`` 进入的是 anyio 的 cancel scope，只能在进入它的那个
    任务里退出。以前连接建在路由协程里、关在 ``EventSourceResponse`` 的响应体里
    （Starlette 用独立任务迭代 ``body_iterator``），收尾必炸
    ``Attempted to exit cancel scope in a different task than it was entered in``；
    非流式分支更糟：成功路径上根本没有关闭调用，每条消息留下一个常驻的 server
    子进程（浏览器类 server 还会带起一串 Chrome）。

    用法：在需要工具的地方 ``async with _mcp_session(use_mcp) as (tools, client)``，
    运行器必须在同一个 ``async with`` 里创建并使用完。
    """
    if not use_mcp:
        yield {}, None
        return
    mcp_tools, client = await _load_mcp_tools()
    try:
        yield mcp_tools, client
    finally:
        await _close_mcp_client_async(client)


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


def _history_from_messages(messages) -> List[Dict[str, str]]:
    """把最近原文窗口内的消息转成规划历史（时间正序，失败/取消轮去污）。

    与聊天链路同一份数据、同一个分界（设计方案 §18.2.2）；失败或取消的 Agent
    轮不把错误细节带给规划器，停止/中断留下的半截回答带 ``[未完成回答]`` 标记
    进历史（§11.3）。
    """
    history: List[Dict[str, str]] = []
    for message in messages or []:
        if isinstance(message, Mapping):
            query = message.get("query") or ""
            response = message.get("response")
            incomplete = False
        else:
            query = getattr(message, "query", "") or ""
            response = getattr(message, "response", None)
            incomplete = usage_manifest.is_incomplete_metadata(
                getattr(message, "meta_data", None)
            )
        body = (response or "").strip()
        if body.startswith(_AGENT_FAILED_MARKERS):
            response = _neutralize_agent_result(response)
        elif incomplete and body:
            # 半截回答可以进规划历史，但必须让模型知道它没写完（§11.3 / §18.3.9）
            response = usage_manifest.mark_incomplete(response)
        if not query and not response:
            continue
        history.append({"role": "user", "content": query})
        history.append({"role": "assistant", "content": response or ""})
    return history


def _build_history(
    session: Session,
    conversation_id: str,
    owner_id: str,
    history_len: Optional[int],
) -> List[Dict[str, str]]:
    """从当前用户会话读取历史（时间正序），仅取可渲染的轮次。

    ``history_len`` 只作为最小轮数：窗口大小由字符预算算出，与聊天链路一致
    （设计方案 §18.2.9）。
    """
    if session is None or not owner_id or not conversation_id:
        return []
    try:
        package = context_package.build_context_package(
            session,
            owner_id,
            conversation_id,
            "",
            use_memory=False,
            refresh=False,
            min_turns=history_len or Settings.model_settings.HISTORY_LEN,
        )
    except Exception:
        logger.warning("读取会话历史失败（忽略）", exc_info=True)
        return []
    return _history_from_messages(package.recent_messages)


def _auto_extract(
    session: Session,
    owner_id: str,
    query: str,
    conversation_id: str,
    message_id: Optional[str],
) -> None:
    """用户明确表达（"请记住…"）在运行开始前写入长期记忆；失败不阻断运行。

    写入侧不受"本轮不使用个人记忆"开关影响（设计方案 §12.1）：那个开关只管
    这一轮读不读，不管这一轮留不留得下。
    """
    if session is None or not owner_id:
        return
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


def _build_package(
    session: Session,
    owner_id: str,
    conversation_id: str,
    query: str,
    *,
    use_memory: bool = True,
    history_len: Optional[int] = None,
) -> context_package.ContextPackage:
    """本轮的统一上下文：最近原文窗口、记忆注入段，也是 manifest 的数据源。

    与聊天链路同一个构建器、同一条分界：摘要与槽位在构造 Prompt 之前按分界
    更新，历史不会既没进摘要也没进原文（设计方案 §6.2 / §18.2.2）。
    测试中 monkeypatch 本函数以避开数据库读取。
    """
    empty = context_package.ContextPackage(
        owner_id=owner_id or "", conversation_id=conversation_id or ""
    )
    if session is None or not owner_id or not conversation_id:
        return empty
    try:
        return context_package.build_context_package(
            session,
            owner_id,
            conversation_id,
            query,
            use_memory=use_memory,
            min_turns=history_len or Settings.model_settings.HISTORY_LEN,
        )
    except Exception:
        logger.warning("读取会话上下文失败（忽略）", exc_info=True)
        return empty


def _event_to_sse(ev: dict, message_id: Optional[str], model_name: str) -> str:
    """把运行器事件映射为 OpenAI 兼容的 SSE 块（额外字段经 extra 透传）。"""
    etype = ev["type"]
    content = ""
    if etype == "run_completed":
        content = ev.get("final_result") or ""
    elif etype in ("plan_delta", "answer_delta"):
        # 生成中的正文预览：只带本片增量。它只是显示加速，最终答案仍以
        # plan_ready / run_completed 里的结构化结果为准。
        content = ev.get("text") or ""
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


def _manifest_frame(
    manifest: Dict[str, Any], message_id: Optional[str], model_name: Optional[str]
) -> str:
    """本轮使用记录的 SSE 元数据帧。

    正文保持空串、事件类型明确，旧客户端不会把它拼进回答（设计方案 §11.4）；
    ``class_name`` 沿用 AgentRunEvent 的既有约定，前端在通用 token 兜底分支
    之前按它识别。
    """
    out = OpenAIChatOutput(
        id=f"memory{uuid.uuid4()}",
        object="chat.completion.chunk",
        content="",
        role="assistant",
        model=model_name or None,
        **usage_manifest.manifest_payload(manifest, message_id),
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
    """运行收敛后把最终可见文本写入来源消息；失败只记日志。

    **按字段合并** metadata：这条消息上还有本轮使用记录（manifest）、引用等
    键，整体覆盖会把它们抹掉（设计方案 §11.2 / §18.3.6）。
    """
    if not message_id or not owner_id:
        return
    fresh = _session_factory()
    try:
        merge_message_metadata(
            session=fresh,
            message_id=message_id,
            owner_id=owner_id,
            updates={"agent_run_id": runner.run_id, "agent_status": runner.status},
            response=_final_message_text(runner),
        )
    except Exception:
        logger.warning("写入 Agent 运行结果到消息失败（忽略）", exc_info=True)
    finally:
        try:
            fresh.close()
        except Exception:
            pass


def _runner_generation_status(status: Optional[str]) -> str:
    """运行器状态 → 生成状态（§11.1 状态机里没有 running 这种中间态）。"""
    if status == "completed":
        return "completed"
    if status == "cancelled":
        return "cancelled"
    return "failed"


def _finalize_turn(
    recorder: Optional[usage_manifest.TurnRecorder],
    message_id: Optional[str],
    owner_id: str,
    runner: Any,
) -> None:
    """运行收敛后的统一收尾：最终文本 + 生成状态 + Agent 键一次合并写入。

    完成、失败、取消、恢复四条路径都走这里（设计方案 §18.3.6/8），半截文本
    不会因为提前 return 而丢掉。
    """
    status = _runner_generation_status(getattr(runner, "status", None))
    if recorder is None:
        _persist_final_message(message_id, owner_id, runner)
        return
    recorder.finish(
        status,
        response=_final_message_text(runner),
        failure_stage=None if status == "completed" else "agent_run",
        failure_reason=None if status == "completed" else (getattr(runner, "error", None) or ""),
        extra_metadata={
            "agent_run_id": runner.run_id,
            "agent_status": runner.status,
        },
    )


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
    use_memory: bool = True,
) -> Any:
    """执行一次单 Agent 任务闭环。

    流式返回 ``EventSourceResponse``（SSE 事件带 ``agent_event`` / ``run_id``
    等额外字段）；非流式返回聚合结果 dict。所有路径收敛到运行终态，并把
    最终可见文本写回来源消息。

    ``use_memory=False`` 表示本轮不使用个人记忆（界面"本次不使用个人偏好"）。
    """
    if not owner_id:
        raise ValueError("owner_id 不能为空（必须来自服务端认证上下文）")

    base_tools = _build_tools(tool_config)
    _auto_extract(session, owner_id, query, conversation_id, message_id)
    package = _build_package(
        session,
        owner_id,
        conversation_id,
        query,
        use_memory=use_memory,
        history_len=history_len,
    )
    history = _history_from_messages(package.recent_messages)
    memory_messages = package.system_messages()
    planner = _build_planner(model_name)

    # 本轮使用记录：Agent 分支也在模型调用之前把 manifest 落库（§18.3.1-2）。
    # 流式生成器跑在响应体任务里，那时请求会话可能已经关闭，所以让它自开短
    # 会话（与 _persist_final_message 同一套做法）。
    recorder = usage_manifest.TurnRecorder(
        message_id=message_id,
        owner_id=owner_id,
        manifest=usage_manifest.build_usage_manifest(package, use_memory=use_memory),
        session_factory=_session_factory,
    )
    prepared = recorder.prepare()

    def _runner(mcp_tools: Dict[str, Any]):
        # 任务 014：运行时由统一工厂创建（默认 LangGraph，legacy 仅回退）；
        # Chat API / WebUI 不感知具体运行时，差异收敛在此处。
        return create_agent_runtime(
            session_factory=_session_factory,
            owner_id=owner_id,
            planner=planner,
            tools={**base_tools, **mcp_tools},
            goal=query,
            conversation_id=conversation_id,
            source_message_id=message_id,
            history=history,
            memory_messages=memory_messages,
            max_steps=max_steps,
            model_name=model_name or "",
        )

    if not stream:
        # 连接建在、也关在这个任务里（见 _mcp_session 注释）
        async with _mcp_session(use_mcp) as (mcp_tools, _client):
            recorder.started()
            runner = _runner(mcp_tools)
            events: List[dict] = []
            async for ev in runner.run():
                if ev["type"] in ("plan_delta", "answer_delta"):
                    continue  # 正文预览只对流式响应有意义
                events.append(ev)
            _finalize_turn(recorder, message_id, owner_id, runner)
            result = _aggregate_result(events, runner, conversation_id)
            # 非流式最终响应块携带与助手消息同源的 manifest 与最终生成状态，
            # 不另造一次查询推算（设计方案 §11.4 / §18.3.12）
            if recorder.manifest is not None:
                result[usage_manifest.MANIFEST_KEY] = recorder.manifest
                result[usage_manifest.STATUS_KEY] = recorder.manifest.get(
                    "generation_status"
                )
            return result

    from sse_starlette import EventSourceResponse

    async def _stream() -> Any:
        # runner 持有 MCP 工具的引用，所以它必须和连接建在同一个任务里 —— 也就是
        # 在响应体开始迭代时才建，不能提前到路由协程里。
        async with _mcp_session(use_mcp) as (mcp_tools, _client):
            runner = _runner(mcp_tools)
            finished = False
            try:
                if prepared is not None:
                    yield _manifest_frame(prepared, message_id, model_name)
                recorder.started()
                async for ev in runner.run():
                    yield _event_to_sse(ev, message_id, model_name or "")
                _finalize_turn(recorder, message_id, owner_id, runner)
                finished = True
                if recorder.manifest is not None:
                    # 收尾状态再发一帧：完成/失败/取消的文案要跟随最终状态
                    yield _manifest_frame(recorder.manifest, message_id, model_name)
            finally:
                # 取消与异常出口同样收敛：半截文本与状态必须落库（§18.3.6/8）
                if not finished:
                    _finalize_turn(recorder, message_id, owner_id, runner)

    return EventSourceResponse(_stream())


async def agent_run_resume(
    *,
    run_id: str,
    owner_id: str,
    model_name: Optional[str] = None,
    tool_config: Optional[Dict[str, Any]] = None,
    use_mcp: bool = False,
    stream: bool = True,
    use_memory: bool = True,
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

    base_tools: Dict[str, Any]
    if tool_config:
        base_tools = _build_tools(tool_config)
    else:
        # 未显式给出工具配置时恢复完整注册表（与运行当时一致的能力面）
        base_tools = {name: t for name, t in get_tool().items() if t}
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
                        ctx_session, owner_id, goal, conversation_id,
                        use_memory=use_memory,
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

    def _runner(mcp_tools: Dict[str, Any]):
        return create_agent_runtime(
            session_factory=_session_factory,
            owner_id=owner_id,
            planner=planner,
            tools={**base_tools, **mcp_tools},
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

    # 恢复路径**保留消息里原有的那份 manifest**（设计方案 §18.3.6）：恢复一次
    # 中断的运行不改变"当时注入了什么"，重新生成快照会把历史回答的使用记录
    # 改写成"恢复时用了什么"。这里只接手它并更新生成状态。
    recorder = usage_manifest.TurnRecorder(
        message_id=source_message_id,
        owner_id=owner_id,
        manifest=None,
        session_factory=_session_factory,
    )
    recorder.adopt_existing()
    recorder.started()

    if not stream:
        # 连接建在、也关在这个任务里（见 _mcp_session 注释）
        async with _mcp_session(use_mcp) as (mcp_tools, _client):
            runner = _runner(mcp_tools)
            events: List[dict] = []
            async for ev in runner.run():
                if ev["type"] in ("plan_delta", "answer_delta"):
                    continue  # 正文预览只对流式响应有意义
                events.append(ev)
            _finalize_turn(recorder, source_message_id, owner_id, runner)
            result = _aggregate_result(events, runner, conversation_id)
            if recorder.manifest is not None:
                result[usage_manifest.MANIFEST_KEY] = recorder.manifest
                result[usage_manifest.STATUS_KEY] = recorder.manifest.get(
                    "generation_status"
                )
            return result

    from sse_starlette import EventSourceResponse

    async def _stream() -> Any:
        # 同 agent_run_chat：连接和持有 MCP 工具的 runner 都必须建在迭代响应体
        # 的那个任务里，否则收尾会在 anyio cancel scope 处炸
        async with _mcp_session(use_mcp) as (mcp_tools, _client):
            runner = _runner(mcp_tools)
            finished = False
            try:
                if recorder.manifest is not None:
                    yield _manifest_frame(recorder.manifest, source_message_id, model_name)
                async for ev in runner.run():
                    yield _event_to_sse(ev, source_message_id, model_name or "")
                _finalize_turn(recorder, source_message_id, owner_id, runner)
                finished = True
                if recorder.manifest is not None:
                    yield _manifest_frame(recorder.manifest, source_message_id, model_name)
            finally:
                if not finished:
                    _finalize_turn(recorder, source_message_id, owner_id, runner)

    return EventSourceResponse(_stream())


__all__ = [
    "agent_run_chat",
    "agent_run_resume",
    "_session_factory",
    "_build_planner",
    "_build_tools",
    "_load_mcp_tools",
]