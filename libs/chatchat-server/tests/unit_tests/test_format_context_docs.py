"""``format_context`` 对 ``docs`` 三种形状的兼容性（离线）。

现场：联网搜索已经拿到真实结果，工具却报
`1 validation error for DocumentWithVSId ... Input should be a valid dictionary or
instance of DocumentWithVSId`。原因是渲染时先做了一次
`DocumentWithVSId.model_validate(doc)`，而 pydantic v2 只认 dict 或
`DocumentWithVSId` 本身：

- `search_internet` 放进来的是 langchain 的 `Document`（父类）→ 被拒，整步 failed；
- `url_reader` 放的是裸 dict → 侥幸通过；
- `search_local_knowledgebase` 放的是 `DocumentWithVSId` → 通过。

而这次校验本来毫无用处：循环里只取了 `doc.page_content`。
"""
import importlib.util
import sys
import types

# `chatchat.server.agent.tools_factory` 的 __init__ 在导入期就会查库
# （search_local_knowledgebase 的 choices=[kb.kb_name for kb in list_kbs().data] 是
# 模块级求值），单测环境里没有迁移过的表，直接 import 会在收集阶段就炸。
# 这里把包本体换成一个只带 __path__ 的桩，再单独导入真正要测的子模块。
_pkg_spec = importlib.util.find_spec("chatchat.server.agent.tools_factory")
_pkg_stub = types.ModuleType("chatchat.server.agent.tools_factory")
_pkg_stub.__path__ = list(_pkg_spec.submodule_search_locations)
sys.modules.setdefault("chatchat.server.agent.tools_factory", _pkg_stub)

from langchain_core.documents import Document  # noqa: E402

from chatchat.server.agent.tools_factory.tools_registry import format_context  # noqa: E402
from chatchat.server.knowledge_base.model.kb_document_model import DocumentWithVSId  # noqa: E402
from langchain_chatchat.agent_toolkits.all_tools.tool import BaseToolOutput  # noqa: E402

NOTE = "苏州大学本部假山迷宫打卡路线"
OTHER = "天赐庄校区老校门"


def _render(docs):
    return format_context(BaseToolOutput({"docs": docs, "search_engine": "duckduckgo"}))


class TestDocShapes:
    def test_plain_langchain_document_is_rendered(self):
        # search_internet 的真实形状：Document(page_content=..., metadata=...)
        docs = [Document(page_content=NOTE, metadata={"source": "https://x/1", "filename": "t"})]

        assert NOTE in _render(docs)

    def test_dict_docs_are_rendered(self):
        # url_reader 的真实形状：直接塞 dict
        assert NOTE in _render([{"page_content": NOTE, "metadata": {"source": "https://x"}}])

    def test_document_with_vs_id_is_rendered(self):
        # search_local_knowledgebase 的真实形状
        doc = DocumentWithVSId(page_content=NOTE, metadata={"source": "kb"}, id="abc", score=1.2)

        assert NOTE in _render([doc])

    def test_mixed_shapes_all_rendered(self):
        out = _render([
            Document(page_content=NOTE, metadata={}),
            {"page_content": OTHER, "metadata": {}},
            DocumentWithVSId(page_content="第三条", metadata={}),
        ])

        assert NOTE in out and OTHER in out and "第三条" in out


class TestEmptyResults:
    def test_no_docs_falls_back_to_retry_hint(self):
        assert _render([]) == "没有找到相关文档,请更换关键词重试"

    def test_blank_page_content_falls_back_to_retry_hint(self):
        # 旧代码把空正文照样拼进去，返回 "\n\n"，模型收到的是空字符串却以为有结果
        out = _render([Document(page_content="", metadata={}),
                       Document(page_content="   \n ", metadata={})])

        assert out == "没有找到相关文档,请更换关键词重试"

    def test_missing_docs_key_does_not_raise(self):
        # url_reader 的失败分支给的是 {"error": ...}，没有 docs
        assert format_context(BaseToolOutput({"error": "Timeout"})) == "没有找到相关文档,请更换关键词重试"

    def test_none_docs_does_not_raise(self):
        assert format_context(BaseToolOutput({"docs": None})) == "没有找到相关文档,请更换关键词重试"


class TestOutputShape:
    def test_each_document_is_followed_by_a_blank_line(self):
        out = _render([{"page_content": NOTE, "metadata": {}}, {"page_content": OTHER, "metadata": {}}])

        assert out == f"{NOTE}\n\n{OTHER}\n\n"

    def test_blank_documents_are_dropped_not_joined(self):
        out = _render([{"page_content": NOTE, "metadata": {}}, {"page_content": "", "metadata": {}}])

        assert out == f"{NOTE}\n\n"
