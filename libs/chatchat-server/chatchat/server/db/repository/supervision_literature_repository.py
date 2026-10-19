"""师生协作论文检索与文献结论的数据访问（《学生研究与导师审核功能重新设计方案》里程碑3）。

四条硬口径（§15.3、§16.4）：

1. **检索有依据**：创建 ``LS`` 任务必须绑定一个**已确认并共享**的
   ``ResearchBrief`` 版本；只有草稿、没有确认过 → ``WorkError``（路由层 409），
   错误信息说明为什么不能创建。绑定的是那一版的副本（``brief_snapshot``），
   研究定义后来出新版本，旧任务仍按当时的依据走，页面据
   ``brief_superseded`` 提示学生是否重新检索（§4.4）。
2. **过程可拆、可重试**：一次检索按「来源 × 关键词批次」拆成独立子任务，
   每个子任务自己记录状态、命中数、去重数与失败原因；单个子任务失败只让
   它自己 ``failed``，其它子任务与整个任务照常推进（§15.3 第3、6条）。
   重试是同一子任务的新一次尝试，旧失败进 ``failure_history`` 保留。
3. **结果去重 + 可解释关联度**：三种幂等键（规范化 DOI、规范化 arXiv ID、
   规范化标题+年份）先查后插，库层还有三条项目内部分唯一索引兜底，重复
   入库只产生一条记录并把来源并进 ``sources``。关联度是词面规则结果：
   等级 + 中文理由 + 命中的研究定义要素 + 缺失要素 + 规则版本，
   ``relevance_score`` 在规则排序下恒为 ``None``——不伪造「95/91/94」这类
   伪精确数值（§6.3.B）。模型语义分析写进 ``semantic_analysis``，与规则
   字段并列、不覆盖。
4. **模型调用一律过容量闸门**：本模块不实现任何锁，也不 import 闸门本身，
   只按契约调用注入进来的适配器::

       token = capacity.acquire(consumer=..., business_ref=..., user_id=...)
       try: ...llm.invoke(messages)...
       finally: capacity.release(token, reason=...)

   消费者类型用已登记的 ``research_plan`` / ``paper_extract`` /
   ``paper_compare``，每次**实际调用**取放一次，绝不跨多篇论文长期持有
   （§8.2）。模型或适配器没接入 → ``LiteratureUnavailable``（路由层 503 +
   中文说明），绝不假装跑过。外部来源检索同样不联网：来源是注入的
   provider，默认实现报告「未接入」，测试用假 provider。

私人材料只有引用快照这一条入口（§9.1）：``create_citation`` 按**具体成果**
授权，把当时可见的字段与内容哈希复制进 ``snapshot``；导师只读这份快照，
来源之后变化不回改历史，撤销只关闭后续引用。本模块不写
``research_project`` / ``paper_record`` / ``evidence_card`` 任何一行。

通知：里程碑3 的检索、决定、结论与授权都不在 §12 的首期站内通知清单里，
因此只写审计（§13「所有正式动作进入审计日志」），不发通知，避免把还没
定义文案的事件塞进通知流。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from typing import Any, Iterable, Optional

from sqlalchemy import case, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from chatchat.server.db.models.research_model import (
    EvidenceCardModel,
    PaperRecordModel,
    ProjectPaperModel,
)
from chatchat.server.db.models.supervision_literature_model import (
    CITATION_PURPOSE_VALUES,
    CITATION_SOURCE_KIND_VALUES,
    CODE_CITATION,
    CODE_CONCLUSION,
    CODE_EVIDENCE_CARD,
    CODE_RESULT,
    CODE_SEARCH_TASK,
    CONCLUSION_STATUS_VALUES,
    DECISION_VALUES,
    EVIDENCE_TYPE_VALUES,
    EXTRACT_STATE_VALUES,
    MENTOR_REVIEW_STATE_VALUES,
    RELEVANCE_LEVEL_VALUES,
    SUBTASK_KIND_VALUES,
    SUBTASK_STATUS_VALUES,
    SUPPORT_RELATION_VALUES,
    TASK_PURPOSE_VALUES,
    SupervisionLiteratureCitationModel,
    SupervisionLiteratureConclusionModel,
    SupervisionLiteratureEvidenceCardModel,
    SupervisionLiteratureExtractionModel,
    SupervisionLiteratureResultModel,
    SupervisionLiteratureSearchTaskModel,
    SupervisionLiteratureSubtaskModel,
)
from chatchat.server.db.models.supervision_model import SupervisionProjectModel
from chatchat.server.db.models.supervision_research_model import (
    BRIEF_CONTENT_FIELDS,
    BRIEF_FIELD_LABELS,
    SupervisionResearchBriefModel,
    SupervisionResearchBriefVersionModel,
)
from chatchat.server.db.repository.supervision_repository import (
    ROLE_MENTOR,
    ROLE_STUDENT,
    active_member_ids,
    new_id,
    next_code,
    record_audit,
    require_member,
)
from chatchat.server.db.repository.supervision_work_repository import (
    WorkError,
    WorkValidationError,
)
from chatchat.server.research.papers import (
    PaperCandidate,
    dedup_priority_keys,
    normalize_url,
    standardize_candidate,
    title_key as paper_title_key,
)

# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class LiteratureUnavailable(Exception):
    """模型或外部来源能力尚未接入；路由层收敛成 503，绝不假装跑过。"""


class LiteratureSourceError(Exception):
    """单个来源本次检索失败：只让该子任务失败，不影响其它子任务与整个任务。"""


class ModelCallError(Exception):
    """一次实际模型调用失败（异常、超时、返回不可用）。"""


# ---------------------------------------------------------------------------
# 版本与上限
# ---------------------------------------------------------------------------

#: 规则排序版本。改了命中规则必须换版本号，页面要展示"排序规则版本"（§6.3.B）。
RULES_VERSION = "lit-rank-v1"
#: 提示词版本，随提取/比较/方案提示一起写进子任务，便于复现（§17 可追踪性）。
PROMPT_VERSION = "lit-prompt-v1"

#: 一次检索最多拆多少个关键词批次（批次 = 可独立重试的子任务）。
MAX_KEYWORD_BATCHES = 10
#: 单来源单批次的命中上限。
MAX_HITS_PER_SOURCE = 50
#: 摘要级关联分析的默认上限（§6.3.1 第3步：只对有限候选做）。
DEFAULT_ABSTRACT_ANALYSIS_LIMIT = 8
#: 多篇比较一次最多几篇，避免一次操作堆出无上限后台任务。
MAX_COMPARE_PAPERS = 6
#: 送给模型的摘要/正文片段长度上限。
MAX_MODEL_TEXT_CHARS = 1200

#: 关联等级从高到低，列表默认排序用。
_LEVEL_ORDER = ("high", "relevant", "reference", "low")

_LEVEL_LABEL = {"high": "高相关", "relevant": "相关", "reference": "可参考", "low": "低相关"}

#: 参与词面命中的研究定义要素分组（§6.3.B 的"关键词、方法、数据/场景、
#: 评价指标的命中与缺失"）。
_RANK_GROUPS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("question", ("question", "sub_questions"), "核心研究问题"),
    ("keywords", ("keywords",), "检索关键词"),
    ("methods", ("methods",), "方法"),
    ("subjects", ("subjects", "scope"), "数据/场景"),
    ("metrics", ("metrics",), "评价指标"),
)


def _now() -> datetime:
    return datetime.now()


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# 纯函数：候选标准化、幂等键、规则排序
# ---------------------------------------------------------------------------


def _clean_text(value: Any, limit: int = 2000) -> str:
    return str(value if value is not None else "").strip()[:limit]


def candidate_from_raw(raw: Any) -> Optional[PaperCandidate]:
    """把 provider 返回的候选（dict 或对象）转成标准化 ``PaperCandidate``。

    复用现有论文研究模块的规范化能力（DOI / arXiv ID / URL / 标题），
    里程碑3 不另写一套去重键。标题为空的候选无法定位，直接丢弃。
    """
    if isinstance(raw, PaperCandidate):
        data = raw.to_dict()
    elif isinstance(raw, dict):
        data = raw
    else:
        data = {
            key: getattr(raw, key, None)
            for key in (
                "title", "source", "authors", "year", "abstract", "doi",
                "arxiv_id", "url", "fulltext_status", "score", "merged_sources",
            )
        }
    if not _clean_text(data.get("title"), 1000):
        return None
    year = data.get("year")
    try:
        year = int(year) if year not in (None, "") else None
    except (TypeError, ValueError):
        year = None
    candidate = PaperCandidate(
        title=_clean_text(data.get("title"), 1000),
        source=_clean_text(data.get("source"), 50) or "unknown",
        authors=[str(a).strip() for a in (data.get("authors") or []) if str(a).strip()][:20],
        year=year,
        abstract=_clean_text(data.get("abstract"), 8000),
        doi=data.get("doi"),
        arxiv_id=data.get("arxiv_id"),
        url=data.get("url"),
        fulltext_status=_clean_text(data.get("fulltext_status"), 16) or "unknown",
        score=data.get("score"),
        merged_sources=list(data.get("merged_sources") or []),
    )
    if candidate.fulltext_status not in ("unknown", "abstract", "url", "pdf"):
        candidate.fulltext_status = "unknown"
    return standardize_candidate(candidate)


def dedup_key_of(candidate: PaperCandidate) -> str:
    """最强幂等键的字符串形式：doi → arxiv_id → url → title_key。"""
    keys = dedup_priority_keys(candidate)
    if not keys:
        return ""
    kind, value = keys[0]
    return f"{kind}:{value}"[:200]


def _norm_text(value: Any) -> str:
    """小写、去标点、压空白——标题、摘要与词条都用同一套归一化。"""
    return re.sub(
        r"\s+", " ",
        re.sub(r"[^\w\s]", " ", str(value or "").lower(), flags=re.UNICODE),
    ).strip()


def _terms_of(value: Any) -> list[str]:
    """把一个研究定义要素拆成可匹配的词条（列表原样拆，文本按标点与空格拆）。

    中英混排的研究定义很常见，所以既按顿号/逗号/分号拆，也按空格拆；纯英文
    词条至少 3 个字符，避免 ``a``、``4`` 这类噪声词条造成假命中。
    """
    items: list[str] = []
    if isinstance(value, (list, tuple, set)):
        raw_items = [str(v) for v in value]
    else:
        raw_items = re.split(r"[、,，;；\n/]+", str(value or ""))
    for item in raw_items:
        for piece in item.strip().split():
            piece = piece.strip()
            if not piece:
                continue
            if re.fullmatch(r"[\x00-\x7f]+", piece):
                if len(piece) >= 3:
                    items.append(piece)
            elif len(piece) >= 2:
                items.append(piece)
    return list(dict.fromkeys(items))[:40]


def _match_term(term: str, haystack: str) -> bool:
    """词条命中判断：英文按词边界，中文按子串。

    不做词边界就会让 ``ray`` 命中 ``array``，把不相干的论文排到前面——
    可解释排序最怕的就是这种说不清的命中。
    """
    needle = _norm_text(term)
    if not needle:
        return False
    if re.fullmatch(r"[\x00-\x7f]+", needle):
        return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", haystack) is not None
    return needle in haystack


def _flatten(value: Any) -> str:
    if isinstance(value, (list, tuple, set)):
        return " ".join(str(v) for v in value)
    return str(value or "")


def rank_candidate(
    candidate: PaperCandidate, brief_content: Optional[dict]
) -> dict:
    """可解释的低成本规则排序（§6.3.B、§16.4 第2条）。

    返回 ``{level, reason, matched, missing, score, score_source,
    recommended_action, rules_version}``。**不产生数值分数**：首版只有词面
    等级与命中理由，模型语义评分必须作为单独的模型任务并另存展示。
    """
    haystack = _norm_text(f"{candidate.title} {candidate.abstract}")
    content = brief_content or {}
    matched: list[dict] = []
    missing: list[dict] = []
    hit_groups: set[str] = set()
    for group, fields, label in _RANK_GROUPS:
        terms: list[str] = []
        for field in fields:
            terms.extend(_terms_of(content.get(field)))
        terms = list(dict.fromkeys(terms))
        if not terms:
            missing.append({
                "field": group, "label": label,
                "note": f"研究定义里没有给出{label}，无法判断是否命中",
            })
            continue
        hits = [term for term in terms if _match_term(term, haystack)]
        if hits:
            hit_groups.add(group)
            for term in hits[:6]:
                matched.append({
                    "field": group, "label": label, "term": term,
                    "where": "标题或摘要",
                })
        else:
            missing.append({
                "field": group, "label": label,
                "note": f"标题与摘要里没有命中{label}要素",
            })

    question_hit = "question" in hit_groups
    if question_hit and ({"methods", "subjects"} & hit_groups):
        level = "high"
    elif question_hit or len(hit_groups) >= 3:
        level = "relevant"
    elif hit_groups:
        level = "reference"
    else:
        level = "low"

    hit_labels = [label for group, _fields, label in _RANK_GROUPS if group in hit_groups]
    missing_labels = [
        item["label"] for item in missing if not item["note"].startswith("研究定义里没有给出")
    ]
    undefined_labels = [
        item["label"] for item in missing if item["note"].startswith("研究定义里没有给出")
    ]
    parts = [f"{_LEVEL_LABEL[level]}：命中" + "、".join(hit_labels) if hit_labels
             else f"{_LEVEL_LABEL[level]}：标题与摘要没有命中研究定义要素"]
    if missing_labels:
        parts.append("、".join(missing_labels) + "尚未命中")
    if undefined_labels:
        parts.append("、".join(undefined_labels) + "尚未确认")
    return {
        "level": level,
        "reason": "；".join(parts)[:2000],
        "matched": matched[:40],
        "missing": missing[:20],
        # 规则排序不产出数值分数；只有真跑过模型语义评分才允许有值。
        "score": None,
        "score_source": "rule_keyword",
        "recommended_action": {
            "high": "priority_read", "relevant": "priority_read",
            "reference": "reference", "low": "low_relevant",
        }[level],
        "rules_version": RULES_VERSION,
    }


def literature_snapshot_rows(
    session: Session, *, project_id: str, search_task_id: str = ""
) -> list[SupervisionLiteratureResultModel]:
    """当前**已纳入**的论文集合（文献结论的依据）。"""
    stmt = select(SupervisionLiteratureResultModel).where(
        SupervisionLiteratureResultModel.project_id == project_id,
        SupervisionLiteratureResultModel.decision == "included",
    )
    if search_task_id:
        stmt = stmt.where(
            SupervisionLiteratureResultModel.search_task_id == search_task_id
        )
    return list(
        session.execute(
            stmt.order_by(SupervisionLiteratureResultModel.code)
        ).scalars().all()
    )


def literature_snapshot_of(rows: Iterable[SupervisionLiteratureResultModel]) -> list[dict]:
    """把纳入集合固定成可比较的快照（§4.5「绑定它依据的文献快照版本」）。"""
    return [
        {
            "result_code": row.code,
            "title": row.title,
            "dedup_key": row.dedup_key,
            "decision": row.decision,
            "decision_reason": row.decision_reason,
            "decided_at": row.decided_at.isoformat(timespec="seconds") if row.decided_at else "",
            "relevance_level": row.relevance_level,
            "update_time": row.update_time.isoformat(timespec="seconds") if row.update_time else "",
        }
        for row in rows
    ]


def snapshot_hash(snapshot: Iterable[dict]) -> str:
    """快照哈希：与当前纳入集合的哈希不一致 = 这条结论的依据已变化。"""
    payload = json.dumps(list(snapshot), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def content_hash(payload: Any) -> str:
    """引用快照的内容哈希：来源后来变化也能证明"当时看到的是什么"（§9.1）。"""
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _ts(value: Optional[datetime]) -> str:
    return value.isoformat(timespec="seconds") if value else ""


# ---------------------------------------------------------------------------
# 纯函数：提示词与输出解析（可单独测试，不联网）
# ---------------------------------------------------------------------------


def _brief_lines(content: dict, *, limit: int = 24) -> list[str]:
    lines: list[str] = []
    for field in BRIEF_CONTENT_FIELDS:
        if field not in content:
            continue
        text = _flatten(content.get(field)).strip()
        if text:
            label = BRIEF_FIELD_LABELS.get(field, field)
            lines.append(f"{label}：{text[:600]}")
    return lines[:limit]


def build_plan_prompt(task: SupervisionLiteratureSearchTaskModel) -> list[dict]:
    """生成检索方案的短调用提示（§6.3.1 第1步：一次短调用，学生确认后才搜索）。"""
    content = task.brief_snapshot or {}
    system = (
        "你是科研协作项目里的文献检索助手。你的任务是把已确认的结构化研究定义"
        "改写成可执行的检索方案。\n"
        "硬性要求：\n"
        "1. 只依据给定的研究定义与检索目标，不得编造研究定义里没有的范围。\n"
        "2. 检索式要短：每条不超过 12 个词，允许布尔组合。\n"
        "3. 排除词只写明确要排除的，不要写同义词。\n"
        "4. 只输出一个 JSON 对象，不要解释、不要 Markdown 代码块。\n"
        '5. JSON 结构固定为：{"queries":[{"query":"","source":"","rationale":""}],'
        '"exclude_terms":[""],"year_from":null,"year_to":null,"note":""}。'
    )
    user = (
        f"检索任务：{task.code}（检索目标性质：{task.purpose}）\n"
        f"检索目标：{task.goal}\n"
        f"允许的来源：{'、'.join(str(s) for s in (task.sources or [])) or '未指定'}\n"
        f"结果上限：{task.result_limit}\n\n"
        "已确认的结构化研究定义（版本 "
        f"{task.brief_code} v{task.brief_version}）：\n" + "\n".join(_brief_lines(content))
        + "\n\n请输出检索方案 JSON。"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def build_analysis_prompt(
    task: SupervisionLiteratureSearchTaskModel, results: list[SupervisionLiteratureResultModel]
) -> list[dict]:
    """摘要级关联分析提示（§6.3.D 的三个产品问题；只给摘要级证据）。"""
    content = task.brief_snapshot or {}
    items = "\n\n".join(
        f"[{row.code}] {row.title}（{row.year or '年份未知'}，来源 {row.source}，"
        f"全文状态 {row.fulltext_status}）\n摘要：{(row.abstract or '（无摘要）')[:MAX_MODEL_TEXT_CHARS]}"
        for row in results
    )
    system = (
        "你是科研协作项目里的文献关联分析助手。只根据给出的论文摘要做分析，"
        "摘要里没有的内容一律写「摘要未提及」，不得用标题或常识补全。\n"
        "必须区分：论文明确陈述的内容、你的推断、无法判断的部分。\n"
        "只输出一个 JSON 对象，不要解释、不要 Markdown 代码块。结构固定为：\n"
        '{"papers":[{"code":"","level":"high|relevant|reference|low",'
        '"solves":"","same":"","different":"","borrowable":"","impact":"",'
        '"reason":"","evidence_quotes":[{"quote":"","locator":""}]}],"note":""}'
    )
    user = (
        f"当前研究定义（{task.brief_code} v{task.brief_version}）：\n"
        + "\n".join(_brief_lines(content))
        + f"\n\n检索目标：{task.goal}\n\n候选论文：\n{items}\n\n请输出 JSON。"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def build_extract_prompt(
    task: SupervisionLiteratureSearchTaskModel, result: SupervisionLiteratureResultModel
) -> list[dict]:
    """单篇深度提取提示（§6.3.C 的字段清单 + 原文定位要求）。"""
    content = task.brief_snapshot or {}
    system = (
        "你是科研协作项目里的论文内容提取助手。只能使用给出的材料"
        "（标题、摘要与已入库的可定位文本片段）。\n"
        "硬性要求：\n"
        "1. 每一条 evidence 必须带 quote（逐字摘录）与 locator"
        "（section/page/line/char 任一），没有出处的内容不能进 evidence。\n"
        "2. 材料里没有的字段写空字符串，并在 limitations 里说明"
        "「本次输入只有摘要级材料」，不得补全。\n"
        "3. 不得声称已经运行或复现论文中的实验。\n"
        "4. 只输出一个 JSON 对象，不要 Markdown 代码块。结构固定为：\n"
        '{"summary":"","research_problem":"","method":"","algorithm":"",'
        '"experiments":"","results_metrics":"","limitations":"",'
        '"differences":{"same":"","different":"","borrowable":"",'
        '"usable_methods":"","baseline_methods":"","conflicting_assumptions":"",'
        '"gaps":"","suggested_tasks":""},'
        '"evidence":[{"evidence_type":"","quote":"","locator":{"section":""}}]}'
    )
    user = (
        f"当前研究定义（{task.brief_code} v{task.brief_version}）：\n"
        + "\n".join(_brief_lines(content))
        + f"\n\n待提取论文 {result.code}：{result.title}"
        f"（{result.year or '年份未知'}，来源 {result.source}）\n"
        f"摘要：{(result.abstract or '（无摘要）')[:4000]}\n\n请输出 JSON。"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def build_compare_prompt(
    task: SupervisionLiteratureSearchTaskModel, results: list[SupervisionLiteratureResultModel]
) -> list[dict]:
    """多篇比较提示（§15.3 的「多篇比较」子任务）。"""
    items = "\n\n".join(
        f"[{row.code}] {row.title}\n摘要：{(row.abstract or '（无摘要）')[:MAX_MODEL_TEXT_CHARS]}"
        for row in results
    )
    system = (
        "你是科研协作项目里的文献比较助手。只比较给出的论文，不得引入未给出的"
        "论文或编造数据。只输出一个 JSON 对象，不要 Markdown 代码块。结构固定为：\n"
        '{"dimensions":[{"name":"","values":{"P-001":""}}],'
        '"agreements":[""],"conflicts":[""],"gaps":[""],"note":""}'
    )
    user = (
        f"检索目标：{task.goal}\n\n待比较论文：\n{items}\n\n请输出比较 JSON。"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def parse_json_payload(raw: str) -> Optional[dict]:
    """解析模型返回的 JSON（容忍 Markdown 代码块与前后杂文本）。"""
    text = str(raw or "").strip()
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, flags=re.S)
    if fenced:
        text = fenced.group(1).strip()
    candidates = [text]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict):
            return data
    return None


# ---------------------------------------------------------------------------
# 注入契约：来源 provider 与模型/容量适配器
# ---------------------------------------------------------------------------


class UnconfiguredLiteratureProvider:
    """默认来源 provider：明确报告「外部论文来源尚未接入」，绝不联网。

    里程碑3 只定义注入点，不接真实检索（§15.3 第2条要求复用现有搜索服务，
    但真实联网检索属于集成阶段）。路由层据此返回 503 + 中文说明，
    而不是造一批假候选。测试注入假 provider。
    """

    name = "unconfigured"

    def available(self) -> tuple[bool, str]:
        return False, (
            "外部论文来源尚未接入：检索需要注入来源 provider"
            "（arXiv / Crossref / Semantic Scholar / 实验室论文库）"
        )

    def search(self, *, query: str, source: str, limit: int) -> list:  # noqa: ARG002
        raise LiteratureSourceError("外部论文来源尚未接入")


def provider_is_available(provider: Any) -> tuple[bool, str]:
    if provider is None:
        return False, "未注入论文来源 provider"
    checker = getattr(provider, "available", None)
    if callable(checker):
        try:
            ok, note = checker()
            return bool(ok), str(note or "")
        except Exception as exc:  # provider 自己报错 = 不可用，原因要能显示
            return False, f"来源 provider 自检失败：{exc}"
    return True, ""


def _provider_search(provider: Any, *, query: str, source: str, limit: int) -> list:
    """调 provider 检索；把任何异常收敛成 ``LiteratureSourceError``。"""
    searcher = getattr(provider, "search", None)
    if not callable(searcher):
        raise LiteratureSourceError("来源 provider 没有 search 方法")
    try:
        try:
            rows = searcher(query=query, source=source, limit=limit)
        except TypeError:  # 兼容位置参数写法 search(query, limit)
            rows = searcher(query, limit)
    except LiteratureSourceError:
        raise
    except Exception as exc:
        raise LiteratureSourceError(f"{type(exc).__name__}: {exc}") from exc
    return list(rows or [])


def _invoke_model(
    llm: Any, messages: list[dict], *, capacity: Any, consumer: str,
    business_ref: str, user_id: str, sink: list[dict],
) -> str:
    """一次实际模型调用 + 取用/释放记录（§8.2 的业务侧证据）。

    真正的单模型并发闸门在 ``chatchat/server/model_capacity``；本模块只保证
    「每次实际调用前后各有一条取用/释放记录」，异常路径也在 ``finally`` 里
    释放。**绝不**自己实现锁。
    """
    if capacity is None:
        raise LiteratureUnavailable(
            "容量适配器尚未接入：论文相关的模型调用必须经过统一模型容量闸门"
        )
    record = {
        "index": len(sink) + 1,
        "consumer": consumer,
        "business_ref": business_ref,
        "user_id": user_id,
        "acquired_at": _now().isoformat(timespec="seconds"),
        "released_at": None,
        "release_reason": "",
        "outcome": "",
    }
    sink.append(record)
    token = None
    try:
        try:
            token = capacity.acquire(
                consumer=consumer, business_ref=business_ref, user_id=user_id
            )
        except TypeError:  # 兼容只接 consumer/business_ref 的适配器
            token = capacity.acquire(consumer=consumer, business_ref=business_ref)
    except Exception as exc:
        record["outcome"] = f"acquire_failed:{type(exc).__name__}"
        record["released_at"] = _now().isoformat(timespec="seconds")
        record["release_reason"] = record["outcome"]
        raise ModelCallError(f"取用模型容量失败：{type(exc).__name__}: {exc}") from exc
    try:
        result = llm.invoke(messages)
        content = getattr(result, "content", None)
        if content is None and isinstance(result, dict):
            content = result.get("content")
        if content is None and isinstance(result, str):
            content = result
        if not isinstance(content, str):
            raise ModelCallError("模型返回内容格式不正确")
        record["outcome"] = "ok"
        return content
    except ModelCallError:
        raise
    except Exception as exc:
        raise ModelCallError(f"模型调用失败：{type(exc).__name__}: {exc}") from exc
    finally:
        record["released_at"] = _now().isoformat(timespec="seconds")
        record["release_reason"] = (
            "completed" if record["outcome"] == "ok" else (record["outcome"] or "error")
        )
        try:
            capacity.release(token, reason=record["release_reason"])
        except TypeError:  # 兼容 release(token)
            capacity.release(token)


def _model_name(llm: Any) -> str:
    for attr in ("model_name", "model", "name"):
        value = getattr(llm, attr, None)
        if isinstance(value, str) and value:
            return value[:120]
    return type(llm).__name__[:120]


# ---------------------------------------------------------------------------
# 权限与定位
# ---------------------------------------------------------------------------


def _student_only(role: str, action: str) -> None:
    """文献决定、结论与授权由学生做（导师可见但不是决策方，§6.3、§10）。"""
    if role != ROLE_STUDENT:
        raise PermissionError(f"需要学生角色：{action}由学生本人作出")


def _mentor_only(role: str, action: str) -> None:
    if role != ROLE_MENTOR:
        raise PermissionError(f"需要主导师角色：{action}只由主导师作出")


def _get_task(
    session: Session, project: SupervisionProjectModel, task_ref: str
) -> SupervisionLiteratureSearchTaskModel:
    task = session.execute(
        select(SupervisionLiteratureSearchTaskModel).where(
            SupervisionLiteratureSearchTaskModel.project_id == project.id,
            (SupervisionLiteratureSearchTaskModel.id == task_ref)
            | (SupervisionLiteratureSearchTaskModel.code == task_ref),
            SupervisionLiteratureSearchTaskModel.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if task is None:
        raise LookupError("论文检索任务不存在")
    return task


def _get_subtask(
    session: Session, task: SupervisionLiteratureSearchTaskModel, subtask_ref: str
) -> SupervisionLiteratureSubtaskModel:
    row = session.execute(
        select(SupervisionLiteratureSubtaskModel).where(
            SupervisionLiteratureSubtaskModel.search_task_id == task.id,
            (SupervisionLiteratureSubtaskModel.id == subtask_ref)
            | (SupervisionLiteratureSubtaskModel.code == subtask_ref),
        )
    ).scalar_one_or_none()
    if row is None:
        raise LookupError("检索子任务不存在")
    return row


def _get_result(
    session: Session, project: SupervisionProjectModel, result_ref: str
) -> SupervisionLiteratureResultModel:
    row = session.execute(
        select(SupervisionLiteratureResultModel).where(
            SupervisionLiteratureResultModel.project_id == project.id,
            (SupervisionLiteratureResultModel.id == result_ref)
            | (SupervisionLiteratureResultModel.code == result_ref),
        )
    ).scalar_one_or_none()
    if row is None:
        raise LookupError("候选论文不存在")
    return row


def _get_conclusion(
    session: Session, project: SupervisionProjectModel, conclusion_ref: str
) -> SupervisionLiteratureConclusionModel:
    row = session.execute(
        select(SupervisionLiteratureConclusionModel)
        .where(
            SupervisionLiteratureConclusionModel.project_id == project.id,
            (SupervisionLiteratureConclusionModel.id == conclusion_ref)
            | (SupervisionLiteratureConclusionModel.code == conclusion_ref),
        )
        # 同一 code 有多个版本时按 code 取的是**最新**版本，历史版本用 id 或
        # 列表接口取；不排序会让多版本时抛 MultipleResultsFound。
        .order_by(SupervisionLiteratureConclusionModel.version.desc())
        .limit(1)
    ).scalars().first()
    if row is None:
        # 猜编号时按 code 取最新版本；取不到与不存在同一个 404。
        raise LookupError("文献结论不存在")
    return row


def _next_subtask_code(
    session: Session, task: SupervisionLiteratureSearchTaskModel, kind: str
) -> str:
    abbr = {
        "plan": "PL", "search": "SE", "rank": "RK", "abstract_analysis": "AA",
        "deep_extract": "DE", "compare": "CP", "conclusion": "CC",
    }.get(kind, "ST")
    count = int(
        session.execute(
            select(func.count(SupervisionLiteratureSubtaskModel.id)).where(
                SupervisionLiteratureSubtaskModel.search_task_id == task.id,
                SupervisionLiteratureSubtaskModel.kind == kind,
            )
        ).scalar()
        or 0
    )
    return f"{task.code}-{abbr}{count + 1:02d}"


def _new_subtask(
    session: Session, task: SupervisionLiteratureSearchTaskModel, *, kind: str,
    actor_id: str, role: str, source: str = "", query: str = "", payload: Optional[dict] = None,
) -> SupervisionLiteratureSubtaskModel:
    """建一条子任务行。``kind`` 必须是 §15.3 第3条列出的那几种。"""
    if kind not in SUBTASK_KIND_VALUES:
        raise WorkValidationError(f"未知的子任务种类：{kind}")
    now = _now()
    row = SupervisionLiteratureSubtaskModel(
        id=new_id(),
        code=_next_subtask_code(session, task, kind),
        project_id=task.project_id,
        search_task_id=task.id,
        search_task_code=task.code,
        kind=kind,
        status="queued",
        attempt=1,
        source=source[:50],
        query=query[:2000],
        payload=payload or {},
        queued_at=now,
        created_by=actor_id,
        created_role=role,
        create_time=now,
        update_time=now,
    )
    session.add(row)
    session.flush()
    return row


def _finish_subtask(
    session: Session, subtask: SupervisionLiteratureSubtaskModel, *, status: str,
    error_code: str = "", error_message: str = "",
    result_total: Optional[int] = None, new_total: Optional[int] = None,
    duplicate_total: Optional[int] = None, model_calls: Optional[list] = None,
    consumer: str = "", model_name: str = "", prompt_version: str = "",
) -> SupervisionLiteratureSubtaskModel:
    """收敛子任务状态。失败原因保留，历史进 ``failure_history``。"""
    if status not in SUBTASK_STATUS_VALUES:
        raise WorkValidationError(f"未知的子任务状态：{status}")
    if status == "failed" and (error_code or error_message):
        subtask.failure_history = list(subtask.failure_history or []) + [{
            "attempt": int(subtask.attempt or 1),
            "error_code": error_code[:64],
            "error_message": (error_message or "")[:2000],
            "finished_at": _ts(_now()),
        }]
    subtask.status = status
    subtask.error_code = error_code[:64]
    subtask.error_message = (error_message or "")[:2000]
    if result_total is not None:
        subtask.result_total = int(result_total)
    if new_total is not None:
        subtask.new_total = int(new_total)
    if duplicate_total is not None:
        subtask.duplicate_total = int(duplicate_total)
    if model_calls is not None:
        subtask.model_calls = list(model_calls)
    if consumer:
        subtask.consumer = consumer[:32]
    if model_name:
        subtask.model_name = model_name[:120]
    if prompt_version:
        subtask.prompt_version = prompt_version[:32]
    subtask.finished_at = _now()
    subtask.update_time = _now()
    session.flush()
    return subtask


# ---------------------------------------------------------------------------
# 检索任务：创建、读取、修改、关闭
# ---------------------------------------------------------------------------


def _brief_version_content(
    session: Session, brief_id: str, version: int
) -> Optional[dict]:
    row = session.execute(
        select(SupervisionResearchBriefVersionModel).where(
            SupervisionResearchBriefVersionModel.brief_id == brief_id,
            SupervisionResearchBriefVersionModel.version == version,
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return {field: getattr(row, field) for field in BRIEF_CONTENT_FIELDS}


def create_task(
    session: Session,
    project_ref: str,
    user_id: str,
    *,
    brief_ref: str,
    goal: str,
    purpose: str = "verify",
    keywords: Optional[Iterable[str]] = None,
    synonyms: Optional[Iterable[str]] = None,
    exclude_terms: Optional[Iterable[str]] = None,
    sources: Optional[Iterable[str]] = None,
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
    require_fulltext: bool = False,
    result_limit: int = 30,
    deep_extract_limit: Optional[int] = None,
    as_draft: bool = False,
) -> SupervisionLiteratureSearchTaskModel:
    """创建论文检索任务；**必须**绑定已确认并共享的 ``ResearchBrief`` 版本。

    只有草稿、或从没确认共享过 → ``WorkError``（409），错误信息说明为什么
    不能创建（§16.4 第1条、§4.3.1 确认门槛）。别人的未确认草稿按 404
    （``LookupError``），不泄露其存在。
    """
    project, role = require_member(session, project_ref, user_id)
    goal = _clean_text(goal, 4000)
    if not goal:
        raise WorkValidationError("必须写明检索目标：要验证、补充或反驳什么")
    if purpose not in TASK_PURPOSE_VALUES:
        raise WorkValidationError(
            "检索目标性质只能是 " + "/".join(TASK_PURPOSE_VALUES)
        )
    try:
        result_limit = int(result_limit)
    except (TypeError, ValueError):
        raise WorkValidationError("结果上限必须是整数")
    if not 1 <= result_limit <= 200:
        raise WorkValidationError("结果上限必须在 1 到 200 之间")
    if deep_extract_limit is None:
        deep_extract_limit = _int_env("CHATCHAT_LITERATURE_DEFAULT_DEEP_EXTRACT", 3)
    try:
        deep_extract_limit = int(deep_extract_limit)
    except (TypeError, ValueError):
        raise WorkValidationError("深度提取上限必须是整数")
    if not 0 <= deep_extract_limit <= 20:
        raise WorkValidationError("深度提取上限必须在 0 到 20 之间")
    for label, low, high in (("时间范围", year_from, year_to),):
        if low is not None and high is not None and int(low) > int(high):
            raise WorkValidationError(f"{label}不合法：起始年份晚于结束年份")

    brief = session.execute(
        select(SupervisionResearchBriefModel).where(
            SupervisionResearchBriefModel.project_id == project.id,
            (SupervisionResearchBriefModel.id == brief_ref)
            | (SupervisionResearchBriefModel.code == brief_ref),
            SupervisionResearchBriefModel.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if brief is None:
        raise LookupError("研究定义不存在")
    if brief.owner_user_id != user_id and brief.shared_version is None:
        # 别人还没共享的草稿：与"不存在"同一个答案，避免枚举别人的草稿。
        raise LookupError("研究定义不存在")
    if brief.shared_version is None or brief.status != "confirmed":
        raise WorkError(
            f"检索任务必须绑定已确认并共享的结构化研究定义：{brief.code} 当前状态是"
            f"「{brief.status}」，还没有确认过的版本。请先在研究讨论页确认本版研究定义，"
            "再创建检索任务（未确认的内容只能作为讨论线索，不能驱动正式检索）。"
        )
    pinned = int(brief.shared_version)
    content = _brief_version_content(session, brief.id, pinned)
    if content is None:
        raise WorkError(
            f"{brief.code} 已标记为确认共享，但版本 v{pinned} 的结构化内容缺失，"
            "无法作为检索依据；请重新确认研究定义后再创建检索任务。"
        )

    now = _now()
    for _ in range(5):
        task = SupervisionLiteratureSearchTaskModel(
            id=new_id(),
            code=next_code(
                session, SupervisionLiteratureSearchTaskModel,
                SupervisionLiteratureSearchTaskModel.code, CODE_SEARCH_TASK,
            ),
            project_id=project.id,
            brief_id=brief.id,
            brief_code=brief.code,
            brief_version=pinned,
            brief_kind=brief.kind,
            brief_confirmed_by=brief.confirmed_by,
            brief_confirmed_at=brief.confirmed_at,
            brief_snapshot=content,
            purpose=purpose,
            goal=goal,
            keywords=[_clean_text(k, 300) for k in (keywords or []) if _clean_text(k, 300)][:50],
            synonyms=[_clean_text(s, 300) for s in (synonyms or []) if _clean_text(s, 300)][:50],
            exclude_terms=[_clean_text(t, 300) for t in (exclude_terms or []) if _clean_text(t, 300)][:50],
            sources=[_clean_text(s, 50) for s in (sources or []) if _clean_text(s, 50)][:10],
            year_from=year_from, year_to=year_to,
            require_fulltext=bool(require_fulltext),
            result_limit=result_limit,
            deep_extract_limit=deep_extract_limit,
            status="draft" if as_draft else "pending",
            created_by=user_id, created_role=role,
            create_time=now, update_time=now,
        )
        session.add(task)
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            continue
        record_audit(
            session, project_id=project.id, object_type="literature_task",
            object_code=task.code, actor_id=user_id, actor_role=role,
            action="创建论文检索任务",
            detail=f"{task.code} 依据 {brief.code} v{pinned}（确认于 {_ts(brief.confirmed_at)}）",
        )
        return task
    raise RuntimeError("检索任务编号生成失败，请重试")


def list_tasks(
    session: Session, project_ref: str, user_id: str, *, status: str = ""
) -> list[SupervisionLiteratureSearchTaskModel]:
    project, _ = require_member(session, project_ref, user_id)
    stmt = (
        select(SupervisionLiteratureSearchTaskModel)
        .where(
            SupervisionLiteratureSearchTaskModel.project_id == project.id,
            SupervisionLiteratureSearchTaskModel.deleted_at.is_(None),
        )
        .order_by(SupervisionLiteratureSearchTaskModel.create_time.desc())
    )
    if status:
        stmt = stmt.where(SupervisionLiteratureSearchTaskModel.status == status)
    return list(session.execute(stmt).scalars().all())


def get_task(
    session: Session, project_ref: str, user_id: str, task_ref: str
) -> SupervisionLiteratureSearchTaskModel:
    project, _ = require_member(session, project_ref, user_id)
    return _get_task(session, project, task_ref)


EDITABLE_TASK_FIELDS = (
    "goal", "purpose", "keywords", "synonyms", "exclude_terms", "sources",
    "year_from", "year_to", "require_fulltext", "result_limit", "deep_extract_limit",
)


def update_task(
    session: Session, project_ref: str, user_id: str, task_ref: str, fields: dict
) -> SupervisionLiteratureSearchTaskModel:
    """修改检索输入。已经开始检索后不能再改输入（§4.4 依据固定）。"""
    project, role = require_member(session, project_ref, user_id)
    task = _get_task(session, project, task_ref)
    # 只有还没跑过的任务能改输入：检索一旦跑过，来源/关键词/上限就是当时的
    # 依据，改了会让已入库的候选与排序对不上账（§4.4 依据固定）。
    if task.status not in ("draft", "pending"):
        raise WorkError(
            f"检索任务当前是「{task.status}」，不能修改检索输入；"
            "已检索过的任务请新建一个检索任务，失败批次请用子任务重试"
        )
    unknown = set(fields) - set(EDITABLE_TASK_FIELDS)
    if unknown:
        raise WorkValidationError("不能修改的字段：" + "、".join(sorted(unknown)))
    if "purpose" in fields and fields["purpose"] not in TASK_PURPOSE_VALUES:
        raise WorkValidationError("检索目标性质只能是 " + "/".join(TASK_PURPOSE_VALUES))
    if "goal" in fields:
        goal = _clean_text(fields["goal"], 4000)
        if not goal:
            raise WorkValidationError("必须写明检索目标")
        task.goal = goal
    for name in ("keywords", "synonyms", "exclude_terms", "sources"):
        if name in fields:
            limit = 10 if name == "sources" else 50
            width = 50 if name == "sources" else 300
            setattr(task, name, [
                _clean_text(v, width) for v in (fields[name] or []) if _clean_text(v, width)
            ][:limit])
    for name in ("year_from", "year_to"):
        if name in fields:
            value = fields[name]
            setattr(task, name, int(value) if value not in (None, "") else None)
    if task.year_from is not None and task.year_to is not None and task.year_from > task.year_to:
        raise WorkValidationError("时间范围不合法：起始年份晚于结束年份")
    if "result_limit" in fields:
        try:
            value = int(fields["result_limit"])
        except (TypeError, ValueError):
            raise WorkValidationError("结果上限必须是整数")
        if not 1 <= value <= 200:
            raise WorkValidationError("结果上限必须在 1 到 200 之间")
        task.result_limit = value
    if "deep_extract_limit" in fields:
        try:
            value = int(fields["deep_extract_limit"])
        except (TypeError, ValueError):
            raise WorkValidationError("深度提取上限必须是整数")
        if not 0 <= value <= 20:
            raise WorkValidationError("深度提取上限必须在 0 到 20 之间")
        task.deep_extract_limit = value
    if "require_fulltext" in fields:
        task.require_fulltext = bool(fields["require_fulltext"])
    task.update_time = _now()
    session.flush()
    record_audit(
        session, project_id=project.id, object_type="literature_task",
        object_code=task.code, actor_id=user_id, actor_role=role,
        action="修改检索输入", detail="、".join(sorted(fields))[:2000],
    )
    return task


def close_task(
    session: Session, project_ref: str, user_id: str, task_ref: str, *, reason: str
) -> SupervisionLiteratureSearchTaskModel:
    project, role = require_member(session, project_ref, user_id)
    task = _get_task(session, project, task_ref)
    if task.status == "closed":
        raise WorkError("该检索任务已关闭")
    if task.status == "searching":
        raise WorkError("检索正在执行，先取消正在运行的子任务再关闭任务")
    reason = _clean_text(reason, 2000)
    if not reason:
        raise WorkValidationError("关闭检索任务必须写明原因")
    task.status = "closed"
    task.closed_at = _now()
    task.close_reason = reason
    task.update_time = _now()
    session.flush()
    record_audit(
        session, project_id=project.id, object_type="literature_task",
        object_code=task.code, actor_id=user_id, actor_role=role,
        action="关闭论文检索任务", detail=reason[:2000],
    )
    return task


# ---------------------------------------------------------------------------
# 检索方案：一次短调用 + 学生确认（§6.3.1 第1步）
# ---------------------------------------------------------------------------


def generate_search_plan(
    session: Session, project_ref: str, user_id: str, task_ref: str, *,
    llm: Any, capacity: Any = None, note: str = "",
) -> SupervisionLiteratureSearchTaskModel:
    """用一次短调用生成检索方案（消费者 ``research_plan``，已登记）。

    重新生成会清掉上一次的确认时间：方案变了就必须重新确认，
    不能拿旧确认去启动新方案（§6.3.1 第1步）。模型失败只把子任务标 failed，
    任务本身与已有结果不受影响。
    """
    if llm is None or not hasattr(llm, "invoke"):
        raise LiteratureUnavailable(
            "模型能力尚未接入：生成检索方案需要注入生成模型（经统一模型容量闸门）"
        )
    project, role = require_member(session, project_ref, user_id)
    task = _get_task(session, project, task_ref)
    if task.status == "closed":
        raise WorkError("该检索任务已关闭")
    subtask = _new_subtask(
        session, task, kind="plan", actor_id=user_id, role=role,
        payload={"note": _clean_text(note, 2000)},
    )
    subtask.status = "running"
    subtask.started_at = _now()
    session.flush()

    calls: list[dict] = []
    try:
        raw = _invoke_model(
            llm, build_plan_prompt(task), capacity=capacity,
            consumer="research_plan", business_ref=task.code, user_id=user_id,
            sink=calls,
        )
    except ModelCallError as exc:
        _finish_subtask(
            session, subtask, status="failed", error_code="model_call_failed",
            error_message=str(exc), model_calls=calls, consumer="research_plan",
            model_name=_model_name(llm), prompt_version=PROMPT_VERSION,
        )
        return task
    payload = parse_json_payload(raw)
    if payload is None:
        _finish_subtask(
            session, subtask, status="failed", error_code="output_not_structured",
            error_message="模型没有返回合法的检索方案 JSON，本次未形成可用检索方案",
            model_calls=calls, consumer="research_plan", model_name=_model_name(llm),
            prompt_version=PROMPT_VERSION,
        )
        return task

    queries = []
    for item in payload.get("queries") or []:
        if not isinstance(item, dict):
            continue
        query = _clean_text(item.get("query"), 500)
        if not query:
            continue
        queries.append({
            "query": query,
            "source": _clean_text(item.get("source"), 50),
            "rationale": _clean_text(item.get("rationale"), 500),
        })
    if not queries:
        _finish_subtask(
            session, subtask, status="failed", error_code="output_empty",
            error_message="模型返回的检索方案里没有任何检索式，未采用",
            model_calls=calls, consumer="research_plan", model_name=_model_name(llm),
            prompt_version=PROMPT_VERSION,
        )
        return task

    plan = {
        "queries": queries[:MAX_KEYWORD_BATCHES],
        "exclude_terms": [
            _clean_text(t, 300) for t in (payload.get("exclude_terms") or [])
            if _clean_text(t, 300)
        ][:50],
        "year_from": payload.get("year_from"),
        "year_to": payload.get("year_to"),
        "note": _clean_text(payload.get("note"), 2000),
    }
    task.search_plan = plan
    task.search_plan_note = _clean_text(note, 2000)
    task.search_plan_model = _model_name(llm)
    task.search_plan_prompt_version = PROMPT_VERSION
    task.search_plan_generated_at = _now()
    # 方案换了就必须重新确认；旧确认不能沿用。
    task.search_plan_confirmed_at = None
    task.search_plan_confirmed_by = ""
    task.update_time = _now()
    _finish_subtask(
        session, subtask, status="completed", result_total=len(queries),
        model_calls=calls, consumer="research_plan", model_name=_model_name(llm),
        prompt_version=PROMPT_VERSION,
    )
    record_audit(
        session, project_id=project.id, object_type="literature_task",
        object_code=task.code, actor_id=user_id, actor_role=role,
        action="生成检索方案",
        detail=f"{subtask.code}：{len(queries)} 条检索式（模型 {task.search_plan_model}，"
               f"提示词 {PROMPT_VERSION}）；等待学生确认",
    )
    return task


def confirm_search_plan(
    session: Session, project_ref: str, user_id: str, task_ref: str, *, note: str = ""
) -> SupervisionLiteratureSearchTaskModel:
    """学生确认检索方案；确认后才允许开始搜索（§6.3.1 第1步）。"""
    project, role = require_member(session, project_ref, user_id)
    task = _get_task(session, project, task_ref)
    if task.status == "closed":
        raise WorkError("该检索任务已关闭")
    if not (task.search_plan or {}).get("queries"):
        raise WorkError(
            "还没有生成检索方案：先用一次短调用生成检索方案，确认后才能开始搜索"
        )
    task.search_plan_confirmed_at = _now()
    task.search_plan_confirmed_by = user_id
    task.search_plan_note = _clean_text(note, 2000) or task.search_plan_note
    if task.status == "draft":
        task.status = "pending"
    task.update_time = _now()
    session.flush()
    record_audit(
        session, project_id=project.id, object_type="literature_task",
        object_code=task.code, actor_id=user_id, actor_role=role,
        action="确认检索方案", detail=_clean_text(note, 2000),
    )
    return task


# ---------------------------------------------------------------------------
# 搜索与去重：按「来源 × 关键词批次」拆子任务，单个失败不外溢
# ---------------------------------------------------------------------------


def _keyword_batches(task: SupervisionLiteratureSearchTaskModel) -> list[dict]:
    """拆成可独立重试的检索批次（§15.3 第1、3条）。

    已确认的检索方案优先：搜索按学生确认过的那几条检索式跑，没确认方案才退回
    关键词与同义词组合。
    """
    plan = task.search_plan or {}
    queries = [
        q for q in (plan.get("queries") or [])
        if isinstance(q, dict) and _clean_text(q.get("query"), 500)
    ]
    if queries:
        return [
            {
                "query": _clean_text(q.get("query"), 500),
                "rationale": _clean_text(q.get("rationale"), 500) or "已确认的检索方案",
            }
            for q in queries
        ][:MAX_KEYWORD_BATCHES]
    keywords = [_clean_text(k, 300) for k in (task.keywords or []) if _clean_text(k, 300)]
    synonyms = [_clean_text(s, 300) for s in (task.synonyms or []) if _clean_text(s, 300)]
    batches: list[dict] = []
    if keywords:
        batches.append({
            "query": " ".join(keywords)[:500],
            "rationale": "研究定义关键词组合",
        })
    for syn in synonyms[: MAX_KEYWORD_BATCHES - 1]:
        head = " ".join(keywords[:3])
        batches.append({
            "query": f"{syn} {head}".strip()[:500],
            "rationale": f"同义词扩展：{syn}",
        })
    if not batches:
        question = _clean_text((task.brief_snapshot or {}).get("question"), 500)
        if question:
            batches.append({"query": question, "rationale": "核心研究问题原文"})
    return batches[:MAX_KEYWORD_BATCHES]


def _store_candidate(
    session: Session, *, task: SupervisionLiteratureSearchTaskModel,
    subtask: SupervisionLiteratureSubtaskModel, raw: Any,
) -> str:
    """入库一条候选，返回 ``new`` / ``duplicate`` / ``invalid``。

    先按三种幂等键查已有记录，命中就把来源并进 ``sources``（去重不丢可追溯
    性）；没命中再插入，插入撞库层部分唯一索引时同样按重复处理——并发或
    服务层漏判都只会产生一条记录（§6.3 第4条）。
    """
    candidate = candidate_from_raw(raw)
    if candidate is None:
        return "invalid"
    key = dedup_key_of(candidate)
    if not key:
        return "invalid"
    conditions = [SupervisionLiteratureResultModel.dedup_key == key]
    if candidate.doi:
        conditions.append(SupervisionLiteratureResultModel.doi == candidate.doi)
    if candidate.arxiv_id:
        conditions.append(SupervisionLiteratureResultModel.arxiv_id == candidate.arxiv_id)
    tkey = paper_title_key(candidate.title, candidate.year)
    if tkey:
        conditions.append(SupervisionLiteratureResultModel.title_key == tkey)
    existing = session.execute(
        select(SupervisionLiteratureResultModel).where(
            SupervisionLiteratureResultModel.project_id == task.project_id,
            or_(*conditions),
        )
    ).scalars().first()
    if existing is not None:
        merged = list(existing.sources or [])
        entry = {"source": candidate.source, "url": candidate.url or ""}
        if entry not in merged:
            merged.append(entry)
            existing.sources = merged
            existing.update_time = _now()
            session.flush()
        return "duplicate"

    ranking = rank_candidate(candidate, task.brief_snapshot or {})
    now = _now()
    for _ in range(5):
        row = SupervisionLiteratureResultModel(
            id=new_id(),
            code=next_code(
                session, SupervisionLiteratureResultModel,
                SupervisionLiteratureResultModel.code, CODE_RESULT,
            ),
            project_id=task.project_id,
            search_task_id=task.id,
            subtask_id=subtask.id,
            dedup_key=key,
            doi=candidate.doi,
            arxiv_id=candidate.arxiv_id,
            title_key=tkey,
            title=candidate.title[:1000],
            authors="; ".join(candidate.authors)[:2000],
            year=candidate.year,
            abstract=candidate.abstract[:8000],
            url=normalize_url(candidate.url),
            source=candidate.source[:50],
            sources=[{"source": candidate.source, "url": candidate.url or ""}]
            + list(candidate.merged_sources or []),
            fulltext_status=candidate.fulltext_status,
            relevance_level=ranking["level"],
            relevance_reason=ranking["reason"],
            relevance_matched=ranking["matched"],
            relevance_missing=ranking["missing"],
            relevance_score_source=ranking["score_source"],
            relevance_score=ranking["score"],
            relevance_rules_version=ranking["rules_version"],
            recommended_action=ranking["recommended_action"],
            decision="pending",
            extract_state="none",
            first_seen_at=now,
            create_time=now,
            update_time=now,
        )
        session.add(row)
        try:
            # 保存点：撞唯一索引只回滚这一条，不牵连本批次已入库的候选。
            with session.begin_nested():
                session.flush()
        except IntegrityError:
            # 并发或服务层漏判：库层唯一索引已经拦下，按重复处理，
            # 结果仍然是「同一篇论文只有一条记录」。
            if row in session:
                session.expunge(row)
            return "duplicate"
        return "new"
    return "invalid"


def _run_search_subtask(
    session: Session, task: SupervisionLiteratureSearchTaskModel,
    subtask: SupervisionLiteratureSubtaskModel, *, provider: Any,
) -> dict:
    """执行一个搜索子任务：命中、去重、入库计数都写在自己行上。"""
    limit = min(int(task.result_limit or 30), MAX_HITS_PER_SOURCE)
    subtask.status = "running"
    subtask.started_at = _now()
    session.flush()
    try:
        rows = _provider_search(
            provider, query=subtask.query, source=subtask.source, limit=limit
        )
    except LiteratureSourceError as exc:
        _finish_subtask(
            session, subtask, status="failed", error_code="source_failed",
            error_message=str(exc)[:2000],
        )
        return {"source": subtask.source, "query": subtask.query, "reason": str(exc)}

    new_total = duplicate_total = invalid_total = 0
    for raw in rows:
        outcome = _store_candidate(session, task=task, subtask=subtask, raw=raw)
        if outcome == "new":
            new_total += 1
        elif outcome == "duplicate":
            duplicate_total += 1
        else:
            invalid_total += 1
    _finish_subtask(
        session, subtask, status="completed", result_total=len(rows),
        new_total=new_total, duplicate_total=duplicate_total,
    )
    if invalid_total:
        subtask.error_code = "invalid_candidates"
        subtask.error_message = f"{invalid_total} 条候选缺少标题或稳定标识，已跳过"
        session.flush()
    return {}


def run_search(
    session: Session, project_ref: str, user_id: str, task_ref: str, *,
    provider: Any, sources: Optional[Iterable[str]] = None,
    keyword_batches: Optional[Iterable[dict]] = None,
) -> SupervisionLiteratureSearchTaskModel:
    """执行检索：每个「来源 × 关键词批次」一个子任务，失败互不影响。

    检索**不会**自动深度提取任何论文（§16.4 第3条）：这里只做来源检索、
    去重入库和低成本规则排序；深度提取必须由学生对单篇显式发起。
    """
    ok, note = provider_is_available(provider)
    if not ok:
        raise LiteratureUnavailable(
            note or "外部论文来源尚未接入：检索需要注入来源 provider"
        )
    project, role = require_member(session, project_ref, user_id)
    task = _get_task(session, project, task_ref)
    if task.status == "closed":
        raise WorkError("该检索任务已关闭")
    if task.status == "concluded":
        raise WorkError("该检索任务已形成文献结论；要换依据重新检索请新建检索任务")
    if not (task.search_plan or {}).get("queries") or not task.search_plan_confirmed_at:
        raise WorkError(
            "检索方案尚未生成或尚未经学生确认：先用一次短调用生成检索方案并确认，"
            "再开始搜索（检索方案未经确认不得直接检索）"
        )
    chosen_sources = [
        _clean_text(s, 50) for s in (sources or task.sources or []) if _clean_text(s, 50)
    ][:10]
    if not chosen_sources:
        raise WorkValidationError(
            "必须指定至少一个论文来源（例如 arxiv / crossref / semantic_scholar / local_kb）"
        )
    if keyword_batches:
        batches = [
            {"query": _clean_text(b.get("query"), 500),
             "rationale": _clean_text(b.get("rationale"), 500)}
            for b in keyword_batches if isinstance(b, dict) and _clean_text(b.get("query"), 500)
        ][:MAX_KEYWORD_BATCHES]
    else:
        batches = _keyword_batches(task)
    if not batches:
        raise WorkValidationError(
            "没有任何检索批次：请填写检索关键词，或先确认包含检索式的检索方案"
        )

    task.status = "searching"
    task.update_time = _now()
    session.flush()

    failures: list[dict] = []
    hits_total = new_total = duplicate_total = 0
    for source in chosen_sources:
        for batch in batches:
            subtask = _new_subtask(
                session, task, kind="search", actor_id=user_id, role=role,
                source=source, query=batch["query"],
                payload={"rationale": batch.get("rationale", "")},
            )
            failure = _run_search_subtask(session, task, subtask, provider=provider)
            if failure:
                failures.append(failure)
                continue
            hits_total += int(subtask.result_total or 0)
            new_total += int(subtask.new_total or 0)
            duplicate_total += int(subtask.duplicate_total or 0)

    task.hits_total = int(task.hits_total or 0) + hits_total
    task.stored_total = int(task.stored_total or 0) + new_total
    task.deduped_total = int(task.deduped_total or 0) + duplicate_total
    task.source_failures = (list(task.source_failures or []) + failures)[:50]
    task.searched_at = _now()
    task.status = "triage"
    task.update_time = _now()

    # 低成本规则排序子任务：首版排序在入库时已算好，这里记账并固化规则版本，
    # 页面据此显示"排序规则版本"（§6.3.B）。
    rank_subtask = _new_subtask(
        session, task, kind="rank", actor_id=user_id, role=role,
        payload={"rules_version": RULES_VERSION},
    )
    _finish_subtask(
        session, rank_subtask, status="completed", result_total=new_total,
    )
    record_audit(
        session, project_id=project.id, object_type="literature_task",
        object_code=task.code, actor_id=user_id, actor_role=role,
        action="执行论文检索",
        detail=f"来源 {'、'.join(chosen_sources)}；批次 {len(batches)}；"
               f"命中 {hits_total}；去重合并 {duplicate_total}；新入库 {new_total}；"
               f"失败批次 {len(failures)}",
    )
    return task


# ---------------------------------------------------------------------------
# 子任务：列表、重试、取消、重启后收敛
# ---------------------------------------------------------------------------


def list_subtasks(
    session: Session, project_ref: str, user_id: str, task_ref: str, *,
    kind: str = "", status: str = "", limit: int = 50,
) -> list[SupervisionLiteratureSubtaskModel]:
    project, _ = require_member(session, project_ref, user_id)
    task = _get_task(session, project, task_ref)
    return _subtasks_of(session, task, kind=kind, status=status, limit=limit)


def _subtasks_of(
    session: Session, task: SupervisionLiteratureSearchTaskModel, *,
    kind: str = "", status: str = "", limit: int = 50,
) -> list[SupervisionLiteratureSubtaskModel]:
    stmt = (
        select(SupervisionLiteratureSubtaskModel)
        .where(SupervisionLiteratureSubtaskModel.search_task_id == task.id)
        .order_by(SupervisionLiteratureSubtaskModel.create_time.desc())
        .limit(max(1, min(limit, 200)))
    )
    if kind:
        stmt = stmt.where(SupervisionLiteratureSubtaskModel.kind == kind)
    if status:
        stmt = stmt.where(SupervisionLiteratureSubtaskModel.status == status)
    return list(session.execute(stmt).scalars().all())


def retry_subtask(
    session: Session, project_ref: str, user_id: str, task_ref: str, subtask_ref: str, *,
    provider: Any = None, llm: Any = None, capacity: Any = None,
) -> SupervisionLiteratureSubtaskModel:
    """重试一个失败/已取消的子任务；只重跑它自己（§15.3 第3、6条）。

    重试是同一条子任务的新一次尝试：``attempt`` 递增，上一次的失败原因进
    ``failure_history``，其它子任务与整个任务的状态不动。
    """
    project, role = require_member(session, project_ref, user_id)
    task = _get_task(session, project, task_ref)
    subtask = _get_subtask(session, task, subtask_ref)
    if subtask.status not in ("failed", "cancelled"):
        raise WorkError(
            f"子任务 {subtask.code} 当前是「{subtask.status}」，只有失败或已取消的子任务能重试"
        )
    if task.status == "closed":
        raise WorkError("该检索任务已关闭，不能重试子任务")
    subtask.attempt = int(subtask.attempt or 1) + 1
    subtask.status = "queued"
    subtask.queued_at = _now()
    subtask.finished_at = None
    subtask.cancelled_at = None
    subtask.cancelled_by = ""
    subtask.cancel_reason = ""
    subtask.update_time = _now()
    session.flush()
    record_audit(
        session, project_id=project.id, object_type="literature_subtask",
        object_code=subtask.code, actor_id=user_id, actor_role=role,
        action="重试检索子任务", detail=f"第 {subtask.attempt} 次尝试",
    )

    if subtask.kind == "search":
        ok, note = provider_is_available(provider)
        if not ok:
            _finish_subtask(
                session, subtask, status="failed", error_code="provider_not_wired",
                error_message=note or "未注入论文来源 provider",
            )
            return subtask
        _run_search_subtask(session, task, subtask, provider=provider)
        return subtask
    if subtask.kind in ("deep_extract", "abstract_analysis", "compare", "plan"):
        if llm is None or not hasattr(llm, "invoke"):
            _finish_subtask(
                session, subtask, status="failed", error_code="model_not_wired",
                error_message="模型能力尚未接入：该子任务需要注入生成模型",
            )
            return subtask
        if subtask.kind == "deep_extract":
            result = _get_result(session, project, str((subtask.payload or {}).get("result_code") or ""))
            return _run_deep_extract(
                session, task=task, result=result, subtask=subtask,
                llm=llm, capacity=capacity, actor_id=user_id, role=role,
            )
        if subtask.kind == "abstract_analysis":
            return _run_abstract_analysis(
                session, task=task, subtask=subtask, llm=llm, capacity=capacity,
                actor_id=user_id, role=role,
                result_codes=list((subtask.payload or {}).get("result_codes") or []),
            )
        if subtask.kind == "compare":
            return _run_compare(
                session, task=task, subtask=subtask, llm=llm, capacity=capacity,
                actor_id=user_id, role=role,
                result_codes=list((subtask.payload or {}).get("result_codes") or []),
            )
        return generate_search_plan(
            session, project.id, user_id, task.id, llm=llm, capacity=capacity,
            note=str((subtask.payload or {}).get("note") or ""),
        )
    if subtask.kind == "rank":
        return _rerun_rank(session, task, subtask)
    raise WorkError(f"子任务 {subtask.code}（{subtask.kind}）不支持自动重试，请重新发起该步骤")


def _rerun_rank(
    session: Session, task: SupervisionLiteratureSearchTaskModel,
    subtask: SupervisionLiteratureSubtaskModel,
) -> SupervisionLiteratureSubtaskModel:
    """按当前研究定义副本重算规则排序（不调模型，可安全重跑）。"""
    rows = list(
        session.execute(
            select(SupervisionLiteratureResultModel).where(
                SupervisionLiteratureResultModel.search_task_id == task.id
            )
        ).scalars().all()
    )
    subtask.status = "running"
    subtask.started_at = _now()
    session.flush()
    for row in rows:
        candidate = candidate_from_raw({
            "title": row.title, "source": row.source, "year": row.year,
            "abstract": row.abstract, "doi": row.doi, "arxiv_id": row.arxiv_id,
            "url": row.url, "fulltext_status": row.fulltext_status,
        })
        if candidate is None:
            continue
        ranking = rank_candidate(candidate, task.brief_snapshot or {})
        row.relevance_level = ranking["level"]
        row.relevance_reason = ranking["reason"]
        row.relevance_matched = ranking["matched"]
        row.relevance_missing = ranking["missing"]
        row.relevance_score_source = ranking["score_source"]
        row.relevance_score = ranking["score"]
        row.relevance_rules_version = ranking["rules_version"]
        row.recommended_action = ranking["recommended_action"]
        row.update_time = _now()
    _finish_subtask(session, subtask, status="completed", result_total=len(rows))
    return subtask


def cancel_subtask(
    session: Session, project_ref: str, user_id: str, task_ref: str, subtask_ref: str, *,
    reason: str = "",
) -> SupervisionLiteratureSubtaskModel:
    """取消排队中/运行中的子任务；已完成的不动（§15.3 的「可取消」）。"""
    project, role = require_member(session, project_ref, user_id)
    task = _get_task(session, project, task_ref)
    subtask = _get_subtask(session, task, subtask_ref)
    if subtask.status not in ("queued", "running", "pending"):
        raise WorkError(
            f"子任务 {subtask.code} 当前是「{subtask.status}」，只有未结束的子任务能取消"
        )
    subtask.status = "cancelled"
    subtask.cancelled_by = user_id
    subtask.cancelled_at = _now()
    subtask.cancel_reason = _clean_text(reason, 2000)
    subtask.finished_at = _now()
    subtask.update_time = _now()
    if subtask.kind == "deep_extract":
        result_id = str((subtask.payload or {}).get("result_id") or "")
        row = session.get(SupervisionLiteratureResultModel, result_id) if result_id else None
        if row is not None and row.extract_state in ("queued", "running"):
            row.extract_state = "cancelled"
            row.update_time = _now()
    session.flush()
    record_audit(
        session, project_id=project.id, object_type="literature_subtask",
        object_code=subtask.code, actor_id=user_id, actor_role=role,
        action="取消检索子任务", detail=_clean_text(reason, 2000),
    )
    return subtask


def converge_interrupted_subtasks(
    session: Session, *, project_id: str = ""
) -> int:
    """服务重启后收敛遗留的 ``running``/``queued`` 子任务（§8.2 最后一段）。

    首版没有后台 worker，进程内正在跑的子任务随进程一起没了，绝不能继续
    显示「运行中」：一律标 failed 并写明「服务重启导致中断」，学生可以重试。
    真实部署在服务启动时调用一次；恢复动作写审计。
    """
    stmt = select(SupervisionLiteratureSubtaskModel).where(
        SupervisionLiteratureSubtaskModel.status.in_(("running", "queued"))
    )
    if project_id:
        stmt = stmt.where(SupervisionLiteratureSubtaskModel.project_id == project_id)
    rows = list(session.execute(stmt).scalars().all())
    for subtask in rows:
        task = session.get(SupervisionLiteratureSearchTaskModel, subtask.search_task_id)
        _finish_subtask(
            session, subtask, status="failed", error_code="interrupted_by_restart",
            error_message="服务重启导致中断：该子任务未继续执行，可重试",
        )
        if subtask.kind == "deep_extract":
            result_id = str((subtask.payload or {}).get("result_id") or "")
            row = session.get(SupervisionLiteratureResultModel, result_id) if result_id else None
            if row is not None and row.extract_state in ("queued", "running"):
                row.extract_state = "failed"
                row.update_time = _now()
        if task is not None and task.status == "searching":
            task.status = "triage"
            task.update_time = _now()
        record_audit(
            session, project_id=subtask.project_id, object_type="literature_subtask",
            object_code=subtask.code, actor_id="system", actor_role="",
            action="收敛中断的检索子任务", detail="服务重启导致中断，标记为失败并可重试",
        )
    session.flush()
    return len(rows)


# ---------------------------------------------------------------------------
# 候选论文：列表、读取、学生决定
# ---------------------------------------------------------------------------


def list_results(
    session: Session, project_ref: str, user_id: str, *, task_ref: str = "",
    decision: str = "", relevance: str = "", limit: int = 50, offset: int = 0,
) -> list[SupervisionLiteratureResultModel]:
    """按关联等级（高→低）列出项目内候选论文。"""
    project, _ = require_member(session, project_ref, user_id)
    stmt = select(SupervisionLiteratureResultModel).where(
        SupervisionLiteratureResultModel.project_id == project.id
    )
    if task_ref:
        task = _get_task(session, project, task_ref)
        stmt = stmt.where(SupervisionLiteratureResultModel.search_task_id == task.id)
    if decision:
        if decision not in DECISION_VALUES:
            raise WorkValidationError("纳入状态只能是 " + "/".join(DECISION_VALUES))
        stmt = stmt.where(SupervisionLiteratureResultModel.decision == decision)
    if relevance:
        if relevance not in RELEVANCE_LEVEL_VALUES:
            raise WorkValidationError("关联等级只能是 " + "/".join(RELEVANCE_LEVEL_VALUES))
        stmt = stmt.where(SupervisionLiteratureResultModel.relevance_level == relevance)
    rank_case = case(
        *[(SupervisionLiteratureResultModel.relevance_level == level, index)
          for index, level in enumerate(_LEVEL_ORDER)],
        else_=len(_LEVEL_ORDER),
    )
    stmt = (
        stmt.order_by(rank_case, SupervisionLiteratureResultModel.code)
        .offset(max(0, offset))
        .limit(max(1, min(limit, 200)))
    )
    return list(session.execute(stmt).scalars().all())


def get_result(
    session: Session, project_ref: str, user_id: str, result_ref: str
) -> SupervisionLiteratureResultModel:
    project, _ = require_member(session, project_ref, user_id)
    return _get_result(session, project, result_ref)


def decide_result(
    session: Session, project_ref: str, user_id: str, result_ref: str, *,
    decision: str, reason: str,
) -> SupervisionLiteratureResultModel:
    """学生对候选论文做纳入 / 排除 / 稍后阅读决定，必须写理由。

    导师看得到决定与理由，但不是决策方（§6.3.B 最后一段、§16.4 第4条）：
    导师调用本接口 → ``PermissionError``（路由层 403）。每次决定都留历史，
    改主意不覆盖旧理由。
    """
    project, role = require_member(session, project_ref, user_id)
    _student_only(role, "对候选论文作出纳入决定")
    if decision not in DECISION_VALUES or decision == "pending":
        raise WorkValidationError(
            "决定只能是 included / excluded / read_later"
        )
    reason = _clean_text(reason, 4000)
    if not reason:
        raise WorkValidationError("必须写明纳入、排除或稍后阅读的理由")
    row = _get_result(session, project, result_ref)
    now = _now()
    previous = {
        "decision": row.decision,
        "reason": row.decision_reason,
        "decided_by": row.decided_by,
        "decided_role": row.decided_role,
        "decided_at": _ts(row.decided_at),
    }
    if previous["decision"] != "pending" or previous["reason"]:
        row.decision_history = list(row.decision_history or []) + [previous]
    row.decision = decision
    row.decision_reason = reason
    row.decided_by = user_id
    row.decided_role = role
    row.decided_at = now
    row.update_time = now
    session.flush()
    label = {"included": "纳入", "excluded": "排除", "read_later": "稍后阅读"}[decision]
    record_audit(
        session, project_id=project.id, object_type="literature_result",
        object_code=row.code, actor_id=user_id, actor_role=role,
        action=f"决定{label}论文", detail=f"{row.title[:200]}；理由：{reason[:1500]}",
    )
    return row


# ---------------------------------------------------------------------------
# 单篇深度提取：学生选篇后才跑，受上限约束，失败不回滚其它论文
# ---------------------------------------------------------------------------


def _extraction_version(session: Session, result_id: str) -> int:
    last = int(
        session.execute(
            select(func.max(SupervisionLiteratureExtractionModel.version)).where(
                SupervisionLiteratureExtractionModel.result_id == result_id
            )
        ).scalar()
        or 0
    )
    return last + 1


def _verify_quote(quote: str, *corpus: str) -> bool:
    """引文是否在本次提供给模型的材料里逐字出现（不核对的不能算已核对证据）。"""
    needle = re.sub(r"\s+", " ", str(quote or "")).strip().lower()
    if len(needle) < 8:
        return False
    for text in corpus:
        haystack = re.sub(r"\s+", " ", str(text or "")).strip().lower()
        if needle in haystack:
            return True
    return False


def _run_deep_extract(
    session: Session, *, task: SupervisionLiteratureSearchTaskModel,
    result: SupervisionLiteratureResultModel, subtask: SupervisionLiteratureSubtaskModel,
    llm: Any, capacity: Any, actor_id: str, role: str,
) -> SupervisionLiteratureResultModel:
    result.extract_state = "running"
    subtask.status = "running"
    subtask.started_at = _now()
    session.flush()

    calls: list[dict] = []
    business_ref = f"{task.code}/{result.code}"
    try:
        raw = _invoke_model(
            llm, build_extract_prompt(task, result), capacity=capacity,
            consumer="paper_extract", business_ref=business_ref, user_id=actor_id,
            sink=calls,
        )
    except (ModelCallError, LiteratureUnavailable) as exc:
        result.extract_state = "failed"
        result.update_time = _now()
        _finish_subtask(
            session, subtask, status="failed", error_code="model_call_failed",
            error_message=str(exc), model_calls=calls, consumer="paper_extract",
            model_name=_model_name(llm), prompt_version=PROMPT_VERSION,
        )
        return result

    payload = parse_json_payload(raw)
    if payload is None:
        result.extract_state = "failed"
        result.update_time = _now()
        _finish_subtask(
            session, subtask, status="failed", error_code="output_not_structured",
            error_message="模型没有返回合法的结构化提取结果，本次深度提取未形成可信内容",
            model_calls=calls, consumer="paper_extract", model_name=_model_name(llm),
            prompt_version=PROMPT_VERSION,
        )
        return result

    # 旧版本只标记被替代，不删行、不改内容（§13「AI 结果版本化」）。
    previous_rows = list(
        session.execute(
            select(SupervisionLiteratureExtractionModel).where(
                SupervisionLiteratureExtractionModel.result_id == result.id,
                SupervisionLiteratureExtractionModel.superseded_at.is_(None),
            )
        ).scalars().all()
    )
    version = _extraction_version(session, result.id)
    evidence: list[dict] = []
    for item in payload.get("evidence") or []:
        if not isinstance(item, dict):
            continue
        quote = _clean_text(item.get("quote"), 4000)
        if not quote:
            continue
        locator = item.get("locator") if isinstance(item.get("locator"), dict) else {}
        evidence.append({
            "evidence_type": _clean_text(item.get("evidence_type"), 32)
            if _clean_text(item.get("evidence_type"), 32) in EVIDENCE_TYPE_VALUES else "",
            "quote": quote,
            "locator": {
                k: locator.get(k)
                for k in ("section", "page_start", "page_end", "line_start",
                          "line_end", "char_start", "char_end")
                if locator.get(k) is not None
            },
            # 只有能在本次输入材料里逐字找到的引文才算「已核对证据」。
            "quote_verified": _verify_quote(quote, result.abstract, result.title),
            "level": "abstract",
        })
    differences = payload.get("differences") if isinstance(payload.get("differences"), dict) else {}
    row = SupervisionLiteratureExtractionModel(
        id=new_id(),
        project_id=task.project_id,
        result_id=result.id,
        result_code=result.code,
        version=version,
        subtask_id=subtask.id,
        subtask_code=subtask.code,
        # 首版输入只有摘要级材料，绝不声称有全文页码定位。
        level="abstract",
        summary=_clean_text(payload.get("summary"), 4000),
        research_problem=_clean_text(payload.get("research_problem"), 4000),
        method=_clean_text(payload.get("method"), 4000),
        algorithm=_clean_text(payload.get("algorithm"), 4000),
        experiments=_clean_text(payload.get("experiments"), 4000),
        results_metrics=_clean_text(payload.get("results_metrics"), 4000),
        limitations=_clean_text(payload.get("limitations"), 4000),
        differences={
            key: _clean_text(differences.get(key), 2000)
            for key in (
                "same", "different", "borrowable", "usable_methods",
                "baseline_methods", "conflicting_assumptions", "gaps",
                "suggested_tasks",
            )
        },
        evidence=evidence[:40],
        model_name=_model_name(llm),
        prompt_version=PROMPT_VERSION,
        created_by=actor_id, created_role=role,
        create_time=_now(),
    )
    session.add(row)
    session.flush()
    for old in previous_rows:
        old.superseded_at = _now()
    result.extract_state = "completed"
    result.update_time = _now()
    _finish_subtask(
        session, subtask, status="completed", result_total=len(evidence),
        model_calls=calls, consumer="paper_extract", model_name=_model_name(llm),
        prompt_version=PROMPT_VERSION,
    )
    record_audit(
        session, project_id=task.project_id, object_type="literature_result",
        object_code=result.code, actor_id=actor_id, actor_role=role,
        action="深度提取论文内容",
        detail=f"{result.code} v{version}：{len(evidence)} 条原文定位证据"
               f"（模型 {row.model_name}，提示词 {PROMPT_VERSION}）",
    )
    return result


def run_deep_extract(
    session: Session, project_ref: str, user_id: str, result_ref: str, *,
    llm: Any, capacity: Any = None,
) -> SupervisionLiteratureResultModel:
    """学生选择深度阅读某一篇后才执行（§16.4 第3、4条）。

    上限来自任务的 ``deep_extract_limit``：一次误操作不能造出无上限的后台
    任务（§6.3.1）。单篇失败只让这一篇 ``failed``，不动其它论文。
    """
    if llm is None or not hasattr(llm, "invoke"):
        raise LiteratureUnavailable(
            "模型能力尚未接入：深度提取需要注入生成模型（经统一模型容量闸门）"
        )
    project, role = require_member(session, project_ref, user_id)
    _student_only(role, "选择深度阅读并提取论文内容")
    result = _get_result(session, project, result_ref)
    task = _get_task(session, project, result.search_task_id)
    if task.status == "closed":
        raise WorkError("该检索任务已关闭")
    used = int(
        session.execute(
            select(func.count(SupervisionLiteratureResultModel.id)).where(
                SupervisionLiteratureResultModel.search_task_id == task.id,
                SupervisionLiteratureResultModel.extract_state.in_(
                    ("queued", "running", "completed")
                ),
            )
        ).scalar()
        or 0
    )
    already = result.extract_state in ("queued", "running", "completed")
    if not already and used >= int(task.deep_extract_limit or 0):
        raise WorkError(
            f"本次检索的深度提取上限是 {task.deep_extract_limit} 篇，已经用了 {used} 篇。"
            "系统不会自动深度提取全部候选论文；需要更多请新建检索任务或调整上限后追加"
        )
    subtask = _new_subtask(
        session, task, kind="deep_extract", actor_id=user_id, role=role,
        payload={"result_code": result.code, "result_id": result.id},
    )
    result.extract_state = "queued"
    result.update_time = _now()
    session.flush()
    return _run_deep_extract(
        session, task=task, result=result, subtask=subtask, llm=llm,
        capacity=capacity, actor_id=user_id, role=role,
    )


def get_extraction(
    session: Session, project_ref: str, user_id: str, result_ref: str
) -> Optional[SupervisionLiteratureExtractionModel]:
    """取最新一版深度提取结果；没提取过返回 ``None``。"""
    project, _ = require_member(session, project_ref, user_id)
    result = _get_result(session, project, result_ref)
    return session.execute(
        select(SupervisionLiteratureExtractionModel)
        .where(SupervisionLiteratureExtractionModel.result_id == result.id)
        .order_by(SupervisionLiteratureExtractionModel.version.desc())
        .limit(1)
    ).scalars().first()


def list_extractions(
    session: Session, project_ref: str, user_id: str, result_ref: str
) -> list[SupervisionLiteratureExtractionModel]:
    project, _ = require_member(session, project_ref, user_id)
    result = _get_result(session, project, result_ref)
    return list(
        session.execute(
            select(SupervisionLiteratureExtractionModel)
            .where(SupervisionLiteratureExtractionModel.result_id == result.id)
            .order_by(SupervisionLiteratureExtractionModel.version.desc())
        ).scalars().all()
    )


# ---------------------------------------------------------------------------
# 证据卡片：可核对原文 + 定位 + 支持关系，重试幂等
# ---------------------------------------------------------------------------


def create_evidence_card(
    session: Session, project_ref: str, user_id: str, *, result_ref: str,
    quote: str, evidence_type: str = "", summary: str = "",
    locator: Optional[dict] = None, support_relation: str = "",
    brief_field: str = "", sub_question: str = "", level: str = "",
    extraction_id: str = "",
) -> SupervisionLiteratureEvidenceCardModel:
    """把一条带原文定位的证据加成证据卡片 ``EC-001``。

    没有原文片段就不能成卡（模型改写的内容不能当已核对证据）；同一结果 +
    同一类型 + 同一定位 + 同一引文只有一张卡片，重复点击不堆重复卡片。
    """
    project, role = require_member(session, project_ref, user_id)
    _student_only(role, "把证据加入证据卡片")
    quote = _clean_text(quote, 4000)
    if not quote:
        raise WorkValidationError(
            "证据卡片必须带可核对原文片段；只有模型改写、没有原文定位的内容不能成卡"
        )
    if evidence_type and evidence_type not in EVIDENCE_TYPE_VALUES:
        raise WorkValidationError("证据类型不合法：" + (evidence_type or ""))
    if support_relation and support_relation not in SUPPORT_RELATION_VALUES:
        raise WorkValidationError("支持关系不合法：" + (support_relation or ""))
    result = _get_result(session, project, result_ref)
    extraction = None
    if extraction_id:
        extraction = session.get(SupervisionLiteratureExtractionModel, extraction_id)
        if extraction is None or extraction.result_id != result.id:
            raise LookupError("深度提取结果不存在")
    if level and level not in ("fulltext", "abstract"):
        raise WorkValidationError("证据层级只能是 fulltext 或 abstract")
    level = level or (extraction.level if extraction else "abstract")
    # 首版只有摘要级材料：给了 fulltext 定位但没有全文材料，按摘要级处理。
    if level == "fulltext" and result.fulltext_status not in ("pdf",):
        level = "abstract"

    locator = locator if isinstance(locator, dict) else {}
    normalized_locator = {
        k: locator.get(k)
        for k in ("section", "page_start", "page_end", "line_start", "line_end",
                  "char_start", "char_end")
        if locator.get(k) is not None
    }
    dedup = hashlib.sha256(
        json.dumps(
            [result.id, evidence_type, normalized_locator, quote],
            ensure_ascii=False, sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    existing = session.execute(
        select(SupervisionLiteratureEvidenceCardModel).where(
            SupervisionLiteratureEvidenceCardModel.project_id == project.id,
            SupervisionLiteratureEvidenceCardModel.dedup_key == dedup,
            SupervisionLiteratureEvidenceCardModel.deleted_at.is_(None),
        )
    ).scalars().first()
    if existing is not None:
        return existing

    now = _now()
    card = SupervisionLiteratureEvidenceCardModel(
        id=new_id(),
        code=next_code(
            session, SupervisionLiteratureEvidenceCardModel,
            SupervisionLiteratureEvidenceCardModel.code, CODE_EVIDENCE_CARD,
        ),
        project_id=project.id,
        result_id=result.id,
        result_code=result.code,
        extraction_id=extraction.id if extraction else None,
        extraction_version=int(extraction.version) if extraction else 0,
        evidence_type=evidence_type or "",
        summary=_clean_text(summary, 2000),
        quote=quote,
        locator=normalized_locator,
        quote_verified=_verify_quote(quote, result.abstract, result.title),
        level=level,
        support_relation=support_relation or "",
        brief_field=_clean_text(brief_field, 32),
        sub_question=_clean_text(sub_question, 500),
        dedup_key=dedup,
        created_by=user_id, created_role=role,
        create_time=now, updated_at=now,
    )
    session.add(card)
    session.flush()
    record_audit(
        session, project_id=project.id, object_type="literature_evidence_card",
        object_code=card.code, actor_id=user_id, actor_role=role,
        action="添加论文证据卡片",
        detail=f"{card.code} ← {result.code}；已核对原文"
               if card.quote_verified else f"{card.code} ← {result.code}；引文未在本次材料中核对到",
    )
    return card


def list_evidence_cards(
    session: Session, project_ref: str, user_id: str, *, result_ref: str = ""
) -> list[SupervisionLiteratureEvidenceCardModel]:
    project, _ = require_member(session, project_ref, user_id)
    stmt = select(SupervisionLiteratureEvidenceCardModel).where(
        SupervisionLiteratureEvidenceCardModel.project_id == project.id,
        SupervisionLiteratureEvidenceCardModel.deleted_at.is_(None),
    )
    if result_ref:
        result = _get_result(session, project, result_ref)
        stmt = stmt.where(
            SupervisionLiteratureEvidenceCardModel.result_id == result.id
        )
    return list(
        session.execute(
            stmt.order_by(SupervisionLiteratureEvidenceCardModel.code)
        ).scalars().all()
    )


# ---------------------------------------------------------------------------
# 摘要级关联分析与多篇比较（有上限，消费者 paper_compare）
# ---------------------------------------------------------------------------


def _analysis_limit(task: SupervisionLiteratureSearchTaskModel, requested: Any) -> int:
    cap = _int_env("CHATCHAT_LITERATURE_MAX_ABSTRACT_ANALYSIS", DEFAULT_ABSTRACT_ANALYSIS_LIMIT)
    try:
        wanted = int(requested) if requested not in (None, "") else cap
    except (TypeError, ValueError):
        raise WorkValidationError("摘要级分析篇数必须是整数")
    if wanted < 1:
        raise WorkValidationError("摘要级分析篇数至少 1 篇")
    return max(1, min(wanted, cap, int(task.result_limit or cap)))


def _pick_for_analysis(
    session: Session, task: SupervisionLiteratureSearchTaskModel,
    result_codes: list[str], limit: int,
) -> list[SupervisionLiteratureResultModel]:
    stmt = select(SupervisionLiteratureResultModel).where(
        SupervisionLiteratureResultModel.search_task_id == task.id,
        SupervisionLiteratureResultModel.decision != "excluded",
    )
    if result_codes:
        stmt = stmt.where(
            SupervisionLiteratureResultModel.code.in_(result_codes[:limit])
        )
    rank_case = case(
        *[(SupervisionLiteratureResultModel.relevance_level == level, index)
          for index, level in enumerate(_LEVEL_ORDER)],
        else_=len(_LEVEL_ORDER),
    )
    rows = list(
        session.execute(
            stmt.order_by(rank_case, SupervisionLiteratureResultModel.code).limit(limit)
        ).scalars().all()
    )
    return rows


def _run_abstract_analysis(
    session: Session, *, task: SupervisionLiteratureSearchTaskModel,
    subtask: SupervisionLiteratureSubtaskModel, llm: Any, capacity: Any,
    actor_id: str, role: str, result_codes: list[str],
) -> SupervisionLiteratureSubtaskModel:
    # 重试用同一批论文编号：payload 里存的就是本次要分析的论文，不重新挑。
    codes = result_codes or [
        str(c) for c in ((subtask.payload or {}).get("result_codes") or []) if str(c)
    ]
    rows = _pick_for_analysis(session, task, codes, max(1, len(codes)))
    subtask.status = "running"
    subtask.started_at = _now()
    session.flush()
    calls: list[dict] = []
    try:
        raw = _invoke_model(
            llm, build_analysis_prompt(task, rows), capacity=capacity,
            consumer="paper_compare", business_ref=task.code, user_id=actor_id,
            sink=calls,
        )
    except (ModelCallError, LiteratureUnavailable) as exc:
        _finish_subtask(
            session, subtask, status="failed", error_code="model_call_failed",
            error_message=str(exc), model_calls=calls, consumer="paper_compare",
            model_name=_model_name(llm), prompt_version=PROMPT_VERSION,
        )
        return subtask
    payload = parse_json_payload(raw)
    if payload is None:
        _finish_subtask(
            session, subtask, status="failed", error_code="output_not_structured",
            error_message="模型没有返回合法的结构化关联分析结果",
            model_calls=calls, consumer="paper_compare", model_name=_model_name(llm),
            prompt_version=PROMPT_VERSION,
        )
        return subtask

    by_code = {row.code: row for row in rows}
    analysed = 0
    for item in payload.get("papers") or []:
        if not isinstance(item, dict):
            continue
        row = by_code.get(_clean_text(item.get("code"), 16))
        if row is None:
            continue
        level = _clean_text(item.get("level"), 16)
        quotes = [
            {"quote": _clean_text(q.get("quote"), 1000),
             "locator": _clean_text(q.get("locator"), 300)}
            for q in (item.get("evidence_quotes") or []) if isinstance(q, dict)
        ]
        # 语义分析单独存，不覆盖规则排序字段（§6.3.B：不得把词面重合包装成
        # 语义评分，也不得让语义结果悄悄改掉规则等级）。
        row.semantic_analysis = {
            "level": level if level in RELEVANCE_LEVEL_VALUES else row.relevance_level,
            "reason": _clean_text(item.get("reason"), 2000),
            "solves": _clean_text(item.get("solves"), 2000),
            "same": _clean_text(item.get("same"), 2000),
            "different": _clean_text(item.get("different"), 2000),
            "borrowable": _clean_text(item.get("borrowable"), 2000),
            "impact": _clean_text(item.get("impact"), 2000),
            "evidence_quotes": quotes[:10],
            "label": "AI语义分析（仅摘要级材料）",
            "model": _model_name(llm),
            "prompt_version": PROMPT_VERSION,
            "subtask_code": subtask.code,
            "at": _ts(_now()),
        }
        row.update_time = _now()
        analysed += 1
    _finish_subtask(
        session, subtask, status="completed", result_total=len(rows), new_total=analysed,
        model_calls=calls, consumer="paper_compare", model_name=_model_name(llm),
        prompt_version=PROMPT_VERSION,
    )
    record_audit(
        session, project_id=task.project_id, object_type="literature_task",
        object_code=task.code, actor_id=actor_id, actor_role=role,
        action="摘要级关联分析",
        detail=f"{subtask.code}：分析 {len(rows)} 篇，写入 {analysed} 篇"
               f"（模型 {_model_name(llm)}，提示词 {PROMPT_VERSION}）",
    )
    return subtask


def run_abstract_analysis(
    session: Session, project_ref: str, user_id: str, task_ref: str, *,
    llm: Any, capacity: Any = None, result_refs: Optional[Iterable[str]] = None,
    limit: Any = None,
) -> SupervisionLiteratureSubtaskModel:
    """有上限的摘要级关联分析（§6.3.1 第3步）：只对排序靠前的有限候选做。"""
    if llm is None or not hasattr(llm, "invoke"):
        raise LiteratureUnavailable(
            "模型能力尚未接入：摘要级关联分析需要注入生成模型（经统一模型容量闸门）"
        )
    project, role = require_member(session, project_ref, user_id)
    task = _get_task(session, project, task_ref)
    if task.status == "closed":
        raise WorkError("该检索任务已关闭")
    cap = _analysis_limit(task, limit)
    codes = [_clean_text(c, 16) for c in (result_refs or []) if _clean_text(c, 16)]
    rows = _pick_for_analysis(session, task, codes, cap)
    if not rows:
        raise WorkError(
            "没有可分析的候选论文：先执行检索，或指定要分析的论文编号"
        )
    subtask = _new_subtask(
        session, task, kind="abstract_analysis", actor_id=user_id, role=role,
        payload={
            "result_codes": [row.code for row in rows],
            "limit": len(rows),
            "requested_limit": cap,
        },
    )
    return _run_abstract_analysis(
        session, task=task, subtask=subtask, llm=llm, capacity=capacity,
        actor_id=user_id, role=role, result_codes=[row.code for row in rows],
    )


def _run_compare(
    session: Session, *, task: SupervisionLiteratureSearchTaskModel,
    subtask: SupervisionLiteratureSubtaskModel, llm: Any, capacity: Any,
    actor_id: str, role: str, result_codes: list[str],
) -> SupervisionLiteratureSubtaskModel:
    rows = list(
        session.execute(
            select(SupervisionLiteratureResultModel).where(
                SupervisionLiteratureResultModel.project_id == task.project_id,
                SupervisionLiteratureResultModel.code.in_(result_codes),
            )
        ).scalars().all()
    )
    subtask.status = "running"
    subtask.started_at = _now()
    session.flush()
    calls: list[dict] = []
    try:
        raw = _invoke_model(
            llm, build_compare_prompt(task, rows), capacity=capacity,
            consumer="paper_compare", business_ref=task.code, user_id=actor_id,
            sink=calls,
        )
    except (ModelCallError, LiteratureUnavailable) as exc:
        _finish_subtask(
            session, subtask, status="failed", error_code="model_call_failed",
            error_message=str(exc), model_calls=calls, consumer="paper_compare",
            model_name=_model_name(llm), prompt_version=PROMPT_VERSION,
        )
        return subtask
    payload = parse_json_payload(raw)
    if payload is None:
        _finish_subtask(
            session, subtask, status="failed", error_code="output_not_structured",
            error_message="模型没有返回合法的结构化比较结果",
            model_calls=calls, consumer="paper_compare", model_name=_model_name(llm),
            prompt_version=PROMPT_VERSION,
        )
        return subtask
    comparison = {
        "dimensions": [
            {"name": _clean_text(d.get("name"), 200),
             "values": {str(k): _clean_text(v, 1000) for k, v in (d.get("values") or {}).items()}}
            for d in (payload.get("dimensions") or []) if isinstance(d, dict)
        ][:20],
        "agreements": [_clean_text(x, 1000) for x in (payload.get("agreements") or [])][:20],
        "conflicts": [_clean_text(x, 1000) for x in (payload.get("conflicts") or [])][:20],
        "gaps": [_clean_text(x, 1000) for x in (payload.get("gaps") or [])][:20],
        "note": _clean_text(payload.get("note"), 2000),
        "label": "AI多篇比较",
        "model": _model_name(llm),
        "prompt_version": PROMPT_VERSION,
        "at": _ts(_now()),
    }
    subtask.payload = {
        **(subtask.payload or {}),
        "result_codes": [row.code for row in rows],
        "comparison": comparison,
    }
    _finish_subtask(
        session, subtask, status="completed", result_total=len(rows),
        model_calls=calls, consumer="paper_compare", model_name=_model_name(llm),
        prompt_version=PROMPT_VERSION,
    )
    record_audit(
        session, project_id=task.project_id, object_type="literature_task",
        object_code=task.code, actor_id=actor_id, actor_role=role,
        action="多篇论文比较",
        detail=f"{subtask.code}：比较 {len(rows)} 篇（模型 {_model_name(llm)}）",
    )
    return subtask


def compare_papers(
    session: Session, project_ref: str, user_id: str, task_ref: str, *,
    result_refs: Iterable[str], llm: Any, capacity: Any = None,
) -> SupervisionLiteratureSubtaskModel:
    """多篇比较：一次最多 ``MAX_COMPARE_PAPERS`` 篇，避免无上限后台任务。"""
    if llm is None or not hasattr(llm, "invoke"):
        raise LiteratureUnavailable(
            "模型能力尚未接入：多篇比较需要注入生成模型（经统一模型容量闸门）"
        )
    project, role = require_member(session, project_ref, user_id)
    task = _get_task(session, project, task_ref)
    if task.status == "closed":
        raise WorkError("该检索任务已关闭")
    codes = list(dict.fromkeys(_clean_text(c, 16) for c in (result_refs or []) if _clean_text(c, 16)))
    if len(codes) < 2:
        raise WorkValidationError("多篇比较至少要指定两篇论文编号")
    if len(codes) > MAX_COMPARE_PAPERS:
        raise WorkValidationError(f"一次最多比较 {MAX_COMPARE_PAPERS} 篇")
    found = list(
        session.execute(
            select(SupervisionLiteratureResultModel).where(
                SupervisionLiteratureResultModel.project_id == project.id,
                SupervisionLiteratureResultModel.code.in_(codes),
            )
        ).scalars().all()
    )
    if len(found) != len(codes):
        missing = sorted(set(codes) - {row.code for row in found})
        raise LookupError("这些论文不在本项目里：" + "、".join(missing))
    subtask = _new_subtask(
        session, task, kind="compare", actor_id=user_id, role=role,
        payload={"result_codes": codes},
    )
    return _run_compare(
        session, task=task, subtask=subtask, llm=llm, capacity=capacity,
        actor_id=user_id, role=role, result_codes=codes,
    )


# ---------------------------------------------------------------------------
# 文献研究结论：三类内容分开，绑定文献快照，旧版本只标历史
# ---------------------------------------------------------------------------


def _validate_conclusion_content(
    rows: list[SupervisionLiteratureResultModel], *, statement: str,
    supported_facts: Iterable[dict], student_judgements: Iterable[dict],
    unverified_hypotheses: Iterable[dict], research_gaps: Iterable[Any],
) -> tuple[list[dict], list[dict], list[dict], list[str]]:
    """校验并规范化三类内容（§4.5：论文事实 / 学生判断 / 待验证假设）。

    「论文原文明确支持的事实」必须指向当前已纳入的论文，否则就是无依据的
    断言；字段非法一律 400。
    """
    statement = _clean_text(statement, 4000)
    if not statement:
        raise WorkValidationError("必须写明文献结论的表述")
    codes = {row.code for row in rows}
    facts: list[dict] = []
    for item in supported_facts or []:
        if not isinstance(item, dict):
            raise WorkValidationError("论文事实必须是对象")
        text = _clean_text(item.get("statement"), 2000)
        if not text:
            raise WorkValidationError("论文事实必须写明内容")
        code = _clean_text(item.get("result_code"), 16)
        if code not in codes:
            raise WorkValidationError(
                f"论文事实必须引用当前已纳入的论文编号（P-xxx），{code or '（空）'} 不在纳入集合里"
            )
        facts.append({
            "statement": text,
            "result_code": code,
            "evidence_card_code": _clean_text(item.get("evidence_card_code"), 16),
            "locator": item.get("locator") if isinstance(item.get("locator"), dict) else {},
        })
    judgements: list[dict] = []
    for item in student_judgements or []:
        if not isinstance(item, dict):
            raise WorkValidationError("学生判断必须是对象")
        text = _clean_text(item.get("statement"), 2000)
        if not text:
            raise WorkValidationError("学生判断必须写明内容")
        basis = [_clean_text(c, 16) for c in (item.get("basis_result_codes") or [])]
        outside = sorted(set(basis) - codes)
        if outside:
            raise WorkValidationError(
                "学生判断依据的论文不在纳入集合里：" + "、".join(outside)
            )
        judgements.append({"statement": text, "basis_result_codes": basis})
    hypotheses: list[dict] = []
    for item in unverified_hypotheses or []:
        if not isinstance(item, dict):
            raise WorkValidationError("待验证假设必须是对象")
        text = _clean_text(item.get("statement"), 2000)
        if not text:
            raise WorkValidationError("待验证假设必须写明内容")
        hypotheses.append({
            "statement": text,
            "how_to_verify": _clean_text(item.get("how_to_verify"), 2000),
        })
    gaps = [_clean_text(g, 2000) for g in (research_gaps or []) if _clean_text(g, 2000)][:30]
    return facts, judgements, hypotheses, gaps


def _card_snapshot(session: Session, project_id: str, card_codes: Iterable[str]) -> list[dict]:
    codes = [_clean_text(c, 16) for c in (card_codes or []) if _clean_text(c, 16)]
    if not codes:
        return []
    rows = list(
        session.execute(
            select(SupervisionLiteratureEvidenceCardModel).where(
                SupervisionLiteratureEvidenceCardModel.project_id == project_id,
                SupervisionLiteratureEvidenceCardModel.code.in_(codes),
                SupervisionLiteratureEvidenceCardModel.deleted_at.is_(None),
            )
        ).scalars().all()
    )
    missing = sorted(set(codes) - {row.code for row in rows})
    if missing:
        raise LookupError("这些证据卡片不在本项目里：" + "、".join(missing))
    return [
        {"code": row.code, "result_code": row.result_code,
         "evidence_type": row.evidence_type, "quote": row.quote[:1000],
         "quote_verified": bool(row.quote_verified)}
        for row in rows
    ]


def create_conclusion(
    session: Session, project_ref: str, user_id: str, *, statement: str,
    supported_facts: Optional[Iterable[dict]] = None,
    student_judgements: Optional[Iterable[dict]] = None,
    unverified_hypotheses: Optional[Iterable[dict]] = None,
    research_gaps: Optional[Iterable[str]] = None,
    evidence_card_codes: Optional[Iterable[str]] = None,
    task_ref: str = "", confirm: bool = False,
) -> SupervisionLiteratureConclusionModel:
    """从**当前已纳入集合**形成文献研究结论 ``LC-001``（§4.5、场景 G 第8步）。

    结论绑定它依据的文献快照与快照哈希；之后纳入集合变化不改写这条结论，
    由 ``literature_changed`` 现算出「依据已变化」，重新形成是新版本。
    """
    project, role = require_member(session, project_ref, user_id)
    _student_only(role, "形成文献研究结论")
    task = _get_task(session, project, task_ref) if task_ref else None
    rows = literature_snapshot_rows(
        session, project_id=project.id,
        search_task_id=task.id if task else "",
    )
    if not rows:
        raise WorkError(
            "当前没有已纳入的论文，不能形成文献结论：请先对候选论文作出纳入决定并写明理由"
        )
    facts, judgements, hypotheses, gaps = _validate_conclusion_content(
        rows, statement=statement, supported_facts=supported_facts or [],
        student_judgements=student_judgements or [],
        unverified_hypotheses=unverified_hypotheses or [], research_gaps=research_gaps or [],
    )
    if not judgements:
        raise WorkValidationError(
            "文献结论必须包含学生基于多篇论文形成的综合判断；"
            "只罗列论文原文事实不构成研究结论"
        )
    cards = _card_snapshot(session, project.id, evidence_card_codes or [])
    snapshot = literature_snapshot_of(rows)
    now = _now()
    for _ in range(5):
        conclusion = SupervisionLiteratureConclusionModel(
            id=new_id(),
            code=next_code(
                session, SupervisionLiteratureConclusionModel,
                SupervisionLiteratureConclusionModel.code, CODE_CONCLUSION,
            ),
            version=1,
            project_id=project.id,
            search_task_id=task.id if task else None,
            search_task_code=task.code if task else "",
            statement=_clean_text(statement, 4000),
            supported_facts=facts, student_judgements=judgements,
            unverified_hypotheses=hypotheses, research_gaps=gaps,
            literature_snapshot=snapshot,
            literature_snapshot_hash=snapshot_hash(snapshot),
            literature_count=len(rows),
            evidence_cards=cards,
            status="confirmed" if confirm else "draft",
            confirmed_by=user_id if confirm else "",
            confirmed_at=now if confirm else None,
            created_by=user_id, created_role=role,
            create_time=now, update_time=now,
        )
        session.add(conclusion)
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            continue
        if task is not None:
            task.status = "concluded"
            task.concluded_at = now
            task.update_time = now
        record_audit(
            session, project_id=project.id, object_type="literature_conclusion",
            object_code=conclusion.code, actor_id=user_id, actor_role=role,
            action="形成文献研究结论",
            detail=f"{conclusion.code} v{conclusion.version}：依据 {len(rows)} 篇已纳入论文"
                   f"（论文事实 {len(facts)} 条、学生判断 {len(judgements)} 条、"
                   f"待验证假设 {len(hypotheses)} 条）",
        )
        return conclusion
    raise RuntimeError("文献结论编号生成失败，请重试")


def _latest_conclusion_version(
    session: Session, project_id: str, code: str
) -> SupervisionLiteratureConclusionModel:
    row = session.execute(
        select(SupervisionLiteratureConclusionModel)
        .where(
            SupervisionLiteratureConclusionModel.project_id == project_id,
            SupervisionLiteratureConclusionModel.code == code,
            SupervisionLiteratureConclusionModel.deleted_at.is_(None),
        )
        .order_by(SupervisionLiteratureConclusionModel.version.desc())
        .limit(1)
    ).scalars().first()
    if row is None:
        raise LookupError("文献结论不存在")
    return row


def revise_conclusion(
    session: Session, project_ref: str, user_id: str, conclusion_ref: str, *,
    statement: str,
    supported_facts: Optional[Iterable[dict]] = None,
    student_judgements: Optional[Iterable[dict]] = None,
    unverified_hypotheses: Optional[Iterable[dict]] = None,
    research_gaps: Optional[Iterable[str]] = None,
    evidence_card_codes: Optional[Iterable[str]] = None,
    confirm: bool = False,
) -> SupervisionLiteratureConclusionModel:
    """修订文献结论：同一 ``code`` 的新版本，旧版本标 ``superseded`` 保留。"""
    project, role = require_member(session, project_ref, user_id)
    _student_only(role, "修订文献研究结论")
    old = _get_conclusion(session, project, conclusion_ref)
    if old.status == "superseded":
        raise WorkError(
            f"{old.code} v{old.version} 已被新版本替代，不能在此基础上修订；"
            "请基于当前最新版本修订"
        )
    latest = _latest_conclusion_version(session, project.id, old.code)
    rows = literature_snapshot_rows(
        session, project_id=project.id, search_task_id=latest.search_task_id or ""
    )
    if not rows:
        raise WorkError(
            "当前没有已纳入的论文，不能形成新版本：纳入集合已清空，"
            "旧结论作为历史保留，不会被改写"
        )
    facts, judgements, hypotheses, gaps = _validate_conclusion_content(
        rows, statement=statement, supported_facts=supported_facts or [],
        student_judgements=student_judgements or [],
        unverified_hypotheses=unverified_hypotheses or [], research_gaps=research_gaps or [],
    )
    if not judgements:
        raise WorkValidationError("文献结论必须包含学生综合判断")
    cards = _card_snapshot(session, project.id, evidence_card_codes or [])
    snapshot = literature_snapshot_of(rows)
    now = _now()
    conclusion = SupervisionLiteratureConclusionModel(
        id=new_id(),
        code=latest.code,
        version=int(latest.version) + 1,
        project_id=project.id,
        search_task_id=latest.search_task_id,
        search_task_code=latest.search_task_code,
        statement=_clean_text(statement, 4000),
        supported_facts=facts, student_judgements=judgements,
        unverified_hypotheses=hypotheses, research_gaps=gaps,
        literature_snapshot=snapshot,
        literature_snapshot_hash=snapshot_hash(snapshot),
        literature_count=len(rows),
        evidence_cards=cards,
        status="confirmed" if confirm else "draft",
        confirmed_by=user_id if confirm else "",
        confirmed_at=now if confirm else None,
        created_by=user_id, created_role=role,
        create_time=now, update_time=now,
    )
    session.add(conclusion)
    session.flush()
    # 旧版本只标记被替代：内容、导师意见与引用快照都不动（§13）。
    older = list(
        session.execute(
            select(SupervisionLiteratureConclusionModel).where(
                SupervisionLiteratureConclusionModel.project_id == project.id,
                SupervisionLiteratureConclusionModel.code == latest.code,
                SupervisionLiteratureConclusionModel.id != conclusion.id,
                SupervisionLiteratureConclusionModel.status != "superseded",
            )
        ).scalars().all()
    )
    for row in older:
        row.status = "superseded"
        row.superseded_at = now
        row.superseded_by_code = conclusion.code
        row.superseded_by_version = conclusion.version
        row.update_time = now
    record_audit(
        session, project_id=project.id, object_type="literature_conclusion",
        object_code=conclusion.code, actor_id=user_id, actor_role=role,
        action="修订文献研究结论",
        detail=f"{conclusion.code} v{conclusion.version}（替代 v{latest.version}，"
               f"依据 {len(rows)} 篇已纳入论文）",
    )
    return conclusion


def list_conclusions(
    session: Session, project_ref: str, user_id: str, *,
    status: str = "", include_superseded: bool = False,
) -> list[SupervisionLiteratureConclusionModel]:
    project, _ = require_member(session, project_ref, user_id)
    stmt = (
        select(SupervisionLiteratureConclusionModel)
        .where(
            SupervisionLiteratureConclusionModel.project_id == project.id,
            SupervisionLiteratureConclusionModel.deleted_at.is_(None),
        )
        .order_by(
            SupervisionLiteratureConclusionModel.code.asc(),
            SupervisionLiteratureConclusionModel.version.desc(),
        )
    )
    if status:
        if status not in CONCLUSION_STATUS_VALUES:
            raise WorkValidationError("结论状态只能是 " + "/".join(CONCLUSION_STATUS_VALUES))
        stmt = stmt.where(SupervisionLiteratureConclusionModel.status == status)
    elif not include_superseded:
        # 默认只列当前生效的版本；历史版本仍可显式取回（§13 不删历史）。
        stmt = stmt.where(SupervisionLiteratureConclusionModel.status != "superseded")
    return list(session.execute(stmt).scalars().all())


def get_conclusion(
    session: Session, project_ref: str, user_id: str, conclusion_ref: str
) -> SupervisionLiteratureConclusionModel:
    project, _ = require_member(session, project_ref, user_id)
    return _get_conclusion(session, project, conclusion_ref)


def mentor_review_conclusion(
    session: Session, project_ref: str, user_id: str, conclusion_ref: str, *,
    state: str, note: str = "",
) -> SupervisionLiteratureConclusionModel:
    """导师对文献结论确认 / 质疑 / 要求补充证据（§4.5）。

    这是导师对**文献结论本身**的态度，不写进里程碑1 的逐条验收结论字段，
    也不替导师作任何正式审核判断（§8.1 人机边界）。
    """
    project, role = require_member(session, project_ref, user_id)
    _mentor_only(role, "复核文献研究结论")
    if state not in ("confirmed", "questioned", "evidence_required"):
        raise WorkValidationError("复核结论只能是 confirmed / questioned / evidence_required")
    note = _clean_text(note, 4000)
    if state in ("questioned", "evidence_required") and not note:
        raise WorkValidationError("质疑或要求补充证据时必须写明理由")
    conclusion = _get_conclusion(session, project, conclusion_ref)
    if conclusion.status == "superseded":
        raise WorkError(f"{conclusion.code} v{conclusion.version} 已被新版本替代，请复核最新版本")
    if conclusion.status != "confirmed":
        raise WorkError(
            "只有学生已确认的文献结论能接受导师复核；草稿状态的结论还不是项目正式依据"
        )
    conclusion.mentor_state = state
    conclusion.mentor_note = note
    conclusion.mentor_by = user_id
    conclusion.mentor_role = role
    conclusion.mentor_at = _now()
    conclusion.update_time = _now()
    session.flush()
    label = {"confirmed": "确认", "questioned": "质疑", "evidence_required": "要求补充证据"}[state]
    record_audit(
        session, project_id=project.id, object_type="literature_conclusion",
        object_code=conclusion.code, actor_id=user_id, actor_role=role,
        action=f"导师{label}文献结论",
        detail=f"{conclusion.code} v{conclusion.version}：{note[:1500] or '未附说明'}",
    )
    return conclusion


# ---------------------------------------------------------------------------
# 引用快照：按具体成果授权，固定当时字段（§9.1、§16.4 第6条）
# ---------------------------------------------------------------------------


def _project_student_id(session: Session, project_id: str, fallback: str) -> str:
    ids = active_member_ids(session, project_id=project_id, role=ROLE_STUDENT)
    return ids[0] if ids else fallback


def _citation_snapshot(
    session: Session, project: SupervisionProjectModel, user_id: str, role: str,
    kind: str, source_ref: str,
) -> tuple[dict, str, str, str]:
    """返回 ``(快照, 来源可读编号, 来源所有者, 来源版本标记)``。

    私人材料必须证明「这个成果就是这个用户的」：只按 ``owner_id`` 过滤，
    别人的私人论文或证据卡片一律 404，不因加入协作项目就放开私人数据。
    """
    if kind == "project_result":
        row = _get_result(session, project, source_ref)
        snapshot = {
            "kind": kind, "title": row.title, "authors": row.authors, "year": row.year,
            "abstract": row.abstract, "url": row.url, "doi": row.doi,
            "arxiv_id": row.arxiv_id, "source": row.source, "sources": row.sources,
            "fulltext_status": row.fulltext_status,
            "relevance_level": row.relevance_level,
            "relevance_reason": row.relevance_reason,
            "relevance_matched": row.relevance_matched,
            "decision": row.decision, "decision_reason": row.decision_reason,
            "extract_state": row.extract_state,
        }
        return (
            snapshot, row.code,
            _project_student_id(session, project.id, user_id), _ts(row.update_time),
        )
    if kind == "conclusion":
        row = _get_conclusion(session, project, source_ref)
        if row.status != "confirmed":
            raise WorkError("只有已确认的文献结论能作为引用依据")
        snapshot = {
            "kind": kind, "version": row.version, "statement": row.statement,
            "supported_facts": row.supported_facts,
            "student_judgements": row.student_judgements,
            "unverified_hypotheses": row.unverified_hypotheses,
            "research_gaps": row.research_gaps,
            "literature_snapshot": row.literature_snapshot,
            "literature_snapshot_hash": row.literature_snapshot_hash,
            "evidence_cards": row.evidence_cards,
        }
        return snapshot, row.code, row.created_by, f"v{row.version}"
    if kind == "private_paper":
        _student_only(role, "把私人论文研究成果授权进协作项目")  # 见下：按所有者再校验
        link = session.execute(
            select(ProjectPaperModel).where(
                ProjectPaperModel.id == source_ref,
                ProjectPaperModel.owner_id == user_id,
            )
        ).scalar_one_or_none()
        if link is None:
            raise LookupError("该论文成果不属于当前用户")
        record = session.get(PaperRecordModel, link.paper_id)
        if record is None:
            raise LookupError("该论文成果不属于当前用户")
        snapshot = {
            "kind": kind, "title": record.title, "authors": record.authors,
            "year": record.year, "abstract": record.abstract, "url": record.url,
            "doi": record.doi, "arxiv_id": record.arxiv_id, "source": record.source,
            "fulltext_status": record.fulltext_status,
            "screening_status": link.screening_status,
            "screening_reason": link.screening_reason,
            "fulltext_state": link.fulltext_state,
        }
        return snapshot, record.id, user_id, _ts(link.update_time)
    if kind == "private_evidence_card":
        _student_only(role, "把私人论文研究成果授权进协作项目")
        card = session.execute(
            select(EvidenceCardModel).where(
                EvidenceCardModel.id == source_ref,
                EvidenceCardModel.owner_id == user_id,
            )
        ).scalar_one_or_none()
        if card is None:
            raise LookupError("该证据卡片不属于当前用户")
        snapshot = {
            "kind": kind, "quote": card.quote, "summary": card.summary,
            "evidence_type": card.evidence_type, "level": card.level,
            "quote_verified": bool(card.quote_verified),
            "review_status": card.review_status, "paper_id": card.paper_id,
            "locator": json.loads(card.locator_json) if card.locator_json else {},
        }
        return snapshot, card.id, user_id, f"v{card.version}"
    raise WorkValidationError("引用来源种类不合法：" + (kind or ""))


def create_citation(
    session: Session, project_ref: str, user_id: str, *, source_kind: str,
    source_ref: str, purpose: str, bound_object_type: str = "",
    bound_object_code: str = "", note: str = "",
) -> SupervisionLiteratureCitationModel:
    """按**具体成果**授权引用快照 ``CS-001``（§9.1）。

    快照复制或固定授权当时可见的字段、内容版本与内容哈希；来源之后变化不会
    回改这份历史，导师只读得到快照本身，不能顺着对象关系浏览学生其它私人
    搜索、笔记或报告。快照不默认跨项目复用：每个项目都要重新授权。
    """
    project, role = require_member(session, project_ref, user_id)
    if source_kind not in CITATION_SOURCE_KIND_VALUES:
        raise WorkValidationError("引用来源种类只能是 " + "/".join(CITATION_SOURCE_KIND_VALUES))
    if purpose not in CITATION_PURPOSE_VALUES:
        raise WorkValidationError("引用用途只能是 " + "/".join(CITATION_PURPOSE_VALUES))
    if not _clean_text(source_ref, 32):
        raise WorkValidationError("必须指明要引用的具体成果编号")
    snapshot, source_code, owner_id, source_version = _citation_snapshot(
        session, project, user_id, role, source_kind, source_ref
    )
    now = _now()
    citation = SupervisionLiteratureCitationModel(
        id=new_id(),
        code=next_code(
            session, SupervisionLiteratureCitationModel,
            SupervisionLiteratureCitationModel.code, CODE_CITATION,
        ),
        project_id=project.id,
        source_kind=source_kind,
        source_ref=source_ref,
        source_code=source_code,
        source_owner_id=owner_id,
        purpose=purpose,
        bound_object_type=_clean_text(bound_object_type, 32),
        bound_object_code=_clean_text(bound_object_code, 32),
        snapshot=snapshot,
        source_version=(source_version or "")[:64],
        content_hash=content_hash(snapshot),
        granted_by=user_id, granted_role=role, granted_at=now,
        note=_clean_text(note, 2000),
        create_time=now,
    )
    session.add(citation)
    session.flush()
    record_audit(
        session, project_id=project.id, object_type="literature_citation",
        object_code=citation.code, actor_id=user_id, actor_role=role,
        action="授权引用快照",
        detail=f"{citation.code} ← {source_kind} {source_code}；用途 {purpose}；"
               f"内容哈希 {citation.content_hash[:16]}",
    )
    return citation


def get_citation(
    session: Session, project_ref: str, user_id: str, citation_ref: str
) -> SupervisionLiteratureCitationModel:
    project, _ = require_member(session, project_ref, user_id)
    row = session.execute(
        select(SupervisionLiteratureCitationModel).where(
            SupervisionLiteratureCitationModel.project_id == project.id,
            (SupervisionLiteratureCitationModel.id == citation_ref)
            | (SupervisionLiteratureCitationModel.code == citation_ref),
        )
    ).scalar_one_or_none()
    if row is None:
        raise LookupError("引用快照不存在")
    return row


def list_citations(
    session: Session, project_ref: str, user_id: str, *, purpose: str = "",
    source_kind: str = "", include_revoked: bool = True,
) -> list[SupervisionLiteratureCitationModel]:
    project, _ = require_member(session, project_ref, user_id)
    stmt = select(SupervisionLiteratureCitationModel).where(
        SupervisionLiteratureCitationModel.project_id == project.id
    )
    if purpose:
        if purpose not in CITATION_PURPOSE_VALUES:
            raise WorkValidationError("引用用途只能是 " + "/".join(CITATION_PURPOSE_VALUES))
        stmt = stmt.where(SupervisionLiteratureCitationModel.purpose == purpose)
    if source_kind:
        stmt = stmt.where(SupervisionLiteratureCitationModel.source_kind == source_kind)
    if not include_revoked:
        stmt = stmt.where(SupervisionLiteratureCitationModel.revoked_at.is_(None))
    return list(
        session.execute(
            stmt.order_by(SupervisionLiteratureCitationModel.code)
        ).scalars().all()
    )


def revoke_citation(
    session: Session, project_ref: str, user_id: str, citation_ref: str, *, reason: str
) -> SupervisionLiteratureCitationModel:
    """撤销未来的引用权限；历史快照不消失（§9.1、§13）。"""
    project, role = require_member(session, project_ref, user_id)
    citation = get_citation(session, project.id, user_id, citation_ref)
    if citation.granted_by != user_id:
        raise PermissionError("需要授权人本人角色：只有当初授权的人能撤销该引用快照")
    if citation.revoked_at is not None:
        raise WorkError("该引用快照已撤销")
    reason = _clean_text(reason, 2000)
    if not reason:
        raise WorkValidationError("撤销引用授权必须写明原因")
    citation.revoked_at = _now()
    citation.revoked_by = user_id
    citation.revoke_reason = reason
    session.flush()
    record_audit(
        session, project_id=project.id, object_type="literature_citation",
        object_code=citation.code, actor_id=user_id, actor_role=role,
        action="撤销引用快照授权",
        detail=f"{citation.code}：{reason[:1500]}（历史快照与已形成的审核记录继续保留）",
    )
    return citation


# ---------------------------------------------------------------------------
# 序列化：响应里的派生字段都在这一节现算，不改任何业务状态（§5.6、§5.7）
# ---------------------------------------------------------------------------


def subtask_payload(row: SupervisionLiteratureSubtaskModel) -> dict:
    return {
        "subtask_id": row.id, "code": row.code, "kind": row.kind, "status": row.status,
        "attempt": int(row.attempt or 1), "source": row.source, "query": row.query,
        "payload": dict(row.payload or {}),
        "result_total": int(row.result_total or 0),
        "new_total": int(row.new_total or 0),
        "duplicate_total": int(row.duplicate_total or 0),
        "error_code": row.error_code, "error_message": row.error_message,
        "failure_history": list(row.failure_history or []),
        "model_calls": list(row.model_calls or []),
        "consumer": row.consumer, "model_name": row.model_name,
        "prompt_version": row.prompt_version,
        "queued_at": row.queued_at, "started_at": row.started_at,
        "finished_at": row.finished_at,
        "cancelled_by": row.cancelled_by, "cancelled_at": row.cancelled_at,
        "cancel_reason": row.cancel_reason,
        "created_by": row.created_by, "created_role": row.created_role,
        "create_time": row.create_time, "update_time": row.update_time,
    }


def task_payload(session: Session, task: SupervisionLiteratureSearchTaskModel) -> dict:
    brief = session.get(SupervisionResearchBriefModel, task.brief_id)
    results = list(
        session.execute(
            select(
                SupervisionLiteratureResultModel.decision,
                SupervisionLiteratureResultModel.extract_state,
                func.count(SupervisionLiteratureResultModel.id),
            )
            .where(SupervisionLiteratureResultModel.search_task_id == task.id)
            .group_by(
                SupervisionLiteratureResultModel.decision,
                SupervisionLiteratureResultModel.extract_state,
            )
        ).all()
    )
    counts = {"total": 0, "included": 0, "excluded": 0, "read_later": 0, "pending": 0,
              "deep_used": 0}
    for decision, extract_state, number in results:
        number = int(number or 0)
        counts["total"] += number
        if decision in ("included", "excluded", "read_later", "pending"):
            counts[decision] += number
        if extract_state in ("queued", "running", "completed"):
            counts["deep_used"] += number
    subtasks = _subtasks_of(session, task, limit=20)
    failed_total = int(
        session.execute(
            select(func.count(SupervisionLiteratureSubtaskModel.id)).where(
                SupervisionLiteratureSubtaskModel.search_task_id == task.id,
                SupervisionLiteratureSubtaskModel.status == "failed",
            )
        ).scalar()
        or 0
    )
    conclusion_total = int(
        session.execute(
            select(func.count(SupervisionLiteratureConclusionModel.id)).where(
                SupervisionLiteratureConclusionModel.search_task_id == task.id,
                SupervisionLiteratureConclusionModel.status != "superseded",
                SupervisionLiteratureConclusionModel.deleted_at.is_(None),
            )
        ).scalar()
        or 0
    )
    plan_subtasks = [row for row in subtasks if row.kind == "plan"]
    latest_plan = plan_subtasks[0] if plan_subtasks else None
    return {
        "task_id": task.id, "code": task.code, "project_id": task.project_id,
        "status": task.status, "purpose": task.purpose, "goal": task.goal,
        "keywords": list(task.keywords or []), "synonyms": list(task.synonyms or []),
        "exclude_terms": list(task.exclude_terms or []), "sources": list(task.sources or []),
        "year_from": task.year_from, "year_to": task.year_to,
        "require_fulltext": bool(task.require_fulltext),
        "result_limit": int(task.result_limit or 0),
        "deep_extract_limit": int(task.deep_extract_limit or 0),
        "brief_id": task.brief_id, "brief_code": task.brief_code,
        "brief_version": int(task.brief_version or 0), "brief_kind": task.brief_kind,
        "brief_confirmed_by": task.brief_confirmed_by,
        "brief_confirmed_at": task.brief_confirmed_at,
        "brief_snapshot": dict(task.brief_snapshot or {}),
        # 研究定义出了新的已确认版本 → 页面提示学生是否重新检索（§4.4）。
        "brief_superseded": bool(
            brief is not None
            and brief.shared_version is not None
            and int(brief.shared_version) != int(task.brief_version or 0)
        ),
        "brief_shared_version": (int(brief.shared_version) if brief and brief.shared_version else None),
        "brief_latest_version": (int(brief.current_version) if brief else None),
        "search_plan": dict(task.search_plan or {}),
        "search_plan_note": task.search_plan_note,
        "search_plan_model": task.search_plan_model,
        "search_plan_prompt_version": task.search_plan_prompt_version,
        "search_plan_generated_at": task.search_plan_generated_at,
        "search_plan_confirmed_at": task.search_plan_confirmed_at,
        "search_plan_confirmed_by": task.search_plan_confirmed_by,
        "plan_status": latest_plan.status if latest_plan else "",
        "plan_error": latest_plan.error_message if latest_plan else "",
        "hits_total": int(task.hits_total or 0),
        "deduped_total": int(task.deduped_total or 0),
        "stored_total": int(task.stored_total or 0),
        "source_failures": list(task.source_failures or []),
        "searched_at": task.searched_at,
        "concluded_at": task.concluded_at, "closed_at": task.closed_at,
        "close_reason": task.close_reason,
        "created_by": task.created_by, "created_role": task.created_role,
        "create_time": task.create_time, "update_time": task.update_time,
        "result_total": counts["total"], "included_total": counts["included"],
        "excluded_total": counts["excluded"], "read_later_total": counts["read_later"],
        "undecided_total": counts["pending"],
        "deep_extract_used": counts["deep_used"],
        "conclusion_total": conclusion_total,
        "subtask_total": len(_subtasks_of(session, task, limit=200)),
        "subtask_failed_total": failed_total,
        "recent_subtasks": [subtask_payload(row) for row in subtasks],
    }


def result_payload(session: Session, row: SupervisionLiteratureResultModel) -> dict:
    cards = list(
        session.execute(
            select(SupervisionLiteratureEvidenceCardModel)
            .where(
                SupervisionLiteratureEvidenceCardModel.result_id == row.id,
                SupervisionLiteratureEvidenceCardModel.deleted_at.is_(None),
            )
            .order_by(SupervisionLiteratureEvidenceCardModel.code)
        ).scalars().all()
    )
    extraction = session.execute(
        select(SupervisionLiteratureExtractionModel)
        .where(SupervisionLiteratureExtractionModel.result_id == row.id)
        .order_by(SupervisionLiteratureExtractionModel.version.desc())
        .limit(1)
    ).scalars().first()
    return {
        "result_id": row.id, "code": row.code, "project_id": row.project_id,
        "search_task_id": row.search_task_id, "subtask_id": row.subtask_id,
        "dedup_key": row.dedup_key, "doi": row.doi, "arxiv_id": row.arxiv_id,
        "title_key": row.title_key,
        "title": row.title, "authors": row.authors, "year": row.year,
        "abstract": row.abstract, "url": row.url, "source": row.source,
        "sources": list(row.sources or []), "fulltext_status": row.fulltext_status,
        "relevance_level": row.relevance_level,
        "relevance_reason": row.relevance_reason,
        "relevance_matched": list(row.relevance_matched or []),
        "relevance_missing": list(row.relevance_missing or []),
        "relevance_score_source": row.relevance_score_source,
        "relevance_score": row.relevance_score,
        "relevance_rules_version": row.relevance_rules_version,
        "recommended_action": row.recommended_action,
        "semantic_analysis": dict(row.semantic_analysis or {}),
        "decision": row.decision, "decision_reason": row.decision_reason,
        "decided_by": row.decided_by, "decided_role": row.decided_role,
        "decided_at": row.decided_at,
        "decision_history": list(row.decision_history or []),
        "extract_state": row.extract_state,
        "evidence_cards": [evidence_card_payload(card) for card in cards],
        "latest_extraction_version": int(extraction.version) if extraction else 0,
        "first_seen_at": row.first_seen_at,
        "create_time": row.create_time, "update_time": row.update_time,
    }


def evidence_card_payload(row: SupervisionLiteratureEvidenceCardModel) -> dict:
    return {
        "card_id": row.id, "code": row.code, "project_id": row.project_id,
        "result_id": row.result_id, "result_code": row.result_code,
        "extraction_id": row.extraction_id,
        "extraction_version": int(row.extraction_version or 0),
        "evidence_type": row.evidence_type, "summary": row.summary,
        "quote": row.quote, "locator": dict(row.locator or {}),
        "quote_verified": bool(row.quote_verified), "level": row.level,
        "support_relation": row.support_relation,
        "brief_field": row.brief_field, "sub_question": row.sub_question,
        "created_by": row.created_by, "created_role": row.created_role,
        "create_time": row.create_time, "updated_at": row.updated_at,
    }


def extraction_payload(row: SupervisionLiteratureExtractionModel) -> dict:
    return {
        "extraction_id": row.id, "project_id": row.project_id,
        "result_id": row.result_id, "result_code": row.result_code,
        "version": int(row.version or 1), "subtask_id": row.subtask_id,
        "subtask_code": row.subtask_code, "level": row.level,
        "summary": row.summary, "research_problem": row.research_problem,
        "method": row.method, "algorithm": row.algorithm,
        "experiments": row.experiments, "results_metrics": row.results_metrics,
        "limitations": row.limitations,
        "differences": dict(row.differences or {}),
        "evidence": list(row.evidence or []),
        "model_name": row.model_name, "prompt_version": row.prompt_version,
        "created_by": row.created_by, "created_role": row.created_role,
        "create_time": row.create_time, "superseded_at": row.superseded_at,
    }


def conclusion_payload(
    session: Session, row: SupervisionLiteratureConclusionModel
) -> dict:
    """结论响应。``literature_changed`` 是现算的历史标记，不改写结论本身。"""
    current = literature_snapshot_rows(
        session, project_id=row.project_id, search_task_id=row.search_task_id or ""
    )
    current_hash = snapshot_hash(literature_snapshot_of(current))
    return {
        "conclusion_id": row.id, "code": row.code, "version": int(row.version or 1),
        "project_id": row.project_id,
        "search_task_id": row.search_task_id, "search_task_code": row.search_task_code,
        "statement": row.statement,
        "supported_facts": list(row.supported_facts or []),
        "student_judgements": list(row.student_judgements or []),
        "unverified_hypotheses": list(row.unverified_hypotheses or []),
        "research_gaps": list(row.research_gaps or []),
        "literature_snapshot": list(row.literature_snapshot or []),
        "literature_snapshot_hash": row.literature_snapshot_hash,
        "literature_count": int(row.literature_count or 0),
        "evidence_cards": list(row.evidence_cards or []),
        "status": row.status,
        "confirmed_by": row.confirmed_by, "confirmed_at": row.confirmed_at,
        "confirm_note": row.confirm_note,
        "mentor_state": row.mentor_state, "mentor_note": row.mentor_note,
        "mentor_by": row.mentor_by, "mentor_role": row.mentor_role,
        "mentor_at": row.mentor_at,
        "superseded_at": row.superseded_at,
        "superseded_by_code": row.superseded_by_code,
        "superseded_by_version": int(row.superseded_by_version or 0),
        # 依据已变化：纳入集合与当时快照哈希不同，本结论只作为历史保留。
        "literature_changed": bool(current_hash != row.literature_snapshot_hash),
        "current_included_count": len(current),
        "created_by": row.created_by, "created_role": row.created_role,
        "create_time": row.create_time, "update_time": row.update_time,
    }


def citation_payload(row: SupervisionLiteratureCitationModel) -> dict:
    return {
        "citation_id": row.id, "code": row.code, "project_id": row.project_id,
        "source_kind": row.source_kind, "source_ref": row.source_ref,
        "source_code": row.source_code, "source_owner_id": row.source_owner_id,
        "purpose": row.purpose,
        "bound_object_type": row.bound_object_type,
        "bound_object_code": row.bound_object_code,
        "snapshot": dict(row.snapshot or {}),
        "source_version": row.source_version, "content_hash": row.content_hash,
        "granted_by": row.granted_by, "granted_role": row.granted_role,
        "granted_at": row.granted_at, "note": row.note,
        "revoked_at": row.revoked_at, "revoked_by": row.revoked_by,
        "revoke_reason": row.revoke_reason,
        "create_time": row.create_time,
    }


__all__ = [
    "CITATION_PURPOSE_VALUES",
    "CODE_CITATION",
    "CODE_CONCLUSION",
    "CODE_EVIDENCE_CARD",
    "CODE_RESULT",
    "CODE_SEARCH_TASK",
    "DEFAULT_ABSTRACT_ANALYSIS_LIMIT",
    "DECISION_VALUES",
    "LiteratureSourceError",
    "LiteratureUnavailable",
    "MAX_COMPARE_PAPERS",
    "ModelCallError",
    "PROMPT_VERSION",
    "RULES_VERSION",
    "SupervisionLiteratureCitationModel",
    "SupervisionLiteratureConclusionModel",
    "SupervisionLiteratureEvidenceCardModel",
    "SupervisionLiteratureExtractionModel",
    "SupervisionLiteratureResultModel",
    "SupervisionLiteratureSearchTaskModel",
    "SupervisionLiteratureSubtaskModel",
    "UnconfiguredLiteratureProvider",
    "build_analysis_prompt",
    "build_compare_prompt",
    "build_extract_prompt",
    "build_plan_prompt",
    "cancel_subtask",
    "candidate_from_raw",
    "citation_payload",
    "close_task",
    "compare_papers",
    "conclusion_payload",
    "content_hash",
    "converge_interrupted_subtasks",
    "confirm_search_plan",
    "create_citation",
    "create_conclusion",
    "create_evidence_card",
    "create_task",
    "decide_result",
    "dedup_key_of",
    "evidence_card_payload",
    "extraction_payload",
    "generate_search_plan",
    "get_conclusion",
    "get_citation",
    "get_extraction",
    "get_result",
    "get_task",
    "list_citations",
    "list_conclusions",
    "list_evidence_cards",
    "list_extractions",
    "list_results",
    "list_subtasks",
    "list_tasks",
    "literature_snapshot_of",
    "literature_snapshot_rows",
    "mentor_review_conclusion",
    "parse_json_payload",
    "provider_is_available",
    "rank_candidate",
    "result_payload",
    "retry_subtask",
    "revoke_citation",
    "revise_conclusion",
    "run_abstract_analysis",
    "run_deep_extract",
    "run_search",
    "snapshot_hash",
    "subtask_payload",
    "task_payload",
    "update_task",
]
