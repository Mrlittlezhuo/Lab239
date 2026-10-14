import { deleteJson, getJson, patchJson, postJson } from "@/api/http";

/**
 * 会话私有上下文（智能聊天记忆第二期设计方案 §4.2）。
 *
 * 这一份内容只在当前会话内注入，不会带入其他会话；`temporary=true` 的会话
 * 不创建也不保留上下文，因此读取时槽位为空、写入返回 400。
 */
export interface SessionContext {
  conversation_id: string;
  temporary: boolean;
  topic: string;
  goal: string;
  confirmed: string[];
  constraints: string[];
  open_questions: string[];
  summary: string;
  /** 摘要已覆盖的消息数（0 表示还没有摘要） */
  summarized_count: number;
  update_time: string | null;
}

/** 只覆盖传入的槽位；列表按整组替换（与抽屉里的编辑框一致）。 */
export interface SessionContextEdit {
  topic?: string;
  goal?: string;
  confirmed?: string[];
  constraints?: string[];
  open_questions?: string[];
  summary?: string;
}

export function getSessionContext(
  conversationId: string,
): Promise<SessionContext> {
  return getJson(`/conversations/${conversationId}/context`);
}

/**
 * 用户编辑过的条目会作为"用户明确修改的结论"参与后续自动更新，
 * 不会被自动摘要静默覆盖（设计方案 §4.2）。
 */
export function editSessionContext(
  conversationId: string,
  patch: SessionContextEdit,
): Promise<SessionContext> {
  return patchJson(`/conversations/${conversationId}/context`, patch);
}

/** 对应抽屉里的「更新摘要」：忽略"历史够长才摘要"的阈值。 */
export function refreshSessionContext(
  conversationId: string,
): Promise<SessionContext> {
  return postJson(`/conversations/${conversationId}/context/refresh`, {});
}

/** 只清本会话上下文；个人偏好与其他会话不受影响（P2-06）。 */
export function resetSessionContext(
  conversationId: string,
): Promise<{ reset: boolean }> {
  return deleteJson(`/conversations/${conversationId}/context`);
}
