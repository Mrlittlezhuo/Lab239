from __future__ import annotations

import asyncio
import uuid
from typing import AsyncIterable, List, Optional, Literal

from fastapi import Body, Depends, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from sse_starlette.sse import EventSourceResponse
from langchain_classic.callbacks import AsyncIteratorCallbackHandler
from langchain_core.prompts.chat import ChatPromptTemplate


from chatchat.settings import Settings
from chatchat.server.auth.deps import require_password_changed
from chatchat.server.db.models.user_model import UserModel
from chatchat.server.db.session import get_db
from chatchat.server.db.repository import (
    add_message,
    create_conversation,
    get_owned_conversation,
    update_message,
)
from chatchat.server.knowledge_base.temp_files import assert_temp_owner
from chatchat.server.memory import context_package, memory_service
from sqlalchemy.orm import Session
from chatchat.server.agent.tools_factory.search_internet import search_engine
from chatchat.server.api_server.api_schemas import OpenAIChatOutput
from chatchat.server.chat.utils import History
from chatchat.server.knowledge_base.kb_service.base import KBServiceFactory
from chatchat.server.knowledge_base.kb_doc_api import (
    search_docs,
    search_temp_docs_by_user_id,
)
from chatchat.server.knowledge_base.utils import (
    build_references,
    format_reference_markdown,
)
from chatchat.server.utils import (wrap_done, get_ChatOpenAI, get_default_llm,
                                   resolve_llm_model_name,
                                   BaseResponse, get_prompt_template, build_logger,
                                   check_embed_model, api_address
                                )


logger = build_logger()


