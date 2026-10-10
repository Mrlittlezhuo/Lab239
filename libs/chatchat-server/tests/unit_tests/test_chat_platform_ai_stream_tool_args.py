"""流式工具参数解析失败不能掀翻整条流。

`stream()` / `astream()` 里那段 `_paser_chunk` 只是为了把工具参数中的 `input`
提前当 token 推给回调（UI 边流边显示）。工具参数是分片流式过来的，中途经常不是
合法 JSON：同步版当时根本没 try/except（JSONDecodeError 直接冒出），异步版对解析
结果不是 dict 的情况抛 `ValueError("Malformed args.")`，两者都会让对话当场失败。
"""
from langchain_core.messages import HumanMessage
from langchain_core.messages.tool import ToolCallChunk
from langchain_core.outputs import ChatGenerationChunk

from langchain_chatchat.chat_models import ChatPlatformAI
from langchain_chatchat.chat_models.platform_tools_message import (
    PlatformToolsMessageChunk,
)

# 半截 JSON、空串、非对象 JSON：都是流式工具参数的常见中间态
MESSY_ARGS = ["", '{"qu', 'not json at all', "[1, 2]", '{"input": "北京"}']


def _model(monkeypatch, chunks):
    model = ChatPlatformAI(
        model="probe-llm", api_key="probe-key", api_base="http://127.0.0.1:9/v1"
    )
    monkeypatch.setattr(model, "_stream", lambda *a, **kw: iter(chunks))

    async def _fake_astream(*a, **kw):
        for chunk in chunks:
            yield chunk

    monkeypatch.setattr(model, "_astream", _fake_astream)
    return model


def _chunks():
    out = []
    for args in MESSY_ARGS:
        message = PlatformToolsMessageChunk(
            content="",
            tool_call_chunks=[
                ToolCallChunk(name="search_internet", args=args, index=0, id="c1")
            ],
        )
        out.append(ChatGenerationChunk(message=message))
    return out


def test_sync_stream_survives_unparsable_tool_arg_chunks(monkeypatch):
    model = _model(monkeypatch, _chunks())
    messages = list(model.stream([HumanMessage(content="查一下")]))
    assert len(messages) == len(MESSY_ARGS)


async def test_async_stream_survives_unparsable_tool_arg_chunks(monkeypatch):
    model = _model(monkeypatch, _chunks())
    messages = [chunk async for chunk in model.astream([HumanMessage(content="查一下")])]
    assert len(messages) == len(MESSY_ARGS)


def test_valid_input_arg_is_still_parsed(monkeypatch):
    """跳过坏帧不能把好的也吞掉：最后一帧 `{"input": "北京"}` 要能解析成 tool_call。"""
    model = _model(monkeypatch, _chunks())
    messages = list(model.stream([HumanMessage(content="查一下")]))
    last = messages[-1]
    assert [c["args"] for c in last.tool_call_chunks] == ['{"input": "北京"}']
    assert last.tool_calls == [
        {"name": "search_internet", "args": {"input": "北京"}, "id": "c1"}
    ]
