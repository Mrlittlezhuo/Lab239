"""Agent 任务"边生成边出字"的离线测试。

覆盖：

- ``AnswerStreamExtractor``：从流式 JSON 里按分片取出顶层正文字段
  （转义与代理对、任意分片边界、值里的诱骗字段、嵌套同名字段、非字符串
  值、同一字段只出一次、代码围栏前缀、截断时冲刷残留）；
- ``LLMPlanner``：给了 ``on_chunk`` 就走 ``llm.stream()`` 并逐片回调，
  模型实例没有 ``stream`` 时退回 ``invoke``，流式异常仍是可控
  ``PlanError``，重试沿用同一回调；
- ``StreamingPlanner``：没有 ``supports_stream`` 的假规划器原样透传（不
  产生任何预览），生产规划器按阶段推送预览且跨重试不重复同一字段；
- ``_interleave``：预览必须先于节点更新产出（否则等于没有流式），消费
  者停止时关闭上游图流；
- ``service._event_to_sse``：``plan_delta`` / ``answer_delta`` 映射为带
  增量正文的 SSE 块；
- 真实 LangGraph 运行时 + 会流式的假规划器：预览必须先于 ``plan_ready`` /
  ``run_completed`` 产出、拼起来就是最终答案，普通假规划器不产生任何预览，
  运行结束后规划器恢复原样。

全程不连接真实模型、外部服务、MCP 或用户数据库。
"""
import asyncio
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from chatchat.server.db.base import Base
from chatchat.server.db.migrate.base import apply_migrations
from chatchat.server.db.migrate.migrations import build_registry
from chatchat.server.db.models.agent_run_model import AgentRunModel  # noqa: F401
from chatchat.server.db.models.agent_step_model import AgentStepModel  # noqa: F401
from chatchat.server.db.models.conversation_model import ConversationModel  # noqa: F401
from chatchat.server.db.models.message_model import MessageModel  # noqa: F401
from chatchat.server.db.models.user_model import UserModel  # noqa: F401
from chatchat.server.agent_run import schema as S
from chatchat.server.agent_run.answer_stream import AnswerStreamExtractor
from chatchat.server.agent_run.planner import (
    LLMPlanner,
    PLAN_PREVIEW_KEYS,
    StreamingPlanner,
)
from chatchat.server.agent_run.runtime import checkpoint as ckpt_mod
from chatchat.server.agent_run.runtime.langgraph_runtime import (
    LangGraphRuntime,
    _interleave,
)
from chatchat.server.agent_run.service import _event_to_sse

CTX = S.PlanContext(goal="介绍一下长江", max_steps=3)

PLAN_TEXT = '{"summary": "直接回答", "steps": [], "direct_answer": "长江很长。"}'


def _pushes_for(keys, chunks):
    """按分片喂入，返回 (阶段, 文本) 推送记录。"""
    pushes = []
    extractor = AnswerStreamExtractor(keys, lambda phase, text: pushes.append((phase, text)))
    for chunk in chunks:
        extractor.feed(chunk)
    extractor.close()
    return [p for p in pushes if p[1]]


def _text_of(pushes):
    return "".join(text for _, text in pushes)


