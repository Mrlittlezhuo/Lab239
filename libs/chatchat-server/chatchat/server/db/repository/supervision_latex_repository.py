"""LaTeX 稿件静态分析的业务层（里程碑5 路由层的持久化与接线）。

职责边界：解析本身全部在 ``chatchat/server/supervision``（纯静态、不编译、
不联网）；本模块只负责**取附件 → 安全解压到一次性临时目录 → 调解析库 →
落快照/草稿 → 审计**，并在解析完立刻删掉临时目录。

三条硬口径：

1. **危险归档必须被拒且留审计**：``inspect_zip_package`` 未通过时不写任何
   业务快照，但会先提交一条审计事件再抛 ``ZipSafetyError``（路由层随后的
   rollback 只回滚业务改动，审计已经落库）。危险命令（``\\write18`` 等）属于
   合法归档里的静态发现，照常落快照并原样呈现，不静默通过。
2. **超预算如实透出**：``partially_read`` / ``not_read`` / ``truncated`` /
   ``dropped_items`` 全部进快照与草稿，绝不伪装成"完整读过"。
3. **草稿不等于进展**：差异只落 ``supervision_latex_progress_draft``
   （``published`` 恒 False、``requires_student_confirmation`` 恒 True）；
   只有创建该草稿的学生本人确认，才在 ``supervision_progress_update`` 里写
   一条独立进展，草稿行只被标记为已确认。重复确认 → 409（不做幂等复用），
   因为"再生成一条进展"和"确认没生效"在页面上必须区分开。

模型摘要是**可选**的：注入模型时按统一容量闸门取放容量
（``CONSUMER_PROGRESS_DRAFT`` + ``capacity_mode="external"``，适配器由路由层
注入），模型未接入或调用失败只把 ``model_summary_status`` 写成
``unavailable``/``failed`` 并留中文说明——草稿是纯静态产物，绝不因为模型
不可用而失败。
"""
from __future__ import annotations

import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from chatchat.server.db.models.supervision_latex_model import (
    CODE_LATEX_SNAPSHOT,
    CODE_PROGRESS_DRAFT,
    SupervisionLatexDraftConfirmationModel,
    SupervisionLatexProgressDraftModel,
    SupervisionLatexSnapshotModel,
)
from chatchat.server.db.models.supervision_work_model import (
    SupervisionAttachmentModel,
)
from chatchat.server.db.repository.supervision_repository import (
    ROLE_STUDENT,
    new_id,
    next_code,
    record_audit,
    require_member,
)
from chatchat.server.db.repository.supervision_work_repository import (
    WorkError,
    WorkValidationError,
    attachment_path,
    get_attachment,
)
from chatchat.server.model_capacity import CONSUMER_PROGRESS_DRAFT
from chatchat.server.supervision import (
    BASELINE_NOTE,
    CHECKER_VERSION,
    DEFAULT_MAX_SOURCE_BYTES,
    DEFAULT_MAX_SOURCE_FILES,
    DRAFT_CAVEAT,
    LatexAnalysis,
    PARSER_VERSION,
    ProgressDraft,
    ZIP_SAFETY_VERSION,
    ZipLimits,
    ZipSafetyError,
    analyze_latex_archive,
    diff_latex_documents,
    inspect_zip_package,
)

#: 模型未接入时的固定口径：草稿照常生成，只说明摘要没生成。
MODEL_SUMMARY_UNAVAILABLE_NOTE = (
    "模型能力未接入：本次没有生成模型摘要。草稿条目本身来自稿件的静态结构"
    "差异，不依赖模型，可照常确认或丢弃。"
)


class LatexAccessDenied(ValueError):
    """项目角色或"草稿归属"不满足；路由层收敛成 403（不是 404）。"""


class MainFileSelectionRequired(ValueError):
    """主文件没能唯一确定：必须把候选交给用户选，不能替用户猜（§16.6 第2条）。

    ``status`` 为 ``ambiguous`` 时路由层给 409 + 候选清单；``not_found`` /
    ``invalid_selection`` 是请求字段或归档内容问题 → 400。
    """

    def __init__(
        self,
        *,
        attachment_code: str,
        status: str,
        message: str,
        candidates: Optional[list[dict]] = None,
    ) -> None:
        super().__init__(message)
        self.attachment_code = attachment_code
        self.status = status
        self.message = message
        self.candidates = list(candidates or [])


def _now() -> datetime:
    return datetime.now()


