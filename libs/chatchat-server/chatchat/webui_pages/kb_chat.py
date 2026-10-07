from datetime import datetime
import uuid
from typing import Dict, List

import openai
import streamlit as st
from streamlit_chatbox import *
from streamlit_extras.bottom_container import bottom

from chatchat.settings import Settings
from chatchat.server.knowledge_base.utils import LOADER_DICT
from chatchat.server.utils import (
    api_address,
    get_config_models,
    get_config_platforms,
    get_default_llm,
)
from chatchat.webui_pages.dialogue.dialogue import upload_temp_docs
from chatchat.webui_pages.rag_helpers import (
    rag_conversation_options,
    rag_history_items,
    reference_markdown,
)
from chatchat.webui_pages.utils import *


chat_box = ChatBox(assistant_avatar=get_img_base64("chatchat_icon_blue_square_v2.png"))

RAG_CONVERSATION_LIST_KEY = "rag_conversation_list"
RAG_CURRENT_CONVERSATION_KEY = "rag_current_conversation_id"
RAG_LOADED_CONVERSATION_KEY = "rag_loaded_conversation_id"
RAG_CONVERSATION_SELECTOR_KEY = "rag_conversation_selector"
RAG_PENDING_CONVERSATION_KEY = "rag_pending_conversation_id"


def ensure_rag_conversation(api: ApiRequest) -> str:
    options = rag_conversation_options(api.list_conversations())
    st.session_state[RAG_CONVERSATION_LIST_KEY] = options
    ids = [item["id"] for item in options]
    pending = st.session_state.pop(RAG_PENDING_CONVERSATION_KEY, None)
    requested = st.session_state.get(RAG_CONVERSATION_SELECTOR_KEY)
    current = st.session_state.get(RAG_CURRENT_CONVERSATION_KEY)
    chosen = next(
        (candidate for candidate in (pending, requested, current) if candidate in ids),
        None,
    )
    if chosen is None and ids:
        chosen = ids[0]
    if chosen is None:
        created = api.create_conversation("RAG 会话1", chat_type="kb_chat")
        chosen = created.get("id")
        options = [{"id": chosen, "name": created.get("name") or "RAG 会话1"}]
        st.session_state[RAG_CONVERSATION_LIST_KEY] = options
    st.session_state[RAG_CURRENT_CONVERSATION_KEY] = chosen
    st.session_state[RAG_CONVERSATION_SELECTOR_KEY] = chosen
    return chosen


def load_rag_history(api: ApiRequest, conversation_id: str) -> None:
    if st.session_state.get(RAG_LOADED_CONVERSATION_KEY) == conversation_id:
        return
    messages = api.conversation_messages(conversation_id)
    history = []
    for item in rag_history_items(messages):
        if item["role"] == "user":
            elements = [Markdown(item["content"])]
        else:
            elements = []
            if item.get("references"):
                elements.append(
                    Markdown(
                        item["references"],
                        in_expander=True,
                        title="知识库引用",
                        state="complete",
                    )
                )
            if item.get("content"):
                elements.append(Markdown(item["content"]))
        history.append(
            {
                "role": item["role"],
                "elements": elements,
                "metadata": {"message_id": item.get("message_id")},
            }
        )
    chat_box.use_chat_name(conversation_id)
    st.session_state[chat_box._session_key][conversation_id]["history"] = history
    st.session_state[RAG_LOADED_CONVERSATION_KEY] = conversation_id


def get_rag_messages_history(history_len: int) -> List[Dict]:
    def normalize(message):
        elements = [
            element.content
            for element in message["elements"]
            if element._output_method in {"markdown", "text"} and not element._in_expander
        ]
        return {"role": message["role"], "content": "\n\n".join(elements)}

    return chat_box.filter_history(history_len=history_len, filter=normalize)


