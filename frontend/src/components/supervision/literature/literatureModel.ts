import type {
  LiteratureConclusion,
  LiteratureResult,
  LiteratureSubtask,
  LiteratureTask,
  ProjectDetail,
} from "@/api/supervision";
import { ApiError } from "@/lib/errors";

/**
 * 论文检索面板的纯逻辑（可单测，不碰 DOM、不发请求）。
 *
 * 权限判断放这里只为决定按钮与提示；真正的裁决在后端（导师调用学生专属接口
 * 一律 403），不能把它当安全边界。
 */

export const TASK_STATUS_LABEL: Record<string, string> = {
  draft: "草稿",
  pending: "待检索",
  searching: "检索中",
  triage: "筛选中",
  concluded: "已出结论",
  closed: "已关闭",
};

export const PURPOSE_LABEL: Record<string, string> = {
  verify: "验证已有判断",
  supplement: "补充依据",
  refute: "找反例",
  explore: "探索方向",
};

export const RELEVANCE_LABEL: Record<string, string> = {
  high: "高度相关",
  relevant: "相关",
  reference: "可参考",
  low: "相关性低",
};

export const DECISION_LABEL: Record<string, string> = {
  pending: "未决定",
  included: "已纳入",
  excluded: "已排除",
  read_later: "稍后阅读",
};

export const ACTION_LABEL: Record<string, string> = {
  priority_read: "建议优先读",
  reference: "可作参考",
  low_relevant: "相关性低",
};

export const FULLTEXT_LABEL: Record<string, string> = {
  unknown: "全文可用性未知",
  abstract: "只有摘要",
  url: "有链接",
  pdf: "有 PDF",
};

export const EXTRACT_STATE_LABEL: Record<string, string> = {
  none: "未深度提取",
  queued: "排队中",
  running: "提取中",
  completed: "已提取",
  failed: "提取失败",
  cancelled: "已取消",
};

export const SUBTASK_KIND_LABEL: Record<string, string> = {
  plan: "检索方案",
  search: "检索",
  rank: "排序",
  abstract_analysis: "摘要关联分析",
  deep_extract: "深度提取",
  compare: "多篇比较",
  conclusion: "结论快照",
};

export const SUBTASK_STATUS_LABEL: Record<string, string> = {
  pending: "待执行",
  queued: "排队中",
  running: "执行中",
  completed: "完成",
  failed: "失败",
  cancelled: "已取消",
};

export const CONCLUSION_STATUS_LABEL: Record<string, string> = {
  draft: "草稿",
  confirmed: "已确认",
  superseded: "已被新版本取代",
};

export const MENTOR_STATE_LABEL: Record<string, string> = {
  "": "导师未表态",
  confirmed: "导师已确认",
  questioned: "导师提出质疑",
  evidence_required: "导师要求补充证据",
};

export const SUPPORT_RELATION_LABEL: Record<string, string> = {
  "": "未标注支撑关系",
  supports: "支持",
  partially_supports: "部分支持",
  contradicts: "反驳",
  conflicting: "存在冲突",
  background: "背景材料",
};

export function labelOf(table: Record<string, string>, value: string): string {
  return table[value] ?? value;
}

/** 决定纳入/排除/稍后阅读都必须写理由（后端 reason 必填）。 */
export const DECISION_OPTIONS = [
  { value: "included", label: "纳入" },
  { value: "excluded", label: "排除" },
  { value: "read_later", label: "稍后阅读" },
];

/** 只有学生能做决定、建卡、确认结论；导师调用这些接口是 403。 */
export function isStudent(project: ProjectDetail): boolean {
  return project.my_role === "student";
}

/** 检索方案确认下来之前不能开始搜索（后端 409）。 */
export function planConfirmed(task: LiteratureTask): boolean {
  return Boolean(task.search_plan_confirmed_at);
}

export function hasSearchPlan(task: LiteratureTask): boolean {
  return Object.keys(task.search_plan ?? {}).length > 0;
}

export function taskClosed(task: LiteratureTask): boolean {
  return task.status === "closed";
}

/** 能不能点「开始检索」：方案已确认、任务没关闭。 */
export function canSearch(task: LiteratureTask): { allowed: boolean; reason: string } {
  if (taskClosed(task)) return { allowed: false, reason: "任务已关闭，需要新建一个检索任务" };
  if (!hasSearchPlan(task))
    return { allowed: false, reason: "还没有检索方案：先生成并确认检索方案" };
  if (!planConfirmed(task))
    return { allowed: false, reason: "检索方案还没经你确认：确认后才会真的去检索" };
  return { allowed: true, reason: "" };
}

/** 能不能改检索输入：只有草稿/待检索阶段可以（依据必须固定，后端同样裁决）。 */
export function canEditTask(task: LiteratureTask): boolean {
  return task.status === "draft" || task.status === "pending";
}

