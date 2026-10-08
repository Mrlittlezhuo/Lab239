"""临时文件与聊天附件用户隔离离线测试（任务 006）。

覆盖：

- 临时文件（文件对话）：用户 A 上传后取得 temp_id；用户 B 无法检索 / 对话 /
  删除 / 复用 A 的 temp_id（一律 404，不泄露存在性）；A 自己可以正常对话；
- 临时文件未登录 401；必须改密用户 403；
- 复用他人 prev_id 不能把 A 的目录重归属给 B；
- 聊天图片/附件（OpenAI 兼容 /v1/files）：A 上传后 B 的列表为空，读取/下载/
  删除 A 的文件 404，A 自己可见；未登录 401；
- 用户身份一律来自服务端认证上下文，客户端不能指定 owner。

只使用临时 SQLite、临时目录与 FastAPI TestClient；模型/向量用离线桩，不连接
网络、模型或真实数据库。
"""
import io
import sys
import types
from typing import List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.language_models import BaseLLM
from langchain_core.embeddings import Embeddings
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# ---------------------------------------------------------------------------
# 在导入 chat_routes / kb_routes 之前打桩 kb_chat：其导入链（search_local_
# knowledgebase）在模块加载时读取真实知识库库，离线无法提供。本测试只关心
# file_chat / search_temp_docs / upload_temp_docs 与 /v1/files，不依赖 kb_chat。
# ---------------------------------------------------------------------------
_kb_stub = types.ModuleType("chatchat.server.chat.kb_chat")
_kb_stub.kb_chat = lambda *args, **kwargs: None
sys.modules["chatchat.server.chat.kb_chat"] = _kb_stub


from chatchat.settings import Settings
from chatchat.server import chat as _chat_pkg  # noqa: F401  (注册子模块命名空间)
from chatchat.server.chat import file_chat as file_chat_mod
from chatchat.server.knowledge_base.kb_cache import faiss_cache as _faiss_cache
from chatchat.server.knowledge_base.kb_cache.faiss_cache import memo_faiss_pool
from chatchat.server.knowledge_base.temp_files import temp_file_ownership
from chatchat.server.api_server.chat_routes import chat_router
from chatchat.server.api_server.kb_routes import kb_router
from chatchat.server.api_server.openai_routes import openai_router
from chatchat.server.auth.routes_auth import auth_router
from chatchat.server.db import session as db_session
from chatchat.server.db.base import Base
from chatchat.server.db.migrate.base import apply_migrations
from chatchat.server.db.migrate.migrations import build_registry
from chatchat.server.db.models.user_model import (  # noqa: F401  (注册到 Base.metadata)
    OpenAIFileModel,
    UserModel,
)
from chatchat.server.auth import jwt as auth_jwt
from chatchat.server.auth import service


# ---------------------------------------------------------------------------
# 离线模型/向量桩
# ---------------------------------------------------------------------------


class _FakeEmbedding(Embeddings):
    """可哈希、可比较的确定性假嵌入（dim=8）。"""

    _dim = 8

    def _vec(self, text: str) -> List[float]:
        seed = 0
        for ch in str(text):
            seed = (seed * 131 + ord(ch)) % (10 ** 6)
        return [float((seed + i) % 7) for i in range(self._dim)]

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> List[float]:
        return self._vec(text)

    async def aembed_documents(self, texts: List[str]) -> List[List[float]]:
        return self.embed_documents(texts)

    async def aembed_query(self, text: str) -> List[float]:
        return self.embed_query(text)

    def __hash__(self):
        return hash("fake-embedding")

    def __eq__(self, other):
        return isinstance(other, _FakeEmbedding)


