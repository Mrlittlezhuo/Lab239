import asyncio
import json
import uuid
import os
from chatchat.server.db.repository.message_repository import filter_message
from typing import AsyncIterable, List, Union, Tuple
from sqlalchemy.orm import Session
from langchain_core.load import dumpd, dumps, load, loads

from fastapi import Body, HTTPException
from langchain_classic.chains import LLMChain
from langchain_core.prompts.chat import ChatPromptTemplate

from chatchat.server.agents_registry.agents_registry import agents_registry
from sse_starlette.sse import EventSourceResponse

from chatchat.server.db.repository.mcp_connection_repository import get_enabled_mcp_connections
from chatchat.settings import Settings
from chatchat.server.api_server.api_schemas import OpenAIChatOutput
from langchain_chatchat.callbacks.agent_callback_handler import (
    AgentExecutorAsyncIteratorCallbackHandler,
    AgentStatus,
)
from langchain_chatchat.agents.platform_tools import PlatformToolsAction, PlatformToolsFinish, \
    PlatformToolsActionToolStart, PlatformToolsActionToolEnd, PlatformToolsLLMStatus
from chatchat.server.chat.utils import History
from chatchat.server.db.repository import add_message, update_message

from langchain_core.agents import AgentAction, AgentFinish
from langchain_classic.agents.output_parsers.tools import ToolAgentAction
from langchain_chatchat import ChatPlatformAI, PlatformToolsRunnable
from langchain_chatchat.agents.output_parsers.platform_knowledge_output_parsers import (
    MCPToolAction,
)
from langchain_chatchat.agents.output_parsers.tools_output.code_interpreter import (
    CodeInterpreterAgentAction,
)
from langchain_chatchat.agents.output_parsers.tools_output.drawing_tool import (
    DrawingToolAgentAction,
)
from langchain_chatchat.agents.output_parsers.tools_output.web_browser import (
    WebBrowserAgentAction,
)
from langchain_chatchat.agent_toolkits.all_tools.code_interpreter_tool import (
    CodeInterpreterToolOutput,
)
from langchain_chatchat.agent_toolkits.all_tools.drawing_tool import DrawingToolOutput
from langchain_chatchat.agent_toolkits.all_tools.tool import BaseToolOutput
from langchain_chatchat.agent_toolkits.all_tools.web_browser_tool import (
    WebBrowserToolOutput,
)
from chatchat.server.memory import memory_service
from chatchat.server.utils import (
    MsgType,
    get_ChatOpenAI,
    get_prompt_template,
    get_tool,
    wrap_done,
    get_default_llm,
    build_logger,
    get_ChatPlatformAIParams
)

logger = build_logger()


# intermediate_steps 里可能出现的类，反序列化白名单。
#
# `loads` 的 `valid_namespaces` 只决定"能不能 import 这个命名空间"，真正决定放行
# 哪个类的是 `allowed_objects`；它的默认值 'core' 只认 langchain-core 自己的类，
# 于是本项目自己的 MCPToolAction / *ToolOutput 会被直接拒掉（ValueError），
# 上一轮的工具上下文每轮都丢 —— 手动模式第二轮永远从零开始重跑工具。
#
# 不用 allowed_objects='all'：manifest 存在库里，'all' 会放行带 base_url/headers 的
# 模型类，等于给反序列化开 SSRF。新增工具时如果 observation/action 换了类，要同步
# 加进这里，否则会退回"解析失败 → 忽略"的老行为。
_STEP_CLASSES = [
    # core
    AgentAction,
    AgentFinish,
    ToolAgentAction,
    # 本项目自定义 action
    MCPToolAction,
    WebBrowserAgentAction,
    CodeInterpreterAgentAction,
    DrawingToolAgentAction,
    # 本项目自定义 observation 包装
    BaseToolOutput,
    WebBrowserToolOutput,
    CodeInterpreterToolOutput,
    DrawingToolOutput,
]