# ---------------------------------------------------------------------------
# 上限与预算
# ---------------------------------------------------------------------------


def latex_limits() -> ZipLimits:
    """本次实际生效的归档安全上限（部署可调，生效值必须写进快照）。

    默认值来自解析库（300 文件 / 单文件 10 MB / 总 40 MB / 压缩比 40 倍）。
    环境变量只放宽"接收"范围，不改变"不编译、不联网、不递归解压"的口径。
    """

    def _env_int(name: str, default: int) -> int:
        raw = os.environ.get(name)
        try:
            return int(raw) if raw else default
        except ValueError:
            return default

    base = ZipLimits()
    ratio_raw = os.environ.get("CHATCHAT_LATEX_MAX_COMPRESSION_RATIO")
    try:
        ratio = float(ratio_raw) if ratio_raw else base.max_compression_ratio
    except ValueError:
        ratio = base.max_compression_ratio

    return ZipLimits(
        max_entries=_env_int("CHATCHAT_LATEX_MAX_ENTRIES", base.max_entries),
        max_total_bytes=_env_int("CHATCHAT_LATEX_MAX_TOTAL_BYTES", base.max_total_bytes),
        max_single_bytes=_env_int("CHATCHAT_LATEX_MAX_SINGLE_BYTES", base.max_single_bytes),
        max_compression_ratio=ratio,
    )


def parse_budget() -> dict:
    """解析预算（解析库常量）。超预算的文件按部分读取/未读取如实标记。"""
    return {
        "max_source_bytes": DEFAULT_MAX_SOURCE_BYTES,
        "max_source_files": DEFAULT_MAX_SOURCE_FILES,
    }


# ---------------------------------------------------------------------------
# 安全解压 + 解析（临时目录用完即删）
# ---------------------------------------------------------------------------


def _make_extract_dir(attachment: SupervisionAttachmentModel) -> Path:
    """一次性空解压目录。稿件是不可信输入，绝不复用、绝不从里面执行任何东西。"""
    return Path(tempfile.mkdtemp(prefix=f"chatchat-latex-{attachment.id[:8]}-"))


def _remove_extract_dir(directory: Path) -> None:
    shutil.rmtree(str(directory), ignore_errors=True)


def _analyze_attachment(
    *,
    attachment: SupervisionAttachmentModel,
    main_file: str = "",
    limits: Optional[ZipLimits] = None,
) -> tuple[ZipLimits, Any, LatexAnalysis]:
    """``inspect_zip_package`` + ``analyze_latex_archive``，解析完立刻清理。

    检查未通过时抛 ``ZipSafetyError``（全部命中项都在 ``problems`` 里），
    解压目录要么完整要么什么都没写；两种情况都会删掉临时目录。
    """
    limits = limits or latex_limits()
    if (attachment.ext or "").lower() != ".zip":
        raise WorkValidationError(
            "LaTeX 稿件分析只接收 .zip 归档；"
            f"当前附件 {attachment.code} 的类型是 {attachment.ext or '（无扩展名）'}"
        )
    archive = attachment_path(attachment)
    report = inspect_zip_package(archive, limits=limits)
    if not report.ok:
        raise ZipSafetyError(report.problems)

    target = _make_extract_dir(attachment)
    try:
        extracted, analysis = analyze_latex_archive(
            archive, target, main_file=main_file, limits=limits
        )
    finally:
        _remove_extract_dir(target)
    return limits, extracted, analysis


def _audit_rejection(
    session: Session,
    *,
    project_id: str,
    actor_id: str,
    actor_role: str,
    attachment: SupervisionAttachmentModel,
    error: ZipSafetyError,
) -> None:
    """危险归档被拒：审计必须留下痕迹，所以这里**显式提交这一条事件**。

    业务快照一行都不写，路由层随后 rollback 也只影响未提交的业务改动；
    不这样处理的话"拒绝了什么"就只剩日志，§12 要求的可追溯性会丢。
    """
    problems = list(error.problems)
    codes = "、".join(sorted({p.code for p in problems if p.code})) or "unspecified"
    first = problems[0].message if problems else "压缩包检查未通过"
    record_audit(
        session,
        project_id=project_id,
        object_type="latex_archive",
        object_code=attachment.code,
        actor_id=actor_id,
        actor_role=actor_role,
        action="拒绝稿件归档",
        detail=f"安全检查未通过（{codes}）：{first}",
    )
    session.commit()


