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

export function listConversations(): Promise<Conversation[]> {
  return getJson<Conversation[]>("/conversations");
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
