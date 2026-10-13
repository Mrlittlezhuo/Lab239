"""论文原文获取与切分（任务 018 阶段 A）。

目的：把候选论文变成**带稳定定位、可逐字核对**的原文单元，供证据提取
引用。三条底线：

- **降级不伪造**：拿不到全文时只用摘要，产出 ``unit_kind='abstract'``
  的单元且 ``page is None``；绝不给摘要级证据编页码或章节；
- **复用既有能力**：远程 PDF 先下载到临时文件，再走仓库已有的文档加载器 ——
  有文字层用 ``PyPDFLoader``（快），扫描件回落
  ``chatchat.server.file_rag.document_loaders.RapidOCRPDFLoader``（与知识库
  同一套 OCR）；本地全文走公共知识库检索，不引入第二套 PDF 框架、不新增依赖；
- **定位稳定**：``block_index`` 按文档顺序连续编号，配合 ``page``/
  ``section`` 与内容哈希构成可重现定位；同一论文重跑得到同样的槽位。

生产路径会联网（arXiv PDF）或读公共知识库；测试一律注入假 provider，
不联网、不读真实知识库。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from chatchat.utils import build_logger

logger = build_logger()

#: 单个原文单元的最大字符数（超过则按段落继续切，保证可核对粒度）
MAX_UNIT_CHARS = 2000
#: 过短的碎片（页眉页码等）不入库，避免噪声单元
MIN_UNIT_CHARS = 40
#: 单篇论文最多入库的单元数（防止超长 PDF 把一次提取撑爆）
MAX_UNITS_PER_PAPER = 200

_ANY_WS_RE = re.compile(r"\s+")
#: 章节标题行：可选编号 + 关键词，整行短且不以句末标点结尾
_HEADING_KEYWORDS = (
    "abstract", "introduction", "related work", "background", "method", "methods",
    "methodology", "approach", "experiment", "experiments", "evaluation", "result",
    "results", "discussion", "conclusion", "conclusions", "limitation", "limitations",
    "references", "appendix", "摘要", "引言", "相关工作", "研究背景", "方法", "实验",
    "结果", "讨论", "结论", "局限", "参考文献", "附录",
)
_HEADING_LINE_RE = re.compile(r"^\s*(?:\d+(?:\.\d+)*\.?\s+)?(.+?)\s*:?\s*$")


class FullTextError(Exception):
    """该来源无法提供可用全文（调用方据此降级，不视为整体失败）。"""


@dataclass
class PaperUnit:
    """一个原文单元：文本 + 稳定定位。"""

    text: str
    unit_kind: str = "block"  # block / abstract
    page: Optional[int] = None
    section: str = ""
    block_index: int = 0
    source_kind: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "unit_kind": self.unit_kind,
            "page": self.page,
            "section": self.section,
            "block_index": self.block_index,
            "text": self.text,
            "source_kind": self.source_kind,
        }


@dataclass
class FullTextResult:
    """某篇论文的原文获取结果（含降级说明，供 UI 如实展示）。"""

    units: List[PaperUnit] = field(default_factory=list)
    state: str = "none"  # fulltext / abstract / none
    source_kind: str = ""
    detail: str = ""  # 降级或失败原因（用户可见，不含敏感信息）

    @property
    def ok(self) -> bool:
        return bool(self.units)


def normalize_space(text: str) -> str:
    """把所有空白串（含换行）折叠成单个空格，用于跨行/跨页的逐字核对。

    PDF 提取的文本里同一句话可能被换行或多余空格拆开；核对只关心字符
    顺序与内容，不关心排版，因此两侧都先做同样的归一化。
    """
    return _ANY_WS_RE.sub(" ", (text or "")).strip()


def _detect_heading(line: str) -> Optional[str]:
    """识别章节标题行（检测不到返回 None，绝不猜章节）。

    条件：整行短、不以句末标点结尾、去掉可选编号后以关键词开头或等于
    关键词（允许 "Evaluation Metrics" 这类带后缀的标题）。
    """
    stripped = (line or "").strip()
    if not stripped or len(stripped) > 80 or stripped.endswith((".", "。", "！", "？", ";", "；")):
        return None
    m = _HEADING_LINE_RE.match(stripped)
    if not m:
        return None
    body = m.group(1).strip()
    lowered = body.lower()
    for keyword in _HEADING_KEYWORDS:
        if lowered == keyword or lowered.startswith(keyword + " ") or lowered.startswith(keyword):
            return body[:200]
    return None


def split_page_text(page_text: str, max_chars: int = MAX_UNIT_CHARS) -> List[str]:
    """把一页文本切成不超过 ``max_chars`` 的块（确定性、可重现）。

    先按句子边界切句，再贪心打包成块；单句过长时按句子边界硬切。
    块内空白被归一化，因此引用核对（两侧同样归一化）仍然逐字成立。
    """
    text = normalize_space(page_text)
    if not text:
        return []
    sentences = [s.strip() for s in re.split(r"(?<=[.!?\u3002\uff01\uff1f])\s+", text)]
    sentences = [s for s in sentences if s]

    pieces: List[str] = []
    for sentence in sentences:
        if len(sentence) <= max_chars:
            pieces.append(sentence)
            continue
        start = 0
        while start < len(sentence):
            chunk = sentence[start : start + max_chars]
            if start + max_chars < len(sentence):
                cut = max(
                    chunk.rfind(". "),
                    chunk.rfind("\u3002"),
                    chunk.rfind("\uff01"),
                    chunk.rfind("\uff1f"),
                )
                if cut > max_chars // 3:
                    chunk = chunk[: cut + 1]
            chunk = chunk.strip()
            if chunk:
                pieces.append(chunk)
            start += max(1, len(chunk))

    blocks: List[str] = []
    buf = ""
    for piece in pieces:
        candidate = f"{buf} {piece}".strip() if buf else piece
        if len(candidate) <= max_chars:
            buf = candidate
        else:
            blocks.append(buf)
            buf = piece
    if buf:
        blocks.append(buf)
    return blocks


def _page_of(metadata: Dict[str, Any]) -> Optional[int]:
    """加载器元数据里的页码，统一成 1 基；取不到就返回 None（不猜页码）。

    langchain 的 PDF 加载器用 0 基 ``page``，知识库分块元数据里的
    ``page_number`` 已是 1 基。
    """
    if metadata.get("page") is not None:
        try:
            return max(1, int(metadata["page"]) + 1)
        except (TypeError, ValueError):
            return None
    if metadata.get("page_number") is not None:
        try:
            return max(1, int(metadata["page_number"]))
        except (TypeError, ValueError):
            return None
    return None


def units_from_documents(
    docs: Iterable[Any], source_kind: str, max_units: int = MAX_UNITS_PER_PAPER
) -> List[PaperUnit]:
    """把 langchain Document 列表转成带定位的原文单元。

    ``block_index`` 按文档顺序连续编号；页码取加载器元数据；章节标题
    在检测到时更新当前 section（检测不到就留空，绝不猜）。
    """
    units: List[PaperUnit] = []
    section = ""
    index = 0
    for doc in docs or []:
        metadata = getattr(doc, "metadata", None) or {}
        if not isinstance(metadata, dict):
            metadata = {}
        page = _page_of(metadata)
        text = getattr(doc, "page_content", None) or (doc if isinstance(doc, str) else "")
        # 取该文档中最后一个标题（分块通常以某个章节标题开头或紧随其后）
        for line in (text or "").splitlines():
            heading = _detect_heading(line)
            if heading:
                section = heading
        for block in split_page_text(text):
            if len(block) < MIN_UNIT_CHARS:
                continue
            units.append(
                PaperUnit(
                    text=block,
                    unit_kind="block",
                    page=page,
                    section=section,
                    block_index=index,
                    source_kind=source_kind,
                )
            )
            index += 1
            if len(units) >= max_units:
                return units
    return units


class FullTextProvider:
    """原文来源适配器基类。"""

    name: str = ""

    def fetch(self, paper: Any) -> List[PaperUnit]:
        raise NotImplementedError


class AbstractUnitProvider(FullTextProvider):
    """摘要降级：把论文摘要作为一个 abstract 单元（无页码）。"""

    name = "abstract"

    def fetch(self, paper: Any) -> List[PaperUnit]:
        abstract = normalize_space(getattr(paper, "abstract", "") or "")
        if not abstract:
            return []
        return [
            PaperUnit(
                text=abstract[:MAX_UNIT_CHARS],
                unit_kind="abstract",
                page=None,  # 摘要级证据绝不携带页码
                section="abstract",
                block_index=0,
                source_kind="abstract",
            )
        ]


class LocalKbUnitProvider(FullTextProvider):
    """本地公共知识库：按标题检索该论文的已索引全文分块。

    知识库是全局共享的，因此只把**文件名/来源与论文标题匹配**的分块
    当作该论文的原文，避免把别的文档片段冒充成这篇论文的证据。
    """

    name = "local_kb"

    def __init__(self, kb_names: Optional[List[str]] = None, top_k: int = 20):
        self.kb_names = kb_names
        self.top_k = top_k

    def _kb_names(self) -> List[str]:
        if self.kb_names:
            return list(self.kb_names)
        try:
            from chatchat.server.knowledge_base.kb_api import list_kbs

            return [kb.kb_name for kb in (list_kbs().data or [])]
        except Exception:
            logger.warning("读取公共知识库列表失败（local_kb 全文按空处理）", exc_info=True)
            return []

    def fetch(self, paper: Any) -> List[PaperUnit]:
        title = (getattr(paper, "title", "") or "").strip()
        if not title:
            return []
        from chatchat.server.knowledge_base.kb_doc_api import search_docs

        key = normalize_space(title).lower()
        docs: List[Any] = []
        for kb_name in self._kb_names():
            try:
                hits = search_docs(
                    query=title,
                    knowledge_base_name=kb_name,
                    top_k=self.top_k,
                    score_threshold=0.0,
                    file_name="",
                    metadata={},
                )
            except Exception:
                logger.warning(f"知识库 {kb_name} 全文检索失败（跳过）", exc_info=True)
                continue
            for hit in hits or []:
                meta = hit.get("metadata") or {}
                name = str(meta.get("file_name") or meta.get("source") or "").lower()
                if key[:40] and key[:40] not in normalize_space(name).lower():
                    continue
                docs.append(
                    type(
                        "Doc",
                        (),
                        {"page_content": hit.get("page_content") or "", "metadata": meta},
                    )
                )
        return units_from_documents(docs, source_kind=self.name)


def _has_text(docs: Iterable[Any]) -> bool:
    """解析结果里是否真有文字（扫描件会得到空/空白页）。"""
    return any((getattr(d, "page_content", "") or "").strip() for d in docs or [])


class PdfUrlUnitProvider(FullTextProvider):
    """远程 PDF：下载后按「文字层 → OCR」顺序解析（运行时联网）。

    两个实测出来的坑，都在这里钉住：

    - arXiv 现在的 ``pdf_url`` 形如 ``https://arxiv.org/pdf/2312.10793v3``，
      **没有** ``.pdf`` 后缀 —— 旧实现只按后缀判断，等于把所有 arXiv 论文
      判成「没有 PDF」，还静默返回空（不记失败原因），界面只看到
      「未取得全文，已降级为摘要级证据」而没有任何解释；
    - 仓库的 ``RapidOCRPDFLoader`` 和 ``PyPDFLoader`` 都只吃**本地路径**，
      直接把 URL 塞进去必然解析失败，所以先下载到临时文件再解析。
    """

    #: 单篇 PDF 的下载上限（防止一篇超长 PDF 撑爆一次运行）
    MAX_PDF_BYTES = 30 * 1024 * 1024

    def __init__(
        self,
        loader_factory: Optional[Any] = None,
        timeout_seconds: float = 60.0,
    ):
        #: 可注入的加载器工厂（测试用假 loader；默认按下载后的本地路径构造）
        self.loader_factory = loader_factory
        self.timeout_seconds = timeout_seconds
        self.name = "pdf"

    @staticmethod
    def _looks_like_pdf(url: str) -> bool:
        path = url.split("?", 1)[0].split("#", 1)[0].lower()
        return path.endswith(".pdf") or path.endswith("/pdf") or "/pdf/" in path

    def _download(self, url: str) -> str:
        """下载到临时文件并返回路径（调用方负责删除）。"""
        import urllib.request
        from tempfile import mkstemp

        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as resp:
                content_type = (resp.headers.get("content-type") or "").lower()
                body = resp.read(self.MAX_PDF_BYTES + 1)
        except Exception as exc:
            raise FullTextError(f"PDF 下载失败：{exc}") from exc
        if len(body) > self.MAX_PDF_BYTES:
            raise FullTextError(
                f"PDF 超过 {self.MAX_PDF_BYTES // (1024 * 1024)} MB 上限，跳过全文"
            )
        if "pdf" not in content_type and not body.startswith(b"%PDF"):
            raise FullTextError(
                f"链接返回的不是 PDF（content-type={content_type or '未知'}）"
            )
        fd, path = mkstemp(prefix="chatchat-research-", suffix=".pdf")
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
        return path

    def _load(self, loader: Any) -> List[Any]:
        try:
            return list(loader.load() or [])
        except Exception as exc:
            raise FullTextError(f"PDF 解析失败：{exc}") from exc

    def _load_text(self, path: str) -> List[Any]:
        """文字层解析：论文 PDF 绝大多数带文字层，比 OCR 快两个数量级。"""
        try:
            from langchain_community.document_loaders import PyPDFLoader
        except Exception as exc:
            raise FullTextError(f"PDF 加载器不可用：{exc}") from exc
        return self._load(PyPDFLoader(path))

    def _load_ocr(self, path: str) -> List[Any]:
        """扫描件兜底：只有文字层取不到内容时才走仓库的 OCR 加载器。"""
        try:
            from chatchat.server.file_rag.document_loaders import RapidOCRPDFLoader
        except Exception as exc:
            raise FullTextError(f"OCR 加载器不可用：{exc}") from exc
        return self._load(RapidOCRPDFLoader(path))

    def fetch(self, paper: Any) -> List[PaperUnit]:
        url = (getattr(paper, "url", "") or "").strip()
        if not url:
            raise FullTextError("论文没有可解析的链接")
        if not self._looks_like_pdf(url):
            raise FullTextError(f"链接不是 PDF：{url[:120]}")

        path = self._download(url)
        try:
            if self.loader_factory is not None:
                docs = self._load(self.loader_factory(path))
            else:
                docs = self._load_text(path)
                if not _has_text(docs):
                    docs = self._load_ocr(path)
        finally:
            try:
                os.remove(path)
            except OSError:
                logger.warning("临时 PDF 删除失败", exc_info=True)
        return units_from_documents(docs, source_kind=self.name)


def default_providers(kb_names: Optional[List[str]] = None) -> List[FullTextProvider]:
    """生产默认顺序：远程 PDF → 本地知识库 → 摘要降级。"""
    return [PdfUrlUnitProvider(), LocalKbUnitProvider(kb_names=kb_names)]


def collect_units(
    paper: Any,
    providers: Optional[List[FullTextProvider]] = None,
    abstract_provider: Optional[FullTextProvider] = None,
) -> FullTextResult:
    """按 provider 顺序取全文；全部失败则降级到摘要单元。

    单个 provider 失败只记录原因并继续下一个（与 017 的来源隔离一致）。
    """
    providers = providers if providers is not None else default_providers()
    abstract_provider = abstract_provider or AbstractUnitProvider()
    failures: List[str] = []
    for provider in providers:
        try:
            units = provider.fetch(paper)
        except FullTextError as exc:
            failures.append(f"{provider.name}: {exc}")
            continue
        except Exception as exc:  # 单来源异常不影响降级路径
            logger.warning(f"原文来源 {provider.name} 异常（跳过）", exc_info=True)
            failures.append(f"{provider.name}: {exc}")
            continue
        if units:
            kinds = {u.unit_kind for u in units}
            return FullTextResult(
                units=units,
                state="fulltext" if "block" in kinds else "abstract",
                source_kind=units[0].source_kind or provider.name,
                detail="；".join(failures)[:500],
            )
    try:
        units = abstract_provider.fetch(paper)
    except Exception:
        logger.warning("摘要降级失败", exc_info=True)
        units = []
    if units:
        return FullTextResult(
            units=units,
            state="abstract",
            source_kind="abstract",
            detail="未取得全文，已降级为摘要级证据"
            + (f"（{'；'.join(failures)[:300]}）" if failures else ""),
        )
    return FullTextResult(
        units=[],
        state="none",
        source_kind="",
        detail="无可用的原文或摘要"
        + (f"（{'；'.join(failures)[:300]}）" if failures else ""),
    )


__all__ = [
    "MAX_UNIT_CHARS",
    "MIN_UNIT_CHARS",
    "MAX_UNITS_PER_PAPER",
    "FullTextError",
    "PaperUnit",
    "FullTextResult",
    "normalize_space",
    "split_page_text",
    "units_from_documents",
    "FullTextProvider",
    "AbstractUnitProvider",
    "LocalKbUnitProvider",
    "PdfUrlUnitProvider",
    "default_providers",
    "collect_units",
]
