# Langchain 自带的 Wolfram Alpha API 封装

from chatchat.server.agent.tools_access import is_configured
from chatchat.server.pydantic_v1 import Field
from chatchat.server.utils import get_tool_config

from .tools_registry import regist_tool

from langchain_chatchat.agent_toolkits.all_tools.tool import (
    BaseToolOutput,
)


@regist_tool
def wolfram(query: str = Field(description="The formula to be calculated")):
    """Useful for when you need to calculate difficult formulas"""

    appid = get_tool_config("wolfram").get("appid")
    if not is_configured(appid):
        return BaseToolOutput({
            "error": "未配置 WolframAlpha appid：请在 tool_settings.yaml 的 "
                     "wolfram.appid 填入真实 appid（当前是空值或占位符）"
        })

    try:
        from langchain_community.utilities.wolfram_alpha import WolframAlphaAPIWrapper

        wolfram = WolframAlphaAPIWrapper(wolfram_alpha_appid=appid)
        ans = wolfram.run(query)
    except ImportError as exc:
        # 缺包在导入期才炸，旧写法把它当工具运行结果抛出去，接口层只剩一句 failed
        return BaseToolOutput(
            {"error": f"缺少 wolframalpha 依赖：pip install wolframalpha（{exc}）"}
        )
    except Exception as exc:
        return BaseToolOutput(
            {"error": f"调用 WolframAlpha 失败：{type(exc).__name__}: {exc}"}
        )
    return BaseToolOutput(ans)
