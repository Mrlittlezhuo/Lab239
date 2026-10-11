"""工具失败时必须带出真实原因（离线：requests / 模型 / 数据库都被替换）。

现场（工具审计挖出来的）：
- 高德 key 是配置里的中文占位符时，高德返回 HTTP 200 + `INVALID_USER_KEY`，
  旧代码只看 HTTP 状态码，把这段错误 JSON 当成搜索结果喂给模型；
- `amap_weather` 在 key 无效时直接 `KeyError: 'districts'`；
- `weather_check` 非 200 时 `raise Exception("Failed to retrieve weather: 403")`，
  响应体（到底是 key 无效还是配额用尽）被丢掉；
- `text2sql` 拿着 `mysql+pymysql://用户名:密码@主机地址/数据库名称` 去连库，
  链里任何一步出错都穿透成接口 500；
- `text2images` 是一句裸 `assert model_config`。

接口层现在会把异常原文带进 `msg`（见 test_tool_routes_roles.py），
但工具自己先返回可读错误更有用：模型能据此换路子，用户能据此改配置。
"""
import importlib.util
import sys
import types

import pytest
import requests

# tools_factory 的 __init__ 在导入期查知识库，换成只带 __path__ 的桩后单独导子模块。
_pkg_name = "chatchat.server.agent.tools_factory"
if _pkg_name not in sys.modules:
    _pkg_spec = importlib.util.find_spec(_pkg_name)
    _pkg_stub = types.ModuleType(_pkg_name)
    _pkg_stub.__path__ = list(_pkg_spec.submodule_search_locations)
    sys.modules[_pkg_name] = _pkg_stub

from chatchat.server.agent.tools_factory import (  # noqa: E402
    amap_poi_search,
    amap_weather,
    text2image,
    text2promql,
    text2sql,
    weather_check,
    wolfram,
)


class FakeResponse:
    def __init__(self, json_data=None, status_code=200, text=""):
        self._json = json_data
        self.status_code = status_code
        self.text = text if text else ("" if json_data is None else str(json_data))

    def json(self):
        if self._json is None:
            raise ValueError("not json")
        return self._json