class TestAnswerStreamExtractor:
    def test_top_level_answer_in_one_chunk(self):
        pushes = _pushes_for({"answer": "answer"}, ['{"answer": "长江是亚洲第一长河。"}'])
        assert pushes == [("answer", "长江是亚洲第一长河。")]

    def test_decodes_escapes_and_surrogate_pairs(self):
        value = '第一行\n第二行 "引号" \\ 斜杠 中文 😀 结束'
        raw = json.dumps({"answer": value})  # ensure_ascii：emoji 变代理对
        assert _text_of(_pushes_for({"answer": "answer"}, [raw])) == value

    def test_survives_arbitrary_chunk_boundaries(self):
        value = 'a"b\\c\nd 中文 😀 end'
        raw = json.dumps({"answer": value})
        whole = _text_of(_pushes_for({"answer": "answer"}, [raw]))
        per_char = _text_of(_pushes_for({"answer": "answer"}, list(raw)))
        assert whole == per_char == value

    def test_ignores_decoy_inside_other_values(self):
        raw = '{"goal": "记得写 \\"answer\\":\\"诱饵\\"", "answer": "真答案"}'
        assert _text_of(_pushes_for({"answer": "answer"}, [raw])) == "真答案"

    def test_ignores_nested_same_key(self):
        raw = '{"meta": {"answer": "嵌套里的"}, "answer": "顶层的"}'
        assert _text_of(_pushes_for({"answer": "answer"}, [raw])) == "顶层的"

    def test_non_string_value_disarms_field(self):
        raw = '{"answer": null, "summary": "计划说明"}'
        pushes = _pushes_for({"answer": "answer", "summary": "plan"}, [raw])
        assert pushes == [("plan", "计划说明")]

    def test_each_field_emits_only_once(self):
        raw = '{"answer": "第一版"} 后面又补 {"answer": "第二版"}'
        assert _text_of(_pushes_for({"answer": "answer"}, [raw])) == "第一版"

    def test_plan_phases_separate_summary_from_direct_answer(self):
        raw = ('{"summary": "查天气", "steps": [{"goal": "查上海天气",'
               ' "tool": "search", "args": {}}], "direct_answer": ""}')
        assert _pushes_for(PLAN_PREVIEW_KEYS, [raw]) == [("plan", "查天气")]

    def test_plan_direct_answer_streams_when_no_steps(self):
        raw = '{"summary": "直接回答", "steps": [], "direct_answer": "无需工具的答案"}'
        assert _pushes_for(PLAN_PREVIEW_KEYS, [raw]) == [
            ("plan", "直接回答"),
            ("answer", "无需工具的答案"),
        ]

    def test_one_push_per_chunk_not_per_char(self):
        chunks = ['{"ans', 'wer": "第一段', '第二段", "x": 1}']
        pushes = _pushes_for({"answer": "answer"}, chunks)
        assert len(pushes) == 2
        assert _text_of(pushes) == "第一段第二段"

    def test_close_flushes_truncated_value(self):
        pushes = _pushes_for({"answer": "answer"}, ['{"answer": "说到一半'])
        assert _text_of(pushes) == "说到一半"

    def test_code_fence_prefix_is_ignored(self):
        raw = '```json\n{"answer": "带围栏"}\n```'
        assert _text_of(_pushes_for({"answer": "answer"}, [raw])) == "带围栏"


class _Chunk:
    def __init__(self, content):
        self.content = content


class _ScriptedStreamLLM:
    """每次 ``stream()`` 按脚本返回一组分片；脚本项可为异常。"""

    def __init__(self, scripts):
        self._scripts = [list(s) for s in scripts]
        self.stream_calls = 0
        self.invoke_calls = 0

    def stream(self, messages):
        self.stream_calls += 1
        for piece in self._scripts.pop(0):
            if isinstance(piece, BaseException):
                raise piece
            yield _Chunk(piece)

    def invoke(self, messages):  # 只用于确认不会被调用
        self.invoke_calls += 1
        raise AssertionError("有 stream 时不应退回 invoke")


class _InvokeOnlyLLM:
    def __init__(self, text):
        self._text = text
        self.invoke_calls = 0

    def invoke(self, messages):
        self.invoke_calls += 1
        return _Chunk(self._text)


