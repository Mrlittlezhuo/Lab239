"""统一上下文构建器（智能聊天记忆第四期·第一阶段 A，设计方案 §6.2 / §18.2.2-3）。

钉住四件事：

1. **无空洞**：摘要覆盖范围 + 最近原文窗口 = 当前有效会话历史，分界只有一处
   （M-01/M-02/M-06/M-07）；
2. 摘要掉队时宁可扩大窗口，也不让历史在 Prompt 里凭空消失，并如实标记降级；
3. 取消/失败的轮次不作为助手说法回放，但用户原话仍参与摘要与槽位；
4. 记忆段与界面同源：开关只允许收紧，快照 ID 与真实注入一致（§12.3）；
5. 当前消息与会话约束冲突时，本轮注入的上下文段就已采用新说法（§18.2.12）。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from chatchat.server.auth import jwt as auth_jwt
from chatchat.server.auth import service
from chatchat.server.db.base import Base
from chatchat.server.db.migrate.base import apply_migrations
from chatchat.server.db.migrate.migrations import build_registry
from chatchat.server.db.models.conversation_model import ConversationModel  # noqa: F401
from chatchat.server.db.models.knowledge_base_model import KnowledgeBaseModel  # noqa: F401
from chatchat.server.db.models.knowledge_file_model import KnowledgeFileModel  # noqa: F401
from chatchat.server.db.models.message_model import MessageModel  # noqa: F401
from chatchat.server.db.models.user_model import UserModel  # noqa: F401
from chatchat.server.db.repository import (
    add_message,
    create_conversation,
    create_memory,
    session_summary_repository,
)
from chatchat.server.memory import context_package, session_context

SECRET = "unit-test-secret-key-0000000000000000"


@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setattr(auth_jwt, "get_secret", lambda: SECRET)
    return SECRET


@pytest.fixture
def db(secret, tmp_path):
    path = tmp_path / "context_package.db"
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as conn:
        apply_migrations(build_registry(), conn)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    yield {"Session": Session, "engine": engine}
    engine.dispose()


@pytest.fixture
def users(db):
    out = {}
    for name in ("alice", "bob"):
        s = db["Session"]()
        try:
            u = service.create_user(
                s, username=name, display_name=name.title(), password="password12345678"
            )
            u.must_change_password = False
            s.commit()
            out[name] = {"id": u.id}
        finally:
            s.close()
    return out


def _conv(db, owner_id, temporary=False) -> str:
    s = db["Session"]()
    try:
        return create_conversation(s, owner_id, "agent_chat", "会话", temporary=temporary)
    finally:
        s.close()


def _say(db, conv, owner_id, *queries, response="回答"):
    s = db["Session"]()
    try:
        for i, q in enumerate(queries):
            add_message(s, conv, owner_id, "agent_chat", q, response=f"{response}{i}")
    finally:
        s.close()


def _build(db, owner_id, conv, query="", **kwargs):
    s = db["Session"]()
    try:
        return context_package.build_context_package(s, owner_id, conv, query, **kwargs)
    finally:
        s.close()


# 每条 query 43 字符 + response 40 字符 + 8 开销 = 91 字符
def _pad(i: int) -> str:
    return f"问题{i:02d}" + "补" * 40


def _say_pad(db, conv, owner_id, start: int, count: int):
    """写入 count 轮问答，每轮固定 91 字符开销，便于按预算算窗口大小。"""
    s = db["Session"]()
    try:
        for i in range(start, start + count):
            add_message(s, conv, owner_id, "agent_chat", _pad(i), response="答" * 40)
    finally:
        s.close()


# ============================================================================
# 1. 分界与无空洞
# ============================================================================


class TestBoundary:
    def test_summary_and_window_meet_exactly_at_the_boundary(self, db, users, monkeypatch):
        """M-01/M-02：窗口起点之前全部进摘要，之后全部留原文。"""
        monkeypatch.setenv("CHATCHAT_RECENT_HISTORY_MAX_CHARS", "500")
        aid = users["alice"]["id"]
        conv = _conv(db, aid)
        _say_pad(db, conv, aid, 0, 12)
        pkg = _build(db, aid, conv)
        # 91 字符一条：预算 500 只装得下最新 5 条（5*91=455，再加一条溢出）
        assert [m.query for m in pkg.recent_messages] == [_pad(i) for i in range(7, 12)]
        assert pkg.boundary_message_id == pkg.recent_messages[0].id
        assert pkg.context.summarized_count == 7
        assert "问题00" in pkg.context.summary
        assert "问题06" in pkg.context.summary
        assert "问题07" not in pkg.context.summary
        assert pkg.history_gap_due_to_degradation is False
        assert pkg.history_truncated is False

    def test_next_turn_only_summarizes_what_left_the_window(self, db, users, monkeypatch):
        """M-07：下一轮把新移出的一条并入摘要，不重复摘要、不丢中间消息。"""
        monkeypatch.setenv("CHATCHAT_RECENT_HISTORY_MAX_CHARS", "500")
        aid = users["alice"]["id"]
        conv = _conv(db, aid)
        _say_pad(db, conv, aid, 0, 12)
        first = _build(db, aid, conv)
        assert first.context.summarized_count == 7
        _say_pad(db, conv, aid, 12, 2)
        second = _build(db, aid, conv)
        assert second.context.summarized_count == 9
        # 新移出的两条进了摘要，仍在窗口内的没有；旧行一条不少也不重复
        assert "问题07" in second.context.summary
        assert "问题08" in second.context.summary
        assert "问题09" not in second.context.summary
        old_lines = first.context.summary.splitlines()
        new_lines = second.context.summary.splitlines()
        assert all(ln in new_lines for ln in old_lines)
        assert len(new_lines) == len(set(new_lines))
        assert second.history_gap_due_to_degradation is False

    def test_single_oversized_turn_is_kept_and_marked(self, db, users, monkeypatch):
        """M-06：单轮就超预算时保留最新一轮并标记截断，不整段丢掉当前问题。"""
        monkeypatch.setenv("CHATCHAT_RECENT_HISTORY_MAX_CHARS", "500")
        aid = users["alice"]["id"]
        conv = _conv(db, aid)
        _say(db, conv, aid, "很长的一个问题" + "长" * 900, response="答" * 300)
        pkg = _build(db, aid, conv)
        assert len(pkg.recent_messages) == 1
        assert pkg.history_truncated is True
        assert pkg.recent_messages[0].query.startswith("很长的一个问题")

    def test_gap_when_summary_falls_behind(self, db, users, monkeypatch):
        """摘要掉队：窗口被迫扩大，历史不会在 Prompt 里凭空消失，并标记降级。"""
        monkeypatch.setenv("CHATCHAT_RECENT_HISTORY_MAX_CHARS", "500")
        aid = users["alice"]["id"]
        conv = _conv(db, aid)
        _say(db, conv, aid, *[_pad(i) for i in range(12)])

        def boom(existing, older_text):
            raise RuntimeError("模型不可用")

        pkg = _build(db, aid, conv, summarizer=boom)
        assert pkg.history_gap_due_to_degradation is True
        # 窗口扩大到全部消息：没有任何一条既不在摘要也不在窗口
        assert len(pkg.recent_messages) == 12
        assert pkg.context.summarized_message_id is None

    def test_unanswered_turn_not_replayed_but_still_scanned(self, db, users):
        """取消/失败的轮次：不作为助手说法回放，但用户原话仍进槽位。"""
        aid = users["alice"]["id"]
        conv = _conv(db, aid)
        _say(db, conv, aid, "讨论存储方案")
        s = db["Session"]()
        try:
            # 只有用户问题、没有回答的一轮（取消或失败）
            add_message(s, conv, aid, "agent_chat", "确认：本项目采用 PostgreSQL 存储", response="")
        finally:
            s.close()
        pkg = _build(db, aid, conv)
        assert [m.response for m in pkg.recent_messages] == ["回答0"]
        assert any("PostgreSQL" in i.text for i in pkg.context.confirmed)


# ============================================================================
# 2. 当前消息优先
# ============================================================================


class TestCurrentTurnWins:
    def test_negation_in_the_current_turn_updates_the_injected_segment(self, db, users):
        """§18.2.12：当前消息与会话约束冲突时，本轮注入的段就已经是新说法。

        本轮的用户原话此时还没有助手回答（``response`` 为空），所以它不进最近
        原文窗口；但槽位必须在同一次构建里被更新，否则模型这一轮看到的仍是
        旧约束——正是"界面已经改了、Prompt 里没改"的老毛病。
        """
        aid = users["alice"]["id"]
        conv = _conv(db, aid)
        _say(db, conv, aid, "讨论数据库选型", "本项目采用 MySQL 存储会话记录")
        s = db["Session"]()
        try:
            add_message(
                s, conv, aid, "agent_chat",
                "不对，改用 PostgreSQL 存储会话记录",
                response="",
            )
        finally:
            s.close()
        pkg = _build(db, aid, conv, "不对，改用 PostgreSQL 存储会话记录")
        assert any("PostgreSQL" in i.text for i in pkg.context.confirmed)
        assert not any("MySQL" in i.text for i in pkg.context.confirmed)
        segment = pkg.context_segment
        assert "PostgreSQL" in segment
        assert "MySQL" not in segment


# ============================================================================
# 3. 记忆段与快照
# ============================================================================


class TestMemorySnapshot:
    def test_preference_ids_and_segment_match_injection(self, db, users):
        aid = users["alice"]["id"]
        conv = _conv(db, aid)
        _say(db, conv, aid, "讨论回答风格")
        s = db["Session"]()
        try:
            pref = create_memory(
                s, aid, "回答统一使用中文", source="user", category="answer_style"
            )
        finally:
            s.close()
        pkg = _build(db, aid, conv, "回答风格")
        assert pref.id in pkg.preference_ids
        assert "回答统一使用中文" in pkg.system_segments()[0]

        off = _build(db, aid, conv, "回答风格", use_memory=False)
        assert off.preference_ids == []
        assert off.memory_segments == []
        # 关闭个人偏好不影响本会话上下文（§12.3：只收紧记忆段）
        assert off.context_segment.startswith(session_context.CONTEXT_HEADER)

    def test_snapshot_ids_are_the_same_ones_injected(self, db, users):
        """清单快照与真实注入同源：第四阶段写清单时不必再查一遍。"""
        aid = users["alice"]["id"]
        conv = _conv(db, aid)
        _say(db, conv, aid, "讨论既往决策")
        s = db["Session"]()
        try:
            old_conv = create_conversation(s, aid, "agent_chat", "旧会话")
            mem = create_memory(
                s,
                aid,
                "本项目不使用自动模型路由",
                source="user",
                kind="decision",
                scope="personal",
                confirmed=True,
                source_conversation_id=old_conv,
            )
        finally:
            s.close()
        pkg = _build(db, aid, conv, "本项目是否使用自动模型路由")
        assert mem.id in pkg.recalled_memory_ids
        joined = "\n".join(pkg.system_segments())
        assert "自动模型路由" in joined

    def test_refresh_false_reads_without_writing(self, db, users):
        aid = users["alice"]["id"]
        conv = _conv(db, aid)
        _say(db, conv, aid, "讨论记忆设计")
        pkg = _build(db, aid, conv, refresh=False)
        s = db["Session"]()
        try:
            assert session_summary_repository.get_session_summary(s, conv, aid) is None
        finally:
            s.close()
        assert pkg.context is None or pkg.context.is_empty()


# ============================================================================
# 4. 边界情况
# ============================================================================


class TestEdgeCases:
    def test_memoryless_conversation_reads_history_but_builds_no_context(
        self, db, users, monkeypatch
    ):
        """无记忆对话：原始消息照常持久化、最近原文照读，但不建会话上下文。

        设计方案 §13.1：现界面名"临时对话"只表示不写记忆，会话与消息照常保存，
        所以多轮连续性仍由数据库里的最近原文提供，不退回请求体里的 history；
        同时不生成会话上下文/摘要，也不因"摘要没覆盖分界之前的消息"报降级。
        """
        monkeypatch.setenv("CHATCHAT_RECENT_HISTORY_MAX_CHARS", "500")
        aid = users["alice"]["id"]
        conv = _conv(db, aid, temporary=True)
        _say_pad(db, conv, aid, 0, 12)
        s = db["Session"]()
        try:
            pref = create_memory(
                s, aid, "回答统一使用中文", source="user", category="answer_style"
            )
        finally:
            s.close()
        pkg = _build(db, aid, conv, "回答风格")
        assert pkg.temporary is True
        # 预算 500、每轮 91 字符 → 只带最近 5 轮，且不带会话上下文段
        assert len(pkg.recent_messages) == 5
        assert [q for q, _ in pkg.history_pairs()][0] == _pad(7)
        assert pkg.context is None
        assert pkg.context_segment == ""
        assert pkg.history_gap_due_to_degradation is False
        assert pkg.preference_ids == [pref.id]
        s = db["Session"]()
        try:
            assert session_summary_repository.get_session_summary(s, conv, aid) is None
        finally:
            s.close()

    def test_no_conversation_still_injects_preferences(self, db, users):
        """新会话首轮（还没有 conversation_id）也要带上个人偏好。"""
        aid = users["alice"]["id"]
        s = db["Session"]()
        try:
            create_memory(s, aid, "回答统一使用中文", source="user", category="answer_style")
        finally:
            s.close()
        s = db["Session"]()
        try:
            pkg = context_package.build_context_package(s, aid, None, "回答风格")
        finally:
            s.close()
        assert pkg.recent_messages == []
        assert "回答统一使用中文" in pkg.system_segments()[0]

    def test_empty_conversation(self, db, users):
        aid = users["alice"]["id"]
        conv = _conv(db, aid)
        pkg = _build(db, aid, conv)
        assert pkg.recent_messages == []
        assert pkg.boundary_message_id is None
        assert pkg.is_empty()

    def test_other_users_conversation_is_not_readable(self, db, users):
        aid = users["alice"]["id"]
        conv = _conv(db, aid)
        _say(db, conv, aid, "讨论记忆设计")
        pkg = _build(db, users["bob"]["id"], conv)
        assert pkg.recent_messages == []
        assert pkg.system_segments() == []

    def test_history_pairs_are_ascending(self, db, users):
        aid = users["alice"]["id"]
        conv = _conv(db, aid)
        _say(db, conv, aid, "第一问", "第二问", "第三问")
        pkg = _build(db, aid, conv)
        assert [q for q, _ in pkg.history_pairs()] == ["第一问", "第二问", "第三问"]
        assert pkg.history_message_ids() == [m.id for m in pkg.recent_messages]
