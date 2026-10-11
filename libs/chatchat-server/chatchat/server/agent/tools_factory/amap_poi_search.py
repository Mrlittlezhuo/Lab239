import requests
from chatchat.server.pydantic_v1 import Field
from .tools_registry import regist_tool

from langchain_chatchat.agent_toolkits.all_tools.tool import (
    BaseToolOutput,
)
from chatchat.server.agent.tools_access import is_configured
from chatchat.server.utils import get_tool_config

BASE_URL = "https://restapi.amap.com/v5/place/text"
TIMEOUT = 15


def _amap_error(data: dict) -> str:
    """高德的业务错误。

    鉴权失败（比如 key 还是配置里的中文占位符）高德返回的是
    HTTP 200 + `{"status":"0","info":"INVALID_USER_KEY","infocode":"10001"}`，
    旧代码只看 HTTP 状态码，于是这段错误 JSON 被当成搜索结果喂给模型。
    """
    if str(data.get("status")) == "1":
        return ""
    info = data.get("info") or "未知错误"
    infocode = data.get("infocode") or "-"
    hint = "（api_key 未配置或无效）" if infocode in ("10001", "10002", "10003") else ""
    return f"高德接口报错：{info}（infocode {infocode}）{hint}"


def amap_poi_search_engine(keywords: str, types: str, config: dict):
    if not is_configured(config.get("api_key")):
        return {
            "error": "未配置高德 api_key：请在 tool_settings.yaml 的 amap.api_key "
                     "填入真实 key（当前是空值或占位符）"
        }

    params = {
        "keywords": keywords,
        "types": types,
        "key": config["api_key"],
    }
    try:
        response = requests.get(BASE_URL, params=params, timeout=TIMEOUT)
    except requests.exceptions.RequestException as exc:
        return {"error": f"请求高德接口失败：{type(exc).__name__}: {exc}"}

    if response.status_code != 200:
        return {
            "error": f"高德接口返回 HTTP {response.status_code}：{response.text[:200]}"
        }

    try:
        data = response.json()
    except ValueError:
        return {"error": f"高德接口返回的不是 JSON：{response.text[:200]}"}

    error = _amap_error(data)
    if error:
        return {"error": error}
    return data


@regist_tool(title="高德地图POI搜索")
def amap_poi_search(location: str = Field(description="'实际地名'或者'具体的地址',不能使用简称或者别称"),
                types: str = Field(description="POI类型，比如商场、学校、医院等等")):
    """ A wrapper that uses Amap to search."""
    tool_config = get_tool_config("amap")
    return BaseToolOutput(amap_poi_search_engine(keywords=location,types=types,config=tool_config))
