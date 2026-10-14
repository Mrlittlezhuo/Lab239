import type { MemoryKind, MemoryScopeKind } from "@/api/memory";
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
  /**
   * 与 `confirmed` 一一对应：该条已确认事项是否已保存为跨会话记忆。
   * 抽屉里据此把「保存为跨会话记忆」显示成「已保存」，避免重复保存。
   */
  confirmed_saved: boolean[];
  /** 会话所在项目空间（第三期 §4.4）；项目范围记忆只在此会话内召回 */
  project_id: string | null;
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

/* ----------------------------------------------------------------------
 * 第三期：候选提升与会话内「本次不使用」（设计方案 §4.2、§4.3）
 * -------------------------------------------------------------------- */

/**
 * 一条"值得保存为跨会话记忆"的候选。
 *
 * 服务端只提出候选，绝不自动升级：界面点「不保存」时不会有任何写入
 * （P3-13）。`already_saved` 表示这条已确认事项已经在跨会话记忆里。
 */
export interface PromotionCandidate {
  content: string;
  recommended_kind: MemoryKind;
  recommended_scope: MemoryScopeKind;
  project_id?: string | null;
  project_name?: string | null;
  reason: string;
  already_saved: boolean;
}

export function listPromotionCandidates(
  conversationId: string,
): Promise<{ candidates: PromotionCandidate[] }> {
  return getJson(`/conversations/${conversationId}/promotions`);
}

/**
 * 本会话「本次不使用」的记忆 id。
 *
 * 记录在服务端而不是浏览器里，因此普通聊天、知识库对话与 Agent 任务一起
 * 遵守，刷新页面也仍然有效；只影响当前会话（P3-04）。
 */
export function listIgnoredMemoryIds(
  conversationId: string,
): Promise<{ memory_ids: string[] }> {
  return getJson(`/conversations/${conversationId}/memory-ignores`);
}

export function ignoreMemory(
  conversationId: string,
  memoryId: string,
): Promise<{ ignored: boolean }> {
  return postJson(`/conversations/${conversationId}/memory-ignores`, {
    memory_id: memoryId,
  });
}

export function unignoreMemory(
  conversationId: string,
  memoryId: string,
): Promise<{ removed: boolean }> {
  return deleteJson(`/conversations/${conversationId}/memory-ignores/${memoryId}`);
}
