import type {
  Attachment,
  SelfAssessment,
  Submission,
  SubmissionInput,
  Task,
  TaskAc,
} from "@/api/supervision";

/**
 * 进展与提交面板的纯逻辑：草稿状态推导、提交前完整性提醒（只警告不阻止，§6.4）、
 * 后端 JSON 快照字段（ac_snapshot / self_assessment / materials / completeness_check）
 * 的防御性解析。与后端 supervision_work_repository 的口径保持一致，可单测。
 */

// ---------------------------------------------------------------------------
// 标签映射：简单枚举翻译。§5.6 的派生混合状态（如「待导师审核」）一律用
// 后端现算的 `label`，这里不重复实现。
// ---------------------------------------------------------------------------

export const SELF_ASSESSMENT_VALUES: SelfAssessment[] = [
  "done",
  "partially_done",
  "not_done",
  "not_applicable",
];

export const SELF_ASSESSMENT_LABEL: Record<SelfAssessment, string> = {
  done: "已完成",
  partially_done: "部分完成",
  not_done: "未完成",
  not_applicable: "不适用",
};

export const SUBMISSION_STATUS_LABEL: Record<string, string> = {
  submitted: "已提交",
  withdrawn: "已撤回",
  superseded: "已被新版本取代",
};

export const REVIEW_STATUS_LABEL: Record<string, string> = {
  not_started: "未开始",
  drafting: "审核草稿中",
  published: "已发布",
};

export const AC_CONCLUSION_LABEL: Record<string, string> = {
  met: "已满足",
  partially_met: "部分满足",
  not_met: "未满足",
  more_evidence_required: "需要更多证据",
  not_applicable: "不适用",
};

export const REVIEW_DECISION_LABEL: Record<string, string> = {
  approved: "已通过",
  revision_required: "退回修改",
  rejected: "已拒绝",
  paused: "暂缓",
};

export const ROLE_LABEL: Record<string, string> = {
  student: "学生",
  mentor: "导师",
};

// ---------------------------------------------------------------------------
// 自评草稿
// ---------------------------------------------------------------------------

export interface AcDraft {
  ac_code: string;
  self_assessment: SelfAssessment | "";
  evidence: string;
  note: string;
}

export function initialAcDrafts(acs: TaskAc[]): Record<string, AcDraft> {
  const drafts: Record<string, AcDraft> = {};
  for (const ac of acs) {
    drafts[ac.ac_code] = { ac_code: ac.ac_code, self_assessment: "", evidence: "", note: "" };
  }
  return drafts;
}

export function draftList(drafts: Record<string, AcDraft>): AcDraft[] {
  return Object.values(drafts);
}

/** 还没有选自评结论的 AC（与后端 self_assessment 完整性检查同口径）。 */
export function unassessedAcCodes(acs: TaskAc[], drafts: Record<string, AcDraft>): string[] {
  return acs
    .filter((ac) => !drafts[ac.ac_code]?.self_assessment)
    .map((ac) => ac.ac_code);
}

/**
 * 声称「已完成/部分完成」却没写证据的 AC。提交前给学生的提醒；
 * 后端不校验这一项，所以只警告，不影响提交成功。
 */
export function acsMissingEvidence(
  acs: TaskAc[],
  drafts: Record<string, AcDraft>,
): string[] {
  return acs
    .filter((ac) => {
      const draft = drafts[ac.ac_code];
      if (!draft) return false;
      const claimsProgress =
        draft.self_assessment === "done" || draft.self_assessment === "partially_done";
      return claimsProgress && !draft.evidence.trim();
    })
    .map((ac) => ac.ac_code);
}

/** 组装 SubmissionInput.self_assessment：跳过没选结论的行，避免后端 400。 */
export function buildSelfAssessmentPayload(
  drafts: Record<string, AcDraft>,
): NonNullable<SubmissionInput["self_assessment"]> {
  return draftList(drafts)
    .filter((draft) => draft.self_assessment)
    .map((draft) => ({
      ac_code: draft.ac_code,
      self_assessment: draft.self_assessment as SelfAssessment,
      evidence: draft.evidence,
      note: draft.note,
    }));
}

