import type {
  LatexDraftItem,
  LatexFinding,
  LatexProgressDraft,
  LatexSnapshot,
  LatexSourceLocation,
  ProjectDetail,
} from "@/api/supervision";
import { ApiError } from "@/lib/errors";

/**
 * LaTeX 稿件面板的纯逻辑（可单测，不碰 DOM、不发请求）。
 *
 * 刻意把"能不能确认/丢弃"这类判断放这里：后端才是权限的裁决者，这里的
 * 结果只用来决定按钮是否出现与提示文案，不能当成安全边界。
 */

// 草稿状态是简单枚举（不是 §5.6 的混合派生状态），所以前端做中文对照。
export const DRAFT_STATUS_LABEL: Record<string, string> = {
  draft: "草稿",
  baseline: "基线（首次上传）",
  confirmed: "已确认成进展",
  discarded: "已丢弃",
};

export const CHANGE_LABEL: Record<string, string> = {
  added: "新增",
  removed: "删除",
  modified: "修改",
  summary: "汇总",
};

export const SEVERITY_LABEL: Record<string, string> = {
  error: "错误",
  warning: "提醒",
  notice: "提示",
};

export const MAIN_STATUS_LABEL: Record<string, string> = {
  resolved: "已判定主文件",
  user_specified: "由你指定",
  ambiguous: "主文件不唯一",
  not_found: "没找到主文件",
  invalid_selection: "指定的主文件不可用",
};

export const ITEM_TYPE_LABEL: Record<string, string> = {
  section: "章节",
  figure: "图",
  table: "表",
  algorithm: "算法",
  equation: "公式",
  citation: "引用",
  bib_entry: "文献条目",
  image_file: "图片文件",
  word_count: "字数",
};

// 快照 counts 与草稿 diff counts 的中文对照（后端按这些键给计数）。
export const COUNT_LABEL: Record<string, string> = {
  sections: "章节",
  labels: "标签",
  references: "交叉引用",
  citations: "引用",
  equations: "公式",
  figures: "图",
  tables: "表",
  algorithms: "算法",
  graphics: "图片文件",
  bib_entries: "文献条目",
  word_count: "字数",
  added_sections: "新增章节",
  removed_sections: "删除章节",
  modified_sections: "修改章节",
  resource_changes: "资源变化",
};

export function labelOf(table: Record<string, string>, value: string): string {
  return table[value] ?? value;
}

/** 把 counts 字典折成"章节 6 · 图 3 · …"一行，未知键原样显示。 */
export function countsLine(counts: Record<string, number>): string {
  return Object.entries(counts ?? {})
    .map(([key, value]) => `${labelOf(COUNT_LABEL, key)} ${value}`)
    .join(" · ");
}

export function locationText(location: LatexSourceLocation): string {
  const range =
    location.end_line && location.end_line !== location.line
      ? `L${location.line}-L${location.end_line}`
      : `L${location.line}`;
  return `${location.path || "?"} ${range}`;
}

export function findingLine(finding: LatexFinding): string {
  const where = finding.path ? `（${locationText(finding)}）` : "";
  return `${labelOf(SEVERITY_LABEL, finding.severity)}：${finding.message}${where}`;
}

export function itemLine(item: LatexDraftItem): string {
  const where = item.locations.length
    ? `　${item.locations.map(locationText).join("、")}`
    : "";
  return `${labelOf(ITEM_TYPE_LABEL, item.item_type)} · ${labelOf(
    CHANGE_LABEL,
    item.change,
  )}：${item.statement}${where}`;
}

/** 主文件没判定下来时必须让学生选一个，否则差异结果没有意义。 */
export function needsMainFileChoice(snapshot: LatexSnapshot): boolean {
  return ["ambiguous", "not_found", "invalid_selection"].includes(snapshot.main_status);
}

/** 候选主文件的可读列表（后端 ambiguous 时给出）。 */
export function candidatePaths(snapshot: LatexSnapshot): string[] {
  return snapshot.candidates
    .map((item) => String(item.path ?? item.name ?? ""))
    .filter((path) => path !== "");
}

