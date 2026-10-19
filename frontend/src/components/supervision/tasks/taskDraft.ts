import type { Task, TaskInput } from "@/api/supervision";

/**
 * 任务草稿的纯逻辑：AC 行的增删改、材料行、草稿校验、提交体拼装。
 *
 * 这里刻意不碰 React，也不碰接口调用，方便单测钉住几条后端硬约束
 * （`AC_STATEMENT_MIN_CHARS`、ac_code 唯一、check_type / priority 取值），
 * 免得这些规则只在 400 响应里才被发现。
 *
 * 注意：任务在页面上的状态标签一律用后端返回的 `label`（§5.6），
 * 本模块只提供 priority / check_type 这类单值枚举的中文对照，不拼混合状态。
 */

/** 与后端 `AC_STATEMENT_MIN_CHARS` 一致：禁止"完成算法""效果较好"这类标准。 */
export const AC_STATEMENT_MIN_CHARS = 8;

/** 材料 key 会和附件的 kind 比对（§6.4 完整性检查），所以给出常用取值提示。 */
export const MATERIAL_KEY_HINT =
  "材料标识要和附件的 kind 对应，常用取值：code、diff、archive、log、result、report、other。";

export const PRIORITY_OPTIONS = [
  { value: "low", label: "低" },
  { value: "normal", label: "中" },
  { value: "high", label: "高" },
] as const;

export const CHECK_TYPE_OPTIONS = [
  { value: "document", label: "文档" },
  { value: "code", label: "代码" },
  { value: "experiment", label: "实验" },
  { value: "analysis", label: "分析" },
  { value: "runtime", label: "运行" },
] as const;

export const SUBMISSION_STATUS_LABEL: Record<string, string> = {
  submitted: "已提交",
  withdrawn: "已撤回",
  superseded: "已被新版本替代",
};

export const REVIEW_STATUS_LABEL: Record<string, string> = {
  not_started: "未开始",
  drafting: "审核草稿中",
  published: "已发布",
};

export const REVIEW_DECISION_LABEL: Record<string, string> = {
  approved: "确认通过",
  revision_required: "要求修改",
  rejected: "不接受",
  paused: "暂缓审核",
};

export const AC_CONCLUSION_LABEL: Record<string, string> = {
  met: "满足",
  partially_met: "部分满足",
  not_met: "不满足",
  more_evidence_required: "要求补充材料",
  not_applicable: "不适用",
};

export const SELF_ASSESSMENT_LABEL: Record<string, string> = {
  done: "已完成",
  partially_done: "部分完成",
  not_done: "未完成",
  not_applicable: "不适用",
};

export type TaskFilterKey =
  | "all"
  | "active"
  | "draft"
  | "completed"
  | "cancelled"
  | "archived";

/** 列表过滤按 `status` 原始值走；页面上那个混合标签只看后端 `label`。 */
export const TASK_FILTERS: { key: TaskFilterKey; label: string }[] = [
  { key: "all", label: "全部" },
  { key: "active", label: "进行中" },
  { key: "draft", label: "草稿" },
  { key: "completed", label: "已完成" },
  { key: "cancelled", label: "已取消" },
  { key: "archived", label: "已归档" },
];

export function filterTasks(tasks: Task[], filter: TaskFilterKey): Task[] {
  if (filter === "all") return tasks;
  return tasks.filter((task) => task.status === filter);
}

export function priorityLabel(value: string): string {
  return PRIORITY_OPTIONS.find((item) => item.value === value)?.label ?? value;
}

export function checkTypeLabel(value: string): string {
  return CHECK_TYPE_OPTIONS.find((item) => item.value === value)?.label ?? value;
}

/** 后端给的是 `datetime.isoformat()`（无时区），界面只显示到分钟。 */
export function formatDateTime(value?: string | null): string {
  if (!value) return "";
  return value.replace("T", " ").slice(0, 16);
}

/** `datetime-local` 输入框要 "YYYY-MM-DDTHH:mm"。 */
export function dueInputValue(value?: string | null): string {
  if (!value) return "";
  return value.slice(0, 16);
}

export function isOverdue(dueAt: string | null | undefined, now: Date): boolean {
  if (!dueAt) return false;
  const due = new Date(dueAt);
  return !Number.isNaN(due.getTime()) && due.getTime() < now.getTime();
}

// ---------------------------------------------------------------------------
// 草稿形状与行操作
// ---------------------------------------------------------------------------