// ---------------------------------------------------------------------------
// 进展草稿
// ---------------------------------------------------------------------------

export interface ProgressDraft {
  done: string;
  results: string;
  materials: string;
  problems: string;
  next_steps: string;
}

export const EMPTY_PROGRESS_DRAFT: ProgressDraft = {
  done: "",
  results: "",
  materials: "",
  problems: "",
  next_steps: "",
};

/** 后端要求至少一项非空（create_progress），前端用同样的条件控制按钮。 */
export function progressHasContent(draft: ProgressDraft): boolean {
  return [draft.done, draft.results, draft.materials, draft.problems, draft.next_steps].some(
    (value) => value.trim() !== "",
  );
}

export interface LinkDraft {
  title: string;
  url: string;
}

/** 只保留填了 URL 的链接；标题缺省时用 URL 兜底。 */
export function buildLinksPayload(links: LinkDraft[]): { title: string; url: string }[] {
  return links
    .filter((link) => link.url.trim() !== "")
    .map((link) => ({
      title: link.title.trim() !== "" ? link.title.trim() : link.url.trim(),
      url: link.url.trim(),
    }));
}

// ---------------------------------------------------------------------------
// 提交前完整性提醒（镜像后端 completeness_warnings，§6.4：只提醒，不阻止）
// ---------------------------------------------------------------------------

export interface CompletenessHint {
  key: string;
  passed: boolean;
  message: string;
}

export interface SubmissionPayloadDraft {
  git_commit: string;
  experiment_notes: string;
  unfinished_items: string;
}

const CODE_KINDS = ["code", "diff", "archive"];
const RUNTIME_KINDS = ["log", "result", "report"];

export function clientCompletenessHints(
  task: Task,
  drafts: Record<string, AcDraft>,
  selectedAttachments: Attachment[],
  payload: SubmissionPayloadDraft,
): CompletenessHint[] {
  const kinds = new Set(selectedAttachments.map((item) => item.kind));
  const hints: CompletenessHint[] = [];

  const missingAssessment = unassessedAcCodes(task.acs ?? [], drafts);
  hints.push({
    key: "self_assessment",
    passed: missingAssessment.length === 0,
    message:
      missingAssessment.length === 0
        ? "已逐条填写自评"
        : `以下验收标准还没有自评：${missingAssessment.join("、")}`,
  });

  const requiredMaterials = (task.required_materials ?? []).filter((m) => m.required);
  const missingMaterials = requiredMaterials.filter(
    (m) => !kinds.has(String(m.key ?? "")),
  );
  hints.push({
    key: "required_materials",
    passed: missingMaterials.length === 0,
    message:
      missingMaterials.length === 0
        ? "必填材料已关联"
        : `仍缺少必填材料：${missingMaterials
            .map((m) => m.label || m.key || "")
            .filter(Boolean)
            .join("、")}`,
  });

  if (task.requires_code_review) {
    const hasCommit = payload.git_commit.trim() !== "";
    const hasCodeFile = CODE_KINDS.some((kind) => kinds.has(kind));
    hints.push({
      key: "code_material",
      passed: hasCommit && hasCodeFile,
      message:
        hasCommit && hasCodeFile
          ? "已填写 commit 并上传代码材料"
          : "任务要求代码检查：需要填写 commit，并上传平台内的代码、差异或归档文件",
    });
  }

  if (task.requires_runtime) {
    const hasExperiment = payload.experiment_notes.trim() !== "";
    const hasOutput = RUNTIME_KINDS.some((kind) => kinds.has(kind));
    hints.push({
      key: "runtime_material",
      passed: hasExperiment && hasOutput,
      message:
        hasExperiment && hasOutput
          ? "已填写实验配置与结果说明"
          : "任务要求实际运行：需要填写配置/命令/环境，并上传日志或结果文件",
    });
  }

  hints.push({
    key: "unfinished_items",
    passed: payload.unfinished_items.trim() !== "",
    message:
      payload.unfinished_items.trim() !== ""
        ? "已列出未完成项"
        : "请明确列出未完成项，导师按此核对范围",
  });

  return hints;
}

