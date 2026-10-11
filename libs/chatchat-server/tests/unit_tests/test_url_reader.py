"""`url_reader` 的离线测试（不联网：requests 与 DNS 都被替换）。

现场：这个工具从来没成功过。
1. 配置里 `timeout` 是字符串 `"10000"`，`requests.get(..., timeout="10000")`
   在发任何请求之前就 `ValueError: Timeout value connect was 10000, but it must
   be an int, float or None`；
2. 它依赖第三方 `https://r.jina.ai/`，本机直连超时、走代理要 17 秒；
3. 非 200 一律报 `{"error": "Timeout"}`，403/404/DNS 失败全看不出来。

现在改成本地直接抓取，并补上内网地址防护（任何登录用户都能调它）。
"""
import importlib.util
import ipaddress
import sys
import types

import pytest
import requests

# tools_factory 的 __init__ 在导入期查知识库（search_local_knowledgebase 的
# choices 是模块级求值），换成只带 __path__ 的桩后单独导子模块。
# 别的测试文件（test_format_context_docs.py）可能已经装过同样的桩，那时
# find_spec 会撞在 `__spec__ is None` 上，所以先看 sys.modules。
_pkg_name = "chatchat.server.agent.tools_factory"
if _pkg_name not in sys.modules:
    _pkg_spec = importlib.util.find_spec(_pkg_name)
    _pkg_stub = types.ModuleType(_pkg_name)
    _pkg_stub.__path__ = list(_pkg_spec.submodule_search_locations)
    sys.modules[_pkg_name] = _pkg_stub

from chatchat.server.agent.tools_factory import url_reader as mod  # noqa: E402


class FakeResponse:
    def __init__(self, text="", status_code=200, content_type="text/html",
                 location=None, reason="OK", encoding="ISO-8859-1"):
        self.text = text
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        if location is not None:
            self.headers["location"] = location
        self.reason = reason
        self.encoding = encoding
        self.apparent_encoding = "utf-8"
        self.url = "http://example.com/"

    @property
    def is_redirect(self):
        return self.status_code in (301, 302) and "location" in self.headers

    @property
    def is_permanent_redirect(self):
        return self.status_code == 308


