import type { Memory, MemoryCategory } from "@/api/memory";

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
