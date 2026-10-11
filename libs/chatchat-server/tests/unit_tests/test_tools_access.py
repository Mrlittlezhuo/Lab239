"""工具准入策略（离线）。

注册表只说明"代码里有这个工具"，不说明"这台机器配好了、这个用户能用"：
- `tool_settings.yaml` 里的 `use` 开关后端从来不读，未配置的工具照样进候选清单；
- `shell` 是 `subprocess.run(shell=True)` 直通，以前对所有登录用户开放；
- 配置里留着中文占位符（`api_key: 高德地图 API KEY`、
  `sqlalchemy_connect_str: mysql+pymysql://用户名:密码@主机地址/数据库名称`）时，
  工具把占位符原样发出去，错误里看不出是配置问题。
"""
import pytest

from chatchat.server.agent import tools_access as mod


class FakeUser:
    def __init__(self, role):
        self.role = role


ADMIN = FakeUser("admin")
USER = FakeUser("user")


@pytest.fixture
def config(monkeypatch):
    """把配置读取换成可控字典（Settings.tool_settings 是懒加载 property）。"""
    table: dict = {}
    monkeypatch.setattr(mod, "_section", lambda name: table.get(name, {}))
    return table


def test_shell_is_off_by_default(config):
    reason = mod.tool_block_reason("shell", is_admin=True)

    assert reason is not None
    assert "默认关闭" in reason


def test_shell_needs_both_the_switch_and_an_admin(config):
    config["shell_tool"] = {"use": True}

    assert mod.tool_block_reason("shell", is_admin=True) is None
    reason = mod.tool_block_reason("shell", is_admin=False)

    assert "只允许管理员" in reason


def test_placeholder_api_key_blocks_the_amap_tools(config):
    config["amap"] = {"api_key": "高德地图 API KEY"}

    for name in ("amap_poi_search", "amap_weather"):
        reason = mod.tool_block_reason(name, is_admin=False)
        assert reason is not None
        assert "amap.api_key" in reason


def test_real_api_key_unblocks_the_amap_tools(config):
    config["amap"] = {"api_key": "f1e2d3c4b5a6978867564433221100ff"}

    assert mod.tool_block_reason("amap_poi_search", is_admin=False) is None


@pytest.mark.parametrize("name,section,field", [
    ("weather_check", "weather_check", "api_key"),
    ("wolfram", "wolfram", "appid"),
    ("text2sql", "text2sql", "sqlalchemy_connect_str"),
])
def test_missing_credentials_name_the_config_item(name, section, field, config):
    config[section] = {field: ""}

    reason = mod.tool_block_reason(name, is_admin=False)

    assert f"{section}.{field}" in reason


def test_placeholder_connection_string_is_treated_as_unconfigured(config):
    config["text2sql"] = {"sqlalchemy_connect_str": "mysql+pymysql://用户名:密码@主机地址/库名"}

    assert mod.tool_block_reason("text2sql", is_admin=False) is not None


def test_text2images_needs_a_configured_model(config, monkeypatch):
    config["text2images"] = {"model": "sd-turbo"}
    monkeypatch.setattr("chatchat.server.utils.get_model_info", lambda *a, **k: {})

    reason = mod.tool_block_reason("text2images", is_admin=False)

    assert "sd-turbo" in reason
    assert "text2image_models" in reason


def test_text2images_allowed_once_the_model_is_configured(config, monkeypatch):
    config["text2images"] = {"model": "sd-turbo"}
    monkeypatch.setattr(
        "chatchat.server.utils.get_model_info",
        lambda *a, **k: {"model_name": "sd-turbo"},
    )

    assert mod.tool_block_reason("text2images", is_admin=False) is None


def test_tools_without_config_requirements_are_allowed(config):
    assert mod.tool_block_reason("calculate", is_admin=False) is None
    assert mod.tool_block_reason("search_internet", is_admin=False) is None


def _fake_registry(monkeypatch, names):
    monkeypatch.setattr(
        "chatchat.server.utils.get_tool",
        lambda *a, **k: {n: object() for n in names},
    )


def test_partition_splits_the_registry_and_keeps_the_reasons(monkeypatch, config):
    config["shell_tool"] = {"use": False}
    config["amap"] = {"api_key": "高德地图 API KEY"}
    _fake_registry(monkeypatch, ["calculate", "shell", "amap_weather"])

    allowed, blocked = mod.partition_tools(USER)

    assert list(allowed) == ["calculate"]
    assert set(blocked) == {"shell", "amap_weather"}
    assert "默认关闭" in blocked["shell"]


def test_partition_hides_shell_from_non_admins_even_when_switched_on(monkeypatch, config):
    config["shell_tool"] = {"use": True}
    _fake_registry(monkeypatch, ["calculate", "shell"])

    as_user, blocked_for_user = mod.partition_tools(USER)
    as_admin, _ = mod.partition_tools(ADMIN)

    assert "shell" not in as_user
    assert "只允许管理员" in blocked_for_user["shell"]
    assert "shell" in as_admin


def test_available_tools_is_the_allowed_half(monkeypatch, config):
    _fake_registry(monkeypatch, ["calculate", "shell"])
    config["shell_tool"] = {"use": False}

    assert list(mod.available_tools(USER)) == ["calculate"]
