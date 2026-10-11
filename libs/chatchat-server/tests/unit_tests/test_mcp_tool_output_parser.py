"""MCP 工具调用的输出解析（``<use_mcp_tool>``），全部离线。

回归的现场：模型把带 ``&`` 的搜索 URL 原样写进 thinking，ElementTree 把裸 ``&``
当成实体开头报 not well-formed，旧代码在 ``except`` 里直接 ``AgentFinish``，于是
模型已经写出来的 ``<use_mcp_tool>`` 被整个丢掉 —— 用户看到的是"Agent 说要导航到
GitHub，然后这一轮就结束了"。
"""
import pytest

from langchain_core.agents import AgentFinish
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration

from langchain_chatchat.agents.output_parsers.platform_knowledge_output_parsers import (
    MCPToolAction,
    PlatformKnowledgeOutputParserCustom,
    _xml_safe,
)

# 真实出错的那一条：thinking 里的 GitHub 搜索 URL 带两个裸 &
THINKING_WITH_AMP = (
    "<thinking>\n用户要搜索 GitHub 里 star 比较多的 Agent 开发项目。"
    "GitHub 支持按 star 排序的搜索 URL："
    "`https://github.com/search?q=agent+framework&type=repositories&s=stars&o=desc`。"
    "</thinking>\n"
)
MCP_BLOCK = (
    "<use_mcp_tool>\n"
    "<server_name>chrome-devtools</server_name>\n"
    "<tool_name>navigate_page</tool_name>\n"
    "<arguments>\n{\n  \"type\": \"url\",\n"
    "  \"url\": \"https://github.com/search?q=agent+framework&type=repositories&s=stars&o=desc\"\n"
    "}\n</arguments>\n"
    "</use_mcp_tool>"
)


def _parse(text):
    """按真实链路喂一条 ChatGeneration（父类解析器只认 ChatGeneration）。"""
    return PlatformKnowledgeOutputParserCustom().parse_result(
        [ChatGeneration(message=AIMessage(content=text))], partial=False)


class TestXmlSafe:
    @pytest.mark.parametrize("raw,expected", [
        ("a&type&s", "a&amp;type&amp;s"),
        ("已转义 &amp; 不动", "已转义 &amp; 不动"),
        ("&nbsp; 与 &#39; 与 &#x1F600;", "&nbsp; 与 &#39; 与 &#x1F600;"),
        ("<thinking>标签</thinking>", "<thinking>标签</thinking>"),
        ("a < b 和 <= 号", "a &lt; b 和 &lt;= 号"),
        ("注释 <!-- x -->", "注释 <!-- x -->"),
        ("控制字符 \x00 \x07", "控制字符  "),
    ])
    def test_escapes_only_what_breaks_xml(self, raw, expected):
        assert _xml_safe(raw) == expected


class TestParseResult:
    def test_url_with_bare_ampersand_still_yields_the_tool_call(self):
        actions = _parse(THINKING_WITH_AMP + MCP_BLOCK)

        assert len(actions) == 1
        assert isinstance(actions[0], MCPToolAction)
        assert actions[0].tool == "navigate_page"
        assert actions[0].server_name == "chrome-devtools"
        assert actions[0].tool_input["url"] == (
            "https://github.com/search?q=agent+framework&type=repositories&s=stars&o=desc")

    def test_plain_tool_call_still_works(self):
        actions = _parse("<thinking>先看页面</thinking>\n" + MCP_BLOCK.replace("&type", "type"))

        assert [a.tool for a in actions] == ["navigate_page"]

    def test_broken_xml_falls_back_to_extracting_the_mcp_block(self):
        # <thinking> 忘了闭合：整段没法解析，但工具调用还在
        broken = "<thinking> 没闭合的标签 " + MCP_BLOCK

        actions = _parse(broken)

        assert [a.tool for a in actions] == ["navigate_page"]
        assert actions[0].tool_input["type"] == "url"

    def test_use_mcp_tool_missing_arguments_is_skipped_not_fatal(self):
        half_broken = (
            "<use_mcp_tool><server_name>chrome-devtools</server_name>"
            "<tool_name>list_pages</tool_name></use_mcp_tool>"
        )

        result = _parse(half_broken)

        # 唯一的调用坏了：保持旧行为，按收尾处理，不抛异常
        assert isinstance(result, AgentFinish)

    def test_text_only_output_finishes_with_the_model_text(self):
        result = _parse("这就是回答，没有工具调用。")

        assert isinstance(result, AgentFinish)
        assert "这就是回答" in result.return_values["output"]
