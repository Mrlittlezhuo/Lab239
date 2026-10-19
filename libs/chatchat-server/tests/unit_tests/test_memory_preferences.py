"""智能聊天记忆第一期（个人偏好记忆）离线测试。

全部使用临时 SQLite 数据库，不连接模型服务、外部网络或真实用户数据库。

覆盖第一期设计方案 §4/§5/§6 的可自动化部分：
- v13 迁移幂等、只加列、旧记忆行与旧用户行完整保留；
- 个人偏好类别白名单（未知类别拒绝，不静默回落）；
- 偏好注入优先级（回答语言 → 输出风格 → 相关背景 → 其余）与数量上限；
- 用户级"个人记忆"总开关：关闭后不再注入，但不删除任何数据；
- 单次请求的"本次不使用个人偏好"（use_memory=False）只影响本轮；
- 清空全部记忆只作用于当前用户；
- 明显敏感内容被拒绝，不静默写入；
- 新端点的未登录 / 禁用用户 / 未改密 / 跨用户隔离。
"""
import asyncio
import sys
import types

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

# 与 test_user_memory 相同：导入 chat 链之前打桩 kb_chat（其导入链会读真实
# 知识库数据库，离线测试无法提供）。
_kb_stub = types.ModuleType("chatchat.server.chat.kb_chat")
_kb_stub.kb_chat = lambda *args, **kwargs: None
sys.modules["chatchat.server.chat.kb_chat"] = _kb_stub

from chatchat.server.api_server.memory_routes import memory_router
from chatchat.server.auth import jwt as auth_jwt
from chatchat.server.auth import service
from chatchat.server.auth.routes_auth import auth_router
from chatchat.server.db.base import Base
from chatchat.server.db.migrate.base import VERSION_TABLE, apply_migrations
from chatchat.server.db.migrate.migrations import build_registry
from chatchat.server.db.models.conversation_model import ConversationModel  # noqa: F401
from chatchat.server.db.models.knowledge_base_model import KnowledgeBaseModel  # noqa: F401
from chatchat.server.db.models.knowledge_file_model import KnowledgeFileModel  # noqa: F401
from chatchat.server.db.models.message_model import MessageModel  # noqa: F401
from chatchat.server.db.models.user_model import UserModel  # noqa: F401
from chatchat.server.db.repository import (
    MemoryNotOwned,
    create_memory,
    delete_all_memories,
    list_memories,
    list_preference_memories,
    update_memory,
)
from chatchat.server.db.session import get_db as db_get_db
from chatchat.server.memory import memory_service


# ---------------------------------------------------------------------------
# fixtures（与 test_user_memory 同一套路：临时库 + 迁移 + ASGI 客户端）
# ---------------------------------------------------------------------------


@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setattr(auth_jwt, "get_secret", lambda: "unit-test-secret-key-0000000000000000")
    return "unit-test-secret-key-0000000000000000"


@pytest.fixture
def db(secret, tmp_path):
    path = tmp_path / "prefs_test.db"
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as conn:
        apply_migrations(build_registry(), conn)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    yield {"Session": Session, "engine": engine}
    engine.dispose()


def _mk_user(db, username, **kwargs):
    s = db["Session"]()
    try:
        u = service.create_user(
            s,
            username=username,
            display_name=username.title(),
            password="password12345678",
            role=kwargs.get("role", "user"),
        )
        u.must_change_password = kwargs.get("must_change_password", False)
        if kwargs.get("disabled"):
            u.status = "disabled"
        s.commit()
        return {"id": u.id, "username": u.username, "auth_version": u.auth_version}
    finally:
        s.close()


def _headers(db, username):
    s = db["Session"]()
    try:
        u = s.query(UserModel).filter(UserModel.username == username).one()
        return {"Authorization": f"Bearer {auth_jwt.create_token(u.id, u.auth_version)}"}
    finally:
        s.close()


@pytest.fixture
def app(db):
    a = FastAPI()
    a.include_router(auth_router)
    a.include_router(memory_router)

    async def override_get_db():
        s = db["Session"]()
        try:
            yield s
        finally:
            s.close()

    a.dependency_overrides[db_get_db] = override_get_db
    return a


@pytest.fixture
def client(app, monkeypatch):
    """同 test_user_memory：本环境 anyio 线程桥连会挂起，直接同步执行端点。"""

    async def direct_call(func, *args, **kwargs):
        return func(*args, **kwargs)

    import fastapi.dependencies.utils
    import fastapi.routing

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
def users(db):
    return {"alice": _mk_user(db, "alice"), "bob": _mk_user(db, "bob")}


