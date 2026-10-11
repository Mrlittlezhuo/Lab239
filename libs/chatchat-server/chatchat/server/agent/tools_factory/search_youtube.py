from chatchat.server.pydantic_v1 import Field

from .tools_registry import regist_tool

from langchain_chatchat.agent_toolkits.all_tools.tool import (
    BaseToolOutput,
)

@regist_tool(title="油管视频")
def search_youtube(query: str = Field(description="Query for Videos search")):
    """use this tools_factory to search youtube videos"""
    try:
        from langchain_community.tools import YouTubeSearchTool

        tool = YouTubeSearchTool()
        return BaseToolOutput(tool.run(tool_input=query))
    except ImportError as exc:
        return BaseToolOutput(
            {"error": f"缺少 youtube-search 依赖：pip install youtube-search（{exc}）"}
        )
    except Exception as exc:
        return BaseToolOutput(
            {"error": f"调用 YouTube 搜索失败：{type(exc).__name__}: {exc}"
                      "（YouTube 需要代理才能访问）"}
        )
