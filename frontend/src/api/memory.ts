import { deleteJson, getJson, patchJson, postJson } from "@/api/http";

/** 个人偏好类别（与服务端 VALID_CATEGORIES 一致）。 */
export type MemoryCategory =
  | "answer_style"
  | "technical_background"
  | "work_style"
  | "long_term_goal";

/**
 * 跨会话记忆的类别（与服务端 VALID_KINDS 一致）。
 * 与第一期的 `category` 不同：category 是"个人偏好"的分面，kind 是
 * 第三期召回用的类别（设计方案 §3）。
 */
export type MemoryKind =
  | "decision"
  | "background"
  | "project_rule"
  | "conclusion"
  | "todo"
  | "fact";

/** 记忆范围：个人（所有会话可用）或某个项目内（只在该项目的会话可用）。 */
export type MemoryScopeKind = "personal" | "project";

/** 服务端算出的记忆状态，界面直接显示，不再自己拼条件。 */
export type MemoryStatus =
  | "confirmed"
  | "unconfirmed"
  | "superseded"
  | "expired"
  | "disabled";

export interface Memory {
  id: string;
  memory_key: string;
  type: string; // preference/fact/goal/important
  /** 个人偏好类别；null 表示不是用户在管理页管理的个人偏好 */
  category?: MemoryCategory | null;
  content: string;
  source: string;
  importance: number;
  enabled: boolean;
  source_conversation_id?: string | null;
  source_message_id?: string | null;
  create_time?: string | null;
  update_time?: string | null;
  // --- 智能聊天记忆第三期 ---
  kind?: MemoryKind;
  scope?: MemoryScopeKind;
  project_id?: string | null;
  project_name?: string | null;
  confirmed?: boolean;
  /** 有效期（到期后不再自动召回，记录保留）；null 表示长期 */
  expires_at?: string | null;
  superseded_by?: string | null;
  supersedes?: string | null;
  status?: MemoryStatus;
  /** 来源说明（会话名 + 日期，或"手动保存"） */
  source_label?: string;
}

/** 本次对话实际会注入的个人偏好 + 用户级总开关（与服务端注入一致）。 */
export interface PreferencesResponse {
  enabled: boolean;
  max_count: number;
  preferences: Memory[];
}

export function listMemories(): Promise<Memory[]> {
  return getJson<Memory[]>("/memories");
}

export function createMemory(input: {
  content: string;
  type?: string;
  category?: MemoryCategory | null;
  importance?: number;
  source?: string;
  kind?: MemoryKind;
  scope?: MemoryScopeKind;
  project_id?: string | null;
  /** ISO 日期/时间；不传表示长期有效 */
  expires_at?: string | null;
}): Promise<Memory> {
  return postJson<Memory>("/memories", input);
}

export function updateMemory(
  id: string,
  patch: {
    type?: string;
    /** 传空串表示"清除类别"（服务端把 undefined 当作不改动） */
    category?: MemoryCategory | null | "";
    content?: string;
    importance?: number;
    enabled?: boolean;
    kind?: MemoryKind;
    scope?: MemoryScopeKind;
    /** 切到 project 范围时必须带当前用户自己的 project_id */
    project_id?: string | null;
    /** 传空串表示"清除有效期"（与 category 同一套约定） */
    expires_at?: string | null;
    confirmed?: boolean;
  },
): Promise<Memory> {
  return patchJson<Memory>(`/memories/${id}`, patch);
}

/** 单条记忆详情（聊天页就地编辑时需要完整字段）。 */
export function getMemory(id: string): Promise<Memory> {
  return getJson<Memory>(`/memories/${id}`);
}

export function deleteMemory(id: string): Promise<unknown> {
  return deleteJson(`/memories/${id}`);
}

/** 清空当前用户全部记忆（调用前必须二次确认）。 */
export function clearAllMemories(): Promise<{ deleted: number }> {
  return deleteJson("/memories");
}

/**
 * 本次对话将使用的个人偏好。`q` 传当前输入可让"与当前问题相关的背景"排到
 * 前面；服务端注入用的就是这一份，界面展示因此不会与实际注入不一致。
 */
export function listUsedPreferences(q?: string): Promise<PreferencesResponse> {
  return getJson<PreferencesResponse>(
    "/memories/preferences",
    q && q.trim() ? { query: { q } } : undefined,
  );
}

/** 个人记忆总开关：关闭只停止注入，不删除任何数据。 */
export function setPreferencesEnabled(
  enabled: boolean,
): Promise<{ enabled: boolean }> {
  return patchJson("/memories/preferences", undefined, { query: { enabled } });
}

/**
 * “记住 / 忘记”确认卡片（第二期设计方案 §4.3）。
 *
 * 检测只读：服务端只看用户本轮原话，知识库文档片段与模型回答不会触发。
 * 真正的写入发生在 `applyIntent`，因此用户点"不保存"时不会有任何数据变化。
 */
export type MemoryScope = "session" | "preference";

export interface IntentTarget {
  id: string;
  content: string;
  category?: MemoryCategory | null;
}

