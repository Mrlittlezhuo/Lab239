"""LangChain 1.x 迁移中容易遗漏的动态导入回归测试。"""

from langchain_text_splitters import RecursiveCharacterTextSplitter

from chatchat.server.knowledge_base.utils import make_text_splitter


def test_builtin_text_splitter_uses_standalone_package():
    """内置分词器应从独立包加载，不能回退到已删除的旧模块。"""
    make_text_splitter.cache_clear()
    splitter = make_text_splitter("RecursiveCharacterTextSplitter", 100, 10)

    assert isinstance(splitter, RecursiveCharacterTextSplitter)