class TestLLMPlannerStreaming:
    def test_on_chunk_uses_stream_and_keeps_parsing(self):
        seen = []
        llm = _ScriptedStreamLLM([[PLAN_TEXT[i:i + 7] for i in range(0, len(PLAN_TEXT), 7)]])
        plan = LLMPlanner(llm).plan(CTX, on_chunk=seen.append)
        assert plan.direct_answer == "长江很长。"
        assert "".join(seen) == PLAN_TEXT
        assert llm.stream_calls == 1

    def test_without_stream_attribute_falls_back_to_invoke(self):
        seen = []
        llm = _InvokeOnlyLLM(PLAN_TEXT)
        plan = LLMPlanner(llm).plan(CTX, on_chunk=seen.append)
        assert plan.direct_answer == "长江很长。"
        assert seen == []
        assert llm.invoke_calls == 1

    def test_no_callback_keeps_original_invoke_path(self):
        class _LLM:
            def __init__(self):
                self.stream_calls = 0
                self.invoke_calls = 0

            def stream(self, messages):
                self.stream_calls += 1
                yield _Chunk(PLAN_TEXT)

            def invoke(self, messages):
                self.invoke_calls += 1
                return _Chunk(PLAN_TEXT)

        llm = _LLM()
        plan = LLMPlanner(llm).plan(CTX)
        assert plan.direct_answer == "长江很长。"
        # 不传 on_chunk 就完全保持原来的非流式调用路径
        assert (llm.invoke_calls, llm.stream_calls) == (1, 0)

    def test_stream_failure_stays_controlled_plan_error(self):
        llm = _ScriptedStreamLLM([["部分输出", RuntimeError("连接断了")],
                                  [RuntimeError("还是断了")]])
        with pytest.raises(S.PlanError) as exc:
            LLMPlanner(llm).plan(CTX, on_chunk=lambda text: None)
        assert "模型调用失败" in str(exc.value)
        assert llm.stream_calls == 2  # 计划无效仍只重试一次

    def test_retry_reuses_the_same_callback(self):
        llm = _ScriptedStreamLLM([
            ['{"summary": "只有摘要"}'],
            ['{"summary": "重试后", "steps": [], "direct_answer": "补好的答案"}'],
        ])
        seen = []
        plan = LLMPlanner(llm).plan(CTX, on_chunk=seen.append)
        assert plan.direct_answer == "补好的答案"
        assert llm.stream_calls == 2
        assert "补好的答案" in "".join(seen)


class TestStreamingPlanner:
    def test_fake_planner_is_passed_through_untouched(self):
        calls = []

        class _FakePlanner:
            marker = "属性透传"

            def plan(self, ctx):
                calls.append("plan")
                return "计划对象"

            def decide(self, ctx, results):
                calls.append("decide")
                return "判断对象"

        planner = StreamingPlanner(
            _FakePlanner(),
            lambda phase, text: pytest.fail("假规划器不应产生预览"),
        )
        assert planner.plan(CTX) == "计划对象"
        assert planner.decide(CTX, []) == "判断对象"
        assert planner.marker == "属性透传"
        assert calls == ["plan", "decide"]

    def test_produces_phase_preview_for_production_planner(self):
        pushes = []
        llm = _ScriptedStreamLLM([[PLAN_TEXT[i:i + 5] for i in range(0, len(PLAN_TEXT), 5)]])
        planner = StreamingPlanner(LLMPlanner(llm), lambda phase, text: pushes.append((phase, text)))
        plan = planner.plan(CTX)
        assert plan.direct_answer == "长江很长。"
        assert _push_text(pushes) == {
            "plan": "直接回答",
            "answer": "长江很长。",
        }

    def test_retry_does_not_repeat_previewed_field(self):
        pushes = []
        llm = _ScriptedStreamLLM([
            ['{"summary": "只有摘要"}'],
            ['{"summary": "重试后的摘要", "steps": [], "direct_answer": "补好的答案"}'],
        ])
        planner = StreamingPlanner(LLMPlanner(llm), lambda phase, text: pushes.append((phase, text)))
        plan = planner.plan(CTX)
        assert plan.direct_answer == "补好的答案"
        # summary 已在第一次输出里预览过，不再重复；direct_answer 第二次才出
        assert [(p, t) for p, t in pushes] == [
            ("plan", "只有摘要"),
            ("answer", "补好的答案"),
        ]