async def kb_chat(query: str = Body(..., description="用户输入", examples=["你好"]),
                mode: Literal["local_kb", "temp_kb", "search_engine"] = Body("local_kb", description="知识来源"),
                kb_name: str = Body("", description="mode=local_kb时为知识库名称；temp_kb时为临时知识库ID，search_engine时为搜索引擎名称", examples=["samples"]),
                top_k: int = Body(Settings.kb_settings.VECTOR_SEARCH_TOP_K, description="匹配向量数"),
                score_threshold: float = Body(
                    Settings.kb_settings.SCORE_THRESHOLD,
                    description="检索阈值：永久知识库取值0-1；临时文件库取值0-2",
                    ge=0,
                    le=2,
                ),
                history: List[History] = Body(
                    [],
                    description="历史对话",
                    examples=[[
                        {"role": "user",
                        "content": "我们来玩成语接龙，我先来，生龙活虎"},
                        {"role": "assistant",
                        "content": "虎头虎脑"}]]
                ),
                stream: bool = Body(True, description="流式输出"),
                model: str = Body(get_default_llm(), description="LLM 模型名称。"),
                temperature: float = Body(Settings.model_settings.TEMPERATURE, description="LLM 采样温度", ge=0.0, le=2.0),
                max_tokens: Optional[int] = Body(
                    Settings.model_settings.MAX_TOKENS,
                    description="限制LLM生成Token数量，默认None代表模型最大值"
                ),
                prompt_name: str = Body(
                    "default",
                    description="使用的prompt模板名称(在prompt_settings.yaml中配置)"
                ),
                return_direct: bool = Body(False, description="直接返回检索结果，不送入 LLM"),
                conversation_id: Optional[str] = Body(None, description="当前用户会话 ID"),
                use_memory: bool = Body(True, description="本轮是否使用个人记忆（个人偏好/长期记忆）"),
                temporary: bool = Body(
                    False,
                    description="临时对话：不创建、不保留会话私有上下文（第二期 §4.4）",
                ),
                project_id: Optional[str] = Body(
                    None,
                    description="会话所在项目空间：只有该项目的范围记忆能在本会话召回（第三期 §4.4）",
                ),
                request: Request = None,
                user: UserModel = Depends(require_password_changed),
                session: Session = Depends(get_db),
                ):
    # 空模型名先回落，否则后面 get_ChatOpenAI 返回 None，整轮崩在 NoneType；
    # 回落后的名字也要写进响应体，否则客户端看到 model="" 会以为请求没带模型
    model = resolve_llm_model_name(model)

    if mode == "local_kb":
        kb = KBServiceFactory.get_service_by_name(kb_name)
        if kb is None:
            return BaseResponse(code=404, msg=f"未找到知识库 {kb_name}")
    elif mode == "temp_kb":
        assert_temp_owner(user, kb_name)

    # 请求会话可能在流式生成器开始前被关闭（FastAPI 在返回响应对象时
    # 即拆除依赖，SQLAlchemy 2.0 的 close() 会 detach 会话内所有实例）。
    # 因此必须在会话仍处于打开状态时先取出 user.id，生成器内部只能
    # 使用这个值，不能再读取 user 的属性。
    user_id = user.id

    if conversation_id:
        if get_owned_conversation(session, conversation_id, user_id) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="会话不存在或无权访问",
            )
    else:
        # project_id 必须是当前用户自己的项目，否则等于把别人的项目空间
        # 挂到会话上，项目范围记忆就会被带进本轮上下文（第三期 §4.4）。
        project = None
        if project_id:
            from chatchat.server.db.repository import (
                research_repository as research_repo,
            )

            project = research_repo.get_owned_project(session, project_id, user_id)
            if project is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="项目不存在或无权访问",
                )
        # temporary=True 表示临时对话：会话上下文与个人偏好写入都在记忆层被拦住
        conversation_id = create_conversation(
            session,
            owner_id=user_id,
            chat_type="kb_chat",
            name=(query or "")[:50],
            temporary=bool(temporary),
            project_id=project.id if project else None,
        )
    message_id = add_message(
        session=session,
        conversation_id=conversation_id,
        owner_id=user_id,
        chat_type="kb_chat",
        query=query,
    )

    async def knowledge_base_chat_iterator() -> AsyncIterable[str]:
        reference_metadata = {
            "mode": mode,
            "knowledge_base": kb_name,
            "retrieval_status": "pending",
            "references": [],
            "docs": [],
        }
        try:
            nonlocal history, prompt_name, max_tokens

            history = [History.from_data(h) for h in history]

            # 自动记忆提取：在本轮构造记忆上下文前，把用户明确表达
            # （如“请记住...”）写入长期记忆。失败不阻断正常回答。
            try:
                memory_service.auto_extract(
                    session,
                    user_id,
                    query,
                    source_conversation_id=conversation_id,
                    source_message_id=message_id,
                )
            except Exception:
                pass

            # 统一上下文构建器：知识库对话的最近历史改为从数据库读取，不再
            # 采信请求体里的 history（客户端传来的历史既可能过期，也可能被
            # 伪造成别人的轮次）；同一份数据同时给出会话摘要与记忆注入段，
            # 分界只有一处（设计方案 §18.2.2 / §18.2.5 / §18.2.9）。
            # 摘要与槽位在构建 Prompt 前按同一分界更新；失败不阻断正常回答。
            package = context_package.build_context_package(
                session,
                user_id,
                conversation_id,
                query,
                use_memory=use_memory,
                min_turns=Settings.model_settings.HISTORY_LEN,
            )
            # 请求体里的 history 一律不采信：无记忆对话的原始消息同样持久化，
            # 最近原文也从数据库读（设计方案 §13.1）。
            history = []
            for query_text, response_text in package.history_pairs():
                history.append(History(role="user", content=query_text))
                history.append(History(role="assistant", content=response_text))

            if mode == "local_kb":
                if score_threshold > 1:
                    raise ValueError("永久知识库的相关度阈值必须在 0 到 1 之间")
                kb = KBServiceFactory.get_service_by_name(kb_name)
                ok, msg = kb.check_embed_model()
                if not ok:
                    raise ValueError(msg)
                docs = await run_in_threadpool(search_docs,
                                                query=query,
                                                knowledge_base_name=kb_name,
                                                top_k=top_k,
                                                score_threshold=score_threshold,
                                                file_name="",
                                                metadata={})
                references = build_references(
                    kb_name, docs, api_address(is_public=True)
                )
            elif mode == "temp_kb":
                ok, msg = check_embed_model()
                if not ok:
                    raise ValueError(msg)
                # 线程池内只允许传递 user_id 字符串：请求会话在生成器开始
                # 前已关闭，ORM user 实例处于 detached 状态，跨线程读取其
                # 属性会抛 DetachedInstanceError（属主校验用纯字符串比较）。
                docs = await run_in_threadpool(search_temp_docs_by_user_id,
                                                kb_name,
                                                query=query,
                                                top_k=top_k,
                                                score_threshold=score_threshold,
                                                user_id=user_id)
                references = build_references(
                    kb_name, docs, api_address(is_public=True)
                )
            elif mode == "search_engine":
                result = await run_in_threadpool(search_engine, query, top_k, kb_name)
                docs = [x.dict() for x in result.get("docs", [])]
                references = []
                for index, doc in enumerate(docs, start=1):
                    metadata = doc.get("metadata") or {}
                    references.append(
                        {
                            "index": index,
                            "knowledge_base": kb_name,
                            "file_name": metadata.get("filename") or "网页来源",
                            "url": metadata.get("source") or "",
                            "content": doc.get("page_content") or "",
                            "score": doc.get("score"),
                            "page": (
                                metadata.get("page")
                                if metadata.get("page") is not None
                                else metadata.get("page_number")
                            ),
                        }
                    )
            else:
                docs = []
                references = []
            source_documents = [
                format_reference_markdown(reference) for reference in references
            ]
            reference_metadata.update(
                {
                    "retrieval_status": "matched" if references else "empty",
                    "references": references,
                    "docs": source_documents,
                }
            )
            # import rich
            # rich.print(dict(
            #     mode=mode,
            #     query=query,
            #     knowledge_base_name=kb_name,
            #     top_k=top_k,
            #     score_threshold=score_threshold,
            # ))
            # rich.print(docs)
            if return_direct:
                update_message(
                    session=session,
                    message_id=message_id,
                    owner_id=user_id,
                    response="",
                    metadata=reference_metadata,
                )
                yield OpenAIChatOutput(
                    id=f"chat{uuid.uuid4()}",
                    model=None,
                    object="chat.completion",
                    content="",
                    role="assistant",
                    finish_reason="stop",
                    docs=source_documents,
                    references=references,
                    retrieval_status=reference_metadata["retrieval_status"],
                    message_id=message_id,
                    conversation_id=conversation_id,
                ).model_dump_json()
                return

            callback = AsyncIteratorCallbackHandler()
            callbacks = [callback]

            # Enable langchain-chatchat to support langfuse
            import os
            langfuse_secret_key = os.environ.get('LANGFUSE_SECRET_KEY')
            langfuse_public_key = os.environ.get('LANGFUSE_PUBLIC_KEY')
            langfuse_host = os.environ.get('LANGFUSE_HOST')
            if langfuse_secret_key and langfuse_public_key and langfuse_host :
                from langfuse import Langfuse
                from langfuse.callback import CallbackHandler
                langfuse_handler = CallbackHandler()
                callbacks.append(langfuse_handler)

            if max_tokens in [None, 0]:
                max_tokens = Settings.model_settings.MAX_TOKENS

            llm = get_ChatOpenAI(
                model_name=model,
                temperature=temperature,
                max_tokens=max_tokens,
                callbacks=callbacks,
            )
            if llm is None:
                # get_ChatOpenAI 失败时不抛异常只返回 None。以前直接往下走，
                # 用户看到的是 langchain 的 NoneType 报错，看不出该改哪里。
                raise ValueError(
                    f"无法创建模型「{model}」：请确认它在服务端可用"
                    f"（GET /server/models 的 llm_models），或在聊天设置里换一个模型"
                )
            # TODO： 视情况使用 API
            # # 加入reranker
            # if Settings.kb_settings.USE_RERANKER:
            #     reranker_model_path = get_model_path(Settings.kb_settings.RERANKER_MODEL)
            #     reranker_model = LangchainReranker(top_n=top_k,
            #                                     device=embedding_device(),
            #                                     max_length=Settings.kb_settings.RERANKER_MAX_LENGTH,
            #                                     model_name_or_path=reranker_model_path
            #                                     )
            #     print("-------------before rerank-----------------")
            #     print(docs)
            #     docs = reranker_model.compress_documents(documents=docs,
            #                                              query=query)
            #     print("------------after rerank------------------")
            #     print(docs)
            context = "\n\n".join(
                f"【资料 {index}】\n{doc.get('page_content') or ''}"
                for index, doc in enumerate(docs, start=1)
            )
            if context:
                context = (
                    "请在使用资料内容时，在对应表述后标注资料编号，如 [1]。\n\n"
                    + context
                )

            if len(docs) == 0:  # 如果没有找到相关文档，使用empty模板
                prompt_name = "empty"
            prompt_template = get_prompt_template("rag", prompt_name)
            input_msg = History(role="user", content=prompt_template).to_msg_template(False)
            # 用户记忆注入：与普通聊天同一套记忆（个人偏好 + 长期记忆 + 较早
            # 内容摘要），作为独立系统消息置于 RAG 输入之前（当前用户本轮表达
            # 优先）。失败/无记忆时为空，不改变原行为，也不阻断正常回答。
            # 只影响"怎么解释和呈现"：检索范围、引用、文档原文、知识库权限
            # 都在此之前已经确定，个人偏好不参与（设计方案 §4.3）。
            _mem_msgs = [
                History(role=m["role"], content=m["content"]).to_msg_template(False)
                for m in package.system_messages()
            ]
            chat_prompt = ChatPromptTemplate.from_messages(
                _mem_msgs + [i.to_msg_template() for i in history] + [input_msg])

            chain = chat_prompt | llm

            # Begin a task that runs in the background.
            task = asyncio.create_task(wrap_done(
                chain.ainvoke({"context": context, "question": query}),
                callback.done),
            )

            if len(source_documents) == 0:  # 没有找到相关文档
                source_documents.append(
                    "未检索到符合条件的资料；以下回答来自模型自身知识。"
                )
                reference_metadata["docs"] = source_documents

            if stream:
                answer = ""
                # yield documents first
                ret = OpenAIChatOutput(
                    id=f"chat{uuid.uuid4()}",
                    object="chat.completion.chunk",
                    content="",
                    role="assistant",
                    model=model,
                    docs=source_documents,
                    references=references,
                    retrieval_status=reference_metadata["retrieval_status"],
                    message_id=message_id,
                    conversation_id=conversation_id,
                )
                yield ret.model_dump_json()

                async for token in callback.aiter():
                    answer += token
                    ret = OpenAIChatOutput(
                        id=f"chat{uuid.uuid4()}",
                        object="chat.completion.chunk",
                        content=token,
                        role="assistant",
                        model=model,
                        message_id=message_id,
                        conversation_id=conversation_id,
                    )
                    yield ret.model_dump_json()
            else:
                answer = ""
                async for token in callback.aiter():
                    answer += token
                ret = OpenAIChatOutput(
                    id=f"chat{uuid.uuid4()}",
                    object="chat.completion",
                    content=answer,
                    role="assistant",
                    model=model,
                    message_id=message_id,
                    conversation_id=conversation_id,
                    docs=source_documents,
                    references=references,
                    retrieval_status=reference_metadata["retrieval_status"],
                )
                yield ret.model_dump_json()
            await task
            update_message(
                session=session,
                message_id=message_id,
                owner_id=user_id,
                response=answer,
                metadata=reference_metadata,
            )
        except asyncio.exceptions.CancelledError:
            logger.warning("streaming progress has been interrupted by user.")
            return
        except Exception as e:
            logger.error(f"error in knowledge chat: {e}")
            reference_metadata.update(
                {"retrieval_status": "error", "error": str(e)}
            )
            try:
                update_message(
                    session=session,
                    message_id=message_id,
                    owner_id=user_id,
                    response="",
                    metadata=reference_metadata,
                )
            except Exception:
                pass
            yield OpenAIChatOutput(
                id=f"chat{uuid.uuid4()}",
                object="chat.completion.chunk",
                content="",
                role="assistant",
                model=model,
                finish_reason="stop",
                error=str(e),
                retrieval_status="error",
                message_id=message_id,
                conversation_id=conversation_id,
            ).model_dump_json()
            return

    if stream:
        return EventSourceResponse(knowledge_base_chat_iterator())
    else:
        # 非流式路径必须消费完整生成器，否则 LLM 任务不会等待完成、
        # 助手回复也不会通过 update_message 持久化（回复直接丢失）。
        last_chunk = ""
        async for chunk in knowledge_base_chat_iterator():
            last_chunk = chunk
        return last_chunk
