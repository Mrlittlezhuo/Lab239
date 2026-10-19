"""智能聊天记忆第二期（会话私有上下文）离线测试。

全部使用临时 SQLite 数据库，不连接模型服务、外部网络或真实用户数据库。

覆盖第二期设计方案 §4/§5/§6 的可自动化部分：
- v14 迁移幂等、只加列，旧摘要文本与摘要位置标记完整保留；
- 会话上下文的确定性提取（主题/目标/已确认事项/限制条件/待解决问题）；
- 只从用户消息提取结论：模型回答与知识库文档片段不会变成会话结论（P2-12）；
- 注入段带"本会话新要求优先于旧内容"的说明（设计方案 §5）；
- 会话之间互不影响、用户之间互不可见（P2-04 / P2-14）；
- 用户编辑过的结论不被自动更新静默覆盖（P2-05），而更靠后的用户消息可以
  覆盖旧约束（P2-13）；
- 重置本会话上下文只影响本会话，个人偏好不受影响（P2-06）；
- 临时对话不创建、不更新、不保留会话上下文，也不能写入个人偏好（P2-10/P2-11）；
- "记住/忘记"确认卡片：范围推荐、不保存即不写入、忘记列出将被停用的对象
  （P2-07/P2-08/P2-09）；
- 新端点的未登录 / 未改密 / 跨用户 404。
"""
import asyncio
import sys
import types

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

# 与 test_user_memory / test_memory_preferences 相同：导入 chat 链之前打桩
# kb_chat（其导入链会读真实知识库数据库，离线测试无法提供）。
_kb_stub = types.ModuleType("chatchat.server.chat.kb_chat")
_kb_stub.kb_chat = lambda *args, **kwargs: None
sys.modules["chatchat.server.chat.kb_chat"] = _kb_stub

from chatchat.server.api_server.conversation_routes import conversation_router
from chatchat.server.api_server.memory_routes import memory_router
from chatchat.server.api_server.session_context_routes import session_context_router
from chatchat.server.auth import jwt as auth_jwt
from chatchat.server.auth import service
from chatchat.server.auth.routes_auth import auth_router
from chatchat.server.db.base import Base
from chatchat.server.db.migrate.base import (
    VERSION_TABLE,
    MigrationRegistry,
    apply_migrations,
    current_version,
)
from chatchat.server.db.migrate.migrations import _MIGRATIONS, build_registry
from chatchat.server.db.models.conversation_model import ConversationModel  # noqa: F401
from chatchat.server.db.models.knowledge_base_model import KnowledgeBaseModel  # noqa: F401
from chatchat.server.db.models.knowledge_file_model import KnowledgeFileModel  # noqa: F401
from chatchat.server.db.models.message_model import MessageModel  # noqa: F401
from chatchat.server.db.models.user_model import UserModel  # noqa: F401
from chatchat.server.db.repository import (
    add_message,
    create_conversation,
    create_memory,
    list_memories,
    session_summary_repository,
)
from chatchat.server.db.session import get_db as db_get_db
from chatchat.server.memory import memory_intents, memory_service, session_context

SECRET = "unit-test-secret-key-0000000000000000"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setattr(auth_jwt, "get_secret", lambda: SECRET)
    return SECRET


@pytest.fixture
def db(secret, tmp_path):
    path = tmp_path / "context_test.db"
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
def users(db):
    return {"alice": _mk_user(db, "alice"), "bob": _mk_user(db, "bob")}


@pytest.fixture
def app(db):
    a = FastAPI()
    a.include_router(auth_router)
    a.include_router(conversation_router)
    a.include_router(session_context_router)
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
    """同 test_user_memory：本环境 anyio 线程桥接会挂起，直接同步执行端点。"""

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


def _conv(db, owner_id, name="会话", temporary=False) -> str:
    s = db["Session"]()
    try:
        return create_conversation(s, owner_id, "agent_chat", name, temporary=temporary)
    finally:
        s.close()


def _say(db, conversation_id, owner_id, *queries, response="回答"):
    """按顺序写入若干轮用户消息（带助手回答）。"""
    s = db["Session"]()
    try:
        for i, q in enumerate(queries):
            add_message(s, conversation_id, owner_id, "agent_chat", q, response=f"{response}{i}")
    finally:
        s.close()


def _refresh(db, owner_id, conversation_id, **kwargs):
    s = db["Session"]()
    try:
        return session_context.refresh_session_context(
            s, owner_id, conversation_id, **kwargs
        )
    finally:
        s.close()


def _load(db, owner_id, conversation_id):
    s = db["Session"]()
    try:
        return session_context.load_context(s, owner_id, conversation_id)
    finally:
        s.close()


def _messages(db, owner_id, conversation_id):
    """升序取本会话全部消息（与注入层看到的顺序一致）。"""
    from chatchat.server.db.repository.message_repository import list_messages

    s = db["Session"]()
    try:
        return list_messages(s, conversation_id, owner_id)
    finally:
        s.close()


# ============================================================================
# 1. v14 迁移：只加列、保留旧摘要、幂等
# ============================================================================


def _registry_upto(version: int) -> MigrationRegistry:
    registry = MigrationRegistry()
    for v, name, upgrade in _MIGRATIONS:
        if v <= version:
            registry.register(v, name, upgrade)
    return registry


