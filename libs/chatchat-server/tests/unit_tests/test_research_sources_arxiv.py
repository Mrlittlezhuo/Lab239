"""ArXiv 论文来源的离线单元测试（打桩 arxiv 包，不联网、不连 arxiv 服务）。

钉住现场：研究运行「检索论文」阶段每个来源都报
``ArXiv 检索失败：'ArxivAPIWrapper' object has no attribute 'load_results'``。
原因是 langchain-community 0.4.x 的 ArxivAPIWrapper 只剩 run/load/lazy_load，
旧实现调的 ``load_results`` 已不存在，来源在发出任何网络请求之前就抛错。
现在直接走 ``arxiv`` 包的结构化结果。
"""
from datetime import datetime

import pytest

from chatchat.server.research.sources import ArxivSource, SourceError


class _FakeAuthor:
    def __init__(self, name):
        self.name = name

    def __str__(self):
        return self.name


class _FakeResult:
    """按 arxiv 2.x 的真实形状构造：没有 result_id / url，只有 entry_id + pdf_url。"""

    def __init__(self, rid="2405.00001"):
        self.entry_id = f"http://arxiv.org/abs/{rid}v2"
        self.title = f" 记忆机制综述 {rid} "
        self.summary = "跨会话保存与检索。"
        self.authors = [_FakeAuthor("张三"), _FakeAuthor("李四")]
        self.published = datetime(2024, 5, 1)
        self.updated = datetime(2024, 6, 1)
        self.pdf_url = f"https://arxiv.org/pdf/{rid}v2"


class _LegacyResult(_FakeResult):
    """arxiv 1.x 老形状（result_id / url 仍在），确认回退路径没坏。"""

    def __init__(self, rid="cs.CL/0101001"):
        super().__init__(rid)
        self.result_id = rid
        self.url = f"http://arxiv.org/abs/{rid}"


@pytest.fixture
def fake_arxiv(monkeypatch):
    """打桩 arxiv.Search / arxiv.Client，返回可改写的假结果列表。"""
    import arxiv

    box = {"results": [_FakeResult()], "raises": None, "query": None, "max_results": None}

    class FakeSearch:
        def __init__(self, query, max_results=None, sort_by=None):
            box["query"] = query
            box["max_results"] = max_results

    class FakeClient:
        def __init__(self, page_size=None, delay_seconds=None, num_retries=None):
            box["page_size"] = page_size

        def results(self, search):
            if box["raises"]:
                raise box["raises"]
            return iter(box["results"])

    monkeypatch.setattr(arxiv, "Search", FakeSearch, raising=False)
    monkeypatch.setattr(arxiv, "Client", FakeClient, raising=False)
    return box


async def test_maps_structured_arxiv_result_to_candidate(fake_arxiv):
    candidates = await ArxivSource().search("memory mechanism", limit=5)

    assert fake_arxiv["query"] == "memory mechanism"
    assert fake_arxiv["max_results"] == 5
    assert len(candidates) == 1
    c = candidates[0]
    assert c.title == "记忆机制综述 2405.00001"  # 去掉了两端空白
    assert c.source == "arxiv"
    # entry_id 带版本后缀，短 id 必须解析出来并去掉 v2（去重按它合并）
    assert c.arxiv_id == "2405.00001"
    assert c.year == 2024
    assert c.authors == ["张三", "李四"]
    assert c.abstract == "跨会话保存与检索。"
    # 全文解析只认以 .pdf 结尾的 url，所以 url 取 pdf_url 而不是 abs 页
    assert c.url == "https://arxiv.org/pdf/2405.00001v2"
    assert c.fulltext_status == "pdf"


async def test_legacy_result_shape_still_maps(fake_arxiv):
    """arxiv 1.x 形状（result_id/url 还在）不能因为新逻辑而退化。"""
    fake_arxiv["results"] = [_LegacyResult()]

    c = (await ArxivSource().search("q"))[0]

    assert c.arxiv_id == "cs.CL/0101001"
    assert c.url == "http://arxiv.org/abs/cs.CL/0101001"


async def test_no_pdf_url_marks_abstract_only(fake_arxiv):
    res = _FakeResult()
    res.pdf_url = None
    fake_arxiv["results"] = [res]

    c = (await ArxivSource().search("q"))[0]

    assert c.fulltext_status == "abstract"
    assert c.url == "http://arxiv.org/abs/2405.00001v2"


async def test_empty_result_returns_empty_list(fake_arxiv):
    fake_arxiv["results"] = []

    assert await ArxivSource().search("q") == []


async def test_network_or_parse_failure_raises_source_error(fake_arxiv):
    """单来源失败只抛 SourceError，研究图据此继续其他来源。"""
    fake_arxiv["raises"] = ConnectionError("arxiv 服务不可达")

    with pytest.raises(SourceError) as exc:
        await ArxivSource().search("q")

    assert "ArXiv 检索失败" in str(exc.value)


async def test_broken_single_row_does_not_drop_the_source(fake_arxiv):
    bad = _FakeResult("bad")
    bad.authors = 123  # 不可迭代：单条解析必然抛错
    fake_arxiv["results"] = [bad, _FakeResult("2405.00002")]

    candidates = await ArxivSource().search("q")

    assert [c.arxiv_id for c in candidates] == ["2405.00002"]


def test_community_wrapper_no_longer_has_load_results():
    """回归根因：旧实现依赖的 load_results 在当前 langchain-community 里不存在。"""
    pytest.importorskip("langchain_community")
    from langchain_community.utilities import ArxivAPIWrapper

    assert not hasattr(ArxivAPIWrapper, "load_results")
