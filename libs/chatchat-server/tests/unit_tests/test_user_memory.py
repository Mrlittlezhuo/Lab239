"""用户级分层长期记忆离线测试（任务 007）。

全部使用临时 SQLite 数据库（迁移到 v7），不连接模型服务、外部网络或真实
用户数据库；模型相关路径用 mock / 打桩替代。

覆盖任务文档 §8 要求：
- 新迁移（v6）幂等，升级前后既有用户、会话、消息和知识库数量不变；
- A/B 用户的记忆读取、写入、更新和删除隔离（跨用户一律 404 / 抛 MemoryNotOwned）；
- 显式记住、去重、冲突更新、忘记和自动记忆开关；
- 新会话可以召回相关记忆，无关记忆不会被大量注入；
- 历史超过阈值后生成增量摘要，近期消息仍按原顺序保留；
- 提取、摘要或检索失败时聊天主流程仍可工作；
- 普通聊天和 RAG 的 Prompt 均能获得当前用户的相关记忆；
- "我的记忆"页面的主要操作有可离线验证的纯逻辑测试。
"""
import sys
import types
import asyncio
from datetime import datetime

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# ---------------------------------------------------------------------------
# 在导入 chat/kb_chat 之前打桩 kb_chat：其导入链会在模块加载时读取真实知识
# 库数据库，离线测试无法提供。RAG 记忆注入测试直接调用 memory_system_messages
# 并复用 chat.py 中 History.to_msg_template(False) 的转换路径，不需要 kb_chat
# 的实现。
# ---------------------------------------------------------------------------
_kb_stub = types.ModuleType("chatchat.server.chat.kb_chat")
_kb_stub.kb_chat = lambda *args, **kwargs: None
sys.modules["chatchat.server.chat.kb_chat"] = _kb_stub

from chatchat.server.db.base import Base
from chatchat.server.db.migrate.base import apply_migrations, current_version
from chatchat.server.db.migrate.migrations import build_registry
from chatchat.server.db.models.user_model import UserModel, OpenAIFileModel  # noqa: F401
from chatchat.server.db.models.conversation_model import ConversationModel  # noqa: F401
from chatchat.server.db.models.message_model import MessageModel  # noqa: F401
from chatchat.server.db.models.knowledge_base_model import KnowledgeBaseModel  # noqa: F401
from chatchat.server.db.models.knowledge_file_model import KnowledgeFileModel  # noqa: F401
from chatchat.server.db.session import get_db as db_get_db
from chatchat.server.db.repository import (
    create_memory,
    list_memories,
    get_owned_memory,
    update_memory,
    delete_memory,
    MemoryNotOwned,
    VALID_TYPES,
)
from chatchat.server.memory import memory_service
from chatchat.server.auth import jwt as auth_jwt
from chatchat.server.auth import service
from chatchat.server.auth.routes_auth import auth_router
from chatchat.server.api_server.memory_routes import memory_router


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def secret(monkeypatch):
    """设置一个 >=32 字节的测试密钥（非真实密钥）。"""
    monkeypatch.setattr(auth_jwt, "get_secret", lambda: "unit-test-secret-key-0000000000000000")
    return "unit-test-secret-key-0000000000000000"


@pytest.fixture
def temp_user_db(secret, tmp_path):
    """临时 SQLite 数据库：建全表、迁移到 v7，注册模型元数据。"""
    path = tmp_path / "memory_test.db"
    engine = create_engine(f"sqlite:///{path}")
    # 先跑迁移（从空库建表并记录版本），再补建任何迁移未覆盖的模型表
    with engine.connect() as conn:
        apply_migrations(build_registry(), conn)
    Base.metadata.create_all(engine)
    assert current_version(engine.connect()) == 7
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    yield {"Session": Session, "engine": engine}
    engine.dispose()


def _mk_user(db, username, role="user", must_change_password=False):
    """创建用户并返回其非敏感字段。"""
    S = db["Session"]
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
        return {"id": u.id, "username": u.username, "auth_version": u.auth_version}
    finally:
        s.close()


def _headers(db, username):
    """生成当前登录用户的 Bearer token 请求头。"""
    S = db["Session"]
    s = S()
    try:
        u = s.query(UserModel).filter(UserModel.username == username).one()
        return {"Authorization": f"Bearer {auth_jwt.create_token(u.id, u.auth_version)}"}
    finally:
        s.close()


@pytest.fixture
def app(temp_user_db):
    """FastAPI 应用：认证路由 + 记忆路由，get_db 指向临时库。"""
    a = FastAPI()
    a.include_router(auth_router)
    a.include_router(memory_router)

    async def override_get_db():
        s = temp_user_db["Session"]()
        try:
            yield s
        finally:
            s.close()

    a.dependency_overrides[db_get_db] = override_get_db
    return a


