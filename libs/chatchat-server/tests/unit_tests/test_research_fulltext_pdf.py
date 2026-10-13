"""远程 PDF 全文 provider 的离线测试（打桩 urlopen 与加载器，不联网）。

钉住现场：论文研究「取原文」只报
``未取得全文，已降级为摘要级证据``，后面没有任何原因。两个原因：

- arXiv 的 pdf_url 是 ``https://arxiv.org/pdf/2312.10793v3``，没有 ``.pdf``
  后缀，旧实现按后缀判断后静默返回空列表（连失败原因都不记）；
- 仓库的加载器只吃本地路径，把 URL 直接塞进去必然失败。
"""
import pytest

from chatchat.server.research.fulltext import (
    FullTextError,
    PdfUrlUnitProvider,
    collect_units,
    units_from_documents,
)


class _FakeResponse:
    def __init__(self, body: bytes, content_type: str = "application/pdf"):
        self.body = body
        self.headers = {"content-type": content_type}

    def read(self, limit: int = -1) -> bytes:
        return self.body if limit < 0 else self.body[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@pytest.fixture
def fake_download(monkeypatch):
    """打桩 urllib.request.urlopen，记录请求 URL 并返回可改写的响应。"""
    import urllib.request

    box = {"url": None, "response": _FakeResponse(b"%PDF-1.4 fake body")}

    def fake_urlopen(request, timeout=None):
        box["url"] = request.full_url
        box["timeout"] = timeout
        return box["response"]

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return box


class _FakeLoader:
    def __init__(self, docs):
        self.docs = docs

    def load(self):
        return self.docs


def _doc(text: str):
    return type("_D", (), {"page_content": text, "metadata": {"page": 1}})()


class _Paper:
    def __init__(self, url: str, abstract: str = "摘要内容"):
        self.url = url
        self.abstract = abstract


def test_arxiv_pdf_url_without_suffix_is_accepted(fake_download):
    """回归：arXiv 的 /pdf/2312.10793v3 没有 .pdf 后缀，旧实现直接判“没有 PDF”。"""
    seen = {}

    def factory(path):
        seen["path"] = path
        with open(path, "rb") as handle:
            seen["bytes"] = handle.read()
        return _FakeLoader([_doc("x" * 120)])

    provider = PdfUrlUnitProvider(loader_factory=factory)
    units = provider.fetch(_Paper("https://arxiv.org/pdf/2312.10793v3"))

    assert fake_download["url"] == "https://arxiv.org/pdf/2312.10793v3"
    assert seen["bytes"] == b"%PDF-1.4 fake body"  # 加载器拿到的是下载后的本地文件
    assert units, "解析出内容就必须产出单元"
    import os

    assert not os.path.exists(seen["path"])  # 临时文件已清理


def test_abs_page_url_reports_reason_instead_of_silence(fake_download):
    """abs 页不是 PDF：要记原因，界面才知道为什么降级。"""
    provider = PdfUrlUnitProvider()

    with pytest.raises(FullTextError) as exc:
        provider.fetch(_Paper("https://arxiv.org/abs/2312.10793"))

    assert "链接不是 PDF" in str(exc.value)


def test_missing_url_reports_reason(fake_download):
    with pytest.raises(FullTextError) as exc:
        PdfUrlUnitProvider().fetch(_Paper(""))

    assert "没有可解析的链接" in str(exc.value)


def test_non_pdf_content_type_raises(fake_download):
    fake_download["response"] = _FakeResponse(b"<html>paywall</html>", "text/html")

    with pytest.raises(FullTextError) as exc:
        PdfUrlUnitProvider().fetch(_Paper("https://example.org/paper"))

    assert "不是 PDF" in str(exc.value)


def test_oversize_pdf_is_refused(fake_download, monkeypatch):
    monkeypatch.setattr(PdfUrlUnitProvider, "MAX_PDF_BYTES", 8)

    with pytest.raises(FullTextError) as exc:
        PdfUrlUnitProvider().fetch(_Paper("https://arxiv.org/pdf/1.1"))

    assert "上限" in str(exc.value)


def test_scan_pdf_falls_back_to_ocr(fake_download, monkeypatch):
    """文字层为空（扫描件）才走仓库的 OCR 加载器。"""
    provider = PdfUrlUnitProvider()
    monkeypatch.setattr(provider, "_load_text", lambda path: [_doc("   ")])
    monkeypatch.setattr(
        provider, "_load_ocr", lambda path: [_doc("OCR 出来的正文" * 20)]
    )

    units = provider.fetch(_Paper("https://arxiv.org/pdf/1.2"))

    assert units and "OCR" in units[0].text


def test_collect_units_detail_now_carries_the_reason(fake_download):
    """collect_units 的 detail 不再是一句没有原因的“未取得全文”。"""
    result = collect_units(
        _Paper("https://arxiv.org/abs/2312.10793"),
        providers=[PdfUrlUnitProvider()],
        abstract_provider=None,
    )

    assert result.state == "abstract"
    assert "链接不是 PDF" in result.detail


def test_units_from_documents_skips_tiny_fragments():
    docs = [_doc("页眉"), _doc("正文" * 60)]

    units = units_from_documents(docs, source_kind="pdf")

    assert len(units) == 1
    assert units[0].source_kind == "pdf"
