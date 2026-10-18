"""智能聊天记忆第三期（跨会话相关记忆召回）离线测试。

全部使用临时 SQLite 数据库，不连接模型服务、外部网络或真实用户数据库。

覆盖第三期设计方案 §3/§4/§5/§6 的可自动化部分：

- v15 迁移：只加列建新表、旧记忆行内容不动、``confirmed`` 按来源回填、
  重复执行无改动；
- 召回开关：总开关 + 4 个分项开关的默认值与生效范围（P3-11、P3-12）；
- 召回判定：只召回相关的、已确认的、未过期未被替代的记忆（P3-02、P3-05、
  P3-06、P3-07），临时对话内容与其他用户的记忆一律不召回（P3-14）；
- 项目范围记忆只在选定该项目的会话里召回（P3-09、P3-10）；
- "本次不使用"只影响当前会话，原记录保留（P3-04）；
- 从会话已确认事项提升为跨会话记忆：范围/类别/有效期由用户选定，同槽位旧
  决策被标记为已被替代（P3-07），临时对话与越权项目一律拒绝（P3-13 的
  "不确认就不保存"由界面保证，这里保证服务端也不会自动写）；
- 编辑保留修订历史、删除连带清理；
- 新端点的未登录 / 禁用用户 / 未改密 / 跨用户与跨项目隔离。
"""
import asyncio
import datetime as dt
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
from chatchat.server.db.migrate.base import apply_migrations, current_version
from chatchat.server.db.migrate.migrations import build_registry
from chatchat.server.db.models.conversation_model import ConversationModel  # noqa: F401
from chatchat.server.db.models.knowledge_base_model import KnowledgeBaseModel  # noqa: F401
from chatchat.server.db.models.knowledge_file_model import KnowledgeFileModel  # noqa: F401
from chatchat.server.db.models.message_model import MessageModel  # noqa: F401
from chatchat.server.db.models.user_model import UserModel  # noqa: F401
from chatchat.server.db.repository import (
    MemoryNotOwned,
    create_conversation,
    create_memory,
    delete_memory,
    disable_memory,
    get_owned_memory,
    list_memories,
    list_revisions,
    update_memory,
)
from chatchat.server.db.repository import memory_repository as memory_repo
from chatchat.server.db.repository import research_repository as research_repo
from chatchat.server.db.session import get_db as db_get_db
from chatchat.server.memory import cross_session_recall, memory_service, session_context


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setattr(
        auth_jwt, "get_secret", lambda: "unit-test-secret-key-0000000000000000"
    )
    return "unit-test-secret-key-0000000000000000"


@pytest.fixture
def db(secret, tmp_path):
    path = tmp_path / "recall_test.db"
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
    a.include_router(session_context_router)
    a.include_router(conversation_router)

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
    """同 test_memory_preferences：本环境 anyio 线程桥连会挂起，直接同步执行端点。"""

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


# --- 小工具：每个用例自己开短会话，避免跨用例共享 Session ---

def _add(db, owner_id, content, *, source="user", **kwargs):
    """写一条记忆；``source`` 默认 user（用户自己写的 → 已确认）。"""
    s = db["Session"]()
    try:
        return create_memory(s, owner_id, content, source=source, **kwargs)
    finally:
        s.close()


def _conv(db, owner_id, name="会话", **kwargs):
    s = db["Session"]()
    try:
        return create_conversation(s, owner_id, "agent_chat", name, **kwargs)
    finally:
        s.close()


def _project(db, owner_id, name="智能聊天记忆"):
    s = db["Session"]()
    try:
        return research_repo.create_project(s, owner_id, name, "如何分层记忆")
    finally:
        s.close()


def _recall(db, owner_id, query, conversation_id=None, project_id=None):
    s = db["Session"]()
    try:
        return cross_session_recall.recall_related_memories(
            s, owner_id, query, conversation_id, project_id
        )
    finally:
        s.close()


def _contents(recalled):
    return [r.memory.content for r in recalled]


# ============================================================================
# 1. v15 迁移：加列建新表、按来源回填 confirmed、幂等
# ============================================================================