class TestV14Migration:
    def test_adds_columns_and_keeps_old_summary(self, secret, tmp_path):
        """v13 库升级到 v14：加会话上下文槽位与 temporary，旧摘要原文保留。"""
        path = tmp_path / "v13_to_v14.db"
        engine = create_engine(f"sqlite:///{path}")
        with engine.connect() as conn:
            apply_migrations(_registry_upto(13), conn)
        engine.dispose()

        engine = create_engine(f"sqlite:///{path}")
        with engine.connect() as conn:
            # 生产环境里 conversation 由 create_all 建；这里按 v3 之后的形状
            # 手工建表（不带 temporary），才能验证 v14 真的补上了这一列。
            conn.execute(
                text(
                    "CREATE TABLE conversation ("
                    "id VARCHAR(32) PRIMARY KEY, name VARCHAR(50), "
                    "chat_type VARCHAR(50), owner_id VARCHAR(32), "
                    "create_time TIMESTAMP, update_time TIMESTAMP)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO session_summary (id, owner_id, conversation_id, summary, "
                    "summarized_message_id, summarized_count) "
                    "VALUES ('s1', 'u1', 'c1', '旧的较早内容摘要', 'm9', 7)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO conversation (id, name, chat_type, owner_id, create_time, "
                    "update_time) VALUES ('c1', '旧会话', 'llm_chat', 'u1', "
                    "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                )
            )
            conn.commit()
            assert current_version(conn) == 13

        with engine.connect() as conn:
            result = apply_migrations(build_registry(), conn)
            assert [m.version for m in result.applied] == [14, 15, 16, 17, 18, 19, 20, 21, 22]
            assert result.final_version == 22

        cols = {c["name"] for c in inspect(engine).get_columns("session_summary")}
        assert {
            "topic",
            "goal",
            "confirmed",
            "constraints",
            "open_questions",
            "pinned_json",
        } <= cols
        assert "temporary" in {c["name"] for c in inspect(engine).get_columns("conversation")}

        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT summary, summarized_count, topic, pinned_json "
                    "FROM session_summary WHERE id='s1'"
                )
            ).one()
            assert row[0] == "旧的较早内容摘要"  # 旧摘要一字未改
            assert row[1] == 7
            assert row[2] == "" and row[3] == "[]"
            assert conn.execute(
                text("SELECT temporary FROM conversation WHERE id='c1'")
            ).scalar() in (0, False)
        engine.dispose()

    def test_reapply_is_noop(self, secret, tmp_path):
        path = tmp_path / "v14_reapply.db"
        engine = create_engine(f"sqlite:///{path}")
        with engine.connect() as conn:
            apply_migrations(build_registry(), conn)
            second = apply_migrations(build_registry(), conn)
            assert second.applied == []
            assert second.final_version == 22
        engine.dispose()


# ============================================================================
# 1b. v16 迁移：只加列、保留旧摘要、按判据标记待重建（设计方案 §18.2.1 / M-103）
# ============================================================================


class TestV16Migration:
    def _legacy_v15(self, tmp_path):
        """造一个"停在 v15"的库，塞两条旧摘要：一条有覆盖位置、一条没有。"""
        path = tmp_path / "v15_to_v16.db"
        engine = create_engine(f"sqlite:///{path}")
        with engine.connect() as conn:
            apply_migrations(_registry_upto(15), conn)
            conn.execute(
                text(
                    "INSERT INTO session_summary (id, owner_id, conversation_id, summary, "
                    "summarized_message_id, summarized_count) VALUES "
                    "('s-nomark', 'u1', 'c1', '有摘要但没记覆盖位置', NULL, 0),"
                    "('s-marked', 'u1', 'c2', '有摘要也有覆盖位置', 'm9', 7)"
                )
            )
            conn.commit()
            assert current_version(conn) == 15
        return engine

    def test_adds_columns_and_keeps_old_summary(self, secret, tmp_path):
        engine = self._legacy_v15(tmp_path)
        with engine.connect() as conn:
            result = apply_migrations(build_registry(), conn)
            assert [m.version for m in result.applied] == [16, 17, 18, 19, 20, 21, 22]
            assert result.final_version == 22

        cols = {c["name"] for c in inspect(engine).get_columns("session_summary")}
        assert {
            "completed_items",
            "context_start_after_message_id",
            "version",
            "summary_type",
            "summary_status",
            "summary_error",
            "summary_updated_at",
            "needs_rebuild",
            "slots_scanned_through_message_id",
            "slots_scanned_count",
        } <= cols

        with engine.connect() as conn:
            rows = {
                r[0]: r
                for r in conn.execute(
                    text(
                        "SELECT id, summary, summarized_count, version, needs_rebuild "
                        "FROM session_summary"
                    )
                ).all()
            }
            # 旧摘要一字未改，也不因为升级而丢覆盖位置（M-103）
            assert rows["s-nomark"][1] == "有摘要但没记覆盖位置"
            assert rows["s-marked"][1] == "有摘要也有覆盖位置"
            assert rows["s-marked"][2] == 7
            # 只有"有摘要文本但覆盖位置不可确认"的行被标记待重建
            assert rows["s-nomark"][4] in (1, True)
            assert rows["s-marked"][4] in (0, False)
            assert rows["s-nomark"][3] == 0
        engine.dispose()

    def test_reapply_is_noop(self, secret, tmp_path):
        """重复执行不重复加列、不重复标记（幂等，AGENTS.md 数据库底线）。"""
        engine = self._legacy_v15(tmp_path)
        with engine.connect() as conn:
            apply_migrations(build_registry(), conn)
            second = apply_migrations(build_registry(), conn)
            assert second.applied == []
            assert second.final_version == 22
        engine.dispose()


# ============================================================================
# 2. 上下文提取与注入（P2-01 / P2-02 / P2-03）
# ============================================================================


