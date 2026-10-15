"""kb_chat 会话生命周期离线回归测试。

背景（线上缺陷，SQLAlchemy 2.0 + FastAPI 0.109）：

- SQLAlchemy 2.0 的 ``Session.close()`` 会 detach 会话内所有 ORM 实例；
- FastAPI 在路由函数返回响应对象（``EventSourceResponse``）时即拆除依赖
  栈，``get_db`` 的请求会话此时已关闭，SSE 流式生成器随后才开始运行；
- ``expire_on_commit=True`` 下，``create_conversation`` / ``add_message``
  提交后 ``user`` 实例过期，生成器中第一次读取 ``user.id``
  （auto_extract / memory_system_messages / update_message）即抛
  ``DetachedInstanceError``：自动记忆提取被静默吞掉、助手回复未持久化、
  用户收到错误 chunk；
- 非流式路径 ``__anext__()`` 只消费第一个 chunk 就丢弃生成器，
  ``update_message`` 永不执行，回复直接丢失。

修复：endpoint 主体在会话仍打开时捕获 ``user_id = user.id``，生成器全程
使用 ``user_id``（temp_kb 检索只把 user_id 字符串交给线程池，属主校验
用纯字符串比较）；非流式路径完整消费生成器。

本测试用临时 SQLite + 假 LLM + TestClient 走真实 FastAPI 依赖栈
（get_db 覆盖 + 真实 ``require_password_changed`` 认证），完整复现
"流式生成器开始时请求会话已关闭"的生产拆除时序，并断言：

- 流式路径无错误 chunk，助手回复完整持久化（含流式拼接的全文）；
- ``memory_service.auto_extract`` / ``summarize_conversation`` /
  ``memory_system_messages`` 都以真实用户 ID 被调用（修复前静默失败）；
- 非流式路径返回完整回复并持久化（修复前回复丢失）；
- return_direct 路径不触发 detached 错误，检索元数据正常持久化；
- temp_kb 路径只向线程池传递 user_id 字符串（属主校验为纯字符串比较），
  不再有 ORM 实例跨线程读取。

测试不连接模型、网络或真实数据库；模型、检索、记忆服务均用替身。
"""
import asyncio  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402
import types  # noqa: E402

# ---------------------------------------------------------------------------
# 导入桩：必须在导入真实 chatchat.server.chat.kb_chat 之前设置。
# 1) chatchat.server.agent.tools_factory 的 __init__ 在导入期执行
#    list_kbs()（真实知识库库读取），离线无法提供；kb_chat 只用到其中的
#    search_internet.search_engine，因此桩掉包与该子模块。
# 2) 其它单测（test_conversation_isolation / test_intent_routing 等）在
#    同进程中把 kb_chat 替换为桩，这里先移除再加载真实模块。
# ---------------------------------------------------------------------------
_pkg_stub = types.ModuleType("chatchat.server.agent.tools_factory")
_pkg_stub.__path__ = []
sys.modules.setdefault("chatchat.server.agent.tools_factory", _pkg_stub)
_si_stub = types.ModuleType("chatchat.server.agent.tools_factory.search_internet")
_si_stub.search_engine = lambda *a, **k: {"docs": []}
sys.modules.setdefault("chatchat.server.agent.tools_factory.search_internet", _si_stub)

sys.modules.pop("chatchat.server.chat.kb_chat", None)

import pytest  # noqa: E402
from fastapi import APIRouter, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sse_starlette.sse import AppStatus  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from typing import List  # noqa: E402

from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402

import chatchat.server.chat.kb_chat as kb_chat_mod  # noqa: E402
from chatchat.server.db import session as db_session  # noqa: E402
from chatchat.server.db.base import Base  # noqa: E402
from chatchat.server.db.migrate.base import apply_migrations  # noqa: E402
from chatchat.server.db.migrate.migrations import build_registry  # noqa: E402
from chatchat.server.db.models.conversation_model import ConversationModel  # noqa: E402
from chatchat.server.db.models.message_model import MessageModel  # noqa: E402
from chatchat.server.db.models.user_model import UserModel  # noqa: F401 (注册到 Base.metadata)
from chatchat.server.knowledge_base.temp_files import temp_file_ownership  # noqa: E402
from chatchat.server.auth import jwt as auth_jwt  # noqa: E402
from chatchat.server.auth import service  # noqa: E402
from chatchat.server.memory import context_package  # noqa: E402


