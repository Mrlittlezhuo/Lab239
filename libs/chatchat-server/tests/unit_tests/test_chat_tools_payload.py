"""聊天接口 ``tools`` 请求体形状的离线测试。

现场：手动模式勾选工具必 500。前端发的是名字数组（``frontend/src/api/chat.ts``
里 ``tools?: string[]``），后端写的是 ``x["function"]["name"]``，
于是 ``TypeError: string indices must be integers``。
"""
import sys
import types

import pytest
from fastapi import HTTPException

# chat_routes 的导入链（kb_chat → search_local_knowledgebase）在模块加载时会读
# 一次真实知识库数据库，离线测试提供不了，先换成桩（与
# test_conversation_isolation.py 同样的做法）。
_kb_stub = types.ModuleType("chatchat.server.chat.kb_chat")
_kb_stub.kb_chat = lambda *args, **kwargs: None
sys.modules["chatchat.server.chat.kb_chat"] = _kb_stub

from chatchat.server.api_server.chat_routes import _resolve_tool_names  # noqa: E402


REGISTRY = {"calculate": object(), "search_internet": object()}


class TestResolveToolNames:
    def test_name_array_from_frontend(self):
        assert _resolve_tool_names(["calculate"], REGISTRY) == ["calculate"]

    def test_openai_function_shape(self):
        tools = [
            {
                "type": "function",
                "function": {"name": "search_internet", "description": "x"},
            }
        ]

        assert _resolve_tool_names(tools, REGISTRY) == ["search_internet"]

    def test_bare_name_dict_shape(self):
        assert _resolve_tool_names([{"name": "calculate"}], REGISTRY) == ["calculate"]

    def test_mixed_shapes(self):
        tools = ["calculate", {"function": {"name": "search_internet"}}]

        assert _resolve_tool_names(tools, REGISTRY) == [
            "calculate",
            "search_internet",
        ]

    def test_no_tools_gives_empty_config(self):
        assert _resolve_tool_names(None, REGISTRY) == []
        assert _resolve_tool_names([], REGISTRY) == []

    def test_unknown_tool_gives_400_with_available_list(self):
        # 旧行为：这一项不会被换成 dict，留到 x["function"]["name"] 变成 500
        with pytest.raises(HTTPException) as exc:
            _resolve_tool_names(["no_such_tool"], REGISTRY)

        assert exc.value.status_code == 400
        assert "no_such_tool" in exc.value.detail
        assert "calculate" in exc.value.detail

    def test_unusable_shape_gives_400_not_type_error(self):
        with pytest.raises(HTTPException) as exc:
            _resolve_tool_names([{"function": {}}], REGISTRY)

        assert exc.value.status_code == 400

    def test_empty_registry_still_reports_400(self):
        with pytest.raises(HTTPException) as exc:
            _resolve_tool_names(["calculate"], {})

        assert exc.value.status_code == 400