class _FakeLLM(BaseLLM):
    """非流式假 LLM（Runnable）：返回固定文本。"""

    def __init__(self, **kwargs):
        kwargs.setdefault("model_name", "fake")
        super().__init__(**kwargs)

    @property
    def _llm_type(self) -> str:
        return "fake"

    def _generate(self, prompts, stop=None, run_manager=None, **kwargs):
        from langchain_core.outputs import Generation
        from langchain_core.outputs import LLMResult

        return LLMResult(generations=[[Generation(text="ok")]])


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    """把 BASE_TEMP_DIR 指向临时目录；重置进程内向量池/属主表；桩掉模型/向量。"""
    tmp_root = tmp_path / "temp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Settings.basic_settings, "BASE_TEMP_DIR", tmp_root)

    memo_faiss_pool._cache.clear()
    temp_file_ownership.clear()

    monkeypatch.setattr(file_chat_mod, "get_Embeddings", lambda **kw: _FakeEmbedding())
    # upload_temp_docs 经 memo_faiss_pool.load_vector_store -> faiss_cache.get_Embeddings
    # 构建临时向量库；同样必须离线桩掉，避免调用真实嵌入（ollama 404）
    monkeypatch.setattr(_faiss_cache, "get_Embeddings", lambda **kw: _FakeEmbedding())
    monkeypatch.setattr(file_chat_mod, "get_ChatOpenAI", lambda **kw: _FakeLLM())
    monkeypatch.setattr(
        file_chat_mod, "get_prompt_template", lambda type, name: "{{input}}"
    )
    yield


@pytest.fixture
def secret(monkeypatch):
    """设置一个 >=32 字节的测试密钥（非真实密钥）。"""
    key = "unit-test-secret-key" + "0" * 20
    monkeypatch.setenv("CHATCHAT_AUTH_SECRET", key)
    return key


@pytest.fixture
def temp_user_db(tmp_path):
    """临时文件库：迁移到 v4 + 业务表（openai_file 等）。"""
    path = tmp_path / "tmpfile.db"
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
    a = FastAPI()
    a.include_router(kb_router)
    a.include_router(chat_router)
    a.include_router(openai_router)
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


def _mk_user(temp_user_db, username, must_change_password=False):
    S = temp_user_db["Session"]
    s = S()
    try:
        u = service.create_user(
            s,
            username=username,
            display_name=username.title(),
            password="password12345678",
        )
        u.must_change_password = must_change_password
        s.commit()
        return {"id": u.id, "username": u.username, "auth_version": u.auth_version}
    finally:
        s.close()


def _headers(temp_user_db, username, must_change_password=False):
    u = _mk_user(temp_user_db, username, must_change_password=must_change_password)
    return {"Authorization": f"Bearer {auth_jwt.create_token(u['id'], u['auth_version'])}"}, u


def _upload_text(client, headers, filename="a.txt", content="alice content"):
    r = client.post(
        "/knowledge_base/upload_temp_docs",
        headers=headers,
        files=[("files", (filename, io.BytesIO(content.encode()), "text/plain"))],
    )
    assert r.status_code == 200, r.text
    return r.json()["data"]["id"]


# ---------------------------------------------------------------------------
# 临时文件（文件对话）隔离
# ---------------------------------------------------------------------------


def test_upload_temp_requires_login(client):
    r = client.post(
        "/knowledge_base/upload_temp_docs",
        files=[("files", ("x.txt", io.BytesIO(b"hi"), "text/plain"))],
    )
    assert r.status_code == 401


def test_search_temp_requires_login(client):
    r = client.post(
        "/knowledge_base/search_temp_docs",
        json={"knowledge_id": "whatever", "query": "hi", "top_k": 1},
    )
    assert r.status_code == 401


def test_file_chat_requires_login(client):
    r = client.post(
        "/chat/file_chat",
        json={"query": "hi", "knowledge_id": "whatever"},
    )
    assert r.status_code == 401


def test_legacy_kb_chat_requires_login(client):
    r = client.post("/chat/kb_chat", json={})
    assert r.status_code == 401


def test_must_change_password_blocked_for_temp(client, temp_user_db, secret):
    hdr, _ = _headers(temp_user_db, "bob", must_change_password=True)
    assert (
        client.post(
            "/knowledge_base/upload_temp_docs",
            headers=hdr,
            files=[("files", ("x.txt", io.BytesIO(b"hi"), "text/plain"))],
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/knowledge_base/search_temp_docs",
            headers=hdr,
            json={"knowledge_id": "whatever", "query": "hi", "top_k": 1},
        ).status_code
        == 403
    )