/** 任务一行摘要：命中 → 去重 → 入库，以及决定分布。 */
export function taskSummaryLine(task: LiteratureTask): string {
  return (
    `命中 ${task.hits_total} · 去重后 ${task.deduped_total} · 入库 ${task.stored_total}` +
    ` · 纳入 ${task.included_total} / 排除 ${task.excluded_total} / 稍后 ${task.read_later_total}` +
    ` / 未决定 ${task.undecided_total}`
  );
}

/** 依据的研究定义版本已被更新版取代时的提示文案。 */
export function briefSupersededNote(task: LiteratureTask): string {
  if (!task.brief_superseded) return "";
  return (
    `本任务依据的是 ${task.brief_code} v${task.brief_version}，` +
    `而该研究定义已有更新版本（当前共享 v${task.brief_shared_version ?? "?"} / 最新 v${task.brief_latest_version ?? "?"}）。` +
    "检索结果与结论仍按当时的依据解释；要按新依据重做请新建检索任务。"
  );
}

/** 来源失败（来源未接入、超时等）逐条列出来，不隐藏检索过程。 */
export function sourceFailureLines(task: LiteratureTask): string[] {
  return task.source_failures
    .map((item) => {
      const source = String(item.source ?? "").trim();
      const message = String(item.message ?? item.error ?? "").trim();
      const code = String(item.code ?? "").trim();
      const tail = message || code;
      // 既没说来源也没说原因的记录没有信息量，显示出来只会多一行空话。
      if (!source && !tail) return "";
      const head = source || "某个来源";
      return tail ? `${head}：${tail}` : head;
    })
    .filter((line) => line !== "");
}

/** 子任务能不能重试：只有失败或已取消的可以。 */
export function canRetrySubtask(subtask: LiteratureSubtask): boolean {
  return subtask.status === "failed" || subtask.status === "cancelled";
}

/** 子任务能不能取消：排队中与执行中才可以（执行是同步的，取消是给未来队列用的）。 */
export function canCancelSubtask(subtask: LiteratureSubtask): boolean {
  return subtask.status === "queued" || subtask.status === "running";
}

export function subtaskLine(subtask: LiteratureSubtask): string {
  const counts =
    subtask.kind === "search"
      ? ` · 命中 ${subtask.result_total}，新增 ${subtask.new_total}，重复 ${subtask.duplicate_total}`
      : "";
  const error = subtask.error_message ? ` · ${subtask.error_message}` : "";
  return `${labelOf(SUBTASK_KIND_LABEL, subtask.kind)} · ${labelOf(
    SUBTASK_STATUS_LABEL,
    subtask.status,
  )}（第 ${subtask.attempt} 次）${subtask.source ? ` · 来源 ${subtask.source}` : ""}${counts}${error}`;
}

/** 关联等级的可解释理由：命中了什么、缺了什么。 */
export function relevanceReasonLines(result: LiteratureResult): string[] {
  const lines: string[] = [];
  if (result.relevance_reason) lines.push(`规则判定：${result.relevance_reason}`);
  const matched = result.relevance_matched
    .map((item) => String(item.term ?? item.keyword ?? item.name ?? ""))
    .filter((term) => term !== "");
  const missing = result.relevance_missing
    .map((item) => String(item.term ?? item.keyword ?? item.name ?? ""))
    .filter((term) => term !== "");
  if (matched.length > 0) lines.push(`命中关键词：${matched.join("、")}`);
  if (missing.length > 0) lines.push(`缺少要素：${missing.join("、")}`);
  lines.push(`全文可用性：${labelOf(FULLTEXT_LABEL, result.fulltext_status)}`);
  lines.push(`建议：${labelOf(ACTION_LABEL, result.recommended_action)}`);
  return lines;
}

/** 结论依据已变的说明（文献快照哈希对不上当前纳入集合）。 */
export function conclusionStalenessNote(conclusion: LiteratureConclusion): string {
  if (!conclusion.literature_changed) return "";
  return (
    `结论绑定的文献集合是 ${conclusion.literature_count} 篇，` +
    `当前纳入集合是 ${conclusion.current_included_count} 篇：依据已经变化，` +
    "需要修订生成新版本（旧版本保留为历史）。"
  );
}

/** 503 在论文检索里是"外部来源/模型能力尚未接入"，不是失败。 */
export function isCapabilityUnavailable(error: unknown): boolean {
  return error instanceof ApiError && error.status === 503;
}

export const SOURCE_UNAVAILABLE_TEXT =
  "外部论文来源尚未接入：检索任务、检索方案与已入库的候选都保留着，" +
  "接入来源后点「开始检索」即可，不需要重建任务。";

export const MODEL_UNAVAILABLE_TEXT =
  "模型能力尚未接入：检索方案、摘要关联分析与深度提取暂时做不了，" +
  "但检索、决定纳入/排除与证据卡片都不受影响。";
