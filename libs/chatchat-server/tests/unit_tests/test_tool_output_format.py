"""``BaseToolOutput.format`` 既接受字符串哨兵也接受渲染函数（回归测试，离线）。

背景：``search_internet`` / ``search_local_knowledgebase`` 以
``format=format_context``（一个函数）构造 ``BaseToolOutput``。旧代码有两处
叠加缺陷：

- 字段声明为 ``format: str``，pydantic 拒绝函数 → ``format str type
  expected (type=type_error.str)``，联网/KB 工具一执行就整步失败；
- ``__str__`` 检查了从未被赋值的 ``_format_callable``，即使能构造也会退化为
  ``str(data)``，把原始 dict/Document 乱码喂给模型。

修复后：字段放宽为 ``str | Callable | None``，``__str__`` 在 ``format`` 为
函数时真正调用它渲染。本测试只用 ``BaseToolOutput`` 与一个普通函数，不导入
``kb_doc_api`` / 不触真实数据库。
"""
from __future__ import annotations

from langchain_chatchat.agent_toolkits.all_tools.tool import BaseToolOutput


class TestBaseToolOutputFormat:
    def test_callable_format_accepted_and_rendered(self):
        """format 为函数：可构造，且 str() 真正调用该函数渲染。"""
        def render(out: BaseToolOutput) -> str:
            return "已渲染：" + str(out.data.get("k"))

        out = BaseToolOutput({"k": "v"}, format=render)
        assert str(out) == "已渲染：v"

    def test_json_format_renders_json(self):
        """format='json'：走 json 分支。"""
        out = BaseToolOutput({"a": 1}, format="json")
        assert '"a": 1' in str(out)

    def test_none_format_falls_back_to_str_data(self):
        """不传 format（默认 None）：退化为 str(data)，不报错。"""
        out = BaseToolOutput({"a": 1})
        assert str(out) == str({"a": 1})

    def test_plain_string_format_falls_back_to_str_data(self):
        """format 为普通字符串（非 'json'、非函数）：退化为 str(data)。"""
        out = BaseToolOutput({"a": 1}, format="not-json")
        assert str(out) == str({"a": 1})

    def test_field_type_accepts_callable(self):
        """字段声明本身必须允许可调用对象，而非仅限 str。"""
        field = BaseToolOutput.__fields__["format"]
        # 用可调用对象实例化不应抛 ValidationError
        BaseToolOutput({"x": 1}, format=lambda o: "ok")
        assert field.default is None