export interface AcDraft {
  /** 表单内的稳定行标识；与后端 ac_code 无关，新建行用 nextKey 生成。 */
  key: string;
  ac_code: string;
  statement: string;
  evidence_requirement: string;
  required: boolean;
  check_type: string;
}

export interface MaterialDraft {
  /** 表单内的稳定行标识；改材料标识本身不会让 React 重建输入框 */
  rowId: string;
  /** 提交给后端的材料标识，完整性检查按它比对附件 kind */
  key: string;
  label: string;
  required: boolean;
}

export interface TaskDraft {
  title: string;
  purpose: string;
  description: string;
  /** `datetime-local` 取值；"" 表示不设截止时间 */
  due_at: string;
  priority: string;
  requires_code_review: boolean;
  requires_runtime: boolean;
  /** "" 表示不关联阶段目标 */
  milestone_id: string;
  acs: AcDraft[];
  materials: MaterialDraft[];
}

/** 生成表单内行标识：取同前缀已有最大序号 + 1，保证可重放、可单测。 */
function nextKey(rows: { key: string }[], prefix: string): string {
  let max = 0;
  for (const row of rows) {
    const matched = new RegExp(`^${prefix}-(\\d+)$`).exec(row.key);
    if (matched) max = Math.max(max, Number(matched[1]));
  }
  return `${prefix}-${max + 1}`;
}

/** 后端按序号补 ac_code，但同一版本内 ac_code 唯一，所以前端先把编号排好。 */
export function nextAcCode(rows: { ac_code: string }[]): string {
  let max = 0;
  for (const row of rows) {
    const matched = /^AC-(\d+)$/.exec((row.ac_code ?? "").trim());
    if (matched) max = Math.max(max, Number(matched[1]));
  }
  return `AC-${max + 1}`;
}

export function emptyAcRow(rows: { ac_code: string; key?: string }[]): AcDraft {
  return {
    key: nextKey(rows.map((row) => ({ key: row.key ?? "" })), "ac"),
    ac_code: nextAcCode(rows),
    statement: "",
    evidence_requirement: "",
    required: true,
    check_type: "document",
  };
}

export function emptyTaskDraft(): TaskDraft {
  const first = emptyAcRow([]);
  return {
    title: "",
    purpose: "",
    description: "",
    due_at: "",
    priority: "normal",
    requires_code_review: false,
    requires_runtime: false,
    milestone_id: "",
    acs: [first],
    materials: [],
  };
}

/** 编辑已有任务：取当前版本的验收标准，ac_code 带回去才不会在后端生成新编号。 */
export function draftFromTask(task: Task): TaskDraft {
  const acs: AcDraft[] = task.acs.map((ac) => ({
    key: `ac-${ac.ac_code}`,
    ac_code: ac.ac_code,
    statement: ac.statement,
    evidence_requirement: ac.evidence_requirement ?? "",
    required: ac.required,
    check_type: ac.check_type,
  }));
  return {
    title: task.title,
    purpose: task.purpose,
    description: task.description ?? "",
    due_at: dueInputValue(task.due_at),
    priority: task.priority,
    requires_code_review: task.requires_code_review,
    requires_runtime: task.requires_runtime,
    milestone_id: task.milestone_id ?? "",
    acs: acs.length > 0 ? acs : [emptyAcRow([])],
    materials: (task.required_materials ?? []).map((material, index) => ({
      rowId: `mat-${index + 1}`,
      key: material.key ?? `M-${index + 1}`,
      label: material.label ?? "",
      required: material.required ?? true,
    })),
  };
}

export function addAcRow(draft: TaskDraft): TaskDraft {
  return { ...draft, acs: [...draft.acs, emptyAcRow(draft.acs)] };
}

export function updateAcRow(
  draft: TaskDraft,
  key: string,
  patch: Partial<Omit<AcDraft, "key">>,
): TaskDraft {
  return {
    ...draft,
    acs: draft.acs.map((row) => (row.key === key ? { ...row, ...patch } : row)),
  };
}

export function removeAcRow(draft: TaskDraft, key: string): TaskDraft {
  return { ...draft, acs: draft.acs.filter((row) => row.key !== key) };
}

export function addMaterialRow(draft: TaskDraft): TaskDraft {
  const rowIds = draft.materials.map((row) => ({ key: row.rowId }));
  const keys = draft.materials.map((row) => ({ key: row.key }));
  return {
    ...draft,
    materials: [
      ...draft.materials,
      { rowId: nextKey(rowIds, "mat"), key: nextKey(keys, "M"), label: "", required: true },
    ],
  };
}

