from __future__ import annotations

import json
import logging
import re
from functools import partial
from operator import itemgetter
from typing import Any, List, Sequence, Tuple, Union

from langchain_classic.agents.agent import AgentExecutor, RunnableAgent
from langchain_classic.agents.output_parsers import ToolsAgentOutputParser
from langchain_classic.agents.structured_chat.output_parser import StructuredChatOutputParser
from langchain_core.prompts.chat import BaseChatPromptTemplate
from langchain_core.agents import (
    AgentAction,
    AgentFinish,
)
from langchain_chatchat.utils.try_parse_json_object import try_parse_json_object

logger = logging.getLogger()
import xml.etree.ElementTree as ET

from langchain_core.outputs import Generation

class MCPToolAction(AgentAction):
    server_name: str  

    @classmethod
    def is_lc_serializable(cls) -> bool:
        """Return whether or not the class is serializable."""
        return True

    @classmethod
    def get_lc_namespace(cls) -> List[str]:
        """Get the namespace of the langchain object."""
        return ["langchain_chatchat", "agents", "output_parsers", "platform_knowledge_output_parsers"]
        
# 模型经常把带 & 的 URL 原样写进 thinking 或工具参数里（例如
# https://github.com/search?q=agent+framework&type=repositories&s=stars）。裸 & 在 XML 里
# 是实体开头，ElementTree 当场报 not well-formed；以前这个异常会让整个 <use_mcp_tool>
# 被丢掉并直接收尾，用户看到的是"Agent 说要调工具，然后就结束了"。
_BARE_AMP = re.compile(
    r"&(?!(?:[a-zA-Z][a-zA-Z0-9]{1,10}|#[0-9]{1,7}|#[xX][0-9a-fA-F]{1,6});)")
_STRAY_LT = re.compile(r"<(?![A-Za-z_/!])")
_XML_INVALID_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

# 兜底：整段解析不了的时候，至少把 use_mcp_tool 块单独抠出来
_MCP_BLOCK = re.compile(
    r"<use_mcp_tool>\s*<server_name>(.*?)</server_name>\s*"
    r"<tool_name>(.*?)</tool_name>\s*<arguments>(.*?)</arguments>\s*</use_mcp_tool>",
    re.S,
)


def _xml_safe(text: str) -> str:
    """把裸 & 和不成标签的 < 转义掉，控制字符直接去掉，再去解析 XML。"""
    text = _XML_INVALID_CHARS.sub("", text)
    text = _BARE_AMP.sub("&amp;", text)
    return _STRAY_LT.sub("&lt;", text)


def _child_text(elem, tag: str) -> str:
    child = elem.find(tag)
    return (child.text or "").strip() if child is not None else ""


def collect_plain_text(root):
    texts = []
    if root.text and root.text.strip():
        texts.append(root.text.strip())
    for elem in root.iter():
        if elem.tail and elem.tail.strip():
            texts.append(elem.tail.strip())
    return "".join(texts)

class PlatformKnowledgeOutputParserCustom(ToolsAgentOutputParser):
    """Output parser with retries for the structured chat agent with custom Knowledge prompt."""

    def parse_result(
            self, result: List[Generation], *, partial: bool = False
    ) -> Union[List[Union[AgentAction, MCPToolAction]], AgentFinish]:

        """Parse a list of candidate model Generations into a specific format."""
        tools = super().parse_result(result, partial=partial)
        message = result[0].message
        temp_tools = []
        try:
            cleaned_content = str(message.content).replace("</think>", "")

            wrapped_xml = f"<root>{_xml_safe(cleaned_content)}</root>"
            # 解析mcp_use标签
            try:
                root = ET.fromstring(wrapped_xml)
            except ET.ParseError as exc:
                # 整段坏了不代表工具调用也坏了：把 use_mcp_tool 块单独抠出来再试一次
                logger.warning(f"模型输出不是合法 XML，改用宽松提取：{exc}")
                return self._lenient_mcp_actions(cleaned_content, tools, message)

            log_text = collect_plain_text(root)
            # 遍历所有顶层标签
            for elem in root:  
                if elem.tag == 'use_mcp_tool':
                    # 处理use_mcp_tool标签
                    server_name = _child_text(elem, "server_name")
                    tool_name = _child_text(elem, "tool_name")

                    # 提取并解析 arguments 中的 JSON 字符串
                    arguments_raw = _child_text(elem, "arguments")
                    if not (server_name and tool_name and arguments_raw):
                        # 少字段只是这一个调用坏了，不该把整轮一起废掉
                        logger.warning(f"use_mcp_tool 块字段不全，跳过：{elem.tag}")
                        continue

                    _, json_input = try_parse_json_object(arguments_raw)
                    act = MCPToolAction(
                        server_name=server_name,
                        tool=tool_name,
                        tool_input=json_input,
                        log=str(log_text)
                    )
                    temp_tools.append(act)
                elif elem.tag == 'thinking':
                    # 忽略thinking标签，这是用于内部思考过程的标签
                    continue
                elif elem.tag in ['use_mcp_resource']:
                    # 处理use_mcp_resource标签，暂时跳过
                    continue
                else:
                    # 处理其他工具标签（如calculate等）
                    tool_name = elem.tag
                    tool_input = {}
                    
                    # 遍历标签内的所有子标签，作为工具参数
                    for child in elem:
                        if child.text and child.text.strip():
                            tool_input[child.tag] = child.text.strip()
                    
                    # 创建通用的AgentAction
                    act = AgentAction(
                        tool=tool_name,
                        tool_input=tool_input,
                        log=str(log_text)
                    )
                    temp_tools.append(act)

            if isinstance(tools, AgentFinish) and len(temp_tools) == 0:
                return tools 

            elif not isinstance(tools, AgentFinish):
                temp_tools.extend(tools)
        except Exception as e:
            logger.error(e)
            return AgentFinish(return_values={"output": str(message.content)}, log=str(message.content))
        return temp_tools

    def _lenient_mcp_actions(self, cleaned_content, tools, message):
        """整段 XML 解析失败时的兜底：只认 use_mcp_tool 块。

        以前解析失败就直接 ``AgentFinish``，模型已经写出来的工具调用被整个丢掉，
        表现为"Agent 说要操作浏览器，然后这一轮就结束了"。
        """
        actions = []
        for server_name, tool_name, arguments_raw in _MCP_BLOCK.findall(cleaned_content):
            _, json_input = try_parse_json_object(arguments_raw.strip())
            actions.append(MCPToolAction(
                server_name=server_name.strip(),
                tool=tool_name.strip(),
                tool_input=json_input,
                log=str(message.content),
            ))

        if not actions:
            return AgentFinish(return_values={"output": str(message.content)},
                               log=str(message.content))
        if not isinstance(tools, AgentFinish):
            actions.extend(tools)
        return actions

    @property
    def _type(self) -> str:
        return "PlatformKnowledgeOutputParserCustom"
