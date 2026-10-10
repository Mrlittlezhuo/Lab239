"""两个 `PlatformToolsMessageChunk` 必须能相加。

流式 Agent 每收一个 chunk 就做 `final = final + ichunk`。本类没有 `example`
字段——那段比较是从老 langchain_core 的 `AIMessageChunk` 抄来的，升级到
langchain 1.x 后基类去掉了它，于是流式 Agent 一合并 chunk 就 AttributeError，
整条对话以 `'output'` 这种看不懂的错误收尾。
"""
from langchain_core.messages.tool import ToolCallChunk

from langchain_chatchat.chat_models.platform_tools_message import (
    PlatformToolsMessageChunk,
)


def test_two_text_chunks_merge():
    merged = PlatformToolsMessageChunk(content="你好") + PlatformToolsMessageChunk(
        content="，世界"
    )
    assert isinstance(merged, PlatformToolsMessageChunk)
    assert merged.content == "你好，世界"


def test_merge_keeps_tool_call_chunks():
    first = PlatformToolsMessageChunk(
        content="",
        tool_call_chunks=[
            ToolCallChunk(name="search_internet", args='{"query": "la', index=0, id="c1")
        ],
    )
    second = PlatformToolsMessageChunk(
        content="",
        tool_call_chunks=[ToolCallChunk(name=None, args='ng")', index=0, id=None)],
    )
    merged = first + second
    # 同 index 的分片按 langchain 语义拼成一片，参数拼全后能解析出完整 tool_call
    assert len(merged.tool_call_chunks) == 1
    chunk = merged.tool_call_chunks[0]
    assert chunk["name"] == "search_internet"
    assert chunk["args"] == '{"query": "lang")'
    assert chunk["id"] == "c1"
    assert merged.tool_calls == [
        {"name": "search_internet", "args": {"query": "lang"}, "id": "c1"}
    ]
    assert merged.invalid_tool_calls == []


def test_merge_carries_metadata_and_id():
    first = PlatformToolsMessageChunk(
        content="a",
        additional_kwargs={"k": 1},
        response_metadata={"model_name": "probe"},
        id="msg-1",
    )
    merged = first + PlatformToolsMessageChunk(content="b")
    assert merged.additional_kwargs == {"k": 1}
    assert merged.response_metadata == {"model_name": "probe"}
    assert merged.id == "msg-1"


def test_many_chunks_merge_without_error():
    merged = PlatformToolsMessageChunk(content="")
    for i in range(200):
        merged = merged + PlatformToolsMessageChunk(content=str(i % 10))
    assert merged.content == "".join(str(i % 10) for i in range(200))