def _load_intermediate_steps(last_metadata):
    """解析上一轮持久化的工具上下文（intermediate_steps）。

    上一轮的 metadata 可能没有 intermediate_steps（例如 Agent Run 轮只写
    agent_run_id/agent_status，kb 轮只写引用元数据，普通轮在流被中断时
    只有默认空 metadata），此时不能把 None 传给 loads，否则下一轮会抛
    "the JSON object must be str, bytes or bytearray, not NoneType"
    导致整轮回答失败。解析失败同样降级为空，不阻断回答。
    """
    raw = last_metadata.get("intermediate_steps") if isinstance(last_metadata, dict) else None
    if not raw:
        return []
    try:
        return loads(
            raw,
            allowed_objects=_STEP_CLASSES,
            valid_namespaces=["langchain_chatchat", "agent_toolkits", "all_tools", "tool"],
        )
    except Exception:
        logger.warning(
            "解析上一轮 intermediate_steps 失败（忽略，不携带工具上下文）",
            exc_info=True,
        )
        return []


def create_models_from_config(configs, callbacks, stream, max_tokens):
    configs = configs or Settings.model_settings.LLM_MODEL_CONFIG
    models = {}
    prompts = {}
    for model_type, params in configs.items():
        model_name = params.get("model", "").strip() or get_default_llm()
        callbacks = callbacks if params.get("callbacks", False) else None
        # 判断是否传入 max_tokens 的值, 如果传入就按传入的赋值(api 调用且赋值), 如果没有传入则按照初始化配置赋值(ui 调用或 api 调用未赋值)
        max_tokens_value = max_tokens if max_tokens is not None else params.get("max_tokens", 1000)
        if model_type == "action_model":

            llm_params = get_ChatPlatformAIParams(
                model_name=model_name,
                temperature=params.get("temperature", 0.5),
                max_tokens=max_tokens_value,
            )
            model_instance = ChatPlatformAI(**llm_params)
        else:
            model_instance = get_ChatOpenAI(
                model_name=model_name,
                temperature=params.get("temperature", 0.5),
                max_tokens=max_tokens_value,
                callbacks=callbacks,
                streaming=stream,
                local_wrap=True,
            )
        models[model_type] = model_instance
        prompt_name = params.get("prompt_name", "default")
        prompt_template = get_prompt_template(type=model_type, name=prompt_name)
        prompts[model_type] = prompt_template
    return models, prompts


def build_mcp_connections() -> dict:
    """把库里启用的 MCP 连接转成 ``MultiServerMCPClient`` 需要的连接字典。

    单独拆出来是因为连接必须在消费流的那个协程里建立、也必须在同一个任务里关闭
    （anyio 的 cancel scope 只能在进入它的那个任务里退出）。以前连接是在
    ``create_agent_executor`` 内部用 ``run_until_complete`` 建的，建完就把 client
    丢了：关不掉，每条消息留下一个常驻的 MCP 子进程。
    """
    connections = get_enabled_mcp_connections() or []

    # 转换为MCP连接格式，支持StdioConnection和SSEConnection类型
    mcp_connections = {}
    for conn in connections:
        if conn["transport"] == "stdio":
            # StdioConnection类型
            mcp_connections[conn["server_name"]] = {
                "transport": "stdio",
                "command": conn["config"].get("command", conn["args"][0] if conn["args"] else ""),
                "args": conn["args"][1:] if len(conn["args"]) > 1 else [],
                "env": conn["env"],
                "encoding": "utf-8",
                "encoding_error_handler": "strict"
            }
        elif conn["transport"] == "sse":
            # SSEConnection类型
            mcp_connections[conn["server_name"]] = {
                "transport": "sse",
                "url": conn["config"].get("url", ""),
                "headers": conn["config"].get("headers", {}),
                "timeout": conn.get("timeout", 30.0),
                "sse_read_timeout": conn.get("sse_read_timeout", 60.0)
            }
    return mcp_connections


