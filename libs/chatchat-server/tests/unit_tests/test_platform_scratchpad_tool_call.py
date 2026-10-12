"""XML 工具调用必须留在 scratchpad 里（离线）。

现场（2026-10-12 15:25 日志）：指定工具模式勾选 ``search_local_knowledgebase``，
问「反射面法向量的计算原理是什么」。工具每次都返回了相关文档，模型仍然连发 4 次
（第 4 次的参数和返回与第 1 次完全相同），直到撞到 ``max_iterations``。

原因在 ``format_to_platform_tool_messages`` 的通用 AgentAction 分支：
AIMessage 只放 ``agent_action.log``，而 XML 解析出来的 log 是标签**外面**的散文，
模型不写散文时就是空串。于是 scratchpad 变成「一条空的 assistant 消息 + 一段
用户消息里的工具结果」：模型看不到自己已经调过这个工具，就把同一个调用再发一遍。
"""
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from chatchat.server.utils import get_prompt_template_dict
from langchain_chatchat.agent_toolkits.all_tools.tool import BaseToolOutput
from langchain_chatchat.agents.all_tools_agent import PlatformToolsAgentExecutor
from langchain_chatchat.agents.format_scratchpad.all_tools import (
    format_to_platform_tool_messages,
)
from langchain_chatchat.agents.output_parsers import MCPToolAction
from langchain_chatchat.agents.output_parsers.platform_knowledge_output_parsers import (
    PlatformKnowledgeOutputParserCustom,
)
from langchain_chatchat.agents.react.create_prompt_template import (
    create_prompt_platform_knowledge_mode_template,
)
from langchain_chatchat.agents.structured_chat.platform_knowledge_bind import (
    create_platform_knowledge_agent,
)

KB_CALL = (
    "<search_local_knowledgebase>"
    "<database>无线通信</database>"
    "<query>反射面法向量的计算原理</query>"
    "</search_local_knowledgebase>"
)


def _parse(text: str, available_tools):
    parser = PlatformKnowledgeOutputParserCustom(available_tools=available_tools)
    return parser.parse_result(
        [ChatGeneration(message=AIMessage(content=text))], partial=False)


class TestScratchpadKeepsTheToolCall:
    def test_call_without_prose_still_produces_a_non_empty_ai_message(self):
        actions = _parse(KB_CALL, available_tools=["search_local_knowledgebase"])

        messages = format_to_platform_tool_messages([(actions[0], "文档正文")])

        assert [m.type for m in messages] == ["ai", "human"]
        # 以前这里是空串：模型那一轮的调用被从上下文里抹掉了
        assert messages[0].content == (
            "<search_local_knowledgebase>\n"
            "<database>无线通信</database>\n"
            "<query>反射面法向量的计算原理</query>\n"
            "</search_local_knowledgebase>"
        )

    def test_prose_the_model_wrote_is_kept_before_the_call(self):
        actions = _parse(
            "我先查一下知识库。\n" + KB_CALL,
            available_tools=["search_local_knowledgebase"],
        )

        ai_message = format_to_platform_tool_messages([(actions[0], "文档正文")])[0]

        assert "我先查一下知识库" in ai_message.content
        assert "<search_local_knowledgebase>" in ai_message.content

    def test_observation_is_labelled_as_the_result_of_that_call(self):
        actions = _parse(KB_CALL, available_tools=["search_local_knowledgebase"])

        observation_message = format_to_platform_tool_messages(
            [(actions[0], "文档正文")]
        )[1]

        assert observation_message.type == "human"
        assert observation_message.content == (
            "工具「search_local_knowledgebase」的执行结果：\n文档正文"
        )

    def test_base_tool_output_is_rendered_through_its_own_format(self):
        # 知识库工具返回 BaseToolOutput(format=format_context)，模型看到的必须是
        # 渲染后的文本，而不是 python 字典 repr
        actions = _parse(KB_CALL, available_tools=["search_local_knowledgebase"])
        observation = BaseToolOutput({"docs": ["第一段"]}, format=lambda o: "渲染结果")

        message = format_to_platform_tool_messages([(actions[0], observation)])[1]

        assert "渲染结果" in message.content
        assert "docs" not in message.content

    def test_mcp_call_is_rendered_in_the_format_the_prompt_teaches(self):
        action = MCPToolAction(
            server_name="chrome-devtools",
            tool="navigate_page",
            tool_input={"url": "https://example.com"},
            log="",
        )

        messages = format_to_platform_tool_messages([(action, "已打开")])

        assert "<use_mcp_tool>" in messages[0].content
        assert "<server_name>chrome-devtools</server_name>" in messages[0].content
        assert "<tool_name>navigate_page</tool_name>" in messages[0].content
        assert "MCP 工具「navigate_page」（服务「chrome-devtools」）的执行结果" in (
            messages[1].content
        )


class _Args(BaseModel):
    database: str = Field(description="Database for Knowledge Search")
    query: str = Field(description="Query for Knowledge Search")


def _kb_tool(calls: list) -> StructuredTool:
    def _search(database: str, query: str) -> BaseToolOutput:
        calls.append((database, query))
        return BaseToolOutput(
            {"knowledge_base": database, "docs": ["反射面法向量由反射点拟合得到"]},
            format=lambda o: "反射面法向量由反射点拟合得到",
        )

    return StructuredTool(
        name="search_local_knowledgebase",
        description="Search the local knowledge base.",
        args_schema=_Args,
        func=_search,
    )


class TestNoMoreToolLoop:
    """回归：一次检索之后模型必须能收尾，而不是把同一个调用重发。"""

    async def test_model_sees_its_own_call_and_answers_on_the_second_round(self):
        tool_calls: list = []
        seen: list = []
        tool = _kb_tool(tool_calls)

        def fake_llm(prompt_value):
            seen.append(prompt_value.to_messages())
            if len(seen) == 1:
                return AIMessage(content=KB_CALL)
            return AIMessage(content="反射面法向量由多个反射点最小二乘拟合得到。")

        template = get_prompt_template_dict("action_model", "platform-knowledge-mode")
        agent = create_platform_knowledge_agent(
            llm=RunnableLambda(fake_llm),
            current_working_directory="/tmp",
            tools=[tool],
            mcp_tools=[],
            prompt=create_prompt_platform_knowledge_mode_template(
                "platform-knowledge-mode", template=template
            ),
        )
        executor = PlatformToolsAgentExecutor(
            agent=agent, tools=[tool], mcp_tools=[],
            return_intermediate_steps=True,
        )

        out = await executor.ainvoke({"input": "反射面法向量的计算原理是什么？"})

        assert out["output"].startswith("反射面法向量由多个反射点")
        assert "agent_error" not in out
        # 第二次调用模型时，上下文里必须已经有它自己那次工具调用
        second_round = seen[1]
        ai_messages = [m for m in second_round if m.type == "ai"]
        assert ai_messages and "<search_local_knowledgebase>" in ai_messages[0].content
        assert any("反射面法向量由反射点拟合得到" in str(m.content) for m in second_round)
