import type {
  BriefComparison,
  BriefVersion,
  ResearchBrief,
} from "@/api/supervision";
import { ApiError } from "@/lib/errors";

/**
 * 结构化研究定义（ResearchBrief）的纯逻辑：字段清单、中文标签、草稿内容
 * 形态、脏字段检测、版本比较的展示分组与后端错误分类。
 *
 * 字段名与标签必须和后端 `BRIEF_FIELD_LABELS` 一致（版本差异里的 `label`
 * 也由后端按同一张表现算），否则界面上同一字段会出现两种叫法。
 */

export const BRIEF_TEXT_FIELDS = [
  "background",
  "question",
  "objectives",
  "subjects",
  "scope",
  "prior_attempts",
  "hypotheses",
  "constraints",
  "methods",
  "metrics",
  "expected_outcomes",
  "to_verify",
] as const;

export const BRIEF_LIST_FIELDS = ["keywords", "sub_questions"] as const;

export const BRIEF_CONTENT_FIELDS: readonly string[] = [
  ...BRIEF_TEXT_FIELDS,
  ...BRIEF_LIST_FIELDS,
];

export const BRIEF_FIELD_LABELS: Record<string, string> = {
  background: "研究背景与已知工作",
  question: "核心研究问题",
  objectives: "研究目标",
  subjects: "研究对象与场景",
  scope: "范围与边界",
  prior_attempts: "已经尝试过什么",
  hypotheses: "研究假设",
  constraints: "约束条件",
  methods: "候选方法",
  metrics: "评价指标",
  expected_outcomes: "预期成果",
  to_verify: "待验证判断与还不确定之处",
  keywords: "需要检索的关键词",
  sub_questions: "需要拆分的子问题",
};

/**
 * 状态标签只翻译后端返回值（§5.6）：前端不拼「待导师确认」之类的混合状态，
 * 也不根据版本号自己推断状态。
 */
export const BRIEF_STATUS_LABEL: Record<string, string> = {
  draft: "草稿",
  confirmed: "已确认",
  rejected: "已否定",
  superseded: "已被新版本替代",
};

export const BRIEF_ENTRY_TYPE_LABEL: Record<string, string> = {
  research_question: "研究问题",
  hypothesis: "研究假设",
  scope_constraint: "范围约束",
  to_verify_judgement: "待验证判断",
  candidate_method: "候选方法",
  evaluation_metric: "评价指标",
  mixed: "混合条目",
};

export const BRIEF_KIND_LABEL: Record<string, string> = {
  research_brief: "研究定义",
  mentor_guidance: "导师建议",
};

export const ROLE_LABEL: Record<string, string> = {
  student: "学生",
  mentor: "导师",
};

export function statusLabel(status: string): string {
  return BRIEF_STATUS_LABEL[status] ?? status;
}

export function entryTypeLabel(entryType: string): string {
  return BRIEF_ENTRY_TYPE_LABEL[entryType] ?? entryType;
}

export function kindLabel(kind: string): string {
  return BRIEF_KIND_LABEL[kind] ?? kind;
}

export function roleLabel(role: string): string {
  return ROLE_LABEL[role] ?? role;
}

export function fieldLabel(field: string): string {
  return BRIEF_FIELD_LABELS[field] ?? field;
}

export type BriefTextField = (typeof BRIEF_TEXT_FIELDS)[number];
export type BriefListField = (typeof BRIEF_LIST_FIELDS)[number];

/** 编辑器里的内容形态：文本卡片字段是字符串，列表字段是字符串数组。 */
export type BriefContentDraft = Record<BriefTextField, string> &
  Record<BriefListField, string[]>;

export function emptyBriefContent(): BriefContentDraft {
  const content = {} as BriefContentDraft;
  for (const field of BRIEF_TEXT_FIELDS) content[field] = "";
  for (const field of BRIEF_LIST_FIELDS) content[field] = [];
  return content;
}

export function contentFromBrief(brief: ResearchBrief): BriefContentDraft {
  const content = emptyBriefContent();
  for (const field of BRIEF_TEXT_FIELDS) content[field] = brief[field] ?? "";
  for (const field of BRIEF_LIST_FIELDS) content[field] = [...brief[field]];
  return content;
}

/**
 * 版本行（`BriefVersion`）的内容字段走的是索引签名，类型是 `unknown`；
 * 后端实际返回的仍是字符串/字符串数组，这里做一次收敛。
 */
export function contentFromVersion(row: BriefVersion): BriefContentDraft {
  const content = emptyBriefContent();
  for (const field of BRIEF_TEXT_FIELDS) {
    content[field] = asText(row[field]);
  }
  for (const field of BRIEF_LIST_FIELDS) {
    content[field] = asList(row[field]);
  }
  return content;
}

