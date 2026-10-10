"""tests/api 手动集成测试的登录辅助。

B5 起 /tools、/chat、/knowledge_base、MCP 等接口要求 Bearer Token。
这些测试面向真实运行的服务器，不属于 CI；用户名/密码从环境变量读取：
CHATCHAT_TEST_USERNAME / CHATCHAT_TEST_PASSWORD（默认 admin/admin123）。
"""
import os

import requests

from chatchat.server.utils import api_address


def login_headers(base_url: str | None = None) -> dict:
    base = base_url or api_address()
    username = os.environ.get("CHATCHAT_TEST_USERNAME", "admin")
    password = os.environ.get("CHATCHAT_TEST_PASSWORD", "admin123")
    resp = requests.post(
        f"{base}/auth/login",
        json={"username": username, "password": password},
        timeout=10,
    )
    resp.raise_for_status()
    token = resp.json()["token"]
    return {"Authorization": f"Bearer {token}"}
