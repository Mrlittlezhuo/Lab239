"""
简单的单参数输入工具实现，用于查询现在天气的情况
"""
import requests

from chatchat.server.agent.tools_access import is_configured
from chatchat.server.pydantic_v1 import Field
from chatchat.server.utils import get_tool_config

from .tools_registry import regist_tool

from langchain_chatchat.agent_toolkits.all_tools.tool import (
    BaseToolOutput,
)

TIMEOUT = 15

@regist_tool(title="天气查询")
def weather_check(
    city: str = Field(description="City name,include city and county,like '厦门'"),
):
    """Use this tool to check the weather at a specific city"""

    tool_config = get_tool_config("weather_check")
    api_key = tool_config.get("api_key")
    if not is_configured(api_key):
        return BaseToolOutput({
            "error": "未配置心知天气 api_key：请在 tool_settings.yaml 的 "
                     "weather_check.api_key 填入真实 key（当前是空值或占位符）"
        })

    url = f"https://api.seniverse.com/v3/weather/now.json?key={api_key}&location={city}&language=zh-Hans&unit=c"
    try:
        response = requests.get(url, timeout=TIMEOUT)
    except requests.exceptions.RequestException as exc:
        return BaseToolOutput(
            {"error": f"请求心知天气失败：{type(exc).__name__}: {exc}"}
        )

    if response.status_code != 200:
        # 状态码不够：403 到底是 key 无效还是配额用尽，只有响应体里写着
        return BaseToolOutput({
            "error": f"心知天气返回 HTTP {response.status_code}：{response.text[:200]}"
        })

    try:
        data = response.json()
        now = (data.get("results") or [{}])[0].get("now") or {}
    except (ValueError, AttributeError, IndexError) as exc:
        return BaseToolOutput(
            {"error": f"心知天气响应形状不对：{type(exc).__name__}: {response.text[:200]}"}
        )

    if not now:
        return BaseToolOutput({"error": f"心知天气没有「{city}」的当前天气数据"})

    weather = {
        "temperature": now.get("temperature"),
        "description": now.get("text"),
    }
    return BaseToolOutput(weather)