def _push_text(pushes):
    merged: dict = {}
    for phase, text in pushes:
        merged[phase] = merged.get(phase, "") + text
    return merged


def _feed_after(queue, delay, item):
    async def _run():
        await asyncio.sleep(delay)
        queue.put_nowait(item)

    return asyncio.ensure_future(_run())


class TestInterleave:
    async def test_preview_precedes_the_node_update(self):
        queue = asyncio.Queue()

        async def updates():
            await asyncio.sleep(0.2)
            yield {"prepare_plan": {"plan": {"summary": "s"}}}

        gen = _interleave(updates(), queue)
        feeder = _feed_after(queue, 0.05, ("answer", "第一段"))
        try:
            assert await gen.__anext__() == ("preview", ("answer", "第一段"))
            assert (await gen.__anext__())[0] == "graph"
        finally:
            await feeder
            await gen.aclose()

    async def test_multiple_previews_come_out_in_order(self):
        queue = asyncio.Queue()

        async def updates():
            await asyncio.sleep(0.3)
            yield {"decide": {"status": "completed"}}

        gen = _interleave(updates(), queue)
        feeder = _feed_after(queue, 0.02, ("answer", "一"))
        feeder2 = _feed_after(queue, 0.06, ("answer", "二"))
        feeder3 = _feed_after(queue, 0.10, ("answer", "三"))
        seen = []
        try:
            for _ in range(4):
                kind, payload = await gen.__anext__()
                seen.append(payload[1] if kind == "preview" else kind)
            assert seen == ["一", "二", "三", "graph"]
        finally:
            await asyncio.gather(feeder, feeder2, feeder3)
            await gen.aclose()

    async def test_ends_when_graph_ends(self):
        async def updates():
            yield {"prepare_plan": {}}
            yield {"decide": {}}

        gen = _interleave(updates(), asyncio.Queue())
        kinds = [kind async for kind, _ in gen]
        assert kinds == ["graph", "graph"]

    async def test_closes_upstream_when_consumer_stops(self):
        closed = False

        async def updates():
            nonlocal closed
            try:
                yield {"prepare_plan": {}}
                yield {"decide": {}}
            finally:
                closed = True

        gen = _interleave(updates(), asyncio.Queue())
        assert (await gen.__anext__())[0] == "graph"
        await gen.aclose()
        assert closed


class TestDeltaSseFrames:
    @pytest.mark.parametrize("event_type", ["plan_delta", "answer_delta"])
    def test_delta_event_carries_increment(self, event_type):
        frame = json.loads(
            _event_to_sse(
                {"type": event_type, "run_id": "run-1", "text": "长江"},
                "msg-1",
                "test-model",
            )
        )
        assert frame["class_name"] == "AgentRunEvent"
        assert frame["agent_event"] == event_type
        assert frame["choices"][0]["delta"]["content"] == "长江"

    def test_structured_events_still_carry_no_preview_text(self):
        frame = json.loads(
            _event_to_sse({"type": "step_started", "run_id": "run-1",
                           "step": {"seq": 1}}, "msg-1", "test-model")
        )
        assert frame["choices"][0]["delta"]["content"] == ""


# ---------------------------------------------------------------------------
# 离线端到端：真实 LangGraph 运行时 + 会流式的假规划器（临时库 + 临时
# checkpoint，不连真实模型/工具）
# ---------------------------------------------------------------------------