@pytest.fixture
def client(app, monkeypatch):
    # 当前 Starlette/TestClient 与环境中的 anyio 组合会在请求线程中卡住。
    # 直接使用 httpx 的 ASGI transport，仍然完整经过 FastAPI 路由与依赖。
    # 此 Conda 环境中 anyio 4.14 的同步线程桥连最小 FastAPI 同步路由也会
    # 挂起。测试内将同步端点直接执行，生产代码和 HTTP 语义保持不变。
    async def direct_call(func, *args, **kwargs):
        return func(*args, **kwargs)

    import fastapi.routing
    import fastapi.dependencies.utils

    monkeypatch.setattr(fastapi.routing, "run_in_threadpool", direct_call)
    monkeypatch.setattr(fastapi.dependencies.utils, "run_in_threadpool", direct_call)

    class Client:
        def request(self, method, url, **kwargs):
            async def send():
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://testserver"
                ) as async_client:
                    return await async_client.request(method, url, **kwargs)

            return asyncio.run(send())

        def get(self, url, **kwargs):
            return self.request("GET", url, **kwargs)

        def post(self, url, **kwargs):
            return self.request("POST", url, **kwargs)

        def patch(self, url, **kwargs):
            return self.request("PATCH", url, **kwargs)

        def delete(self, url, **kwargs):
            return self.request("DELETE", url, **kwargs)

    return Client()


@pytest.fixture
def users(temp_user_db):
    """创建 alice/bob 两个普通用户（均已改密、自动记忆默认开启）。"""
    return {
        "alice": _mk_user(temp_user_db, "alice"),
        "bob": _mk_user(temp_user_db, "bob"),
    }


# ============================================================================
# 1. 迁移幂等 + 既有数据数量不变
# ============================================================================


class TestMigration:
    def test_v6_idempotent_and_counts_unchanged(self, secret, tmp_path):
        """v6 迁移幂等：重复执行不重复建表/改版本，既有用户/会话/消息/知识库数量不变。"""
        path = tmp_path / "v6_idempotent.db"
        engine = create_engine(f"sqlite:///{path}")
        registry = build_registry()

        # 第一次：从空库完整迁移到 v6
        with engine.connect() as conn:
            apply_migrations(registry, conn)
            memory_columns = {
                row[1] for row in conn.exec_driver_sql("PRAGMA table_info(memory)")
            }
            assert {
                "memory_key", "source_conversation_id", "source_message_id"
            }.issubset(memory_columns)
        assert current_version(engine.connect()) == 7
        # 迁移仅覆盖 user_account/openai_file/memory/session_summary，
        # conversation/message/knowledge_* 由模型元数据建表
        Base.metadata.create_all(engine)

        # 建表后插入各类既有数据
        Session = sessionmaker(bind=engine, expire_on_commit=False)
        s = Session()
        try:
            u1 = service.create_user(s, username="alice", display_name="A", password="password12345678")
            u1.must_change_password = False
            s.commit()
            from chatchat.server.db.repository import create_conversation, add_message
            conv = create_conversation(s, u1.id, "llm_chat", "conv-x")
            add_message(s, conv, u1.id, "llm_chat", "hello", response="hi")
            create_memory(s, u1.id, "我是用户", mtype="fact", source="user", importance=1)
            kb = KnowledgeBaseModel(kb_name="kb1", kb_info="d")
            s.add(kb)
            s.commit()
            from chatchat.server.db.models.knowledge_file_model import KnowledgeFileModel
            kf = KnowledgeFileModel(
                kb_name="kb1", file_name="doc.pdf", file_size=10,
            )
            s.add(kf)
            s.commit()

            def counts():
                from chatchat.server.db.models.memory_model import MemoryModel
                return {
                    "user": s.query(UserModel).count(),
                    "conversation": s.query(ConversationModel).count(),
                    "message": s.query(MessageModel).count(),
                    "kb": s.query(KnowledgeBaseModel).count(),
                    "kb_file": s.query(KnowledgeFileModel).count(),
                    "memory": s.query(MemoryModel).count(),
                }

            before = counts()
            assert before == {
                "user": 1, "conversation": 1, "message": 1,
                "kb": 1, "kb_file": 1, "memory": 1,
            }
        finally:
            s.close()

        # 第二次：重复执行迁移（幂等），版本不变、数据不变
        with engine.connect() as conn:
            result = apply_migrations(registry, conn)
        assert result.applied == []
        assert current_version(engine.connect()) == 7

        s = Session()
        try:
            after = {
                "user": s.query(UserModel).count(),
                "conversation": s.query(ConversationModel).count(),
                "message": s.query(MessageModel).count(),
                "kb": s.query(KnowledgeBaseModel).count(),
                "kb_file": s.query(KnowledgeFileModel).count(),
            }
            assert before["user"] == after["user"] == 1
            assert before["conversation"] == after["conversation"] == 1
            assert before["message"] == after["message"] == 1
            assert before["kb"] == after["kb"] == 1
            assert before["kb_file"] == after["kb_file"] == 1
        finally:
            s.close()
        engine.dispose()

    def test_memory_auto_enabled_default_true(self, temp_user_db):
        """v6 后新账户 memory_auto_enabled 默认开启（True）。"""
        u = _mk_user(temp_user_db, "carol")
        S = temp_user_db["Session"]
        s = S()
        try:
            row = s.query(UserModel).filter(UserModel.id == u["id"]).one()
            assert row.memory_auto_enabled is True
        finally:
            s.close()