/** 草稿列表里的一行摘要：章节增删改 + 字数变化。 */
export function draftSummaryLine(draft: LatexProgressDraft): string {
  const counts = draft.counts ?? {};
  const parts = [
    `章节 +${counts.added_sections ?? 0} / −${counts.removed_sections ?? 0} / 改${counts.modified_sections ?? 0}`,
    `资源 ${counts.resource_changes ?? 0}`,
  ];
  if (draft.word_count_delta) {
    parts.push(`字数 ${draft.word_count_delta > 0 ? "+" : ""}${draft.word_count_delta}`);
  }
  return parts.join(" · ");
}

/**
 * 能否把草稿转成正式进展：只有创建者本人、且项目角色是学生。
 * 后端同样这样裁决（导师调用是 403），这里只是决定按钮与提示。
 */
export function canConfirmDraft(
  draft: LatexProgressDraft,
  project: ProjectDetail,
  userId: string,
): { allowed: boolean; reason: string } {
  if (draft.status === "confirmed") return { allowed: false, reason: "已确认过，重复确认会被后端拒绝" };
  if (draft.status === "discarded") return { allowed: false, reason: "草稿已丢弃，需要重新生成" };
  if (project.my_role !== "student")
    return { allowed: false, reason: "只有学生本人能把稿件差异确认为自己的进展" };
  if (draft.created_by !== userId)
    return { allowed: false, reason: "只能确认自己生成的草稿" };
  return { allowed: true, reason: "" };
}

export function canDiscardDraft(
  draft: LatexProgressDraft,
  project: ProjectDetail,
  userId: string,
): { allowed: boolean; reason: string } {
  if (draft.status === "confirmed") return { allowed: false, reason: "已确认的草稿不能丢弃" };
  if (draft.status === "discarded") return { allowed: false, reason: "已经丢弃过了" };
  if (draft.created_by !== userId)
    return { allowed: false, reason: "只能丢弃自己生成的草稿" };
  if (project.my_role !== "student")
    return { allowed: false, reason: "导师只能查看，不能处置学生的草稿" };
  return { allowed: true, reason: "" };
}

export interface BlockedDetail {
  headline: string;
  lines: string[];
}

/**
 * 把 409/400 的结构化 detail 还原成可读说明。
 *
 * 后端在这两类情况下把 ``detail`` 发成对象（危险归档给 ``problems``，主文件
 * 有歧义给 ``candidates``），此时 ``ApiError.message`` 只剩"请求失败（HTTP
 * 409）"。必须从 detail 里取后端原话，否则用户只看到一个没解释的失败。
 */
export function describeBlocked(error: unknown): BlockedDetail | null {
  if (!(error instanceof ApiError)) return null;
  const detail = error.detail;
  if (!detail || typeof detail !== "object" || Array.isArray(detail)) return null;
  const record = detail as Record<string, unknown>;
  const headline = typeof record.message === "string" ? record.message : "";
  const lines: string[] = [];
  for (const item of asArray(record.problems)) lines.push(problemLine(item));
  const candidates = candidatePathsFrom(record.candidates);
  if (candidates.length > 0) lines.push(`候选主文件：${candidates.join("、")}`);
  if (!headline && lines.length === 0) return null;
  return { headline, lines };
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function problemLine(item: unknown): string {
  if (typeof item === "string") return item;
  if (!item || typeof item !== "object") return "";
  const record = item as Record<string, unknown>;
  const message = typeof record.message === "string" ? record.message : "";
  const entry = typeof record.entry === "string" ? record.entry : "";
  const code = typeof record.code === "string" ? record.code : "";
  const where = entry ? `（条目 ${entry}）` : code ? `（${code}）` : "";
  return `${message}${where}`;
}

function candidatePathsFrom(value: unknown): string[] {
  return asArray(value)
    .map((item) => {
      if (typeof item === "string") return item;
      if (!item || typeof item !== "object") return "";
      const record = item as Record<string, unknown>;
      return String(record.path ?? record.name ?? "");
    })
    .filter((path) => path !== "");
}

/** 快照里需要如实显示的问题（危险命令、未读取、部分读取）。 */
export function snapshotProblemLines(snapshot: LatexSnapshot): string[] {
  const lines = snapshot.dangerous_findings.map(findingLine);
  for (const item of snapshot.not_read) {
    const path = String(item.path ?? "");
    if (path) lines.push(`未读取：${path}`);
  }
  for (const item of snapshot.partially_read) {
    const path = String(item.path ?? "");
    if (path) lines.push(`只读取了一部分：${path}`);
  }
  return lines;
}