// ---------------------------------------------------------------------------
// 撤回权限（后端还有「导师已发布审核不能撤回」，由 409 兜底）
// ---------------------------------------------------------------------------

export function canWithdraw(
  submission: Pick<Submission, "status" | "author_id">,
  currentUserId: string | null | undefined,
  role: string,
): boolean {
  return (
    role === "student" &&
    submission.status === "submitted" &&
    Boolean(currentUserId) &&
    submission.author_id === currentUserId
  );
}

// ---------------------------------------------------------------------------
// 后端 JSON 快照字段的防御性解析（接口类型是 Record<string, unknown>）
// ---------------------------------------------------------------------------

function str(value: unknown): string {
  return typeof value === "string" ? value : value == null ? "" : String(value);
}

function flag(value: unknown): boolean {
  return value === true || value === "true" || value === 1;
}

function num(value: unknown): number {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

export interface AcSnapshotRow {
  ac_code: string;
  statement: string;
  evidence_requirement: string;
  required: boolean;
  check_type: string;
  version: number;
}

export function parseAcSnapshot(rows: Record<string, unknown>[] | undefined): AcSnapshotRow[] {
  return (rows ?? []).map((row) => ({
    ac_code: str(row.ac_code),
    statement: str(row.statement),
    evidence_requirement: str(row.evidence_requirement),
    required: flag(row.required),
    check_type: str(row.check_type),
    version: num(row.version),
  }));
}

export interface SelfAssessmentRow {
  ac_code: string;
  self_assessment: string;
  evidence: string;
  note: string;
}

export function parseSelfAssessmentRows(
  rows: Record<string, unknown>[] | undefined,
): SelfAssessmentRow[] {
  return (rows ?? []).map((row) => ({
    ac_code: str(row.ac_code),
    self_assessment: str(row.self_assessment),
    evidence: str(row.evidence),
    note: str(row.note),
  }));
}

export interface MaterialRow {
  attachment_code: string;
  name: string;
  kind: string;
  version: number;
  size: number;
}

export function parseMaterialRows(rows: Record<string, unknown>[] | undefined): MaterialRow[] {
  return (rows ?? []).map((row) => ({
    attachment_code: str(row.attachment_code),
    name: str(row.name),
    kind: str(row.kind),
    version: num(row.version),
    size: num(row.size),
  }));
}

export function parseCompletenessRows(
  rows: Record<string, unknown>[] | undefined,
): CompletenessHint[] {
  return (rows ?? []).map((row) => ({
    key: str(row.key),
    passed: flag(row.passed),
    message: str(row.message),
  }));
}

export interface LinkRow {
  title: string;
  url: string;
}

export function parseLinkRows(rows: Record<string, unknown>[] | undefined): LinkRow[] {
  return (rows ?? [])
    .map((row) => ({ title: str(row.title), url: str(row.url) }))
    .filter((row) => row.url !== "");
}

/** 把提交快照里的 AC 与学生当时的自评按 ac_code 关联，供详情页逐条展示。 */
export function joinAcWithAssessment(
  snapshotRows: AcSnapshotRow[],
  assessmentRows: SelfAssessmentRow[],
): (AcSnapshotRow & { assessment: SelfAssessmentRow | null })[] {
  const byCode = new Map(assessmentRows.map((row) => [row.ac_code, row]));
  return snapshotRows.map((row) => ({ ...row, assessment: byCode.get(row.ac_code) ?? null }));
}
