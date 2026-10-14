from __future__ import annotations

from typing import Dict, List

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session
from langchain_core.prompts.prompt import PromptTemplate

from chatchat.server.agent.tools_access import available_tools, partition_tools
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
    get_config_models,
    get_default_llm,
    get_model_info,
    get_prompt_template,
    get_tool,
    get_tool_config,
)
from chatchat.settings import Settings
from chatchat.utils import build_logger


logger = build_logger()

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


def _require_configured_model(body: OpenAIChatInput) -> None:
    """把模型名收敛成已配置的那个，配置问题给 400 而不是 500。

    - 空 ``model`` 是前端“用服务端默认模型”的哨兵值：pydantic 不把 ``""``
      当成缺省，必须自己回落，否则 ``get_OpenAIClient`` 里的 assert 会把
      请求变成 500；
    - 未配置的模型同样会在 ``get_OpenAIClient`` 里 assert 崩掉
      （``get_model_info`` 对未知模型返回空 dict，不是 ``None``），这里提前
      拦下并附上可用清单，客户端不用猜。
    """
    if not (body.model or "").strip():
        body.model = get_default_llm()
    if not get_model_info(model_name=body.model):
        available = ", ".join(sorted(get_config_models(model_type="llm")))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"模型 {body.model} 未配置。可用模型：{available}",
        )


def _resolve_tool_names(tools, registry: Dict, block_reasons: Dict[str, str] = None) -> List[str]:
    """把请求体里的 ``tools`` 收敛成工具名列表，配置/拼写问题给 400 而不是 500。

    两种形状都要支持：前端（``frontend/src/api/chat.ts``）发的是名字数组
    ``["calculate"]``，OpenAI 兼容客户端发的是
    ``{"type": "function", "function": {"name": ...}}``。旧写法只认后者，
    名字数组里任何没被前面那段代码换成 dict 的项（拼错、未注册、被角色过滤掉）
    会留在原地，到 ``x["function"]["name"]`` 变成
    ``TypeError: string indices must be integers`` —— 一个 500，
    用户只看到"工具没生效"，猜不到是名字的问题。
    """
    names: List[str] = []
    for item in tools or []:
        if isinstance(item, str):
            name = item
        elif isinstance(item, dict):
            name = (item.get("function") or {}).get("name") or item.get("name")
        else:
            name = None
        if not name:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"tools 中有无法识别的项：{item!r}",
            )
        if name not in registry:
            blocked = (block_reasons or {}).get(name)
            if blocked:
                # 工具存在但当前不该用（未配置 / 只给管理员）：把原因说清楚，
                # 否则用户只会看到"我勾了工具但没生效"
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, detail=blocked
                )
            available = "、".join(sorted(registry)) or "无"
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"工具「{name}」不可用。本轮可选工具：{available}",
            )
        names.append(name)
    return names


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

    if not body.messages:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="messages 不能为空",
        )

    # 本接口把最后一条消息的 content 当字符串用（会话标题、落库 query、
    # prompt 模板），多模态数组会在写库时失败、也不会到达模型。
    # 图片解析走 /v1/chat/completions（代理路由原样透传）。
    if not isinstance(body.messages[-1].get("content"), str):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="该接口不支持多模态消息，图片解析请使用 /v1/chat/completions",
        )

    extra = {**body.model_extra} or {}
    for key in list(extra):
        delattr(body, key)

    # 界面"本次不使用个人偏好"（会话级）：只让本轮不注入个人记忆，
    # 不删除数据，也不影响会话摘要与自动记忆提取。
    use_memory = bool(extra.get("use_memory", True))

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

    # 模型校验排在越权检查之后、建会话之前：跨用户访问仍然是 404，不能被
    # 400 掩盖；模型名错误则不会留下只有用户问题的空会话。
    _require_configured_model(body)

    if not conversation_id:
        # 为当前用户创建默认会话；响应中保留会话 ID 供客户端取得。
        # extra.temporary=True 表示临时对话：后续不创建、不保留会话上下文
        # （智能聊天记忆第二期设计方案 §4.4）。
        # extra.project_id 表示会话所在项目空间：只有该项目的范围记忆能在
        # 这个会话里被召回（智能聊天记忆第三期设计方案 §4.4）。
        project_id = extra.get("project_id") or None
        if project_id:
            from chatchat.server.db.repository import (
                research_repository as research_repo,
            )

            if research_repo.get_owned_project(session, project_id, user.id) is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="项目不存在或无权访问",
                )
        conversation_id = create_conversation(
            session,
            owner_id=user.id,
            chat_type="agent_chat",
            name=(body.messages[-1]["content"] or "")[:50],
            temporary=bool(extra.get("temporary", False)),
            project_id=project_id,
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
        # `partition_tools()` 会 reload tools_factory 并顺带查一次知识库列表，
        # 没勾工具的普通对话没必要付这个代价（也避免工具导入问题波及所有轮次）。
        allowed, blocked = partition_tools(user, get_tool())
        tool_names = _resolve_tool_names(body.tools, allowed, blocked)
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
            use_memory=use_memory,
        )
        if isinstance(result, dict):
            result["conversation_id"] = conversation_id
        return result

    # 任务 015：extra 显式开启 auto_agent（面向用户的"智能对话"主入口）时，
    # 通过智能对话总图（真实 LangGraph 状态图）统一意图路由与分发：
    # route → 直接回答 / 知识库任务 / 单工具任务 / 多步骤 Agent → 统一输出。
    # 总图内部复用现有意图路由、普通聊天流程与任务 014 的 Agent Runtime；
    # 知识库 / 单工具任务限制为一步工具任务；路由或执行准备失败在工具执行
    # 开始前可控回退直接回答。流式首条事件为路由 / 执行分支事件。
    # （同时显式给出 agent_run 时按手动调试路径优先，保持任务 011 行为不变。）
    if extra.get("auto_agent"):
        from chatchat.server.smart_conversation import run_smart_conversation

        return await run_smart_conversation(
            query=body.messages[-1]["content"],
            conversation_id=conversation_id,
            message_id=message_id,
            session=session,
            owner_id=user.id,
            stream=body.stream,
            model_name=body.model,
            max_tokens=body.max_tokens,
            history_len=Settings.model_settings.HISTORY_LEN,
            metadata=extra.get("metadata", {}),
            chat_model_config=extra.get("chat_model_config", chat_model_config),
            # MCP 按请求走：前端「智能路由」里的开关（extra.use_mcp）决定本轮
            # 是否加载 MCP 工具；全局 AUTO_ROUTE_USE_MCP 保留为强制开启的逃生口。
            # 以前只看全局配置，请求里的 use_mcp 在这个分支被直接忽略。
            auto_use_mcp=bool(extra.get("use_mcp", False))
            or bool(Settings.model_settings.AUTO_ROUTE_USE_MCP),
            # 只把"这台机器配好了、这个用户能用"的工具交给意图路由：
            # 否则模型会挑一个必然失败的工具（假 key、占位连接串、只有管理员
            # 能用的 shell），用户看到的只是"工具没生效"
            available_tools=available_tools(user, get_tool()),
            use_memory=use_memory,
        )

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
        use_memory=use_memory,
    )
    # 保留可供客户端取得的会话 ID
    if isinstance(result, dict):
        result["conversation_id"] = conversation_id
    return result
