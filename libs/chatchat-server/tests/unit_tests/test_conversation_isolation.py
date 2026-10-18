"""WebUI 会话隔离离线测试（任务 005）。

覆盖 WebUI 登录/会话管理对后端隔离要求的实现：

- 会话列表、历史消息、重命名、删除均只作用于当前登录用户自己的会话；
- 未登录时所有会话管理操作必须失败（WebUI 抛出 AuthenticationError）；
- 后端对"其他用户的会话"一律返回 404，WebUI 不会把它误当成成功；
- 用户 A 无法操作/读取用户 B 的会话与消息；
- 必须改密（must_change_password=True）的用户会话管理返回 403；
- 主聊天使用其他用户的 conversation_id 在调用模型前返回 404（mock 模型）；
- owner 正确时，消息创建与回答更新使用同一会话；
- 改密成功后必须改密标志被清除，会话管理随之可用。

测试只使用临时 SQLite 与 FastAPI TestClient / httpx ASGITransport，不连接
模型、网络或真实数据库；模型相关部分使用 mock。
"""
import sys
import types

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# ---------------------------------------------------------------------------
# 在导入 chat_routes 之前，先打桩 chatchat.server.chat.kb_chat。
# kb_chat 的导入链（search_local_knowledgebase）在模块加载时会执行一次真实
# 知识库数据库读取（list_kbs），离线测试无法提供该库。主聊天隔离测试只关心
# chat_completions 处理器，不依赖 kb_chat 的实现，因此用桩替换。
# ---------------------------------------------------------------------------
_kb_stub = types.ModuleType("chatchat.server.chat.kb_chat")
_kb_stub.kb_chat = lambda *args, **kwargs: None
sys.modules["chatchat.server.chat.kb_chat"] = _kb_stub


from chatchat.server.db import session as db_session
from chatchat.server.db.base import Base
from chatchat.server.db.migrate.base import apply_migrations
from chatchat.server.db.migrate.migrations import build_registry
from chatchat.server.db.models.conversation_model import ConversationModel
from chatchat.server.db.models.message_model import MessageModel
from chatchat.server.db.models.user_model import UserModel  # noqa: F401 (注册到 Base.metadata)
from chatchat.server.db.repository import (
    add_message,
    update_message,
)
from chatchat.server.auth import jwt as auth_jwt
from chatchat.server.auth import service
from chatchat.server.api_server.chat_routes import chat_router
from chatchat.server.api_server.conversation_routes import conversation_router
from chatchat.server.auth.routes_auth import auth_router

