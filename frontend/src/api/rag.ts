import { streamSse } from "@/lib/sse/stream";
import type { SseEvent } from "@/lib/sse/types";
import type { ChatMessage } from "@/api/chat";

export type RagMode = "local_kb" | "temp_kb" | "search_engine";

export interface RagChatRequest {
  messages: ChatMessage[];
  /** 留空/缺省时由服务端用默认模型；不能传空串（见 streamRagChat 里的说明）。 */
  model?: string;
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
  const body: RagChatRequest = { ...req, stream: true };
  // 空 model 必须整个不传：服务端签名是 `model: str = Body(get_default_llm())`，
  // 客户端显式传 "" 时默认值不生效，get_ChatOpenAI("") 返回 None，
  // 整轮知识库对话崩在 `chat_prompt | None`
  // （界面上是 Expected a Runnable, callable or dict ... NoneType）。
  if (!body.model?.trim()) delete body.model;
  return streamSse({
    path: `/knowledge_base/${mode}/${encodeURIComponent(param)}/chat/completions`,
    body,
    onEvent,
    signal,
  });
}