def _add(db, owner_id, content, category=None, mtype="preference", importance=1, enabled=True):
    s = db["Session"]()
    try:
        return create_memory(
            s,
            owner_id,
            content,
            mtype=mtype,
            source="user",
            importance=importance,
            enabled=enabled,
            category=category,
        )
    finally:
        s.close()


# ============================================================================
# 1. v13 迁移：只加列、保留旧数据、幂等
# ============================================================================


class TestV13Migration:
    def test_adds_columns_and_keeps_old_rows(self, secret, tmp_path):
        """已有 v12 库升级到 v13：加两列，旧记忆与旧用户行完整保留。"""
        path = tmp_path / "v12_to_v13.db"
        engine = create_engine(f"sqlite:///{path}")
        with engine.connect() as conn:
            conn.execute(
                text(
                    f"CREATE TABLE IF NOT EXISTS {VERSION_TABLE} ("
                    "version INTEGER PRIMARY KEY, name VARCHAR(255) NOT NULL, "
                    "applied_at TIMESTAMP NOT NULL)"
                )
            )
            for version, name in [
                (1, "baseline"), (2, "user_account"), (3, "conversation_owner"),
                (4, "openai_file"), (5, "legacy_conversation_owner"),
                (6, "user_memory"), (7, "agent_run"), (8, "research"),
                (9, "research_evidence"), (10, "research_comparison"),
                (11, "research_report"), (12, "research_run_stage_artifacts"),
            ]:
                conn.execute(
                    text(
                        f"INSERT INTO {VERSION_TABLE} (version, name, applied_at) "
                        "VALUES (:v, :n, CURRENT_TIMESTAMP)"
                    ),
                    {"v": version, "n": name},
                )
            # v6 形状的 memory 表（无 category）+ 一行旧数据
            conn.execute(
                text(
                    "CREATE TABLE memory ("
                    "id VARCHAR(32) PRIMARY KEY, owner_id VARCHAR(32) NOT NULL, "
                    "memory_key VARCHAR(128) NOT NULL, "
                    "type VARCHAR(16) NOT NULL DEFAULT 'fact', "
                    "content VARCHAR(2000) NOT NULL, "
                    "source VARCHAR(16) NOT NULL DEFAULT 'chat', "
                    "source_conversation_id VARCHAR(32), source_message_id VARCHAR(32), "
                    "importance INTEGER NOT NULL DEFAULT 1, "
                    "enabled BOOLEAN NOT NULL DEFAULT 1, "
                    "create_time TIMESTAMP, update_time TIMESTAMP, "
                    "UNIQUE (owner_id, memory_key))"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO memory (id, owner_id, memory_key, type, content) "
                    "VALUES ('m1', 'u1', 'auto:x', 'fact', '旧的自动提取事实')"
                )
            )
            # v6 形状的 user_account（无 memory_preferences_enabled）
            conn.execute(
                text(
                    "CREATE TABLE user_account ("
                    "id VARCHAR(32) PRIMARY KEY, username VARCHAR(64) NOT NULL, "
                    "display_name VARCHAR(100) NOT NULL, password_hash VARCHAR(255) NOT NULL, "
                    "role VARCHAR(16) NOT NULL DEFAULT 'user', "
                    "status VARCHAR(16) NOT NULL DEFAULT 'active', "
                    "must_change_password BOOLEAN NOT NULL DEFAULT 1, "
                    "auth_version INTEGER NOT NULL DEFAULT 1, "
                    "memory_auto_enabled BOOLEAN NOT NULL DEFAULT 1, "
                    "create_time TIMESTAMP, update_time TIMESTAMP)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO user_account (id, username, display_name, password_hash) "
                    "VALUES ('u1', 'old-user', 'Old', 'x')"
                )
            )
            conn.commit()

        with engine.connect() as conn:
            result = apply_migrations(build_registry(), conn)
            assert [m.version for m in result.applied] == [13, 14, 15, 16, 17, 18, 19, 20, 21, 22]
            assert result.final_version == 22
            assert "category" in {c["name"] for c in inspect(conn).get_columns("memory")}
            assert "memory_preferences_enabled" in {
                c["name"] for c in inspect(conn).get_columns("user_account")
            }
            # 旧数据保留：内容不变、category 为 NULL、新用户开关默认开启
            assert (
                conn.execute(text("SELECT content FROM memory WHERE id = 'm1'")).scalar()
                == "旧的自动提取事实"
            )
            assert (
                conn.execute(text("SELECT category FROM memory WHERE id = 'm1'")).scalar()
                is None
            )
            assert (
                conn.execute(
                    text(
                        "SELECT memory_preferences_enabled FROM user_account WHERE id = 'u1'"
                    )
                ).scalar()
                == 1
            )
            assert "ix_memory_owner_category" in {
                i["name"] for i in inspect(conn).get_indexes("memory")
            }

        # 幂等：再跑一次不重复执行，也不改动数据
        with engine.connect() as conn:
            result2 = apply_migrations(build_registry(), conn)
            assert result2.applied == []
            assert result2.final_version == 22
            assert conn.execute(text("SELECT COUNT(*) FROM memory")).scalar() == 1
        engine.dispose()


