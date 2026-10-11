"""读取 URL 内容：服务端直接抓取，把正文转成 LLM 好读的文本。

以前这个工具走第三方 `https://r.jina.ai/`：本机直连超时、走代理要 17 秒才返回，
而且配置里的 `timeout` 是字符串 `"10000"`，`requests.get(..., timeout="10000")`
在发任何请求之前就 `ValueError: Timeout value connect was 10000, but it must be
an int, float or None` —— 也就是说它从来没成功过一次，失败还被统一报成
`{"error": "Timeout"}`，看不出真正的原因。
"""
import ipaddress
import re
from socket import gaierror, getaddrinfo
from urllib.parse import urljoin, urlparse

import requests

from chatchat.server.pydantic_v1 import Field
from chatchat.server.utils import get_tool_config
from chatchat.utils import build_logger

from .tools_registry import format_context, regist_tool

from langchain_chatchat.agent_toolkits.all_tools.tool import (
    BaseToolOutput,
)

logger = build_logger()

# 正文上限：一次抓取就把上下文塞满，后面几步的工具结果就没了
MAX_CONTENT_CHARS = 8000
MAX_REDIRECTS = 5
DEFAULT_TIMEOUT = 30
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# 旧正则的字符类里没有 `:`，带端口的地址会在冒号处被截断
_URL_PATTERN = re.compile(r"https?://[^\s<>'\"）】]+")
_TRAILING_PUNCT = ".,;:!?、。）】"


def _extract_url(text: str):
    """从一句话里抠出链接；模型经常把整句话当参数传进来。"""
    match = _URL_PATTERN.search(text or "")
    if not match:
        return None
    return match.group(0).rstrip(_TRAILING_PUNCT)


def _timeout_seconds(raw) -> float:
    """把配置里的 timeout 收敛成秒。

    历史值是字符串 `"10000"`（毫秒），直接交给 requests 会在发请求之前就抛
    `ValueError`。这里兼容数字与字符串，大于 600 的按毫秒解释，最后夹到
    `[1, 120]`，配置写错也不至于让工具崩掉。
    """
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return float(DEFAULT_TIMEOUT)
    if value > 600:
        value /= 1000
    return min(max(value, 1.0), 120.0)


def _reject_reason(url: str):
    """内网/保留地址与非 http 方案一律拒绝，返回 ``None`` 表示放行。

    这个工具任何登录用户都能调（`POST /tools/call` 也能直接点名），
    不做这层就等于把内网探测口开给所有账号。
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"只支持 http/https 地址，收到的是「{parsed.scheme or '无方案'}」"
    host = parsed.hostname
    if not host:
        return "URL 里没有主机名"
    try:
        infos = getaddrinfo(host, None)
    except gaierror:
        return f"域名解析失败：{host}"
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_unspecified
        ):
            return f"{host} 指向内网或保留地址（{address}），已拒绝"
    return None


def _to_text(response) -> str:
    """把响应体转成文本：HTML 转 Markdown，JSON/纯文本原样给。"""
    if response.encoding == "ISO-8859-1":
        # 没声明 charset 时 requests 默认按 ISO-8859-1 解，中文会变乱码
        response.encoding = response.apparent_encoding or "utf-8"

    content_type = response.headers.get("content-type", "")
    if "json" in content_type or "xml" in content_type:
        return response.text
    if "html" in content_type or re.search(
        r"<html|<!doctype html", response.text[:500], re.I
    ):
        from markdownify import markdownify

        return markdownify(response.text)
    return response.text


@regist_tool(title="URL内容阅读")
def url_reader(
        url: str = Field(
            description="The URL to be processed, so that its web content can be made more clear to read. Then provide a detailed description of the content in about 500 words. As structured as possible. ONLY THE LINK SHOULD BE PASSED IN."),
):
    """Use this tool to get the clear content of a URL."""

    tool_config = get_tool_config("url_reader")
    timeout = _timeout_seconds(tool_config.get("timeout"))

    target = _extract_url(url)
    if target is None:
        return BaseToolOutput({"error": "没有从输入里找到 URL，请只传链接本身"})

    # 重定向要一跳一跳查：allow_redirects=True 会让最终地址绕过上面的检查
    for _ in range(MAX_REDIRECTS + 1):
        reason = _reject_reason(target)
        if reason:
            return BaseToolOutput({"error": reason})
        try:
            response = requests.get(
                target,
                timeout=timeout,
                headers={"User-Agent": USER_AGENT},
                allow_redirects=False,
            )
        except requests.exceptions.RequestException as exc:
            # 真实原因带出来。旧写法非 200 一律报 "Timeout"，403/404/DNS 失败
            # 全都看不出来，模型只会原样再试一次。
            logger.warning(f"url_reader 抓取 {target} 失败：{exc}")
            return BaseToolOutput(
                {"error": f"抓取 {target} 失败：{type(exc).__name__}: {exc}"}
            )
        if response.is_redirect or response.is_permanent_redirect:
            location = response.headers.get("location")
            if not location:
                return BaseToolOutput({"error": f"{target} 返回重定向但没有 Location"})
            target = urljoin(target, location)
            continue
        break
    else:
        return BaseToolOutput({"error": f"{target} 重定向超过 {MAX_REDIRECTS} 次"})

    if response.status_code != 200:
        return BaseToolOutput(
            {"error": f"抓取 {target} 失败：HTTP {response.status_code} {response.reason}"}
        )

    text = _to_text(response).strip()
    if len(text) > MAX_CONTENT_CHARS:
        text = text[:MAX_CONTENT_CHARS] + "\n…（正文已截断）"
    if not text:
        return BaseToolOutput({"error": f"{target} 没有可读取的文本内容"})

    return BaseToolOutput(
        {"result": text, "docs": [{"page_content": text, "metadata": {"source": target, "id": ""}}]},
        format=format_context,
    )