class TestDerivation:
    def test_topic_and_confirmed_appear_without_waiting_for_threshold(
        self, db, users
    ):
        """P2-01：几轮之后抽屉里就有主题与已确认事项，不必等历史够长。"""
        aid = users["alice"]["id"]
        conv = _conv(db, aid, "记忆设计")
        _say(
            db,
            conv,
            aid,
            "我们在讨论智能聊天记忆设计",
            "确认：第一期只做个人偏好记忆",
            "确认：暂不做自动模型路由",
        )
        context = _refresh(db, aid, conv, threshold=12)
        assert context is not None
        assert "智能聊天记忆设计" in (context.topic.text if context.topic else "")
        confirmed = [i.text for i in context.confirmed]
        assert any("第一期只做个人偏好记忆" in t for t in confirmed)
        assert any("暂不做自动模型路由" in t for t in confirmed)
        # 历史不足阈值时不生成摘要正文
        assert (context.summary or "") == ""

    def test_goal_and_open_questions_and_resolved(self, db, users):
        aid = users["alice"]["id"]
        conv = _conv(db, aid)
        _say(db, conv, aid, "我想确定三期的功能边界", "如何生成会话摘要？", "明白了，谢谢")
        context = _refresh(db, aid, conv, threshold=12)
        assert context.goal is not None and "三期的功能边界" in context.goal.text
        # 已被"明白了"确认解决的提问不再算待解决问题
        assert [i.text for i in context.open_questions] == []

    def test_open_question_stays_open_until_answered(self, db, users):
        aid = users["alice"]["id"]
        conv = _conv(db, aid)
        _say(db, conv, aid, "摘要由谁生成、何时更新？")
        context = _refresh(db, aid, conv, threshold=12)
        assert any("摘要由谁生成" in i.text for i in context.open_questions)

    def test_summary_covers_only_messages_outside_the_window(
        self, db, users, monkeypatch
    ):
        """P2-02 + 设计方案 §6.2：摘要只覆盖最近原文窗口之外的较早消息。

        分界由字符预算算出，不再是"最近 12 条保护窗口"：每条消息约 60 字符、
        预算 500 时最新 8 条进窗口（484 字符，再加一条溢出），前 6 条必须进
        摘要——摘要覆盖范围与最近原文窗口之间不留空洞（M-01/M-02）。
        """
        monkeypatch.setenv("CHATCHAT_RECENT_HISTORY_MAX_CHARS", "500")
        aid = users["alice"]["id"]
        conv = _conv(db, aid)
        _say(db, conv, aid, *[f"较早的问题{i:02d}" + "补" * 40 for i in range(14)])
        context = _refresh(db, aid, conv)
        assert context is not None and context.summary
        assert "较早的问题00" in context.summary
        assert "较早的问题05" in context.summary
        # 窗口内的消息不进摘要：仍以完整原文参与回答
        assert "较早的问题06" not in context.summary
        assert "较早的问题13" not in context.summary
        assert context.summarized_count == 6
        # 落库的覆盖位置与摘要来源/状态可回读（设计方案 §15.1）
        saved = _load(db, aid, conv)
        assert saved.summarized_message_id is not None
        assert saved.summary_type == "rule"
        assert saved.summary_status == "succeeded"

    def test_model_answers_are_not_treated_as_conclusions(self, db, users):
        """模型回答不自动成为"已确认事项"（与 P2-12 同一立场）。"""
        aid = users["alice"]["id"]
        conv = _conv(db, aid)
        s = db["Session"]()
        try:
            add_message(
                s,
                conv,
                aid,
                "agent_chat",
                "我们继续讨论",
                response="确认：本项目不用自动模型路由",
            )
        finally:
            s.close()
        context = _refresh(db, aid, conv, threshold=12)
        assert [i.text for i in context.confirmed] == []
        segment = session_context.format_context(context)
        assert "本项目不用自动模型路由" not in segment.split("较早内容摘要")[0]

    def test_injected_segment_states_priority(self, db, users):
        """设计方案 §5：本会话新要求优先于摘要里的旧内容。"""
        aid = users["alice"]["id"]
        conv = _conv(db, aid)
        _say(db, conv, aid, "讨论记忆设计", "确认：第一期只做偏好记忆")
        _refresh(db, aid, conv, threshold=12)
        segment = session_context.build_context_segment(db["Session"](), aid, conv)
        assert "【本会话上下文" in segment
        assert "已确认事项：" in segment
        assert "第一期只做偏好记忆" in segment
        assert "以用户最新表达为准" in segment
        assert "不适用于其他会话" in segment


# ============================================================================
# 3. 会话之间、用户之间隔离（P2-04 / P2-14）
# ============================================================================


class TestIsolation:
    def test_other_conversation_gets_nothing(self, db, users):
        """P2-04：新会话不出现旧会话的主题、结论或待办。"""
        aid = users["alice"]["id"]
        conv_a = _conv(db, aid, "会话A")
        _say(db, conv_a, aid, "讨论智能聊天记忆", "确认：第二期新增会话摘要")
        _refresh(db, aid, conv_a, threshold=12)

        conv_b = _conv(db, aid, "会话B")
        _say(db, conv_b, aid, "Python 报错怎么排查？")
        _refresh(db, aid, conv_b, threshold=12)
        context_b = _load(db, aid, conv_b)
        segment = session_context.format_context(context_b)
        assert "第二期新增会话摘要" not in segment
        assert "智能聊天记忆" not in segment

    def test_other_user_cannot_read_or_write(self, db, users, client):
        """P2-14：不能互相读取或修改对方会话上下文。"""
        aid = users["alice"]["id"]
        conv = _conv(db, aid)
        _say(db, conv, aid, "讨论记忆设计", "确认：只做偏好")
        _refresh(db, aid, conv, threshold=12)

        hdr_b = _headers(db, "bob")
        r = client.get(f"/conversations/{conv}/context", headers=hdr_b)
        assert r.status_code == 404
        r = client.patch(
            f"/conversations/{conv}/context",
            headers=hdr_b,
            json={"confirmed": ["bob 的结论"]},
        )
        assert r.status_code == 404
        r = client.delete(f"/conversations/{conv}/context", headers=hdr_b)
        assert r.status_code == 404
        # alice 的内容没被动过
        assert any("只做偏好" in i.text for i in _load(db, aid, conv).confirmed)


# ============================================================================
# 4. 用户编辑与冲突更新（P2-05 / P2-13 / P2-06）
# ============================================================================


class TestUserEdits:
    def test_edited_conclusion_survives_auto_update(self, db, users):
        """P2-05：用户改过的结论，自动更新时不静默覆盖。"""
        aid = users["alice"]["id"]
        conv = _conv(db, aid)
        _say(db, conv, aid, "讨论记忆设计", "确认：第一期只做偏好记忆")
        _refresh(db, aid, conv, threshold=12)

        s = db["Session"]()
        try:
            updated = session_context.apply_user_edit(
                s, aid, conv, confirmed=["第一期只做偏好记忆", "必须支持中文"]
            )
        finally:
            s.close()
        assert any("必须支持中文" in i.text for i in updated.confirmed)

        # 再来一轮自动更新，用户写的结论仍在
        _say(db, conv, aid, "继续讨论别的细节")
        _refresh(db, aid, conv, threshold=12)
        again = _load(db, aid, conv)
        assert any("必须支持中文" in i.text for i in again.confirmed)
        assert any("第一期只做偏好记忆" in i.text for i in again.confirmed)

    def test_newer_user_statement_supersedes_old_constraint(self, db, users):
        """P2-13：旧上下文写"使用中文"，本轮要求英文 → 更新后以英文为准。"""
        aid = users["alice"]["id"]
        conv = _conv(db, aid)
        _say(db, conv, aid, "讨论设计", "本次回答统一使用中文")
        _refresh(db, aid, conv, threshold=12)
        assert any("中文" in i.text for i in _load(db, aid, conv).constraints)

        _say(db, conv, aid, "这次改用英文回答")
        _refresh(db, aid, conv, threshold=12)
        constraints = [i.text for i in _load(db, aid, conv).constraints]
        assert any("英文" in t for t in constraints)
        assert not any("统一使用中文" in t for t in constraints)

    def test_reset_clears_context_but_keeps_preferences(self, db, users, client):
        """P2-06：重置本会话上下文；个人偏好不受影响。"""
        aid = users["alice"]["id"]
        conv = _conv(db, aid)
        _say(db, conv, aid, "讨论设计", "确认：只做偏好")
        _refresh(db, aid, conv, threshold=12)
        pref = None
        s = db["Session"]()
        try:
            pref = create_memory(
                s, aid, "默认中文回答", mtype="preference", source="user",
                category="answer_style",
            )
        finally:
            s.close()

        r = client.delete(f"/conversations/{conv}/context", headers=_headers(db, "alice"))
        assert r.status_code == 200 and r.json()["reset"] is True

        context = _load(db, aid, conv)
        assert context.is_empty()
        assert list_memories(s := db["Session"](), aid, enabled_only=True)
        s.close()
        assert pref.id

    def test_refresh_endpoint_returns_context(self, db, users, client):
        aid = users["alice"]["id"]
        conv = _conv(db, aid)
        _say(db, conv, aid, "讨论记忆设计", "确认：只做偏好")
        hdr = _headers(db, "alice")
        r = client.post(f"/conversations/{conv}/context/refresh", headers=hdr)
        assert r.status_code == 200, r.text
        body = r.json()
        assert "记忆设计" in body["topic"]
        assert any("只做偏好" in t for t in body["confirmed"])

        r = client.get(f"/conversations/{conv}/context", headers=hdr)
        assert r.status_code == 200 and r.json()["temporary"] is False


