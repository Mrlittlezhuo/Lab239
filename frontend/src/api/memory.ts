import { deleteJson, getJson, patchJson, postJson } from "@/api/http";

export interface Memory {
  id: string;
  memory_key: string;
  type: string; // preference/fact/goal/important
  content: string;
  source: string;
  importance: number;
  enabled: boolean;
  source_conversation_id?: string | null;
  source_message_id?: string | null;
  create_time?: string | null;
  update_time?: string | null;
}

export function listMemories(): Promise<Memory[]> {
  return getJson<Memory[]>("/memories");
}

export function createMemory(input: {
  content: string;
  type?: string;
  importance?: number;
  source?: string;
}): Promise<Memory> {
  return postJson<Memory>("/memories", input);
}

export function updateMemory(
  id: string,
  patch: {
    type?: string;
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

export function getAutoMemory(): Promise<{ auto_memory: boolean }> {
  return getJson("/memories/auto");
}

export function setAutoMemory(enabled: boolean): Promise<{ auto_memory: boolean }> {
  return patchJson("/memories/auto", undefined, { query: { enabled } });
}