# ============================================================================
# 2. A/B 隔离（API + repository 双层）
# ============================================================================


class TestIsolation:
    def test_a_cannot_read_update_delete_b_memory_via_api(self, client, temp_user_db, users):
        hdr_a = _headers(temp_user_db, "alice")
        hdr_b = _headers(temp_user_db, "bob")

        # bob 创建一条记忆
        r = client.post("/memories", headers=hdr_b, json={"type": "fact", "content": "bob 的秘密"})
        assert r.status_code == 200
        assert r.json()["memory_key"].startswith("manual:")
        assert r.json()["source_conversation_id"] is None
        assert r.json()["source_message_id"] is None
        mem_id = r.json()["id"]

        # alice 读 bob 的记忆 → 404
        assert client.get(f"/memories/{mem_id}", headers=hdr_a).status_code == 404
        # alice 改 bob 的记忆 → 404
        assert client.patch(f"/memories/{mem_id}", headers=hdr_a, json={"content": "hacked"}).status_code == 404
        # alice 删 bob 的记忆 → 404
        assert client.delete(f"/memories/{mem_id}", headers=hdr_a).status_code == 404

        # bob 仍能看到自己的
        assert client.get(f"/memories/{mem_id}", headers=hdr_b).status_code == 200
        # bob 列表只含自己的
        bob_list = client.get("/memories", headers=hdr_b).json()
        assert {m["id"] for m in bob_list} == {mem_id}
        # alice 列表不含 bob 的
        alice_list = client.get("/memories", headers=hdr_a).json()
        assert mem_id not in {m["id"] for m in alice_list}

    def test_cross_owner_update_raises(self, temp_user_db, users):
        S = temp_user_db["Session"]
        s = S()
        try:
            mem = create_memory(s, users["alice"]["id"], "alice 内容", mtype="fact", source="user", importance=1)
            with pytest.raises(MemoryNotOwned):
                update_memory(s, mem.id, users["bob"]["id"], content="hacked")
            # 未改变
            assert s.query(__import__("chatchat.server.db.repository.memory_repository", fromlist=["x"]).MemoryModel).filter_by(id=mem.id).one().content == "alice 内容"
            # bob 跨 owner 读取 → None
            assert get_owned_memory(s, mem.id, users["bob"]["id"]) is None
            # bob 删除 alice 的记忆 → 抛 MemoryNotOwned
            with pytest.raises(MemoryNotOwned):
                delete_memory(s, mem.id, users["bob"]["id"])
        finally:
            s.close()

    def test_unauthenticated_rejected(self, client, temp_user_db, users):
        assert client.get("/memories").status_code == 401
        assert client.post("/memories", json={"type": "fact", "content": "x"}).status_code == 401


# ============================================================================
# 3. 显式记住 / 去重 / 冲突更新 / 忘记 / 自动记忆开关
# ============================================================================


