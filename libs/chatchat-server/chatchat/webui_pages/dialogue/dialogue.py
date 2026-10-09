import base64
import hashlib
import io
import os
import uuid
from datetime import datetime
from PIL import Image as PILImage
from typing import Dict, List
from urllib.parse import urlencode

# from audio_recorder_streamlit import audio_recorder
import openai
import streamlit as st
import streamlit_antd_components as sac
from streamlit_chatbox import *
from streamlit_extras.bottom_container import bottom
from streamlit_paste_button import paste_image_button

from chatchat.settings import Settings
from langchain_chatchat.callbacks.agent_callback_handler import AgentStatus
from chatchat.server.knowledge_base.model.kb_document_model import DocumentWithVSId
from chatchat.server.knowledge_base.utils import format_reference
from chatchat.server.utils import MsgType, get_config_models, get_config_platforms, get_default_llm
from chatchat.webui_pages.utils import *
from streamlit_chatbox import Markdown as _Markdown
from chatchat.webui_pages import auth_state
from chatchat.webui_pages.auth_state import (
    get_current_user,
    get_token,
    resolve_current_conversation,
    synchronize_current_conversation,
    conversation_options,
    messages_to_history,
)


chat_box = ChatBox(assistant_avatar=get_img_base64("chatchat_icon_blue_square_v2.png"))


# 不属于「会话级配置」的 session_state 键：不应被持久化进 ChatBox 的会话上下文，
# 也不应随切换会话被恢复。包含会话管理、认证、UI 与图片/粘贴等状态。
_CONTEXT_EXCLUDE = [
    "selected_page",
    "prompt",
    "cur_conv_name",
    "last_conv_name",
    "upload_image",
    "cur_image",
    "paste_image",
    "conversation_list",
    "current_conversation_id",
    "last_conversation_id",
    "loaded_conv_ids",
    "loaded_conversation_id",
    "auth_token",
    "current_user",
    "_session_api",
]


def save_session(conv_name: str = None):
    """save session state to chat context"""
    chat_box.context_from_session(conv_name, exclude=_CONTEXT_EXCLUDE)


def restore_session(conv_name: str = None):
    """restore sesstion state from chat context"""
    chat_box.context_to_session(conv_name, exclude=_CONTEXT_EXCLUDE)


def rerun():
    """
    save chat context before rerun
    """
    save_session()
    st.rerun()


def get_messages_history(
    history_len: int, content_in_expander: bool = False
) -> List[Dict]:
    """
    返回消息历史。
    content_in_expander控制是否返回expander元素中的内容，一般导出的时候可以选上，传入LLM的history不需要
    """

    def filter(msg):
        content = [
            x for x in msg["elements"] if x._output_method in ["markdown", "text"]
        ]
        if not content_in_expander:
            content = [x for x in content if not x._in_expander]
        content = [x.content for x in content]

        return {
            "role": msg["role"],
            "content": "\n\n".join(content),
        }

    messages = chat_box.filter_history(history_len=history_len, filter=filter)
    if sys_msg := chat_box.context.get("system_message"):
        messages = [{"role": "system", "content": sys_msg}] + messages

    return messages


def upload_temp_docs(files, _api: ApiRequest) -> str:
    """
    将文件上传到临时目录，用于文件对话
    返回临时向量库ID

    临时文件属于用户私有内容，本任务不缓存该结果，避免跨会话复用。
    """
    return _api.upload_temp_docs(files).get("data", {}).get("id")


def upload_image_file(file_name: str, content: bytes, token: str) -> dict:
    '''upload image for vision model using openai sdk'''
    # OpenAI SDK 客户端携带当前会话 Token（不再 api_key="NONE"）。
    # 图片上传属于用户私有操作，不使用跨会话缓存。
    client = openai.Client(
        base_url=f"{api_address()}/v1",
        api_key=token or "NONE",
    )
    return client.files.create(file=(file_name, content), purpose="assistants").to_dict()


def clear_private_caches() -> None:
    """兼容退出流程；用户私有上传函数当前不使用共享缓存。"""
    return None


# ---------------------------------------------------------------------------
# 后端会话为事实来源（任务 7.3）：会话列表/当前会话/历史均以后端为准。
# ---------------------------------------------------------------------------
def ensure_backend_conversation(api: ApiRequest) -> str:
    """确保当前会话存在并返回其后端 id。

    - 已有 current_conversation_id 且仍在后端列表中：返回它。
    - 已有 current_conversation_id 但已不在列表（如被删除）：选择剩余会话的第一个。
    - 后端无会话：创建默认会话。
    同时将会话列表（[{id, name}]）写入 session_state.conversation_list。
    """
    try:
        backend_list = api.list_conversations()
    except AuthenticationError:
        backend_list = []
    options = conversation_options(backend_list)
    st.session_state["conversation_list"] = options
    chosen = synchronize_current_conversation(st.session_state, options)
    if chosen is None:
        chosen = api.create_conversation("会话1").get("id")
        st.session_state["current_conversation_id"] = chosen
        st.session_state["cur_conv_name"] = chosen
    return chosen