def _read_state_rows(document: Any) -> tuple[list[dict], list[dict]]:
    """把解析库里非 ``fully_read`` 的文件原样列出来。"""
    partially: list[dict] = []
    not_read: list[dict] = []
    if document is None:
        return partially, not_read
    for path, source in sorted(document.sources.items()):
        if source.read_state == "fully_read":
            continue
        row = {
            "path": path,
            "read_state": source.read_state,
            "truncated_at_line": source.truncated_at_line,
            "reason": source.not_read_reason,
        }
        if source.read_state == "partially_read":
            partially.append(row)
        else:
            not_read.append(row)
    return partially, not_read


def _require_document(
    analysis: LatexAnalysis, attachment: SupervisionAttachmentModel
) -> None:
    """主文件没唯一确定时不能继续解析结构，更不能替用户猜一个。"""
    if analysis.document is not None:
        return
    main = analysis.main
    candidates = [c.as_dict() for c in main.candidates]
    if main.status == "ambiguous":
        names = "、".join(c["path"] for c in candidates[:10]) or "（无候选）"
        raise MainFileSelectionRequired(
            attachment_code=attachment.code,
            status=main.status,
            message=(
                f"附件 {attachment.code} 里有多个同分主文件候选，请选择主文件后重试：{names}"
            ),
            candidates=candidates,
        )
    if main.status == "invalid_selection":
        raise MainFileSelectionRequired(
            attachment_code=attachment.code,
            status=main.status,
            message=(
                f"指定的主文件不在附件 {attachment.code} 的归档清单里；"
                "请按归档内相对路径填写 main_file"
            ),
            candidates=candidates,
        )
    raise MainFileSelectionRequired(
        attachment_code=attachment.code,
        status=main.status or "not_found",
        message=(
            f"附件 {attachment.code} 里没有找到含 "
            "\\documentclass 与 \\begin{document} 的主文件"
        ),
        candidates=candidates,
    )


# ---------------------------------------------------------------------------
# 分析快照
# ---------------------------------------------------------------------------


def _add_snapshot(
    session: Session,
    *,
    project_id: str,
    actor_id: str,
    actor_role: str,
    attachment: SupervisionAttachmentModel,
    main_file: str,
    limits: ZipLimits,
    extracted: Any,
    analysis: LatexAnalysis,
) -> SupervisionLatexSnapshotModel:
    """把一次分析落成快照行（只追加，不覆盖旧快照）。"""
    document = analysis.document
    check = analysis.check
    findings = [f.as_dict() for f in (check.findings if check else [])]
    partially, not_read = _read_state_rows(document)
    snapshot = SupervisionLatexSnapshotModel(
        id=new_id(),
        code=next_code(
            session,
            SupervisionLatexSnapshotModel,
            SupervisionLatexSnapshotModel.code,
            CODE_LATEX_SNAPSHOT,
        ),
        project_id=project_id,
        attachment_id=attachment.id,
        attachment_code=attachment.code,
        attachment_name=attachment.name,
        attachment_sha256=attachment.sha256,
        attachment_version=attachment.version,
        main_file=(main_file or "")[:500],
        main_status=analysis.main.status or "not_found",
        main_path=(analysis.main.main_path or "")[:500],
        main_json=analysis.main.as_dict(),
        # 三个版本号一律取解析库常量：升级解析器后旧快照仍能被认出是哪一版算的。
        parser_version=PARSER_VERSION,
        checker_version=CHECKER_VERSION,
        zip_safety_version=ZIP_SAFETY_VERSION,
        limits_json=limits.as_dict(),
        parse_budget_json=parse_budget(),
        document_present=document is not None,
        document_json=document.as_dict() if document else None,
        check_json=check.as_dict() if check else None,
        outline=document.outline() if document else [],
        counts=document.counts() if document else {},
        findings=findings,
        finding_counts=dict(check.counts) if check else {},
        dangerous_findings=[f for f in findings if f.get("kind") == "dangerous_command"],
        partially_read=partially,
        not_read=not_read,
        unreferenced_tex=list(document.unreferenced_tex) if document else [],
        available_files=list(analysis.available_files),
        extracted_files=[f.as_dict() for f in extracted.files],
        file_count=len(extracted.files),
        total_bytes=extracted.total_bytes,
        word_count=document.word_count if document else 0,
        compiled=False,  # 恒 False：系统从不编译 LaTeX
        created_by=actor_id,
        created_role=actor_role,
        create_time=_now(),
    )
    session.add(snapshot)
    session.flush()
    return snapshot


