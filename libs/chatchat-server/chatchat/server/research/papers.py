"""集中式候选论文结构与元数据标准化/去重（任务 017 阶段 B）。

所有来源适配器的输出都必须先转换为 ``PaperCandidate``，LangGraph 节点
只处理该结构，不解析任何工具的自由文本输出（任务 017 §5.2）。

去重优先级（§5.2）：DOI / ArXiv ID 等稳定标识 → 规范 URL → 规范化
标题+年份；合并时保留全部来源信息，不丢失可追溯性。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

#: 摘要进入候选结构时的截断长度（入库前再按列长截断）
ABSTRACT_MAX_CHARS = 4000

_ARXIV_ID_RE = re.compile(r"(?:arxiv[:/.]?|/abs/|/pdf/)?(\d{4}\.\d{4,5}(?:v\d+)?|[a-z\-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?)", re.IGNORECASE)
_DOI_RE = re.compile(r"(?:https?://(?:dx\.)?doi\.org/)?(10\.\d{4,9}/[^\s\"'<>]+)", re.IGNORECASE)


@dataclass
class PaperCandidate:
    """来源无关的候选论文（标准化前的最小公共结构）。"""

    title: str
    source: str
    authors: List[str] = field(default_factory=list)
    year: Optional[int] = None
    abstract: str = ""
    doi: Optional[str] = None
    arxiv_id: Optional[str] = None
    url: Optional[str] = None
    fulltext_status: str = "unknown"  # unknown/abstract/url/pdf
    score: Optional[float] = None  # 来源给出的相关度（可选）
    # 去重合并进来的其他来源（可追溯性：不因去重丢失来源信息）
    merged_sources: List[Dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "source": self.source,
            "authors": list(self.authors),
            "year": self.year,
            "abstract": self.abstract,
            "doi": self.doi,
            "arxiv_id": self.arxiv_id,
            "url": self.url,
            "fulltext_status": self.fulltext_status,
            "score": self.score,
            "merged_sources": list(self.merged_sources),
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "PaperCandidate":
        return cls(
            title=str(raw.get("title") or ""),
            source=str(raw.get("source") or ""),
            authors=list(raw.get("authors") or []),
            year=raw.get("year"),
            abstract=str(raw.get("abstract") or ""),
            doi=raw.get("doi"),
            arxiv_id=raw.get("arxiv_id"),
            url=raw.get("url"),
            fulltext_status=str(raw.get("fulltext_status") or "unknown"),
            score=raw.get("score"),
            merged_sources=list(raw.get("merged_sources") or []),
        )


def normalize_doi(value: Optional[str]) -> Optional[str]:
    """DOI 规范化：小写、去 URL 前缀与首尾空白。"""
    if not value:
        return None
    s = str(value).strip().lower()
    m = _DOI_RE.search(s)
    doi = (m.group(1) if m else s).strip().rstrip(".,;)")
    return doi or None


def normalize_arxiv_id(value: Optional[str]) -> Optional[str]:
    """ArXiv ID 规范化：从 URL/旧式 ID 中提取，去版本后缀（vN）。"""
    if not value:
        return None
    s = str(value).strip()
    m = _ARXIV_ID_RE.search(s)
    aid = m.group(1) if m else s
    aid = re.sub(r"v\d+$", "", aid, flags=re.IGNORECASE).lower()
    return aid or None


def normalize_url(value: Optional[str]) -> Optional[str]:
    """URL 规范化：小写 scheme/host、去 fragment 与结尾斜杠。"""
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    s = s.split("#", 1)[0]
    s = s.rstrip("/")
    # scheme/域名大小写不敏感
    m = re.match(r"^(https?://)([^/]+)(.*)$", s, re.IGNORECASE)
    if m:
        s = m.group(1).lower() + m.group(2).lower() + m.group(3)
    return s or None


def normalize_title(value: Optional[str]) -> str:
    """标题规范化：小写、去标点、压缩空白（去重兜底键的组成部分）。"""
    s = str(value or "").lower()
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def title_key(title: Optional[str], year: Optional[int]) -> Optional[str]:
    """规范化标题 + 年份兜底键；标题为空返回 None。"""
    t = normalize_title(title)
    if not t:
        return None
    return f"{t[:180]}:{year}" if year else t[:180]


def dedup_priority_keys(candidate: PaperCandidate) -> List[tuple]:
    """候选的去重键（按优先级）：doi → arxiv → url → title+year。"""
    keys: List[tuple] = []
    if candidate.doi:
        keys.append(("doi", candidate.doi))
    if candidate.arxiv_id:
        keys.append(("arxiv_id", candidate.arxiv_id))
    if candidate.url:
        keys.append(("url", candidate.url))
    tk = title_key(candidate.title, candidate.year)
    if tk:
        keys.append(("title_key", tk))
    return keys


def standardize_candidate(raw: PaperCandidate) -> PaperCandidate:
    """把来源候选标准化：规范化 DOI/ArXiv ID/URL，截断摘要。

    返回新对象（不修改入参）；来源名与全文状态原样保留。
    """
    return PaperCandidate(
        title=str(raw.title or "").strip()[:1000],
        source=raw.source,
        authors=[str(a).strip() for a in (raw.authors or []) if str(a).strip()][:20],
        year=raw.year,
        abstract=(raw.abstract or "")[:ABSTRACT_MAX_CHARS],
        doi=normalize_doi(raw.doi),
        arxiv_id=normalize_arxiv_id(raw.arxiv_id),
        url=normalize_url(raw.url),
        fulltext_status=raw.fulltext_status or "unknown",
        score=raw.score,
    )


def paper_record_fields(candidate: PaperCandidate) -> Dict[str, Any]:
    """标准化候选 → ``upsert_paper`` 所需字段字典。"""
    return {
        "title": candidate.title,
        "authors": "; ".join(candidate.authors)[:2000],
        "year": candidate.year,
        "abstract": candidate.abstract[:8000],
        "doi": candidate.doi,
        "arxiv_id": candidate.arxiv_id,
        "url": candidate.url,
        "source": candidate.source,
        "sources": [{"source": candidate.source, "url": candidate.url or ""}]
        + list(candidate.merged_sources),
        "fulltext_status": candidate.fulltext_status,
        "title_key": title_key(candidate.title, candidate.year),
    }


def dedup_candidates(
    candidates: List[PaperCandidate],
) -> tuple[List[PaperCandidate], int]:
    """进程内去重（同一批候选之间）：稳定标识优先，保留来源合并信息。

    返回 ``(去重后候选列表, 合并掉的条数)``。合并规则：后到候选若与已
    保留候选命中同一去重键，则并入其 ``merged_sources``（dict 列表），
    不新增条目。跨运行/跨项目的去重由仓储层 ``upsert_paper`` 完成。
    """
    kept: List[PaperCandidate] = []
    # key -> kept 列表下标
    index: Dict[tuple, int] = {}
    merged = 0
    for cand in candidates:
        keys = dedup_priority_keys(cand)
        hit = None
        for k in keys:
            if k in index:
                hit = index[k]
                break
        if hit is None:
            kept.append(cand)
            pos = len(kept) - 1
            for k in keys:
                index.setdefault(k, pos)
        else:
            merged += 1
            target = kept[hit]
            # 先固化目标原有来源（在字段补齐之前，避免混入后到的 URL）
            if not target.merged_sources:
                target.merged_sources = [
                    {"source": target.source, "url": target.url or ""}
                ]
            # 补齐目标缺失字段（不覆盖已有值）
            if not target.doi and cand.doi:
                target.doi = cand.doi
            if not target.arxiv_id and cand.arxiv_id:
                target.arxiv_id = cand.arxiv_id
            if not target.url and cand.url:
                target.url = cand.url
            if not target.abstract and cand.abstract:
                target.abstract = cand.abstract
            if not target.authors and cand.authors:
                target.authors = list(cand.authors)
            if target.year is None and cand.year is not None:
                target.year = cand.year
            srcs = target.merged_sources
            entry = {"source": cand.source, "url": cand.url or ""}
            if entry not in srcs:
                srcs.append(entry)
    return kept, merged


__all__ = [
    "ABSTRACT_MAX_CHARS",
    "PaperCandidate",
    "normalize_doi",
    "normalize_arxiv_id",
    "normalize_url",
    "normalize_title",
    "title_key",
    "dedup_priority_keys",
    "standardize_candidate",
    "paper_record_fields",
    "dedup_candidates",
]