class _StreamingFakePlanner:
    """把脚本 JSON 按分片交给 ``on_chunk``，再返回结构化结果。"""

    supports_stream = True

    def __init__(self, plan_json, decision_json):
        self._plan_json = plan_json
        self._decision_json = decision_json

    def _feed(self, raw, on_chunk):
        if on_chunk is None:
            return
        for i in range(0, len(raw), 6):
            on_chunk(raw[i:i + 6])

    def plan(self, ctx, on_chunk=None):
        self._feed(self._plan_json, on_chunk)
        return S.parse_plan(json.loads(self._plan_json), set(ctx.tools), ctx.max_steps)

    def decide(self, ctx, results, on_chunk=None):
        self._feed(self._decision_json, on_chunk)
        return S.parse_decision(json.loads(self._decision_json), set(ctx.tools), 1)


class _PlainFakePlanner:
    """没有 ``supports_stream`` 的假规划器：行为必须与改动前完全一致。"""

    def plan(self, ctx):
        return S.Plan(summary="直接回答", steps=[], direct_answer="无需工具的答案")

    def decide(self, ctx, results):
        return S.Decision(action="answer", answer="不该走到判断这一步")


class _FakeTool:
    def __init__(self, name, result):
        self.name = name
        self.description = "mock tool"
        self.args = {}
        self.result = result
        self.calls = []

    def invoke(self, args):
        self.calls.append(dict(args or {}))
        return self.result


@pytest.fixture
def temp_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'answer_preview.db'}")
    with engine.connect() as conn:
        apply_migrations(build_registry(), conn)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)
    owner_id = "owner-preview"
    row = session()
    try:
        row.add(UserModel(id=owner_id, username="preview", display_name="preview",
                          password_hash="x", role="user", status="active",
                          auth_version=1))
        row.commit()
    finally:
        row.close()
    yield {"session": session, "owner_id": owner_id}
    engine.dispose()


@pytest.fixture
async def ckpt(tmp_path):
    store = await ckpt_mod.initialize_agent_checkpoint_store(
        str(tmp_path / "answer_preview_ckpt.sqlite")
    )
    yield store
    await ckpt_mod.shutdown_agent_checkpoint_store()


def _runtime(db, store, planner, tools, goal):
    return LangGraphRuntime(
        db["session"],
        db["owner_id"],
        planner,
        tools,
        goal,
        max_steps=3,
        timeout_seconds=30,
        checkpointer=store,
    )


class TestRuntimePreviewEvents:
    async def test_answer_preview_streams_before_completion(self, temp_db, ckpt):
        answer = "根据检索结果，长江是亚洲第一长河。"
        planner = _StreamingFakePlanner(
            json.dumps({
                "summary": "检索知识库后回答",
                "steps": [{"goal": "检索长江资料", "tool": "kb",
                           "args": {"query": "长江"}}],
                "direct_answer": "",
            }),
            json.dumps({"action": "answer", "answer": answer}),
        )
        tool = _FakeTool("kb", "观察：长江，亚洲第一长河")
        rt = _runtime(temp_db, ckpt, planner, {"kb": tool}, "介绍一下长江")
        events = [ev async for ev in rt.run()]
        types = [ev["type"] for ev in events]

        assert "plan_delta" in types and "answer_delta" in types
        # 预览必须早于对应的结构化事件，否则等于没有流式
        assert types.index("plan_delta") < types.index("plan_ready")
        assert types.index("answer_delta") < types.index("run_completed")
        assert "".join(
            ev["text"] for ev in events if ev["type"] == "answer_delta"
        ) == answer
        assert rt.status == "completed"
        assert rt.final_result == answer
        assert tool.calls == [{"query": "长江"}]
        # 预览旁路只在执行图期间挂着：运行结束后规划器恢复原样
        assert rt.planner is planner

    async def test_plain_planner_run_emits_no_preview(self, temp_db, ckpt):
        planner = _PlainFakePlanner()
        rt = _runtime(temp_db, ckpt, planner, {}, "解释一下常识")
        types = [ev["type"] async for ev in rt.run()]
        assert "plan_delta" not in types and "answer_delta" not in types
        assert rt.status == "completed"
        assert rt.final_result == "无需工具的答案"
        assert rt.planner is planner