def load_backend_history(api: ApiRequest, conv_id: str) -> None:
    """将后端历史消息加载到 ChatBox（以 conv_id 为键）。

    后端为事实来源；ChatBox 只用于显示与本次交互。重复调用（同会话）不重复加载。
    """
    if st.session_state.get("loaded_conversation_id") == conv_id:
        return
    try:
        msgs = api.conversation_messages(conv_id)
    except AuthenticationError:
        msgs = []
    history = [
        {
            "role": item["role"],
            "elements": [_Markdown(item["content"])],
            "metadata": {},
        }
        for item in messages_to_history(msgs)
    ]
    chat_box.use_chat_name(conv_id)
    st.session_state[chat_box._session_key][conv_id]["history"] = history
    st.session_state["loaded_conversation_id"] = conv_id


def add_conv(name: str = ""):
    """新建会话（本地 ChatBox 名称；kb_chat 仍使用）。"""
    conv_names = chat_box.get_chat_names()
    if not name:
        i = len(conv_names) + 1
        while True:
            name = f"会话{i}"
            if name not in conv_names:
                break
            i += 1
    if name in conv_names:
        sac.alert(
            "创建新会话出错",
            f"该会话名称 “{name}” 已存在",
            color="error",
            closable=True,
        )
    else:
        chat_box.use_chat_name(name)
        st.session_state["cur_conv_name"] = name


def del_conv(name: str = None):
    """删除会话（本地 ChatBox 名称；kb_chat 仍使用）。"""
    conv_names = chat_box.get_chat_names()
    name = name or chat_box.cur_chat_name

    if len(conv_names) == 1:
        sac.alert(
            "删除会话出错", f"这是最后一个会话，无法删除",
            color="error",
            closable=True,
        )
    elif not name or name not in conv_names:
        sac.alert(
            "删除会话出错", f"无效的会话名称：“{name}”",
            color="error",
            closable=True,
        )
    else:
        chat_box.del_chat_name(name)
        # restore_session()
    st.session_state["cur_conv_name"] = chat_box.cur_chat_name


def backend_add_conv(api: ApiRequest):
    """新建会话（多功能对话页）：先创建后端会话，成功后切换为当前会话。"""
    name = "会话" + str(len(st.session_state.get("conversation_list", [])) + 1)
    try:
        created = api.create_conversation(name)
    except AuthenticationError:
        sac.alert("新建会话出错", "登录已失效，请重新登录", color="error", closable=True)
        return
    cid = created.get("id")
    st.session_state.pop("loaded_conversation_id", None)
    st.session_state["current_conversation_id"] = cid
    chat_box.use_chat_name(cid)
    st.rerun()


def backend_del_conv(api: ApiRequest):
    """删除会话（多功能对话页）：先调用后端接口，成功后切换到剩余会话。"""
    cid = st.session_state.get("current_conversation_id")
    if not cid:
        return
    try:
        api.delete_conversation(cid)
    except AuthenticationError:
        sac.alert("删除会话出错", "登录已失效，请重新登录", color="error", closable=True)
        return
    st.session_state.pop("loaded_conversation_id", None)
    st.session_state.pop("current_conversation_id", None)
    # 重新确定当前会话（剩余会话第一个，或创建默认会话）
    ensure_backend_conversation(api)
    st.rerun()


def clear_conv(name: str = None):
    chat_box.reset_history(name=name or None)


# @st.cache_data
def list_tools(_api: ApiRequest):
    return _api.list_tools() or {}


