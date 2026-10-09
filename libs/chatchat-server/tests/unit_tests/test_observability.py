"""Phoenix 本地可观测性离线测试（任务 016）。

全部使用内存 span exporter（opentelemetry-sdk InMemorySpanExporter）、
假 register / 假路由 / 假模型 / 假工具，不连接真实 Phoenix、LLM、
FastJev、MCP、知识库、外部网络或用户数据。

覆盖任务文档 §8 要求：
- 关闭时 no-op：不注册、无导出、不改变对话结果；
- 启用时按配置注册 endpoint / project name；
- 重复初始化不重复注册 / 不重复埋点；
- register 抛异常时安全降级，对话路径仍可用；
- 直接回答 / 知识库 / 单工具 / 多步 Agent 分支生成含关键属性的 Trace；
- 路由回退与 Agent 失败状态可标记，不导致第二次执行；
- 非流式 span 在返回结果后结束；
- 流式 span 在迭代器消费完成 / 生成异常 / 取消时才结束；
- 内容捕获开关正确设置 OpenInference 隐藏环境变量。
"""
import json
import sys
import types

import pytest

# 与其余对话测试一致：打桩 kb_chat，避免模块加载时读取真实知识库
if "chatchat.server.chat.kb_chat" not in sys.modules:
    _kb_stub = types.ModuleType("chatchat.server.chat.kb_chat")
    _kb_stub.kb_chat = lambda *args, **kwargs: None
    sys.modules["chatchat.server.chat.kb_chat"] = _kb_stub

from sse_starlette import EventSourceResponse

from chatchat.settings import Settings
import chatchat.server.agent_run.service as agent_service
import chatchat.server.api_server.chat_routes as chat_routes_mod
import chatchat.server.intent_router as intent_router_pkg
import chatchat.server.observability.tracing as tracing_mod
from chatchat.server.observability import (
    init_observability,
    is_observability_enabled,
    shutdown_observability,
)
from chatchat.server.intent_router import (
    INTENT_GENERAL,
    INTENT_KB_SEARCH,
    INTENT_SINGLE_TOOL,
    INTENT_COMPLEX,
    PATH_AGENT_RUN,
    PATH_DIRECT,
    RouteResult,
)
from chatchat.server.intent_router.schemas import (
    COMPLEXITY_DIRECT,
    COMPLEXITY_MULTI,
    COMPLEXITY_SINGLE,
)
from chatchat.server.smart_conversation import run_smart_conversation

# ---------------------------------------------------------------------------
# 内存 TracerProvider：进程内只设置一次全局 provider，每个测试挂自己的
# InMemorySpanProcessor（OTel 不允许覆盖已设置的全局 provider）。
# ---------------------------------------------------------------------------

from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import StatusCode

_provider = TracerProvider()
_provider_set = False


@pytest.fixture
def memory_spans():
    """返回 (exporter, spans_by_name)；span 在测试结束时可读取。"""
    global _provider_set
    if not _provider_set:
        otel_trace.set_tracer_provider(_provider)
        _provider_set = True
    exporter = InMemorySpanExporter()
    _provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield exporter
    exporter.shutdown()


