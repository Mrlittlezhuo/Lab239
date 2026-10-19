import { ApiError } from "@/lib/errors";

import type {
  AiDraftBody,
  AiMentorDiff,
  AiReviewFinding,
  AiReviewRun,
  AiStaleness,
  AiSuggestion,
  SelfAssessment,
  TaskAc,
} from "@/api/supervision";

/**
 * AI 预审面板的纯逻辑（《AI预审功能详细设计方案》§11、§12.3、§13、§14、§15）。
 *
 * 三条必须守住的口径：
 * 1. 创建类接口只排队，执行是独立调用；模型未接入时 execute 返回 503，
 *    那是「能力尚未接入」而不是失败（§14.2）；
 * 2. 时效（stale）不是运行状态，是结果相对当前草稿/当前提交的标记（§11.1）；
 * 3. AI 状态与导师结论是两个域：只有导师逐条 adopt 才写进导师结论（§15.2）。
 */

// ---------------------------------------------------------------------------
// 简单枚举翻译。§5.6 的派生标签与 `stale_label`、`summary_state`、
// `suggested_label`、`queue_status.note` 都由后端现算，页面直接用，不在此重复。
// ---------------------------------------------------------------------------

export const AI_STATUS_LABEL: Record<string, string> = {
  queued: "排队中",
  running: "运行中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

export const TRIGGER_SOURCE_LABEL: Record<string, string> = {
  student_manual: "学生手动自检",
  mentor_manual: "导师手动预审",
  auto_on_submission: "提交后自动预审",
};

export const VERDICT_LABEL: Record<string, string> = {
  satisfied: "满足",
  partial: "部分满足",
  not_satisfied: "不满足",
  unverifiable: "无法验证",
};

/** 证据强度（§8.3）。E4 本期不提供。 */
export const EVIDENCE_DEPTH_LABEL: Record<string, string> = {
  "": "未给出证据等级",
  E0: "E0 自述",
  E1: "E1 文档证据",
  E2: "E2 静态实现证据",
  E3: "E3 运行产物",
  E4: "E4 受控复现（本期不提供）",
};

/** 支持关系是与判断状态正交的另一条轴（§8.4）。 */
export const SUPPORT_RELATION_LABEL: Record<string, string> = {
  "": "未给出支持关系",
  no_evidence: "没有可读材料涉及该项",
  supports: "材料支持该标准",
  partially_supports: "只覆盖部分条件",
  contradicts: "存在明确反证",
  conflicting: "材料之间互相冲突",
  self_report_only: "只有学生自述",
};

// ---------------------------------------------------------------------------
// 503：模型能力尚未接入
// ---------------------------------------------------------------------------

export const MODEL_UNAVAILABLE_TEXT =
  "模型能力尚未接入，预审已排队；导师审核不受影响";

/** 503 不是失败：预审仍在队列里，导师照常人工审核（§14.2、P-11）。 */
export function isModelUnavailable(error: unknown): boolean {
  return error instanceof ApiError && error.status === 503;
}

// ---------------------------------------------------------------------------
// 运行上的可用动作（§12.3、§13.1、§13.3、§14.2）
// ---------------------------------------------------------------------------

export interface RunActions {
  /** 排队中的运行需要显式执行。 */
  execute: boolean;
  /** 只有排队中/运行中可以取消。 */
  cancel: boolean;
  /** 失败、取消或已过时 → 重新运行会生成新版本，不覆盖旧结果。 */
  retry: boolean;
  /** 只有学生本人未完成/未附带且未过时的自检可以附到提交。 */
  attach: boolean;
  /** 采纳建议只对完成且绑定正式提交的运行开放（自检不是导师审核依据）。 */
  adopt: boolean;
}

export function runActions(
  run: AiReviewRun,
  staleness?: AiStaleness | null,
): RunActions {
  const stale = staleness ? staleness.stale : run.stale;
  const attachAllowed = staleness ? staleness.attach_allowed : run.attach_allowed;
  return {
    execute: run.status === "queued",
    cancel: run.status === "queued" || run.status === "running",
    retry:
      run.status === "failed" ||
      run.status === "cancelled" ||
      (run.status === "completed" && stale),
    attach:
      run.trigger_source === "student_manual" &&
      run.status === "completed" &&
      !run.attached_to_submission &&
      !stale &&
      attachAllowed,
    adopt: run.status === "completed" && Boolean(run.submission_id),
  };
}

/** 排队可见性（§12.3）：文案来自后端 `queue_status.note`，不承诺分钟数。 */
export function queueNote(run: AiReviewRun): string {
  const status = run.queue_status ?? {};
  const note = typeof status.note === "string" ? status.note : "";
  const ahead =
    status.has_queued_ahead === true
      ? `前方还有 ${String(status.queued_ahead ?? "")} 个预审请求在排队`
      : "";
  return [note, ahead].filter((text) => text !== "").join("；");
}

/**
 * P-08：旧提交版本的预审不能显示成新提交的当前结果。
 * `latestVersion` 是该任务当前最新有效提交版本号（0 表示还没有提交）。
 */
export function isRunCurrentForSubmission(
  run: AiReviewRun,
  latestVersion: number,
): boolean {
  if (!run.submission_id) return true;
  if (latestVersion <= 0) return true;
  return Number(run.submission_version || 0) >= latestVersion;
}

// ---------------------------------------------------------------------------
// 逐条 AI 结论（§10.1）
// ---------------------------------------------------------------------------

/** 后端这些字段是 unknown 形状（有的版本是字符串，有的是对象），统一成一行文字。 */
export function describeUnknown(value: unknown): string {
  if (value == null) return "";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) {
    return value.map((item) => describeUnknown(item)).filter((text) => text !== "").join("、");
  }
  if (typeof value === "object") {
    const row = value as Record<string, unknown>;
    const preferred = [
      "message",
      "name",
      "label",
      "reason",
      "note",
      "location",
      "attachment_code",
      "ac_code",
      "key",
    ];
    const hit = preferred
      .map((key) => (typeof row[key] === "string" ? (row[key] as string) : ""))
      .find((text) => text !== "");
    if (hit) return hit;
    return Object.entries(row)
      .map(([key, item]) => `${key}=${describeUnknown(item)}`)
      .filter((text) => !text.endsWith("="))
      .join(" ");
  }
  return "";
}

