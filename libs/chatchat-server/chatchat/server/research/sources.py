"""论文来源适配层（任务 017 阶段 B）。

每个来源实现统一接口 ``async search(query, limit) -> List[PaperCandidate]``，
失败时抛 ``SourceError``——研究图据此隔离单来源失败（记录该来源失败并
继续其他来源，全部失败才使阶段失败，任务 017 §5.2）。

第一版复用两类已有能力：

- ``arxiv``：直接调 ``arxiv`` 包（与现有 ArXiv 工具同一底层依赖），取结构化
  结果而非工具的自由文本输出；底层依赖缺失时该来源以 ``SourceError``
  明确不可用（与现有工具行为一致），不影响其他来源；
- ``local_kb``：现有公共知识库检索（``kb_doc_api.search_docs``），
  文档结构化字段转换为候选。

用户论文上传、网页搜索或 MCP 工具可经同一接口接入，本任务不实现。
测试一律注入假来源，不连接真实网络。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from chatchat.server.research.papers import PaperCandidate, normalize_arxiv_id
from chatchat.utils import build_logger

logger = build_logger()

#: 单来源单检索式的默认命中上限
DEFAULT_SOURCE_TOP_K = 10


class SourceError(Exception):
    """来源不可用或本次检索失败（调用方记录失败并继续其他来源）。"""


class PaperSource:
    """来源适配器基类。"""

    name: str = ""

    async def search(self, query: str, limit: int = DEFAULT_SOURCE_TOP_K) -> List[PaperCandidate]:
        raise NotImplementedError


class ArxivSource(PaperSource):
    """ArXiv 检索：直接调 ``arxiv`` 包取结构化结果。

    不再经 ``langchain_community`` 的 ArxivAPIWrapper：0.4.x 已移除
    ``load_results``（只剩 ``run`` / ``load`` / ``lazy_load``），拿回的是
    Document 而非带 arxiv_id、published、pdf_url 的原始结果，本来源需要
    后者。聊天里的 ArXiv 工具仍走 wrapper 的 ``run``，不受影响。
    """

    name = "arxiv"

    async def search(self, query: str, limit: int = DEFAULT_SOURCE_TOP_K) -> List[PaperCandidate]:
        try:
            import arxiv
        except ImportError as exc:  # 底层依赖缺失：来源明确不可用
            raise SourceError(f"ArXiv 依赖不可用：{exc}") from exc
        try:
            top = max(1, int(limit))
            search = arxiv.Search(
                query=query,
                max_results=top,
                sort_by=arxiv.SortCriterion.Relevance,
            )
            # arxiv 2.x 用 Client().results(search)；1.x 只有 Search.results()
            client_cls = getattr(arxiv, "Client", None)
            if client_cls is None:
                results = list(search.results())
            else:
                client = client_cls(page_size=min(top, 100), delay_seconds=0.0, num_retries=2)
                results = list(client.results(search))
        except Exception as exc:
            raise SourceError(f"ArXiv 检索失败：{exc}") from exc
        candidates: List[PaperCandidate] = []
        for res in results or []:
            try:
                year = None
                published = getattr(res, "published", None) or getattr(res, "updated", None)
                if published is not None:
                    year = int(getattr(published, "year", 0)) or None
                # arxiv 2.x 删掉了 1.x 的 result_id / url：短 id 只能从 entry_id
                # （http://arxiv.org/abs/1102.2017v2）解析，全文下载要的 .pdf
                # 链接只有 pdf_url —— fulltext 的 PdfUrlUnitProvider 只认
                # 以 .pdf 结尾的 url，填 abs 页会直接解析不到正文。
                entry_id = str(getattr(res, "entry_id", "") or "")
                pdf_url = str(getattr(res, "pdf_url", "") or "") or None
                arxiv_id = (
                    str(getattr(res, "result_id", "") or "")
                    or normalize_arxiv_id(entry_id)
                    or None
                )
                url = str(getattr(res, "url", "") or "") or pdf_url or entry_id or None
                candidates.append(
                    PaperCandidate(
                        title=str(getattr(res, "title", "") or "").strip(),
                        source=self.name,
                        authors=[str(a) for a in (getattr(res, "authors", None) or [])],
                        year=year,
                        abstract=str(getattr(res, "summary", "") or ""),
                        arxiv_id=arxiv_id,
                        url=url,
                        fulltext_status="pdf" if pdf_url else "abstract",
                    )
                )
            except Exception:  # 单条解析失败不影响该来源其余结果
                logger.warning("ArXiv 结果单条解析失败（跳过）", exc_info=True)
        return candidates


class LocalKbSource(PaperSource):
    """本地/公共知识库检索：复用现有 ``search_docs``（全局共享知识库）。"""

    name = "local_kb"

    def __init__(self, kb_names: Optional[List[str]] = None, top_k: int = DEFAULT_SOURCE_TOP_K):
        self.kb_names = kb_names
        self.top_k = top_k

    def _resolve_kb_names(self) -> List[str]:
        if self.kb_names:
            return list(self.kb_names)
        try:
            from chatchat.server.knowledge_base.kb_api import list_kbs

            return [kb.kb_name for kb in (list_kbs().data or [])]
        except Exception:
            logger.warning("读取公共知识库列表失败（local_kb 来源按空处理）", exc_info=True)
            return []

    async def search(self, query: str, limit: int = DEFAULT_SOURCE_TOP_K) -> List[PaperCandidate]:
        from chatchat.server.knowledge_base.kb_doc_api import search_docs

        names = self._resolve_kb_names()
        if not names:
            raise SourceError("没有可用的公共知识库")
        candidates: List[PaperCandidate] = []
        errors: List[str] = []
        for kb_name in names:
            try:
                docs = search_docs(
                    query=query,
                    knowledge_base_name=kb_name,
                    top_k=min(self.top_k, max(1, int(limit))),
                    score_threshold=0.0,
                    file_name="",
                    metadata={},
                )
            except Exception as exc:
                errors.append(f"{kb_name}: {exc}")
                continue
            for doc in docs or []:
                meta: Dict[str, Any] = doc.get("metadata") or {}
                title = str(
                    meta.get("title")
                    or meta.get("file_name")
                    or meta.get("source")
                    or ""
                ).strip()
                if not title:
                    continue
                candidates.append(
                    PaperCandidate(
                        title=title,
                        source=self.name,
                        abstract=str(doc.get("page_content") or "")[:2000],
                        url=str(meta.get("source") or "") or None,
                        fulltext_status="abstract",
                        score=doc.get("score"),
                    )
                )
        if not candidates and errors:
            raise SourceError("；".join(errors)[:500])
        return candidates


def build_default_sources() -> Dict[str, PaperSource]:
    """生产默认来源集合（名称 → 适配器）。"""
    return {
        "arxiv": ArxivSource(),
        "local_kb": LocalKbSource(),
    }


__all__ = [
    "DEFAULT_SOURCE_TOP_K",
    "SourceError",
    "PaperSource",
    "ArxivSource",
    "LocalKbSource",
    "build_default_sources",
]