FAKE_LLM_TOKENS = ["你", "好", "！"]
FAKE_LLM_ANSWER = "".join(FAKE_LLM_TOKENS)
TEST_KB_NAME = "test_kb"
TEMP_KB_ID = "test-temp-kb"


# ---------------------------------------------------------------------------
# 替身：假 LLM / 检索 / 记忆
# ---------------------------------------------------------------------------


class FakeStreamingChatModel(BaseChatModel):
    """离线假 LLM：异步生成逐 token 触发 on_llm_new_token。

    真实 ChatOpenAI 在 ``_agenerate`` 中随网络流式触发 on_llm_new_token，
    token 到达之间有间隔，kb_chat 的 AsyncIteratorCallbackHandler 会逐个
    消费。这里复现同样的回调链路，并在 token 之间加入短间隔（含最后一个
    token 之后），让消费者总能先取走 token、晚于 on_llm_end 置位 done——
    否则 LLM 瞬间完成时 aiter 的单轮 wait 会同时看到"队列有值"和
    "done 已置位"，随机丢弃 token，测试不再确定。
    """

    tokens: List[str] = FAKE_LLM_TOKENS
    token_gap: float = 0.02

    @property
    def _llm_type(self):
        return "fake-streaming"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=FAKE_LLM_ANSWER))]
        )

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        if run_manager is not None:
            for token in self.tokens:
                await run_manager.on_llm_new_token(token, verbose=self.verbose)
                await asyncio.sleep(self.token_gap)
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=FAKE_LLM_ANSWER))]
        )


class MemoryRecorder:
    """memory_service 替身：记录调用参数，返回固定记忆消息。"""

    def __init__(self):
        self.auto_extract_calls = []
        self.summarize_calls = []
        self.memory_messages_calls = []
        self.memory_messages = []

    def auto_extract(self, session, owner_id, query, **kwargs):
        self.auto_extract_calls.append((owner_id, query, kwargs))

    def summarize_conversation(self, session, owner_id, conversation_id):
        self.summarize_calls.append((owner_id, conversation_id))

    def memory_system_messages(self, session, owner_id, query, conversation_id, **kwargs):
        # kwargs 里带 use_memory（"本次不使用个人偏好"），一并记录以便断言透传
        self.memory_messages_calls.append((owner_id, conversation_id, kwargs))
        return list(self.memory_messages)


class ContextPackageRecorder:
    """context_package 替身：记录统一上下文构建的调用参数。

    第四期起知识库对话的最近历史、会话摘要与记忆注入段都由构建器一次给出
    （设计方案 §18.2.2/§18.2.5），因此"记忆层以真实用户 ID 被调用"和
    "use_memory 透传"这两条断言都落在构建调用上。
    """

    def __init__(self, memory_recorder: MemoryRecorder):
        self.memory_recorder = memory_recorder
        self.build_calls = []

    def build_context_package(self, session, owner_id, conversation_id, query="", **kwargs):
        self.build_calls.append((owner_id, conversation_id, kwargs))
        return context_package.ContextPackage(
            owner_id=owner_id or "",
            conversation_id=conversation_id or "",
            memory_segments=[m["content"] for m in self.memory_recorder.memory_messages],
        )


class _FakeKBService:
    def check_embed_model(self):
        return True, ""


class _FakeKBServiceFactory:
    @staticmethod
    def get_service_by_name(name):
        return _FakeKBService()


def _fake_docs(page_content):
    return [
        {
            "page_content": page_content,
            "metadata": {"source": "test-file.md", "filename": "test-file.md", "page": 1},
            "score": 0.8,
        }
    ]


def fake_search_docs(query=None, knowledge_base_name=None, top_k=3,
                     score_threshold=0.2, file_name="", metadata=None, **kwargs):
    return _fake_docs("测试知识：这是来自知识库的检索结果。")


def fake_search_temp_docs(kb_name, user_id=None, query=None, top_k=3,
                          score_threshold=0.2, **kwargs):
    """search_temp_docs_by_user_id 替身。

    修复后线程池 worker 只收到 user_id 字符串，不再接触 ORM 实例；
    这里记录 (kb_name, user_id)，断言属主身份确实跨边界传到了 worker。
    """
    TEMP_DOC_CALLS.append((kb_name, user_id))
    return _fake_docs("测试知识：这是来自临时文件库的检索结果。")


TEMP_DOC_CALLS = []