def analyze_attachment(
    session: Session,
    *,
    project_ref: str,
    actor_id: str,
    attachment_ref: str,
    main_file: str = "",
) -> SupervisionLatexSnapshotModel:
    """分析一份 .zip 稿件附件 → 落一份分析快照（只追加）。

    危险归档：不写快照、抛 ``ZipSafetyError``（路由层 → 409 + 全部命中项），
    但先提交一条"拒绝稿件归档"审计事件。主文件不确定时快照照样落
    （``main_status`` + 候选清单都在里面），供页面提示用户另选主文件。
    """
    project, role = require_member(session, project_ref, actor_id)
    attachment = get_attachment(
        session, project_ref=project.id, attachment_ref=attachment_ref, actor_id=actor_id
    )
    limits = latex_limits()
    try:
        limits, extracted, analysis = _analyze_attachment(
            attachment=attachment, main_file=main_file, limits=limits
        )
    except ZipSafetyError as exc:
        _audit_rejection(
            session,
            project_id=project.id,
            actor_id=actor_id,
            actor_role=role,
            attachment=attachment,
            error=exc,
        )
        raise
    if analysis.main.status == "invalid_selection":
        # 指定的主文件不在归档清单里：这是请求字段问题，不落一份注定没用的快照。
        raise WorkValidationError(
            f"指定的主文件 {main_file} 不在附件 {attachment.code} 的归档清单里；"
            "请按归档内相对路径填写 main_file"
        )
    snapshot = _add_snapshot(
        session,
        project_id=project.id,
        actor_id=actor_id,
        actor_role=role,
        attachment=attachment,
        main_file=main_file,
        limits=limits,
        extracted=extracted,
        analysis=analysis,
    )
    record_audit(
        session,
        project_id=project.id,
        object_type="latex_snapshot",
        object_code=snapshot.code,
        actor_id=actor_id,
        actor_role=role,
        action="分析 LaTeX 稿件",
        detail=(
            f"{attachment.code}（sha256 {attachment.sha256[:12]}…）："
            f"主文件 {snapshot.main_status or '未定'}，文件 {snapshot.file_count} 个，"
            f"章节 {snapshot.counts.get('sections', 0)} 个，"
            f"发现 {len(snapshot.findings)} 项"
            f"（危险命令 {len(snapshot.dangerous_findings)} 项，"
            f"部分读取 {len(snapshot.partially_read)} 个，未读取 {len(snapshot.not_read)} 个）"
        ),
    )
    if snapshot.dangerous_findings:
        record_audit(
            session,
            project_id=project.id,
            object_type="latex_snapshot",
            object_code=snapshot.code,
            actor_id=actor_id,
            actor_role=role,
            action="稿件危险命令提示",
            detail="；".join(
                f"{item.get('path', '')}:{item.get('line', 0)} {item.get('target', '')}"
                for item in snapshot.dangerous_findings[:20]
            )[:2000],
        )
    session.flush()
    return snapshot


def get_snapshot(
    session: Session, *, project_ref: str, snapshot_ref: str, actor_id: str
) -> SupervisionLatexSnapshotModel:
    """按内部 id 或可读编号（``LX-001``）取快照；非成员与不存在由上层收敛。"""
    project, _ = require_member(session, project_ref, actor_id)
    snapshot = session.execute(
        select(SupervisionLatexSnapshotModel).where(
            SupervisionLatexSnapshotModel.project_id == project.id,
            (SupervisionLatexSnapshotModel.id == snapshot_ref)
            | (SupervisionLatexSnapshotModel.code == snapshot_ref),
        )
    ).scalar_one_or_none()
    if snapshot is None:
        raise LookupError("分析快照不存在")
    return snapshot


def list_snapshots(
    session: Session, *, project_ref: str, actor_id: str, attachment_ref: str = ""
) -> list[SupervisionLatexSnapshotModel]:
    project, _ = require_member(session, project_ref, actor_id)
    stmt = (
        select(SupervisionLatexSnapshotModel)
        .where(SupervisionLatexSnapshotModel.project_id == project.id)
        .order_by(SupervisionLatexSnapshotModel.create_time.desc())
    )
    if attachment_ref:
        attachment = get_attachment(
            session, project_ref=project.id, attachment_ref=attachment_ref,
            actor_id=actor_id,
        )
        stmt = stmt.where(SupervisionLatexSnapshotModel.attachment_id == attachment.id)
    return list(session.execute(stmt).scalars().all())