@pytest.fixture(autouse=True)
def _reset_observability_state(monkeypatch):
    """隔离 observability 模块全局状态与 OpenInference 环境变量。"""
    monkeypatch.setattr(tracing_mod, "_initialized", False, raising=False)
    monkeypatch.setattr(tracing_mod, "_instrumentor", None, raising=False)
    monkeypatch.setattr(
        Settings.basic_settings, "TRACE_ENABLED", False, raising=True
    )
    for var in (
        "OPENINFERENCE_HIDE_INPUTS",
        "OPENINFERENCE_HIDE_OUTPUTS",
        "OPENINFERENCE_HIDE_RETRIEVAL_DOCUMENTS",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


def _find_span(exporter, name):
    for span in exporter.get_finished_spans():
        if span.name == name:
            return span
    return None


# ---------------------------------------------------------------------------
# 1. 关闭 / 启用 / 幂等 / 降级
# ---------------------------------------------------------------------------


class TestInitLifecycle:
    def test_disabled_is_noop(self, monkeypatch):
        calls = []

        def fake_register(**kwargs):
            calls.append(kwargs)

        import phoenix.otel as phx_otel

        monkeypatch.setattr(phx_otel, "register", fake_register)
        assert Settings.basic_settings.TRACE_ENABLED is False
        assert init_observability() is False
        assert is_observability_enabled() is False
        assert calls == []
        # 未配置全局 provider 时手工 span 为 NonRecordingSpan
        span = tracing_mod.get_tracer().start_span("probe")
        assert span.is_recording() is False

    def test_enabled_registers_endpoint_and_project(self, monkeypatch):
        calls = []

        def fake_register(**kwargs):
            calls.append(kwargs)

        import phoenix.otel as phx_otel

        monkeypatch.setattr(phx_otel, "register", fake_register)
        monkeypatch.setattr(Settings.basic_settings, "TRACE_ENABLED", True)
        monkeypatch.setattr(
            Settings.basic_settings,
            "TRACE_PHOENIX_ENDPOINT",
            "http://127.0.0.1:6123",
        )
        monkeypatch.setattr(
            Settings.basic_settings, "TRACE_PROJECT_NAME", "chatchat-test"
        )
        assert init_observability() is True
        assert len(calls) == 1
        # 只写 host:port 时自动补 OTLP HTTP 路径 /v1/traces
        assert calls[0]["endpoint"] == "http://127.0.0.1:6123/v1/traces"
        assert calls[0]["project_name"] == "chatchat-test"
        assert calls[0]["auto_instrument"] is False
        shutdown_observability()
        assert is_observability_enabled() is False

    def test_endpoint_with_explicit_path_preserved(self, monkeypatch):
        calls = []

        def fake_register(**kwargs):
            calls.append(kwargs)

        import phoenix.otel as phx_otel

        monkeypatch.setattr(phx_otel, "register", fake_register)
        monkeypatch.setattr(Settings.basic_settings, "TRACE_ENABLED", True)
        monkeypatch.setattr(
            Settings.basic_settings,
            "TRACE_PHOENIX_ENDPOINT",
            "http://127.0.0.1:4318/v1/traces",
        )
        assert init_observability() is True
        # 已带路径的 endpoint 原样使用，不重复追加
        assert calls[0]["endpoint"] == "http://127.0.0.1:4318/v1/traces"
        shutdown_observability()

    def test_repeat_init_registers_once(self, monkeypatch):
        register_calls = []
        instrument_calls = []

        def fake_register(**kwargs):
            register_calls.append(kwargs)

        class FakeInstrumentor:
            _is_instrumented_by_opentelemetry = False

            def instrument(self, *a, **k):
                instrument_calls.append(1)
                self._is_instrumented_by_opentelemetry = True

            def uninstrument(self, *a, **k):
                self._is_instrumented_by_opentelemetry = False

        import phoenix.otel as phx_otel
        import openinference.instrumentation.langchain as oi_lc

        monkeypatch.setattr(phx_otel, "register", fake_register)
        monkeypatch.setattr(oi_lc, "LangChainInstrumentor", FakeInstrumentor)
        monkeypatch.setattr(Settings.basic_settings, "TRACE_ENABLED", True)
        assert init_observability() is True
        assert init_observability() is True
        assert init_observability() is True
        assert len(register_calls) == 1
        assert len(instrument_calls) == 1
        shutdown_observability()

    def test_register_failure_degrades_safely(self, monkeypatch):
        def boom(**kwargs):
            raise RuntimeError("phoenix 不可达")

        import phoenix.otel as phx_otel

        monkeypatch.setattr(phx_otel, "register", boom)
        monkeypatch.setattr(Settings.basic_settings, "TRACE_ENABLED", True)
        # 初始化失败：返回 False、不抛出、不启用
        assert init_observability() is False
        assert is_observability_enabled() is False

    def test_content_capture_policy(self, monkeypatch):
        import phoenix.otel as phx_otel

        monkeypatch.setattr(phx_otel, "register", lambda **k: None)
        monkeypatch.setattr(Settings.basic_settings, "TRACE_ENABLED", True)
        # 默认关闭内容捕获 → 隐藏输入/输出/检索文档
        init_observability()
        assert (
            __import__("os").environ["OPENINFERENCE_HIDE_INPUTS"] == "true"
        )
        assert (
            __import__("os").environ["OPENINFERENCE_HIDE_OUTPUTS"] == "true"
        )
        assert (
            __import__("os").environ["OPENINFERENCE_HIDE_RETRIEVAL_DOCUMENTS"]
            == "true"
        )
        shutdown_observability()
        # 显式开启内容捕获
        tracing_mod._initialized = False
        monkeypatch.setattr(
            Settings.basic_settings, "TRACE_CAPTURE_CONTENT", True
        )
        init_observability()
        assert (
            __import__("os").environ["OPENINFERENCE_HIDE_INPUTS"] == "false"
        )
        shutdown_observability()


# ---------------------------------------------------------------------------
# 2. 智能对话分支 Trace（直接调用 run_smart_conversation，无 HTTP/DB）
# ---------------------------------------------------------------------------


def _patch_fakes(monkeypatch, route, *, chat_result=None, agent_result=None,
                 chat_stream=None, agent_stream=None):
    """把总图依赖替换为假路由 / 假聊天 / 假 Agent（不触真实服务）。"""
    monkeypatch.setattr(
        intent_router_pkg,
        "route_auto_agent",
        lambda q, **k: route,
    )
    tools = {
        "search_local_knowledgebase": object(),
        "weather_check": object(),
        "calculate": object(),
    }
    monkeypatch.setattr(
        chat_routes_mod, "get_tool", lambda name=None: tools if name is None else tools.get(name)
    )
    monkeypatch.setattr(chat_routes_mod, "get_tool_config", lambda name: {})

    async def fake_chat(**kwargs):
        if chat_stream is not None:
            return EventSourceResponse(chat_stream())
        return dict(chat_result or {"object": "chat.completion", "content": "直接回答"})

    async def fake_agent_run_chat(**kwargs):
        if agent_stream is not None:
            return EventSourceResponse(agent_stream())
        return dict(agent_result or {"agent_run": {"run_id": "run-1", "status": "completed"}})

    monkeypatch.setattr(chat_routes_mod, "chat", fake_chat)
    monkeypatch.setattr(agent_service, "agent_run_chat", fake_agent_run_chat)


async def _consume(response):
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk)
    return chunks