export interface MemoryProposal {
  kind: "remember" | "forget";
  content: string;
  /** null = 含义不明确，界面不预选，要求用户选范围 */
  recommended_scope: MemoryScope | null;
  scope_reason: string;
  recommended_category: MemoryCategory | null;
  conversation_temporary: boolean;
  /** forget 时列出将被停用的记忆，避免误删 */
  targets: IntentTarget[];
  note: string;
}

export interface ApplyIntentInput {
  kind: "remember" | "forget";
  content: string;
  scope: MemoryScope;
  conversation_id?: string | null;
  category?: MemoryCategory | null;
  target_ids?: string[];
}

export function detectIntents(
  text: string,
  conversationId?: string | null,
): Promise<{ proposals: MemoryProposal[] }> {
  return postJson("/memories/intents", {
    text,
    conversation_id: conversationId ?? null,
  });
}

export function applyIntent(
  input: ApplyIntentInput,
): Promise<{ scope: string; kind: string; message: string }> {
  return postJson("/memories/intents/apply", input);
}

export function getAutoMemory(): Promise<{ auto_memory: boolean }> {
  return getJson("/memories/auto");
}

export function setAutoMemory(enabled: boolean): Promise<{ auto_memory: boolean }> {
  return patchJson("/memories/auto", undefined, { query: { enabled } });
}

/* ----------------------------------------------------------------------
 * 智能聊天记忆第三期：跨会话相关记忆召回（设计方案 §4.1–§4.3）
 * -------------------------------------------------------------------- */

/**
 * 召回开关（与服务端 `RecallSwitches` 一致）。
 * 默认只有 `preferences` 与 `decisions` 打开；关闭只停止召回，不删任何数据。
 */
export interface RecallSwitches {
  /** 跨会话相关记忆总开关（不影响个人偏好，P3-11） */
  enabled: boolean;
  /** 使用已确认的历史决策 */
  decisions: boolean;
  /** 使用项目内记忆（默认关，P3-09/P3-10） */
  project: boolean;
  /** 使用一般历史会话结论 —— 自动提取的内容属于这一桶（默认关，P3-12） */
  general: boolean;
  /** 使用个人偏好（第一期的开关，同一份状态） */
  preferences: boolean;
}

/** 一条被本次回答使用的历史记忆 + 界面要能解释的信息（设计方案 §7）。 */
export interface RecalledMemory {
  id: string;
  content: string;
  kind: MemoryKind;
  kind_label: string;
  scope: MemoryScopeKind;
  project_id?: string | null;
  project_name?: string | null;
  confirmed: boolean;
  status: MemoryStatus;
  source_label: string;
  /** 为什么用它（命中关键词 / 主题一致），用于"它为什么被使用" */
  reason: string;
  bucket: string;
  importance: number;
  update_time?: string | null;
  expires_at?: string | null;
  source_conversation_id?: string | null;
}

export interface RecallPreview {
  switches: RecallSwitches;
  max_count: number;
  conversation_id?: string | null;
  project_id?: string | null;
  memories: RecalledMemory[];
}

export function getRecallSwitches(): Promise<RecallSwitches> {
  return getJson<RecallSwitches>("/memories/recall/switches");
}

/** 只传要改的键；服务端按"未传的键保持原值"处理。 */
export function setRecallSwitches(
  patch: Partial<RecallSwitches>,
): Promise<RecallSwitches> {
  return patchJson<RecallSwitches>("/memories/recall/switches", patch);
}

/**
 * 本次回答实际会参考的历史记忆。召回是确定性的：同样的
 * (用户, 问题, 会话, 项目) 得到同一批，因此界面展示与注入给模型的内容一致。
 */
export function listRecalledMemories(input: {
  q: string;
  conversationId?: string | null;
  projectId?: string | null;
}): Promise<RecallPreview> {
  return getJson<RecallPreview>("/memories/recall", {
    query: {
      q: input.q ?? "",
      conversation_id: input.conversationId || undefined,
      project_id: input.projectId || undefined,
    },
  });
}

/**
 * 把会话里的已确认事项保存为跨会话记忆（设计方案 §4.3）。
 * 只有用户确认后才调用；范围/类别/有效期由用户选定。
 * 同槽位的旧决策会被服务端标记为"已被替代"（P3-07）。
 */
export interface PromoteMemoryInput {
  content: string;
  kind: MemoryKind;
  scope: MemoryScopeKind;
  project_id?: string | null;
  expires_at?: string | null;
  conversation_id?: string | null;
}

export function promoteMemory(
  input: PromoteMemoryInput,
): Promise<{ memory: Memory; superseded_ids: string[] }> {
  return postJson("/memories/promote", input);
}

/** 某条记忆的修订历史（新→旧，含被替代前的内容，设计方案 §4.5）。 */
export interface MemoryRevision {
  id: string;
  memory_id: string;
  content: string;
  kind: string;
  scope: string;
  project_id?: string | null;
  project_name?: string | null;
  expires_at?: string | null;
  /** edit = 用户编辑；supersede = 被新决策替代前的快照 */
  reason: string;
  create_time?: string | null;
}

export function listMemoryRevisions(id: string): Promise<MemoryRevision[]> {
  return getJson<MemoryRevision[]>(`/memories/${id}/revisions`);
}
