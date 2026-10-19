import type {
  AcConclusion,
  Review,
  ReviewAcConclusion,
  ReviewDecision,
} from "@/api/supervision";

import type { AcSnapshotRow } from "@/components/supervision/review/snapshot";

/**
 * 导师审核面板的纯逻辑（《学生研究与导师审核功能重新设计方案》§5.4、§5.5、§7.3）：
 * 逐条结论矩阵状态、草稿 diff、发布前校验与请求体组装。
 *
 * 校验口径与后端 `supervision_work_repository`（`_write_ac_conclusions`、
 * `publish_review`）一一对应，只为让按钮和提示提前出现；后端仍是最终裁决。
 * §5.6 的派生混合状态（「待导师审核」「退回修改」等）一律用后端现算的 `label`，
 * 这里只翻译简单枚举。
 */

export const AC_CONCLUSION_VALUES: AcConclusion[] = [
  "met",
  "partially_met",
  "not_met",
  "more_evidence_required",
  "not_applicable",
];

/** 与后端 `MENTOR_CONCLUSION_LABEL` 同措辞，保证与 AI 的 `suggested_label` 对得上。 */
export const AC_CONCLUSION_LABEL: Record<AcConclusion, string> = {
  met: "满足",
  partially_met: "部分满足",
  not_met: "不满足",
  more_evidence_required: "要求补充材料",
  not_applicable: "不适用",
};

export const REVIEW_DECISION_VALUES: Exclude<ReviewDecision, "">[] = [
  "approved",
  "revision_required",
  "rejected",
  "paused",
];

export const REVIEW_DECISION_LABEL: Record<Exclude<ReviewDecision, "">, string> = {
  approved: "确认通过",
  revision_required: "要求修改",
  rejected: "拒绝",
  paused: "暂缓审核",
};

export const REVIEW_STATUS_LABEL: Record<string, string> = {
  not_started: "未开始",
  drafting: "审核草稿中",
  published: "已发布",
};

export const SUBMISSION_STATUS_LABEL: Record<string, string> = {
  submitted: "已提交",
  withdrawn: "已撤回",
  superseded: "已被新版本取代",
};

/** 导师结论与 AI 建议的关系（后端 `ai_relation` 取值）。 */
export const AI_RELATION_LABEL: Record<string, string> = {
  none: "未关联 AI 建议",
  adopted: "采纳 AI 建议",
  different: "与 AI 建议不同",
};

export const SELF_ASSESSMENT_LABEL: Record<string, string> = {
  done: "已完成",
  partially_done: "部分完成",
  not_done: "未完成",
  not_applicable: "不适用",
};

// ---------------------------------------------------------------------------
// 逐条结论矩阵
// ---------------------------------------------------------------------------

export interface ConclusionDraft {
  ac_code: string;
  conclusion: AcConclusion | "";
  reason: string;
  ai_relation: string;
}

export type ConclusionMatrix = Record<string, ConclusionDraft>;

/** 矩阵按提交快照建行；任务里的 AC 变了也不会多出或漏掉行（§11.4）。 */
export function initialMatrix(rows: AcSnapshotRow[]): ConclusionMatrix {
  const matrix: ConclusionMatrix = {};
  for (const row of rows) {
    matrix[row.ac_code] = {
      ac_code: row.ac_code,
      conclusion: "",
      reason: "",
      ai_relation: "none",
    };
  }
  return matrix;
}

/** 已保存的结论（草稿或已发布）覆盖初始矩阵；快照里没有的 ac_code 直接丢弃。 */
export function matrixFromConclusions(
  rows: AcSnapshotRow[],
  saved: ReviewAcConclusion[] | undefined,
): ConclusionMatrix {
  const matrix = initialMatrix(rows);
  for (const row of saved ?? []) {
    const target = matrix[row.ac_code];
    if (!target) continue;
    target.conclusion = row.conclusion || "";
    target.reason = row.reason ?? "";
    target.ai_relation = row.ai_relation || "none";
  }
  return matrix;
}

export function matrixList(matrix: ConclusionMatrix): ConclusionDraft[] {
  return Object.values(matrix);
}

/**
 * 组装 `conclusions` 请求体。后端 `_write_ac_conclusions` 对空结论直接 400，
 * 所以没选结论的行必须跳过（可选 AC 允许留空，§5.5）。
 */
export function conclusionPayload(matrix: ConclusionMatrix) {
  return matrixList(matrix)
    .filter((row) => row.conclusion !== "")
    .map((row) => ({
      ac_code: row.ac_code,
      conclusion: row.conclusion as AcConclusion,
      reason: row.reason,
      ai_relation: row.ai_relation || "none",
    }));
}

/** 草稿 diff：与已保存结论逐字段比较，只返回真正改动的行。 */
export function changedConclusions(
  matrix: ConclusionMatrix,
  saved: ReviewAcConclusion[] | undefined,
): ConclusionDraft[] {
  const byCode = new Map((saved ?? []).map((row) => [row.ac_code, row]));
  return matrixList(matrix).filter((row) => {
    const old = byCode.get(row.ac_code);
    if (!old) return row.conclusion !== "";
    return (
      (old.conclusion || "") !== row.conclusion ||
      (old.reason || "") !== row.reason ||
      (old.ai_relation || "none") !== (row.ai_relation || "none")
    );
  });
}