def create_models_chains(
    history_len, prompts, models, tools, callbacks, conversation_id,
    session, owner_id, metadata, use_mcp: bool = False, query: str = "",
    mcp_client=None
):

    # 从数据库获取conversation_id对应的 intermediate_steps 、 mcp_connections
    # （仅当会话属于 owner_id 时返回历史）
    messages = filter_message(
        session=session,
        conversation_id=conversation_id,
        owner_id=owner_id,
        limit=history_len,
    )
    # 返回的记录按时间倒序，转为正序
    messages = list(reversed(messages))
    history: List[Union[List, Tuple]] = []
    # 会话摘要：历史超过阈值时增量压缩较早内容（默认基于规则，不依赖模型），
    # 记录覆盖位置，避免每轮重复总结全部历史。失败不阻断正常回答。
    try:
        if session is not None and owner_id and conversation_id:
            memory_service.summarize_conversation(session, owner_id, conversation_id)
    except Exception:
        pass
    # 用户记忆注入：长期记忆段 + 较早内容摘要段，作为独立系统消息置于近期
    # 轮次之前（当前用户本轮明确表达优先于记忆）。失败/无会话时返回空列表，
    # 不改变无记忆时的原有行为，也不阻断正常回答。
    memory_msgs = memory_service.memory_system_messages(
        session, owner_id, query, conversation_id
    )
    for message in messages:
        history.append({"role": "user", "content": message["query"]})
        history.append({"role": "assistant", "content":  message["response"]})
    for msg in reversed(memory_msgs):
        # 置于历史最前，作为独立、清晰的记忆/摘要段
        history.insert(0, dict(msg))

    # 上一轮 metadata 缺少 intermediate_steps 时安全降级为空（见
    # _load_intermediate_steps 注释），不再因 loads(None) 崩溃。
    intermediate_steps = _load_intermediate_steps(
        messages[-1].get("metadata") if messages else None
    )
    llm = models["action_model"]
    llm.callbacks = callbacks

    agent_executor = PlatformToolsRunnable.create_agent_executor(
        agent_type="platform-knowledge-mode",
        agents_registry=agents_registry,
        llm=llm,
        tools=tools,
        history=history,
        intermediate_steps=intermediate_steps,
        # 传了 mcp_client 就只取工具，连接的生命周期由调用方（同一个任务）负责；
        # 没传才走遗留的自建路径（会漏子进程，仅集成测试用）。
        mcp_connections=build_mcp_connections() if (use_mcp and mcp_client is None) else {},
        mcp_client=mcp_client,
    )

    full_chain = {"chat_input": lambda x: x["input"]} | agent_executor

    return full_chain, agent_executor