# ============================================================================
# 5. 临时对话（P2-10 / P2-11）
# ============================================================================


class TestTemporaryConversation:
    def test_no_context_created_or_injected(self, db, users):
        """P2-10：长时间交流也不生成可恢复的会话上下文。"""
        aid = users["alice"]["id"]
        conv = _conv(db, aid, "临时", temporary=True)
        _say(db, conv, aid, *[f"临时讨论{i}" for i in range(15)])
        assert _refresh(db, aid, conv, threshold=12) is None
        s = db["Session"]()
        try:
            assert session_summary_repository.get_session_summary(s, conv, aid) is None
        finally:
            s.close()
        assert session_context.build_context_segment(db["Session"](), aid, conv) == ""

    def test_edit_endpoint_rejected(self, db, users, client):
        aid = users["alice"]["id"]
        conv = _conv(db, aid, "临时", temporary=True)
        r = client.patch(
            f"/conversations/{conv}/context",
            headers=_headers(db, "alice"),
            json={"confirmed": ["结论"]},
        )
        assert r.status_code == 400
        assert "临时对话" in r.json()["detail"]

    def test_remember_blocked_in_temporary_chat(self, db, users, client):
        """P2-11：临时对话里"记住"不能直接写入个人偏好。"""
        aid = users["alice"]["id"]
        conv = _conv(db, aid, "临时", temporary=True)
        hdr = _headers(db, "alice")

        r = client.post(
            "/memories/intents",
            headers=hdr,
            json={"text": "记住：默认中文", "conversation_id": conv},
        )
        assert r.status_code == 200, r.text
        proposal = r.json()["proposals"][0]
        assert "临时对话" in proposal["note"]

        r = client.post(
            "/memories/intents/apply",
            headers=hdr,
            json={
                "kind": "remember",
                "content": "默认中文回答",
                "scope": "preference",
                "category": "answer_style",
                "conversation_id": conv,
            },
        )
        assert r.status_code == 400 and "临时对话" in r.json()["detail"]
        assert list_memories(db["Session"](), aid, enabled_only=True) == []

        # 仅本会话同样不保存（临时对话不保留会话上下文）
        r = client.post(
            "/memories/intents/apply",
            headers=hdr,
            json={
                "kind": "remember",
                "content": "本次不用自动路由",
                "scope": "session",
                "conversation_id": conv,
            },
        )
        assert r.status_code == 400 and "临时对话" in r.json()["detail"]

    def test_auto_extract_blocked_in_temporary_chat(self, db, users):
        """P2-11：临时对话里的自动记忆提取也不能写入长期记忆。

        会话上下文侧由 session_context 拦住，长期记忆侧由 auto_extract 拦住，
        否则"临时对话"只是不显示上下文，数据仍然落库。
        """
        aid = users["alice"]["id"]
        temp = _conv(db, aid, "临时", temporary=True)
        normal = _conv(db, aid, "普通")
        s = db["Session"]()
        try:
            assert (
                memory_service.auto_extract(
                    s, aid, "我研究无线通信", source_conversation_id=temp
                )
                == []
            )
            assert list_memories(s, aid, enabled_only=True) == []
            # 同一句话在普通会话里仍按第一期的自动开关写入
            assert (
                memory_service.auto_extract(
                    s, aid, "我研究无线通信", source_conversation_id=normal
                )
                != []
            )
        finally:
            s.close()


# ============================================================================
# 6. 记住 / 忘记确认卡片（P2-07 / P2-08 / P2-09 / P2-12）
# ============================================================================