def fake_run_in_threadpool(fn, *args, **kwargs):
    async def _call():
        return fn(*args, **kwargs)

    return _call()


def fake_get_chat_openai(model_name=None, temperature=None, max_tokens=None,
                         callbacks=None, **kwargs):
    return FakeStreamingChatModel(callbacks=callbacks or [])


def fake_get_prompt_template(category, name):
    assert category == "rag"
    return "请根据以下资料回答问题。\n【资料】\n{context}\n【问题】\n{question}"


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def secret(monkeypatch):
    """设置一个 >=32 字节的测试密钥（非真实密钥）。"""
    key = "unit-test-secret-key" + "0" * 20
    monkeypatch.setenv("CHATCHAT_AUTH_SECRET", key)
    return key


@pytest.fixture
def temp_user_db(tmp_path):
    """临时文件库：迁移 + 业务表（user/conversation/message 等）。"""
    path = tmp_path / "kbchat.db"
    path.touch()
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as conn:
        apply_migrations(build_registry(), conn)
    Base.metadata.create_all(engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    yield {"engine": engine, "Session": Session, "path": path}
    engine.dispose()


@pytest.fixture
def kbchat_app(temp_user_db, monkeypatch):
    """FastAPI 应用：注册真实 kb_chat endpoint，依赖结构与生产一致。

    get_db 指向临时库；认证走真实 require_password_changed（JWT 由服务端
    签发）。模型 / 检索 / 记忆服务 / 线程池均替换为离线替身。
    """
    a = FastAPI()
    router = APIRouter()
    router.post("/kb_chat")(kb_chat_mod.kb_chat)
    a.include_router(router)

    def override_get_db():
        s = temp_user_db["Session"]()
        try:
            yield s
        finally:
            s.close()

    a.dependency_overrides[db_session.get_db] = override_get_db

    monkeypatch.setattr(kb_chat_mod, "get_ChatOpenAI", fake_get_chat_openai)
    monkeypatch.setattr(kb_chat_mod, "search_docs", fake_search_docs)
    monkeypatch.setattr(kb_chat_mod, "search_temp_docs_by_user_id", fake_search_temp_docs)
    monkeypatch.setattr(kb_chat_mod, "run_in_threadpool", fake_run_in_threadpool)
    monkeypatch.setattr(kb_chat_mod, "get_prompt_template", fake_get_prompt_template)
    monkeypatch.setattr(kb_chat_mod, "check_embed_model", lambda: (True, ""))
    monkeypatch.setattr(kb_chat_mod, "KBServiceFactory", _FakeKBServiceFactory)
    recorder = MemoryRecorder()
    recorder.memory_messages = [{"role": "system", "content": "用户偏好：回答要简短。"}]
    monkeypatch.setattr(kb_chat_mod, "memory_service", recorder)
    packages = ContextPackageRecorder(recorder)
    monkeypatch.setattr(kb_chat_mod, "context_package", packages)
    a.state.memory = recorder
    a.state.context = packages
    return a


@pytest.fixture
def client(kbchat_app):
    # sse-starlette 的 AppStatus.should_exit_event 是"首次使用时创建"的
    # 模块级单例，会绑定到首个 TestClient portal 的事件循环；本文件每个
    # 测试使用独立 TestClient（独立 portal/事件循环），不重置的话第二个
    # 及之后的测试会在 listen_for_exit_signal 处抛 "bound to a different
    # event loop"。按该类自身的设计（None 表示未创建）重置即可。
    AppStatus.should_exit = False
    AppStatus.should_exit_event = None
    with TestClient(kbchat_app) as test_client:
        yield test_client


def _mk_user(temp_user_db, username, role="user"):
    """在临时库创建用户，返回 (id, user 实例)。默认视为已完成改密。"""
    S = temp_user_db["Session"]
    s = S()
    try:
        u = service.create_user(
            s,
            username=username,
            display_name=username.title(),
            password="password12345678",
            role=role,
        )
        u.must_change_password = False
        s.commit()
        return u.id, u
    finally:
        s.close()


def _headers_of(temp_user_db, username):
    uid, _ = _mk_user(temp_user_db, username)
    return {"Authorization": f"Bearer {auth_jwt.create_token(uid, _auth_version(temp_user_db, username))}"}, uid


def _auth_version(temp_user_db, username):
    s = temp_user_db["Session"]()
    try:
        u = s.query(UserModel).filter_by(username=username).one()
        return u.auth_version
    finally:
        s.close()


def _kb_chat_body(**overrides):
    body = {
        "query": "知识库里写了什么？",
        "mode": "local_kb",
        "kb_name": TEST_KB_NAME,
        "top_k": 3,
        "score_threshold": 0.2,
        "history": [],
        "stream": True,
        "model": "fake-llm",
        "temperature": 0.1,
        "max_tokens": 128,
        "prompt_name": "default",
        "return_direct": False,
    }
    body.update(overrides)
    return body


def _iter_sse_chunks(stream_response):
    """解析 SSE 响应中的 data 行，返回 JSON 负载列表。"""
    chunks = []
    for line in stream_response.iter_lines():
        if line.startswith("data:"):
            payload = line[len("data:"):].strip()
            if payload:
                chunks.append(json.loads(payload))
    return chunks


def _chunk_content(chunk: dict) -> str:
    """从 OpenAIChatOutput 负载中提取文本内容。

    流式 chunk 的内容在 choices[0].delta.content；非流式/完成包在
    choices[0].message.content（model_dump 的 OpenAI 兼容格式）。
    """
    for choice in chunk.get("choices") or []:
        part = choice.get("delta") or choice.get("message") or {}
        return part.get("content") or ""
    return chunk.get("content") or ""


def _assert_no_error_chunks(chunks):
    for chunk in chunks:
        assert not chunk.get("error"), f"SSE 负载含错误：{chunk.get('error')}"


# ---------------------------------------------------------------------------
# 流式路径：会话拆除后生成器仍正常工作（核心回归）
# ---------------------------------------------------------------------------


def test_streaming_kb_chat_persists_reply_after_session_close(client, kbchat_app, temp_user_db, secret):
    """修复前：DetachedInstanceError → 错误 chunk + 回复未持久化 + 记忆静默失败。"""
    hdr, uid = _headers_of(temp_user_db, "alice")
    with client.stream("POST", "/kb_chat", headers=hdr, json=_kb_chat_body()) as resp:
        assert resp.status_code == 200
        chunks = _iter_sse_chunks(resp)

    assert chunks, "未收到任何 SSE 数据负载"
    _assert_no_error_chunks(chunks)

    # 首包为检索结果包（delta.content 为空），其后为逐 token 流式 chunk
    first = chunks[0]
    assert first.get("retrieval_status") == "matched"
    answer = "".join(_chunk_content(c) for c in chunks)
    assert answer == FAKE_LLM_ANSWER

    # 助手回复完整持久化，归属当前用户会话
    s = temp_user_db["Session"]()
    try:
        msgs = s.query(MessageModel).all()
        assert len(msgs) == 1
        m = msgs[0]
        assert m.response == FAKE_LLM_ANSWER
        conv = s.get(ConversationModel, m.conversation_id)
        assert conv is not None
        assert conv.owner_id == uid
    finally:
        s.close()

    # 记忆链路全部以真实用户 ID 被调用（修复前 user.id 读取即抛异常，
    # auto_extract / 统一上下文构建被 try/except 静默吞掉）。第四期起会话摘要
    # 与记忆注入段由 context_package 一次给出，不再单独调 summarize。
    mem = kbchat_app.state.memory
    assert [c[0] for c in mem.auto_extract_calls] == [uid]
    assert [c[0] for c in kbchat_app.state.context.build_calls] == [uid]


def test_kb_chat_passes_use_memory_flag_to_memory_layer(
    client, kbchat_app, temp_user_db, secret
):
    """请求体的 use_memory=false（界面"本次不使用个人偏好"）必须到达记忆层。

    记忆注入只影响"怎么解释和呈现"，检索与引用不受影响，因此这里只断言
    透传本身与回答仍然正常产出。
    """
    hdr, uid = _headers_of(temp_user_db, "alice")
    with client.stream(
        "POST", "/kb_chat", headers=hdr, json=_kb_chat_body(use_memory=False)
    ) as resp:
        assert resp.status_code == 200
        chunks = _iter_sse_chunks(resp)

    _assert_no_error_chunks(chunks)
    builds = kbchat_app.state.context.build_calls
    assert [c[0] for c in builds] == [uid]
    assert builds[0][2].get("use_memory") is False
    # 写入侧不受该开关影响
    mem = kbchat_app.state.memory
    assert [c[0] for c in mem.auto_extract_calls] == [uid]


def test_streaming_kb_chat_existing_conversation_id(client, kbchat_app, temp_user_db, secret):
    """已有会话（先提交过一次）时，生成器中 user 实例必然已过期，
    修复前同样触发 DetachedInstanceError。"""
    from chatchat.server.db.repository import create_conversation

    hdr, uid = _headers_of(temp_user_db, "bob")
    s = temp_user_db["Session"]()
    try:
        conv_id = create_conversation(s, uid, "kb_chat", "已有会话")
    finally:
        s.close()

    with client.stream(
        "POST", "/kb_chat", headers=hdr,
        json=_kb_chat_body(conversation_id=conv_id),
    ) as resp:
        assert resp.status_code == 200
        chunks = _iter_sse_chunks(resp)

    _assert_no_error_chunks(chunks)
    answer = "".join(_chunk_content(c) for c in chunks)
    assert answer == FAKE_LLM_ANSWER

    s = temp_user_db["Session"]()
    try:
        msgs = s.query(MessageModel).filter_by(conversation_id=conv_id).all()
        assert len(msgs) == 1
        assert msgs[0].response == FAKE_LLM_ANSWER
        conv = s.get(ConversationModel, conv_id)
        assert conv is not None
        assert conv.owner_id == uid
    finally:
        s.close()


# ---------------------------------------------------------------------------
# 非流式路径：完整消费生成器，回复不再丢失
# ---------------------------------------------------------------------------


def test_non_stream_kb_chat_returns_and_persists_full_reply(client, kbchat_app, temp_user_db, secret):
    """修复前：__anext__() 只取首包即丢弃生成器，update_message 不执行，
    返回首包（无内容）、数据库中 response 永远为空。"""
    hdr, uid = _headers_of(temp_user_db, "carol")
    r = client.post("/kb_chat", headers=hdr, json=_kb_chat_body(stream=False))
    assert r.status_code == 200
    payload = r.json()
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert not payload.get("error"), f"响应含错误：{payload.get('error')}"
    assert _chunk_content(payload) == FAKE_LLM_ANSWER
    assert payload.get("retrieval_status") == "matched"

    s = temp_user_db["Session"]()
    try:
        msgs = s.query(MessageModel).all()
        assert len(msgs) == 1
        assert msgs[0].response == FAKE_LLM_ANSWER
        conv = s.get(ConversationModel, msgs[0].conversation_id)
        assert conv is not None
        assert conv.owner_id == uid
    finally:
        s.close()


# ---------------------------------------------------------------------------
# return_direct 路径：不触发 detached 错误，检索元数据持久化
# ---------------------------------------------------------------------------


def test_return_direct_persists_metadata_without_detached_error(client, kbchat_app, temp_user_db, secret):
    """修复前：return_direct 分支的 update_message(owner_id=user.id) 在
    已关闭会话上抛 DetachedInstanceError，被外层 catch 转成错误 chunk。"""
    hdr, uid = _headers_of(temp_user_db, "dave")
    with client.stream(
        "POST", "/kb_chat", headers=hdr,
        json=_kb_chat_body(return_direct=True),
    ) as resp:
        assert resp.status_code == 200
        chunks = _iter_sse_chunks(resp)

    assert len(chunks) == 1
    chunk = chunks[0]
    _assert_no_error_chunks([chunk])
    assert chunk.get("retrieval_status") == "matched"
    assert chunk.get("object") == "chat.completion"

    s = temp_user_db["Session"]()
    try:
        msgs = s.query(MessageModel).all()
        assert len(msgs) == 1
        m = msgs[0]
        assert m.response == ""
        meta = m.meta_data
        assert meta.get("retrieval_status") == "matched"
        assert meta.get("mode") == "local_kb"
        assert meta.get("knowledge_base") == TEST_KB_NAME
        conv = s.get(ConversationModel, m.conversation_id)
        assert conv is not None
        assert conv.owner_id == uid
    finally:
        s.close()


# ---------------------------------------------------------------------------
# temp_kb 路径：只向线程池传递 user_id，属主身份完整送达 worker
# ---------------------------------------------------------------------------


def test_temp_kb_mode_passes_user_id_to_threadpool(client, kbchat_app, temp_user_db, secret):
    """修复前把 detached 的 user ORM 实例传入线程池，worker 内读取
    user.id 抛 DetachedInstanceError（属主校验即失败）；修复后 worker
    只收到 user_id 字符串，属主校验用纯字符串比较。"""
    hdr, uid = _headers_of(temp_user_db, "erin")

    s = temp_user_db["Session"]()
    try:
        u = s.query(UserModel).filter_by(username="erin").one()
    finally:
        s.close()
    temp_file_ownership.register(TEMP_KB_ID, u)
    # 测试结束清掉注册，避免影响同进程其它测试
    try:
        TEMP_DOC_CALLS.clear()
        with client.stream(
            "POST", "/kb_chat", headers=hdr,
            json=_kb_chat_body(mode="temp_kb", kb_name=TEMP_KB_ID),
        ) as resp:
            assert resp.status_code == 200
            chunks = _iter_sse_chunks(resp)
    finally:
        temp_file_ownership.remove(TEMP_KB_ID)

    _assert_no_error_chunks(chunks)
    answer = "".join(_chunk_content(c) for c in chunks)
    assert answer == FAKE_LLM_ANSWER

    assert TEMP_DOC_CALLS == [(TEMP_KB_ID, uid)], (
        f"search_temp_docs_by_user_id 未按属主身份执行：{TEMP_DOC_CALLS}"
    )

    s = temp_user_db["Session"]()
    try:
        msgs = s.query(MessageModel).all()
        assert len(msgs) == 1
        assert msgs[0].response == FAKE_LLM_ANSWER
        conv = s.get(ConversationModel, msgs[0].conversation_id)
        assert conv is not None
        assert conv.owner_id == uid
    finally:
        s.close()


def test_temp_kb_mode_rejects_other_users_temp_kb(client, kbchat_app, temp_user_db, secret):
    """属主校验在 endpoint 主体执行（会话仍打开）：他人临时库一律 404。"""
    # 属主是 gina；frank 访问该临时库 → 404
    _gina_uid, gina = _mk_user(temp_user_db, "gina")
    temp_file_ownership.register(TEMP_KB_ID, gina)
    try:
        hdr, _ = _headers_of(temp_user_db, "frank")
        r = client.post(
            "/kb_chat", headers=hdr,
            json=_kb_chat_body(mode="temp_kb", kb_name=TEMP_KB_ID, stream=False),
        )
        assert r.status_code == 404
    finally:
        temp_file_ownership.remove(TEMP_KB_ID)

    s = temp_user_db["Session"]()
    try:
        assert s.query(MessageModel).count() == 0
    finally:
        s.close()


def test_kb_chat_tags_new_conversation_with_own_project(
    client, kbchat_app, temp_user_db, secret
):
    """新建知识库对话时带上 project_id：会话被记入该项目空间（第三期 §4.4）。

    项目范围记忆只在选定该项目的会话里召回，因此会话与项目的关联必须由
    请求建立，而不是靠前端事后补写。
    """
    from chatchat.server.db.repository import research_repository as research_repo

    hdr, uid = _headers_of(temp_user_db, "iris")
    s = temp_user_db["Session"]()
    try:
        project = research_repo.create_project(s, uid, "我的项目", "研究问题")
        project_id = project.id
    finally:
        s.close()

    with client.stream(
        "POST", "/kb_chat", headers=hdr, json=_kb_chat_body(project_id=project_id)
    ) as resp:
        assert resp.status_code == 200
        _assert_no_error_chunks(_iter_sse_chunks(resp))

    s = temp_user_db["Session"]()
    try:
        conv = s.query(ConversationModel).order_by(ConversationModel.create_time).all()[-1]
        assert conv.project_id == project_id
    finally:
        s.close()


def test_kb_chat_rejects_foreign_project_without_creating_anything(
    client, kbchat_app, temp_user_db, secret
):
    """project_id 属于别人 → 404，且不建会话、不写消息。"""
    from chatchat.server.db.repository import research_repository as research_repo

    _owner_uid, _owner = _mk_user(temp_user_db, "owner")
    hdr, _uid = _headers_of(temp_user_db, "intruder")
    s = temp_user_db["Session"]()
    try:
        project = research_repo.create_project(s, _owner_uid, "别人的项目", "研究问题")
        project_id = project.id
    finally:
        s.close()

    r = client.post(
        "/kb_chat",
        headers=hdr,
        json=_kb_chat_body(project_id=project_id, stream=False),
    )
    assert r.status_code == 404

    s = temp_user_db["Session"]()
    try:
        assert s.query(ConversationModel).count() == 0
        assert s.query(MessageModel).count() == 0
    finally:
        s.close()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))