"""工具准入：注册表只说明"代码里有这个工具"，这里说明"这台机器配好了、这个用户能用"。

以前两者混在一起：

- `tool_settings.yaml` 里每个工具的 `use` 开关后端根本不读（全仓没有任何地方消费），
  未配置的工具照样注册、照样进意图路由的候选清单，模型选中之后只能失败；
- `shell` 是 `subprocess.run(shell=True)` 直通，对所有登录用户开放，
  `POST /tools/call` 还能直接点名调用；
- 配置里留着中文占位符（`api_key: 高德地图 API KEY`、
  `sqlalchemy_connect_str: mysql+pymysql://用户名:密码@主机地址/数据库名称`）时，
  工具会把占位符原样发出去，错误信息里看不出是配置问题。
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional

from chatchat.settings import Settings
from chatchat.utils import build_logger

logger = build_logger()

# 能执行任意系统命令的工具：只给管理员，并且必须在配置里显式打开
ADMIN_ONLY_TOOLS = {"shell"}

# 开关所在的配置段。shell 的开关放在 shell_tool 段：字段名不能叫 shell，
# 否则环境变量 SHELL 会被 pydantic-settings 当成该字段的值，整个配置解析失败。
_USE_SECTION = {"shell": "shell_tool"}

# 工具名 -> (配置段, 必填字段)。amap 的两个工具共用 amap 段。
_REQUIRED_CONFIG = {
    "amap_poi_search": ("amap", "api_key"),
    "amap_weather": ("amap", "api_key"),
    "weather_check": ("weather_check", "api_key"),
    "wolfram": ("wolfram", "appid"),
    "text2sql": ("text2sql", "sqlalchemy_connect_str"),
}

# 配置里残留的占位符都带中文（key、连接串、主机地址都是）
_PLACEHOLDER = re.compile(r"[^\x00-\x7F]")


def _section(name: str) -> Dict[str, Any]:
    return Settings.tool_settings.model_dump().get(name) or {}


def is_configured(value: Any) -> bool:
    """这个配置值是否像真的填过：非空，且不是留着的中文占位符。

    工具自己也要用它：把 `api_key: 高德地图 API KEY` 原样发给第三方，
    换回来的是 HTTP 200 + `INVALID_USER_KEY`，错误里完全看不出是配置问题。
    """
    if value is None:
        return False
    text = str(value).strip()
    return bool(text) and not _PLACEHOLDER.search(text)


def tool_block_reason(name: str, is_admin: bool = False) -> Optional[str]:
    """这个工具当前不该被使用的原因；返回 ``None`` 表示放行。

    给调用方（也给用户）看得懂的话，而不是等到工具在半路炸掉。
    """
    # 注意：这里只对这些高危工具消费 `use` 开关。其余工具的 yaml 里普遍写着
    # `use: false`（历史上没人读它），一刀切按它过滤会把本来能用的工具全藏掉。
    if name in ADMIN_ONLY_TOOLS:
        section = _USE_SECTION.get(name, name)
        if not bool(_section(section).get("use")):
            return (
                f"工具「{name}」默认关闭：它会在服务器上执行任意系统命令，"
                f"确认需要时把 tool_settings.yaml 里的 {section}.use 设为 true"
            )
        if not is_admin:
            return f"工具「{name}」只允许管理员使用"

    config_key = _REQUIRED_CONFIG.get(name)
    if config_key:
        section, field = config_key
        if not is_configured(_section(section).get(field)):
            return (
                f"工具「{name}」未配置：请在 tool_settings.yaml 的 "
                f"{section}.{field} 填入真实值（当前是空值或占位符）"
            )

    if name == "text2images":
        model = _section("text2images").get("model")
        from chatchat.server.utils import get_model_info

        if not model or not get_model_info(model):
            return (
                f"工具「text2images」未配置：模型「{model}」不在任何平台的 "
                f"text2image_models 里（model_settings.yaml）"
            )

    return None


def partition_tools(
    user: Any = None, registry: Optional[Dict[str, Any]] = None
) -> tuple[Dict[str, Any], Dict[str, str]]:
    """把注册表分成"这个用户现在能用"和"不能用以及为什么"。

    意图路由的候选清单、手动模式的可选工具、`GET /tools` 都应该走这里，
    否则模型会挑一个必然失败的工具，用户只看到"选了但没生效"。
    被挡掉的原因同时用于给请求返回 400，而不是只写进服务端日志。

    ``registry`` 让调用方复用已经取到的注册表：``get_tool()`` 会 reload
    tools_factory 并顺带查一次知识库列表，一次请求里不该付两遍。
    """
    if registry is None:
        from chatchat.server.utils import get_tool

        registry = get_tool()

    is_admin = getattr(user, "role", None) == "admin"
    allowed: Dict[str, Any] = {}
    blocked: Dict[str, str] = {}
    for name, tool in registry.items():
        reason = tool_block_reason(name, is_admin=is_admin)
        if reason:
            logger.info(f"工具「{name}」不进入候选：{reason}")
            blocked[name] = reason
            continue
        allowed[name] = tool
    return allowed, blocked


def available_tools(
    user: Any = None, registry: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """`partition_tools` 的便捷入口：只要可用那一份。"""
    return partition_tools(user, registry)[0]