/** 编辑器与当前版本逐字段比对，用于「有未保存修改」提示与保存前的裁剪。 */
export function changedContentFields(
  before: BriefContentDraft,
  after: BriefContentDraft,
): string[] {
  const changed: string[] = [];
  for (const field of BRIEF_TEXT_FIELDS) {
    if ((before[field] ?? "").trim() !== (after[field] ?? "").trim()) changed.push(field);
  }
  for (const field of BRIEF_LIST_FIELDS) {
    const oldItems = before[field] ?? [];
    const newItems = after[field] ?? [];
    if (oldItems.length !== newItems.length || oldItems.some((x, i) => x !== newItems[i])) {
      changed.push(field);
    }
  }
  return changed;
}

export function isContentEmpty(content: BriefContentDraft): boolean {
  for (const field of BRIEF_TEXT_FIELDS) if (content[field].trim()) return false;
  for (const field of BRIEF_LIST_FIELDS) if (content[field].length > 0) return false;
  return true;
}

/** 列表字段在界面上是「一行一条」的文本框；送给后端前拆成数组。 */
export function textToList(text: string): string[] {
  const items: string[] = [];
  for (const raw of text.split(/[\n,，、;；]/)) {
    const item = raw.trim();
    if (item && !items.includes(item)) items.push(item);
  }
  return items;
}

export function listToText(items: string[]): string {
  return (items ?? []).join("\n");
}

/** 提交给 `createBrief` / `updateBrief` 的 `content` 载荷。 */
export function briefContentPayload(content: BriefContentDraft): Record<string, unknown> {
  const payload: Record<string, unknown> = {};
  for (const field of BRIEF_TEXT_FIELDS) payload[field] = content[field];
  for (const field of BRIEF_LIST_FIELDS) payload[field] = content[field];
  return payload;
}

/** 接口引用优先用内部 id：`code` 是 RB-001，跨版本稳定但 id 更保险。 */
export function briefRefOf(brief: ResearchBrief): string {
  return brief.brief_id || brief.code;
}

/**
 * 版本比较的默认选择：从「最近一次确认的那一版」比到「最新一版」，
 * 这样默认看到的差异正是「对方正在读的那一版 → 我现在这一版」
 * （`shared_version` 就是最近确认的版本）。
 */
export function versionChoices(versions: BriefVersion[]): { from: number; to: number } | null {
  if (!versions || versions.length < 2) return null;
  const numbers = versions.map((row) => Number(row.version)).filter((n) => Number.isFinite(n));
  if (numbers.length < 2) return null;
  const sorted = [...numbers].sort((a, b) => a - b);
  const to = sorted[sorted.length - 1];
  const confirmed = versions
    .filter((row) => row.status === "confirmed")
    .map((row) => Number(row.version))
    .filter((n) => Number.isFinite(n) && n < to)
    .sort((a, b) => a - b);
  const from = confirmed.length > 0 ? confirmed[confirmed.length - 1] : sorted[0];
  if (from === to) return null;
  return { from, to };
}

export interface ComparisonRow {
  field: string;
  label: string;
  /** 列表字段（关键词、子问题）按条目显示增删，文本字段按整段显示。 */
  kind: "text" | "list";
  fromText: string;
  toText: string;
  added: string[];
  removed: string[];
}

/**
 * 把 `compareBriefVersions` 的差异收敛成可渲染的行。
 *
 * `src/api/supervision.ts` 把 `changes` 声明成 `from: string; to: string;
 * added?: boolean; removed?: boolean`，但后端 `diff_brief_content` 对列表字段
 * 返回的是数组，并给出 `added`/`removed` 两个数组。这里按运行时实际形状处理，
 * 不改动接口文件。
 */
export function comparisonRows(comparison: BriefComparison): ComparisonRow[] {
  return (comparison?.changes ?? []).map((change) => {
    const raw = change as unknown as {
      field: string;
      label?: unknown;
      from?: unknown;
      to?: unknown;
      added?: unknown;
      removed?: unknown;
    };
    const field = String(raw.field ?? "");
    const looksLikeList =
      (BRIEF_LIST_FIELDS as readonly string[]).includes(field) ||
      Array.isArray(raw.from) ||
      Array.isArray(raw.to) ||
      Array.isArray(raw.added) ||
      Array.isArray(raw.removed);
    const label = typeof raw.label === "string" && raw.label ? raw.label : fieldLabel(field);
    if (!looksLikeList) {
      return {
        field,
        label,
        kind: "text" as const,
        fromText: asText(raw.from),
        toText: asText(raw.to),
        added: [],
        removed: [],
      };
    }
    const fromList = asList(raw.from);
    const toList = asList(raw.to);
    const added = Array.isArray(raw.added) ? asList(raw.added) : diffLists(toList, fromList);
    const removed = Array.isArray(raw.removed) ? asList(raw.removed) : diffLists(fromList, toList);
    return {
      field,
      label,
      kind: "list" as const,
      fromText: fromList.join("、"),
      toText: toList.join("、"),
      added,
      removed,
    };
  });
}

/** 差异按字段类型分组：文本卡片一组，列表条目一组（§16.3 第4条）。 */
export function splitComparisonRows(rows: ComparisonRow[]): {
  textRows: ComparisonRow[];
  listRows: ComparisonRow[];
} {
  return {
    textRows: rows.filter((row) => row.kind === "text"),
    listRows: rows.filter((row) => row.kind === "list"),
  };
}