/**
 * 逐条结论上的安全与降级提示（§8.5、P-12、P-19）。
 * 措辞按方案 §10.3：静态材料不能证明已运行。
 */
export function findingNotes(finding: AiReviewFinding): string[] {
  const notes: string[] = [];
  if (finding.injection_suspected) {
    notes.push(
      `材料里出现了疑似指令文本，已按数据处理${finding.injection_note ? `：${finding.injection_note}` : ""}`,
    );
  }
  if (finding.runtime_claim_flag) {
    notes.push("材料里有「已运行/测试通过」类说法，但静态材料不能证明已运行");
  }
  if (finding.downgrade_reason) {
    notes.push(`结论已降级：${finding.downgrade_reason}`);
  }
  if (!finding.validated) {
    notes.push("该条不是模型返回的可信结构化结论，只作线索，不作为依据");
  }
  return notes;
}

export function findingEvidenceLines(finding: AiReviewFinding): string[] {
  return (finding.evidence_refs ?? [])
    .map((ref) => describeUnknown(ref))
    .filter((text) => text !== "");
}

export function findingMissingMaterialLines(finding: AiReviewFinding): string[] {
  return (finding.missing_materials ?? [])
    .map((item) => describeUnknown(item))
    .filter((text) => text !== "");
}

export function findingRiskLines(finding: AiReviewFinding): string[] {
  return (finding.risks ?? []).map((risk) => describeUnknown(risk)).filter((text) => text !== "");
}

// ---------------------------------------------------------------------------
// AI 建议 vs 导师结论（§15.2、P-22）
// ---------------------------------------------------------------------------

export interface AiMentorRow {
  ac_code: string;
  verdict: string;
  suggested_conclusion: string;
  suggested_label: string;
  ai_reason: string;
  note: string;
  /** 空串表示导师还没填这条结论。 */
  mentor_conclusion: string;
  ai_relation: string;
  /** 后端没给导师结论时为 null：还没得比。 */
  consistent: boolean | null;
}

/**
 * 把 AI 建议与 AI/导师差异按 ac_code 合成一张对照表。
 * 两个接口都可能缺行（例如导师还没写结论时 diff 里 mentor_conclusion 为空），
 * 所以取并集，不假设两边一致。
 */
