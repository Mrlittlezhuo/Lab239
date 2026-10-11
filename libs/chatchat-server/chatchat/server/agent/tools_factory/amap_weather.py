import requests
from chatchat.server.pydantic_v1 import Field
from .tools_registry import regist_tool

from langchain_chatchat.agent_toolkits.all_tools.tool import (
    BaseToolOutput,
)
from chatchat.server.agent.tools_access import is_configured
from chatchat.server.utils import get_tool_config

BASE_DISTRICT_URL = "https://restapi.amap.com/v3/config/district"
BASE_WEATHER_URL = "https://restapi.amap.com/v3/weather/weatherInfo"
TIMEOUT = 15


def _amap_error(data: dict) -> str:
    """高德的业务错误（鉴权失败也是 HTTP 200）。"""
    if str(data.get("status")) == "1":
        return ""
    info = data.get("info") or "未知错误"
    infocode = data.get("infocode") or "-"
    hint = "（api_key 未配置或无效）" if infocode in ("10001", "10002", "10003") else ""
    return f"高德接口报错：{info}（infocode {infocode}）{hint}"


def _get_json(url: str, params: dict):
    """返回 (data, error)。"""
    try:
        response = requests.get(url, params=params, timeout=TIMEOUT)
    except requests.exceptions.RequestException as exc:
        return None, f"请求高德接口失败：{type(exc).__name__}: {exc}"

    if response.status_code != 200:
        return None, f"高德接口返回 HTTP {response.status_code}：{response.text[:200]}"

    try:
        data = response.json()
    except ValueError:
        return None, f"高德接口返回的不是 JSON：{response.text[:200]}"

    error = _amap_error(data)
    if error:
        return None, error
    return data, ""


def get_adcode(city: str, config: dict):
    """返回 (adcode, error)。

    旧写法直接 `data["districts"][0]["adcode"]`：key 无效时高德返回 200 但没有
    `districts`，于是穿透成一个 `KeyError: 'districts'`，调用方完全看不懂。
    """
    params = {
        "keywords": city,
        "subdistrict": 0,
        "extensions": "base",
        "key": config["api_key"],
    }
    data, error = _get_json(BASE_DISTRICT_URL, params)
    if error:
        return None, error

    districts = data.get("districts") or []
    if not districts:
        return None, f"高德没有匹配到城市「{city}」的行政区编码"
    adcode = (districts[0] or {}).get("adcode")
    if not adcode:
        return None, f"城市「{city}」的高德记录里没有 adcode"
    return adcode, ""


def get_weather(adcode: str, config: dict):
    """返回 (weather_data, error)。"""
    params = {
        "city": adcode,
        "extensions": "all",
        "key": config["api_key"],
    }
    return _get_json(BASE_WEATHER_URL, params)


@regist_tool(title="高德地图天气查询")
def amap_weather(city: str = Field(description="城市名")):
    """A wrapper that uses Amap to get weather information."""
    tool_config = get_tool_config("amap")
    if not is_configured(tool_config.get("api_key")):
        return BaseToolOutput({
            "error": "未配置高德 api_key：请在 tool_settings.yaml 的 amap.api_key "
                     "填入真实 key（当前是空值或占位符）"
        })

    adcode, error = get_adcode(city, tool_config)
    if error:
        return BaseToolOutput({"error": error})

    weather_data, error = get_weather(adcode, tool_config)
    if error:
        return BaseToolOutput({"error": error})
    return BaseToolOutput(weather_data)
