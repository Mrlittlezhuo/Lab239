import { deleteJson, getJson, patchJson, postJson } from "@/api/http";

export interface Conversation {
  id: string;
  name: string | null;
  chat_type: string | null;
  create_time: string | null;
  update_time: string | null;
}

export interface MessageRecord {
  id: string;
  chat_type: string | null;
  query: string | null;
  response: string | null;
  metadata: Record<string, unknown>;
  create_time: string | null;
  feedback_score: number | null;
  feedback_reason: string | null;
}

/**
 * 会话列表。chatType 决定只看哪一类会话：智能聊天是 agent_chat，
 * 知识库对话是 kb_chat —— 不传就混在一起（知识库对话页会看到智能聊天的会话）。
 */
export function listConversations(chatType?: string): Promise<Conversation[]> {
  return getJson<Conversation[]>("/conversations", {
    query: chatType ? { chat_type: chatType } : undefined,
  });
}

export function createConversation(
  name = "",
  chatType = "agent_chat",
): Promise<Conversation> {
  return postJson<Conversation>("/conversations", {
    name,
    chat_type: chatType,
  });
}

export function renameConversation(
  id: string,
  name: string,
): Promise<Conversation> {
  return patchJson<Conversation>(`/conversations/${id}`, { name });
}

export function deleteConversation(id: string): Promise<unknown> {
  return deleteJson(`/conversations/${id}`);
}

export function listMessages(
  conversationId: string,
): Promise<{ conversation_id: string; messages: MessageRecord[] }> {
  return getJson(`/conversations/${conversationId}/messages`);
}
