"""
source  https://github.com/langchain-ai/langchain-mcp-adapters
"""
from typing import Any, Dict, List, Type
import inspect

from langchain_core.tools import BaseTool, StructuredTool, ToolException
from mcp import ClientSession
from mcp.types import (
    CallToolResult,
    EmbeddedResource,
    ImageContent,
    TextContent,
)
from mcp.types import (
    Tool as MCPTool,
)
from chatchat.server.pydantic_v1 import BaseModel, create_model, Field, FieldInfo

NonTextContent = ImageContent | EmbeddedResource


class MCPStructuredTool(StructuredTool):
    server_name: str


_JSON_SCALAR_TYPES = {"integer": int, "string": str, "number": float, "boolean": bool}


def _enum_type_name(schema: Dict[str, Any]) -> str | None:
    """enum 里所有值同种类型时返回对应的 JSON Schema 类型名，混合类型返回 None。"""
    values = schema.get("enum") or []
    kinds = {type(value) for value in values}
    if kinds == {str}:
        return "string"
    if kinds == {int}:
        return "integer"
    if kinds == {float}:
        return "number"
    if kinds == {bool}:
        return "boolean"
    return None


def _json_type_candidates(details: Dict[str, Any]) -> list[str]:
    """收集一个属性上所有可能出现的 JSON Schema 类型名。

    真实的 MCP server 经常不直接给 `type`：playwright-mcp 的 browser_emulate_media
    用 anyOf，firecrawl-mcp 的 scrape.alexandria 也是 anyOf，还有只给 enum 的。
    老代码写的是 `details['type']`，一个这样的属性就能让 KeyError 冒到连接层，
    结果整个 server 一个工具都加载不出来。
    """
    candidates: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str):
            candidates.append(value)
        elif isinstance(value, list):
            candidates.extend(v for v in value if isinstance(v, str))

    add(details.get("type"))
    for key in ("anyOf", "oneOf", "allOf"):
        for branch in details.get(key) or []:
            if not isinstance(branch, dict):
                continue
            add(branch.get("type"))
            if not branch.get("type"):
                add(_enum_type_name(branch))
    if not candidates:
        add(_enum_type_name(details))
    return candidates


def _resolve_field_type(details: Dict[str, Any]) -> Any:
    """把一个 JSON Schema 属性描述解析成能收下该值的 python 类型。"""
    for name in _json_type_candidates(details):
        if name == "null":
            continue
        if name in _JSON_SCALAR_TYPES:
            return _JSON_SCALAR_TYPES[name]
        if name == "array":
            items = details.get("items")
            item_type = Any
            if isinstance(items, dict):
                item_type = _JSON_SCALAR_TYPES.get(items.get("type"), Any)
            return List[item_type]
        if name == "object":
            return Dict[str, Any]

    # 一个类型名都没收集到：有子结构的按容器收，其余退化成 Any，绝不抛异常。
    if "properties" in details or details.get("additionalProperties"):
        return Dict[str, Any]
    if "items" in details:
        return List[Any]
    return Any


def schema_dict_to_model(schema: Dict[str, Any]) -> Any:
    """
    Convert JSON schema to Pydantic model with required field validation.
    
    Args:
        schema: JSON schema dictionary containing tool parameter definitions
        
    Returns:
        Dynamic Pydantic model class with proper field validation
        
    Note:
        Required fields are marked with required=True to ensure they have actual content,
        empty or null values are strictly prohibited for required parameters.
    """
    fields = schema.get('properties', {})
    required_fields = schema.get('required', [])

    model_fields = {}
    for field_name, details in fields.items():
        field_type = _resolve_field_type(details)
        
        # Add field description if available
        field_description = details.get('description', '')

        # For required fields, use Field with required=True
        if field_name in required_fields:
            # Ensure required fields have actual content and cannot be empty/null
            if field_type == str:
                model_fields[field_name] = (field_type, Field(..., min_length=1, required=True, 
                    description=field_description or f"Required string parameter: {field_name}"))
            elif field_type in (int, float):
                model_fields[field_name] = (field_type, Field(..., required=True, 
                    description=field_description or f"Required numeric parameter: {field_name}"))
            elif field_type == bool:
                model_fields[field_name] = (field_type, Field(..., required=True, 
                    description=field_description or f"Required boolean parameter: {field_name}"))
            else:
                model_fields[field_name] = (field_type, Field(..., required=True, 
                    description=field_description or f"Required parameter: {field_name}"))
        else:
            # Optional fields can be None
            model_fields[field_name] = (field_type, Field(None, required=False, 
                description=field_description or f"Optional parameter: {field_name}"))

    DynamicSchema = create_model(schema.get('title', 'DynamicSchema'), **model_fields)
    return DynamicSchema


def _convert_call_tool_result(
        call_tool_result: CallToolResult,
) -> tuple[str | list[str], list[NonTextContent] | None]:
    text_contents: list[TextContent] = []
    non_text_contents = []
    for content in call_tool_result.content:
        if isinstance(content, TextContent):
            text_contents.append(content)
        else:
            non_text_contents.append(content)

    tool_content: str | list[str] = [content.text for content in text_contents]
    if len(text_contents) == 1:
        tool_content = tool_content[0]

    if call_tool_result.isError:
        raise ToolException(tool_content)

    # convert_mcp_tool_to_langchain_tool 声明的是 response_format="content_and_artifact"，
    # LangChain 要求这种工具必须返回 (content, artifact) 二元组；只返回 content 的话
    # 每一次 MCP 工具调用都会在 pydantic 校验处抛 ValueError，工具等于完全不可用。
    # 非文本内容（截图等）就是 artifact，交给上层展示。
    return tool_content, non_text_contents or None


def convert_mcp_tool_to_langchain_tool(
        server_name: str,
        session: ClientSession,
        tool: MCPTool,
) -> BaseTool:
    """Convert an MCP tool to a LangChain tool.

    NOTE: this tool can be executed only in a context of an active MCP client session.

    Args:
        server_name: MCP server name
        session: MCP client session
        tool: MCP tool to convert

    Returns:
        a LangChain tool
    """

    async def call_tool(
            **arguments: dict[str, Any],
    ) -> tuple[str | list[str], list[NonTextContent] | None]:
        # args_schema 里的可选字段默认值是 None，LangChain 会把它们一并传进来。
        # 服务端普遍按"字段没出现"当缺省，显式 null 会被拒（playwright-mcp 的
        # browser_snapshot 就是这样：`expected string, received null → at target`）。
        arguments = {k: v for k, v in arguments.items() if v is not None}
        call_tool_result = await session.call_tool(tool.name, arguments)
        return _convert_call_tool_result(call_tool_result)

    tool_input_model = schema_dict_to_model(tool.inputSchema)
    return MCPStructuredTool(
        server_name=server_name,
        name=tool.name,
        description=tool.description or "",
        args_schema=tool_input_model,
        coroutine=call_tool,
        response_format="content_and_artifact",
    )


async def load_mcp_tools(server_name: str, session: ClientSession) -> list[BaseTool]:
    """Load all available MCP tools and convert them to LangChain tools."""
    tools = await session.list_tools()
    return [convert_mcp_tool_to_langchain_tool(server_name, session, tool) for tool in tools.tools]