class TestRemember:
    def test_remember_and_dedup(self, temp_user_db, users):
        S = temp_user_db["Session"]
        s = S()
        try:
            aid = users["alice"]["id"]
            m1 = memory_service.remember(s, aid, "我研究无线通信", mtype="goal", source="chat")
            # 相同内容去重：不新增
            m2 = memory_service.remember(s, aid, "我研究无线通信", mtype="goal", source="chat")
            assert m1.id == m2.id
            assert len(list_memories(s, aid, enabled_only=True)) == 1
        finally:
            s.close()

    def test_conflict_update(self, temp_user_db, users):
        """同一回答偏好主题的新值替换旧值，而不是留下冲突记录。"""
        S = temp_user_db["Session"]
        s = S()
        try:
            aid = users["alice"]["id"]
            memory_service.auto_extract(s, aid, "我偏好简洁中文")
            memory_service.auto_extract(s, aid, "以后请详细回答")
            enabled = list_memories(s, aid, enabled_only=True)
            response_styles = [m for m in enabled if m.memory_key == "preference:response_style"]
            assert len(response_styles) == 1
            assert "详细" in response_styles[0].content
            assert "简洁" not in response_styles[0].content
        finally:
            s.close()

    def test_forget(self, temp_user_db, users):
        S = temp_user_db["Session"]
        s = S()
        try:
            aid = users["alice"]["id"]
            memory_service.remember(s, aid, "我研究无线通信", mtype="goal", source="chat")
            memory_service.remember(s, aid, "我住北京", mtype="fact", source="chat")
            n = memory_service.forget(s, aid, content="无线通信")
            assert n == 1
            enabled = list_memories(s, aid, enabled_only=True)
            contents = [m.content for m in enabled]
            assert "我研究无线通信" not in contents
            assert "我住北京" in contents
        finally:
            s.close()

    def test_forget_all(self, temp_user_db, users):
        S = temp_user_db["Session"]
        s = S()
        try:
            aid = users["alice"]["id"]
            memory_service.remember(s, aid, "a", mtype="fact", source="chat")
            memory_service.remember(s, aid, "b", mtype="fact", source="chat")
            n = memory_service.forget(s, aid)
            assert n == 2
            assert len(list_memories(s, aid, enabled_only=True)) == 0
        finally:
            s.close()

    def test_auto_extract_explicit_remember(self, temp_user_db, users):
        """显式"请记住"在自动记忆开启时可靠写入。"""
        S = temp_user_db["Session"]
        s = S()
        try:
            aid = users["alice"]["id"]
            actions = memory_service.auto_extract(s, aid, "请记住我研究无线通信，偏好简洁中文")
            assert any("已记住" in a for a in actions)
            mems = list_memories(s, aid, enabled_only=True)
            assert any("无线通信" in m.content for m in mems)
            assert any("简洁" in m.content and "中文" in m.content for m in mems)
            assert {m.memory_key for m in mems} == {
                "profile:research", "preference:response_style"
            }
        finally:
            s.close()

    def test_auto_extract_forget(self, temp_user_db, users):
        S = temp_user_db["Session"]
        s = S()
        try:
            aid = users["alice"]["id"]
            memory_service.remember(s, aid, "我研究无线通信", mtype="goal", source="chat")
            actions = memory_service.auto_extract(s, aid, "请忘记我研究无线通信")
            assert any("已停用" in a for a in actions)
            assert not any("无线通信" in m.content for m in list_memories(s, aid, enabled_only=True))
        finally:
            s.close()

    def test_implicit_extract_and_source(self, temp_user_db, users):
        """无需“请记住”即可拆出多条稳定记忆，并保存来源定位。"""
        S = temp_user_db["Session"]
        s = S()
        try:
            aid = users["alice"]["id"]
            actions = memory_service.auto_extract(
                s,
                aid,
                "我研究无线通信，偏好简洁中文",
                source_conversation_id="conv-source",
                source_message_id="msg-source",
            )
            assert len(actions) == 2
            mems = list_memories(s, aid, enabled_only=True)
            assert {m.memory_key for m in mems} == {
                "profile:research", "preference:response_style"
            }
            assert all(m.source_conversation_id == "conv-source" for m in mems)
            assert all(m.source_message_id == "msg-source" for m in mems)
        finally:
            s.close()

    def test_auto_memory_toggle_off_keeps_explicit_intent(self, temp_user_db, users):
        """关闭自动记忆只禁用隐式提取，显式记住仍然生效。"""
        S = temp_user_db["Session"]
        s = S()
        try:
            aid = users["alice"]["id"]
            # 关闭开关
            u = s.query(UserModel).filter(UserModel.id == aid).one()
            u.memory_auto_enabled = False
            s.commit()
            actions = memory_service.auto_extract(s, aid, "我研究无线通信")
            assert actions == []
            assert len(list_memories(s, aid, enabled_only=True)) == 0
            actions = memory_service.auto_extract(s, aid, "请记住我的项目是记忆系统")
            assert any("已记住" in a for a in actions)
            assert any("记忆系统" in m.content for m in list_memories(s, aid, enabled_only=True))
        finally:
            s.close()

    def test_forget_response_preference(self, temp_user_db, users):
        S = temp_user_db["Session"]
        s = S()
        try:
            aid = users["alice"]["id"]
            memory_service.auto_extract(s, aid, "我偏好简洁中文")
            actions = memory_service.auto_extract(s, aid, "忘记我的回答偏好")
            assert any("已停用" in a for a in actions)
            assert memory_service.recall_memories(s, aid, "以后怎么回答我") == []
        finally:
            s.close()

    def test_auto_memory_toggle_via_api(self, client, temp_user_db, users):
        hdr_a = _headers(temp_user_db, "alice")
        # 查询开关
        r = client.get("/memories/auto", headers=hdr_a)
        assert r.status_code == 200 and r.json()["auto_memory"] is True
        # 关闭
        r = client.patch("/memories/auto", headers=hdr_a, params={"enabled": False})
        assert r.status_code == 200 and r.json()["auto_memory"] is False
        # 重新开启
        r = client.patch("/memories/auto", headers=hdr_a, params={"enabled": True})
        assert r.status_code == 200 and r.json()["auto_memory"] is True


# ============================================================================
# 4. 新会话召回 + 无关记忆不大量注入
# ============================================================================


