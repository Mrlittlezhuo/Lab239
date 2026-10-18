"""两个已解析版本的结构差异 → 进度草稿载荷（§6.6 第6—9条，§16.6 第4、5条）。

钉死的底线：

- **只产出草稿，绝不发布**：本模块不碰数据库、不写业务对象、不调模型。
  ``ProgressDraft.published`` 恒为 ``False``、``requires_student_confirmation``
  恒为 ``True``，只有学生确认后由上层业务才允许发布为正式进展（§6.6 第9条）；
- **首次上传只建立基线**：没有上一版本时不生成任何差异条目，也不写
  "本周完成了什么"（§16.6 第4条）；
- **不编译、不运行**：差异只能说明"稿件文本变了"（E1 文档证据），
  不能说明实验已执行或结果成立（§16.6 第6条）；
- **每条差异都必须带来源**：文件 + 行号区间，来自解析结果，不来自推测。

章节跨版本的匹配是启发式的（先按 文件+层级+标题，再按 层级+标题，
最后按 文件+层级+编号），匹配不上就按新增/删除处理，绝不硬凑。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from chatchat.server.supervision.latex_parse import (
    PARSER_VERSION,
    Equation,
    FloatItem,
    LatexDocument,
    Section,
    SourceLocation,
)

DRAFT_VERSION = "latex-progress-draft/1.0"
#: 单次草稿最多产出的条目数；超出时置 ``truncated`` 并如实说明，不静默丢弃。
MAX_DRAFT_ITEMS = 200

#: 草稿口径：静态差异不等于实验完成。
DRAFT_CAVEAT = (
    "以上条目仅来自论文稿件的静态结构差异（E1 文档证据）：系统未编译 LaTeX，"
    "未运行论文中的公式、算法或实验，因此只能说明稿件形成了相应内容，"
    "不能证明实验已执行、结果已验证，也不能据此判定任务验收通过。"
)
BASELINE_NOTE = (
    "这是该稿件的第一个版本，只建立当前完成情况基线；"
    "没有上一版本可供比较，不能推断本周期完成了什么。"
)


@dataclass
class SectionChange:
    """一个章节在两个版本之间的变化。"""

    key: str                 # 匹配后的稳定标识（新版本一侧的 key，或匹配签名）
    title: str
    level: int
    number: str
    change: str              # added / removed / moved / renamed / renumbered / modified / unchanged
    old_word_count: int
    new_word_count: int
    word_count_delta: int
    old_path: str = ""
    old_line: int = 0
    new_path: str = ""
    new_line: int = 0
    new_end_line: int = 0
    detail: str = ""

    def locations(self) -> List[SourceLocation]:
        out: List[SourceLocation] = []
        if self.new_path:
            out.append(SourceLocation(self.new_path, self.new_line,
                                      self.new_end_line or self.new_line, self.title[:120]))
        if self.old_path:
            out.append(SourceLocation(self.old_path, self.old_line, self.old_line,
                                      self.title[:120]))
        return out

    def as_dict(self) -> Dict[str, object]:
        return {
            "key": self.key,
            "title": self.title,
            "level": self.level,
            "number": self.number,
            "change": self.change,
            "old_word_count": self.old_word_count,
            "new_word_count": self.new_word_count,
            "word_count_delta": self.word_count_delta,
            "old_path": self.old_path,
            "old_line": self.old_line,
            "new_path": self.new_path,
            "new_line": self.new_line,
            "new_end_line": self.new_end_line,
            "detail": self.detail,
            "locations": [loc.as_dict() for loc in self.locations()],
        }


@dataclass
class ResourceChange:
    """图表、公式、算法、引用、参考文献条目、图片文件的变化。"""

    kind: str            # figure / table / algorithm / equation / citation / bib_entry / image_file
    identifier: str      # label、cite key、图片归档路径
    change: str          # added / removed
    path: str            # 来源文件（added 取新版本，removed 取旧版本）
    line: int            # 来源行号
    end_line: int = 0
    old_path: str = ""
    old_line: int = 0
    caption: str = ""
    detail: str = ""

    def locations(self) -> List[SourceLocation]:
        out: List[SourceLocation] = []
        if self.change == "added" or self.path:
            out.append(SourceLocation(self.path, self.line, self.end_line or self.line,
                                      self.caption[:120]))
        if self.old_path:
            out.append(SourceLocation(self.old_path, self.old_line, self.old_line,
                                      self.caption[:120]))
        return out

    def as_dict(self) -> Dict[str, object]:
        return {
            "kind": self.kind,
            "identifier": self.identifier,
            "change": self.change,
            "path": self.path,
            "line": self.line,
            "end_line": self.end_line or self.line,
            "old_path": self.old_path,
            "old_line": self.old_line,
            "caption": self.caption,
            "detail": self.detail,
            "locations": [loc.as_dict() for loc in self.locations()],
        }


@dataclass
class ProgressDraftItem:
    """进度草稿里的一条：文案 + 变化类型 + 可定位来源 + 数值变化。"""

    item_type: str          # section / figure / table / algorithm / equation /
                            # citation / bib_entry / image_file / word_count
    change: str             # added / removed / modified / summary
    statement: str          # 中文草稿文案（学生可编辑）
    locations: List[SourceLocation] = field(default_factory=list)
    delta: Dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, object]:
        return {
            "item_type": self.item_type,
            "change": self.change,
            "statement": self.statement,
            "locations": [loc.as_dict() for loc in self.locations],
            "delta": dict(self.delta),
        }


@dataclass
class ProgressDraft:
    """进度草稿载荷。**必须由学生确认后**才能发布为正式进展（§6.6 第9条）。"""

    draft_id: str
    version_ref: str
    base_version_ref: str
    status: str = "draft"                        # draft / baseline
    published: bool = False                      # 恒 False：库本身永不发布
    requires_student_confirmation: bool = True   # 恒 True
    items: List[ProgressDraftItem] = field(default_factory=list)
    summary: str = ""
    caveat: str = DRAFT_CAVEAT
    truncated: bool = False
    dropped_items: int = 0
    draft_version: str = DRAFT_VERSION
    generated_at: str = ""                       # 由调用方（业务层）填写

    def as_dict(self) -> Dict[str, object]:
        return {
            "draft_id": self.draft_id,
            "draft_version": self.draft_version,
            "version_ref": self.version_ref,
            "base_version_ref": self.base_version_ref,
            "status": self.status,
            "published": self.published,
            "requires_student_confirmation": self.requires_student_confirmation,
            "summary": self.summary,
            "caveat": self.caveat,
            "truncated": self.truncated,
            "dropped_items": self.dropped_items,
            "generated_at": self.generated_at,
            "items": [item.as_dict() for item in self.items],
        }


@dataclass
class LatexDiff:
    """两个版本的结构差异 + 进度草稿。``first_version`` 时只有基线。"""

    version_ref: str
    base_version_ref: str
    first_version: bool
    baseline_only: bool
    parser_version: str = PARSER_VERSION
    sections: List[SectionChange] = field(default_factory=list)
    resources: List[ResourceChange] = field(default_factory=list)
    old_word_count: int = 0
    new_word_count: int = 0
    word_count_delta: int = 0
    added_sections: int = 0
    removed_sections: int = 0
    modified_sections: int = 0
    counts: Dict[str, int] = field(default_factory=dict)
    note: str = ""
    draft: Optional[ProgressDraft] = None

    def as_dict(self) -> Dict[str, object]:
        return {
            "parser_version": self.parser_version,
            "version_ref": self.version_ref,
            "base_version_ref": self.base_version_ref,
            "first_version": self.first_version,
            "baseline_only": self.baseline_only,
            "old_word_count": self.old_word_count,
            "new_word_count": self.new_word_count,
            "word_count_delta": self.word_count_delta,
            "added_sections": self.added_sections,
            "removed_sections": self.removed_sections,
            "modified_sections": self.modified_sections,
            "counts": dict(self.counts),
            "note": self.note,
            "sections": [s.as_dict() for s in self.sections],
            "resources": [r.as_dict() for r in self.resources],
            "draft": self.draft.as_dict() if self.draft else None,
        }


def _norm_title(title: str) -> str:
    return re.sub(r"\s+", " ", (title or "").strip().lower())


def _float_identity(item: FloatItem) -> str:
    if item.label:
        return f"label:{item.label}"
    if item.caption:
        return f"{item.kind}:caption:{_norm_title(item.caption)}"
    return f"{item.kind}:pos:{item.path}:{item.line}"


def _equation_identity(item: Equation) -> str:
    if item.label:
        return f"label:{item.label}"
    return f"equation:pos:{item.path}:{item.line}"


def _titles_similar(left: str, right: str) -> bool:
    """标题是否像"同一个章节改了名"：共享词，或共享 ≥2 个相邻字符。

    只用于第三遍按编号配对；完全不像的标题绝不硬凑成"改名"。
    """
    a, b = _norm_title(left), _norm_title(right)
    if not a or not b:
        return False
    if a == b or a.startswith(b) or b.startswith(a):
        return True
    tokens_a = set(re.findall(r"[a-z0-9]+", a))
    tokens_b = set(re.findall(r"[a-z0-9]+", b))
    if tokens_a & tokens_b:
        return True
    bigrams_a = {a[index:index + 2] for index in range(len(a) - 1)}
    bigrams_b = {b[index:index + 2] for index in range(len(b) - 1)}
    return len(bigrams_a & bigrams_b) >= 2


def _match_sections(
    old: Sequence[Section], new: Sequence[Section]
) -> List[Tuple[Optional[Section], Optional[Section]]]:
    """三遍匹配：文件+层级+标题 → 层级+标题 → 文件+层级+编号（且标题相似）。

    匹配不上就按新增/删除处理；绝不把两个明显不同的章节硬凑成"改动"。
    """
    pairs: List[Tuple[Optional[Section], Optional[Section]]] = []
    remaining_old = list(old)
    remaining_new = list(new)

    def pass_by(key_fn, accept=None) -> None:
        buckets: Dict[str, List[Section]] = {}
        for section in remaining_old:
            buckets.setdefault(key_fn(section), []).append(section)
        for section in list(remaining_new):
            bucket = buckets.get(key_fn(section))
            if not bucket:
                continue
            index = 0
            while index < len(bucket) and accept is not None and not accept(
                bucket[index], section
            ):
                index += 1
            if index >= len(bucket):
                continue
            partner = bucket.pop(index)
            pairs.append((partner, section))
            remaining_new.remove(section)
            remaining_old.remove(partner)

    pass_by(lambda s: f"{s.path}|{s.level}|{_norm_title(s.title)}")
    pass_by(lambda s: f"{s.level}|{_norm_title(s.title)}")
    # 第三遍只接受"标题也像同一章节"的配对：编号相同但标题毫不相干的，
    # 按新增 + 删除处理，比编造成"改名"更诚实。
    pass_by(
        lambda s: f"{s.path}|{s.level}|{s.number}",
        accept=lambda old_item, new_item: _titles_similar(old_item.title, new_item.title),
    )
    for section in remaining_old:
        pairs.append((section, None))
    for section in remaining_new:
        pairs.append((None, section))
    return pairs


def _describe_section(old: Optional[Section], new: Optional[Section]) -> SectionChange:
    section = new or old
    assert section is not None
    change = "unchanged"
    detail = ""
    if old is None:
        change = "added"
        detail = "新版本里新出现的章节"
    elif new is None:
        change = "removed"
        detail = "旧版本有、新版本已不再有"
    else:
        if old.path != new.path:
            change = "moved"
            detail = f"章节从 {old.path} 移到 {new.path}"
        elif _norm_title(old.title) != _norm_title(new.title):
            change = "renamed"
            detail = f"标题由“{old.title}”改为“{new.title}”"
        elif old.number != new.number:
            change = "renumbered"
            detail = f"编号由 {old.number} 变为 {new.number}"
        elif old.word_count != new.word_count:
            change = "modified"
            detail = "章节内容有增删"
    return SectionChange(
        key=(new or old).key,
        title=section.title,
        level=section.level,
        number=section.number,
        change=change,
        old_word_count=old.word_count if old else 0,
        new_word_count=new.word_count if new else 0,
        word_count_delta=(new.word_count if new else 0) - (old.word_count if old else 0),
        old_path=old.path if old else "",
        old_line=old.line if old else 0,
        new_path=new.path if new else "",
        new_line=new.line if new else 0,
        new_end_line=new.end_line if new else 0,
        detail=detail,
    )


def _float_changes(old: LatexDocument, new: LatexDocument) -> List[ResourceChange]:
    changes: List[ResourceChange] = []
    for kind in ("figure", "table", "algorithm"):
        old_items = [f for f in old.floats if f.kind == kind]
        new_items = [f for f in new.floats if f.kind == kind]
        old_by = {_float_identity(f): f for f in old_items}
        new_by = {_float_identity(f): f for f in new_items}
        for key, item in new_by.items():
            if key in old_by:
                continue
            changes.append(
                ResourceChange(
                    kind=kind,
                    identifier=item.label or item.caption or f"{item.path}:{item.line}",
                    change="added",
                    path=item.path,
                    line=item.line,
                    end_line=item.end_line,
                    caption=item.caption,
                    detail=f"环境 {item.name}；按 label/标题匹配旧版本时未找到",
                )
            )
        for key, item in old_by.items():
            if key in new_by:
                continue
            changes.append(
                ResourceChange(
                    kind=kind,
                    identifier=item.label or item.caption or f"{item.path}:{item.line}",
                    change="removed",
                    path=item.path,
                    line=item.line,
                    end_line=item.end_line,
                    caption=item.caption,
                    detail="旧版本存在、新版本不再存在",
                )
            )
    return changes


def _equation_changes(old: LatexDocument, new: LatexDocument) -> List[ResourceChange]:
    changes: List[ResourceChange] = []
    old_by = {_equation_identity(e): e for e in old.equations}
    new_by = {_equation_identity(e): e for e in new.equations}
    for key, item in new_by.items():
        if key in old_by:
            continue
        changes.append(
            ResourceChange(
                kind="equation",
                identifier=item.label or f"{item.path}:{item.line}",
                change="added",
                path=item.path,
                line=item.line,
                end_line=item.end_line,
                detail=f"显示数学块 {item.kind}；无 label 时按位置匹配，跨版本可能不稳定",
            )
        )
    for key, item in old_by.items():
        if key in new_by:
            continue
        changes.append(
            ResourceChange(
                kind="equation",
                identifier=item.label or f"{item.path}:{item.line}",
                change="removed",
                path=item.path,
                line=item.line,
                end_line=item.end_line,
                detail="旧版本存在、新版本不再存在",
            )
        )
    return changes


def _citation_changes(old: LatexDocument, new: LatexDocument) -> List[ResourceChange]:
    changes: List[ResourceChange] = []
    old_keys: Dict[str, Tuple[str, int]] = {}
    new_keys: Dict[str, Tuple[str, int]] = {}
    for citation in old.citations:
        for key in citation.keys:
            old_keys.setdefault(key, (citation.path, citation.line))
    for citation in new.citations:
        for key in citation.keys:
            new_keys.setdefault(key, (citation.path, citation.line))
    for key, (path, line) in new_keys.items():
        if key in old_keys:
            continue
        changes.append(
            ResourceChange(
                kind="citation", identifier=key, change="added", path=path, line=line,
                detail="正文新增对该参考文献的引用",
            )
        )
    for key, (path, line) in old_keys.items():
        if key in new_keys:
            continue
        changes.append(
            ResourceChange(
                kind="citation", identifier=key, change="removed", path=path, line=line,
                detail="正文不再引用该参考文献",
            )
        )
    return changes


def _bib_changes(old: LatexDocument, new: LatexDocument) -> List[ResourceChange]:
    changes: List[ResourceChange] = []
    old_by = {e.key: e for e in old.bib_entries}
    new_by = {e.key: e for e in new.bib_entries}
    for key, entry in new_by.items():
        if key in old_by:
            continue
        changes.append(
            ResourceChange(
                kind="bib_entry", identifier=key, change="added",
                path=entry.path, line=entry.line,
                caption=entry.fields.get("title", ""),
                detail=f".bib 里新增 {entry.entry_type} 条目",
            )
        )
    for key, entry in old_by.items():
        if key in new_by:
            continue
        changes.append(
            ResourceChange(
                kind="bib_entry", identifier=key, change="removed",
                path=entry.path, line=entry.line,
                caption=entry.fields.get("title", ""),
                detail=".bib 里不再存在该条目",
            )
        )
    return changes


def _image_changes(old: LatexDocument, new: LatexDocument) -> List[ResourceChange]:
    changes: List[ResourceChange] = []
    old_used = {g.resolved for g in old.graphics if g.resolved}
    new_used = {g.resolved for g in new.graphics if g.resolved}
    for path in sorted(new_used - old_used):
        item = next(g for g in new.graphics if g.resolved == path)
        changes.append(
            ResourceChange(
                kind="image_file", identifier=path, change="added",
                path=item.path, line=item.line,
                detail=f"正文新引用了归档内的图片文件 {path}",
            )
        )
    for path in sorted(old_used - new_used):
        item = next(g for g in old.graphics if g.resolved == path)
        changes.append(
            ResourceChange(
                kind="image_file", identifier=path, change="removed",
                path=item.path, line=item.line,
                detail=f"正文不再引用该图片文件 {path}",
            )
        )
    return changes


def _main_location(document: LatexDocument) -> List[SourceLocation]:
    """总字数这类整体结论的来源：主文件第一行（整篇的载体文件）。"""
    source = document.sources.get(document.main_path)
    snippet = source.snippet(1) if source else ""
    return [SourceLocation(document.main_path, 1, 1, snippet)]


def _build_draft(
    version_ref: str,
    base_version_ref: str,
    sections: Sequence[SectionChange],
    resources: Sequence[ResourceChange],
    old_word_count: int,
    new_word_count: int,
    new_document: LatexDocument,
) -> ProgressDraft:
    items: List[ProgressDraftItem] = []

    for change in sections:
        if change.change == "unchanged":
            continue
        label = {"added": "新增章节", "removed": "删除章节"}.get(change.change, "改动章节")
        number = f"（第 {change.number} 节）" if change.number else ""
        if change.change == "added":
            text = f"{label}《{change.title}》{number}，新增 {change.word_count_delta} 字"
        elif change.change == "removed":
            text = f"{label}《{change.title}》，减少 {abs(change.word_count_delta)} 字"
        else:
            text = (
                f"{label}《{change.title}》{number}："
                f"字数 {change.old_word_count} → {change.new_word_count}"
                f"（{'+' if change.word_count_delta >= 0 else ''}{change.word_count_delta}）"
            )
            if change.detail:
                text += f"；{change.detail}"
        items.append(
            ProgressDraftItem(
                item_type="section", change=change.change, statement=text,
                locations=change.locations(),
                delta={
                    "word_count_delta": change.word_count_delta,
                    "old_word_count": change.old_word_count,
                    "new_word_count": change.new_word_count,
                    "level": change.level,
                    "number": change.number,
                },
            )
        )

    kind_label = {
        "figure": "图", "table": "表", "algorithm": "算法", "equation": "公式",
        "citation": "参考文献引用", "bib_entry": "参考文献条目", "image_file": "图片文件",
    }
    for change in resources:
        verb = "新增" if change.change == "added" else "删除"
        subject = kind_label.get(change.kind, change.kind)
        extra = f"：{change.caption}" if change.caption else ""
        items.append(
            ProgressDraftItem(
                item_type=change.kind, change=change.change,
                statement=f"{verb}{subject} {change.identifier}{extra}",
                locations=change.locations(),
                delta={"kind": change.kind, "identifier": change.identifier},
            )
        )

    delta = new_word_count - old_word_count
    if delta:
        items.append(
            ProgressDraftItem(
                item_type="word_count", change="summary",
                statement=f"正文总字数 {old_word_count} → {new_word_count}"
                          f"（净{'增' if delta > 0 else '减'} {abs(delta)} 字）",
                locations=_main_location(new_document),
                delta={"word_count_delta": delta, "old_word_count": old_word_count,
                       "new_word_count": new_word_count},
            )
        )

    truncated = len(items) > MAX_DRAFT_ITEMS
    dropped = len(items) - MAX_DRAFT_ITEMS if truncated else 0
    kept = items[:MAX_DRAFT_ITEMS]
    summary = (
        f"与上一版本相比：章节新增 {sum(1 for s in sections if s.change == 'added')} 个、"
        f"删除 {sum(1 for s in sections if s.change == 'removed')} 个、"
        f"改动 {sum(1 for s in sections if s.change not in ('added', 'removed', 'unchanged'))} 个；"
        f"图表/公式/引用变化 {len(resources)} 项；字数净变化 {delta:+d}。"
    )
    if truncated:
        summary += f"（条目超过 {MAX_DRAFT_ITEMS} 条，已省略 {dropped} 条）"

    digest = hashlib.sha256(
        "|".join(
            [version_ref, base_version_ref] + [item.statement for item in kept]
        ).encode("utf-8", "replace")
    ).hexdigest()[:16]
    return ProgressDraft(
        draft_id=f"PD-{digest}",
        version_ref=version_ref,
        base_version_ref=base_version_ref,
        status="draft",
        published=False,                      # 库永不发布
        requires_student_confirmation=True,   # 必须学生确认
        items=kept,
        summary=summary,
        truncated=truncated,
        dropped_items=dropped,
    )


def diff_latex_documents(
    old: Optional[LatexDocument],
    new: LatexDocument,
    *,
    version_ref: str,
    base_version_ref: str = "",
) -> LatexDiff:
    """比较两个已解析版本；首次上传只给基线，不给差异进度。

    ``old`` 为 ``None`` 表示该稿件的第一个版本：``sections``/``resources``
    为空、``word_count_delta`` 固定为 0、草稿 ``status="baseline"`` 且无条目。
    """
    if old is None:
        draft = ProgressDraft(
            draft_id=f"PD-baseline-{hashlib.sha256(version_ref.encode('utf-8', 'replace')).hexdigest()[:16]}",
            version_ref=version_ref,
            base_version_ref="",
            status="baseline",
            published=False,
            requires_student_confirmation=True,
            items=[],
            summary=BASELINE_NOTE,
            caveat=DRAFT_CAVEAT,
        )
        return LatexDiff(
            version_ref=version_ref,
            base_version_ref="",
            first_version=True,
            baseline_only=True,
            sections=[],
            resources=[],
            old_word_count=0,
            new_word_count=new.word_count,
            word_count_delta=0,   # 基线不声称"变化"
            note=BASELINE_NOTE,
            draft=draft,
            counts={"added_sections": 0, "removed_sections": 0, "modified_sections": 0,
                    "resource_changes": 0},
        )

    pairs = _match_sections(old.sections, new.sections)
    section_changes = [_describe_section(old_part, new_part) for old_part, new_part in pairs]
    resources = (
        _float_changes(old, new)
        + _equation_changes(old, new)
        + _citation_changes(old, new)
        + _bib_changes(old, new)
        + _image_changes(old, new)
    )
    added = sum(1 for s in section_changes if s.change == "added")
    removed = sum(1 for s in section_changes if s.change == "removed")
    modified = sum(
        1 for s in section_changes
        if s.change not in ("added", "removed", "unchanged")
    )
    draft = _build_draft(
        version_ref, base_version_ref, section_changes, resources,
        old.word_count, new.word_count, new,
    )
    return LatexDiff(
        version_ref=version_ref,
        base_version_ref=base_version_ref,
        first_version=False,
        baseline_only=False,
        sections=section_changes,
        resources=resources,
        old_word_count=old.word_count,
        new_word_count=new.word_count,
        word_count_delta=new.word_count - old.word_count,
        added_sections=added,
        removed_sections=removed,
        modified_sections=modified,
        counts={
            "added_sections": added,
            "removed_sections": removed,
            "modified_sections": modified,
            "resource_changes": len(resources),
        },
        note="差异来自两个版本的静态结构比较；未编译、未运行，不能据此判定实验完成。",
        draft=draft,
    )