# ---------------------------------------------------------------------------
# 进度草稿
# ---------------------------------------------------------------------------


def _summarize_with_model(
    draft: ProgressDraft,
    *,
    llm: Any,
    capacity: Any,
    draft_code: str,
    actor_id: str,
) -> tuple[str, str, str, str]:
    """可选模型摘要：把草稿条目汇总成一段中文说明。

    返回 ``(摘要, 状态, 说明, 模型名)``。**任何失败都不抛出**：草稿是纯静态
    产物，模型缺席只影响摘要字段。取放容量的是注入的 ``capacity``
    （``CapacityLeaseAdapter``），消费者固定为已登记的
    ``CONSUMER_PROGRESS_DRAFT``；配对的模型对象必须是
    ``capacity_mode="external"``，否则模型会在发请求前抱怨租约不在。
    """
    if llm is None or not hasattr(llm, "invoke"):
        return "", "unavailable", MODEL_SUMMARY_UNAVAILABLE_NOTE, ""
    statements = [item.statement for item in draft.items if item.statement][:80]
    if not statements:
        return (
            "",
            "unavailable",
            "本次没有可汇总的草稿条目（首次上传只建立基线），因此没有模型摘要。",
            "",
        )
    model_name = str(
        getattr(llm, "model_name", "") or getattr(llm, "model", "") or ""
    )[:120]
    messages = [
        {
            "role": "system",
            "content": (
                "你是研究进展摘要助手。输入是论文稿件两个版本的静态结构差异条目"
                "（系统未编译 LaTeX，未运行任何公式、算法或实验）。请用中文写成"
                "一段 120 字以内的客观说明，只描述稿件写了什么内容，"
                "不得表述为实验已执行、结果已验证或任务已验收通过。"
            ),
        },
        {
            "role": "user",
            "content": "草稿条目：\n" + "\n".join(f"- {s}" for s in statements),
        },
    ]
    token = None
    try:
        if capacity is not None:
            token = capacity.acquire(
                consumer=CONSUMER_PROGRESS_DRAFT,
                business_ref=draft_code,
                user_id=actor_id,
            )
        result = llm.invoke(messages)
        content = getattr(result, "content", None)
        if content is None and isinstance(result, dict):
            content = result.get("content")
        if content is None and isinstance(result, str):
            content = result
        text = str(content or "").strip()
        if not text:
            return "", "failed", "模型没有返回可用摘要；草稿条目本身不受影响。", model_name
        return text[:4000], "generated", "", model_name
    except Exception as exc:  # 摘要失败绝不波及草稿生成
        return (
            "",
            "failed",
            f"模型摘要生成失败（{type(exc).__name__}）；草稿条目本身不受影响，"
            "可照常确认或丢弃。",
            model_name,
        )
    finally:
        if capacity is not None:
            try:
                capacity.release(token, reason="completed")
            except TypeError:  # 兼容 release(token)
                capacity.release(token)
            except Exception:  # 释放失败只影响可观测性，不影响草稿
                pass