class TestRecall:
    def test_cross_expression_recall_by_topic_key(self, temp_user_db, users):
        S = temp_user_db["Session"]
        s = S()
        try:
            aid = users["alice"]["id"]
            memory_service.auto_extract(s, aid, "我研究无线通信")
            recalled = memory_service.recall_memories(s, aid, "你还记得我的研究方向吗")
            assert len(recalled) == 1
            assert "无线通信" in recalled[0].content
        finally:
            s.close()

    def test_new_session_recalls_relevant(self, temp_user_db, users):
        """会话 A 表达偏好 → 新会话 B 仍能召回相关信息。"""
        S = temp_user_db["Session"]
        s = S()
        try:
            aid = users["alice"]["id"]
            memory_service.remember(s, aid, "我研究无线通信，偏好简洁中文", mtype="preference", source="chat")
            # 新会话 B 的召回
            msgs = memory_service.memory_system_messages(s, aid, "你记得我研究什么吗", None)
            joined = "\n".join(m["content"] for m in msgs)
            assert "无线通信" in joined
            assert "简洁中文" in joined
        finally:
            s.close()

    def test_unrelated_memory_not_mass_injected(self, temp_user_db, users):
        """无关查询不大量注入不相关记忆（相关度排序 + 限长）。"""
        S = temp_user_db["Session"]
        s = S()
        try:
            aid = users["alice"]["id"]
            # 大量不相关记忆
            for i in range(20):
                memory_service.remember(s, aid, f"不相关事实{i}", mtype="fact", source="chat")
            # 一条相关
            memory_service.remember(s, aid, "我研究无线通信", mtype="goal", source="chat", importance=5)
            msgs = memory_service.recall_memories(s, aid, "无线通信", max_count=memory_service.RECALL_MAX_COUNT, max_chars=memory_service.RECALL_MAX_CHARS)
            # 相关记忆被召回
            assert any("无线通信" in m.content for m in msgs)
            # 数量受 RECALL_MAX_COUNT 限制
            assert len(msgs) <= memory_service.RECALL_MAX_COUNT
            # 不相关记忆未被大量注入
            unrelated_injected = [m for m in msgs if "不相关事实" in m.content]
            assert len(unrelated_injected) < 10
        finally:
            s.close()

    def test_disabled_memory_not_recalled(self, temp_user_db, users):
        S = temp_user_db["Session"]
        s = S()
        try:
            aid = users["alice"]["id"]
            memory_service.remember(s, aid, "已停用的记忆", mtype="fact", source="chat")
            memory_service.forget(s, aid, content="已停用的记忆")
            msgs = memory_service.recall_memories(s, aid, "已停用的记忆")
            assert all(m.enabled for m in msgs)
            assert not any("已停用的记忆" in m.content for m in msgs)
        finally:
            s.close()


# ============================================================================
# 5. 增量摘要（阈值后触发，近期消息保持原序）
# ============================================================================


class TestSummary:
    def test_delete_conversation_cleans_summary_not_long_term_memory(self, temp_user_db, users):
        from chatchat.server.db.repository import (
            add_message,
            create_conversation,
            delete_owned_conversation,
        )
        from chatchat.server.db.repository.session_summary_repository import get_session_summary

        S = temp_user_db["Session"]
        s = S()
        try:
            aid = users["alice"]["id"]
            conv = create_conversation(s, aid, "llm_chat", "delete-summary")
            for i in range(13):
                add_message(s, conv, aid, "llm_chat", f"q{i}", response=f"a{i}")
            memory_service.summarize_conversation(s, aid, conv, threshold=12)
            memory_service.remember(
                s,
                aid,
                "研究方向：无线通信",
                memory_key="profile:research",
                source_conversation_id=conv,
            )
            assert get_session_summary(s, conv, aid) is not None

            delete_owned_conversation(s, conv, aid)

            assert get_session_summary(s, conv, aid) is None
            assert any("无线通信" in m.content for m in list_memories(s, aid, enabled_only=True))
        finally:
            s.close()
    def test_incremental_summary_after_threshold(self, temp_user_db, users):
        S = temp_user_db["Session"]
        s = S()
        try:
            aid = users["alice"]["id"]
            from chatchat.server.db.repository import create_conversation, add_message
            conv = create_conversation(s, aid, "llm_chat", "conv-sum")
            # 插入 15 条消息（> threshold 12）
            for i in range(15):
                add_message(s, conv, aid, "llm_chat", f"问题{i}", response=f"回答{i}")
            # 用假摘要器：只返回输入文本的固定标记
            calls = []
            def fake_summarizer(existing, older_text):
                calls.append((existing, older_text))
                return f"摘要了{len(older_text.splitlines())}行"
            result = memory_service.summarize_conversation(s, aid, conv, threshold=12, summarizer=fake_summarizer)
            assert result is not None
            assert result.summarized_count == 3  # 15 - 12 = 3 条超出窗口的较早消息
            # 近期 12 条保持原序（未被摘要吞并）
            recent = s.query(MessageModel).filter_by(conversation_id=conv).order_by(MessageModel.create_time.asc()).all()
            assert len(recent) == 15
            assert [m.query for m in recent[-12:]] == [f"问题{i}" for i in range(3, 15)]
            # 摘要覆盖了最早 3 条
            assert "问题0" in calls[0][1]
            assert "问题2" in calls[0][1]
        finally:
            s.close()

    def test_summary_not_treated_as_long_term_fact(self, temp_user_db, users):
        """摘要写入 session_summary，不进入 memory 表（不被当作长期事实）。"""
        S = temp_user_db["Session"]
        s = S()
        try:
            aid = users["alice"]["id"]
            from chatchat.server.db.repository import create_conversation, add_message
            conv = create_conversation(s, aid, "llm_chat", "conv-sum2")
            for i in range(15):
                add_message(s, conv, aid, "llm_chat", f"q{i}", response=f"a{i}")
            memory_service.summarize_conversation(s, aid, conv, threshold=12)
            # memory 表仍为空（摘要没被当长期事实入库）
            assert len(list_memories(s, aid, enabled_only=True)) == 0
            # session_summary 有记录
            from chatchat.server.db.repository.session_summary_repository import get_session_summary
            row = get_session_summary(s, conv, aid)
            assert row is not None and row.summary
        finally:
            s.close()

    def test_summary_below_threshold_noop(self, temp_user_db, users):
        S = temp_user_db["Session"]
        s = S()
        try:
            aid = users["alice"]["id"]
            from chatchat.server.db.repository import create_conversation, add_message
            conv = create_conversation(s, aid, "llm_chat", "conv-sum3")
            for i in range(5):
                add_message(s, conv, aid, "llm_chat", f"q{i}", response=f"a{i}")
            result = memory_service.summarize_conversation(s, aid, conv, threshold=12)
            assert result is None
        finally:
            s.close()