class TestBranchTraces:
    async def test_direct_answer_trace(self, monkeypatch, memory_spans):
        route = RouteResult(
            path=PATH_DIRECT,
            intent=INTENT_GENERAL,
            complexity=COMPLEXITY_DIRECT,
            candidate_tools=[],
            confidence=0.9,
            summary="普通对话",
            provider="rule",
            source="rule",
        )
        _patch_fakes(monkeypatch, route)
        result = await run_smart_conversation(
            query="你好",
            conversation_id="conv-1",
            message_id="msg-1",
            session=None,
            owner_id="user-1",
            stream=False,
            model_name="fake-llm",
            available_tools={
                "search_local_knowledgebase": object(),
                "weather_check": object(),
                "calculate": object(),
            },
        )
        assert result["execution_mode"] == "direct_answer"
        root = _find_span(memory_spans, "smart_conversation")
        route_span = _find_span(memory_spans, "intent_route")
        assert root is not None and route_span is not None
        assert root.attributes["chatchat.conversation_id"] == "conv-1"
        assert root.attributes["chatchat.message_id"] == "msg-1"
        assert root.attributes["chatchat.user_id"] == "user-1"
        assert root.attributes["chatchat.execution_mode"] == "direct_answer"
        assert root.attributes["chatchat.route.intent"] == INTENT_GENERAL
        assert route_span.attributes["chatchat.execution_mode"] == "direct_answer"
        # 非流式：返回结果后 span 已结束（出现在 finished spans）
        assert root.status.status_code is StatusCode.OK

    async def test_kb_branch_run_id_and_single_step_tools(
        self, monkeypatch, memory_spans
    ):
        route = RouteResult(
            path=PATH_AGENT_RUN,
            intent=INTENT_KB_SEARCH,
            complexity=COMPLEXITY_SINGLE,
            candidate_tools=["search_local_knowledgebase"],
            confidence=0.95,
            summary="知识库检索",
            provider="llm",
            source="llm",
        )
        _patch_fakes(
            monkeypatch,
            route,
            agent_result={
                "agent_run": {"run_id": "run-kb-9", "status": "completed"},
                "content": "知识库回答",
            },
        )
        result = await run_smart_conversation(
            query="Mem0 是什么",
            conversation_id="conv-2",
            message_id="msg-2",
            session=None,
            owner_id="user-1",
            stream=False,
            model_name="fake-llm",
            available_tools={
                "search_local_knowledgebase": object(),
                "weather_check": object(),
                "calculate": object(),
            },
        )
        assert result["execution_mode"] == "knowledge_task"
        root = _find_span(memory_spans, "smart_conversation")
        assert root.attributes["chatchat.execution_mode"] == "knowledge_task"
        assert root.attributes["chatchat.agent_run_id"] == "run-kb-9"
        assert root.attributes["chatchat.agent_run_status"] == "completed"
        assert list(root.attributes["chatchat.route.candidate_tools"]) == [
            "search_local_knowledgebase"
        ]

    async def test_single_tool_and_complex_branch_modes(
        self, monkeypatch, memory_spans
    ):
        for route, expected_mode in (
            (
                RouteResult(
                    path=PATH_AGENT_RUN,
                    intent=INTENT_SINGLE_TOOL,
                    complexity=COMPLEXITY_SINGLE,
                    candidate_tools=["weather_check"],
                    confidence=0.9,
                    summary="单工具",
                    provider="llm",
                    source="llm",
                ),
                "single_tool_task",
            ),
            (
                RouteResult(
                    path=PATH_AGENT_RUN,
                    intent=INTENT_COMPLEX,
                    complexity=COMPLEXITY_MULTI,
                    candidate_tools=["search_local_knowledgebase", "calculate"],
                    confidence=0.9,
                    summary="复杂任务",
                    provider="llm",
                    source="llm",
                ),
                "multi_step_agent",
            ),
        ):
            _patch_fakes(monkeypatch, route)
            result = await run_smart_conversation(
                query="任务",
                conversation_id="conv-x",
                message_id="msg-x",
                session=None,
                owner_id="user-1",
                stream=False,
                model_name="fake-llm",
                available_tools={
                "search_local_knowledgebase": object(),
                "weather_check": object(),
                "calculate": object(),
            },
            )
            assert result["execution_mode"] == expected_mode
        names = [
            s.attributes["chatchat.execution_mode"]
            for s in memory_spans.get_finished_spans()
            if s.name == "smart_conversation"
        ]
        assert names == ["single_tool_task", "multi_step_agent"]

    async def test_route_fallback_marked_on_trace(self, monkeypatch, memory_spans):
        """未知候选工具 → 回退直接回答；回退原因与来源在 span 上可见。"""
        route = RouteResult(
            path=PATH_AGENT_RUN,
            intent=INTENT_SINGLE_TOOL,
            complexity=COMPLEXITY_SINGLE,
            candidate_tools=["ghost_tool"],
            confidence=0.9,
            summary="未知工具",
            provider="llm",
            source="llm",
        )
        _patch_fakes(monkeypatch, route)
        result = await run_smart_conversation(
            query="用神秘工具做事",
            conversation_id="conv-3",
            message_id="msg-3",
            session=None,
            owner_id="user-1",
            stream=False,
            model_name="fake-llm",
            available_tools={
                "search_local_knowledgebase": object(),
                "weather_check": object(),
                "calculate": object(),
            },
        )
        assert result["execution_mode"] == "direct_answer"
        assert result["route_result"]["source"] == "fallback"
        route_span = _find_span(memory_spans, "intent_route")
        assert route_span.attributes["chatchat.route.source"] == "fallback"
        assert "无可用候选工具" in route_span.attributes[
            "chatchat.route.fallback_reason"
        ]
        root = _find_span(memory_spans, "smart_conversation")
        assert root.attributes["chatchat.execution_mode"] == "direct_answer"
        # 只执行了一条路径：仅一个根 span，且没有 Agent run 属性
        roots = [
            s for s in memory_spans.get_finished_spans()
            if s.name == "smart_conversation"
        ]
        assert len(roots) == 1
        assert "chatchat.agent_run_id" not in root.attributes

    async def test_agent_failure_status_visible(self, monkeypatch, memory_spans):
        """Agent 明确失败：状态记录在 span 上，但不抛异常、不二次执行。"""
        route = RouteResult(
            path=PATH_AGENT_RUN,
            intent=INTENT_KB_SEARCH,
            complexity=COMPLEXITY_SINGLE,
            candidate_tools=["search_local_knowledgebase"],
            confidence=0.95,
            summary="知识库检索",
            provider="llm",
            source="llm",
        )
        _patch_fakes(
            monkeypatch,
            route,
            agent_result={
                "agent_run": {"run_id": "run-fail-1", "status": "failed"},
                "content": "执行失败",
            },
        )
        result = await run_smart_conversation(
            query="知识库问题",
            conversation_id="conv-4",
            message_id="msg-4",
            session=None,
            owner_id="user-1",
            stream=False,
            model_name="fake-llm",
            available_tools={
                "search_local_knowledgebase": object(),
                "weather_check": object(),
                "calculate": object(),
            },
        )
        root = _find_span(memory_spans, "smart_conversation")
        assert root.attributes["chatchat.agent_run_status"] == "failed"
        # 业务失败不是异常：span 状态仍为 OK（错误语义在 agent_run_status）
        assert root.status.status_code is StatusCode.OK