from chatchat.webui_pages.utils import ApiRequest, AuthenticationError
from chatchat.webui_pages import utils as webui_utils


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
    """临时文件库：迁移到 v3 + 业务表（conversation/message）。"""
    path = tmp_path / "conv.db"
    path.touch()
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as conn:
        apply_migrations(build_registry(), conn)
    Base.metadata.create_all(engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    yield {"engine": engine, "Session": Session, "path": path}
    engine.dispose()


@pytest.fixture
def app(temp_user_db):
    """FastAPI 应用：会话路由 + 主聊天路由 + 认证路由，get_db 指向临时库。"""
    a = FastAPI()
    a.include_router(conversation_router)
    a.include_router(chat_router)
    a.include_router(auth_router)

    def override_get_db():
        s = temp_user_db["Session"]()
        try:
            yield s
        finally:
            s.close()

    a.dependency_overrides[db_session.get_db] = override_get_db
    return a


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _model_lookup_is_stable(monkeypatch):
    """把模型查找固定为可用，聊天测试不再依赖本机 model_settings.yaml。

    否则 `qwen:7b` 这类测试模型名在配置了真实平台的机器上会被判成未配置，
    `get_OpenAIClient` 里的 assert 直接把请求变成 500，测试结果取决于
    CHATCHAT_ROOT 指向哪里。要验证拒绝路径的用例自行覆盖它。
    """
    import chatchat.server.api_server.chat_routes as cr

    monkeypatch.setattr(cr, "get_model_info", lambda **kwargs: {"platform_name": "test"})


class _ApiBridge(TestClient):
    """TestClient 子类：兼容 ApiRequest 的 get/post/patch/delete 调用签名。

    ApiRequest 会显式传入 data=None/json=None，而 httpx 的 delete/patch 不接收
    这些参数；这里统一过滤后再委托给 httpx/TestClient。
    """

    def get(self, url, params=None, headers=None, **kw):
        return super().get(url, params=params, headers=headers)

    def post(self, url, data=None, json=None, headers=None, **kw):
        return super().post(url, data=data, json=json, headers=headers)

    def patch(self, url, data=None, json=None, headers=None, **kw):
        return super().patch(url, data=data, json=json, headers=headers)

    def delete(self, url, data=None, json=None, headers=None, **kw):
        return super().delete(url, headers=headers)


@pytest.fixture
def api_client(app, monkeypatch):
    """让 WebUI 的 ApiRequest 直连同一个 ASGI 应用（不启动网络、不依赖真实服务）。"""
    def _fake_get_httpx_client(use_async=False, **kwargs):
        return _ApiBridge(app)

    monkeypatch.setattr(webui_utils, "get_httpx_client", _fake_get_httpx_client)
    yield None


def _mk_user(temp_user_db, username, role="user", must_change_password=False):
    """在临时库创建用户，返回其非敏感字段（默认视为已改密）。"""
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
        u.must_change_password = must_change_password
        s.commit()
        return {
            "id": u.id,
            "username": u.username,
            "role": u.role,
            "auth_version": u.auth_version,
        }
    finally:
        s.close()


def _mk_api(temp_user_db, secret, username, must_change_password=False):
    """创建一个已登录的 WebUI ApiRequest（token 由服务端签发）。"""
    u = _mk_user(temp_user_db, username, must_change_password=must_change_password)
    token = auth_jwt.create_token(u["id"], u["auth_version"])
    return ApiRequest(base_url="http://api.test", token=token), u


def _create_conv(client, headers, name="t", chat_type="llm_chat"):
    r = client.post("/conversations", headers=headers, json={"name": name, "chat_type": chat_type})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _headers_of(temp_user_db, username, must_change_password=False):
    u = _mk_user(temp_user_db, username, must_change_password=must_change_password)
    return {"Authorization": f"Bearer {auth_jwt.create_token(u['id'], u['auth_version'])}"}, u


# ---------------------------------------------------------------------------
# 会话创建：自动归属当前用户；客户端不能指定 owner
# ---------------------------------------------------------------------------


def test_new_conversation_owned_by_server_not_client(client, temp_user_db, secret):
    hdr, a = _headers_of(temp_user_db, "alice")
    r = client.post(
        "/conversations",
        headers=hdr,
        json={"name": "x", "chat_type": "llm_chat", "owner_id": "attacker"},
    )
    assert r.status_code == 200
    cid = r.json()["id"]
    s = temp_user_db["Session"]()
    try:
        c = s.get(ConversationModel, cid)
        assert c is not None
        assert c.owner_id == a["id"]
        assert "owner_id" not in r.json()
    finally:
        s.close()


def test_unlogged_create_conversation_401(client):
    r = client.post("/conversations", json={"name": "x", "chat_type": "llm_chat"})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# 必须改密的普通用户：WebUI 会话管理必须失败（403）
# ---------------------------------------------------------------------------


def test_must_change_password_blocks_conversation_management(client, temp_user_db, secret):
    hdr, _ = _headers_of(temp_user_db, "bob", must_change_password=True)
    assert client.post("/conversations", headers=hdr, json={"name": "x", "chat_type": "llm_chat"}).status_code == 403
    assert client.get("/conversations", headers=hdr).status_code == 403


def test_must_change_password_api_client_raises(client, api_client, temp_user_db, secret):
    api, _ = _mk_api(temp_user_db, secret, "carol", must_change_password=True)
    with pytest.raises(Exception):
        api.list_conversations()


def test_change_password_clears_flag_and_enables_conversations(client, temp_user_db, secret):
    hdr, _ = _headers_of(temp_user_db, "dave", must_change_password=True)
    assert client.get("/conversations", headers=hdr).status_code == 403
    r = client.post(
        "/auth/change-password",
        headers=hdr,
        json={"old_password": "password12345678", "password": "newpass567890"},
    )
    assert r.status_code == 200, r.text
    s = temp_user_db["Session"]()
    try:
        u = s.query(UserModel).filter_by(username="dave").one()
        assert u.must_change_password is False
        new_token = auth_jwt.create_token(u.id, u.auth_version)
    finally:
        s.close()
    new_hdr = {"Authorization": f"Bearer {new_token}"}
    r = client.get("/conversations", headers=new_hdr)
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# A/B 列表互不可见
# ---------------------------------------------------------------------------


def test_lists_are_per_user(client, temp_user_db, secret):
    hdr_a, _ = _headers_of(temp_user_db, "alice")
    hdr_b, _ = _headers_of(temp_user_db, "bob")
    ca = _create_conv(client, hdr_a, "only-a")
    cb = _create_conv(client, hdr_b, "only-b")

    a_ids = {c["id"] for c in client.get("/conversations", headers=hdr_a).json()}
    b_ids = {c["id"] for c in client.get("/conversations", headers=hdr_b).json()}
    assert ca in a_ids and cb not in a_ids
    assert cb in b_ids and ca not in b_ids


# ---------------------------------------------------------------------------
# 按 chat_type 过滤（智能聊天 / 知识库对话各自的会话列表）
# ---------------------------------------------------------------------------


def test_list_can_be_filtered_by_chat_type(client, temp_user_db, secret):
    """不过滤时两个入口的会话混在一起，知识库对话页能打开智能聊天的会话。

    前端两个页面共用同一个侧栏组件，之前只把 chat_type 用在创建上，
    列表请求带了这个 query 参数但服务端忽略，所以知识库对话页会列出
    智能聊天的会话，选中后历史与 kb_chat 消息混成一串。
    """
    hdr, _ = _headers_of(temp_user_db, "alice")
    chat_conv = _create_conv(client, hdr, "智能聊天里的", chat_type="agent_chat")
    kb_conv = _create_conv(client, hdr, "知识库对话里的", chat_type="kb_chat")

    all_ids = {c["id"] for c in client.get("/conversations", headers=hdr).json()}
    assert {chat_conv, kb_conv} <= all_ids

    kb_ids = {
        c["id"]
        for c in client.get("/conversations?chat_type=kb_chat", headers=hdr).json()
    }
    assert kb_ids == {kb_conv}

    agent_ids = {
        c["id"]
        for c in client.get("/conversations?chat_type=agent_chat", headers=hdr).json()
    }
    assert agent_ids == {chat_conv}


def test_chat_type_filter_does_not_leak_other_users_conversations(
    client, temp_user_db, secret
):
    hdr_a, _ = _headers_of(temp_user_db, "alice")
    hdr_b, _ = _headers_of(temp_user_db, "bob")
    kb_b = _create_conv(client, hdr_b, "b 的知识库会话", chat_type="kb_chat")

    ids = {
        c["id"]
        for c in client.get("/conversations?chat_type=kb_chat", headers=hdr_a).json()
    }
    assert kb_b not in ids


# ---------------------------------------------------------------------------
# A 读取 / 重命名 / 删除 B 的会话均为 404
# ---------------------------------------------------------------------------


def test_a_cannot_read_rename_delete_b_conversation(client, temp_user_db, secret):
    hdr_a, _ = _headers_of(temp_user_db, "alice")
    hdr_b, _ = _headers_of(temp_user_db, "bob")
    cb = _create_conv(client, hdr_b, "secret")

    assert client.get(f"/conversations/{cb}", headers=hdr_a).status_code == 404
    assert client.patch(f"/conversations/{cb}", headers=hdr_a, json={"name": "hax"}).status_code == 404
    assert client.delete(f"/conversations/{cb}", headers=hdr_a).status_code == 404

    assert client.get(f"/conversations/{cb}", headers=hdr_b).status_code == 200


def test_a_cannot_rename_b_conversation_via_api(client, api_client, temp_user_db, secret):
    api_a, _ = _mk_api(temp_user_db, secret, "alice")
    api_b, _ = _mk_api(temp_user_db, secret, "bob")
    hdr_b = {"Authorization": f"Bearer {api_b.token}"}
    cb = _create_conv(client, hdr_b, "secret")
    r = api_b.rename_conversation(cb, "mine")
    assert r.get("name") == "mine"
    r2 = api_a.rename_conversation(cb, "stolen")
    assert r2.get("name") != "stolen"
    assert api_b.get_conversation(cb).get("name") == "mine"


# ---------------------------------------------------------------------------
# A 不能读取 B 的会话历史消息
# ---------------------------------------------------------------------------


def test_a_cannot_read_messages_of_b_conversation(client, temp_user_db, secret):
    hdr_a, _ = _headers_of(temp_user_db, "alice")
    hdr_b, b = _headers_of(temp_user_db, "bob")
    cb = _create_conv(client, hdr_b, "secret")
    s = temp_user_db["Session"]()
    try:
        add_message(s, cb, b["id"], "llm_chat", "q", response="a")
    finally:
        s.close()

    assert client.get(f"/conversations/{cb}/messages", headers=hdr_a).status_code == 404
    r = client.get(f"/conversations/{cb}/messages", headers=hdr_b)
    assert r.status_code == 200
    assert [m["query"] for m in r.json()["messages"]] == ["q"]


def test_a_cannot_read_b_messages_via_api(client, api_client, temp_user_db, secret):
    api_a, _ = _mk_api(temp_user_db, secret, "alice")
    api_b, b = _mk_api(temp_user_db, secret, "bob")
    hdr_b = {"Authorization": f"Bearer {api_b.token}"}
    cb = _create_conv(client, hdr_b, "secret")
    s = temp_user_db["Session"]()
    try:
        add_message(s, cb, b["id"], "llm_chat", "secret-q", response="a")
    finally:
        s.close()
    b_msgs = api_b.conversation_messages(cb)
    assert [m["query"] for m in b_msgs] == ["secret-q"]
    r = api_a.conversation_messages(cb)
    assert "messages" not in r


# ---------------------------------------------------------------------------
# 删除 A 的会话不影响 B
# ---------------------------------------------------------------------------


def test_delete_a_conversation_does_not_affect_b(client, temp_user_db, secret):
    hdr_a, a = _headers_of(temp_user_db, "alice")
    hdr_b, b = _headers_of(temp_user_db, "bob")
    ca = _create_conv(client, hdr_a, "a-conv")
    cb = _create_conv(client, hdr_b, "b-conv")
    s = temp_user_db["Session"]()
    try:
        add_message(s, ca, a["id"], "llm_chat", "qa", response="ra")
        add_message(s, cb, b["id"], "llm_chat", "qb", response="rb")
    finally:
        s.close()

    assert client.delete(f"/conversations/{ca}", headers=hdr_a).status_code == 200

    s = temp_user_db["Session"]()
    try:
        assert s.get(ConversationModel, ca) is None
        assert s.query(MessageModel).filter_by(conversation_id=ca).count() == 0
        assert s.get(ConversationModel, cb) is not None
        assert s.query(MessageModel).filter_by(conversation_id=cb).count() == 1
    finally:
        s.close()


# ---------------------------------------------------------------------------
# 消息反馈不能跨 owner
# ---------------------------------------------------------------------------


def test_a_cannot_feedback_b_message(client, temp_user_db, secret):
    hdr_a, _ = _headers_of(temp_user_db, "alice")
    hdr_b, b = _headers_of(temp_user_db, "bob")
    cb = _create_conv(client, hdr_b, "secret")
    s = temp_user_db["Session"]()
    try:
        mid = add_message(s, cb, b["id"], "llm_chat", "q", response="a")
    finally:
        s.close()

    assert client.post("/chat/feedback", headers=hdr_a, json={"message_id": mid, "score": 100}).status_code == 404
    assert client.post("/chat/feedback", headers=hdr_b, json={"message_id": mid, "score": 100}).status_code == 200


# ---------------------------------------------------------------------------
# 主聊天：其他用户 conversation_id 在模型调用前 404（mock 模型）
# ---------------------------------------------------------------------------


def test_chat_other_user_conversation_id_404_before_model(client, temp_user_db, secret, monkeypatch):
    import chatchat.server.api_server.chat_routes as cr

    model_called = {"n": 0}

    async def fake_chat(*args, **kwargs):
        model_called["n"] += 1
        return {"choices": [{"message": "should-not-reach"}]}

    monkeypatch.setattr(cr, "chat", fake_chat)

    hdr_a, _ = _headers_of(temp_user_db, "alice")
    hdr_b, _ = _headers_of(temp_user_db, "bob")
    cb = _create_conv(client, hdr_b, "secret")

    body = {
        "model": "qwen:7b",
        "stream": False,
        "messages": [{"role": "user", "content": "hi"}],
        "conversation_id": cb,
    }
    r = client.post("/chat/chat/completions", headers=hdr_a, json=body)
    assert r.status_code == 404
    assert model_called["n"] == 0

    ca = _create_conv(client, hdr_a, "mine")
    body["conversation_id"] = ca
    r = client.post("/chat/chat/completions", headers=hdr_a, json=body)
    assert r.status_code == 200
    assert r.json().get("conversation_id") == ca


def test_chat_rejects_multimodal_content_before_model(
    client, temp_user_db, secret, monkeypatch
):
    """图片解析只能走 /v1：本接口的 content 是字符串语义（标题/落库/prompt），
    收到多模态数组必须在写库和模型调用之前拒绝。"""
    import chatchat.server.api_server.chat_routes as cr

    model_called = {"n": 0}

    async def fake_chat(*args, **kwargs):
        model_called["n"] += 1
        return {"choices": [{"message": "should-not-reach"}]}

    monkeypatch.setattr(cr, "chat", fake_chat)

    hdr, _ = _headers_of(temp_user_db, "vision-alice")
    body = {
        "model": "qwen:7b",
        "stream": False,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "看这张图"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                ],
            }
        ],
    }
    r = client.post("/chat/chat/completions", headers=hdr, json=body)
    assert r.status_code == 400
    assert "/v1/chat/completions" in r.json()["detail"]
    assert model_called["n"] == 0