def init_widgets():
    st.session_state.setdefault("history_len", Settings.model_settings.HISTORY_LEN)
    st.session_state.setdefault("selected_kb", Settings.kb_settings.DEFAULT_KNOWLEDGE_BASE)
    st.session_state.setdefault("kb_top_k", Settings.kb_settings.VECTOR_SEARCH_TOP_K)
    st.session_state.setdefault("se_top_k", Settings.kb_settings.SEARCH_ENGINE_TOP_K)
    st.session_state.setdefault("score_threshold", Settings.kb_settings.SCORE_THRESHOLD)
    st.session_state.setdefault("search_engine", Settings.kb_settings.DEFAULT_SEARCH_ENGINE)
    st.session_state.setdefault("return_direct", False)
    st.session_state.setdefault("file_chat_id", None)


def kb_chat(api: ApiRequest):
    init_widgets()
    conversation_id = ensure_rag_conversation(api)
    load_rag_history(api, conversation_id)
    chat_box.use_chat_name(conversation_id)
    ctx = chat_box.context
    ctx.setdefault("uid", uuid.uuid4().hex)
    ctx.setdefault("file_chat_id", None)
    ctx.setdefault("llm_model", get_default_llm())
    ctx.setdefault("temperature", Settings.model_settings.TEMPERATURE)

    # st.write(chat_box.cur_chat_name)
    # st.write(st.session_state)

    @st.experimental_dialog("模型配置", width="large")
    def llm_model_setting():
        # 模型
        cols = st.columns(3)
        platforms = ["所有"] + list(get_config_platforms())
        platform = cols[0].selectbox("选择模型平台", platforms, key="platform")
        llm_models = list(
            get_config_models(
                model_type="llm", platform_name=None if platform == "所有" else platform
            )
        )
        llm_models += list(
            get_config_models(
                model_type="image2text", platform_name=None if platform == "所有" else platform
            )
        )
        llm_model = cols[1].selectbox("选择LLM模型", llm_models, key="llm_model")
        temperature = cols[2].slider("Temperature", 0.0, 1.0, key="temperature")
        system_message = st.text_area("System Message:", key="system_message")
        if st.button("OK"):
            st.rerun()

    @st.experimental_dialog("重命名会话")
    def rename_conversation():
        name = st.text_input("会话名称")
        if st.button("OK") and name.strip():
            api.rename_conversation(conversation_id, name.strip())
            st.session_state.pop(RAG_LOADED_CONVERSATION_KEY, None)
            st.rerun()

    # 配置参数
    with st.sidebar:
        tabs = st.tabs(["RAG 配置", "会话设置"])
        with tabs[0]:
            dialogue_modes = ["知识库问答", "文件对话", "搜索引擎问答"]
            dialogue_mode = st.selectbox(
                "请选择对话模式：", dialogue_modes, key="dialogue_mode"
            )
            placeholder = st.empty()
            st.divider()
            # prompt_templates_kb_list = list(Settings.prompt_settings.rag)
            # prompt_name = st.selectbox(
            #     "请选择Prompt模板：",
            #     prompt_templates_kb_list,
            #     key="prompt_name",
            # )
            prompt_name = "default"
            history_len = st.number_input("历史对话轮数：", 0, 20, key="history_len")
            kb_top_k = st.number_input("匹配知识条数：", 1, 20, key="kb_top_k")
            if dialogue_mode == "文件对话":
                threshold_max = 2.0
                threshold_help = "距离阈值越小匹配越接近；2 表示不按距离过滤。"
            else:
                threshold_max = 1.0
                threshold_help = "相关度阈值越大要求越高；0 表示不按相关度过滤。"
            current_threshold = float(st.session_state.get("score_threshold", 0.4))
            if not 0 <= current_threshold <= threshold_max:
                st.session_state["score_threshold"] = min(
                    max(float(Settings.kb_settings.SCORE_THRESHOLD), 0.0),
                    threshold_max,
                )
            score_threshold = st.slider(
                "知识匹配分数阈值：",
                0.0,
                threshold_max,
                step=0.01,
                key="score_threshold",
                help=threshold_help,
            )
            return_direct = st.checkbox("仅返回检索结果", key="return_direct")

            def on_kb_change():
                st.toast(f"已加载知识库： {st.session_state.selected_kb}")

            with placeholder.container():
                if dialogue_mode == "知识库问答":
                    kb_list = [x["kb_name"] for x in api.list_knowledge_bases()]
                    if kb_list:
                        if st.session_state.get("selected_kb") not in kb_list:
                            st.session_state["selected_kb"] = kb_list[0]
                        selected_kb = st.selectbox(
                            "请选择知识库：",
                            kb_list,
                            on_change=on_kb_change,
                            key="selected_kb",
                        )
                    else:
                        selected_kb = None
                        st.warning("当前没有可用知识库，请先创建并导入文档。")
                elif dialogue_mode == "文件对话":
                    files = st.file_uploader(
                        "上传知识文件：",
                        [i for ls in LOADER_DICT.values() for i in ls],
                        accept_multiple_files=True,
                    )
                    if st.button("开始上传", disabled=len(files) == 0):
                        st.session_state["file_chat_id"] = upload_temp_docs(files, api)
                elif dialogue_mode == "搜索引擎问答":
                    search_engine_list = list(
                        Settings.tool_settings.search_internet["search_engine_config"]
                    )
                    search_engine = st.selectbox(
                        label="请选择搜索引擎",
                        options=search_engine_list,
                        key="search_engine",
                    )

        with tabs[1]:
            # RAG 会话以后端为事实来源，并与普通聊天会话状态隔离。
            cols = st.columns(3)
            options = st.session_state.get(RAG_CONVERSATION_LIST_KEY, [])
            ids = [item["id"] for item in options]
            id_to_name = {item["id"]: item["name"] for item in options}

            def display_conversation(value):
                return f"{id_to_name.get(value, 'RAG 会话')}（{value[:8]}…）"

            selected_conversation = st.selectbox(
                "当前会话：",
                ids,
                key=RAG_CONVERSATION_SELECTOR_KEY,
                format_func=display_conversation,
            )
            if selected_conversation != conversation_id:
                st.session_state[RAG_CURRENT_CONVERSATION_KEY] = selected_conversation
                st.session_state.pop(RAG_LOADED_CONVERSATION_KEY, None)
                st.rerun()

            if cols[0].button("新建"):
                created = api.create_conversation(
                    f"RAG 会话{len(options) + 1}", chat_type="kb_chat"
                )
                st.session_state[RAG_PENDING_CONVERSATION_KEY] = created.get("id")
                st.session_state.pop(RAG_LOADED_CONVERSATION_KEY, None)
                st.rerun()
            if cols[1].button("重命名"):
                rename_conversation()
            if cols[2].button("删除"):
                api.delete_conversation(conversation_id)
                st.session_state[RAG_CURRENT_CONVERSATION_KEY] = None
                st.session_state.pop(RAG_LOADED_CONVERSATION_KEY, None)
                st.rerun()

    # Display chat messages from history on app rerun
    chat_box.output_messages()
    chat_input_placeholder = "请输入对话内容，换行请使用Shift+Enter。"

    llm_model = ctx.get("llm_model")

    # chat input
    with bottom():
        cols = st.columns([1, 0.2, 15,  1])
        if cols[0].button(":gear:", help="模型配置"):
            widget_keys = ["platform", "llm_model", "temperature", "system_message"]
            chat_box.context_to_session(include=widget_keys)
            llm_model_setting()
        if cols[-1].button(":wastebasket:", help="清空当前显示（不删除后端历史）"):
            chat_box.reset_history()
            st.rerun()
        # with cols[1]:
        #     mic_audio = audio_recorder("", icon_size="2x", key="mic_audio")
        prompt = cols[2].chat_input(chat_input_placeholder, key="prompt")
    if prompt:
        history = get_rag_messages_history(ctx.get("history_len", history_len))
        messages = history + [{"role": "user", "content": prompt}]
        chat_box.user_say(prompt)

        extra_body = dict(
            top_k=kb_top_k,
            score_threshold=score_threshold,
            temperature=ctx.get("temperature"),
            prompt_name=prompt_name,
            return_direct=return_direct,
            conversation_id=conversation_id,
        )

        api_url = api_address(is_public=True)
        # OpenAI SDK 客户端携带当前会话 Token（不再 api_key="NONE"）。
        api_key = api.token or "NONE"
        if dialogue_mode == "知识库问答":
            if not selected_kb:
                st.error("当前没有可用知识库，请先创建并导入文档。")
                st.stop()
            client = openai.Client(
                base_url=f"{api_url}/knowledge_base/local_kb/{selected_kb}",
                api_key=api_key,
            )
            chat_box.ai_say(
                [
                    Markdown(
                        "...",
                        in_expander=True,
                        title="知识库引用",
                        state="running",
                        expanded=return_direct,
                    ),
                    f"正在查询知识库 `{selected_kb}` ...",
                ]
            )
        elif dialogue_mode == "文件对话":
            if st.session_state.get("file_chat_id") is None:
                st.error("请先上传文件再进行对话")
                st.stop()
            knowledge_id = st.session_state.get("file_chat_id")
            client = openai.Client(
                base_url=f"{api_url}/knowledge_base/temp_kb/{knowledge_id}",
                api_key=api_key,
            )
            chat_box.ai_say(
                [
                    Markdown(
                        "...",
                        in_expander=True,
                        title="知识库引用",
                        state="running",
                        expanded=return_direct,
                    ),
                    f"正在查询文件 `{knowledge_id}` ...",
                ]
            )
        else:
            client = openai.Client(
                base_url=f"{api_url}/knowledge_base/search_engine/{search_engine}",
                api_key=api_key,
            )
            chat_box.ai_say(
                [
                    Markdown(
                        "...",
                        in_expander=True,
                        title="知识库引用",
                        state="running",
                        expanded=return_direct,
                    ),
                    f"正在执行 `{search_engine}` 搜索...",
                ]
            )

        text = ""
        first = True

        try:
            for d in client.chat.completions.create(
                messages=messages,
                model=llm_model,
                stream=True,
                extra_body=extra_body,
            ):
                if getattr(d, "error", None):
                    raise RuntimeError(d.error)
                if first:
                    references = getattr(d, "references", None)
                    if references:
                        citation_text = reference_markdown(
                            {
                                "references": references,
                                "retrieval_status": getattr(d, "retrieval_status", None),
                            }
                        )
                    else:
                        citation_text = "\n\n".join(getattr(d, "docs", []) or [])
                    chat_box.update_msg(
                        citation_text,
                        element_index=0,
                        streaming=False,
                        state="complete",
                    )
                    chat_box.update_msg("", streaming=False)
                    first = False
                    continue
                text += d.choices[0].delta.content or ""
                chat_box.update_msg(text.replace("\n", "\n\n"), streaming=True)
            chat_box.update_msg(text, streaming=False)
            # TODO: 搜索未配置API KEY时产生报错
        except Exception as e:
            st.error(getattr(e, "body", None) or str(e))

    now = datetime.now()
    with tabs[1]:
        cols = st.columns(2)
        export_btn = cols[0]
        if cols[1].button(
            "清空当前显示",
            use_container_width=True,
        ):
            chat_box.reset_history()
            st.rerun()

    export_btn.download_button(
        "导出记录",
        "".join(chat_box.export2md()),
        file_name=f"{now:%Y-%m-%d %H.%M}_对话记录.md",
        mime="text/markdown",
        use_container_width=True,
    )

    # st.write(chat_box.history)
