"""MCP 工具桥接层的回归测试。

这四组坑都是在真实 server 上撞到的，不是假想：
- `@playwright/mcp` 的 browser_emulate_media 用 anyOf，老代码 `details['type']` 直接
  KeyError，导致整个 server 一个工具都加载不出来；
- 工具声明 response_format="content_and_artifact" 却返回单值，每次调用都被
  LangChain 的校验拒掉；
- 可选字段默认 None 被原样发给 server，playwright 的 schema 回
  `expected string, received null`；
- server 反过来发 `roots/list`，客户端不答（SDK 默认回 ErrorData）时
  playwright-mcp 会一直卡到超时。

全部离线：不起 server、不调模型、不碰数据库。
"""
from typing import Any
from unittest.mock import AsyncMock

import pytest
from mcp.types import CallToolResult, ImageContent, ListRootsResult, TextContent, Tool

from langchain_chatchat.agent_toolkits.mcp_kit.client import _roots_callback
from langchain_chatchat.agent_toolkits.mcp_kit.tools import (
    _convert_call_tool_result,
    _resolve_field_type,
    convert_mcp_tool_to_langchain_tool,
    schema_dict_to_model,
)


# ---------------------------------------------------------------- schema 转换


@pytest.mark.parametrize(
    "details,expected",
    [
        ({"type": "string"}, str),
        ({"type": "integer"}, int),
        ({"type": "number"}, float),
        ({"type": "boolean"}, bool),
        # 只给 anyOf，不给 type（playwright-mcp browser_emulate_media 的真实形状）
        ({"anyOf": [{"type": "null"}, {"type": "string"}]}, str),
        # type 写成数组形式
        ({"type": ["null", "integer"]}, int),
        # 只给 enum
        ({"enum": ["light", "dark"]}, str),
        ({"anyOf": [{"enum": ["no-preference", "reduce"]}]}, str),
        # 什么类型信息都没有：绝不抛异常，退化成 Any
        ({"description": "no type at all"}, Any),
    ],
)
def test_resolve_field_type_scalar_and_union(details, expected):
    assert _resolve_field_type(details) is expected


@pytest.mark.parametrize(
    "details,origin",
    [
        ({"type": "array", "items": {"type": "string"}}, list),
        ({"type": "object", "properties": {"a": {"type": "string"}}}, dict),
        # 没有 type 但有子结构，也要收成容器
        ({"items": {"type": "string"}}, list),
        ({"properties": {"a": {"type": "string"}}}, dict),
    ],
)
def test_resolve_field_type_containers(details, origin):
    resolved = _resolve_field_type(details)
    assert getattr(resolved, "__origin__", resolved) is origin


def test_schema_dict_to_model_accepts_playwright_shaped_schema():
    """照 @playwright/mcp 的 browser_emulate_media + browser_fill_form 拼的形状。"""
    schema = {
        "type": "object",
        "properties": {
            "media": {"type": "string", "enum": ["screen", "print"], "description": "Media"},
            "colorScheme": {"anyOf": [{"type": "null"}, {"type": "string"}]},
            "fields": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "value": {"type": "string"},
                    },
                },
            },
        },
        "required": ["media", "fields"],
    }

    model = schema_dict_to_model(schema)
    instance = model(media="print", fields=[{"name": "q", "value": "x"}])
    assert instance.media == "print"
    assert instance.fields == [{"name": "q", "value": "x"}]
    # 缺 anyOf 那种"没有 type"的属性不会把整个模型带崩
    assert instance.colorScheme is None


def test_schema_dict_to_model_still_rejects_empty_required_string():
    """放宽类型解析不能顺带放宽必填校验，这条行为要钉住。

    （chatchat.server.pydantic_v1 是基于 pydantic v2 的兼容层，
    ValidationError 直接从 pydantic 取。）
    """
    from pydantic import ValidationError

    model = schema_dict_to_model(
        {"properties": {"url": {"type": "string"}}, "required": ["url"]}
    )
    with pytest.raises(ValidationError):
        model(url="")


# ------------------------------------------------------- 工具返回值形状


def test_convert_call_tool_result_returns_content_and_artifact():
    """response_format="content_and_artifact" 要求二元组，单值会被 LangChain 拒。"""
    result = CallToolResult(
        content=[
            TextContent(type="text", text="页面标题"),
            ImageContent(type="image", mimeType="image/png", data="AAAA"),
        ],
        isError=False,
    )

    content, artifact = _convert_call_tool_result(result)

    # 只有一段文本时折叠成 str（多段才是 list），非文本内容进 artifact
    assert content == "页面标题"
    assert [item.type for item in artifact] == ["image"]


def test_convert_call_tool_result_single_text_collapses_to_str():
    content, artifact = _convert_call_tool_result(
        CallToolResult(content=[TextContent(type="text", text="5")], isError=False)
    )
    assert content == "5"
    assert artifact is None


def test_convert_call_tool_result_raises_on_error():
    from langchain_core.tools import ToolException

    with pytest.raises(ToolException):
        _convert_call_tool_result(
            CallToolResult(
                content=[TextContent(type="text", text="boom")], isError=True
            )
        )


# --------------------------------------------------------- 可选参数不发 null


def _session_returning(text: str) -> AsyncMock:
    session = AsyncMock()
    session.call_tool = AsyncMock(
        return_value=CallToolResult(content=[TextContent(type="text", text=text)], isError=False)
    )
    return session


@pytest.mark.asyncio
async def test_unfilled_optional_args_are_not_sent_as_null():
    session = _session_returning("done")
    tool = convert_mcp_tool_to_langchain_tool(
        "playwright",
        session,
        Tool(
            name="browser_snapshot",
            description="snapshot",
            inputSchema={
                "type": "object",
                "properties": {
                    "target": {"type": "string"},
                    "depth": {"type": "integer"},
                    "filename": {"type": "string"},
                },
            },
        ),
    )

    await tool.ainvoke({})

    sent = session.call_tool.await_args.args[1]
    assert sent == {}, f"没填的可选字段不能当 null 发出去：{sent}"


@pytest.mark.asyncio
async def test_provided_args_still_reach_the_server():
    session = _session_returning("done")
    tool = convert_mcp_tool_to_langchain_tool(
        "playwright",
        session,
        Tool(
            name="browser_navigate",
            description="navigate",
            inputSchema={
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        ),
    )

    out = await tool.ainvoke({"url": "data:text/html,<h1>hi</h1>"})

    assert session.call_tool.await_args.args == ("browser_navigate", {"url": "data:text/html,<h1>hi</h1>"})
    assert out == "done"


# --------------------------------------------------------------- roots 应答


@pytest.mark.asyncio
async def test_roots_callback_answers_with_file_uris(tmp_path):
    result = await _roots_callback([str(tmp_path)])(None)

    assert isinstance(result, ListRootsResult)
    assert len(result.roots) == 1
    # mcp 1.30 把 uri 收成 FileUrl 对象，不是裸 str
    assert str(result.roots[0].uri).startswith("file://")


@pytest.mark.asyncio
async def test_roots_callback_without_roots_still_answers():
    """空列表也是合法应答；不答（SDK 默认回 ErrorData）会让 playwright-mcp 卡死。"""
    result = await _roots_callback(None)(None)

    assert isinstance(result, ListRootsResult)
    assert result.roots == []
