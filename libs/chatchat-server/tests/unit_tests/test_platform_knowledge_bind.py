"""`create_platform_knowledge_agent` 的两条分支都必须构造得出来（离线）。

现场：手动模式勾上工具后整条聊天直接 500 ——
`1 validation error for PlatformToolsAgentOutputParser: knowledge_parser
Input should be a valid dictionary or instance of AgentOutputParser`。

bind 里显式传给 PlatformToolsAgentOutputParser 的是带工具白名单的
`PlatformKnowledgeOutputParserCustom` 实例，而这个类的继承链是
ToolsAgentOutputParser → MultiActionAgentOutputParser，
不是字段上标的 `AgentOutputParser` 的子类。以前只走 default_factory
（pydantic 不校验默认值）所以一直没暴露。
"""
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import tool

from langchain_chatchat.agents.output_parsers.platform_tools import (
    PlatformToolsAgentOutputParser,
)
from langchain_chatchat.agents.structured_chat.platform_knowledge_bind import (
    create_platform_knowledge_agent,
)


@tool
def calculate(text: str) -> str:
    """数学计算"""
    return str(eval(text))


def _prompt() -> ChatPromptTemplate:
    return ChatPromptTemplate.from_messages(
        [
            (
                "system",
                "{tools} {mcp_tools} {datetime} {current_working_directory}",
            ),
            MessagesPlaceholder(variable_name="agent_scratchpad"),
        ]
    )


def _parser_of(agent) -> PlatformToolsAgentOutputParser:
    return agent.steps[-1]


def test_direct_chat_branch_builds_and_blocks_all_tools():
    agent = create_platform_knowledge_agent(
        llm=RunnableLambda(lambda x: "你好"),
        current_working_directory="/tmp",
        tools=[],
        mcp_tools=[],
        prompt=_prompt(),
    )

    parser = _parser_of(agent)
    assert isinstance(parser, PlatformToolsAgentOutputParser)
    # 本轮没有工具，解析器也不该把任何 XML 标签当工具调用
    assert parser.knowledge_parser.available_tools == []
    assert parser.knowledge_parser.available_mcp_tools == []


def test_tools_branch_builds_and_passes_the_tool_names():
    agent = create_platform_knowledge_agent(
        llm=RunnableLambda(lambda x: "<calculate><text>1+2</text></calculate>"),
        current_working_directory="/tmp",
        tools=[calculate],
        mcp_tools=[],
        prompt=_prompt(),
    )

    assert _parser_of(agent).knowledge_parser.available_tools == ["calculate"]
