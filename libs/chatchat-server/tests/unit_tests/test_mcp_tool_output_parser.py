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
from langchain_chatchat.agents.output_parsers.platform_tools import (
    PlatformToolsAgentOutputParser,
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


def _parse(text, **parser_kwargs):
    """按真实链路喂一条 ChatGeneration（父类解析器只认 ChatGeneration）。"""
    return PlatformKnowledgeOutputParserCustom(**parser_kwargs).parse_result(
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


# 兜底分支把"任何未知顶层标签"当工具名。本轮没有这个工具时，造出来的调用
# 找不到执行者，不发 tool_start/tool_end，模型看到空观察就原样重试，
# 一直转到 max_iterations（15 步）才吐 "Agent stopped due to max iterations."
NAVIGATE_TAG = "<navigate_page><url>https://example.com</url></navigate_page>"


class TestAvailableToolsWhitelist:
    def test_unknown_tag_is_not_a_tool_call_when_tools_are_listed(self):
        result = _parse(
            "这是回答正文。\n<navigate_page><url>https://x.com</url></navigate_page>",
            available_tools=["calculate"],
        )

        assert isinstance(result, AgentFinish)
        assert "这是回答正文" in result.return_values["output"]

    def test_listed_tool_tag_still_becomes_a_tool_call(self):
        actions = _parse(
            "<calculate><text>1+2</text></calculate>",
            available_tools=["calculate"],
        )

        assert [a.tool for a in actions] == ["calculate"]
        assert actions[0].tool_input == {"text": "1+2"}

    def test_no_tools_this_round_fabricates_nothing(self):
        # 智能对话路由回退 direct 时就是这种轮次
        result = _parse("我来搜一下。\n" + NAVIGATE_TAG,
                        available_tools=[], available_mcp_tools=[])

        assert isinstance(result, AgentFinish)

    def test_mcp_call_is_dropped_when_the_server_is_not_loaded(self):
        result = _parse(THINKING_WITH_AMP + MCP_BLOCK, available_mcp_tools=[])

        assert isinstance(result, AgentFinish)

    def test_mcp_call_survives_when_the_tool_is_loaded(self):
        actions = _parse(THINKING_WITH_AMP + MCP_BLOCK,
                         available_mcp_tools=["navigate_page"])

        assert [a.tool for a in actions] == ["navigate_page"]

    def test_no_whitelist_keeps_the_old_behaviour(self):
        # 其它调用方（没有传白名单的）行为不变
        actions = _parse(NAVIGATE_TAG)

        assert [a.tool for a in actions] == ["navigate_page"]


class TestPlatformToolsAgentComposition:
    """带工具的轮次里，bind 会把带白名单的解析器实例交给 PlatformToolsAgentOutputParser。

    现场（手动模式勾工具后整条聊天 500）：
    `knowledge_parser` 字段标的是 `AgentOutputParser`，而
    `PlatformKnowledgeOutputParserCustom` 继承的是
    ToolsAgentOutputParser → MultiActionAgentOutputParser，不是它的子类。
    以前只靠 default_factory 赋值（pydantic 不校验默认值）所以没暴露，
    一旦显式传实例就报
    `Input should be a valid dictionary or instance of AgentOutputParser`。
    """

    def _outer(self, **parser_kwargs):
        return PlatformToolsAgentOutputParser(
            instance_type="platform-knowledge-mode",
            knowledge_parser=PlatformKnowledgeOutputParserCustom(**parser_kwargs),
        )

    def test_accepts_a_customized_knowledge_parser_instance(self):
        outer = self._outer(available_tools=["calculate"], available_mcp_tools=[])

        assert outer.knowledge_parser.available_tools == ["calculate"]

    def test_default_construction_still_works(self):
        outer = PlatformToolsAgentOutputParser(instance_type="platform-knowledge-mode")

        assert isinstance(outer.knowledge_parser, PlatformKnowledgeOutputParserCustom)

    def test_whitelist_survives_through_the_outer_parser(self):
        outer = self._outer(available_tools=["calculate"], available_mcp_tools=[])

        result = outer.parse_result(
            [ChatGeneration(message=AIMessage(content="我来搜一下。\n" + NAVIGATE_TAG))],
            partial=False,
        )

        assert isinstance(result, AgentFinish)

    def test_listed_tool_still_runs_through_the_outer_parser(self):
        outer = self._outer(available_tools=["calculate"])

        actions = outer.parse_result(
            [ChatGeneration(message=AIMessage(content="<calculate><text>1+2</text></calculate>"))],
            partial=False,
        )

        assert [a.tool for a in actions] == ["calculate"]