class TestV15Migration:
    def _legacy_db(self, tmp_path):
        """造一个"停在 v14"的库：先升到 v15，再手工删掉 v15 的产物并塞旧数据。

        ``conversation`` 这类业务表由应用启动时的 ``create_all`` 创建、
        版本迁移只负责加列，所以迁移与建表都要跑，再回退成 v14 的形状，
        贴近真实升级路径。
        """
        path = tmp_path / "v15_legacy.db"
        engine = create_engine(f"sqlite:///{path}")
        with engine.connect() as conn:
            apply_migrations(build_registry(), conn)
        Base.metadata.create_all(engine)
        # 升级前已存在的用户，升级后要拿到第三期默认开关
        Session = sessionmaker(bind=engine)
        s = Session()
        try:
            old_user = service.create_user(
                s, username="old", display_name="Old", password="password12345678"
            )
            s.commit()
            old_user_id = old_user.id
        finally:
            s.close()
        with engine.connect() as conn:
            conn.execute(text("DELETE FROM schema_migrations WHERE version IN (15, 16, 17, 18, 19, 20)"))
            conn.execute(text("DROP TABLE IF EXISTS memory_revision"))
            conn.execute(text("DROP TABLE IF EXISTS conversation_memory_ignore"))
            conn.execute(text("ALTER TABLE conversation DROP COLUMN project_id"))
            for column in (
                "memory_recall_enabled",
                "memory_recall_decisions_enabled",
                "memory_recall_project_enabled",
                "memory_recall_general_enabled",
            ):
                conn.execute(text(f"ALTER TABLE user_account DROP COLUMN {column}"))
            # memory 重建为 v13 的列结构（缺 v15 的 8 列）
            conn.execute(
                text(
                    "CREATE TABLE memory_old ("
                    "id VARCHAR(32) PRIMARY KEY, owner_id VARCHAR(32) NOT NULL, "
                    "memory_key VARCHAR(128) NOT NULL, type VARCHAR(16) NOT NULL, "
                    "category VARCHAR(32), content VARCHAR(2000) NOT NULL, "
                    "source VARCHAR(16) NOT NULL, source_conversation_id VARCHAR(32), "
                    "source_message_id VARCHAR(32), importance INTEGER NOT NULL, "
                    "enabled BOOLEAN NOT NULL, create_time TIMESTAMP, update_time TIMESTAMP)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO memory_old SELECT id, owner_id, memory_key, type, "
                    "category, content, source, source_conversation_id, "
                    "source_message_id, importance, enabled, create_time, update_time "
                    "FROM memory"
                )
            )
            conn.execute(text("DROP TABLE memory"))
            conn.execute(text("ALTER TABLE memory_old RENAME TO memory"))
            conn.execute(
                text(
                    "INSERT INTO memory (id, owner_id, memory_key, type, content, "
                    "source, importance, enabled, create_time, update_time) VALUES "
                    "('m-auto','u1','k1','fact','自动提取的研究方向','auto_extract',3,1,"
                    "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP),"
                    "('m-user','u1','k2','fact','我确认过：第三期做跨会话召回','user',3,1,"
                    "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP),"
                    "('m-manual','u1','k3','fact','手动保存的项目规则','manual',3,1,"
                    "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
                )
            )
            conn.commit()
            assert current_version(conn) == 14
        return engine, old_user_id

    def test_adds_columns_tables_and_backfills_confirmed(self, secret, tmp_path):
        engine, old_user_id = self._legacy_db(tmp_path)
        with engine.connect() as conn:
            result = apply_migrations(build_registry(), conn)
            # 造库时把 v15 与 v16 的记录都退掉了，重新升级要跑两支迁移
            assert [m.version for m in result.applied] == [15, 16, 17, 18, 19, 20]
            assert result.final_version == 20

            columns = {c["name"] for c in inspect(conn).get_columns("memory")}
            assert {
                "kind",
                "scope",
                "project_id",
                "project_name",
                "confirmed",
                "expires_at",
                "superseded_by",
                "supersedes",
            } <= columns
            assert inspect(conn).has_table("memory_revision")
            assert inspect(conn).has_table("conversation_memory_ignore")
            assert "project_id" in {
                c["name"] for c in inspect(conn).get_columns("conversation")
            }
            user_columns = {c["name"] for c in inspect(conn).get_columns("user_account")}
            assert {
                "memory_recall_enabled",
                "memory_recall_decisions_enabled",
                "memory_recall_project_enabled",
                "memory_recall_general_enabled",
            } <= user_columns
            # 升级前已存在的用户按 §4.1 的默认值补齐：只开总开关与已确认决策
            assert conn.execute(
                text(
                    "SELECT memory_recall_enabled, memory_recall_decisions_enabled, "
                    "memory_recall_project_enabled, memory_recall_general_enabled "
                    "FROM user_account WHERE id = :u"
                ),
                {"u": old_user_id},
            ).one() == (1, 1, 0, 0)

            # 旧行内容一字未改；confirmed 按来源回填
            rows = {
                r[0]: (r[1], r[2])
                for r in conn.execute(
                    text("SELECT id, content, confirmed FROM memory WHERE owner_id='u1'")
                )
            }
            assert rows["m-auto"][0] == "自动提取的研究方向"
            assert rows["m-auto"][1] == 0  # 自动提取不算已确认
            assert rows["m-user"][1] == 1
            assert rows["m-manual"][1] == 1
            # 旧行的新列取默认值：一般记忆、个人范围
            kind, scope = conn.execute(
                text("SELECT kind, scope FROM memory WHERE id='m-user'")
            ).one()
            assert kind == "fact" and scope == "personal"
        engine.dispose()

    def test_reapply_is_noop(self, secret, db):
        with db["engine"].connect() as conn:
            second = apply_migrations(build_registry(), conn)
            assert second.applied == []
            assert second.final_version == 20

    def test_new_user_switch_defaults(self, db):
        """默认只开"个人偏好 + 已确认决策"（设计方案 §4.1）。"""
        s = db["Session"]()
        try:
            switches = cross_session_recall.get_switches(s, _mk_user(db, "carol")["id"])
        finally:
            s.close()
        assert switches == cross_session_recall.RecallSwitches(
            enabled=True,
            decisions=True,
            project=False,
            general=False,
            preferences=True,
        )


# ============================================================================
# 2. 开关：总开关与分项开关
# ============================================================================