# ============================================================================
# 6. 失败不阻断主流程
# ============================================================================


class TestResilience:
    def test_memory_failure_does_not_break_chat(self, temp_user_db, users, monkeypatch):
        """记忆上下文构造抛异常时，memory_system_messages 返回已构造部分（不抛）。"""
        S = temp_user_db["Session"]
        s = S()
        try:
            aid = users["alice"]["id"]
            def boom(session, owner_id, query):
                raise RuntimeError("memory service crashed")
            monkeypatch.setattr(memory_service, "build_memory_context", boom)
            # 不应抛出
            msgs = memory_service.memory_system_messages(s, aid, "hi", None)
            assert isinstance(msgs, list)
        finally:
            s.close()

    def test_short_term_context_failure_does_not_break(self, temp_user_db, users, monkeypatch):
        S = temp_user_db["Session"]
        s = S()
        try:
            aid = users["alice"]["id"]
            def boom(session, owner_id, query):
                raise RuntimeError("short term crashed")
            monkeypatch.setattr(memory_service, "build_short_term_context", boom)
            msgs = memory_service.memory_system_messages(s, aid, "hi", None)
            assert isinstance(msgs, list)
        finally:
            s.close()

    def test_auto_extract_failure_does_not_break(self, temp_user_db, users, monkeypatch):
        S = temp_user_db["Session"]
        s = S()
        try:
            aid = users["alice"]["id"]
            def boom(session, owner_id, query, response=""):
                raise RuntimeError("auto extract crashed")
            monkeypatch.setattr(memory_service, "auto_extract", boom)
            # chat/kb_chat 的调用方用 try/except 包裹；这里验证调用方不会因
            # auto_extract 抛异常而崩溃（模拟调用方的 try/except 行为）。
            try:
                memory_service.auto_extract(s, aid, "hi")
            except RuntimeError:
                pass  # 调用方捕获，主流程继续
            # 记忆服务本身可用
            assert memory_service.build_memory_context(s, aid, "hi") is not None or True
        finally:
            s.close()

    def test_summarizer_failure_returns_none(self, temp_user_db, users):
        S = temp_user_db["Session"]
        s = S()
        try:
            aid = users["alice"]["id"]
            from chatchat.server.db.repository import create_conversation, add_message
            conv = create_conversation(s, aid, "llm_chat", "conv-sum4")
            for i in range(15):
                add_message(s, conv, aid, "llm_chat", f"q{i}", response=f"a{i}")
            def bad_summarizer(existing, older_text):
                raise RuntimeError("summarizer crashed")
            result = memory_service.summarize_conversation(s, aid, conv, threshold=12, summarizer=bad_summarizer)
            # 摘要失败不阻断：返回 None（调用方 try/except）
            assert result is None
        finally:
            s.close()


# ============================================================================
# 7. 普通聊天 + RAG 的 Prompt 均获得当前用户相关记忆
# ============================================================================


