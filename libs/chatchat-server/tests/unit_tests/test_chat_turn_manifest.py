"""智能聊天链路（chat.py）的本轮使用记录与半截回答（第四期·第一阶段 B，§18.3.1-2/4/8/12、§11.4）。

kb_chat 与 Agent 链路的 manifest 已有各自测试，这里补上普通聊天这一条：

1. 模型调用**之前**就下发 ``MemoryUsageEvent`` 元数据帧，正文为空，旧客户端
   不会把它拼进回答（§11.4）；
2. 收尾再发一帧，状态是最终状态；流式与非流式消费者拿到的是同一份快照
   （§18.3.12）；
3. 中途取消时，已经流出来的半截回答照常落库并标记 ``cancelled``（§11.3）；
4. 元数据帧与 token 帧互不污染：token 帧不带 manifest，元数据帧正文为空。
"""
from __future__ import annotations

import asyncio
import json
import types

import pytest
from langchain_chatchat.agents.platform_tools import PlatformToolsLLMStatus
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from chatchat.server.auth import jwt as auth_jwt
from chatchat.server.auth import service
from chatchat.server.chat import chat as chat_mod
from chatchat.server.db.base import Base
from chatchat.server.db.migrate.base import apply_migrations
from chatchat.server.db.migrate.migrations import build_registry
from chatchat.server.db.models.conversation_model import ConversationModel  # noqa: F401
from chatchat.server.db.models.knowledge_base_model import KnowledgeBaseModel  # noqa: F401
from chatchat.server.db.models.knowledge_file_model import KnowledgeFileModel  # noqa: F401
from chatchat.server.db.models.message_model import MessageModel  # noqa: F401
from chatchat.server.db.models.user_model import UserModel  # noqa: F401
from chatchat.server.db.repository import add_message, create_conversation, create_memory
from chatchat.server.memory import context_package, usage_manifest

SECRET = "unit-test-secret-key-0000000000000000"


@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setattr(auth_jwt, "get_secret", lambda: SECRET)
    return SECRET


@pytest.fixture
def db(secret, tmp_path):
    path = tmp_path / "chat_manifest.db"
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


class _FakeChain:
    """把脚本化的 Agent 事件当作模型输出流出来。"""

    def __init__(self, items):
        self._items = items

    def invoke(self, _inputs):
        async def gen():
            for item in self._items:
                yield item

        return gen()


def _token(text: str) -> PlatformToolsLLMStatus:
    return PlatformToolsLLMStatus(run_id="r1", status=2, text=text)


def _patch_model(monkeypatch, chain, package):
    """打桩模型装配与工具表：只留本轮真正要验证的那几段逻辑。"""
    monkeypatch.setattr(
        chat_mod,
        "create_models_from_config",
        lambda callbacks, configs, stream, max_tokens: (
            {"llm_model": types.SimpleNamespace(model_name="fake-llm")},
            {},
        ),
    )
    monkeypatch.setattr(chat_mod, "get_tool", lambda: {})
    monkeypatch.setattr(
        chat_mod,
        "create_models_chains",
        lambda **kwargs: (
            chain,
            types.SimpleNamespace(history=[], intermediate_steps=[]),
            package,
        ),
    )
    # 自动记忆提取会走模型/规则判定，与本轮 manifest 无关
    monkeypatch.setattr(chat_mod.memory_service, "auto_extract", lambda *a, **k: None)
    # 不套 SSE 外壳，直接消费生成器
    monkeypatch.setattr(chat_mod, "EventSourceResponse", lambda gen: gen)


def _setup_turn(db, aid, query, *prior_turns):
    """建会话、写入历史与一条偏好，并建好本轮的助手占位消息。"""
    s = db["Session"]()
    try:
        conv = create_conversation(s, aid, "agent_chat", "会话")
        for i, q in enumerate(prior_turns):
            add_message(s, conv, aid, "agent_chat", q, response=f"早前回答{i}")
        create_memory(s, aid, "回答统一使用中文", source="user", category="answer_style")
        message_id = add_message(s, conv, aid, "agent_chat", query, response="")
        package = context_package.build_context_package(s, aid, conv, query)
        return conv, message_id, package
    finally:
        s.close()


def _run_chat(db, aid, conv, message_id, query, *, stream):
    async def go():
        response = await chat_mod.chat(
            query=query,
            conversation_id=conv,
            message_id=message_id,
            stream=stream,
            tool_config={},
            use_mcp=False,
            session=db["Session"](),
            owner_id=aid,
        )
        if not stream:
            return [response]
        return [json.loads(chunk) async for chunk in response]

    return asyncio.run(go())