class TestSwitches:
    def test_master_switch_off_keeps_preferences(self, db, users):
        """P3-11：关闭跨会话召回后不再召回历史决策，个人偏好仍按第一期开关工作。"""
        owner = users["alice"]["id"]
        _add(db, owner, "默认中文回答", category="answer_style")
        _add(db, owner, "智能聊天记忆三期规划：第三期做跨会话召回")
        s = db["Session"]()
        try:
            cross_session_recall.set_switches(s, owner, enabled=False)
            msgs = memory_service.memory_system_messages(
                s, owner, "第三期是否要做向量检索"
            )
        finally:
            s.close()
        joined = "\n".join(m["content"] for m in msgs)
        assert "第三期做跨会话召回" not in joined
        assert "默认中文回答" in joined  # 个人偏好仍注入
        assert memory_service.PREFERENCE_SECTION_HEADER in joined

    def test_general_switch_off_excludes_unconfirmed(self, db, users):
        """P3-12：只使用已确认内容，不使用自动提取的一般历史结论。"""
        owner = users["alice"]["id"]
        _add(db, owner, "已确认：第三期做跨会话召回", source="user")
        _add(db, owner, "自动提取：第三期可能要做向量检索", source="auto_extract")
        recalled = _recall(db, owner, "第三期做跨会话召回")
        assert "已确认：第三期做跨会话召回" in _contents(recalled)
        assert "自动提取：第三期可能要做向量检索" not in _contents(recalled)

        s = db["Session"]()
        try:
            cross_session_recall.set_switches(s, owner, general=True)
        finally:
            s.close()
        recalled = _recall(db, owner, "第三期做跨会话召回")
        assert "自动提取：第三期可能要做向量检索" in _contents(recalled)

    def test_decisions_switch_off_stops_confirmed_recall(self, db, users):
        owner = users["alice"]["id"]
        _add(db, owner, "已确认：第三期做跨会话召回")
        assert _recall(db, owner, "第三期做跨会话召回")
        s = db["Session"]()
        try:
            cross_session_recall.set_switches(s, owner, decisions=False)
        finally:
            s.close()
        assert _recall(db, owner, "第三期做跨会话召回") == []

    def test_unknown_user_gets_no_switches(self, db):
        switches = cross_session_recall.get_switches(db["Session"](), "ghost")
        assert switches.enabled is False and switches.preferences is False

    def test_set_switches_rejects_unknown_key(self, db, users):
        with pytest.raises(ValueError):
            cross_session_recall.set_switches(
                db["Session"](), users["alice"]["id"], nonsense=True
            )


# ============================================================================
# 3. 召回判定：少而准、可解释
# ============================================================================