// ---------------------------------------------------------------------------
// 审核表单（矩阵之外的整体部分）
// ---------------------------------------------------------------------------

export interface ReviewFormState {
  matrix: ConclusionMatrix;
  feedback: string;
  /** 每行一条，提交时拆成数组。 */
  mustFix: string;
  suggestions: string;
  nextSteps: string;
}

export function emptyReviewForm(rows: AcSnapshotRow[]): ReviewFormState {
  return {
    matrix: initialMatrix(rows),
    feedback: "",
    mustFix: "",
    suggestions: "",
    nextSteps: "",
  };
}

export function reviewFormFromReview(
  review: Review | null,
  rows: AcSnapshotRow[],
): ReviewFormState {
  if (review === null) return emptyReviewForm(rows);
  return {
    matrix: matrixFromConclusions(rows, review.conclusions),
    feedback: review.feedback ?? "",
    mustFix: itemsToLines(review.must_fix ?? []),
    suggestions: itemsToLines(review.suggestions ?? []),
    nextSteps: review.next_steps ?? "",
  };
}

/** 多行文本 → 列表：去掉空行与首尾空白。 */
export function linesToItems(text: string): string[] {
  return text
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line !== "");
}

export function itemsToLines(items: string[]): string {
  return items.join("\n");
}

/** 草稿按钮是否值得可点：矩阵之外没有任何内容且没改结论时不必发请求。 */
export function hasDraftContent(form: ReviewFormState): boolean {
  return (
    matrixList(form.matrix).some((row) => row.conclusion !== "") ||
    form.feedback.trim() !== "" ||
    linesToItems(form.mustFix).length > 0 ||
    linesToItems(form.suggestions).length > 0 ||
    form.nextSteps.trim() !== ""
  );
}

/** 与后端已保存草稿比较，用于「未保存的修改」提示。 */
export function isFormDirty(form: ReviewFormState, review: Review | null): boolean {
  if (review === null) return hasDraftContent(form);
  if (changedConclusions(form.matrix, review.conclusions).length > 0) return true;
  return (
    form.feedback !== (review.feedback ?? "") ||
    itemsToLines(linesToItems(form.mustFix)) !== itemsToLines(review.must_fix ?? []) ||
    itemsToLines(linesToItems(form.suggestions)) !==
      itemsToLines(review.suggestions ?? []) ||
    form.nextSteps !== (review.next_steps ?? "")
  );
}

export type ReviewPayload = {
  conclusions: ReturnType<typeof conclusionPayload>;
  feedback: string;
  must_fix: string[];
  suggestions: string[];
  next_steps: string;
};

export function reviewPayload(form: ReviewFormState): ReviewPayload {
  return {
    conclusions: conclusionPayload(form.matrix),
    feedback: form.feedback,
    must_fix: linesToItems(form.mustFix),
    suggestions: linesToItems(form.suggestions),
    next_steps: form.nextSteps,
  };
}

// ---------------------------------------------------------------------------
// 发布前校验（镜像后端 publish_review，§7.3、§15.3）
// ---------------------------------------------------------------------------

/**
 * 返回阻塞发布的说明；空数组表示可以发布。
 *
 * 后端规则：必选 AC 在 `approved` 时必须有结论；`revision_required` 至少一条
 * must_fix；`rejected`/`paused` 必须填原因；`not_applicable` 必须填理由。
 * 本面板额外要求「选了结论就要写理由」，让逐条结论可追溯。
 */
export function validatePublish(
  form: ReviewFormState,
  rows: AcSnapshotRow[],
  decision: ReviewDecision,
  reason: string,
): string[] {
  const problems: string[] = [];

  if (!decision) problems.push("请先选择整体决定。");

  const missingReason = matrixList(form.matrix)
    .filter((row) => row.conclusion !== "" && row.reason.trim() === "")
    .map((row) => row.ac_code);
  if (missingReason.length > 0) {
    problems.push(`以下验收标准已选结论但没写理由：${missingReason.join("、")}。`);
  }

  if (decision === "approved") {
    const missing = rows
      .filter((row) => row.required)
      .filter((row) => form.matrix[row.ac_code]?.conclusion === "")
      .map((row) => row.ac_code);
    if (missing.length > 0) {
      problems.push(`确认通过前，所有必选验收标准都要有导师结论；缺少：${missing.join("、")}。`);
    }
  }

  if (decision === "revision_required" && linesToItems(form.mustFix).length === 0) {
    problems.push("要求修改时必须至少填写一条必须修改项。");
  }

  if ((decision === "rejected" || decision === "paused") && reason.trim() === "") {
    problems.push("拒绝或暂缓审核必须填写决定原因。");
  }

  return problems;
}

/** 矩阵里已选结论的行数（用于「已填 X / 共 Y 条」）。 */
export function concludedCount(matrix: ConclusionMatrix): number {
  return matrixList(matrix).filter((row) => row.conclusion !== "").length;
}