# ============================================================================
# 2. 类别白名单与偏好读取/清空
# ============================================================================


class TestCategoryRepository:
    def test_unknown_category_rejected(self, db):
        with pytest.raises(ValueError):
            _add(db, "u1", "内容", category="not_a_category")

    def test_category_roundtrip_and_clear(self, db):
        m = _add(db, "u1", "默认中文回答", category="answer_style")
        assert m.category == "answer_style"
        s = db["Session"]()
        try:
            m2 = update_memory(s, m.id, "u1", category="")
            assert m2.category is None
        finally:
            s.close()

    def test_list_preference_memories_filters_owner_enabled_and_category(self, db):
        _add(db, "alice", "默认中文回答", category="answer_style")
        _add(db, "alice", "已停用的偏好", category="work_style", enabled=False)
        _add(db, "alice", "自动提取的事实", category=None, mtype="fact")
        _add(db, "bob", "bob 的偏好", category="answer_style")
        got = list_preference_memories(db["Session"](), "alice")
        contents = {m.content for m in got}
        assert contents == {"默认中文回答"}

    def test_delete_all_only_touches_owner(self, db):
        _add(db, "alice", "a1", category="answer_style")
        _add(db, "alice", "a2", category=None, mtype="fact")
        _add(db, "bob", "b1", category="answer_style")
        s = db["Session"]()
        try:
            assert delete_all_memories(s, "alice") == 2
            assert list_memories(s, "alice") == []
            assert len(list_memories(s, "bob")) == 1
        finally:
            s.close()

    def test_cross_owner_update_still_rejected(self, db):
        m = _add(db, "alice", "默认中文回答", category="answer_style")
        s = db["Session"]()
        try:
            with pytest.raises(MemoryNotOwned):
                update_memory(s, m.id, "bob", category="work_style")
        finally:
            s.close()


# ============================================================================
# 3. 敏感内容识别
# ============================================================================


class TestSensitiveGuard:
    @pytest.mark.parametrize(
        "content",
        [
            "我的密码是 hunter2hunter2",
            "password: LetMeIn2026",
            "api_key = sk-abcdefghijklmnop1234",
            "sk-abcdefghijklmnop1234",
            "Authorization: Bearer abcdefghijklmnop1234",
            "身份证号 110101199003071234",
            "银行卡 6222021234567890123",
        ],
    )
    def test_blocks_obvious_secrets(self, content):
        assert memory_service.sensitive_reason(content)

    @pytest.mark.parametrize(
        "content",
        [
            "默认中文回答，先给结论再解释",
            "正在开发 LangGraph Agent 平台",
            "改代码前先给方案，优先解释风险",
            "回答里不要出现密码这个词",
            "我的手机号是 13800001111",
        ],
    )
    def test_keeps_normal_preferences(self, content):
        assert memory_service.sensitive_reason(content) is None


# ============================================================================
# 4. 偏好挑选：优先级与上限
# ============================================================================


class TestSelectPreferences:
    def test_language_before_style_before_background(self, db):
        owner = "alice"
        _add(db, owner, "偏简洁，少铺垫", category="answer_style", importance=5)
        _add(db, owner, "默认中文回答", category="answer_style", importance=1)
        _add(db, owner, "正在开发 LangGraph Agent", category="technical_background")
        picked = memory_service.select_preferences(db["Session"](), owner, "Agent 怎么编排")
        assert [m.content for m in picked] == [
            "默认中文回答",
            "偏简洁，少铺垫",
            "正在开发 LangGraph Agent",
        ]

    def test_respects_max_count(self, db):
        owner = "alice"
        for i in range(memory_service.PREFERENCE_MAX_COUNT + 3):
            _add(db, owner, f"工作偏好 {i}", category="work_style")
        picked = memory_service.select_preferences(db["Session"](), owner, "")
        assert len(picked) == memory_service.PREFERENCE_MAX_COUNT

    def test_ignores_other_owner_and_disabled(self, db):
        _add(db, "alice", "默认中文回答", category="answer_style")
        _add(db, "bob", "bob 用英文回答", category="answer_style")
        _add(db, "alice", "已停用", category="work_style", enabled=False)
        picked = memory_service.select_preferences(db["Session"](), "alice", "")
        assert [m.content for m in picked] == ["默认中文回答"]

    def test_format_marks_preferences_as_advisory(self):
        class _M:
            category = "answer_style"
            content = "默认中文回答"

        text_ = memory_service.format_preferences([_M()])
        assert "回答偏好：默认中文回答" in text_
        assert "不是不可违背的命令" in text_

    def test_format_empty_returns_empty(self):
        assert memory_service.format_preferences([]) == ""