class TestRecall:
    def test_related_decision_recalled_in_new_conversation(self, db, users):
        """P3-01 / P3-03：新会话里相关问题能召回已确认决策，并带来源与原因。"""
        owner = users["alice"]["id"]
        source_conv = _conv(db, owner, "智能聊天记忆设计")
        _add(
            db,
            owner,
            "智能聊天记忆三期规划：第一期只做偏好，第二期做会话摘要，第三期做跨会话召回",
            source_conversation_id=source_conv,
        )
        new_conv = _conv(db, owner, "新会话")
        recalled = _recall(db, owner, "第三期是否要加入向量检索", new_conv)
        assert len(recalled) == 1
        item = recalled[0]
        assert "三期规划" in item.memory.content
        assert item.confirmed is True
        assert "智能聊天记忆设计" in item.source_label
        assert item.reason
        assert item.bucket == cross_session_recall.BUCKET_DECISION

    def test_unrelated_question_does_not_recall(self, db, users):
        """P3-02：无关问题不展示、不使用该决策。"""
        owner = users["alice"]["id"]
        _add(db, owner, "智能聊天记忆三期规划：第三期做跨会话召回")
        assert _recall(db, owner, "帮我看看今天的天气如何") == []

    def test_caps_count(self, db, users):
        owner = users["alice"]["id"]
        for i in range(6):
            _add(db, owner, f"三期规划第{i}条：跨会话召回方案")
        recalled = _recall(db, owner, "三期规划跨会话召回")
        assert len(recalled) <= cross_session_recall.MAX_RECALL_COUNT

    def test_disabled_and_deleted_not_recalled(self, db, users):
        """P3-05 / P3-06：停用与删除后都不再召回；停用保留记录，删除彻底移除。"""
        owner = users["alice"]["id"]
        m1 = _add(db, owner, "已确认：第三期做跨会话召回")
        m2 = _add(db, owner, "已确认：第三期不做向量检索")
        s = db["Session"]()
        try:
            disable_memory(s, m1.id, owner)
            delete_memory(s, m2.id, owner)
        finally:
            s.close()
        assert _recall(db, owner, "第三期做跨会话召回") == []
        assert get_owned_memory(db["Session"](), m2.id, owner) is None
        # 停用只是不召回，记录仍在管理页
        assert get_owned_memory(db["Session"](), m1.id, owner) is not None

    def test_expired_not_recalled_but_kept(self, db, users):
        """§4.5：到期后不再自动召回，记录仍保留。"""
        owner = users["alice"]["id"]
        m = _add(
            db,
            owner,
            "临时项目约束：本期不做跨会话召回",
            expires_at=dt.datetime.now() - dt.timedelta(days=1),
        )
        assert _recall(db, owner, "本期不做跨会话召回") == []
        kept = get_owned_memory(db["Session"](), m.id, owner)
        assert kept is not None and kept.enabled
        assert cross_session_recall.memory_status(kept) == cross_session_recall.STATUS_EXPIRED

    def test_future_expiry_still_recalled(self, db, users):
        owner = users["alice"]["id"]
        _add(
            db,
            owner,
            "已确认：本期不做跨会话召回",
            expires_at=dt.datetime.now() + dt.timedelta(days=30),
        )
        assert _recall(db, owner, "本期不做跨会话召回")

    def test_temporary_conversation_content_never_recalled(self, db, users):
        """§3：临时对话内容不参与跨会话召回。"""
        owner = users["alice"]["id"]
        temp_conv = _conv(db, owner, "临时会话", temporary=True)
        _add(
            db,
            owner,
            "临时会话里确认：跨会话召回先不做",
            source_conversation_id=temp_conv,
        )
        assert _recall(db, owner, "跨会话召回先不做", _conv(db, owner, "正式会话")) == []

    def test_other_user_memory_never_recalled(self, db, users):
        """P3-14：任何召回只来自当前用户自己的历史。"""
        alice, bob = users["alice"]["id"], users["bob"]["id"]
        _add(db, alice, "已确认：Alice 的跨会话召回方案")
        _add(db, bob, "已确认：Bob 的跨会话召回方案")
        contents = _contents(_recall(db, bob, "跨会话召回方案"))
        assert contents == ["已确认：Bob 的跨会话召回方案"]

    def test_current_conversation_memory_not_duplicated(self, db, users):
        """来源就是当前会话的记忆不重复注入（它已在会话上下文段里）。"""
        owner = users["alice"]["id"]
        conv = _conv(db, owner, "本会话")
        _add(db, owner, "已确认：跨会话召回按会话范围生效", source_conversation_id=conv)
        assert _recall(db, owner, "跨会话召回按会话范围生效", conv) == []
        assert _recall(db, owner, "跨会话召回按会话范围生效", _conv(db, owner, "另一会话"))

    def test_preference_not_recalled_through_this_channel(self, db, users):
        """个人偏好只经第一期通道注入，不在历史记忆段里重复出现。"""
        owner = users["alice"]["id"]
        _add(db, owner, "跨会话召回要用中文回答", category="answer_style")
        assert _recall(db, owner, "跨会话召回要用中文回答") == []

    def test_segment_marks_current_turn_as_authority(self, db, users):
        """P3-08 / §5：注入段明确写"本轮优先"，且不替代知识库引用。"""
        owner = users["alice"]["id"]
        _add(db, owner, "已确认：不做自动模型路由")
        segment, used = cross_session_recall.build_recall_segment(
            db["Session"](), owner, "不做自动模型路由"
        )
        assert used
        assert cross_session_recall.RECALL_SECTION_HEADER in segment
        assert "以" in segment and "检索到的文档" in segment

    def test_injection_order_preferences_then_recall_then_context(self, db, users):
        """§5 顺序：个人偏好 → 相关历史记忆 → 当前会话上下文。"""
        owner = users["alice"]["id"]
        conv = _conv(db, owner, "会话A")
        _add(db, owner, "默认中文回答", category="answer_style")
        _add(db, owner, "已确认：第三期做跨会话召回", source_conversation_id=_conv(db, owner, "旧会话"))
        s = db["Session"]()
        try:
            session_context.add_session_confirmation(
                s, owner, conv, "本次只做界面，不动数据库"
            )
            msgs = memory_service.memory_system_messages(
                s, owner, "第三期做跨会话召回", conv
            )
        finally:
            s.close()
        joined = [m["content"] for m in msgs]
        assert memory_service.PREFERENCE_SECTION_HEADER in joined[0]
        assert cross_session_recall.RECALL_SECTION_HEADER in joined[1]
        assert session_context.CONTEXT_HEADER in joined[2]

    def test_use_memory_false_skips_recall_segment(self, db, users):
        owner = users["alice"]["id"]
        _add(db, owner, "已确认：第三期做跨会话召回")
        s = db["Session"]()
        try:
            msgs = memory_service.memory_system_messages(
                s, owner, "第三期做跨会话召回", None, use_memory=False
            )
        finally:
            s.close()
        assert msgs == []


# ============================================================================
# 4. 项目范围记忆（§4.4）
# ============================================================================


class TestProjectScope:
    def _seed(self, db, users):
        owner = users["alice"]["id"]
        project_a = _project(db, owner, "项目A")
        project_b = _project(db, owner, "项目B")
        _add(
            db,
            owner,
            "项目A规则：本项目不做自动模型路由",
            scope="project",
            project_id=project_a.id,
            project_name=project_a.name,
            kind="project_rule",
        )
        return owner, project_a, project_b

    def test_not_recalled_without_project(self, db, users):
        """P3-10：普通智能聊天不选项目时，项目级记忆不出现。"""
        owner, _a, _b = self._seed(db, users)
        s = db["Session"]()
        try:
            cross_session_recall.set_switches(s, owner, project=True)
        finally:
            s.close()
        plain_conv = _conv(db, owner, "普通聊天")
        assert _recall(db, owner, "本项目不做自动模型路由", plain_conv) == []

    def test_project_switch_off_blocks_recall_even_in_project(self, db, users):
        owner, project_a, _b = self._seed(db, users)
        conv = _conv(db, owner, "项目A空间", project_id=project_a.id)
        assert _recall(db, owner, "本项目不做自动模型路由", conv) == []

    def test_recalled_only_inside_own_project(self, db, users):
        """P3-09：项目 A 的规则不会出现在项目 B 的会话里。"""
        owner, project_a, project_b = self._seed(db, users)
        s = db["Session"]()
        try:
            cross_session_recall.set_switches(s, owner, project=True)
        finally:
            s.close()
        conv_a = _conv(db, owner, "项目A空间", project_id=project_a.id)
        conv_b = _conv(db, owner, "项目B空间", project_id=project_b.id)
        contents = _contents(_recall(db, owner, "本项目不做自动模型路由", conv_a))
        assert contents == ["项目A规则：本项目不做自动模型路由"]
        assert _recall(db, owner, "本项目不做自动模型路由", conv_b) == []

    def test_mentioned_project_allows_recall(self, db, users):
        """§4.4：普通聊天里明确提及该项目后，该项目记忆才可用。"""
        owner, _a, _b = self._seed(db, users)
        s = db["Session"]()
        try:
            cross_session_recall.set_switches(s, owner, project=True)
        finally:
            s.close()
        plain = _conv(db, owner, "普通聊天")
        contents = _contents(
            _recall(db, owner, "项目A里要不要做自动模型路由", plain)
        )
        assert contents == ["项目A规则：本项目不做自动模型路由"]

    def test_personal_memory_still_recalled_in_project(self, db, users):
        owner, project_a, _b = self._seed(db, users)
        _add(db, owner, "已确认：跨会话召回要显示来源")
        conv = _conv(db, owner, "项目A空间", project_id=project_a.id)
        assert _recall(db, owner, "跨会话召回要显示来源", conv)