def test_a_can_chat_own_temp_but_b_cannot(client, temp_user_db, secret):
    a_hdr, a = _headers(temp_user_db, "alice")
    b_hdr, b = _headers(temp_user_db, "bob")
    tid = _upload_text(client, a_hdr, filename="alice.txt", content="alice content")
    assert tid in memo_faiss_pool.keys()
    assert temp_file_ownership.owner_of(tid) == a["id"]

    # A 自己可正常对话
    with client.stream(
        "POST",
        "/chat/file_chat",
        headers=a_hdr,
        json={"query": "hi", "knowledge_id": tid},
    ) as resp:
        assert resp.status_code == 200

    # B 不能对话 A 的 temp_id（404，不泄露存在性）
    with client.stream(
        "POST",
        "/chat/file_chat",
        headers=b_hdr,
        json={"query": "hi", "knowledge_id": tid},
    ) as resp:
        assert resp.status_code == 404


def test_b_cannot_search_a_temp(client, temp_user_db, secret):
    a_hdr, _ = _headers(temp_user_db, "alice")
    b_hdr, _ = _headers(temp_user_db, "bob")
    tid = _upload_text(client, a_hdr, filename="alice.txt", content="alice content")
    # A 可检索
    r = client.post(
        "/knowledge_base/search_temp_docs",
        headers=a_hdr,
        json={"knowledge_id": tid, "query": "alice", "top_k": 2, "score_threshold": 0.5},
    )
    assert r.status_code == 200
    # B 检索 A 的 404
    r = client.post(
        "/knowledge_base/search_temp_docs",
        headers=b_hdr,
        json={"knowledge_id": tid, "query": "alice", "top_k": 2, "score_threshold": 0.5},
    )
    assert r.status_code == 404


def test_b_cannot_reuse_a_prev_id(client, temp_user_db, secret):
    a = _mk_user(temp_user_db, "alice")
    b = _mk_user(temp_user_db, "bob")
    a_hdr = {"Authorization": f"Bearer {auth_jwt.create_token(a['id'], a['auth_version'])}"}
    b_hdr = {"Authorization": f"Bearer {auth_jwt.create_token(b['id'], b['auth_version'])}"}
    tid = _upload_text(client, a_hdr, filename="alice.txt", content="alice content")
    # B 复用 A 的 prev_id：属主校验失败 -> 404
    r = client.post(
        "/knowledge_base/upload_temp_docs",
        headers=b_hdr,
        data={"prev_id": tid},
        files=[("files", ("bob.txt", io.BytesIO(b"bob"), "text/plain"))],
    )
    assert r.status_code == 404
    # A 的属主未被改写（仍是 A）
    assert temp_file_ownership.owner_of(tid) == a["id"]


# ---------------------------------------------------------------------------
# 聊天图片/附件（OpenAI 兼容 /v1/files）隔离
# ---------------------------------------------------------------------------


def test_files_requires_login(client):
    r = client.get("/v1/files", params={"purpose": "assistants"})
    assert r.status_code == 401
    r = client.post(
        "/v1/files",
        files={"file": ("x.txt", io.BytesIO(b"hi"), "text/plain")},
        data={"purpose": "assistants"},
    )
    assert r.status_code == 401