def build_progress_draft(
    session: Session,
    *,
    project_ref: str,
    actor_id: str,
    attachment_ref: str,
    base_attachment_ref: str = "",
    main_file: str = "",
    base_main_file: str = "",
    llm: Any = None,
    capacity: Any = None,
) -> SupervisionLatexProgressDraftModel:
    """两个附件版本 → 结构差异 → 落一份进度草稿（只追加，不覆盖旧草稿）。

    - 两侧都会先落分析快照（``LX-``），草稿绑定这两份快照与附件哈希；
    - ``base_attachment_ref`` 为空 → 首次上传：``baseline_only=True``、
      草稿 ``status="baseline"`` 且**没有条目**，不编造"进展"；
    - 主文件不确定 → 抛 ``MainFileSelectionRequired``，不生成草稿；
    - 危险归档 → 抛 ``ZipSafetyError`` 并留拒绝审计。
    """
    project, role = require_member(session, project_ref, actor_id)
    if base_attachment_ref and base_attachment_ref == attachment_ref:
        raise WorkValidationError(
            "上一版本必须与当前版本是不同的附件；"
            "同一份稿件重复比较只会得到空差异，不能据此断定“这周没干活”"
        )
    limits = latex_limits()

    # 先把两侧都解析完，再落快照：任何一侧被安全检查拒掉时都还没有业务行
    # 落库，拒绝审计的单独提交不会顺带把半套草稿状态写进去。
    attachment = get_attachment(
        session, project_ref=project.id, attachment_ref=attachment_ref, actor_id=actor_id
    )
    try:
        limits, extracted, analysis = _analyze_attachment(
            attachment=attachment, main_file=main_file, limits=limits
        )
    except ZipSafetyError as exc:
        _audit_rejection(
            session,
            project_id=project.id,
            actor_id=actor_id,
            actor_role=role,
            attachment=attachment,
            error=exc,
        )
        raise

    base_attachment: Optional[SupervisionAttachmentModel] = None
    base_extracted = None
    base_analysis: Optional[LatexAnalysis] = None
    if base_attachment_ref:
        base_attachment = get_attachment(
            session, project_ref=project.id, attachment_ref=base_attachment_ref,
            actor_id=actor_id,
        )
        try:
            _, base_extracted, base_analysis = _analyze_attachment(
                attachment=base_attachment, main_file=base_main_file, limits=limits,
            )
        except ZipSafetyError as exc:
            _audit_rejection(
                session,
                project_id=project.id,
                actor_id=actor_id,
                actor_role=role,
                attachment=base_attachment,
                error=exc,
            )
            raise

    _require_document(analysis, attachment)
    if base_attachment is not None:
        _require_document(base_analysis, base_attachment)

    base_snapshot: Optional[SupervisionLatexSnapshotModel] = None
    old_document = None
    if base_attachment is not None:
        base_snapshot = _add_snapshot(
            session,
            project_id=project.id,
            actor_id=actor_id,
            actor_role=role,
            attachment=base_attachment,
            main_file=base_main_file,
            limits=limits,
            extracted=base_extracted,
            analysis=base_analysis,
        )
        old_document = base_analysis.document
    snapshot = _add_snapshot(
        session,
        project_id=project.id,
        actor_id=actor_id,
        actor_role=role,
        attachment=attachment,
        main_file=main_file,
        limits=limits,
        extracted=extracted,
        analysis=analysis,
    )

    diff = diff_latex_documents(
        old_document,
        analysis.document,
        version_ref=snapshot.code,
        base_version_ref=base_snapshot.code if base_snapshot else "",
    )
    payload: ProgressDraft = diff.draft
    payload.generated_at = _now().isoformat(timespec="seconds")

    row = SupervisionLatexProgressDraftModel(
        id=new_id(),
        code=next_code(
            session,
            SupervisionLatexProgressDraftModel,
            SupervisionLatexProgressDraftModel.code,
            CODE_PROGRESS_DRAFT,
        ),
        project_id=project.id,
        library_draft_id=payload.draft_id[:80],
        draft_version=payload.draft_version,
        parser_version=diff.parser_version,
        checker_version=snapshot.checker_version,
        zip_safety_version=snapshot.zip_safety_version,
        status=payload.status or "draft",
        published=False,
        requires_student_confirmation=True,
        baseline_only=bool(diff.baseline_only),
        snapshot_id=snapshot.id,
        snapshot_code=snapshot.code,
        attachment_id=attachment.id,
        attachment_code=attachment.code,
        attachment_sha256=attachment.sha256,
        attachment_version=attachment.version,
        main_file=(main_file or "")[:500],
        base_snapshot_id=base_snapshot.id if base_snapshot else None,
        base_snapshot_code=base_snapshot.code if base_snapshot else "",
        base_attachment_id=base_attachment.id if base_attachment else None,
        base_attachment_code=base_attachment.code if base_attachment else "",
        base_attachment_sha256=base_attachment.sha256 if base_attachment else "",
        base_attachment_version=base_attachment.version if base_attachment else 0,
        base_main_file=(base_main_file or "")[:500],
        summary=payload.summary or (BASELINE_NOTE if diff.baseline_only else ""),
        caveat=payload.caveat or DRAFT_CAVEAT,
        items=[item.as_dict() for item in payload.items],
        item_count=len(payload.items),
        truncated=payload.truncated,
        dropped_items=payload.dropped_items,
        draft_json=payload.as_dict(),
        diff_counts=dict(diff.counts),
        word_count_old=diff.old_word_count,
        word_count_new=diff.new_word_count,
        word_count_delta=diff.word_count_delta,
        created_by=actor_id,
        created_role=role,
        create_time=_now(),
        update_time=_now(),
    )
    session.add(row)
    session.flush()

    summary, status, note, model_name = _summarize_with_model(
        payload, llm=llm, capacity=capacity, draft_code=row.code, actor_id=actor_id,
    )
    row.model_summary = summary
    row.model_summary_status = status
    row.model_summary_note = note
    row.model_name = model_name
    row.update_time = _now()

    record_audit(
        session,
        project_id=project.id,
        object_type="latex_progress_draft",
        object_code=row.code,
        actor_id=actor_id,
        actor_role=role,
        action="生成 LaTeX 进度草稿",
        detail=(
            f"{row.attachment_code} ← {row.base_attachment_code or '（无上一版本）'}："
            f"{row.summary[:300]}"
        ),
    )
    session.flush()
    return row