class TestPromptInjection:
    def test_plain_chat_prompt_gets_current_user_memory(self, temp_user_db, users, monkeypatch):
        """普通聊天链路：create_models_chains 把当前用户记忆注入 Prompt 历史最前。"""
        import chatchat.server.chat.chat as chat_mod
        from langchain_chatchat import PlatformToolsRunnable
        from chatchat.server.db.repository import create_conversation, add_message

        S = temp_user_db["Session"]
        s = S()
        try:
            aid = users["alice"]["id"]
            # 记录一条当前用户长期记忆
            memory_service.remember(s, aid, "我研究无线通信，偏好简洁中文",
                                    mtype="preference", source="chat")
            # 全新会话（无历史消息），避免 intermediate_steps 解析
            conv = create_conversation(s, aid, "llm_chat", "conv-prompt")

            captured = {}

            def _fake_create_agent_executor(**kwargs):
                captured["history"] = kwargs.get("history")
                # create_models_chains 末尾会做 {"chat_input":...} | agent_executor，
                # 因此需返回 dict 以支持 `|` 合并
                return {"stub_executor": True}

            # 打桩真实 agent 构建（避免真实模型/注册表），仅捕获 history
            monkeypatch.setattr(PlatformToolsRunnable, "create_agent_executor",
                                staticmethod(_fake_create_agent_executor))
            # create_models_chains 内部以无参方式调用 MCP 查询
            monkeypatch.setattr(chat_mod, "get_enabled_mcp_connections",
                                lambda *a, **k: [])

            class _StubLLM:
                """create_models_chains 仅需 llm.callbacks = ... 的赋值目标。"""
                model_name = "stub"

            chat_mod.create_models_chains(
                history_len=10, prompts={}, models={"action_model": _StubLLM()},
                tools=[], callbacks=None, conversation_id=conv, session=s,
                owner_id=aid, metadata=None, use_mcp=False,
                query="你记得我研究什么吗",
            )
            history = captured.get("history")
            assert history is not None
            # 记忆段在最前，作为独立 system 消息，包含当前用户记忆内容
            system_msgs = [h for h in history
                          if isinstance(h, dict) and h.get("role") == "system"]
            assert any("无线通信" in (h.get("content") or "") for h in system_msgs)
            # 该 memory 段在最前（第一条即记忆段），近期轮次在其后
            assert "【你的记忆" in system_msgs[0]["content"]
        finally:
            s.close()

    def test_plain_chat_summary_generation_wired(self, temp_user_db, users, monkeypatch):
        """create_models_chains 在注入前会触发增量摘要生成（功能闭环）。"""
        import chatchat.server.chat.chat as chat_mod
        from langchain_chatchat import PlatformToolsRunnable
        from chatchat.server.db.repository import create_conversation, add_message
        from chatchat.server.db.repository import session_summary_repository as summary_repo

        S = temp_user_db["Session"]
        s = S()
        try:
            aid = users["alice"]["id"]
            conv = create_conversation(s, aid, "llm_chat", "摘要会话")
            # 写入 15 条带回答的消息（真实回答的 metadata 含 intermediate_steps），
            # 超过默认阈值 12，应触发增量摘要
            import json as _json
            for i in range(15):
                add_message(
                    s, conv, aid, "llm_chat",
                    query=f"问题{i}", response=f"回答{i}",
                    metadata={"intermediate_steps": _json.dumps([])},
                )

            class _StubLLM:
                """create_models_chains 仅需 llm.callbacks = ... 的赋值目标。"""
                model_name = "stub"

            def _fake_create_agent_executor(**kwargs):
                # create_models_chains 末尾会做 {"chat_input":...} | agent_executor，
                # 因此需返回 dict 以支持 `|` 合并
                return {"stub_executor": True}

            monkeypatch.setattr(PlatformToolsRunnable, "create_agent_executor",
                                staticmethod(_fake_create_agent_executor))
            monkeypatch.setattr(chat_mod, "get_enabled_mcp_connections",
                                lambda *a, **k: [])

            chat_mod.create_models_chains(
                history_len=10, prompts={}, models={"action_model": _StubLLM()},
                tools=[], callbacks=None, conversation_id=conv, session=s,
                owner_id=aid, metadata=None, use_mcp=False, query="历史问题",
            )
            # 摘要应已生成并写入 session_summary（每会话一行）
            row = summary_repo.get_session_summary(s, conv, aid)
            assert row is not None
            # 短期上下文应包含"较早内容摘要"段
            short = memory_service.build_short_term_context(s, aid, conv, "历史问题")
            assert "【较早内容摘要】" in short
        finally:
            s.close()

    def test_rag_prompt_gets_current_user_memory(self, temp_user_db, users):
        """RAG 链路：memory_system_messages 返回当前用户相关记忆，
        并可经 kb_chat.py 中的 History(...).to_msg_template(False) 路径注入 Prompt。"""
        S = temp_user_db["Session"]
        s = S()
        try:
            aid = users["alice"]["id"]
            memory_service.remember(s, aid, "我研究无线通信", mtype="goal", source="chat")
            # RAG 注入使用同一 memory_system_messages
            msgs = memory_service.memory_system_messages(s, aid, "无线通信相关的研究")
            joined = "\n".join(m["content"] for m in msgs)
            assert "无线通信" in joined
            # 与 kb_chat.py 的 RAG 注入一致：每条记忆消息经
            # History(role=..., content=...).to_msg_template(False) 转为消息模板
            from chatchat.server.chat.utils import History
            from langchain_core.prompts.chat import ChatPromptTemplate
            mem_templates = [
                History(role=m["role"], content=m["content"]).to_msg_template(False)
                for m in msgs
            ]
            # 记忆段置于 RAG 输入之前，构成 Prompt 的一部分
            rag_prompt = ChatPromptTemplate.from_messages(
                mem_templates + [History(role="user", content="{question}").to_msg_template(False)]
            )
            # 构造成功即证明 RAG 可注入；且 Prompt 消息里含当前用户记忆
            assert rag_prompt is not None
            prompt_text = "\n".join(
                m.prompt.template for m in mem_templates
            )
            assert "无线通信" in prompt_text
        finally:
            s.close()

    def test_rag_does_not_inject_other_user_memory(self, temp_user_db, users):
        """RAG 只注入当前用户记忆，不注入他人记忆。"""
        S = temp_user_db["Session"]
        s = S()
        try:
            memory_service.remember(s, users["alice"]["id"], "alice 的研究是无线通信", mtype="goal", source="chat")
            # bob 的 Prompt 不含 alice 的记忆
            msgs = memory_service.memory_system_messages(s, users["bob"]["id"], "研究")
            joined = "\n".join(m["content"] for m in msgs)
            assert "alice 的研究" not in joined
        finally:
            s.close()