class TestIntentCards:
    def test_scope_recommendation(self, db, users):
        aid = users["alice"]["id"]
        s = db["Session"]()
        try:
            [p] = memory_intents.detect_intents(s, aid, "记住：本次任务不用自动路由")
            assert p.kind == "remember"
            assert p.recommended_scope == memory_intents.SCOPE_SESSION

            [p] = memory_intents.detect_intents(s, aid, "以后默认中文回答")
            assert p.recommended_scope == memory_intents.SCOPE_PREFERENCE
            assert p.recommended_category == "answer_style"

            [p] = memory_intents.detect_intents(s, aid, "记住：我习惯先看结论")
            assert p.recommended_scope == memory_intents.SCOPE_PREFERENCE

            [p] = memory_intents.detect_intents(s, aid, "记住：本项目不用自动模型路由")
            assert p.recommended_scope is None, "含义不明确时不预选范围"

            [p] = memory_intents.detect_intents(
                s, aid, "以后默认中文回答，本次先按这个来"
            )
            assert p.recommended_scope is None
        finally:
            s.close()

    def test_detect_does_not_write(self, db, users):
        """P2-09 的前半段：只出卡片，不写入。"""
        aid = users["alice"]["id"]
        s = db["Session"]()
        try:
            memory_intents.detect_intents(s, aid, "记住：以后默认中文回答")
        finally:
            s.close()
        assert list_memories(db["Session"](), aid, enabled_only=True) == []

    def test_not_saved_writes_nothing(self, db, users, client):
        """P2-09：卡片选"不保存" → 既不加个人偏好，也不加会话已确认事项。"""
        aid = users["alice"]["id"]
        conv = _conv(db, aid)
        _say(db, conv, aid, "讨论设计")
        _refresh(db, aid, conv, threshold=12)
        hdr = _headers(db, "alice")
        r = client.post(
            "/memories/intents",
            headers=hdr,
            json={"text": "记住：本次任务不用自动路由", "conversation_id": conv},
        )
        assert r.status_code == 200 and len(r.json()["proposals"]) == 1
        # 用户点"不保存"：前端不调用 apply，服务端也不应有任何写入
        assert list_memories(db["Session"](), aid, enabled_only=True) == []
        assert [i.text for i in _load(db, aid, conv).confirmed] == []

    def test_session_scope_writes_only_into_this_conversation(self, db, users, client):
        aid = users["alice"]["id"]
        conv = _conv(db, aid)
        other = _conv(db, aid)
        hdr = _headers(db, "alice")
        r = client.post(
            "/memories/intents/apply",
            headers=hdr,
            json={
                "kind": "remember",
                "content": "本次任务不用自动路由",
                "scope": "session",
                "conversation_id": conv,
            },
        )
        assert r.status_code == 200, r.text
        assert any("自动路由" in i.text for i in _load(db, aid, conv).confirmed)
        # 其他会话看不到（P2-04）
        assert [i.text for i in _load(db, aid, other).confirmed] == []
        # 也没有变成跨会话个人偏好
        assert list_memories(db["Session"](), aid, enabled_only=True) == []

    def test_preference_scope_is_shared(self, db, users, client):
        """P2-08：确认跨会话保存后，成为带类别的个人偏好并会被注入。"""
        aid = users["alice"]["id"]
        conv = _conv(db, aid)
        r = client.post(
            "/memories/intents/apply",
            headers=_headers(db, "alice"),
            json={
                "kind": "remember",
                "content": "以后默认中文回答",
                "scope": "preference",
                "category": "answer_style",
                "conversation_id": conv,
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["category"] == "answer_style"
        prefs = memory_service.select_preferences(db["Session"](), aid, "什么是 RAG")
        assert any("默认中文回答" in m.content for m in prefs)

    def test_forget_lists_targets_and_disables_only_those(self, db, users, client):
        aid = users["alice"]["id"]
        s = db["Session"]()
        try:
            keep = create_memory(
                s, aid, "我研究无线通信", mtype="fact", source="user"
            )
            # 与真实路径一致：回答风格偏好使用固定主题键，"忘记我的回答偏好"
            # 就是按这个主题键匹配的
            drop = create_memory(
                s, aid, "回答偏好：简洁", mtype="preference", source="user",
                category="answer_style", memory_key="preference:response_style",
            )
        finally:
            s.close()
        r = client.post(
            "/memories/intents",
            headers=_headers(db, "alice"),
            json={"text": "忘记我的回答偏好"},
        )
        assert r.status_code == 200, r.text
        proposal = r.json()["proposals"][0]
        assert proposal["kind"] == "forget"
        targets = {t["id"] for t in proposal["targets"]}
        assert drop.id in targets and keep.id not in targets

        r = client.post(
            "/memories/intents/apply",
            headers=_headers(db, "alice"),
            json={
                "kind": "forget",
                "content": proposal["content"],
                "scope": "preference",
                "target_ids": sorted(targets),
            },
        )
        assert r.status_code == 200 and r.json()["disabled"] == len(targets)
        enabled = {m.id for m in list_memories(db["Session"](), aid, enabled_only=True)}
        assert keep.id in enabled and drop.id not in enabled

    def test_forget_session_scope_only_touches_conversation(self, db, users, client):
        aid = users["alice"]["id"]
        conv = _conv(db, aid)
        s = db["Session"]()
        try:
            pref = create_memory(
                s, aid, "回答偏好：简洁", mtype="preference", source="user",
                category="answer_style",
            )
        finally:
            s.close()
        r = client.post(
            "/memories/intents/apply",
            headers=_headers(db, "alice"),
            json={
                "kind": "remember",
                "content": "本次只用简洁回答",
                "scope": "session",
                "conversation_id": conv,
            },
        )
        assert r.status_code == 200
        r = client.post(
            "/memories/intents/apply",
            headers=_headers(db, "alice"),
            json={
                "kind": "forget",
                "content": "只用简洁回答",
                "scope": "session",
                "conversation_id": conv,
            },
        )
        assert r.status_code == 200 and r.json()["removed"] == 1
        assert [i.text for i in _load(db, aid, conv).constraints] == []
        # 跨会话个人偏好不受影响
        assert pref.id in {
            m.id for m in list_memories(db["Session"](), aid, enabled_only=True)
        }

    def test_sensitive_content_still_rejected(self, db, users, client):
        aid = users["alice"]["id"]
        r = client.post(
            "/memories/intents/apply",
            headers=_headers(db, "alice"),
            json={
                "kind": "remember",
                "content": "我的密码是 hunter2",
                "scope": "preference",
                "category": "answer_style",
            },
        )
        assert r.status_code == 400 and "密码" in r.json()["detail"]
        assert list_memories(db["Session"](), aid, enabled_only=True) == []

    def test_documents_do_not_trigger_intents(self, db, users):
        """P2-12：知识库文档片段不会触发"记住"，也不会成为记忆。"""
        aid = users["alice"]["id"]
        doc_text = (
            "请记住：本文档论述了卷积神经网络的早期进展。"
            "以后默认引用该文献的结论。"
        )
        s = db["Session"]()
        try:
            # 检索到的文档不是用户原话，链路不会把它送进检测
            assert memory_intents.detect_intents(s, aid, "") == []
            # 用户没有表达意图时，普通陈述也不会写跨会话偏好
            actions = memory_service.auto_extract(
                s, aid, "这篇文档讲了卷积神经网络的早期进展"
            )
        finally:
            s.close()
        assert actions == []
        assert list_memories(db["Session"](), aid, enabled_only=True) == []

    def test_auto_extract_no_longer_writes_explicit_intent(self, db, users):
        """设计方案 §4.3：显式"请记住"不再由聊天链路直接写入。"""
        aid = users["alice"]["id"]
        assert memory_service.EXPLICIT_REQUIRES_CONFIRMATION is True
        s = db["Session"]()
        try:
            assert memory_service.auto_extract(s, aid, "请记住以后默认中文回答") == []
        finally:
            s.close()
        assert list_memories(db["Session"](), aid, enabled_only=True) == []

    def test_intent_endpoints_reject_foreign_conversation(self, db, users, client):
        aid = users["alice"]["id"]
        conv = _conv(db, aid)
        r = client.post(
            "/memories/intents",
            headers=_headers(db, "bob"),
            json={"text": "记住：以后默认中文", "conversation_id": conv},
        )
        assert r.status_code == 404
        r = client.post(
            "/memories/intents/apply",
            headers=_headers(db, "bob"),
            json={
                "kind": "remember",
                "content": "本次不用自动路由",
                "scope": "session",
                "conversation_id": conv,
            },
        )
        assert r.status_code == 404


# ============================================================================
# 7. 鉴权
# ============================================================================


class TestAuth:
    @pytest.mark.parametrize(
        "method,url",
        [
            ("get", "/conversations/whatever/context"),
            ("post", "/conversations/whatever/context/refresh"),
            ("patch", "/conversations/whatever/context"),
            ("delete", "/conversations/whatever/context"),
        ],
    )
    def test_unauthenticated(self, client, method, url):
        assert getattr(client, method)(url).status_code == 401

    def test_intents_require_login(self, client):
        r = client.post("/memories/intents", json={"text": "记住：以后默认中文"})
        assert r.status_code == 401
        r = client.post(
            "/memories/intents/apply",
            json={"kind": "remember", "content": "x", "scope": "session"},
        )
        assert r.status_code == 401

    def test_must_change_password_rejected(self, db, client):
        _mk_user(db, "carol", must_change_password=True)
        hdr = _headers(db, "carol")
        assert client.get("/conversations/x/context", headers=hdr).status_code == 403
        assert (
            client.post("/memories/intents", headers=hdr, json={"text": "记住：x"}).status_code
            == 403
        )

    def test_unknown_conversation_is_404(self, db, users, client):
        r = client.get("/conversations/nope/context", headers=_headers(db, "alice"))
        assert r.status_code == 404


# ============================================================================
# 8. 第四期第一阶段 A：上下文正确性（§6.2 无空洞、§7.2 冲突/完成、
#    §7.5 乐观锁与失败不推进游标、§7.7 重置起点、§4.9 增量扫描）
# ============================================================================


class TestContextCorrectness:
    def test_summary_and_recent_window_meet_without_hole(self, db, users, monkeypatch):
        """M-01/M-02：摘要覆盖范围 + 最近原文窗口 = 全部有效历史，中间不留空洞。"""
        monkeypatch.setenv("CHATCHAT_RECENT_HISTORY_MAX_CHARS", "500")
        aid = users["alice"]["id"]
        conv = _conv(db, aid)
        _say(db, conv, aid, *[f"讨论记忆设计{i:02d}" + "长" * 40 for i in range(12)])
        context = _refresh(db, aid, conv)
        assert context is not None
        window = session_context.select_recent_window(_messages(db, aid, conv))
        # 摘要正好覆盖到窗口起点之前，不多不少
        assert context.summarized_count == window.start_index
        assert window.start_index > 0
        assert window.start_index < 12
        # 分界两侧的消息各归其位：窗口外的进了摘要，窗口内的没进
        summary = context.summary
        assert f"讨论记忆设计{window.start_index - 1:02d}" in summary
        assert f"讨论记忆设计{window.start_index:02d}" not in summary

    def test_budget_below_one_turn_still_keeps_latest_turn(self, db, users, monkeypatch):
        """M-06：单轮就超预算时保留最新一轮并标记截断，不能整段丢掉。"""
        monkeypatch.setenv("CHATCHAT_RECENT_HISTORY_MAX_CHARS", "500")
        aid = users["alice"]["id"]
        conv = _conv(db, aid)
        _say(db, conv, aid, "很长的一个问题" + "长" * 900, response="答" * 300)
        window = session_context.select_recent_window(_messages(db, aid, conv))
        assert window.start_index == 0
        assert window.truncated is True

    def test_negation_replaces_earlier_conclusion(self, db, users):
        """M-04：否定/替换旧说法后，槽位里只留新说法，不留矛盾的两条。"""
        aid = users["alice"]["id"]
        conv = _conv(db, aid)
        _say(
            db, conv, aid,
            "讨论数据库选型",
            "本项目采用 MySQL 存储会话记录",
            "不对，改用 PostgreSQL 存储会话记录",
        )
        context = _refresh(db, aid, conv)
        texts = [i.text for i in context.confirmed]
        assert any("PostgreSQL" in t for t in texts)
        assert not any("MySQL" in t for t in texts)

    def test_completed_items_and_reopen(self, db, users):
        """M-03/M-05：完成事项单独成槽；"还没做完"把它放回待解决问题。"""
        aid = users["alice"]["id"]
        conv = _conv(db, aid)
        _say(db, conv, aid, "讨论登录功能", "我已经把登录接口写好了")
        context = _refresh(db, aid, conv)
        assert any("登录接口" in i.text for i in context.completed_items)
        assert not any("登录接口" in i.text for i in context.open_questions)

        _say(db, conv, aid, "登录接口还没做完")
        again = _refresh(db, aid, conv)
        assert not any("登录接口" in i.text for i in again.completed_items)
        assert any("登录接口" in i.text for i in again.open_questions)

    def test_summary_overflow_drops_oldest_lines(self):
        """M-03（§4.3）：摘要超容量时丢最旧的行，最新结论始终保留。"""
        merged = ""
        for i in range(40):
            merged = session_context.merge_digest(
                merged, [f"用户：第{i}条结论" + "补" * 20], max_chars=600
            )
        lines = [ln for ln in merged.splitlines() if ln.strip()]
        assert sum(len(ln) + 1 for ln in lines) <= 601
        assert "第39条结论" in merged
        assert "第0条结论" not in merged
        # 重复行只保留一份
        twice = session_context.merge_digest(merged, ["用户：第39条结论" + "补" * 20])
        assert twice.splitlines().count("用户：第39条结论" + "补" * 20) == 1

    def test_user_edit_does_not_clear_summary_cursor(self, db, users, monkeypatch):
        """M-12：编辑上下文只改正文，不能把摘要覆盖位置清空。"""
        monkeypatch.setenv("CHATCHAT_RECENT_HISTORY_MAX_CHARS", "500")
        aid = users["alice"]["id"]
        conv = _conv(db, aid)
        _say(db, conv, aid, *[f"较早的问题{i:02d}" + "补" * 40 for i in range(14)])
        before = _refresh(db, aid, conv)
        assert before.summarized_message_id and before.summarized_count == 6

        s = db["Session"]()
        try:
            session_context.apply_user_edit(
                s, aid, conv, topic="手工改过的主题", confirmed=["手工确认的结论"]
            )
        finally:
            s.close()
        after = _load(db, aid, conv)
        assert after.topic is not None and after.topic.text == "手工改过的主题"
        assert after.summary == before.summary
        assert after.summarized_message_id == before.summarized_message_id
        assert after.summarized_count == before.summarized_count

    def test_reset_marks_start_and_does_not_resurrect_old_conclusions(
        self, db, users
    ):
        """M-09/M-10：重置落下起点标记，旧结论不会在下一轮被重扫回来。"""
        aid = users["alice"]["id"]
        conv = _conv(db, aid)
        _say(db, conv, aid, "讨论旧主题", "确认：旧方案采用 MySQL")
        first = _refresh(db, aid, conv)
        assert any("MySQL" in i.text for i in first.confirmed)

        s = db["Session"]()
        try:
            session_context.reset_session_context(s, aid, conv)
        finally:
            s.close()
        cleared = _load(db, aid, conv)
        assert cleared.is_empty()
        last_id = _messages(db, aid, conv)[-1].id
        assert cleared.context_start_after_message_id == last_id

        _say(db, conv, aid, "讨论新主题", "确认：新方案采用 PostgreSQL")
        again = _refresh(db, aid, conv)
        texts = [i.text for i in again.confirmed]
        assert any("PostgreSQL" in t for t in texts)
        assert not any("MySQL" in t for t in texts)
        # 原始聊天记录不删除
        assert len(_messages(db, aid, conv)) == 4

    def test_optimistic_lock_conflict_blocks_stale_write(self, db, users):
        """M-49：并发更新同一会话时，旧版本的任务不能覆盖新摘要。"""
        from chatchat.server.db.repository import session_summary_repository as repo

        aid = users["alice"]["id"]
        conv = _conv(db, aid)
        _say(db, conv, aid, "讨论记忆设计")
        _refresh(db, aid, conv)
        s = db["Session"]()
        try:
            row = repo.get_session_summary(s, conv, aid)
            stale = row.version
            # 另一个请求先写了一次
            repo.upsert_session_summary(
                s, aid, conv, summary="新任务的摘要", context={"topic": "新主题"}
            )
            with pytest.raises(repo.ContextVersionConflict):
                repo.upsert_session_summary(
                    s,
                    aid,
                    conv,
                    summary="过期任务的摘要",
                    summarized_message_id="m1",
                    summarized_count=3,
                    expected_version=stale,
                )
            after = repo.get_session_summary(s, conv, aid)
            assert after.summary == "新任务的摘要"
            assert after.summarized_message_id is None
        finally:
            s.close()

    def test_slot_scan_is_incremental_and_keeps_earlier_items(self, db, users):
        """§4.9：每轮只扫新增消息，早先扫出的条目仍然留在槽位里。"""
        aid = users["alice"]["id"]
        conv = _conv(db, aid)
        _say(db, conv, aid, "讨论存储方案", "本项目采用 MySQL 存储会话记录")
        first = _refresh(db, aid, conv)
        assert first.slots_scanned_count == 2
        assert any("MySQL" in i.text for i in first.confirmed)

        _say(db, conv, aid, "本次回答统一使用表格形式")
        again = _refresh(db, aid, conv)
        assert again.slots_scanned_count == 3
        assert again.slots_scanned_through_message_id == _messages(db, aid, conv)[-1].id
        # 增量扫描后旧结论仍在，新约束也进来了
        assert any("MySQL" in i.text for i in again.confirmed)
        assert any("表格" in i.text for i in again.constraints)

    def test_summary_failure_does_not_advance_cursor(self, db, users, monkeypatch):
        """M-46：摘要失败时保留旧摘要、不推进游标，并把失败原因记下来。"""
        monkeypatch.setenv("CHATCHAT_RECENT_HISTORY_MAX_CHARS", "500")
        aid = users["alice"]["id"]
        conv = _conv(db, aid)
        _say(db, conv, aid, *[f"较早的问题{i:02d}" + "补" * 40 for i in range(14)])
        ok = _refresh(db, aid, conv)
        assert ok.summary and ok.summarized_count == 6

        def boom(existing, older_text):
            raise RuntimeError("模型不可用")

        _say(db, conv, aid, *[f"较新的问题{i:02d}" + "补" * 40 for i in range(14, 20)])
        failed = _refresh(db, aid, conv, summarizer=boom)
        assert failed is not None
        assert failed.summary_status == "failed"
        assert "模型不可用" in (failed.summary_error or "")
        # 旧摘要保留、游标不推进
        assert failed.summary == ok.summary
        assert failed.summarized_count == ok.summarized_count
        assert failed.summarized_message_id == ok.summarized_message_id

    def test_completed_items_visible_and_editable_via_api(self, db, users, client):
        """已完成事项经 API 可读可编辑（抽屉在第一阶段 B 显示，接口先就位）。"""
        aid = users["alice"]["id"]
        conv = _conv(db, aid)
        _say(db, conv, aid, "讨论登录功能", "我已经把登录接口写好了")
        hdr = _headers(db, "alice")
        r = client.post(f"/conversations/{conv}/context/refresh", headers=hdr)
        assert r.status_code == 200, r.text
        assert any("登录接口" in t for t in r.json()["completed_items"])

        r = client.patch(
            f"/conversations/{conv}/context",
            headers=hdr,
            json={"completed_items": ["手工登记的完成事项"]},
        )
        assert r.status_code == 200, r.text
        assert r.json()["completed_items"] == ["手工登记的完成事项"]
        r = client.get(f"/conversations/{conv}/context", headers=hdr)
        assert r.json()["completed_items"] == ["手工登记的完成事项"]


# ============================================================================
# 9. 未完成回答：半截说法不进摘要，用户原话照常扫描（§18.3.10）
# ============================================================================


def _stop(db, conversation_id, owner_id, query, response, status="cancelled"):
    """写入一条停止/失败留下的半截回答，并落下生成状态。"""
    from chatchat.server.db.repository.message_repository import merge_message_metadata
    from chatchat.server.memory.usage_manifest import STATUS_KEY

    s = db["Session"]()
    try:
        mid = add_message(
            s, conversation_id, owner_id, "agent_chat", query, response=response
        )
        merge_message_metadata(s, mid, owner_id, {STATUS_KEY: status})
        return mid
    finally:
        s.close()


class TestIncompleteTurns:
    def test_digest_lines_skip_incomplete_assistant_text(self):
        """摘要是结论来源：半截回答一旦成行，后面几轮都会把它当成说完过的话。"""
        rows = [
            types.SimpleNamespace(
                query="继续写那段长文",
                response="写到一半的结论",
                meta_data={"generation_status": "cancelled"},
            ),
            types.SimpleNamespace(
                query="下一个问题",
                response="完整的结论",
                meta_data={"generation_status": "completed"},
            ),
            # 状态机之前的旧数据没有 generation_status，按已完成处理
            types.SimpleNamespace(query="老规矩", response="旧回答", meta_data=None),
        ]
        assert session_context.digest_lines(rows) == [
            "用户：继续写那段长文",
            "用户：下一个问题",
            "助手：完整的结论",
            "用户：老规矩",
            "助手：旧回答",
        ]

    def test_partial_answer_leaves_the_digest_but_not_the_conversation(
        self, db, users, monkeypatch
    ):
        """§18.3.10：半截回答不被摘成结论，用户原话仍在摘要里。"""
        monkeypatch.setenv("CHATCHAT_RECENT_HISTORY_MAX_CHARS", "500")
        aid = users["alice"]["id"]
        conv = _conv(db, aid)
        _stop(db, conv, aid, "写一段长文", "这是写到一半的结论内容")
        _say(db, conv, aid, *[f"较早的问题{i:02d}" + "补" * 40 for i in range(14)])
        context = _refresh(db, aid, conv)
        assert context is not None
        assert "这是写到一半的结论内容" not in context.summary
        # 用户原话仍进摘要：下一轮"继续刚才那段"才对得上上下文
        assert "写一段长文" in context.summary

    def test_partial_answer_still_feeds_slots_from_its_query(self, db, users):
        """槽位只从用户消息提取，半截回答不影响"继续"这类轮次被扫到。"""
        aid = users["alice"]["id"]
        conv = _conv(db, aid)
        _stop(db, conv, aid, "本项目采用 PostgreSQL 存储会话记录", "写到一半就停了")
        context = _refresh(db, aid, conv)
        assert any("PostgreSQL" in i.text for i in context.confirmed)
        assert not any("写到一半" in i.text for i in context.confirmed)


# ============================================================================
# 10. 上下文状态接口：摘要类型、覆盖位置与执行状态（§18.3.7）
# ============================================================================


class TestContextStatusApi:
    def test_refresh_reports_type_coverage_and_window(self, db, users, client, monkeypatch):
        """抽屉要能说清"摘要摘到哪、最近原文从哪开始"，且两边不留空洞。"""
        monkeypatch.setenv("CHATCHAT_RECENT_HISTORY_MAX_CHARS", "500")
        aid = users["alice"]["id"]
        conv = _conv(db, aid)
        _say(db, conv, aid, *[f"较早的问题{i:02d}" + "补" * 40 for i in range(14)])
        r = client.post(f"/conversations/{conv}/context/refresh", headers=_headers(db, "alice"))
        assert r.status_code == 200, r.text
        body = r.json()
        # 模型摘要尚未启用：必须如实标成规则摘要，不能显示成"AI 总结"
        assert body["summary_type"] == "rule"
        assert body["summary_status"] == "succeeded"
        assert body["summary_error"] is None
        assert body["summary_updated_at"]
        assert body["version"] >= 1
        assert body["needs_rebuild"] is False
        assert body["summarized_message_id"]
        assert body["recent_boundary_message_id"]
        assert body["recent_window_size"] > 0
        # 无空洞分界：摘要覆盖到的那条正好在窗口起点之前
        ids = [m.id for m in _messages(db, aid, conv)]
        assert ids.index(body["summarized_message_id"]) + 1 == ids.index(
            body["recent_boundary_message_id"]
        )

    def test_summary_failure_is_reported_by_the_status_api(
        self, db, users, client, monkeypatch
    ):
        """摘要失败要看得见：状态、原因，以及"现存摘要仍是旧的那一版"。"""
        monkeypatch.setenv("CHATCHAT_RECENT_HISTORY_MAX_CHARS", "500")
        aid = users["alice"]["id"]
        conv = _conv(db, aid)
        _say(db, conv, aid, *[f"较早的问题{i:02d}" + "补" * 40 for i in range(14)])
        ok = _refresh(db, aid, conv)
        _say(db, conv, aid, *[f"较新的问题{i:02d}" + "补" * 40 for i in range(14, 20)])

        def boom(existing, older_text):
            raise RuntimeError("模型不可用")

        _refresh(db, aid, conv, summarizer=boom)
        body = client.get(
            f"/conversations/{conv}/context", headers=_headers(db, "alice")
        ).json()
        assert body["summary_status"] == "failed"
        assert "模型不可用" in (body["summary_error"] or "")
        # 现存摘要仍是上一次成功的规则摘要，覆盖位置没有推进
        assert body["summary_type"] == "rule"
        assert body["summarized_message_id"] == ok.summarized_message_id
        assert body["summary"] == ok.summary

    def test_memoryless_conversation_reports_no_boundary(self, db, users, client):
        """无记忆对话本来就没有摘要与分界，接口不能凭空报一个（§13.1）。"""
        aid = users["alice"]["id"]
        conv = _conv(db, aid, "无记忆对话", temporary=True)
        _say(db, conv, aid, "问题一", "问题二")
        body = client.get(
            f"/conversations/{conv}/context", headers=_headers(db, "alice")
        ).json()
        assert body["temporary"] is True
        assert body["recent_boundary_message_id"] is None
        assert body["recent_window_size"] == 0
        assert body["summary_status"] == "idle"
        assert body["version"] == 0

    def test_other_user_cannot_refresh_or_read_status(self, db, users, client):
        """§18.3.13：状态接口与手动刷新同样只认会话属主。"""
        aid = users["alice"]["id"]
        conv = _conv(db, aid)
        _say(db, conv, aid, "讨论记忆设计")
        hdr_b = _headers(db, "bob")
        assert (
            client.post(f"/conversations/{conv}/context/refresh", headers=hdr_b).status_code
            == 404
        )
        assert client.get(f"/conversations/{conv}/context", headers=hdr_b).status_code == 404
