"""公共知识库共享与最小审计的离线单元测试（任务 006）。

覆盖：

- 公共知识库管理接口仅允许「已登录且已完成强制改密」用户：
  未登录 401；必须改密用户 403；
- 操作成功后记录最小审计（操作者 / 操作 / 知识库），操作者恒取自服务端认证上下文；
- 公共知识库保持全局共享（不引入 owner/ACL）：A 创建后，B 也能操作同一知识库；
- 读取和变更接口均要求登录；登录用户之间仍共享同一份知识库；
- 只使用临时 SQLite 与 FastAPI TestClient；KB 服务用桩，不连接网络、向量库或真实知识库。
"""
import sys
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# ---------------------------------------------------------------------------
# 在导入 kb_routes 之前打桩 kb_chat：其导入链（search_local_knowledgebase）
# 在模块加载时会执行真实知识库读取，离线无法提供。本测试不依赖 kb_chat。
# ---------------------------------------------------------------------------
_kb_stub = types.ModuleType("chatchat.server.chat.kb_chat")
_kb_stub.kb_chat = lambda *args, **kwargs: None
sys.modules["chatchat.server.chat.kb_chat"] = _kb_stub

import loguru

from chatchat.server.db.migrate.base import apply_migrations
from chatchat.server.db.migrate.migrations import build_registry
from chatchat.server.db.session import get_db as db_get_db
from chatchat.server.auth import jwt as auth_jwt
from chatchat.server.auth import service
from chatchat.server.knowledge_base.kb_service.base import KBServiceFactory
from chatchat.server.knowledge_base.kb_api import log_kb_operation
from chatchat.server.knowledge_base import kb_api
from chatchat.server.api_server.kb_routes import kb_router


@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setenv("CHATCHAT_AUTH_SECRET", "test-kb-secret-that-is-long-enough-32b")
    return "test-kb-secret-that-is-long-enough-32b"


@pytest.fixture
def temp_user_db(tmp_path):
    path = tmp_path / "kb.db"
    path.touch()
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as conn:
        apply_migrations(build_registry(), conn)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    yield {"engine": engine, "Session": Session}
    engine.dispose()


class _FakeKB:
    """最小桩：记录调用即可，不触达向量库/磁盘。"""
    def create_kb(self):
        return True
    def clear_vs(self):
        return True
    def drop_kb(self):
        return True


@pytest.fixture
def kb_state():
    """可被各测试改写的 KB 工厂状态：name -> kb 对象（或不存在）。"""
    return {"by_name": {}}


@pytest.fixture
def client(temp_user_db, secret, kb_state, monkeypatch):
    # KB 工厂桩：get_service_by_name 返回当前状态中的对象；get_service 返回桩
    def get_by_name(name):
        return kb_state["by_name"].get(name)
    def get_service(name, vs_type, embed_model, kb_info=None):
        return _FakeKB()
    KBServiceFactory.get_service_by_name = get_by_name
    KBServiceFactory.get_service = get_service
    monkeypatch.setattr(kb_api, "list_kbs_from_db", lambda: [])

    app = FastAPI()
    app.include_router(kb_router)

    def override_get_db():
        s = temp_user_db["Session"]()
        try:
            yield s
        finally:
            s.close()
    app.dependency_overrides[db_get_db] = override_get_db

    with TestClient(app) as test_client:
        yield test_client


def _mk_user(temp_user_db, username, must_change_password=False):
    """创建用户并返回非敏感字段（避免实例脱离会话）。"""
    S = temp_user_db["Session"]
    s = S()
    try:
        u = service.create_user(
            s, username=username, display_name=username.title(),
            password="password1234", role="user",
        )
        u.must_change_password = must_change_password
        s.commit()
        return {
            "id": u.id, "username": u.username, "role": u.role,
            "status": u.status, "auth_version": u.auth_version,
        }
    finally:
        s.close()


def _bearer(u):
    return {"Authorization": f"Bearer {auth_jwt.create_token(u['id'], u['auth_version'])}"}


def _create_request(kb_name):
    return {"knowledge_base_name": kb_name, "vector_store_type": "faiss", "kb_info": "x"}


def test_create_kb_requires_login(client):
    r = client.post("/knowledge_base/create_knowledge_base", json=_create_request("mykb"))
    assert r.status_code == 401