/** 按 `rowId` 定位行；patch 里的 key 改的是材料标识本身。 */
export function updateMaterialRow(
  draft: TaskDraft,
  rowId: string,
  patch: Partial<Omit<MaterialDraft, "rowId">>,
): TaskDraft {
  return {
    ...draft,
    materials: draft.materials.map((row) =>
      row.rowId === rowId ? { ...row, ...patch } : row,
    ),
  };
}

export function removeMaterialRow(draft: TaskDraft, rowId: string): TaskDraft {
  return { ...draft, materials: draft.materials.filter((row) => row.rowId !== rowId) };
}

// ---------------------------------------------------------------------------
// 校验与提交体
// ---------------------------------------------------------------------------

/** 返回中文错误列表；空列表表示可以提交。发布与存草稿用同一套最低门槛。 */
export function validateTaskDraft(draft: TaskDraft): string[] {
  const errors: string[] = [];

  if (!draft.title.trim()) errors.push("任务必须有标题。");
  if (!draft.purpose.trim()) errors.push("任务必须写明为什么要做（任务目的）。");

  if (draft.acs.length === 0) {
    errors.push("至少要有一条验收标准。");
  }
  const seen = new Set<string>();
  draft.acs.forEach((row, index) => {
    const statement = row.statement.trim();
    if (statement.length < AC_STATEMENT_MIN_CHARS) {
      errors.push(
        `第 ${index + 1} 条验收标准过于笼统，要写成可核验的条件` +
          `（至少 ${AC_STATEMENT_MIN_CHARS} 个字，例如"固定随机种子下中位定位误差小于 1 m"）。`,
      );
    }
    const code = (row.ac_code ?? "").trim();
    if (!code) {
      errors.push(`第 ${index + 1} 条验收标准缺少编号。`);
    } else if (seen.has(code)) {
      errors.push(`验收标准编号重复：${code}。`);
    } else {
      seen.add(code);
    }
  });

  const materialKeys = new Set<string>();
  draft.materials.forEach((row, index) => {
    const key = row.key.trim();
    if (!key) {
      errors.push(`第 ${index + 1} 行提交材料缺少材料标识。`);
    } else if (key.length > 32) {
      errors.push(`第 ${index + 1} 行提交材料的标识不能超过 32 个字符。`);
    } else if (materialKeys.has(key)) {
      errors.push(`提交材料标识重复：${key}。`);
    } else {
      materialKeys.add(key);
    }
  });

  return errors;
}

export function buildTaskInput(draft: TaskDraft): TaskInput {
  return {
    title: draft.title.trim(),
    purpose: draft.purpose.trim(),
    description: draft.description.trim(),
    acs: draft.acs.map((row) => ({
      ac_code: row.ac_code.trim(),
      statement: row.statement.trim(),
      evidence_requirement: row.evidence_requirement.trim(),
      required: row.required,
      check_type: row.check_type,
    })),
    required_materials: draft.materials.map((row) => ({
      key: row.key.trim(),
      label: row.label.trim(),
      required: row.required,
    })),
    due_at: draft.due_at ? draft.due_at : null,
    priority: draft.priority,
    requires_code_review: draft.requires_code_review,
    requires_runtime: draft.requires_runtime,
    milestone_id: draft.milestone_id || undefined,
  };
}

/**
 * 后端只在验收标准真的变化时升任务版本（§7.2）。用它决定要不要弹出
 * "学生看到的是新版本"的提示，避免对只改错别字的改动也吓唬导师。
 */
export function acRowsChanged(task: Task, draft: TaskDraft): boolean {
  const current = task.acs.map((ac) =>
    JSON.stringify([
      ac.ac_code,
      ac.statement.trim(),
      (ac.evidence_requirement ?? "").trim(),
      ac.required,
      ac.check_type,
    ]),
  );
  const next = draft.acs.map((ac) =>
    JSON.stringify([
      ac.ac_code.trim(),
      ac.statement.trim(),
      ac.evidence_requirement.trim(),
      ac.required,
      ac.check_type,
    ]),
  );
  return JSON.stringify(current) !== JSON.stringify(next);
}

/** 列表行上的截止时间提示文案；已结束的终态任务不标"已逾期"。 */
export function dueLabel(task: Task, now: Date): string {
  if (!task.due_at) return "无截止时间";
  const text = formatDateTime(task.due_at);
  const terminal =
    task.status === "completed" || task.status === "cancelled" || task.status === "archived";
  return terminal ? text : `${text}${isOverdue(task.due_at, now) ? " · 已逾期" : ""}`;
}