class FakeRequests:
    """记录调用、按脚本返回响应或抛异常。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        item = self.script.pop(0) if self.script else FakeResponse()
        if isinstance(item, Exception):
            raise item
        return item


def _patch(monkeypatch, script, addresses=None):
    fake = FakeRequests(script)
    monkeypatch.setattr(mod, "requests", types.SimpleNamespace(
        get=fake.get,
        exceptions=requests.exceptions,
    ))
    def resolve(host, port):
        # 像真实 DNS 那样：IP 字面量原样返回，域名给一个公网地址
        if addresses:
            return [(2, 1, 6, "", (addr, 0)) for addr in addresses]
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return [(2, 1, 6, "", ("93.184.216.34", 0))]
        return [(2, 1, 6, "", (host, 0))]

    monkeypatch.setattr(mod, "getaddrinfo", resolve)
    return fake


class TestTimeout:
    @pytest.mark.parametrize("raw,expected", [
        ("10000", 10.0),   # 历史配置：字符串毫秒
        (10000, 10.0),
        (30, 30.0),
        (0.5, 1.0),        # 下限
        (10 ** 9, 120.0),  # 上限
        (None, 30.0),      # 配置缺失
        ("abc", 30.0),     # 配置写错
    ])
    def test_timeout_is_always_a_usable_number_of_seconds(self, raw, expected):
        assert mod._timeout_seconds(raw) == expected


class TestExtractUrl:
    def test_url_inside_a_sentence(self):
        assert mod._extract_url("请看 https://docs.example.com/a?b=1&c=2 这个页面") == (
            "https://docs.example.com/a?b=1&c=2")

    def test_port_is_not_cut_off(self):
        # 旧正则的字符类里没有冒号，带端口的地址会被截断
        assert mod._extract_url("http://10.0.0.1:8080/metrics") == "http://10.0.0.1:8080/metrics"

    def test_trailing_chinese_punctuation_is_not_part_of_the_url(self):
        assert mod._extract_url("看这个 https://example.com/a。") == "https://example.com/a"

    def test_no_url(self):
        assert mod._extract_url("随便聊两句") is None


class TestInternalAddressGuard:
    @pytest.mark.parametrize("url", [
        "http://127.0.0.1:8081/v1/models",
        "http://10.0.0.5/admin",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "file:///etc/passwd",
    ])
    def test_internal_or_non_http_targets_are_rejected(self, url):
        assert mod._reject_reason(url) is not None

    def test_public_host_is_allowed(self, monkeypatch):
        monkeypatch.setattr(
            mod, "getaddrinfo", lambda host, port: [(2, 1, 6, "", ("93.184.216.34", 0))])

        assert mod._reject_reason("https://example.com/") is None

    def test_domain_resolving_to_an_internal_ip_is_rejected(self, monkeypatch):
        monkeypatch.setattr(
            mod, "getaddrinfo", lambda host, port: [(2, 1, 6, "", ("127.0.0.1", 0))])

        assert mod._reject_reason("http://evil.example.com/") is not None


class TestUrlReaderTool:
    def test_html_body_is_returned_as_text(self, monkeypatch):
        fake = _patch(monkeypatch, [FakeResponse(text="<html><body><h1>标题</h1><p>正文</p></body></html>")])

        out = mod.url_reader.invoke({"url": "请看 https://example.com/ 谢谢"})

        assert "标题" in str(out)
        assert fake.calls[0][0] == "https://example.com/"

    def test_timeout_reaches_requests_as_a_number(self, monkeypatch):
        # 旧行为：timeout="10000" 让 requests 在发请求之前就 ValueError
        fake = _patch(monkeypatch, [FakeResponse(text="<p>ok</p>")])
        monkeypatch.setattr(mod, "get_tool_config", lambda name: {"timeout": "10000"})

        mod.url_reader.invoke({"url": "https://example.com/"})

        assert fake.calls[0][1]["timeout"] == 10.0

    def test_non_200_carries_the_status_code(self, monkeypatch):
        _patch(monkeypatch, [FakeResponse(text="没有这个页面", status_code=404, reason="Not Found")])

        out = mod.url_reader.invoke({"url": "https://example.com/nope"})

        assert "404" in out.data["error"]

    def test_connection_error_carries_the_real_reason(self, monkeypatch):
        _patch(monkeypatch, [requests.exceptions.ConnectTimeout("timed out connecting")])

        out = mod.url_reader.invoke({"url": "https://example.com/"})

        assert "ConnectTimeout" in out.data["error"]
        assert "timed out connecting" in out.data["error"]

    def test_internal_target_is_refused_without_any_request(self, monkeypatch):
        fake = _patch(monkeypatch, [FakeResponse(text="<p>secret</p>")])

        out = mod.url_reader.invoke({"url": "http://127.0.0.1:8081/v1/models"})

        assert "内网" in out.data["error"]
        assert fake.calls == []

    def test_redirect_to_an_internal_address_is_refused(self, monkeypatch):
        _patch(monkeypatch, [
            FakeResponse(status_code=302, location="http://169.254.169.254/"),
            FakeResponse(text="<p>secret</p>"),
        ])

        out = mod.url_reader.invoke({"url": "https://example.com/redirect"})

        assert "内网" in out.data["error"]

    def test_long_body_is_truncated(self, monkeypatch):
        _patch(monkeypatch, [FakeResponse(text="<p>" + ("很长" * 6000) + "</p>")])

        out = mod.url_reader.invoke({"url": "https://example.com/long"})

        assert len(out.data["docs"][0]["page_content"]) < mod.MAX_CONTENT_CHARS + 100
        assert "正文已截断" in out.data["docs"][0]["page_content"]

    def test_empty_body_reports_instead_of_returning_blank(self, monkeypatch):
        _patch(monkeypatch, [FakeResponse(text="   ")])

        out = mod.url_reader.invoke({"url": "https://example.com/empty"})

        assert "没有可读取" in out.data["error"]