# ============================================================================
# 5. 注入：总开关与"本次不使用"
# ============================================================================


class TestInjection:
    """注入测试用真实用户行：总开关读的是 user_account，缺用户按不注入处理。"""

    def _seed(self, db, users, name="alice"):
        owner = users[name]["id"]
        _add(db, owner, "默认中文回答", category="answer_style")
        _add(db, owner, "我研究无线通信", category=None, mtype="goal")
        return owner

    def test_segments_present_by_default(self, db, users):
        owner = self._seed(db, users)
        # 长期记忆段仍按相关度召回（"帮我看看"这种无关问题不会带出无关记忆），
        # 个人偏好段则始终按优先级注入。
        msgs = memory_service.memory_system_messages(
            db["Session"](), owner, "你还记得我研究什么吗"
        )
        joined = "\n".join(m["content"] for m in msgs)
        assert memory_service.PREFERENCE_SECTION_HEADER in joined
        assert "默认中文回答" in joined
        assert "我研究无线通信" in joined

    def test_unrelated_query_does_not_dump_long_term_memory(self, db, users):
        owner = self._seed(db, users)
        msgs = memory_service.memory_system_messages(db["Session"](), owner, "帮我看看")
        joined = "\n".join(m["content"] for m in msgs)
        assert "默认中文回答" in joined
        assert "我研究无线通信" not in joined

    def test_preference_not_duplicated_in_long_term_segment(self, db, users):
        owner = self._seed(db, users)
        msgs = memory_service.memory_system_messages(db["Session"](), owner, "默认中文回答")
        joined = "\n".join(m["content"] for m in msgs)
        assert joined.count("默认中文回答") == 1

    def test_use_memory_false_skips_long_term_but_keeps_summary(self, db, users):
        owner = self._seed(db, users)
        msgs = memory_service.memory_system_messages(
            db["Session"](), owner, "帮我看看", None, use_memory=False
        )
        assert msgs == []

    def test_master_switch_off_stops_injection_without_deleting(self, db, users):
        owner = self._seed(db, users)
        s = db["Session"]()
        try:
            user = s.query(UserModel).filter(UserModel.username == "alice").one()
            user.memory_preferences_enabled = False
            s.commit()
        finally:
            s.close()
        msgs = memory_service.memory_system_messages(db["Session"](), owner, "帮我看看")
        assert msgs == []
        # 数据仍在，重新开启即恢复
        assert len(list_preference_memories(db["Session"](), owner)) == 1
        s = db["Session"]()
        try:
            user = s.query(UserModel).filter(UserModel.username == "alice").one()
            user.memory_preferences_enabled = True
            s.commit()
        finally:
            s.close()
        assert memory_service.memory_system_messages(db["Session"](), owner, "帮我看看")

    def test_missing_user_does_not_inject(self, db, users):
        self._seed(db, users)
        assert memory_service.memory_system_messages(db["Session"](), "ghost", "帮我看看") == []


# ============================================================================
# 6. API：类别、敏感拦截、偏好端点、清空、鉴权与隔离
# ============================================================================