# ============================================================================
# 5. 冲突与替代链（§4.5）
# ============================================================================


class TestSupersede:
    def test_new_decision_supersedes_old_one(self, db, users):
        """P3-07：同槽位的新决策生效，旧决策标记为已被替代且只注入新的。"""
        owner = users["alice"]["id"]
        old = cross_session_recall.promote_to_long_term(
            db["Session"](), owner, content="已确认：不做自动模型路由", kind="decision"
        )
        new = cross_session_recall.promote_to_long_term(
            db["Session"](),
            owner,
            content="已确认：下一版本恢复自动模型路由",
            kind="decision",
        )
        s = db["Session"]()
        try:
            old_row = get_owned_memory(s, old.id, owner)
            new_row = get_owned_memory(s, new.id, owner)
        finally:
            s.close()
        assert old_row.superseded_by == new.id
        assert new_row.supersedes == old.id
        assert cross_session_recall.memory_status(old_row) == "superseded"

        contents = _contents(_recall(db, owner, "自动模型路由要不要恢复"))
        assert contents == ["已确认：下一版本恢复自动模型路由"]

    def test_revision_chain_keeps_old_content(self, db, users):
        """§4.5：查看历史时保留完整修订链。"""
        owner = users["alice"]["id"]
        old = cross_session_recall.promote_to_long_term(
            db["Session"](), owner, content="已确认：不做自动模型路由"
        )
        cross_session_recall.promote_to_long_term(
            db["Session"](), owner, content="已确认：下一版本恢复自动模型路由"
        )
        revisions = list_revisions(db["Session"](), old.id, owner)
        assert [r.content for r in revisions] == ["已确认：不做自动模型路由"]
        assert revisions[0].reason == "supersede"

    def test_edit_keeps_revision_history(self, db, users):
        """§4.2：编辑修正内容并保留修订历史。"""
        owner = users["alice"]["id"]
        m = _add(db, owner, "已确认：第三期做跨会话召回")
        s = db["Session"]()
        try:
            update_memory(s, m.id, owner, content="已确认：第三期做跨会话召回（含有效期）")
        finally:
            s.close()
        revisions = list_revisions(db["Session"](), m.id, owner)
        assert [r.content for r in revisions] == ["已确认：第三期做跨会话召回"]
        # 修订记录只属于该用户
        assert list_revisions(db["Session"](), m.id, users["bob"]["id"]) == []

    def test_delete_removes_revisions_and_ignores(self, db, users):
        owner = users["alice"]["id"]
        m = _add(db, owner, "已确认：第三期做跨会话召回")
        conv = _conv(db, owner, "会话A")
        s = db["Session"]()
        try:
            update_memory(s, m.id, owner, content="已确认：第三期做跨会话召回v2")
            memory_repo.ignore_memory(s, conv, owner, m.id)
            assert list_revisions(s, m.id, owner)
            delete_memory(s, m.id, owner)
            assert list_revisions(s, m.id, owner) == []
            assert memory_repo.list_ignored_memory_ids(s, conv, owner) == set()
        finally:
            s.close()


# ============================================================================
# 6. "本次不使用"（§4.2 / P3-04）
# ============================================================================