def test_chat_blank_model_falls_back_to_server_default(
    client, temp_user_db, secret, monkeypatch
):
    """空 model 是前端“用服务端默认模型”的哨兵，必须回落而不是撞 500。"""
    import chatchat.server.api_server.chat_routes as cr

    seen = {}

    async def fake_chat(*args, **kwargs):
        return {"choices": [{"message": "ok"}]}

    def fake_model_info(**kwargs):
        # 回落后的模型名会在配置校验这一步被查一次，从这里观察回落结果，
        # 比观察一个已经不存在的客户端构造调用更贴近真实契约。
        seen["model_name"] = kwargs.get("model_name")
        return {"platform_name": "test"}

    monkeypatch.setattr(cr, "chat", fake_chat)
    monkeypatch.setattr(cr, "get_model_info", fake_model_info)
    monkeypatch.setattr(cr, "get_default_llm", lambda: "default-llm")

    hdr, _ = _headers_of(temp_user_db, "blank-model-alice")
    r = client.post(
        "/chat/chat/completions",
        headers=hdr,
        json={
            "model": "",
            "stream": False,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert r.status_code == 200
    assert seen["model_name"] == "default-llm"


def test_chat_unknown_model_returns_400_before_touching_the_model(
    client, temp_user_db, secret, monkeypatch
):
    """未配置的模型过去会在 get_OpenAIClient 里 assert 崩成 500，还留下空会话。"""
    import chatchat.server.api_server.chat_routes as cr

    called = {"n": 0}

    async def fake_chat(*args, **kwargs):
        called["n"] += 1
        return {"choices": [{"message": "should-not-reach"}]}

    monkeypatch.setattr(cr, "chat", fake_chat)
    monkeypatch.setattr(cr, "get_model_info", lambda **kwargs: {})
    monkeypatch.setattr(
        cr, "get_config_models", lambda **kwargs: {"qwen2:7b": {}, "glm-4": {}}
    )

    hdr, user = _headers_of(temp_user_db, "unknown-model-alice")
    r = client.post(
        "/chat/chat/completions",
        headers=hdr,
        json={
            "model": "not-configured",
            "stream": False,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "未配置" in detail and "qwen2:7b" in detail
    assert called["n"] == 0

    # 校验发生在建会话之前，不能留下只有用户问题的空会话
    with temp_user_db["Session"]() as session:
        count = session.execute(
            text("SELECT COUNT(*) FROM conversation WHERE owner_id = :o"),
            {"o": user["id"]},
        ).scalar()
    assert count == 0


def test_chat_rejects_empty_messages(
    client, temp_user_db, secret, monkeypatch
):
    """空 messages 会在取 messages[-1] 时 IndexError，属于 400 不是 500。"""
    hdr, _ = _headers_of(temp_user_db, "empty-messages-alice")
    r = client.post(
        "/chat/chat/completions",
        headers=hdr,
        json={"model": "qwen:7b", "stream": False, "messages": []},
    )
    assert r.status_code == 400
    assert "messages" in r.json()["detail"]


# ---------------------------------------------------------------------------
# owner 正确时消息创建和回答更新使用同一会话
# ---------------------------------------------------------------------------


def test_create_conversation_rejects_empty_owner(temp_user_db):
    from chatchat.server.db.repository import create_conversation

    with temp_user_db["Session"]() as session:
        with pytest.raises(ValueError, match="owner_id"):
            create_conversation(session, "", "llm_chat", "invalid")


def test_add_and_update_message_same_conversation(temp_user_db, secret):
    _, a = _headers_of(temp_user_db, "alice")
    S = temp_user_db["Session"]
    s = S()
    try:
        from chatchat.server.db.repository import create_conversation, get_owned_conversation

        conv = create_conversation(s, a["id"], "llm_chat", "c")
        mid = add_message(s, conv, a["id"], "llm_chat", "q", response="")
        update_message(s, mid, a["id"], response="a")

        m = s.get(MessageModel, mid)
        assert m is not None
        assert m.conversation_id == conv
        assert m.response == "a"
        assert get_owned_conversation(s, conv, a["id"]) is not None
        from chatchat.server.db.repository.message_repository import _owned_message

        assert _owned_message(s, mid, "someone-else") is None
    finally:
        s.close()


def test_chat_owner_correct_creates_message_in_same_conversation(
    client, temp_user_db, secret, monkeypatch
):
    import chatchat.server.api_server.chat_routes as cr

    async def fake_chat(*args, **kwargs):
        return {"choices": []}

    monkeypatch.setattr(cr, "chat", fake_chat)

    hdr_a, _ = _headers_of(temp_user_db, "alice")
    ca = _create_conv(client, hdr_a, "mine")
    r = client.post(
        "/chat/chat/completions",
        headers=hdr_a,
        json={
            "model": "qwen:7b",
            "stream": False,
            "messages": [{"role": "user", "content": "hello"}],
            "conversation_id": ca,
        },
    )
    assert r.status_code == 200
    s = temp_user_db["Session"]()
    try:
        msgs = s.query(MessageModel).filter_by(conversation_id=ca).all()
        assert len(msgs) == 1
        assert msgs[0].query == "hello"
        assert r.json().get("conversation_id") == ca
    finally:
        s.close()


# ---------------------------------------------------------------------------
# WebUI 客户端：未登录时所有会话管理操作失败
# ---------------------------------------------------------------------------


def test_unlogged_api_client_fails_on_all_operations(api_client, temp_user_db, secret):
    api = ApiRequest(base_url="http://api.test", token=None)
    for op in (
        api.list_conversations,
        lambda: api.create_conversation("x", "llm_chat"),
        lambda: api.get_conversation("any-id"),
        lambda: api.rename_conversation("any-id", "x"),
        lambda: api.delete_conversation("any-id"),
        lambda: api.conversation_messages("any-id"),
    ):
        with pytest.raises(Exception):
            op()


def test_unlogged_client_list_401(client):
    assert client.get("/conversations").status_code == 401
    assert client.post("/conversations", json={"name": "x", "chat_type": "llm_chat"}).status_code == 401


def test_me_requires_login(client):
    assert client.get("/auth/me").status_code == 401


def test_me_returns_current_user(client, temp_user_db, secret):
    hdr, u = _headers_of(temp_user_db, "alice")
    r = client.get("/auth/me", headers=hdr)
    assert r.status_code == 200
    assert r.json()["username"] == "alice"


# ---------------------------------------------------------------------------
# WebUI 客户端：会话管理只作用于自己的会话（通过 ApiRequest 完整闭环）
# ---------------------------------------------------------------------------


def test_api_client_full_lifecycle_only_own(api_client, temp_user_db, secret):
    api, u = _mk_api(temp_user_db, secret, "alice")
    conv = api.create_conversation("my-conv", "llm_chat")
    cid = conv["id"]
    assert cid
    assert any(c["id"] == cid for c in api.list_conversations())
    r = api.rename_conversation(cid, "renamed")
    assert r.get("name") == "renamed"
    api.delete_conversation(cid)
    assert not any(c["id"] == cid for c in api.list_conversations())
    s = temp_user_db["Session"]()
    try:
        assert s.get(ConversationModel, cid) is None
    finally:
        s.close()


# ---------------------------------------------------------------------------
# WebUI 客户端：无法操作/读取其他用户的会话
# ---------------------------------------------------------------------------


def test_api_client_cannot_touch_other_users_conversation(client, api_client, temp_user_db, secret):
    api_a, _ = _mk_api(temp_user_db, secret, "alice")
    api_b, b = _mk_api(temp_user_db, secret, "bob")
    cb = api_b.create_conversation("b-secret", "llm_chat")["id"]

    r = api_a.get_conversation(cb)
    assert r.get("id") != cb or "detail" in r

    api_a.rename_conversation(cb, "stolen")
    assert api_b.get_conversation(cb).get("name") == "b-secret"

    api_a.delete_conversation(cb)
    assert api_b.get_conversation(cb).get("id") == cb

    s = temp_user_db["Session"]()
    try:
        add_message(s, cb, b["id"], "llm_chat", "b-q", response="b-a")
    finally:
        s.close()
    r = api_a.conversation_messages(cb)
    assert "messages" not in r


def test_unlogged_chat_completions_401(client):
    r = client.post(
        "/chat/chat/completions",
        json={"model": "qwen:7b", "stream": False, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# 前端重构 B8：历史消息接口返回反馈字段，供前端反馈 UI 显示当前状态
# ---------------------------------------------------------------------------


def test_messages_endpoint_exposes_feedback_fields(client, temp_user_db, secret):
    hdr, a = _headers_of(temp_user_db, "carol")
    cid = _create_conv(client, hdr, "fb")
    s = temp_user_db["Session"]()
    try:
        mid = add_message(s, cid, a["id"], "llm_chat", "q", response="a")
    finally:
        s.close()

    r = client.get(f"/conversations/{cid}/messages", headers=hdr)
    assert r.status_code == 200
    msg = r.json()["messages"][0]
    assert msg["feedback_score"] == -1
    assert msg["feedback_reason"] == ""

    fb = client.post(
        "/chat/feedback",
        headers=hdr,
        json={"message_id": mid, "score": 100, "reason": "很有帮助"},
    )
    assert fb.status_code == 200, fb.text

    r = client.get(f"/conversations/{cid}/messages", headers=hdr)
    msg = r.json()["messages"][0]
    assert msg["feedback_score"] == 100
    assert msg["feedback_reason"] == "很有帮助"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
