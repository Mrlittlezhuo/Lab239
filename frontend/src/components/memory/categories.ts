import type {
  Memory,
  MemoryCategory,
  MemoryKind,
  MemoryStatus,
} from "@/api/memory";

/** 个人偏好类别，与服务端 `VALID_CATEGORIES` 一一对应。 */
export const MEMORY_CATEGORIES: {
  value: MemoryCategory;
  label: string;
  hint: string;
}[] = [
  {
    value: "answer_style",
    label: "回答偏好",
    hint: "输出语言、长度、表达结构。例：默认中文回答；先给结论再解释",
  },
  {
    value: "technical_background",
    label: "技术背景",
    hint: "长期有效、对回答质量有帮助的背景。例：Python 开发者、在用 LangGraph",
  },
  {
    value: "work_style",
    label: "工作偏好",
    hint: "稳定的协作习惯。例：改代码前先给方案；优先解释风险",
  },
  {
    value: "long_term_goal",
    label: "长期目标",
    hint: "希望助手长期理解的方向。例：正在开发 Agent 平台",
  },
];

/** 没有类别的记忆：自动提取的长期事实等，仍会注入，但不属于"个人偏好"。 */
export const UNCATEGORIZED_LABEL = "其他记忆";

export function categoryLabel(category?: string | null): string {
  return MEMORY_CATEGORIES.find((c) => c.value === category)?.label ?? UNCATEGORIZED_LABEL;
}

export interface MemoryGroup {
  key: string;
  label: string;
  hint?: string;
  items: Memory[];
}

/** 按类别分组（类别顺序固定，空组不显示；无类别的排在最后）。 */
export function groupMemories(memories: Memory[]): MemoryGroup[] {
  const groups: MemoryGroup[] = [];
  for (const category of MEMORY_CATEGORIES) {
    const items = memories.filter((m) => m.category === category.value);
    if (items.length > 0) {
      groups.push({ key: category.value, label: category.label, hint: category.hint, items });
    }
  }
  const rest = memories.filter(
    (m) => !MEMORY_CATEGORIES.some((c) => c.value === m.category),
  );
  if (rest.length > 0) {
    groups.push({
      key: "__uncategorized",
      label: UNCATEGORIZED_LABEL,
      hint: "系统从对话中提取的长期信息；不属于你手动保存的个人偏好",
      items: rest,
    });
  }
  return groups;
}

/** 保存前必须让用户看到的说明（设计方案 §4.1/§4.5）。 */
export const MEMORY_SAVE_NOTICE =
  "已启用的个人记忆会用于你的新智能聊天和知识库对话，以改善回答方式。你可以随时修改、停用或删除。";

/** 敏感内容提醒：服务端会拒绝明显敏感的内容，界面先把话说明白。 */
export const MEMORY_SENSITIVE_NOTICE =
  "请不要把密码、令牌、API Key、身份证件号、银行卡号、病历或财务信息保存为记忆；这类内容会被拒绝，也不会被静默写入。";

/* --- 智能聊天记忆第三期：类别、范围与状态标签（设计方案 §3、§4.1）--- */

/** 跨会话记忆类别，与服务端 `VALID_KINDS` 一一对应。 */
export const MEMORY_KINDS: { value: MemoryKind; label: string; hint: string }[] = [
  {
    value: "decision",
    label: "已确认决策",
    hint: "已经定下来的做法。例：第三期不做自动模型路由",
  },
  {
    value: "background",
    label: "稳定背景",
    hint: "长期成立的事实。例：项目用的是 React + FastAPI",
  },
  {
    value: "project_rule",
    label: "项目规则",
    hint: "只在某个项目内适用的约定",
  },
  {
    value: "conclusion",
    label: "历史结论",
    hint: "以往会话里得出的结论",
  },
  {
    value: "todo",
    label: "待办与目标",
    hint: "还没做完或要持续推进的事",
  },
  {
    value: "fact",
    label: "一般记忆",
    hint: "不属于以上类别的长期信息",
  },
];

export function kindLabel(kind?: string | null): string {
  return MEMORY_KINDS.find((k) => k.value === kind)?.label ?? "一般记忆";
}

/** 记忆状态标签（服务端算出，界面直接显示；P3-05/P3-06/P3-07）。 */
export const MEMORY_STATUS_LABELS: Record<MemoryStatus, string> = {
  confirmed: "已确认",
  unconfirmed: "未确认（自动提取）",
  superseded: "已被替代",
  expired: "已过期",
  disabled: "已停用",
};

export function statusLabel(status?: string | null): string {
  return MEMORY_STATUS_LABELS[(status ?? "confirmed") as MemoryStatus] ?? "已确认";
}

/** 有效期显示：长期 / 到期日期（本地日期，不显示时分）。 */
export function expiresLabel(expiresAt?: string | null): string {
  if (!expiresAt) return "长期";
  const date = new Date(expiresAt);
  if (Number.isNaN(date.getTime())) return "长期";
  return `有效期至 ${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(
    date.getDate(),
  ).padStart(2, "0")}`;
}

/** `<input type="date">` 需要 YYYY-MM-DD；服务端存的是 datetime。 */
export function toDateString(value?: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(
    date.getDate(),
  ).padStart(2, "0")}`;
}
