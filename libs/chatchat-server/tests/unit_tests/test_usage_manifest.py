"""每轮实际使用记录 memory_usage_manifest 与生成状态（第四期·第一阶段 B，§11、§18.3）。

钉住五件事：

1. manifest 与模型输入同源：窗口消息 ID、偏好内容与召回理由、摘要版本都来自
   同一次构建，不是回答后重查（§11.1）；
2. metadata **按字段合并**：使用记录、检索引用、Agent 状态共存，谁都不能整体
   覆盖谁（§11.2）；
3. 状态机如实：prepared → started → completed，模型没启动就不写
   model_started_at（§11.1）；
4. 停止/失败时半截回答照常落库，没有文本就不制造空的助手结论（§11.3）；
5. 写入按属主隔离：别人的 message_id 写不进去（§18.3.13）。
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
    update_message,
)
from chatchat.server.db.repository.message_repository import (
    ConversationNotOwned,
    merge_message_metadata,
)
from chatchat.server.memory import context_package, usage_manifest

SECRET = "unit-test-secret-key-0000000000000000"


@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setattr(auth_jwt, "get_secret", lambda: SECRET)
    return SECRET


@pytest.fixture
def db(secret, tmp_path):
    path = tmp_path / "usage_manifest.db"
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


def _conv(db, owner_id) -> str:
    s = db["Session"]()
    try:
        return create_conversation(s, owner_id, "agent_chat", "会话")
    finally:
        s.close()


def _say(db, conv, owner_id, *queries, response="回答") -> str:
    """写入若干轮问答，返回最后一条消息的 ID。"""
    s = db["Session"]()
    last = None
    try:
        for i, q in enumerate(queries):
            last = add_message(
                s, conv, owner_id, "agent_chat", q, response=f"{response}{i}"
            )
        return last
    finally:
        s.close()


def _pending(db, conv, owner_id, query="问题") -> str:
    """助手占位消息：response 先留空，等生成结果回填（真实链路的起点）。"""
    s = db["Session"]()
    try:
        return add_message(s, conv, owner_id, "agent_chat", query, "")
    finally:
        s.close()


def _build(db, owner_id, conv, query="", **kwargs):
    s = db["Session"]()
    try:
        return context_package.build_context_package(s, owner_id, conv, query, **kwargs)
    finally:
        s.close()


def _meta(db, message_id):
    s = db["Session"]()
    try:
        m = s.get(MessageModel, message_id)
        return dict(m.meta_data or {}), (m.response or "")
    finally:
        s.close()


# ============================================================================
# 1. manifest 与模型输入同源
# ============================================================================


class TestManifestShape:
    def test_manifest_carries_window_preferences_and_recalls(self, db, users):
        aid = users["alice"]["id"]
        conv = _conv(db, aid)
        _say(db, conv, aid, "我们的部署约定是什么", "回答部署约定")
        s = db["Session"]()
        try:
            pref = create_memory(
                s, aid, "回答统一使用中文", source="user", category="answer_style"
            )
            recalled = create_memory(
                s,
                aid,
                "本项目采用 LangGraph 编排 Agent",
                source="user",
                kind="decision",
            )
        finally:
            s.close()

        package = _build(db, aid, conv, "LangGraph 部署约定")
        manifest = usage_manifest.build_usage_manifest(package)

        assert manifest["conversation_id"] == conv
        assert manifest["recent_message_ids"] == package.history_message_ids()
        assert manifest["history_boundary_message_id"] == package.boundary_message_id
        assert manifest["generation_status"] == "prepared"
        assert manifest["memory_disabled_for_turn"] is False
        assert manifest["model_started_at"] is None
        # 偏好：记内容而不只是 ID，记忆后来被改删也能还原当时注入了什么
        pref_ids = [p["id"] for p in manifest["preferences"]]
        assert pref_ids == [pref.id]
        assert "回答统一使用中文" in [p["content"] for p in manifest["preferences"]]
        # 召回：带理由，界面要能解释"为什么这条被用上"
        assert recalled.id in [r["id"] for r in manifest["recalled_memories"]]
        reason = [r.get("reason") for r in manifest["recalled_memories"]]
        assert any(reason)
        # 会话上下文版本与摘要覆盖位置：历史回答能对上"当时是哪一版上下文"
        assert manifest["session_context_version"] == package.context.version
        assert manifest["summary_through_message_id"] == (
            package.context.summarized_message_id
        )

    def test_turn_without_memory_is_marked_not_just_empty(self, db, users):
        """本轮不使用个人记忆要如实标记，不能只留空列表让界面猜。"""
        aid = users["alice"]["id"]
        conv = _conv(db, aid)
        _say(db, conv, aid, "回答风格")
        s = db["Session"]()
        try:
            create_memory(
                s, aid, "回答统一使用中文", source="user", category="answer_style"
            )
        finally:
            s.close()
        package = _build(db, aid, conv, "回答风格", use_memory=False)
        manifest = usage_manifest.build_usage_manifest(package, use_memory=False)
        assert manifest["memory_disabled_for_turn"] is True
        assert manifest["preferences"] == []
        assert manifest["recalled_memories"] == []

    def test_manifest_is_a_snapshot_not_a_live_query(self, db, users):
        """生成后删掉记忆，历史 manifest 不变（§11.2 最后一条）。"""
        aid = users["alice"]["id"]
        conv = _conv(db, aid)
        _say(db, conv, aid, "回答风格")
        s = db["Session"]()
        try:
            pref = create_memory(
                s, aid, "回答统一使用中文", source="user", category="answer_style"
            )
        finally:
            s.close()
        package = _build(db, aid, conv, "回答风格")
        manifest = usage_manifest.build_usage_manifest(package)
        s = db["Session"]()
        try:
            from chatchat.server.db.repository import delete_memory

            delete_memory(s, pref.id, aid)
        finally:
            s.close()
        assert [p["id"] for p in manifest["preferences"]] == [pref.id]
        assert manifest["preferences"][0]["content"] == "回答统一使用中文"


# ============================================================================
# 2. metadata 按字段合并
# ============================================================================


class TestMergeSemantics:
    def test_merge_keeps_other_keys(self, db, users):
        aid = users["alice"]["id"]
        conv = _conv(db, aid)
        msg = _say(db, conv, aid, "知识库里写了什么")
        s = db["Session"]()
        try:
            update_message(
                session=s,
                message_id=msg,
                owner_id=aid,
                metadata={"retrieval_status": "matched", "references": [{"index": 1}]},
            )
        finally:
            s.close()
        merge_message_metadata(
            db["Session"](), msg, aid, {"generation_status": "completed"}
        )
        meta, _ = _meta(db, msg)
        assert meta["retrieval_status"] == "matched"
        assert meta["references"] == [{"index": 1}]
        assert meta["generation_status"] == "completed"

    def test_merge_can_delete_a_key_with_none(self, db, users):
        aid = users["alice"]["id"]
        conv = _conv(db, aid)
        msg = _say(db, conv, aid, "问题")
        merge_message_metadata(db["Session"](), msg, aid, {"a": 1, "b": 2})
        merge_message_metadata(db["Session"](), msg, aid, {"a": None})
        meta, _ = _meta(db, msg)
        assert "a" not in meta
        assert meta["b"] == 2

    def test_foreign_message_cannot_be_written(self, db, users):
        """manifest 只能落在当前用户自己的消息上（§18.3.13）。"""
        aid = users["alice"]["id"]
        bid = users["bob"]["id"]
        conv = _conv(db, aid)
        msg = _say(db, conv, aid, "问题")
        with pytest.raises(ConversationNotOwned):
            merge_message_metadata(db["Session"](), msg, bid, {"generation_status": "completed"})
        meta, _ = _meta(db, msg)
        assert "generation_status" not in meta

    def test_recorder_write_never_breaks_the_answer(self, db, users):
        """落库失败（消息不存在）只记日志，绝不抛到回答链路上。"""
        recorder = usage_manifest.TurnRecorder(
            message_id="does-not-exist",
            owner_id=users["alice"]["id"],
            manifest={"generation_status": "prepared"},
            session=db["Session"](),
        )
        assert recorder.prepare() is not None  # 不抛异常即通过


# ============================================================================
# 3-4. 状态机与半截回答
# ============================================================================


class TestGenerationStatus:
    def _recorder(self, db, message_id, owner_id):
        return usage_manifest.TurnRecorder(
            message_id=message_id,
            owner_id=owner_id,
            manifest={
                "generation_status": "prepared",
                "recent_message_ids": [],
                "model_started_at": None,
                "model_finished_at": None,
            },
            session=db["Session"](),
        )

    def test_prepared_started_completed(self, db, users):
        aid = users["alice"]["id"]
        msg = _pending(db, _conv(db, aid), aid, "问题")
        rec = self._recorder(db, msg, aid)
        rec.prepare()
        assert _meta(db, msg)[0]["generation_status"] == "prepared"
        rec.started()
        rec.finish("completed", response="完整回答")
        meta, response = _meta(db, msg)
        assert response == "完整回答"
        assert meta["generation_status"] == "completed"
        assert meta["memory_usage_manifest"]["generation_status"] == "completed"
        assert meta["memory_usage_manifest"]["model_started_at"]
        assert meta["memory_usage_manifest"]["model_finished_at"]

    def test_model_started_at_not_fabricated(self, db, users):
        """模型没启动就失败：状态是 failed，但不能说模型用过这些上下文。"""
        aid = users["alice"]["id"]
        msg = _pending(db, _conv(db, aid), aid, "问题")
        rec = self._recorder(db, msg, aid)
        rec.prepare()
        rec.finish("failed", failure_stage="retrieval", failure_reason="嵌入模型不可用")
        meta, response = _meta(db, msg)
        assert meta["generation_status"] == "failed"
        manifest = meta["memory_usage_manifest"]
        assert manifest["model_started_at"] is None
        assert manifest["model_finished_at"] is None
        assert manifest["failure_stage"] == "retrieval"
        assert manifest["failure_reason"] == "嵌入模型不可用"
        # 没有文本就不制造空的助手结论
        assert response == ""

    def test_cancel_keeps_partial_answer(self, db, users):
        """用户点停止：已生成的半截回答必须保存，刷新后不是空串（§11.3）。"""
        aid = users["alice"]["id"]
        msg = _pending(db, _conv(db, aid), aid, "写一段长文")
        rec = self._recorder(db, msg, aid)
        rec.prepare()
        rec.started()
        rec.note("第一段已经生成出来了，")
        rec.note("第二段也在")
        rec.finish("cancelled", failure_stage="streaming")
        meta, response = _meta(db, msg)
        assert response == "第一段已经生成出来了，第二段也在"
        assert meta["generation_status"] == "cancelled"
        assert usage_manifest.is_incomplete_metadata(meta) is True

    def test_finish_is_idempotent(self, db, users):
        aid = users["alice"]["id"]
        msg = _pending(db, _conv(db, aid), aid, "问题")
        rec = self._recorder(db, msg, aid)
        rec.prepare()
        rec.finish("completed", response="一次就好")
        rec.finish("cancelled", failure_stage="streaming")
        meta, response = _meta(db, msg)
        assert response == "一次就好"
        assert meta["generation_status"] == "completed"

    def test_resume_adopts_the_existing_manifest(self, db, users):
        """恢复路径保留同一份快照，只更新状态（§18.3.6）。"""
        aid = users["alice"]["id"]
        conv = _conv(db, aid)
        msg = _say(db, conv, aid, "问题")
        original = {
            "generation_status": "cancelled",
            "recent_message_ids": ["m1", "m2"],
            "preferences": [{"id": "p1", "content": "回答统一使用中文"}],
        }
        merge_message_metadata(
            db["Session"](), msg, aid, {usage_manifest.MANIFEST_KEY: original}
        )
        rec = usage_manifest.TurnRecorder(
            message_id=msg, owner_id=aid, session=db["Session"]()
        )
        adopted = rec.adopt_existing()
        assert adopted == original
        rec.finish("completed", response="续写完成")
        meta, response = _meta(db, msg)
        assert response == "续写完成"
        manifest = meta["memory_usage_manifest"]
        assert manifest["recent_message_ids"] == ["m1", "m2"]
        assert manifest["preferences"] == original["preferences"]
        assert manifest["generation_status"] == "completed"

    def test_status_written_even_without_a_manifest(self, db, users):
        aid = users["alice"]["id"]
        msg = _pending(db, _conv(db, aid), aid, "问题")
        rec = usage_manifest.TurnRecorder(
            message_id=msg, owner_id=aid, session=db["Session"]()
        )
        rec.finish("failed", failure_stage="routing", failure_reason="路由失败")
        meta, response = _meta(db, msg)
        assert meta["generation_status"] == "failed"
        assert usage_manifest.MANIFEST_KEY not in meta
        assert response == ""


# ============================================================================
# 5. 失败原因脱敏与未完成回答标记
# ============================================================================


class TestSanitizeAndMarker:
    def test_api_key_is_redacted(self):
        text = usage_manifest.sanitize_reason(
            "调用失败 api_key=sk-live-abcdef123456 请检查配置"
        )
        assert "sk-live-abcdef123456" not in text
        assert "api_key" in text

    def test_url_credentials_are_redacted(self):
        """失败原因里的连接串凭据不能进 metadata（任意 scheme）。"""
        text = usage_manifest.sanitize_reason(
            "连接 postgres://user:pass@db.internal:5432 失败"
        )
        assert "user:pass@" not in text
        assert "postgres://" in text
        text = usage_manifest.sanitize_reason("GET https://u:p@example.com/x 返回 401")
        assert "u:p@" not in text

    def test_long_reason_is_collapsed_to_one_line(self):
        text = usage_manifest.sanitize_reason("第一行\n第二行 " + "很" * 500, max_chars=120)
        assert "\n" not in text
        assert len(text) <= 121

    def test_incomplete_marker_is_added_once(self):
        marked = usage_manifest.mark_incomplete("写了一半")
        assert marked.startswith(usage_manifest.INCOMPLETE_MARKER)
        assert usage_manifest.mark_incomplete(marked) == marked

    def test_completed_answer_is_not_incomplete(self):
        assert usage_manifest.is_incomplete_metadata({"generation_status": "completed"}) is False
        assert usage_manifest.is_incomplete_metadata(None) is False
        assert usage_manifest.is_incomplete_metadata("not-a-dict") is False

    def test_manifest_frame_payload_shape(self):
        """SSE 元数据帧的额外字段：类型明确、带 message_id（§11.4）。"""
        payload = usage_manifest.manifest_payload(
            {"generation_status": "prepared"}, "msg-1"
        )
        assert payload["class_name"] == "MemoryUsageEvent"
        assert payload["message_id"] == "msg-1"
        assert payload[usage_manifest.STATUS_KEY] == "prepared"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-o", "addopts=", "-p", "no:cacheprovider"])