class TestPreferencesApi:
    def test_create_with_category_echoed(self, client, db, users):
        r = client.post(
            "/memories",
            headers=_headers(db, "alice"),
            json={"content": "默认中文回答", "type": "preference", "category": "answer_style"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["category"] == "answer_style"
        assert body["content"] == "默认中文回答"
        # 响应不回传 owner_id
        assert "owner_id" not in body

    def test_unknown_category_rejected(self, client, db, users):
        r = client.post(
            "/memories",
            headers=_headers(db, "alice"),
            json={"content": "随便一条", "category": "not_a_category"},
        )
        assert r.status_code == 400
        s = db["Session"]()
        try:
            assert list_memories(s, users["alice"]["id"]) == []
        finally:
            s.close()

    def test_sensitive_content_rejected_and_not_stored(self, client, db, users):
        r = client.post(
            "/memories",
            headers=_headers(db, "alice"),
            json={"content": "我的密码是 hunter2hunter2", "category": "answer_style"},
        )
        assert r.status_code == 400
        assert "密码" in r.json()["detail"]
        s = db["Session"]()
        try:
            assert list_memories(s, users["alice"]["id"]) == []
        finally:
            s.close()

    def test_update_to_sensitive_content_rejected(self, client, db, users):
        created = client.post(
            "/memories",
            headers=_headers(db, "alice"),
            json={"content": "默认中文回答", "category": "answer_style"},
        ).json()
        r = client.patch(
            f"/memories/{created['id']}",
            headers=_headers(db, "alice"),
            json={"content": "api_key = sk-abcdefghijklmnop1234"},
        )
        assert r.status_code == 400
        s = db["Session"]()
        try:
            m = list_memories(s, users["alice"]["id"])[0]
            assert m.content == "默认中文回答"
        finally:
            s.close()

    def test_preferences_endpoint_lists_ordered_and_capped(self, client, db, users):
        owner = users["alice"]["id"]
        _add(db, owner, "偏简洁，少铺垫", category="answer_style", importance=5)
        _add(db, owner, "默认中文回答", category="answer_style", importance=1)
        _add(db, owner, "自动提取的事实", category=None, mtype="fact")
        r = client.get("/memories/preferences", headers=_headers(db, "alice"))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["enabled"] is True
        assert body["max_count"] == memory_service.PREFERENCE_MAX_COUNT
        assert [m["content"] for m in body["preferences"]] == [
            "默认中文回答",
            "偏简洁，少铺垫",
        ]

    def test_master_switch_off_keeps_data(self, client, db, users):
        owner = users["alice"]["id"]
        _add(db, owner, "默认中文回答", category="answer_style")
        r = client.patch(
            "/memories/preferences?enabled=false", headers=_headers(db, "alice")
        )
        assert r.status_code == 200, r.text
        assert r.json() == {"enabled": False}
        body = client.get("/memories/preferences", headers=_headers(db, "alice")).json()
        assert body["enabled"] is False
        assert body["preferences"] == []
        # 关闭不删除数据
        s = db["Session"]()
        try:
            assert len(list_memories(s, owner)) == 1
        finally:
            s.close()
        # 重新开启即恢复
        assert client.patch(
            "/memories/preferences?enabled=true", headers=_headers(db, "alice")
        ).json() == {"enabled": True}
        body = client.get("/memories/preferences", headers=_headers(db, "alice")).json()
        assert [m["content"] for m in body["preferences"]] == ["默认中文回答"]

    def test_clear_all_only_clears_current_user(self, client, db, users):
        _add(db, users["alice"]["id"], "a1", category="answer_style")
        _add(db, users["alice"]["id"], "a2", category=None, mtype="fact")
        _add(db, users["bob"]["id"], "b1", category="answer_style")
        r = client.delete("/memories", headers=_headers(db, "alice"))
        assert r.status_code == 200, r.text
        assert r.json()["deleted"] == 2
        s = db["Session"]()
        try:
            assert list_memories(s, users["alice"]["id"]) == []
            assert len(list_memories(s, users["bob"]["id"])) == 1
        finally:
            s.close()
        body = client.get("/memories/preferences", headers=_headers(db, "alice")).json()
        assert body["preferences"] == []

    def test_preferences_endpoint_never_shows_other_user(self, client, db, users):
        _add(db, users["bob"]["id"], "bob 用英文回答", category="answer_style")
        body = client.get("/memories/preferences", headers=_headers(db, "alice")).json()
        assert body["preferences"] == []

    @pytest.mark.parametrize(
        "method,url",
        [
            ("get", "/memories/preferences"),
            ("patch", "/memories/preferences?enabled=false"),
            ("delete", "/memories"),
        ],
    )
    def test_unauthenticated_rejected(self, client, users, method, url):
        r = getattr(client, method)(url)
        assert r.status_code == 401

    def test_disabled_user_rejected(self, client, db, secret):
        _mk_user(db, "carol", disabled=True)
        r = client.get("/memories/preferences", headers=_headers(db, "carol"))
        assert r.status_code == 401

    def test_must_change_password_rejected(self, client, db, secret):
        _mk_user(db, "dave", must_change_password=True)
        r = client.get("/memories/preferences", headers=_headers(db, "dave"))
        assert r.status_code == 403
        r2 = client.delete("/memories", headers=_headers(db, "dave"))
        assert r2.status_code == 403
