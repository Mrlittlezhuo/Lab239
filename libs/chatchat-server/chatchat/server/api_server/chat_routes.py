from __future__ import annotations

import uuid
from typing import Dict, List

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session
from langchain_core.prompts.prompt import PromptTemplate
from sse_starlette import EventSourceResponse

from chatchat.server.api_server.api_schemas import OpenAIChatInput
from chatchat.server.auth.deps import require_password_changed
from chatchat.server.chat.chat import chat
from chatchat.server.chat.kb_chat import kb_chat
from chatchat.server.chat.feedback import chat_feedback
from chatchat.server.chat.file_chat import file_chat
from chatchat.server.db.models.user_model import UserModel
from chatchat.server.db.session import get_db
from chatchat.server.db.repository import (
    add_message,
    create_conversation,
    get_owned_conversation,
)
from chatchat.server.utils import (
    get_OpenAIClient,
    get_prompt_template,
    get_tool,
    get_tool_config,
)
from chatchat.settings import Settings
from chatchat.utils import build_logger
from .openai_routes import openai_request, OpenAIChatOutput


logger = build_logger()


def _attach_route_result(result, route_dict: dict, conversation_id: str):
    """把自动意图路由结果附加到响应（任务 012）。

    非流式：在结果 dict 上增加 ``route_result`` 扩展字段；
    流式：在 SSE 流开头插入一条 ``RouteResult`` 事件，保持与现有
    流式响应（OpenAIChatOutput 扩展字段）一致的风格。
    """
    if isinstance(result, dict):
        result["conversation_id"] = conversation_id
        result["route_result"] = route_dict
        return result

    async def _wrapped():
        yield OpenAIChatOutput(
            id=f"route{uuid.uuid4().hex}",
            object="chat.completion.chunk",
            content="",
            role="assistant",
            class_name="RouteResult",
            route_result=route_dict,
            conversation_id=conversation_id,
        ).model_dump_json()
        async for chunk in result.body_iterator:
            yield chunk

    return EventSourceResponse(_wrapped())

chat_router = APIRouter(
    prefix="/chat",
    tags=["ChatChat 对话"],
    dependencies=[Depends(require_password_changed)],
)

# chat_router.post(
#     "/chat",
#     summary="与llm模型对话(通过LLMChain)",
# )(chat)

chat_router.post(
    "/feedback",
    summary="返回llm模型对话评分",
)(chat_feedback)


chat_router.post("/kb_chat", summary="知识库对话")(kb_chat)
chat_router.post("/file_chat", summary="文件对话")(file_chat)


