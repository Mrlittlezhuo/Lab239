# LangChain 的 ArxivQueryRun 工具
from chatchat.server.pydantic_v1 import Field

from .tools_registry import regist_tool

from langchain_chatchat.agent_toolkits.all_tools.tool import (
    BaseToolOutput,
)

@regist_tool(title="ARXIV论文")
def arxiv(query: str = Field(description="The search query title")):
    """A wrapper around Arxiv.org for searching and retrieving scientific articles in various fields."""
    # 依赖是可选的：没装 arxiv 包时，以前是一句 ImportError 穿透成接口 500。
    # 注意 arxiv 必须 <3：langchain_community 的封装调 `arxiv.Search(...).results()`，
    # arxiv 3/4 把它挪成了 `arxiv.Client().results(search)`。
    try:
        from langchain_community.tools.arxiv.tool import ArxivQueryRun

        tool = ArxivQueryRun()
        return BaseToolOutput(tool.run(tool_input=query))
    except ImportError as exc:
        return BaseToolOutput(
            {"error": f"缺少 arxiv 依赖：pip install 'arxiv<3'（{exc}）"}
        )
    except Exception as exc:
        return BaseToolOutput(
            {"error": f"调用 arXiv 失败：{type(exc).__name__}: {exc}"
                      "（arXiv 需要能访问 export.arxiv.org）"}
        )