def dialogue_page(
    api: ApiRequest,
    is_lite: bool = False,
):
    ctx = chat_box.context
    ctx.setdefault("uid", uuid.uuid4().hex)
    ctx.setdefault("file_chat_id", None)
    ctx.setdefault("llm_model", get_default_llm())
    ctx.setdefault("temperature", Settings.model_settings.TEMPERATURE)

    # 后端为事实来源：确定当前会话 id，并从后端恢复其历史消息。
    # 服务重启后仍可从后端加载历史（7.3）。
    current_conv_id = ensure_backend_conversation(api)
    load_backend_history(api, current_conv_id)
    chat_box.use_chat_name(current_conv_id)
    st.session_state["current_conversation_id"] = current_conv_id

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
            rerun()

    @st.experimental_dialog("重命名会话")
    def rename_conversation():
        cid = st.session_state.get("current_conversation_id")
        name = st.text_input("会话名称")
        if st.button("OK") and name and cid:
            try:
                api.rename_conversation(cid, name)
            except AuthenticationError:
                st.toast("登录已失效，请重新登录")
            # 更新会话列表缓存中的显示名称
            for o in st.session_state.get("conversation_list", []):
                if o.get("id") == cid:
                    o["name"] = name
            rerun()

    with st.sidebar:
        tab1, tab2 = st.tabs(["工具设置", "会话设置"])

        with tab1:
            # 任务 015：统一对话入口「智能对话」为默认主入口，由系统自动判断
            # 执行路径（直接回答 / 知识库任务 / 单工具任务 / 多步骤 Agent）；
            # 用户无需再在「普通对话 / 自动 Agent」之间选择。强制模式（普通
            # 直聊）与手动调试（工具多选 / MCP / Agent Run）收入高级设置
            # （调试用），默认收起，不影响主入口的简洁性。
            advanced = st.expander(
                "高级设置（强制模式 / 手动调试）",
                expanded=False,
            )
            auto_agent = True
            use_agent = False
            use_mcp = False
            use_agent_run = False
            # 选择工具
            tools = list_tools(api)
            tool_names = ["None"] + list(tools)
            selected_tools = []
            with advanced:
                dialogue_mode = st.radio(
                    "模式覆盖",
                    ["智能对话（默认）", "普通直聊", "手动调试"],
                    index=0,
                    horizontal=True,
                    key="dialogue_mode",
                    help=(
                        "智能对话（默认）：系统自动判断直接回答、知识库任务、"
                        "单工具任务或多步骤 Agent 执行；"
                        "普通直聊：强制直接聊天，不做自动路由；"
                        "手动调试：手动选择工具 / MCP / Agent Run，用于调试。"
                    ),
                )
                auto_agent = dialogue_mode == "智能对话（默认）"
                use_agent = dialogue_mode == "手动调试"
                if auto_agent:
                    st.caption("智能对话会由系统判断是否使用工具，无需手动选择。")
                elif use_agent:
                    use_mcp = st.checkbox("使用MCP", key="use_mcp")
                    use_agent_run = st.checkbox(
                        "启用任务闭环执行（Agent Run）",
                        help=(
                            "启用后按单 Agent 任务闭环执行：生成结构化计划 → 多步执行工具 → "
                            "记录观察结果 → 判断完成或重新规划 → 生成最终回答，执行轨迹会持久化，"
                            "运行中可用左侧“取消 Agent 运行”按钮取消。请确保所选模型具备规划与 "
                            "JSON 输出能力。"
                        ),
                        key="use_agent_run",
                    )
                    # selected_tools = sac.checkbox(list(tools), format_func=lambda x: tools[x]["title"], label="选择工具",
                    # check_all=True, key="selected_tools")
                    selected_tools = st.multiselect(
                        "选择工具",
                        list(tools),
                        format_func=lambda x: tools[x]["title"],
                        key="selected_tools",
                    )
                else:
                    # 普通直聊：强制直接聊天，不装配工具
                    # selected_tool = sac.buttons(list(tools), format_func=lambda x: tools[x]["title"], label="选择工具",
                    selected_tools = []
            selected_tool_configs = {
                name: tool["config"]
                for name, tool in tools.items()
                if name in selected_tools
            }

            if "None" in selected_tools:
                selected_tools.remove("None")

            # 任务 011：进行中的 Agent 任务运行（Agent Run）取消入口。
            # 流式输出期间页面被占用、无法点击控件；点击本按钮会触发页面重跑，
            # 既中断当前请求（服务端运行收敛到终态），又显式调用取消接口，
            # 已执行步骤的轨迹保持持久化。
            _agent_run_state = st.session_state.get("agent_run") or {}
            if (
                _agent_run_state.get("run_id")
                and _agent_run_state.get("status") in ("pending", "running")
            ):
                if st.button(
                    "取消 Agent 运行",
                    key="agent_run_cancel_btn",
                    help="请求取消进行中的 Agent 任务运行（已执行步骤会保留）",
                ):
                    _cancel_res = None
                    try:
                        _cancel_res = api.agent_run_cancel(
                            _agent_run_state["run_id"]
                        )
                    except AuthenticationError:
                        st.toast("登录已失效，请重新登录")
                    if isinstance(_cancel_res, dict) and isinstance(
                        _cancel_res.get("run"), dict
                    ):
                        st.session_state["agent_run"]["status"] = (
                            _cancel_res["run"].get("status", "cancelled")
                        )
                        st.toast("已发送取消请求", icon="⏹️")
                    else:
                        _detail = (
                            _cancel_res.get("detail")
                            if isinstance(_cancel_res, dict)
                            else None
                        )
                        st.toast(_detail or "取消运行失败", icon="⚠️")
                    rerun()
            # 当不启用Agent时，手动生成工具参数
            # TODO: 需要更精细的控制控件
            tool_input = {}
            if not use_agent and len(selected_tools) == 1:
                with st.expander("工具参数", True):
                    for k, v in tools[selected_tools[0]]["args"].items():
                        if choices := v.get("choices", v.get("enum")):
                            tool_input[k] = st.selectbox(v["title"], choices)
                        else:
                            if v["type"] == "integer":
                                tool_input[k] = st.slider(
                                    v["title"], value=v.get("default")
                                )
                            elif v["type"] == "number":
                                tool_input[k] = st.slider(
                                    v["title"], value=v.get("default"), step=0.1
                                )
                            else:
                                tool_input[k] = st.text_input(
                                    v["title"], v.get("default")
                                )

            # uploaded_file = st.file_uploader("上传附件", accept_multiple_files=False)
            # files_upload = process_files(files=[uploaded_file]) if uploaded_file else None
            files_upload = None

            # 用于图片对话、文生图的图片
            upload_image = None
            upload_image_data_url = None
            def on_upload_file_change():
                if f := st.session_state.get("upload_image"):
                    name = ".".join(f.name.split(".")[:-1]) + ".png"
                    st.session_state["cur_image"] = (name, PILImage.open(f))
                else:
                    st.session_state["cur_image"] = (None, None)
                st.session_state.pop("paste_image", None)

            st.file_uploader("上传图片", ["bmp", "jpg", "jpeg", "png"],
                                            accept_multiple_files=False,
                                            key="upload_image",
                                            on_change=on_upload_file_change)
            paste_image = paste_image_button("黏贴图像", key="paste_image")
            cur_image = st.session_state.get("cur_image", (None, None))
            if cur_image[1] is None and paste_image.image_data is not None:
                name = hashlib.md5(paste_image.image_data.tobytes()).hexdigest()+".png"
                cur_image = (name, paste_image.image_data) 
            if cur_image[1] is not None:
                st.image(cur_image[1])
                buffer = io.BytesIO()
                cur_image[1].save(buffer, format="png")
                image_bytes = buffer.getvalue()
                upload_image_data_url = (
                    "data:image/png;base64,"
                    + base64.b64encode(image_bytes).decode("ascii")
                )
                upload_image = upload_image_file(
                    cur_image[0], image_bytes, st.session_state.get(auth_state.TOKEN_KEY)
                )

        with tab2:
            # 会话（后端为事实来源，以 conversation id 为稳定键）。
            # selectbox 的选项与选中值均为后端 id，显示时用 formatter 映射为名称，
            # 同名会话因 id 不同而被正确区分。
            cols = st.columns(3)
            options = st.session_state.get("conversation_list", [])
            ids = [o.get("id") for o in options]
            id_to_name = {o.get("id"): o.get("name", "") for o in options}

            def _conv_display(value):
                return f"{id_to_name.get(value, '')}（{value[:8]}…）"

            cur_conv_name = st.selectbox(
                "当前会话：",
                ids,
                key="cur_conv_name",
                format_func=_conv_display,
            )

            # 切换会话：cur_conv_name（后端 id）变化时，保存旧会话、恢复新会话（从后端）。
            if cur_conv_name != current_conv_id:
                save_session(current_conv_id)
                st.session_state["current_conversation_id"] = cur_conv_name
                load_backend_history(api, cur_conv_name)
                chat_box.use_chat_name(cur_conv_name)
                st.rerun()

            if cols[0].button("新建", on_click=lambda: backend_add_conv(api)):
                ...
            if cols[1].button("重命名"):
                rename_conversation()
            if cols[2].button("删除", on_click=lambda: backend_del_conv(api)):
                ...

    # Display chat messages from history on app rerun
    chat_box.output_messages()
    chat_input_placeholder = "请输入对话内容，换行请使用Shift+Enter。"

    # def on_feedback(
    #         feedback,
    #         message_id: str = "",
    #         history_index: int = -1,
    # ):

    #     reason = feedback["text"]
    #     score_int = chat_box.set_feedback(feedback=feedback, history_index=history_index)
    #     api.chat_feedback(message_id=message_id,
    #                       score=score_int,
    #                       reason=reason)
    #     st.session_state["need_rerun"] = True

    # feedback_kwargs = {
    #     "feedback_type": "thumbs",
    #     "optional_text_label": "欢迎反馈您打分的理由",
    # }

    # TODO: 这里的内容有点奇怪，从后端导入Settings.model_settings.LLM_MODEL_CONFIG，然后又从前端传到后端。需要优化
    #  传入后端的内容
    llm_model_config = Settings.model_settings.LLM_MODEL_CONFIG
    chat_model_config = {key: {} for key in llm_model_config.keys()}
    for key in llm_model_config:
        if c := llm_model_config[key]:
            model = c.get("model", "").strip() or get_default_llm()
            chat_model_config[key][model] = llm_model_config[key]
    llm_model = ctx.get("llm_model")
    if llm_model is not None:
        chat_model_config["llm_model"][llm_model] = llm_model_config["llm_model"].get(
            llm_model, {}
        )

    # chat input
    with bottom():
        cols = st.columns([1, 0.2, 15,  1])
        if cols[0].button(":gear:", help="模型配置"):
            widget_keys = ["platform", "llm_model", "temperature", "system_message"]
            chat_box.context_to_session(include=widget_keys)
            llm_model_setting()
        if cols[-1].button(":wastebasket:", help="清空对话"):
            chat_box.reset_history()
            rerun()
        # with cols[1]:
        #     mic_audio = audio_recorder("", icon_size="2x", key="mic_audio")
        prompt = cols[2].chat_input(chat_input_placeholder, key="prompt")
    if prompt:
        history = get_messages_history(
            chat_model_config["llm_model"]
            .get(next(iter(chat_model_config["llm_model"])), {})
            .get("history_len", 1)
        )

        is_vision_chat = upload_image and not selected_tools

        if is_vision_chat: # multimodal chat
            chat_box.user_say([Image(upload_image_data_url, width=100), Markdown(prompt)])
        else:
            chat_box.user_say(prompt)
        if files_upload:
            if files_upload["images"]:
                st.markdown(
                    f'<img src="data:image/jpeg;base64,{files_upload["images"][0]}" width="300">',
                    unsafe_allow_html=True,
                )
            elif files_upload["videos"]:
                st.markdown(
                    f'<video width="400" height="300" controls><source src="data:video/mp4;base64,{files_upload["videos"][0]}" type="video/mp4"></video>',
                    unsafe_allow_html=True,
                )
            elif files_upload["audios"]:
                st.markdown(
                    f'<audio controls><source src="data:audio/wav;base64,{files_upload["audios"][0]}" type="audio/wav"></audio>',
                    unsafe_allow_html=True,
                )

        chat_box.ai_say("正在思考...")
        text = ""
        started = False

        client = openai.Client(
            base_url=f"{api_address()}/chat",
            api_key=st.session_state.get(auth_state.TOKEN_KEY) or "NONE",
            timeout=100000,
        )
        if is_vision_chat: # multimodal chat
            content = [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": upload_image_data_url}}
            ]
            messages = [{"role": "user", "content": content}]
        else:
            messages = history + [{"role": "user", "content": prompt}]
        tools = list(selected_tool_configs)
        if len(selected_tools) == 1:
            tool_choice = selected_tools[0]
        else:
            tool_choice = None
        # 如果 tool_input 中有空的字段，设为用户输入
        for k in tool_input:
            if tool_input[k] in [None, ""]:
                tool_input[k] = prompt

        extra_body = dict(
            metadata=files_upload,
            chat_model_config=chat_model_config,
            conversation_id=current_conv_id,
            tool_input=tool_input,
            upload_image=upload_image,
            use_mcp=use_mcp,
            # 任务 011：启用单 Agent 任务闭环（规划→多步执行→观察→判断→持久化轨迹）
            agent_run=use_agent_run,
            # 任务 015：智能对话主入口触发服务端意图路由与统一分发（智能对话总图）
            auto_agent=auto_agent,
        )
        stream = not is_vision_chat
        params = dict(
            messages=messages,
            model=llm_model,
            stream=stream, # TODO：xinference qwen-vl-chat 流式输出会出错，后续看更新
            extra_body=extra_body,
        )
        if tools:
            params["tools"] = tools
        if tool_choice:
            params["tool_choice"] = tool_choice
        if Settings.model_settings.MAX_TOKENS:
            params["max_tokens"] = Settings.model_settings.MAX_TOKENS

        # 任务 011：Agent 任务闭环（Agent Run）的流式事件展示辅助
        # （运行/计划/步骤/重规划/终态），同时把轨迹同步到 session_state，
        # 供侧边栏“取消 Agent 运行”按钮读取 run_id 与状态。
        agent_tl = {
            "run_id": None,
            "plan": None,
            "steps": {},
            "status": None,
            "final": "",
        }
        agent_announced = False  # run_created 会触发两次（pending/running），只提示一次

        def _agent_chunk_content(d):
            try:
                return d.choices[0].delta.content or ""
            except Exception:
                return getattr(d, "content", "") or ""

        def _agent_step_seq(step):
            seq = (step or {}).get("seq")
            if seq is None:
                seq = len(agent_tl["steps"]) + 1
            return seq

        def _agent_step_line(step, state):
            icon = {
                "completed": "✅",
                "failed": "❌",
                "running": "⏳",
                "cancelled": "⏹️",
            }.get(state, "•")
            line = f"{icon} 第 {step.get('seq')} 步：{step.get('goal') or ''}"
            if step.get("tool_name"):
                line += f"（工具 `{step['tool_name']}`）"
            return line

        def _agent_sync_state():
            st.session_state["agent_run"] = dict(agent_tl)

        if stream:
            try:
                for d in client.chat.completions.create(**params):
                    # import rich
                    # rich.print(d)
                    message_id = d.message_id
                    metadata = {
                        "message_id": message_id,
                    }

                    # clear initial message
                    if not started:
                        chat_box.update_msg("", streaming=False)
                        started = True

                    # 任务 015：智能对话首条事件为路由 / 执行分支（RouteResult）。
                    # 只展示执行分支与简短意图名称（如「知识库任务 · 知识库检索」），
                    # 不展示模型隐藏推理过程。
                    route_result = getattr(d, "route_result", None)
                    if route_result:
                        _branch = (
                            getattr(d, "execution_mode_label", None)
                            or route_result.get("path_label")
                            or route_result.get("path")
                        )
                        _intent = (
                            route_result.get("intent_label")
                            or route_result.get("intent")
                        )
                        st.caption(f"🧭 {_branch} · {_intent}")
                        continue

                    # 任务 011：单 Agent 任务闭环事件。Agent 事件的 status 为
                    # None，必须先于下方 status 分支处理，否则会落入普通内容
                    # 分支把事件内容当作文本追加。
                    agent_event = getattr(d, "agent_event", None)
                    if agent_event:
                        if getattr(d, "run_id", None):
                            agent_tl["run_id"] = d.run_id
                        content = _agent_chunk_content(d)
                        if agent_event == "run_created":
                            agent_tl["status"] = "running"
                            _agent_sync_state()
                            if not agent_announced:
                                agent_announced = True
                                st.info("Agent 任务运行已启动", icon="🤖")
                        elif agent_event == "plan_ready":
                            plan = getattr(d, "agent_plan", None) or {}
                            agent_tl["plan"] = plan
                            _agent_sync_state()
                            plan_steps = plan.get("steps") or []
                            plan_title = "执行计划" + (
                                f"（{plan['summary']}）" if plan.get("summary") else ""
                            )
                            with st.expander(plan_title, expanded=False):
                                if plan.get("direct_answer") and not plan_steps:
                                    st.markdown("无需执行工具，将直接回答。")
                                for i, p in enumerate(plan_steps, 1):
                                    try:
                                        args_txt = json.dumps(
                                            p.get("args") or {}, ensure_ascii=False
                                        )
                                    except Exception:
                                        args_txt = str(p.get("args"))
                                    st.markdown(
                                        f"{i}. **{p.get('goal') or ''}** "
                                        f"（工具 `{p.get('tool') or ''}`，参数 `{args_txt}`）"
                                    )
                        elif agent_event == "step_started":
                            step = dict(getattr(d, "agent_step", None) or {})
                            step["status"] = "running"
                            agent_tl["steps"][_agent_step_seq(step)] = step
                            _agent_sync_state()
                            if step.get("type") == "answer":
                                st.markdown("⏳ 正在生成最终回答…")
                            else:
                                st.markdown(_agent_step_line(step, "running"))
                        elif agent_event == "step_finished":
                            step = dict(getattr(d, "agent_step", None) or {})
                            agent_tl["steps"][_agent_step_seq(step)] = step
                            _agent_sync_state()
                            state = step.get("status") or "completed"
                            st.markdown(_agent_step_line(step, state))
                            detail = step.get("output_summary") or step.get(
                                "error_summary"
                            )
                            if step.get("type") == "answer" and detail:
                                agent_tl["final"] = detail
                            if state == "failed":
                                st.warning(detail or "步骤执行失败")
                            elif detail:
                                with st.expander("步骤输出摘要", expanded=False):
                                    st.code(detail, language=None)
                        elif agent_event == "replanned":
                            new_steps = getattr(d, "agent_steps", None) or []
                            for s in new_steps:
                                if isinstance(s, dict):
                                    agent_tl["steps"][_agent_step_seq(s)] = s
                            _agent_sync_state()
                            st.markdown(f"🔁 重新规划：新增 {len(new_steps)} 个步骤")
                            for s in new_steps:
                                if isinstance(s, dict):
                                    st.markdown(_agent_step_line(s, "pending"))
                        elif agent_event in (
                            "run_completed",
                            "run_failed",
                            "run_cancelled",
                        ):
                            agent_tl["status"] = agent_event.split("_", 1)[1]
                            if agent_event == "run_completed":
                                agent_tl["final"] = content
                            _agent_sync_state()
                            if agent_event == "run_completed":
                                chat_box.update_msg(
                                    (content or "").replace("\n", "\n\n"),
                                    streaming=False,
                                    metadata=metadata,
                                )
                                st.success("任务执行完成")
                            elif agent_event == "run_failed":
                                chat_box.update_msg(
                                    content, streaming=False, metadata=metadata
                                )
                                st.error(content or "Agent 运行失败")
                            else:
                                chat_box.update_msg(
                                    content, streaming=False, metadata=metadata
                                )
                                st.warning(content or "Agent 运行已取消")
                        continue

                    if d.status == AgentStatus.error:
                        st.error(d.choices[0].delta.content)
                    elif d.status == AgentStatus.llm_start:
                        chat_box.insert_msg("正在解读工具输出结果...")
                        text = d.choices[0].delta.content or ""
                    elif d.status == AgentStatus.llm_new_token:
                        text += d.choices[0].delta.content or ""
                        chat_box.update_msg(
                            text.replace("\n", "\n\n"), streaming=True, metadata=metadata
                        )
                    elif d.status == AgentStatus.llm_end:
                        text += d.choices[0].delta.content or ""
                        chat_box.update_msg(
                            text.replace("\n", "\n\n"), streaming=False, metadata=metadata
                        )
                    # tool 的输出与 llm 输出重复了
                    elif d.status == AgentStatus.tool_start:
                        formatted_data = {
                            "Function": d.choices[0].delta.tool_calls[0].function.name,
                            "function_input": d.choices[0].delta.tool_calls[0].function.arguments,
                        }
                        formatted_json = json.dumps(formatted_data, indent=2, ensure_ascii=False)
                        text = """\n```{}\n```\n""".format(formatted_json)
                        chat_box.insert_msg( # TODO: insert text directly not shown
                            Markdown(text, title="Function call", in_expander=True, expanded=True, state="running"))
                    elif d.status == AgentStatus.tool_end:
                        tool_output = d.choices[0].delta.tool_calls[0].tool_output
                        if d.message_type == MsgType.IMAGE:
                            for url in json.loads(tool_output).get("images", []):
                                # 判断是否携带域名
                                if not url.startswith("http"):
                                    url = f"{api.base_url}/media/{url}"
                                # md语法不支持，所以pos 跳过
                                chat_box.insert_msg(Image(url), pos=-2)
                            chat_box.update_msg(text, streaming=False, expanded=True, state="complete")
                        else:
                            text += """\n```\nObservation:\n{}\n```\n""".format(tool_output)
                            chat_box.update_msg(text, streaming=False, expanded=False, state="complete")
                    elif d.status == AgentStatus.agent_finish:
                        text = d.choices[0].delta.content or ""
                        chat_box.update_msg(text.replace("\n", "\n\n"))
                    elif d.status is None:  # not agent chat
                        if getattr(d, "is_ref", False):
                            context = str(d.tool_output)
                            if isinstance(d.tool_output, dict):
                                docs = d.tool_output.get("docs", [])
                                source_documents = format_reference(kb_name=d.tool_output.get("knowledge_base"),
                                                                    docs=docs,
                                                                    api_base_url=api_address(is_public=True))
                                context = "\n".join(source_documents)

                            chat_box.insert_msg(
                                Markdown(
                                    context,
                                    in_expander=True,
                                    state="complete",
                                    title="参考资料",
                                )
                            )
                            chat_box.insert_msg("")
                        elif getattr(d, "tool_call", None) == "text2images":  # TODO：特定工具特别处理，需要更通用的处理方式
                            for img in d.tool_output.get("images", []):
                                chat_box.insert_msg(Image(f"{api.base_url}/media/{img}"), pos=-2)
                        else:
                            text += d.choices[0].delta.content or ""
                            chat_box.update_msg(
                                text.replace("\n", "\n\n"), streaming=True, metadata=metadata
                            )
                    chat_box.update_msg(text, streaming=False, metadata=metadata)
            except Exception as e:
                st.error(e.body)
        else:
            try:
                d =client.chat.completions.create(**params)
                # 任务 015：非流式响应同样附带路由 / 执行分支信息，展示执行分支与简短意图
                if getattr(d, "route_result", None):
                    _rr = d.route_result
                    _branch = (
                        getattr(d, "execution_mode_label", None)
                        or _rr.get("path_label")
                        or _rr.get("path")
                    )
                    st.caption(
                        f"🧭 {_branch} · "
                        f"{_rr.get('intent_label') or _rr.get('intent')}"
                    )
                if getattr(d, "class_name", None) == "AgentRunResult":
                    # 任务 011：非流式 Agent 任务闭环结果（如图片对话）
                    content = ""
                    if d.choices:
                        content = d.choices[0].message.content or ""
                    content = content or (getattr(d, "content", "") or "")
                    run = getattr(d, "agent_run", None) or {}
                    run_steps = {
                        s.get("seq"): s
                        for s in (run.get("steps") or [])
                        if isinstance(s, dict)
                    }
                    st.session_state["agent_run"] = {
                        "run_id": run.get("run_id"),
                        "plan": run.get("plan"),
                        "steps": run_steps,
                        "status": run.get("status"),
                        "final": run.get("final_result") or content,
                    }
                    if run:
                        with st.expander("执行轨迹", expanded=False):
                            plan = run.get("plan") or {}
                            if plan.get("summary"):
                                st.markdown(f"计划：{plan['summary']}")
                            for s in (run.get("steps") or []):
                                if isinstance(s, dict):
                                    state = s.get("status") or "completed"
                                    line = _agent_step_line(s, state)
                                    st.markdown(line)
                                    detail = s.get("output_summary") or s.get(
                                        "error_summary"
                                    )
                                    if detail:
                                        with st.expander("步骤详情", expanded=False):
                                            st.code(detail, language=None)
                    chat_box.update_msg(content, streaming=False)
                else:
                    chat_box.update_msg(d.choices[0].message.content or "", streaming=False)
            except Exception as e:
                st.error(e.body)

        # if os.path.exists("tmp/image.jpg"):
        #     with open("tmp/image.jpg", "rb") as image_file:
        #         encoded_string = base64.b64encode(image_file.read()).decode()
        #         img_tag = (
        #             f'<img src="data:image/jpeg;base64,{encoded_string}" width="300">'
        #         )
        #         st.markdown(img_tag, unsafe_allow_html=True)
            # os.remove("tmp/image.jpg")
        # chat_box.show_feedback(**feedback_kwargs,
        #                        key=message_id,
        #                        on_submit=on_feedback,
        #                        kwargs={"message_id": message_id, "history_index": len(chat_box.history) - 1})

        # elif dialogue_mode == "文件对话":
        #     if st.session_state["file_chat_id"] is None:
        #         st.error("请先上传文件再进行对话")
        #         st.stop()
        #     chat_box.ai_say([
        #         f"正在查询文件 `{st.session_state['file_chat_id']}` ...",
        #         Markdown("...", in_expander=True, title="文件匹配结果", state="complete"),
        #     ])
        #     text = ""
        #     for d in api.file_chat(prompt,
        #                            knowledge_id=st.session_state["file_chat_id"],
        #                            top_k=kb_top_k,
        #                            score_threshold=score_threshold,
        #                            history=history,
        #                            model=llm_model,
        #                            prompt_name=prompt_template_name,
        #                            temperature=temperature):
        #         if error_msg := check_error_msg(d):
        #             st.error(error_msg)
        #         elif chunk := d.get("answer"):
        #             text += chunk
        #             chat_box.update_msg(text, element_index=0)
        #     chat_box.update_msg(text, element_index=0, streaming=False)
        #     chat_box.update_msg("\n\n".join(d.get("docs", [])), element_index=1, streaming=False)

    now = datetime.now()
    with tab2:
        cols = st.columns(2)
        export_btn = cols[0]
        if cols[1].button(
            "清空对话",
            use_container_width=True,
        ):
            chat_box.reset_history()
            rerun()

    export_btn.download_button(
        "导出记录",
        "".join(chat_box.export2md()),
        file_name=f"{now:%Y-%m-%d %H.%M}_对话记录.md",
        mime="text/markdown",
        use_container_width=True,
    )

    # st.write(chat_box.history)