# ---------------------------------------------------------------------------
# 3. 流式 span 生命周期
# ---------------------------------------------------------------------------


class TestStreamingSpanLifecycle:
    async def test_stream_span_ends_after_full_consumption(
        self, monkeypatch, memory_spans
    ):
        route = RouteResult(
            path=PATH_DIRECT,
            intent=INTENT_GENERAL,
            complexity=COMPLEXITY_DIRECT,
            candidate_tools=[],
            confidence=0.9,
            summary="普通对话",
            provider="rule",
            source="rule",
        )

        async def stream_body():
            yield "data: " + json.dumps({"content": "第一段"}) + "\n\n"
            yield "data: " + json.dumps({"content": "第二段"}) + "\n\n"

        _patch_fakes(monkeypatch, route, chat_stream=stream_body)
        response = await run_smart_conversation(
            query="你好",
            conversation_id="conv-s1",
            message_id="msg-s1",
            session=None,
            owner_id="user-1",
            stream=True,
            model_name="fake-llm",
            available_tools={
                "search_local_knowledgebase": object(),
                "weather_check": object(),
                "calculate": object(),
            },
        )
        # 响应对象已返回，但流未消费：根 span 尚未结束
        assert _find_span(memory_spans, "smart_conversation") is None
        events = []
        async for chunk in response.body_iterator:
            events.append(chunk)
        assert len(events) == 3  # 路由事件 + 两段内容
        root = _find_span(memory_spans, "smart_conversation")
        assert root is not None
        assert root.status.status_code is StatusCode.OK
        first = json.loads(events[0].replace("data:", "").strip())
        assert first["class_name"] == "RouteResult"
        assert first["execution_mode"] == "direct_answer"

    async def test_stream_run_id_captured_from_agent_events(
        self, monkeypatch, memory_spans
    ):
        route = RouteResult(
            path=PATH_AGENT_RUN,
            intent=INTENT_KB_SEARCH,
            complexity=COMPLEXITY_SINGLE,
            candidate_tools=["search_local_knowledgebase"],
            confidence=0.95,
            summary="知识库检索",
            provider="llm",
            source="llm",
        )

        async def agent_body():
            yield "data: " + json.dumps(
                {"agent_event": "run_created", "run_id": "run-stream-7"}
            ) + "\n\n"
            yield "data: " + json.dumps(
                {
                    "agent_event": "run_completed",
                    "run_id": "run-stream-7",
                    "status": "completed",
                }
            ) + "\n\n"

        _patch_fakes(monkeypatch, route, agent_stream=agent_body)
        response = await run_smart_conversation(
            query="知识库问题",
            conversation_id="conv-s2",
            message_id="msg-s2",
            session=None,
            owner_id="user-1",
            stream=True,
            model_name="fake-llm",
            available_tools={
                "search_local_knowledgebase": object(),
                "weather_check": object(),
                "calculate": object(),
            },
        )
        await _consume(response)
        root = _find_span(memory_spans, "smart_conversation")
        assert root.attributes["chatchat.agent_run_id"] == "run-stream-7"
        assert root.attributes["chatchat.agent_run_status"] == "completed"

    async def test_stream_generation_error_marks_span(
        self, monkeypatch, memory_spans
    ):
        route = RouteResult(
            path=PATH_DIRECT,
            intent=INTENT_GENERAL,
            complexity=COMPLEXITY_DIRECT,
            candidate_tools=[],
            confidence=0.9,
            summary="普通对话",
            provider="rule",
            source="rule",
        )

        async def broken_body():
            yield "data: " + json.dumps({"content": "第一段"}) + "\n\n"
            raise RuntimeError("生成中断")

        _patch_fakes(monkeypatch, route, chat_stream=broken_body)
        response = await run_smart_conversation(
            query="你好",
            conversation_id="conv-s3",
            message_id="msg-s3",
            session=None,
            owner_id="user-1",
            stream=True,
            model_name="fake-llm",
            available_tools={
                "search_local_knowledgebase": object(),
                "weather_check": object(),
                "calculate": object(),
            },
        )
        with pytest.raises(RuntimeError):
            await _consume(response)
        root = _find_span(memory_spans, "smart_conversation")
        assert root is not None
        assert root.status.status_code is StatusCode.ERROR
        assert root.events  # 记录了异常事件

    async def test_stream_cancel_ends_span_with_error(
        self, monkeypatch, memory_spans
    ):
        route = RouteResult(
            path=PATH_DIRECT,
            intent=INTENT_GENERAL,
            complexity=COMPLEXITY_DIRECT,
            candidate_tools=[],
            confidence=0.9,
            summary="普通对话",
            provider="rule",
            source="rule",
        )

        async def slow_body():
            yield "data: " + json.dumps({"content": "第一段"}) + "\n\n"
            yield "data: " + json.dumps({"content": "第二段"}) + "\n\n"

        _patch_fakes(monkeypatch, route, chat_stream=slow_body)
        response = await run_smart_conversation(
            query="你好",
            conversation_id="conv-s4",
            message_id="msg-s4",
            session=None,
            owner_id="user-1",
            stream=True,
            model_name="fake-llm",
            available_tools={
                "search_local_knowledgebase": object(),
                "weather_check": object(),
                "calculate": object(),
            },
        )
        agen = response.body_iterator
        # 只消费首段后取消（模拟客户端断开）
        await agen.__anext__()
        await agen.aclose()
        root = _find_span(memory_spans, "smart_conversation")
        assert root is not None
        assert root.status.status_code is StatusCode.ERROR
        assert "cancel" in (root.status.description or "").lower()