def test_list_kb_requires_login_but_is_shared_for_authenticated_users(
    client, temp_user_db
):
    assert client.get("/knowledge_base/list_knowledge_bases").status_code == 401
    a = _mk_user(temp_user_db, "reader_a")
    b = _mk_user(temp_user_db, "reader_b")
    assert client.get(
        "/knowledge_base/list_knowledge_bases", headers=_bearer(a)
    ).status_code == 200
    assert client.get(
        "/knowledge_base/list_knowledge_bases", headers=_bearer(b)
    ).status_code == 200


def test_create_kb_requires_password_changed(client, temp_user_db):
    u = _mk_user(temp_user_db, "pending", must_change_password=True)
    r = client.post(
        "/knowledge_base/create_knowledge_base",
        json=_create_request("mykb"),
        headers=_bearer(u),
    )
    assert r.status_code == 403


def test_create_kb_success_records_audit(client, temp_user_db, kb_state):
    u = _mk_user(temp_user_db, "alice")
    records = []
    sink = loguru.logger.add(lambda m: records.append(str(m)), level="INFO")
    try:
        r = client.post(
            "/knowledge_base/create_knowledge_base",
            json=_create_request("mykb"),
            headers=_bearer(u),
        )
    finally:
        loguru.logger.remove(sink)
    assert r.status_code == 200
    assert any(
        "KB操作审计" in rec and "alice" in rec and "mykb" in rec and "create" in rec
        for rec in records
    )


def test_kb_is_shared_between_users(client, temp_user_db, kb_state):
    """公共知识库无 owner/ACL：A 创建后 B 也能操作同一知识库。"""
    a = _mk_user(temp_user_db, "alice")
    b = _mk_user(temp_user_db, "bob")

    r1 = client.post(
        "/knowledge_base/create_knowledge_base",
        json=_create_request("sharedkb"),
        headers=_bearer(a),
    )
    assert r1.status_code == 200

    # 模拟 A 创建后该知识库已存在，供 B 操作
    kb_state["by_name"]["sharedkb"] = _FakeKB()

    r2 = client.post(
        "/knowledge_base/delete_knowledge_base",
        json="sharedkb",
        headers=_bearer(b),
    )
    assert r2.status_code == 200


def test_list_knowledge_bases_exposes_kb_info(client, temp_user_db, monkeypatch):
    """前端重构 B9：列表接口必须带 kb_info（前端知识库卡片/介绍编辑依赖）。"""
    from chatchat.server.db.base import Base
    from chatchat.server.db.models.knowledge_base_model import (
        KnowledgeBaseModel,
        KnowledgeBaseSchema,
    )

    Base.metadata.create_all(temp_user_db["engine"], tables=[KnowledgeBaseModel.__table__])

    S = temp_user_db["Session"]
    s = S()
    try:
        s.add(
            KnowledgeBaseModel(
                kb_name="infokb",
                kb_info="公共介绍",
                vs_type="faiss",
                embed_model="bge-m3",
            )
        )
        s.commit()
    finally:
        s.close()

    def fake_list(min_file_count=-1):
        s = S()
        try:
            rows = (
                s.query(KnowledgeBaseModel)
                .filter(KnowledgeBaseModel.file_count > min_file_count)
                .all()
            )
            return [KnowledgeBaseSchema.model_validate(kb) for kb in rows]
        finally:
            s.close()

    monkeypatch.setattr(kb_api, "list_kbs_from_db", fake_list)
    u = _mk_user(temp_user_db, "viewer")
    r = client.get("/knowledge_base/list_knowledge_bases", headers=_bearer(u))
    assert r.status_code == 200
    kb = next(k for k in r.json()["data"] if k["kb_name"] == "infokb")
    assert kb["kb_info"] == "公共介绍"
    assert kb["vs_type"] == "faiss"
    assert kb["embed_model"] == "bge-m3"


def test_log_kb_operation_format():
    """直接验证审计格式：操作者取自传入 user.username。"""
    records = []
    sink = loguru.logger.add(lambda m: records.append(str(m)), level="INFO")
    try:
        log_kb_operation(types.SimpleNamespace(username="alice"), "create", "mykb", "vs_type=faiss")
    finally:
        loguru.logger.remove(sink)
    assert any(
        "alice" in rec and "mykb" in rec and "create" in rec for rec in records
    )