function diffLists(from: string[], to: string[]): string[] {
  return from.filter((item) => !to.includes(item));
}

/**
 * 「已确认的定义不可原地修改」的界面说明：改内容会升版本，
 * 而 `shared_version` 仍指向对外生效的旧版，另一方还在读旧版。
 */
export function sharedVersionNote(brief: ResearchBrief): string | null {
  // 只对创建者说：另一方看不到 current_version 那一版，说「你当前的草稿」会误导。
  if (!brief.is_owner) return null;
  const shared = brief.shared_version;
  if (shared === null || shared === undefined) return null;
  if (Number(brief.current_version) === Number(shared)) return null;
  return (
    `已共享的是 v${shared}，你当前的草稿是 v${brief.current_version}。` +
    `另一方读到的仍然是 v${shared}，直到你确认新版本为止；` +
    "引用旧版本的检索任务与提交不会追溯改写。"
  );
}

/** 非所有者读到的永远是已共享的那一版，界面必须说清是哪一版。 */
export function shownVersionNote(brief: ResearchBrief): string | null {
  if (brief.is_owner) return null;
  if (brief.shared_version === null || brief.shared_version === undefined) return null;
  return `你看到的是对方已确认共享的 v${brief.shown_version}；对方之后的草稿与修改对你不可见。`;
}

/** 已确认后再编辑的预告：改动会生成新版本，而不是原地覆盖。 */
export function editWouldForkNote(brief: ResearchBrief): string | null {
  if (!brief.is_owner) return null;
  if (brief.status !== "confirmed") return null;
  return "这一版已确认共享。再改动内容不会原地覆盖，而是生成新版本并把状态退回草稿；已共享的版本继续对项目生效。";
}

export function canEditBrief(brief: ResearchBrief): boolean {
  return Boolean(brief.is_owner) && Boolean(brief.can_edit);
}

/** 只有草稿能确认共享；确认前必须有核心研究问题（后端同样校验）。 */
export function canConfirmBrief(brief: ResearchBrief): boolean {
  return Boolean(brief.is_owner) && brief.status === "draft";
}

export function confirmBlockedReason(brief: ResearchBrief): string | null {
  if (!brief.is_owner) return "只有创建者能确认自己的研究定义。";
  if (brief.status !== "draft") return `当前状态是「${statusLabel(brief.status)}」，只有草稿可以确认共享。`;
  if (!(brief.question ?? "").trim()) return "确认共享前必须填写核心研究问题。";
  return null;
}

/** 否定与丢弃只对未共享过的草稿开放；已共享的是项目审计资产。 */
export function canRejectBrief(brief: ResearchBrief): boolean {
  return (
    Boolean(brief.is_owner) &&
    (brief.shared_version === null || brief.shared_version === undefined) &&
    brief.status !== "rejected" &&
    brief.status !== "superseded"
  );
}

export function canDiscardBrief(brief: ResearchBrief): boolean {
  return (
    Boolean(brief.is_owner) &&
    (brief.shared_version === null || brief.shared_version === undefined) &&
    (brief.status === "draft" || brief.status === "rejected")
  );
}

export function sharedCannotRemoveNote(brief: ResearchBrief): string | null {
  if (brief.shared_version === null || brief.shared_version === undefined) return null;
  return "已确认共享的研究定义不能否定或丢弃：它是项目审计资产，只能出新版本替代。";
}

// ---------------------------------------------------------------------------
// 错误分类：后端用状态码表达语义，界面按状态码给说明
// ---------------------------------------------------------------------------

export function httpStatusOf(error: unknown): number | undefined {
  return error instanceof ApiError ? error.status : undefined;
}

/** 非成员、不存在、无权读一律 404（后端刻意不区分，避免枚举）。 */
export function isNotFound(error: unknown): boolean {
  return httpStatusOf(error) === 404;
}

export function isForbidden(error: unknown): boolean {
  return httpStatusOf(error) === 403;
}

/** 状态不允许（例如已共享的定义不能丢弃）后端返回 409。 */
export function isConflict(error: unknown): boolean {
  return httpStatusOf(error) === 409;
}

/** 模型能力尚未接入：503 不是失败，界面要显示成说明。 */
export function isModelUnavailable(error: unknown): boolean {
  return httpStatusOf(error) === 503;
}

export const MODEL_UNAVAILABLE_HINT =
  "模型能力尚未接入：从讨论里自动提取研究定义需要注入生成模型。" +
  "这不影响人工流程——你可以直接手动填写研究定义，照样能确认共享。";

// ---------------------------------------------------------------------------
// 小工具
// ---------------------------------------------------------------------------

export function formatTime(value?: string | null): string {
  if (!value) return "";
  return String(value).replace("T", " ").slice(0, 19);
}

export function asText(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (Array.isArray(value)) return value.map((item) => String(item)).join("、");
  return String(value);
}

export function asList(value: unknown): string[] {
  if (Array.isArray(value)) {
    return value.map((item) => String(item).trim()).filter((item) => item.length > 0);
  }
  if (typeof value === "string") return textToList(value);
  return [];
}
