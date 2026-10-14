import { deleteJson, getJson, patchJson, postJson } from "@/api/http";

/** 个人偏好类别（与服务端 VALID_CATEGORIES 一致）。 */
export type MemoryCategory =
  | "answer_style"
  | "technical_background"
  | "work_style"
  | "long_term_goal";

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
  },
): Promise<Memory> {
  return patchJson<Memory>(`/memories/${id}`, patch);
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

export function getAutoMemory(): Promise<{ auto_memory: boolean }> {
  return getJson("/memories/auto");
}

export function setAutoMemory(enabled: boolean): Promise<{ auto_memory: boolean }> {
  return patchJson("/memories/auto", undefined, { query: { enabled } });
}