class TestConversationIgnore:
    def test_ignore_only_affects_that_conversation(self, db, users):
        owner = users["alice"]["id"]
        m = _add(db, owner, "已确认：第三期做跨会话召回")
        conv_a = _conv(db, owner, "会话A")
        conv_b = _conv(db, owner, "会话B")
        s = db["Session"]()
        try:
            memory_repo.ignore_memory(s, conv_a, owner, m.id)
        finally:
            s.close()
        assert _recall(db, owner, "第三期做跨会话召回", conv_a) == []
        assert _contents(_recall(db, owner, "第三期做跨会话召回", conv_b)) == [
            "已确认：第三期做跨会话召回"
        ]
        # 原记录仍在管理页
        assert get_owned_memory(db["Session"](), m.id, owner) is not None

    def test_unignore_restores_recall(self, db, users):
        owner = users["alice"]["id"]
        m = _add(db, owner, "已确认：第三期做跨会话召回")
        conv = _conv(db, owner, "会话A")
        s = db["Session"]()
        try:
            memory_repo.ignore_memory(s, conv, owner, m.id)
            assert memory_repo.unignore_memory(s, conv, owner, m.id) is True
        finally:
            s.close()
        assert _recall(db, owner, "第三期做跨会话召回", conv)

    def test_ignore_requires_owned_conversation_and_memory(self, db, users):
        alice, bob = users["alice"]["id"], users["bob"]["id"]
        m = _add(db, bob, "Bob 的记忆：跨会话召回")
        alice_conv = _conv(db, alice, "Alice 的会话")
        s = db["Session"]()
        try:
            with pytest.raises(MemoryNotOwned):
                memory_repo.ignore_memory(s, alice_conv, alice, m.id)
            bob_conv = _conv(db, bob, "Bob 的会话")
            with pytest.raises(MemoryNotOwned):
                memory_repo.ignore_memory(s, bob_conv, alice, m.id)
        finally:
            s.close()

    def test_deleting_conversation_clears_ignores(self, db, users):
        from chatchat.server.db.repository import delete_owned_conversation

        owner = users["alice"]["id"]
        m = _add(db, owner, "已确认：第三期做跨会话召回")
        conv = _conv(db, owner, "会话A")
        s = db["Session"]()
        try:
            memory_repo.ignore_memory(s, conv, owner, m.id)
            delete_owned_conversation(s, conv, owner)
            assert memory_repo.list_ignored_memory_ids(s, conv, owner) == set()
            # 记忆本体不受会话删除影响
            assert get_owned_memory(s, m.id, owner) is not None
        finally:
            s.close()


# ============================================================================
# 7. 提升为跨会话记忆（§4.3）
# ============================================================================


class TestPromotion:
    def test_promote_creates_confirmed_memory(self, db, users):
        """P3-01：会话 A 里保存的已确认决策，在新会话中可用。"""
        owner = users["alice"]["id"]
        conv_a = _conv(db, owner, "会话A")
        m = cross_session_recall.promote_to_long_term(
            db["Session"](),
            owner,
            content="第三期启用跨会话相关记忆召回。",
            kind="decision",
            scope="personal",
            source_conversation_id=conv_a,
        )
        assert m.confirmed is True
        assert m.kind == "decision"
        assert m.scope == "personal"
        assert _contents(_recall(db, owner, "第三期启用跨会话相关记忆召回", _conv(db, owner, "会话B"))) == [
            "第三期启用跨会话相关记忆召回。"
        ]

    def test_promote_project_scope_requires_owned_project(self, db, users):
        owner, other = users["alice"]["id"], users["bob"]["id"]
        bob_project = _project(db, other, "Bob 的项目")
        with pytest.raises(ValueError):
            cross_session_recall.promote_to_long_term(
                db["Session"](),
                owner,
                content="已确认：本项目不做自动模型路由",
                scope="project",
                project_id=bob_project.id,
            )

    def test_promote_rejects_temporary_conversation(self, db, users):
        owner = users["alice"]["id"]
        temp = _conv(db, owner, "临时会话", temporary=True)
        with pytest.raises(ValueError):
            cross_session_recall.promote_to_long_term(
                db["Session"](),
                owner,
                content="已确认：第三期做跨会话召回",
                source_conversation_id=temp,
            )

    def test_promote_rejects_sensitive_content(self, db, users):
        owner = users["alice"]["id"]
        with pytest.raises(ValueError):
            cross_session_recall.promote_to_long_term(
                db["Session"](), owner, content="已确认：密码是 hunter2hunter2"
            )

    def test_promote_dedupes_identical_content(self, db, users):
        owner = users["alice"]["id"]
        s = db["Session"]()
        try:
            first = cross_session_recall.promote_to_long_term(
                s, owner, content="已确认：第三期做跨会话召回"
            )
            second = cross_session_recall.promote_to_long_term(
                s, owner, content="已确认：第三期做跨会话召回"
            )
            assert first.id == second.id
        finally:
            s.close()

    def test_candidates_are_only_suggestions(self, db, users):
        """P3-13：系统只提出候选；用户不确认就不产生跨会话记忆。"""
        owner = users["alice"]["id"]
        conv = _conv(db, owner, "会话A")
        s = db["Session"]()
        try:
            session_context.add_session_confirmation(
                s, owner, conv, "已确认：第三期启用跨会话相关记忆召回"
            )
            candidates = cross_session_recall.promotion_candidates(s, owner, conv)
            assert len(candidates) == 1
            assert candidates[0].already_saved is False
            assert memory_repo.count_by_owner(s, owner) == 0
        finally:
            s.close()

    def test_candidates_mark_already_saved(self, db, users):
        owner = users["alice"]["id"]
        conv = _conv(db, owner, "会话A")
        s = db["Session"]()
        try:
            session_context.add_session_confirmation(
                s, owner, conv, "已确认：第三期启用跨会话相关记忆召回"
            )
            cross_session_recall.promote_to_long_term(
                s,
                owner,
                content="已确认：第三期启用跨会话相关记忆召回",
                source_conversation_id=conv,
            )
            candidates = cross_session_recall.promotion_candidates(s, owner, conv)
            assert candidates[0].already_saved is True
        finally:
            s.close()

    def test_candidates_empty_for_temporary_conversation(self, db, users):
        owner = users["alice"]["id"]
        temp = _conv(db, owner, "临时会话", temporary=True)
        assert cross_session_recall.promotion_candidates(db["Session"](), owner, temp) == []

    def test_suggest_promotion_only_for_confirmed_durable_wording(self, db, users):
        owner = users["alice"]["id"]
        s = db["Session"]()
        try:
            hit = cross_session_recall.suggest_promotion(
                s, owner, "我们确认：第三期采用关键词召回，不做向量检索", None
            )
            assert hit is not None
            assert hit.recommended_kind == "decision"
            miss = cross_session_recall.suggest_promotion(s, owner, "今天天气不错", None)
            assert miss is None
            one_off = cross_session_recall.suggest_promotion(
                s, owner, "本次确认：先用简洁回答", None
            )
            assert one_off is None
        finally:
            s.close()

    def test_expiry_stops_recall_after_date(self, db, users):
        """§4.5：用户可设有效期，到期后不再自动召回。"""
        owner = users["alice"]["id"]
        m = cross_session_recall.promote_to_long_term(
            db["Session"](),
            owner,
            content="已确认：本周不做跨会话召回",
            kind="todo",
            expires_at=dt.datetime.now() - dt.timedelta(hours=1),
        )
        assert _recall(db, owner, "本周不做跨会话召回") == []
        assert m.enabled is True