export function mergeAiMentorRows(
  suggestions: AiSuggestion[],
  diff: AiMentorDiff[],
): AiMentorRow[] {
  const byCode = new Map<string, AiMentorRow>();
  for (const item of suggestions) {
    byCode.set(item.ac_code, {
      ac_code: item.ac_code,
      verdict: item.verdict,
      suggested_conclusion: item.suggested_conclusion,
      suggested_label: item.suggested_label,
      ai_reason: item.reason,
      note: item.note,
      mentor_conclusion: "",
      ai_relation: "none",
      consistent: null,
    });
  }
  for (const item of diff) {
    const row = byCode.get(item.ac_code);
    if (row === undefined) {
      byCode.set(item.ac_code, {
        ac_code: item.ac_code,
        verdict: item.verdict,
        suggested_conclusion: item.suggested_conclusion,
        suggested_label: "",
        ai_reason: "",
        note: "",
        mentor_conclusion: item.mentor_conclusion || "",
        ai_relation: item.ai_relation || "none",
        consistent: item.mentor_conclusion ? item.consistent : null,
      });
      continue;
    }
    row.mentor_conclusion = item.mentor_conclusion || "";
    row.ai_relation = item.ai_relation || "none";
    row.consistent = item.mentor_conclusion ? item.consistent : null;
  }
  return [...byCode.values()];
}

/** 采纳按钮只在导师结论还没落定或与建议不同时才有意义。 */
export function canAdoptRow(row: AiMentorRow, actions: RunActions): boolean {
  if (!actions.adopt) return false;
  return row.mentor_conclusion === "" || row.consistent === false;
}

// ---------------------------------------------------------------------------
// 学生草稿（自检与时效比对都要把当前草稿送去后端算指纹，§7.4）
// ---------------------------------------------------------------------------

export interface SelfAssessmentDraft {
  ac_code: string;
  self_assessment: SelfAssessment | "";
  evidence: string;
  note: string;
}

export interface SelfCheckDraft {
  summary: string;
  selfAssessment: Record<string, SelfAssessmentDraft>;
  git_repo: string;
  git_branch: string;
  git_commit: string;
  experiment_notes: string;
  known_issues: string;
  unfinished_items: string;
  attachmentCodes: string[];
  coreAttachmentCodes: string[];
}

/** 草稿按任务当前 AC 建行；自检针对的是草稿，不是提交快照。 */
export function emptySelfCheckDraft(acs: TaskAc[]): SelfCheckDraft {
  const selfAssessment: Record<string, SelfAssessmentDraft> = {};
  for (const ac of acs) {
    selfAssessment[ac.ac_code] = {
      ac_code: ac.ac_code,
      self_assessment: "",
      evidence: "",
      note: "",
    };
  }
  return {
    summary: "",
    selfAssessment,
    git_repo: "",
    git_branch: "",
    git_commit: "",
    experiment_notes: "",
    known_issues: "",
    unfinished_items: "",
    attachmentCodes: [],
    coreAttachmentCodes: [],
  };
}

/**
 * 组装 `AiDraftBody`。没选自评的行跳过（后端按提交快照校验取值合法性），
 * 核心材料只保留同时被勾选为证据的那些，避免出现「未关联却被当作核心材料」。
 */
export function buildDraftBody(draft: SelfCheckDraft): AiDraftBody {
  const selfAssessment = Object.values(draft.selfAssessment)
    .filter((row) => row.self_assessment !== "")
    .map((row) => ({
      ac_code: row.ac_code,
      self_assessment: row.self_assessment as SelfAssessment,
      evidence: row.evidence,
      note: row.note,
    }));
  const core = draft.coreAttachmentCodes.filter((code) =>
    draft.attachmentCodes.includes(code),
  );
  return {
    summary: draft.summary,
    self_assessment: selfAssessment,
    git_repo: draft.git_repo,
    git_branch: draft.git_branch,
    git_commit: draft.git_commit,
    experiment_notes: draft.experiment_notes,
    known_issues: draft.known_issues,
    unfinished_items: draft.unfinished_items,
    attachment_codes: draft.attachmentCodes,
    core_attachment_codes: core,
  };
}

/** 草稿里是否已经有内容（空草稿也能自检，但页面要提示意义不大）。 */
export function draftHasContent(draft: SelfCheckDraft): boolean {
  return (
    draft.summary.trim() !== "" ||
    draft.git_commit.trim() !== "" ||
    draft.experiment_notes.trim() !== "" ||
    draft.known_issues.trim() !== "" ||
    draft.unfinished_items.trim() !== "" ||
    draft.attachmentCodes.length > 0 ||
    Object.values(draft.selfAssessment).some((row) => row.self_assessment !== "")
  );
}
