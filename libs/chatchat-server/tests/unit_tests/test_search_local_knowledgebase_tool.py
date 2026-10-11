"""`search_local_knowledgebase` 不再在导入期查库，且知识库名在调用期校验。

以前参数写成 `choices=[kb.kb_name for kb in list_kbs().data]`：装饰器参数在
导入期求值，于是
- `import chatchat.server.agent.tools_factory` 必须有一个能连上的数据库
  （单元测试只能把整个包换成桩才能导入）；
- 每次 `get_tool()` reload 这个包都要再查一遍（而它紧接着还要通过
  update_search_local_knowledgebase_tool() 再查一遍）；
- 新库要等到下一次 reload 才可选。

清单改由 update_search_local_knowledgebase_tool() 在运行期注入 schema，
工具自身在调用时再校验一次名字。
"""
import importlib
import types

import pytest

import chatchat.server.knowledge_base.kb_api as kb_api

# 不能用 `import ...search_local_knowledgebase as slk`：包的 __init__ 里有
# `from .search_local_knowledgebase import search_local_knowledgebase`，
# 包属性被工具对象遮住，拿到的不是模块。
slk = importlib.import_module(
    "chatchat.server.agent.tools_factory.search_local_knowledgebase")


def _kb(name):
    return types.SimpleNamespace(kb_name=name, kb_info=f"{name} 的描述")


class TestNoImportTimeDatabase:
    def test_importing_the_module_does_not_query_the_database(self, monkeypatch):
        monkeypatch.setattr(kb_api, "list_kbs", lambda: pytest.fail("导入期不该查库"))
        try:
            mod = importlib.reload(slk)  # 查了库就会 fail
            assert "choices" not in mod.search_local_knowledgebase.args["database"]
        finally:
            importlib.reload(slk)  # 把模块和注册表恢复成未打桩的版本

class TestDescriptionCarriesTheLiveKbList:
    """schema 里的 choices 没了，模型靠 description 知道有哪些知识库。"""

    def test_update_refreshes_the_description(self, monkeypatch):
        import chatchat.server.db.repository.knowledge_base_repository as kb_repo
        from chatchat.server.utils import update_search_local_knowledgebase_tool

        monkeypatch.setattr(kb_repo, "list_kbs_from_db",
                            lambda: [_kb("samples"), _kb("产品文档")])
        update_search_local_knowledgebase_tool()

        description = slk.search_local_knowledgebase.description
        assert "samples: samples 的描述" in description
        assert "产品文档" in description

    def test_choices_are_injected_into_the_schema_at_runtime(self, monkeypatch):
        # 清单不写在 Field 上（那会逼着导入期查一次数据库），而是在这里注入。
        # BaseTool.args 返回的是 pydantic 缓存的 JSON schema dict，改它确实会留下效果，
        # 所以别把这几行当死代码删掉。
        import chatchat.server.db.repository.knowledge_base_repository as kb_repo
        from chatchat.server.utils import update_search_local_knowledgebase_tool

        monkeypatch.setattr(kb_repo, "list_kbs_from_db",
                            lambda: [_kb("samples"), _kb("产品文档")])
        update_search_local_knowledgebase_tool()

        assert slk.search_local_knowledgebase.args["database"]["choices"] == [
            "samples", "产品文档"]


class TestKnowledgeBaseNameIsValidatedAtCallTime:
    def test_unknown_knowledge_base_lists_the_available_ones(self, monkeypatch):
        monkeypatch.setattr(slk, "list_kbs", lambda: types.SimpleNamespace(
            data=[_kb("samples"), _kb("产品文档")]))

        out = slk.search_local_knowledgebase.invoke(
            {"database": "不存在的库", "query": "怎么登录"})

        assert "不存在的库" in out.data["error"]
        assert "samples" in out.data["error"] and "产品文档" in out.data["error"]

    def test_no_knowledge_base_yet_is_said_plainly(self, monkeypatch):
        monkeypatch.setattr(slk, "list_kbs", lambda: types.SimpleNamespace(data=[]))

        out = slk.search_local_knowledgebase.invoke({"database": "samples", "query": "x"})

        assert "还没有创建任何知识库" in out.data["error"]

    def test_search_failure_is_returned_as_a_readable_error(self, monkeypatch):
        monkeypatch.setattr(slk, "list_kbs", lambda: types.SimpleNamespace(data=[_kb("samples")]))

        def boom(**kwargs):
            raise RuntimeError("embedding 模型未加载")

        monkeypatch.setattr(slk, "search_knowledgebase", boom)

        out = slk.search_local_knowledgebase.invoke({"database": "samples", "query": "x"})

        # 不能配 format_context：它会把 {"error": ...} 吞成"没有找到相关文档"
        assert "embedding 模型未加载" in str(out)

    def test_hits_are_still_formatted_as_context(self, monkeypatch):
        monkeypatch.setattr(slk, "list_kbs", lambda: types.SimpleNamespace(data=[_kb("samples")]))
        monkeypatch.setattr(slk, "search_knowledgebase", lambda **kwargs: {
            "knowledge_base": "samples",
            "docs": [{"page_content": "登录前请先修改默认密码"}],
        })

        out = slk.search_local_knowledgebase.invoke(
            {"database": "samples", "query": "怎么登录"})

        assert "登录前请先修改默认密码" in str(out)