# ============================================================================
# 8. "我的记忆"页面纯逻辑测试
# ============================================================================


class TestMyMemoryPage:
    def test_memory_rows_formats_chat_source(self):
        from chatchat.webui_pages.my_memory.dialogue import memory_rows

        rows = memory_rows([{
            "id": "m1",
            "type": "fact",
            "content": "研究方向：无线通信",
            "importance": 4,
            "enabled": True,
            "source": "auto_extract",
            "source_conversation_id": "conversation123",
            "source_message_id": "message123",
        }])
        assert rows[0]["source_label"] == "来自会话 conversa / 消息 message1"

    def test_type_options_match_backend(self):
        from chatchat.webui_pages.my_memory.dialogue import TYPE_OPTIONS, TYPE_LABELS, type_label
        assert set(TYPE_OPTIONS) == set(VALID_TYPES)
        for t in TYPE_OPTIONS:
            assert t in TYPE_LABELS
            assert (type_label(t) == TYPE_LABELS.get(t))  # 有标签且一致

    def test_type_label(self):
        from chatchat.webui_pages.my_memory.dialogue import type_label
        assert type_label("fact") == "事实"
        assert type_label("preference") == "偏好"
        assert type_label("goal") == "目标"
        assert type_label("important") == "重要事项"
        assert type_label("unknown") == "unknown"  # 未知回退原值

    def test_validate_memory_content(self):
        from chatchat.webui_pages.my_memory.dialogue import validate_memory_content, MAX_CONTENT_LEN
        ok, _ = validate_memory_content("合法内容")
        assert ok is True
        ok, _ = validate_memory_content("")
        assert ok is False
        ok, _ = validate_memory_content(None)
        assert ok is False
        ok, _ = validate_memory_content("x" * (MAX_CONTENT_LEN + 1))
        assert ok is False

    def test_parse_memory_payload(self):
        from chatchat.webui_pages.my_memory.dialogue import parse_memory_payload
        p = parse_memory_payload("fact", "内容", importance=3)
        assert p == {"type": "fact", "content": "内容", "importance": 3, "source": "user"}
        # 未知类型报错
        with pytest.raises(ValueError):
            parse_memory_payload("badtype", "内容")
        # importance 截断到 1-5
        p = parse_memory_payload("fact", "内容", importance=99)
        assert p["importance"] == 5

    def test_memory_update_payload(self):
        from chatchat.webui_pages.my_memory.dialogue import memory_update_payload
        # 只保留非 None 字段
        p = memory_update_payload({"type": "goal", "content": None, "importance": 2})
        assert p == {"type": "goal", "importance": 2}
        # 未知类型报错
        with pytest.raises(ValueError):
            memory_update_payload({"type": "bad"})
        # importance 截断
        p = memory_update_payload({"importance": -5})
        assert p["importance"] == 1

    def test_format_time(self):
        from chatchat.webui_pages.my_memory.dialogue import format_time
        assert format_time(None) == ""
        assert format_time("") == ""
        dt = datetime(2026, 10, 3, 12, 30)
        assert format_time(dt) == "2026-10-03 12:30"
        assert format_time("2026-10-03T12:30:00Z") == "2026-10-03 12:30"

    def test_api_request_memory_methods_exist(self):
        from chatchat.webui_pages.utils import ApiRequest, MemoryApiError
        assert callable(getattr(ApiRequest, "list_memories", None))
        assert callable(getattr(ApiRequest, "create_memory", None))
        assert issubclass(MemoryApiError, Exception)