def get_draft(
    session: Session, *, project_ref: str, draft_ref: str, actor_id: str
) -> SupervisionLatexProgressDraftModel:
    project, _ = require_member(session, project_ref, actor_id)
    draft = session.execute(
        select(SupervisionLatexProgressDraftModel).where(
            SupervisionLatexProgressDraftModel.project_id == project.id,
            (SupervisionLatexProgressDraftModel.id == draft_ref)
            | (SupervisionLatexProgressDraftModel.code == draft_ref),
        )
    ).scalar_one_or_none()
    if draft is None:
        raise LookupError("进度草稿不存在")
    return draft


def list_drafts(
    session: Session,
    *,
    project_ref: str,
    actor_id: str,
    status: str = "",
    attachment_ref: str = "",
) -> list[SupervisionLatexProgressDraftModel]:
    """草稿列表（新→旧）。历史草稿永远保留：确认/丢弃只改状态，不删行。"""
    project, _ = require_member(session, project_ref, actor_id)
    stmt = (
        select(SupervisionLatexProgressDraftModel)
        .where(SupervisionLatexProgressDraftModel.project_id == project.id)
        .order_by(SupervisionLatexProgressDraftModel.create_time.desc())
    )
    if status:
        stmt = stmt.where(SupervisionLatexProgressDraftModel.status == status)
    if attachment_ref:
        attachment = get_attachment(
            session, project_ref=project.id, attachment_ref=attachment_ref,
            actor_id=actor_id,
        )
        stmt = stmt.where(
            SupervisionLatexProgressDraftModel.attachment_id == attachment.id
        )
    return list(session.execute(stmt).scalars().all())


def _progress_done_text(draft: SupervisionLatexProgressDraftModel, student_done: str) -> str:
    """进展正文 = 学生自己写的说明（可编辑）+ 草稿条目 + 固定口径。"""
    head = (student_done or "").strip() or (draft.summary or "").strip()
    lines = [head] if head else []
    statements = [
        str(item.get("statement") or "").strip()
        for item in (draft.items or [])
        if str(item.get("statement") or "").strip()
    ]
    if statements:
        lines.append("【LaTeX 稿件静态差异条目】")
        lines.extend(f"- {item}" for item in statements)
    if draft.truncated:
        lines.append(f"（条目超过上限，另有 {draft.dropped_items} 条未列出）")
    lines.append(f"【口径】{(draft.caveat or DRAFT_CAVEAT).strip()}")
    return "\n".join(lines)