# ============================================================================
# 8. API：开关、预览、提升、忽略、修订、鉴权与隔离
# ============================================================================


class TestApi:
    def test_recall_preview_matches_injection(self, client, db, users):
        owner = users["alice"]["id"]
        _add(db, owner, "已确认：第三期做跨会话召回", source_conversation_id=_conv(db, owner, "旧会话"))
        r = client.get(
            "/memories/recall",
            params={"q": "第三期做跨会话召回"},
            headers=_headers(db, "alice"),
        )
        assert r.status_code == 200
        data = r.json()
        assert data["switches"]["enabled"] is True
        assert [m["content"] for m in data["memories"]] == ["已确认：第三期做跨会话召回"]
        assert data["memories"][0]["status"] == "confirmed"
        assert data["memories"][0]["source_label"]

    def test_recall_preview_rejects_foreign_conversation(self, client, db, users):
        bob_conv = _conv(db, users["bob"]["id"], "Bob 的会话")
        r = client.get(
            "/memories/recall",
            params={"q": "跨会话召回", "conversation_id": bob_conv},
            headers=_headers(db, "alice"),
        )
        assert r.status_code == 404

    def test_switches_roundtrip(self, client, db, users):
        headers = _headers(db, "alice")
        r = client.get("/memories/recall/switches", headers=headers)
        assert r.status_code == 200
        assert r.json() == {
            "enabled": True,
            "decisions": True,
            "project": False,
            "general": False,
            "preferences": True,
        }
        r = client.patch(
            "/memories/recall/switches",
            json={"general": True, "project": True},
            headers=headers,
        )
        assert r.status_code == 200
        assert r.json()["general"] is True and r.json()["project"] is True
        assert client.get("/memories/recall/switches", headers=headers).json()["general"] is True

    def test_promote_endpoint_marks_superseded(self, client, db, users):
        headers = _headers(db, "alice")
        first = client.post(
            "/memories/promote",
            json={"content": "已确认：不做自动模型路由", "kind": "decision"},
            headers=headers,
        )
        assert first.status_code == 200
        second = client.post(
            "/memories/promote",
            json={"content": "已确认：下一版本恢复自动模型路由", "kind": "decision"},
            headers=headers,
        )
        assert second.status_code == 200
        body = second.json()
        assert body["superseded_ids"] == [first.json()["memory"]["id"]]
        listed = client.get("/memories", headers=headers).json()
        statuses = {m["id"]: m["status"] for m in listed}
        assert statuses[first.json()["memory"]["id"]] == "superseded"
        assert statuses[body["memory"]["id"]] == "confirmed"

    def test_promote_endpoint_rejects_bad_input(self, client, db, users):
        headers = _headers(db, "alice")
        assert (
            client.post(
                "/memories/promote",
                json={"content": "已确认：第三期做跨会话召回", "kind": "nonsense"},
                headers=headers,
            ).status_code
            == 400
        )
        bob_conv = _conv(db, users["bob"]["id"], "Bob 的会话")
        assert (
            client.post(
                "/memories/promote",
                json={"content": "已确认：第三期做跨会话召回", "conversation_id": bob_conv},
                headers=headers,
            ).status_code
            == 404
        )

    def test_ignore_endpoints_only_affect_own_conversation(self, client, db, users):
        headers = _headers(db, "alice")
        m = _add(db, users["alice"]["id"], "已确认：第三期做跨会话召回")
        conv = _conv(db, users["alice"]["id"], "会话A")
        r = client.post(
            f"/conversations/{conv}/memory-ignores",
            json={"memory_id": m.id},
            headers=headers,
        )
        assert r.status_code == 200
        assert client.get(
            f"/conversations/{conv}/memory-ignores", headers=headers
        ).json()["memory_ids"] == [m.id]
        # 越权：别人的会话 / 别人的记忆
        bob_conv = _conv(db, users["bob"]["id"], "Bob 的会话")
        assert (
            client.post(
                f"/conversations/{bob_conv}/memory-ignores",
                json={"memory_id": m.id},
                headers=headers,
            ).status_code
            == 404
        )
        bob_memory = _add(db, users["bob"]["id"], "Bob 的记忆：跨会话召回")
        assert (
            client.post(
                f"/conversations/{conv}/memory-ignores",
                json={"memory_id": bob_memory.id},
                headers=headers,
            ).status_code
            == 404
        )
        assert (
            client.delete(
                f"/conversations/{conv}/memory-ignores/{m.id}", headers=headers
            ).status_code
            == 200
        )
        assert client.get(
            f"/conversations/{conv}/memory-ignores", headers=headers
        ).json()["memory_ids"] == []

    def test_revisions_endpoint_requires_owner(self, client, db, users):
        m = _add(db, users["alice"]["id"], "已确认：第三期做跨会话召回")
        s = db["Session"]()
        try:
            update_memory(s, m.id, users["alice"]["id"], content="已确认：第三期做跨会话召回v2")
        finally:
            s.close()
        r = client.get(
            f"/memories/{m.id}/revisions", headers=_headers(db, "alice")
        )
        assert r.status_code == 200
        assert [x["content"] for x in r.json()] == ["已确认：第三期做跨会话召回"]
        assert (
            client.get(
                f"/memories/{m.id}/revisions", headers=_headers(db, "bob")
            ).status_code
            == 404
        )

    def test_conversation_project_endpoints(self, client, db, users):
        headers = _headers(db, "alice")
        alice_project = _project(db, users["alice"]["id"], "项目A")
        bob_project = _project(db, users["bob"]["id"], "项目B")
        conv = _conv(db, users["alice"]["id"], "会话A")
        r = client.patch(
            f"/conversations/{conv}/project",
            json={"project_id": alice_project.id},
            headers=headers,
        )
        assert r.status_code == 200
        assert r.json()["project_id"] == alice_project.id
        assert (
            client.patch(
                f"/conversations/{conv}/project",
                json={"project_id": bob_project.id},
                headers=headers,
            ).status_code
            == 404
        )
        # 清除后回到无项目
        assert (
            client.patch(
                f"/conversations/{conv}/project",
                json={"project_id": None},
                headers=headers,
            ).json()["project_id"]
            is None
        )

    def test_memory_list_exposes_scope_and_status(self, client, db, users):
        owner = users["alice"]["id"]
        project = _project(db, owner, "项目A")
        _add(
            db,
            owner,
            "项目A规则：本项目不做自动模型路由",
            scope="project",
            project_id=project.id,
            project_name=project.name,
            kind="project_rule",
        )
        _add(db, owner, "自动提取：可能相关的一般结论", source="auto_extract")
        listed = client.get("/memories", headers=_headers(db, "alice")).json()
        by_content = {m["content"]: m for m in listed}
        project_row = by_content["项目A规则：本项目不做自动模型路由"]
        assert project_row["scope"] == "project"
        assert project_row["project_id"] == project.id
        assert project_row["confirmed"] is True
        assert by_content["自动提取：可能相关的一般结论"]["status"] == "unconfirmed"

    def test_create_memory_rejects_foreign_project(self, client, db, users):
        bob_project = _project(db, users["bob"]["id"], "Bob 的项目")
        r = client.post(
            "/memories",
            json={
                "content": "已确认：本项目不做自动模型路由",
                "scope": "project",
                "project_id": bob_project.id,
            },
            headers=_headers(db, "alice"),
        )
        assert r.status_code == 404

    def test_update_to_project_scope_requires_project(self, client, db, users):
        m = _add(db, users["alice"]["id"], "已确认：本项目不做自动模型路由")
        r = client.patch(
            f"/memories/{m.id}", json={"scope": "project"}, headers=_headers(db, "alice")
        )
        assert r.status_code == 400

    def test_switch_endpoints_never_show_other_user(self, client, db, users):
        _add(db, users["bob"]["id"], "Bob 的已确认：跨会话召回方案")
        r = client.get(
            "/memories/recall", params={"q": "跨会话召回方案"}, headers=_headers(db, "alice")
        )
        assert r.json()["memories"] == []

    @pytest.mark.parametrize(
        "method,url",
        [
            ("get", "/memories/recall"),
            ("get", "/memories/recall/switches"),
            ("patch", "/memories/recall/switches"),
            ("post", "/memories/promote"),
            ("get", "/memories/abc/revisions"),
        ],
    )
    def test_unauthenticated_rejected(self, client, method, url):
        r = getattr(client, method)(url, json={} if method in ("patch", "post") else None)
        assert r.status_code == 401

    def test_disabled_user_rejected(self, client, db, secret):
        _mk_user(db, "gone", disabled=True)
        r = client.get("/memories/recall", headers=_headers(db, "gone"))
        assert r.status_code in (401, 403)

    def test_must_change_password_rejected(self, client, db, secret):
        _mk_user(db, "fresh", must_change_password=True)
        r = client.get("/memories/recall/switches", headers=_headers(db, "fresh"))
        assert r.status_code in (401, 403)


# ============================================================================
# 9. 知识库对话：历史记忆只提供背景，不替代引用（P3-15 的服务端部分）
# ============================================================================


class TestRagBoundary:
    def test_recall_segment_states_it_does_not_replace_citations(self, db, users):
        owner = users["alice"]["id"]
        _add(db, owner, "已确认：本项目不做自动模型路由")
        segment, used = cross_session_recall.build_recall_segment(
            db["Session"](), owner, "本项目不做自动模型路由"
        )
        assert used
        assert "检索到的文档" in segment