async def chat(
        query: str = Body(..., description="用户输入", examples=["恼羞成怒"]),
        metadata: dict = Body({}, description="附件，可能是图像或者其他功能", examples=[]),
        conversation_id: str = Body("", description="对话框ID"),
        message_id: str = Body(None, description="数据库消息ID"),
        history_len: int = Body(-1, description="从数据库中取历史消息的数量"),
        stream: bool = Body(True, description="流式输出"),
        chat_model_config: dict = Body({}, description="LLM 模型配置", examples=[]),
        tool_config: dict = Body({}, description="工具配置", examples=[]),
        use_mcp: bool = Body(False, description="使用MCP"),
        max_tokens: int = Body(None, description="LLM最大token数配置", examples=[4096]),
        session: Session = None,
        owner_id: str = None,
):
    """Agent 对话

    ``session`` / ``owner_id`` 由受保护的路由注入（已验证当前用户），
    聊天内所有读写都限定在 owner 自己的会话中。
    """

    async def chat_iterator_event() -> AsyncIterable[OpenAIChatOutput]:
        current_message_id = message_id
        mcp_client = None
        try:
            callbacks = []

            # Enable langchain-chatchat to support langfuse
            import os

            langfuse_secret_key = os.environ.get("LANGFUSE_SECRET_KEY")
            langfuse_public_key = os.environ.get("LANGFUSE_PUBLIC_KEY")
            langfuse_host = os.environ.get("LANGFUSE_HOST")
            if langfuse_secret_key and langfuse_public_key and langfuse_host:
                from langfuse import Langfuse
                from langfuse.callback import CallbackHandler

                langfuse_handler = CallbackHandler()
                callbacks.append(langfuse_handler)

            models, prompts = create_models_from_config(
                callbacks=callbacks, configs=chat_model_config, stream=stream, max_tokens=max_tokens
            )
            all_tools = get_tool().values()
            tools = [tool for tool in all_tools if tool.name in tool_config]
            tools = [t.copy(update={"callbacks": callbacks}) for t in tools]
            # OpenAI 兼容路由会提前创建消息；直接调用 chat() 时在这里补建，
            # 这样自动提取的记忆始终可以定位到来源会话和消息。
            if current_message_id is None:
                current_message_id = add_message(
                    session=session,
                    owner_id=owner_id,
                    chat_type="llm_chat",
                    query=query,
                    conversation_id=conversation_id,
                )
            # 自动记忆提取：在本轮构造记忆上下文前，把用户明确表达
            # （如"请记住..."）写入长期记忆。失败不阻断正常回答。
            try:
                if session is not None and owner_id:
                    memory_service.auto_extract(
                        session,
                        owner_id,
                        query,
                        source_conversation_id=conversation_id,
                        source_message_id=current_message_id,
                    )
            except Exception:
                pass
            if use_mcp:
                mcp_connections = build_mcp_connections()
                if mcp_connections:
                    # 连接必须建在消费这条流的同一个任务里，收尾也在同一个任务里
                    # 关掉；换任务关会在 anyio 的 cancel scope 处炸
                    # "Attempted to exit cancel scope in a different task"。
                    mcp_client = await PlatformToolsRunnable.create_mcp_client(
                        mcp_connections)
            full_chain, agent_executor = create_models_chains(
                prompts=prompts,
                models=models,
                conversation_id=conversation_id,
                tools=tools,
                callbacks=callbacks,
                history_len=history_len,
                session=session,
                owner_id=owner_id,
                metadata=metadata,
                use_mcp = use_mcp,
                query=query,
                mcp_client=mcp_client,
            )
            chat_iterator = full_chain.invoke({
                "input": query
            })
            last_tool = {}
            async for item in chat_iterator:

                data = {}

                data["status"] = item.status
                data["tool_calls"] = []
                data["message_type"] = MsgType.TEXT
                if isinstance(item, PlatformToolsAction):
                    logger.info("PlatformToolsAction:" + str(item.to_json()))
                    data["text"] = item.log
                    tool_call = {
                        "index": 0,
                        "id": item.run_id,
                        "type": "function",
                        "function": {
                            "name": item.tool,
                            "arguments": item.tool_input,
                        },
                        "tool_output": None,
                        "is_error": False,
                    }
                    data["tool_calls"].append(tool_call)

                elif isinstance(item, PlatformToolsFinish):
                    data["text"] = item.log

                    # Agent 失败收尾时 return_values 里没有 "output"（也可能整轮没起过
                    # 工具，last_tool 还是 {}）：直接下标取会抛 KeyError('output')，把
                    # 真正的失败原因盖成一个看不懂的键名；空 last_tool 还会往 tool_calls
                    # 里塞一条没有 function/id 的脏记录。
                    finish_output = item.return_values.get("output")
                    if last_tool:
                        last_tool.update(tool_output=finish_output)
                        data["tool_calls"].append(last_tool)

                    try:
                        tool_output = json.loads(finish_output)
                        if message_type := tool_output.get("message_type"):
                            data["message_type"] = message_type
                    except:
                        ...

                elif isinstance(item, PlatformToolsActionToolStart):
                    logger.info("PlatformToolsActionToolStart:" + str(item.to_json()))

                    last_tool = {
                        "index": 0,
                        "id": item.run_id,
                        "type": "function",
                        "function": {
                            "name": item.tool,
                            "arguments": item.tool_input,
                        },
                        "tool_output": None,
                        "is_error": False,
                    }
                    data["tool_calls"].append(last_tool)

                elif isinstance(item, PlatformToolsActionToolEnd):
                    logger.info("PlatformToolsActionToolEnd:" + str(item.to_json()))
                    last_tool.update(
                        tool_output=item.tool_output,
                        is_error=False,
                    )
                    data["tool_calls"] = [last_tool]

                    last_tool = {}
                    try:
                        tool_output = json.loads(item.tool_output)
                        if message_type := tool_output.get("message_type"):
                            data["message_type"] = message_type
                    except:
                        ...
                elif isinstance(item, PlatformToolsLLMStatus):

                    data["text"] = item.text

                ret = OpenAIChatOutput(
                    id=f"chat{uuid.uuid4()}",
                    object="chat.completion.chunk",
                    content=data.get("text", ""),
                    role="assistant",
                    tool_calls=data["tool_calls"],
                    model=models["llm_model"].model_name,
                    status=data["status"],
                    message_type=data["message_type"],
                    message_id=current_message_id,
                    class_name=item.class_name()
                )
                yield ret.model_dump_json()

            string_intermediate_steps = dumps(agent_executor.intermediate_steps, pretty=True)

            update_message(
                session=session,
                message_id=current_message_id,
                owner_id=owner_id,
                response=agent_executor.history[-1].get("content"),
                metadata = {
                    "intermediate_steps": string_intermediate_steps
                }
            )
             
        except asyncio.exceptions.CancelledError:
            logger.warning("streaming progress has been interrupted by user.")
            return
        except Exception as e:
            logger.error(f"error in chat: {e}")
            # 与正常 chunk 保持一致，统一输出 JSON 字符串（之前输出 dict，
            # SSE 与非流式 json.loads 两种消费方式都无法正确处理）。
            yield json.dumps({"error": str(e)}, ensure_ascii=False)
            return
        finally:
            # 每条消息一套 MCP 连接：不关就是每轮常驻一个 server 子进程
            # （浏览器类 server 还会带起一整串 Chrome）。放在 finally 才能覆盖
            # 正常收尾、异常、客户端断流（GeneratorExit/CancelledError）三种出口。
            if mcp_client is not None:
                try:
                    await mcp_client.__aexit__(None, None, None)
                except Exception:
                    logger.warning("关闭 MCP 连接失败", exc_info=True)

    if stream:
        return EventSourceResponse(chat_iterator_event())
    else:
        ret = OpenAIChatOutput(
            id=f"chat{uuid.uuid4()}",
            object="chat.completion",
            content="",
            role="assistant",
            finish_reason="stop",
            tool_calls=[],
            status=AgentStatus.agent_finish,
            message_type=MsgType.TEXT,
            message_id=message_id,
        )

        async for chunk in chat_iterator_event():
            data = json.loads(chunk)
            if "error" in data:
                # 出错时 yield 出来的是 {"error": ...}，不是 OpenAI 帧，没有
                # choices 键。之前直接下标取会抛 KeyError('choices')，把真正的
                # 失败原因盖成一个看不懂的键名。
                raise HTTPException(status_code=500, detail=str(data["error"]))
            if text := data["choices"][0]["delta"]["content"]:
                ret.content += text
            if data["status"] == AgentStatus.tool_end:
                ret.tool_calls += data["choices"][0]["delta"]["tool_calls"]
            ret.model = data["model"]
            ret.created = data["created"]

        return ret.model_dump()