class FakeRequests:
    """按脚本返回响应或抛异常，并记下调用参数。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        item = self.script.pop(0) if self.script else FakeResponse(status_code=500)
        if isinstance(item, Exception):
            raise item
        return item


def _patch_requests(monkeypatch, module, script):
    fake = FakeRequests(script)
    monkeypatch.setattr(module, "requests", types.SimpleNamespace(
        get=fake.get,
        exceptions=requests.exceptions,
    ))
    return fake


def _config(monkeypatch, module, values):
    monkeypatch.setattr(module, "get_tool_config", lambda name: dict(values))


# ---------------------------------------------------------------- 高德

PLACEHOLDER_KEY = "高德地图 API KEY"


class TestAmapPoiSearch:
    def test_placeholder_key_is_reported_as_a_config_problem(self, monkeypatch):
        _config(monkeypatch, amap_poi_search, {"api_key": PLACEHOLDER_KEY})
        fake = _patch_requests(monkeypatch, amap_poi_search, [])

        out = amap_poi_search.amap_poi_search.invoke({"location": "西湖", "types": "景区"})

        assert "api_key" in out.data["error"]
        assert fake.calls == []  # 不该把占位符发出去

    def test_invalid_key_returned_as_http_200_is_not_treated_as_a_result(self, monkeypatch):
        # 高德鉴权失败：HTTP 200 + status=0
        _config(monkeypatch, amap_poi_search, {"api_key": "bad-key"})
        _patch_requests(monkeypatch, amap_poi_search, [FakeResponse(json_data={
            "status": "0", "info": "INVALID_USER_KEY", "infocode": "10001",
        })])

        out = amap_poi_search.amap_poi_search.invoke({"location": "西湖", "types": "景区"})

        assert "INVALID_USER_KEY" in out.data["error"]
        assert "api_key" in out.data["error"]

    def test_network_failure_names_the_exception(self, monkeypatch):
        _config(monkeypatch, amap_poi_search, {"api_key": "real-key"})
        _patch_requests(monkeypatch, amap_poi_search,
                        [requests.exceptions.ConnectionError("名字解析暂时失败")])

        out = amap_poi_search.amap_poi_search.invoke({"location": "西湖", "types": "景区"})

        assert "ConnectionError" in out.data["error"]

    def test_non_json_body_is_reported(self, monkeypatch):
        _config(monkeypatch, amap_poi_search, {"api_key": "real-key"})
        _patch_requests(monkeypatch, amap_poi_search,
                        [FakeResponse(status_code=200, text="<html>网关错误</html>")])

        out = amap_poi_search.amap_poi_search.invoke({"location": "西湖", "types": "景区"})

        assert "不是 JSON" in out.data["error"]

    def test_success_returns_the_payload(self, monkeypatch):
        _config(monkeypatch, amap_poi_search, {"api_key": "real-key"})
        fake = _patch_requests(monkeypatch, amap_poi_search,
                               [FakeResponse(json_data={"status": "1", "pois": [{"name": "西湖"}]})])

        out = amap_poi_search.amap_poi_search.invoke({"location": "西湖", "types": "景区"})

        assert out.data["pois"][0]["name"] == "西湖"
        assert fake.calls[0][1]["timeout"] == amap_poi_search.TIMEOUT


class TestAmapWeather:
    def test_missing_districts_is_not_a_keyerror(self, monkeypatch):
        _config(monkeypatch, amap_weather, {"api_key": "real-key"})
        _patch_requests(monkeypatch, amap_weather,
                        [FakeResponse(json_data={"status": "1", "count": "0"})])

        out = amap_weather.amap_weather.invoke({"city": "不存在的城市"})

        assert "不存在的城市" in out.data["error"]

    def test_adcode_and_weather_are_requested_in_order(self, monkeypatch):
        _config(monkeypatch, amap_weather, {"api_key": "real-key"})
        fake = _patch_requests(monkeypatch, amap_weather, [
            FakeResponse(json_data={"status": "1", "districts": [{"adcode": "330100"}]}),
            FakeResponse(json_data={"status": "1", "forecasts": [{"city": "杭州市"}]}),
        ])

        out = amap_weather.amap_weather.invoke({"city": "杭州"})

        assert out.data["forecasts"][0]["city"] == "杭州市"
        assert [call[0] for call in fake.calls] == [
            amap_weather.BASE_DISTRICT_URL, amap_weather.BASE_WEATHER_URL,
        ]

    def test_second_call_failure_is_attributed_to_the_weather_api(self, monkeypatch):
        _config(monkeypatch, amap_weather, {"api_key": "real-key"})
        _patch_requests(monkeypatch, amap_weather, [
            FakeResponse(json_data={"status": "1", "districts": [{"adcode": "330100"}]}),
            requests.exceptions.Timeout("读超时"),
        ])

        out = amap_weather.amap_weather.invoke({"city": "杭州"})

        assert "Timeout" in out.data["error"]

    def test_placeholder_key_blocks_the_request(self, monkeypatch):
        _config(monkeypatch, amap_weather, {"api_key": PLACEHOLDER_KEY})
        fake = _patch_requests(monkeypatch, amap_weather, [])

        out = amap_weather.amap_weather.invoke({"city": "杭州"})

        assert "amap.api_key" in out.data["error"]
        assert fake.calls == []


# ---------------------------------------------------------------- 心知天气

class TestWeatherCheck:
    def test_empty_key_is_reported_as_a_config_problem(self, monkeypatch):
        _config(monkeypatch, weather_check, {"api_key": ""})
        fake = _patch_requests(monkeypatch, weather_check, [])

        out = weather_check.weather_check.invoke({"city": "厦门"})

        assert "weather_check.api_key" in out.data["error"]
        assert fake.calls == []

    def test_403_carries_the_response_body(self, monkeypatch):
        # 403 是 key 无效还是配额用尽，只有响应体里写着
        _config(monkeypatch, weather_check, {"api_key": "real-key"})
        _patch_requests(monkeypatch, weather_check, [FakeResponse(
            status_code=403, text='{"body":{"code":403,"message":"Forbidden"}}')])

        out = weather_check.weather_check.invoke({"city": "厦门"})

        assert "403" in out.data["error"]
        assert "Forbidden" in out.data["error"]

    def test_missing_results_key_does_not_raise(self, monkeypatch):
        _config(monkeypatch, weather_check, {"api_key": "real-key"})
        _patch_requests(monkeypatch, weather_check, [FakeResponse(json_data={"results": []})])

        out = weather_check.weather_check.invoke({"city": "厦门"})

        assert "厦门" in out.data["error"]

    def test_success(self, monkeypatch):
        _config(monkeypatch, weather_check, {"api_key": "real-key"})
        _patch_requests(monkeypatch, weather_check, [FakeResponse(json_data={
            "results": [{"now": {"temperature": "26", "text": "晴"}}]})])

        out = weather_check.weather_check.invoke({"city": "厦门"})

        assert out.data == {"temperature": "26", "description": "晴"}


# ---------------------------------------------------------------- WolframAlpha

class TestWolfram:
    def test_missing_appid_is_reported_as_a_config_problem(self, monkeypatch):
        _config(monkeypatch, wolfram, {"appid": ""})

        out = wolfram.wolfram.invoke({"query": "1+1"})

        assert "wolfram.appid" in out.data["error"]

    def test_missing_package_names_the_package(self, monkeypatch):
        # sys.modules 里放 None 会让 `from x import y` 抛 ImportError
        _config(monkeypatch, wolfram, {"appid": "real-appid"})
        monkeypatch.setitem(sys.modules, "langchain_community.utilities.wolfram_alpha", None)

        out = wolfram.wolfram.invoke({"query": "1+1"})

        assert "pip install wolframalpha" in out.data["error"]

    def test_runtime_failure_is_not_an_unhandled_exception(self, monkeypatch):
        class Wrapper:
            def __init__(self, **kwargs):
                pass

            def run(self, query):
                raise RuntimeError("appid 已过期")

        fake = types.ModuleType("langchain_community.utilities.wolfram_alpha")
        fake.WolframAlphaAPIWrapper = Wrapper
        _config(monkeypatch, wolfram, {"appid": "real-appid"})
        monkeypatch.setitem(sys.modules, "langchain_community.utilities.wolfram_alpha", fake)

        out = wolfram.wolfram.invoke({"query": "1+1"})

        assert "appid 已过期" in out.data["error"]

    def test_success(self, monkeypatch):
        class Wrapper:
            def __init__(self, **kwargs):
                pass

            def run(self, query):
                return "2"

        fake = types.ModuleType("langchain_community.utilities.wolfram_alpha")
        fake.WolframAlphaAPIWrapper = Wrapper
        _config(monkeypatch, wolfram, {"appid": "real-appid"})
        monkeypatch.setitem(sys.modules, "langchain_community.utilities.wolfram_alpha", fake)

        out = wolfram.wolfram.invoke({"query": "1+1"})

        assert out.data == "2"


# ---------------------------------------------------------------- text2sql

PLACEHOLDER_DSN = "mysql+pymysql://用户名:密码@主机地址/数据库名称"


def _sql_config(**overrides):
    config = {
        "model_name": "qwen-plus",
        "top_k": 50,
        "return_intermediate_steps": False,
        "sqlalchemy_connect_str": PLACEHOLDER_DSN,
        "read_only": False,
        "table_names": [],
        "table_comments": {},
    }
    config.update(overrides)
    return config


class TestText2Sql:
    def test_placeholder_connect_string_is_reported_as_a_config_problem(self, monkeypatch):
        _config(monkeypatch, text2sql, _sql_config())
        monkeypatch.setattr(text2sql, "SQLDatabase", types.SimpleNamespace(
            from_uri=lambda uri: pytest.fail("占位连接串不该去连库")))

        out = text2sql.text2sql.invoke({"query": "有多少用户"})

        assert "text2sql.sqlalchemy_connect_str" in out.data

    def test_connection_failure_names_the_exception_and_the_driver_hint(self, monkeypatch):
        def boom(uri):
            raise ModuleNotFoundError("No module named 'pymysql'")

        _config(monkeypatch, text2sql, _sql_config(sqlalchemy_connect_str="mysql+pymysql://u:p@127.0.0.1:3306/db"))
        monkeypatch.setattr(text2sql, "SQLDatabase", types.SimpleNamespace(from_uri=boom))

        out = text2sql.text2sql.invoke({"query": "有多少用户"})

        assert "ModuleNotFoundError" in out.data
        assert "pymysql" in out.data

    def test_unavailable_model_is_reported_instead_of_crashing_the_chain(self, monkeypatch):
        _config(monkeypatch, text2sql, _sql_config(sqlalchemy_connect_str="sqlite:///x.db"))
        monkeypatch.setattr(text2sql, "SQLDatabase", types.SimpleNamespace(
            from_uri=lambda uri: types.SimpleNamespace(_engine=None)))
        monkeypatch.setattr("chatchat.server.utils.get_ChatOpenAI", lambda **kwargs: None)

        out = text2sql.text2sql.invoke({"query": "有多少用户"})

        assert "qwen-plus" in out.data

    def test_chain_failure_is_returned_as_text(self, monkeypatch):
        def chain(*args, **kwargs):
            raise RuntimeError("Table 'users' doesn't exist")

        _config(monkeypatch, text2sql, _sql_config(
            sqlalchemy_connect_str="sqlite:///x.db", table_names=["users"]))
        monkeypatch.setattr(text2sql, "SQLDatabase", types.SimpleNamespace(
            from_uri=lambda uri: types.SimpleNamespace(_engine=None)))
        monkeypatch.setattr("chatchat.server.utils.get_ChatOpenAI", lambda **kwargs: object())
        monkeypatch.setattr(text2sql, "SQLDatabaseChain", types.SimpleNamespace(from_llm=chain))

        out = text2sql.text2sql.invoke({"query": "有多少用户"})

        assert "Table 'users' doesn't exist" in out.data

    def test_result_without_a_result_key_does_not_raise_keyerror(self, monkeypatch):
        _config(monkeypatch, text2sql, _sql_config(
            sqlalchemy_connect_str="sqlite:///x.db", table_names=["users"]))
        monkeypatch.setattr(text2sql, "SQLDatabase", types.SimpleNamespace(
            from_uri=lambda uri: types.SimpleNamespace(_engine=None)))
        monkeypatch.setattr("chatchat.server.utils.get_ChatOpenAI", lambda **kwargs: object())
        monkeypatch.setattr(text2sql, "SQLDatabaseChain", types.SimpleNamespace(
            from_llm=lambda *a, **k: types.SimpleNamespace(invoke=lambda payload: {"sql": "SELECT 1"})))

        out = text2sql.text2sql.invoke({"query": "有多少用户"})

        assert "没有返回结果" in out.data


# ---------------------------------------------------------------- text2promql

class TestText2Promql:
    def test_empty_endpoint_is_reported_as_a_config_problem(self, monkeypatch):
        _config(monkeypatch, text2promql, {"prometheus_endpoint": "", "username": "", "password": ""})

        out = text2promql.text2promql.invoke({"query": "up 有多少"})

        assert "text2promql.prometheus_endpoint" in out.data

    def test_unavailable_model_is_reported(self, monkeypatch):
        _config(monkeypatch, text2promql, {
            "prometheus_endpoint": "http://127.0.0.1:9090", "username": "", "password": ""})
        monkeypatch.setattr(text2promql, "get_ChatOpenAI", lambda **kwargs: None)

        out = text2promql.text2promql.invoke({"query": "up 有多少"})

        assert "默认模型" in out.data

    def test_unreachable_prometheus_names_the_exception_and_the_url(self, monkeypatch):
        def boom(url, **kwargs):
            raise requests.exceptions.ConnectionError("Connection refused")

        monkeypatch.setattr(text2promql, "requests", types.SimpleNamespace(
            get=boom, exceptions=requests.exceptions))

        out = text2promql.execute_promql_request(
            "http://127.0.0.1:9090/api/v1/query", {"query": "up"}, None, "query?query=up")

        assert "ConnectionError" in out
        assert "http://127.0.0.1:9090/api/v1/query" in out

    def test_llm_failure_is_returned_as_text(self, monkeypatch):
        _config(monkeypatch, text2promql, {
            "prometheus_endpoint": "http://127.0.0.1:9090", "username": "", "password": ""})

        class FailingLLM:
            # 不是 Runnable，会被 langchain 包成 RunnableLambda；调用时才炸
            def __call__(self, _input):
                raise RuntimeError("模型服务未启动")

        monkeypatch.setattr(text2promql, "get_ChatOpenAI", lambda **kwargs: FailingLLM())

        out = text2promql.text2promql.invoke({"query": "up 有多少"})

        assert "模型服务未启动" in out.data


# ---------------------------------------------------------------- text2images

class TestText2Images:
    def test_unconfigured_model_is_not_a_bare_assert(self, monkeypatch):
        _config(monkeypatch, text2image, {"model": "sd-turbo", "size": "1024x1024"})
        monkeypatch.setattr(text2image, "get_model_info", lambda name: None)

        out = text2image.text2images.invoke({"prompt": "一间山中小屋"})

        assert "sd-turbo" in out.data["error"]
        assert "text2image_models" in out.data["error"]

    def test_api_failure_names_the_model_and_the_endpoint(self, monkeypatch):
        class Client:
            def __init__(self, **kwargs):
                pass

            images = types.SimpleNamespace(
                generate=lambda **kwargs: pytest.fail("不该走到这里"))

        def raising_client(**kwargs):
            raise RuntimeError("api_key 无效")

        _config(monkeypatch, text2image, {"model": "sd-turbo", "size": "1024x1024"})
        monkeypatch.setattr(text2image, "get_model_info", lambda name: {
            "model_name": "sd-turbo", "api_base_url": "https://api.example.com/v1",
            "api_key": "sk-x"})
        monkeypatch.setattr(text2image.openai, "Client", raising_client)

        out = text2image.text2images.invoke({"prompt": "一间山中小屋"})

        assert "sd-turbo" in out.data["error"]
        assert "https://api.example.com/v1" in out.data["error"]

    def test_empty_response_is_reported(self, monkeypatch):
        class Client:
            def __init__(self, **kwargs):
                pass

            images = types.SimpleNamespace(generate=lambda **kwargs: types.SimpleNamespace(data=[]))

        _config(monkeypatch, text2image, {"model": "sd-turbo", "size": "1024x1024"})
        monkeypatch.setattr(text2image, "get_model_info", lambda name: {
            "model_name": "sd-turbo", "api_base_url": "https://api.example.com/v1",
            "api_key": "sk-x"})
        monkeypatch.setattr(text2image.openai, "Client", Client)

        out = text2image.text2images.invoke({"prompt": "一间山中小屋"})

        assert "没有返回图片" in out.data["error"]

    @pytest.mark.parametrize("requested,configured,expected", [
        ("1024x1024", "256*256", "1024x1024"),
        # 历史配置里的 "256*256" 既不是接口支持的尺寸，工具也从不读它
        (None, "256*256", "1024x1024"),
        (None, "768*1344", "768x1344"),
        ("768X1344", None, "768x1344"),
        ("512x512", None, "1024x1024"),   # 不支持的尺寸退回默认值
        (None, None, "1024x1024"),
    ])
    def test_size_is_normalized_instead_of_failing_validation(self, requested, configured, expected):
        assert text2image._resolve_size(requested, configured) == expected