def confirm_draft(
    session: Session,
    *,
    project_ref: str,
    draft_ref: str,
    actor_id: str,
    done: str = "",
    results: str = "",
    materials: str = "",
    problems: str = "",
    next_steps: str = "",
    task_ref: str = "",
    milestone_ref: str = "",
) -> tuple[SupervisionLatexProgressDraftModel, Any]:
    """学生确认草稿 → 写一条正式进展（``supervision_progress_update``）。

    口径（选 409，不做幂等复用）：

    - **只有创建该草稿的学生本人**能确认：导师调用 → 403，别的成员同理；
    - 已确认的草稿再确认 → 409，并告诉调用者已经生成的进展编号；不做
      "幂等返回同一条进展"，因为页面上"确认没生效"和"已经确认过"必须是
      两种可区分的反馈；
    - 已丢弃的草稿不能确认 → 409，需要时重新生成一份草稿；
    - 进展内容来自草稿条目 + 学生可编辑的正文，``links`` 里回指草稿与
      两侧附件，导师读到时能追到当时那份快照。
    """
    from chatchat.server.db.repository import supervision_work_repository as work

    project, role = require_member(session, project_ref, actor_id)
    draft = get_draft(session, project_ref=project.id, draft_ref=draft_ref, actor_id=actor_id)
    if role != ROLE_STUDENT:
        raise LatexAccessDenied("只有学生能把进度草稿确认成正式进展")
    if draft.created_by != actor_id:
        raise LatexAccessDenied("只有创建该草稿的学生本人能确认它")
    if draft.status == "confirmed":
        raise WorkError(
            f"草稿 {draft.code} 已确认，进展记录 {draft.confirmed_progress_code} "
            "已经生成；不重复生成进展"
        )
    if draft.status == "discarded":
        raise WorkError(
            f"草稿 {draft.code} 已丢弃；需要时请重新生成一份进度草稿"
        )

    progress = work.create_progress(
        session,
        project_ref=project.id,
        actor_id=actor_id,
        task_ref=task_ref,
        milestone_ref=milestone_ref,
        done=_progress_done_text(draft, done),
        results=(results or "").strip(),
        materials=(materials or "").strip(),
        problems=(problems or "").strip(),
        next_steps=(next_steps or "").strip(),
        links=[
            {
                "kind": "latex_progress_draft",
                "draft_code": draft.code,
                "attachment_code": draft.attachment_code,
                "base_attachment_code": draft.base_attachment_code,
                "snapshot_code": draft.snapshot_code,
                "base_snapshot_code": draft.base_snapshot_code,
                "label": f"LaTeX 进度草稿 {draft.code}",
            }
        ],
    )

    confirmation = SupervisionLatexDraftConfirmationModel(
        id=new_id(),
        draft_id=draft.id,
        draft_code=draft.code,
        project_id=project.id,
        progress_id=progress.id,
        progress_code=progress.code,
        draft_status=draft.status,
        actor_id=actor_id,
        actor_role=role,
        statement=(done or "").strip()[:8000],
        item_count=draft.item_count,
        created_at=_now(),
    )
    session.add(confirmation)

    draft.status = "confirmed"
    draft.confirmed_progress_id = progress.id
    draft.confirmed_progress_code = progress.code
    draft.confirmed_by = actor_id
    draft.confirmed_role = role
    draft.confirmed_at = _now()
    draft.update_time = _now()

    try:
        session.flush()
    except IntegrityError as exc:
        # 并发下的兜底：唯一约束保证同一草稿只有一条确认记录。
        session.rollback()
        raise WorkError(f"草稿 {draft.code} 刚刚已被确认；不重复生成进展") from exc

    record_audit(
        session,
        project_id=project.id,
        object_type="latex_progress_draft",
        object_code=draft.code,
        actor_id=actor_id,
        actor_role=role,
        action="确认 LaTeX 进度草稿",
        detail=f"已生成进展 {progress.code}（草稿条目 {draft.item_count} 条）",
    )
    session.flush()
    return draft, progress


def discard_draft(
    session: Session,
    *,
    project_ref: str,
    draft_ref: str,
    actor_id: str,
    reason: str = "",
) -> SupervisionLatexProgressDraftModel:
    """丢弃草稿：只标记状态，不删任何数据（快照与草稿行都留着）。"""
    project, role = require_member(session, project_ref, actor_id)
    draft = get_draft(session, project_ref=project.id, draft_ref=draft_ref, actor_id=actor_id)
    if draft.created_by != actor_id:
        raise LatexAccessDenied("只有创建该草稿的成员能丢弃它")
    if draft.status == "confirmed":
        raise WorkError(
            f"草稿 {draft.code} 已确认为进展 {draft.confirmed_progress_code}，"
            "不能丢弃；进展本身请在进展记录里处理"
        )
    if draft.status == "discarded":
        raise WorkError(f"草稿 {draft.code} 已经是丢弃状态")
    draft.status = "discarded"
    draft.discarded_by = actor_id
    draft.discarded_role = role
    draft.discarded_at = _now()
    draft.discard_reason = (reason or "").strip()[:4000]
    draft.update_time = _now()
    record_audit(
        session,
        project_id=project.id,
        object_type="latex_progress_draft",
        object_code=draft.code,
        actor_id=actor_id,
        actor_role=role,
        action="丢弃 LaTeX 进度草稿",
        detail=draft.discard_reason or "（未填写原因）",
    )
    session.flush()
    return draft


__all__ = [
    "CODE_LATEX_SNAPSHOT",
    "CODE_PROGRESS_DRAFT",
    "LatexAccessDenied",
    "MainFileSelectionRequired",
    "MODEL_SUMMARY_UNAVAILABLE_NOTE",
    "analyze_attachment",
    "build_progress_draft",
    "confirm_draft",
    "discard_draft",
    "get_draft",
    "get_snapshot",
    "latex_limits",
    "list_drafts",
    "list_snapshots",
    "parse_budget",
]
