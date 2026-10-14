"""``kb_routes.kb_chat_endpoint`` 必须显式转发 ``temporary``（第二期 §4.4 回归）。

缺陷：``kb_chat`` 的形参写成 ``temporary: bool = Body(False, ...)``，那个默认值
只有在 FastAPI 解析请求时才有效。``kb_chat_endpoint`` 是直接调用 ``kb_chat``
的普通函数调用，没传该参数时拿到的是整个 ``FieldInfo`` 对象，而 ``kb_chat``
内部按 ``bool(temporary)`` 判断——对象恒为真，于是所有不带 ``conversation_id``
的知识库对话都被建成临时会话：会话上下文、"本会话已确认事项"抽屉与跨会话
召回全部对 RAG 会话失效（临时对话按设计不保存上下文）。

这里用最小 FastAPI 应用 + ``kb_chat`` 替身，钉住转发到记忆/会话层的参数形状，
不触碰知识库、模型或真实数据库。
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from chatchat.server.api_server import kb_routes
from chatchat.server.api_server.api_schemas import OpenAIChatInput  # noqa: F401
from chatchat.server.auth.deps import require_password_changed
from chatchat.server.db import session as db_session
from chatchat.server.utils import BaseResponse


class _FakeUser:
    id = "user-1"
    username = "alice"
    must_change_password = False


def _build_app(monkeypatch):
    calls = []

    async def fake_kb_chat(**kwargs):
        calls.append(kwargs)
        return BaseResponse()

    monkeypatch.setattr(kb_routes, "kb_chat", fake_kb_chat)

    app = FastAPI()
    app.post("/{mode}/{param}/chat/completions")(kb_routes.kb_chat_endpoint)
    app.dependency_overrides[require_password_changed] = lambda: _FakeUser()
    app.dependency_overrides[db_session.get_db] = lambda: None
    return app, calls


def _post(client, extra):
    body = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "测试问题"}],
        **extra,
    }
    return client.post("/local_kb/kb1/chat/completions", json=body)


def test_temporary_defaults_to_false(monkeypatch):
    """不带 temporary 的知识库对话不是临时对话（修复前恒为临时）。"""
    app, calls = _build_app(monkeypatch)
    with TestClient(app) as client:
        assert _post(client, {}).status_code == 200
    assert "temporary" in calls[0]
    assert bool(calls[0]["temporary"]) is False


def test_temporary_true_is_forwarded(monkeypatch):
    app, calls = _build_app(monkeypatch)
    with TestClient(app) as client:
        assert _post(client, {"temporary": True}).status_code == 200
    assert bool(calls[0]["temporary"]) is True


def test_conversation_id_and_use_memory_forwarded(monkeypatch):
    """会话 id 与"本次不使用个人偏好"必须原样到达 kb_chat。"""
    app, calls = _build_app(monkeypatch)
    with TestClient(app) as client:
        assert (
            _post(client, {"conversation_id": "conv-9", "use_memory": False}).status_code
            == 200
        )
    assert calls[0]["conversation_id"] == "conv-9"
    assert calls[0]["use_memory"] is False
