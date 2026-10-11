# LangChain 的 WikipediaQueryRun 工具
from langchain_community.tools import WikipediaQueryRun
from langchain_community.utilities import WikipediaAPIWrapper
from chatchat.server.pydantic_v1 import Field



from .tools_registry import regist_tool

from langchain_chatchat.agent_toolkits.all_tools.tool import (
    BaseToolOutput,
)

# Wikimedia 的 robot policy 要求 User-Agent 里带可识别的标识和联系方式，
# 否则直接 403（纯文本），而 wikipedia 包会把这段纯文本当 JSON 解析，
# 用户只看到 `JSONDecodeError: Expecting value: line 1 column 1 (char 0)`。
USER_AGENT = (
    "chatchat/0.1 (Langchain-Chatchat knowledge assistant; "
    "https://github.com/chatchat-space/Langchain-Chatchat)"
)


@regist_tool(title="维基百科搜索")
def wikipedia_search(query: str = Field(description="The search query")):
    """ A wrapper that uses Wikipedia to search."""
    try:
        import wikipedia

        wikipedia.set_user_agent(USER_AGENT)
        api_wrapper = WikipediaAPIWrapper(lang="zh")
        tool = WikipediaQueryRun(api_wrapper=api_wrapper)
        return BaseToolOutput(tool.run(tool_input=query))
    except ImportError as exc:
        return BaseToolOutput(
            {"error": f"缺少 wikipedia 依赖：pip install wikipedia（{exc}）"}
        )
    except Exception as exc:
        # 连不上（本机直连 zh.wikipedia.org 超时，需要代理）与 403 都在这里，
        # 必须带出异常类型，否则排查时只能看到一个 JSONDecodeError
        detail = f"{type(exc).__name__}: {exc}"
        if "JSONDecodeError" in type(exc).__name__:
            detail += "（Wikipedia 返回的不是 JSON：多半是被 403 拒绝或网络不通，需要代理）"
        return BaseToolOutput({"error": f"调用 Wikipedia 失败：{detail}"})