@chat_router.post("/chat/completions", summary="兼容 openai 的统一 chat 接口")
async def chat_completions(
    request: Request,
    body: OpenAIChatInput,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> Dict:
    """Agent 对话

    要求登录。带 conversation_id 时先验证它属于当前用户（不满足在模型
    调用前返回 404）；缺失时为当前用户创建默认会话，并在响应中保留会话
    ID。
    """
    # 当调用本接口且 body 中没有传入 "max_tokens" 参数时, 默认使用配置中定义的值
    if body.max_tokens in [None, 0]:
        body.max_tokens = Settings.model_settings.MAX_TOKENS

    extra = {**body.model_extra} or {}
    for key in list(extra):
        delattr(body, key)

    # check tools & tool_choice in request body
    if isinstance(body.tool_choice, str):
        if t := get_tool(body.tool_choice):
            body.tool_choice = {"function": {"name": t.name}, "type": "function"}
    if isinstance(body.tools, list):
        for i in range(len(body.tools)):
            if isinstance(body.tools[i], str):
                if t := get_tool(body.tools[i]):
                    body.tools[i] = {
                        "type": "function",
                        "function": {
                            "name": t.name,
                            "description": t.description,
                            "parameters": t.args,
                        },
                    }

    # 先验证/创建当前用户的会话，必须在任何模型调用之前完成
    conversation_id = extra.get("conversation_id")
    if conversation_id:
        if get_owned_conversation(session, conversation_id, user.id) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="会话不存在或无权访问",
            )
    else:
        # 为当前用户创建默认会话；响应中保留会话 ID 供客户端取得
        conversation_id = create_conversation(
            session,
            owner_id=user.id,
            chat_type="agent_chat",
            name=(body.messages[-1]["content"] or "")[:50],
        )

    client = get_OpenAIClient(model_name=body.model, is_async=True)

    message_id = add_message(
        session=session,
        conversation_id=conversation_id,
        owner_id=user.id,
        chat_type="agent_chat",
        query=body.messages[-1]["content"],
    )

    chat_model_config = {}  # TODO: 前端支持配置模型
    tool_config = {}
    if body.tools:
        tool_names = [x["function"]["name"] for x in body.tools]
        tool_config = {name: get_tool_config(name) for name in tool_names}

    # 任务 011：extra 中显式开启 agent_run 时，走新的单 Agent 任务闭环
    # （规划 → 多步工具执行 → 观察 → 判断 → 持久化轨迹）；未开启时保持
    # 原有 Agent 执行器行为不变。
    if extra.get("agent_run"):
        from chatchat.server.agent_run.service import agent_run_chat

        result = await agent_run_chat(
            query=body.messages[-1]["content"],
            conversation_id=conversation_id,
            message_id=message_id,
            session=session,
            owner_id=user.id,
            stream=body.stream,
            tool_config=tool_config,
            use_mcp=extra.get("use_mcp", False),
            model_name=body.model,
            history_len=Settings.model_settings.HISTORY_LEN,
            max_steps=extra.get("agent_max_steps"),
        )
        if isinstance(result, dict):
            result["conversation_id"] = conversation_id
        return result

    # 任务 012：extra 显式开启 auto_agent（自动 Agent 模式）时，先做意图
    # 路由，再按路由结果复用普通聊天或任务 011 的 Agent Run，且只把
    # 候选工具交给 Agent Run 的规划器。路由失败/超时/非法结果/低置信度
    # 统一回退普通聊天，不阻断用户对话。（同时显式给出 agent_run 时按
    # 手动调试路径优先，保持任务 011 行为不变。）
    if extra.get("auto_agent"):
        from chatchat.server.intent_router import route_auto_agent

        auto_use_mcp = bool(Settings.model_settings.AUTO_ROUTE_USE_MCP)
        route = route_auto_agent(
            body.messages[-1]["content"],
            available_tools=get_tool(),
            use_mcp=auto_use_mcp,
        )
        if route.is_agent_run:
            from chatchat.server.agent_run.service import agent_run_chat

            auto_tool_config = {
                name: get_tool_config(name) for name in route.candidate_tools
            }
            result = await agent_run_chat(
                query=body.messages[-1]["content"],
                conversation_id=conversation_id,
                message_id=message_id,
                session=session,
                owner_id=user.id,
                stream=body.stream,
                tool_config=auto_tool_config,
                use_mcp=auto_use_mcp,
                model_name=body.model,
                history_len=Settings.model_settings.HISTORY_LEN,
            )
        else:
            # 直接回答：复用现有普通聊天流程，不装配任何工具、
            # 不创建无意义的 Agent Run
            result = await chat(
                query=body.messages[-1]["content"],
                metadata=extra.get("metadata", {}),
                conversation_id=conversation_id,
                message_id=message_id,
                history_len=Settings.model_settings.HISTORY_LEN,
                stream=body.stream,
                chat_model_config=extra.get("chat_model_config", chat_model_config),
                tool_config={},
                use_mcp=False,
                max_tokens=body.max_tokens,
                session=session,
                owner_id=user.id,
            )
        return _attach_route_result(result, route.to_dict(), conversation_id)

    result = await chat(
        query=body.messages[-1]["content"],
        metadata=extra.get("metadata", {}),
        conversation_id=conversation_id,
        message_id=message_id,
        history_len=Settings.model_settings.HISTORY_LEN,
        stream=body.stream,
        chat_model_config=extra.get("chat_model_config", chat_model_config),
        tool_config=tool_config,
        use_mcp=extra.get("use_mcp", False),
        max_tokens=body.max_tokens,
        session=session,
        owner_id=user.id,
    )
    # 保留可供客户端取得的会话 ID
    if isinstance(result, dict):
        result["conversation_id"] = conversation_id
    return result
