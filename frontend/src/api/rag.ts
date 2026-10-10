import { streamSse } from "@/lib/sse/stream";
import type { SseEvent } from "@/lib/sse/types";
import type { ChatMessage } from "@/api/chat";

export type RagMode = "local_kb" | "temp_kb" | "search_engine";

export interface RagChatRequest {
  messages: ChatMessage[];
  model: string;
  stream: boolean;
  temperature?: number;
  top_k?: number;
  score_threshold?: number;
  prompt_name?: string;
  return_direct?: boolean;
  conversation_id?: string;
}

/**
 * 知识库对话：POST /knowledge_base/{mode}/{param}/chat/completions
 * param：local_kb=知识库名，temp_kb=临时库 id，search_engine=搜索引擎名。
 */
export function streamRagChat(
  mode: RagMode,
  param: string,
  req: RagChatRequest,
  onEvent: (event: SseEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  return streamSse({
    path: `/knowledge_base/${mode}/${encodeURIComponent(param)}/chat/completions`,
    body: { ...req, stream: true },
    onEvent,
    signal,
  });
}