def test_files_a_uploads_b_cannot_access(client, temp_user_db, secret):
    a_hdr, a = _headers(temp_user_db, "alice")
    b_hdr, b = _headers(temp_user_db, "bob")
    # A 上传
    r = client.post(
        "/v1/files",
        headers=a_hdr,
        files={"file": ("a.txt", io.BytesIO(b"hello"), "text/plain")},
        data={"purpose": "assistants"},
    )
    assert r.status_code == 200, r.text
    fid = r.json()["id"]

    # A 列表含该文件
    lst = client.get("/v1/files", headers=a_hdr, params={"purpose": "assistants"})
    assert lst.status_code == 200
    assert any(f["id"] == fid for f in lst.json()["data"])

    # B 列表为空（看不到 A 的文件）
    lst_b = client.get("/v1/files", headers=b_hdr, params={"purpose": "assistants"})
    assert lst_b.status_code == 200
    assert not any(f["id"] == fid for f in lst_b.json()["data"])

    # B 读取 / 下载 / 删除 A 的文件：404
    assert client.get(f"/v1/files/{fid}", headers=b_hdr).status_code == 404
    assert client.get(f"/v1/files/{fid}/content", headers=b_hdr).status_code == 404
    assert client.delete(f"/v1/files/{fid}", headers=b_hdr).status_code == 404

    # 文件仍在库中（未被 B 删除）
    s = temp_user_db["Session"]()
    try:
        assert s.get(OpenAIFileModel, fid) is not None
    finally:
        s.close()

    # A 自己可读/删
    assert client.get(f"/v1/files/{fid}", headers=a_hdr).status_code == 200
    assert client.delete(f"/v1/files/{fid}", headers=a_hdr).status_code == 200
    s = temp_user_db["Session"]()
    try:
        assert s.get(OpenAIFileModel, fid) is None
    finally:
        s.close()


def test_files_owner_from_server_not_client(client, temp_user_db, secret):
    # /v1/files 不接受客户端指定 owner；owner 恒为认证用户
    a_hdr, a = _headers(temp_user_db, "alice")
    r = client.post(
        "/v1/files",
        headers=a_hdr,
        files={"file": ("c.txt", io.BytesIO(b"z"), "text/plain")},
        data={"purpose": "assistants", "owner_id": "attacker"},
    )
    assert r.status_code == 200
    fid = r.json()["id"]
    s = temp_user_db["Session"]()
    try:
        row = s.get(OpenAIFileModel, fid)
        assert row is not None
        assert row.owner_id == a["id"]
    finally:
        s.close()


def test_same_name_uploads_do_not_overwrite_each_other(client, temp_user_db, secret):
    a_hdr, _ = _headers(temp_user_db, "alice")
    ids = []
    for content in (b"first", b"second"):
        r = client.post(
            "/v1/files",
            headers=a_hdr,
            files={"file": ("same.png", io.BytesIO(content), "image/png")},
            data={"purpose": "assistants"},
        )
        assert r.status_code == 200
        ids.append(r.json()["id"])
    assert ids[0] != ids[1]


# ---------------------------------------------------------------------------
# 未登录 / 过期 / 禁用 / 跨用户（认证依赖统一行为）
# ---------------------------------------------------------------------------


def test_expired_token_rejected(client, temp_user_db, secret):
    u = _mk_user(temp_user_db, "carol")
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    expired = auth_jwt.create_token(u["id"], u["auth_version"], now=now - timedelta(days=30))
    hdr = {"Authorization": f"Bearer {expired}"}
    assert (
        client.get("/v1/files", headers=hdr, params={"purpose": "assistants"}).status_code
        == 401
    )


def test_disabled_user_rejected(client, temp_user_db, secret):
    u = _mk_user(temp_user_db, "dave")
    s = temp_user_db["Session"]()
    try:
        row = s.get(UserModel, u["id"])
        row.status = "disabled"
        s.commit()
    finally:
        s.close()
    hdr = {"Authorization": f"Bearer {auth_jwt.create_token(u['id'], u['auth_version'])}"}
    assert (
        client.get("/v1/files", headers=hdr, params={"purpose": "assistants"}).status_code
        == 401
    )


def test_bad_signature_rejected(client, temp_user_db, secret):
    u = _mk_user(temp_user_db, "erin")
    hdr = {"Authorization": "Bearer not-a-valid-jwt"}
    assert (
        client.get("/v1/files", headers=hdr, params={"purpose": "assistants"}).status_code
        == 401
    )