def _turn(db, users, monkeypatch, items, *, stream=True, query="再说说部署要求"):
    """跑一轮智能聊天，返回 ``(帧列表, 助手消息 ID)``。"""
    aid = users["alice"]["id"]
    conv, message_id, package = _setup_turn(db, aid, query, "我们之前定了什么")
    _patch_model(monkeypatch, _FakeChain(items), package)
    return _run_chat(db, aid, conv, message_id, query, stream=stream), message_id


def _manifest_frames(frames):
    return [f for f in frames if f.get("class_name") == "MemoryUsageEvent"]


def _content(frame) -> str:
    return frame["choices"][0]["delta"].get("content") or ""


def _meta_and_response(db, message_id):
    s = db["Session"]()
    try:
        m = s.get(MessageModel, message_id)
        return dict(m.meta_data or {}), (m.response or "")
    finally:
        s.close()


class TestChatManifestFrames:
    def test_manifest_frame_precedes_tokens_and_closes_the_stream(
        self, db, users, monkeypatch
    ):
        frames, message_id = _turn(
            db, users, monkeypatch, [_token("第一段。"), _token("第二段。")]
        )
        manifests = _manifest_frames(frames)
        assert manifests, "智能聊天链路缺少本轮使用记录元数据帧"
        # 第一帧就是快照：模型还没开始，前端已经能显示"上下文已准备"
        assert manifests[0]["generation_status"] in ("prepared", "started")
        assert manifests[-1]["generation_status"] == "completed"
        for frame in manifests:
            assert _content(frame) == ""  # 正文为空，旧客户端不会拼进回答
        # token 帧不带 manifest：元数据不混进正文流
        for frame in frames:
            if frame.get("class_name") != "MemoryUsageEvent":
                assert usage_manifest.MANIFEST_KEY not in frame
        tokens = "".join(
            _content(f) for f in frames if f.get("class_name") != "MemoryUsageEvent"
        )
        assert tokens == "第一段。第二段。"

        meta, response = _meta_and_response(db, message_id)
        assert response == "第一段。第二段。"
        assert meta["generation_status"] == "completed"
        manifest = meta[usage_manifest.MANIFEST_KEY]
        assert manifest["generation_status"] == "completed"
        assert manifest["recent_message_ids"]
        assert [p["content"] for p in manifest["preferences"]] == ["回答统一使用中文"]
        assert manifest["model_started_at"] and manifest["model_finished_at"]

    def test_snapshot_is_bound_to_the_assistant_message(self, db, users, monkeypatch):
        """元数据帧带 message_id，前端才能把快照绑到对应回答上（§11.2）。"""
        frames, message_id = _turn(db, users, monkeypatch, [_token("答案")])
        assert _manifest_frames(frames)
        for frame in _manifest_frames(frames):
            assert frame["message_id"] == message_id

    def test_cancelled_stream_keeps_partial_answer(self, db, users, monkeypatch):
        """模型中途取消：已流出的半截回答照常保存，状态是 cancelled（§11.3）。"""

        class _CancelChain(_FakeChain):
            def invoke(self, _inputs):
                async def gen():
                    yield _token("已经写出来的前半段，")
                    raise asyncio.CancelledError()

                return gen()

        aid = users["alice"]["id"]
        conv, message_id, package = _setup_turn(db, aid, "写一段长文")
        _patch_model(monkeypatch, _CancelChain([]), package)
        frames = _run_chat(db, aid, conv, message_id, "写一段长文", stream=True)
        assert _manifest_frames(frames), "取消路径也要下发使用记录帧"
        meta, response = _meta_and_response(db, message_id)
        assert response == "已经写出来的前半段，"
        assert meta["generation_status"] == "cancelled"
        assert usage_manifest.is_incomplete_metadata(meta) is True

    def test_non_stream_result_carries_the_same_manifest(self, db, users, monkeypatch):
        """非流式最终块带 manifest 与最终状态，与库里那份同源（§18.3.12）。"""
        frames, message_id = _turn(
            db, users, monkeypatch, [_token("一"), _token("二")], stream=False
        )
        result = frames[0]
        # 非流式块的正文在 choices[0].message.content（model_dump 的形状），
        # manifest 与状态放顶层，供非流式消费者直接取
        assert result["choices"][0]["message"]["content"] == "一二"
        assert result["generation_status"] == "completed"
        manifest = result[usage_manifest.MANIFEST_KEY]
        assert manifest["generation_status"] == "completed"
        meta, response = _meta_and_response(db, message_id)
        assert meta[usage_manifest.MANIFEST_KEY] == manifest
        assert response == "一二"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-o", "addopts=", "-p", "no:cacheprovider"])
