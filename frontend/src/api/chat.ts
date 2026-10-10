import { postJson } from "@/api/http";
import { streamSse } from "@/lib/sse/stream";
import type { SseEvent } from "@/lib/sse/types";

export interface ChatMessage {
  role: "user" | "assistant" | "system";
  content: string;
}

/** OpenAI 多模态 content 片段（视觉输入用）。 */
export type ChatContentPart =
  | { type: "text"; text: string }
  | { type: "image_url"; image_url: { url: string } };

/** 扁平结构：服务端只认这个形状（Streamlit 的嵌套形状是坏的）。 */
export interface ChatModelConfig {
  preprocess_model?: Record<string, unknown>;
  llm_model?: Record<string, unknown>;
  action_model?: Record<string, unknown>;
  postprocess_model?: Record<string, unknown>;
}

export interface ChatCompletionRequest {
  messages: ChatMessage[];
  model: string;
  stream: boolean;
  temperature?: number;
  max_tokens?: number;
  tools?: string[];
  tool_choice?: string;
  // chatchat 扩展字段必须放顶层（服务端从 body.model_extra 读取）
  conversation_id?: string;
  auto_agent?: boolean;
  agent_run?: boolean;
  use_mcp?: boolean;
  metadata?: Record<string, unknown>;
  chat_model_config?: ChatModelConfig;
  tool_input?: Record<string, unknown>;
  agent_max_steps?: number;
}

/** 实际发出去的请求体：model 可以整个缺席。 */
export type ChatRequestBody = Omit<ChatCompletionRequest, "model"> & {
  model?: string;
};

/**
 * `model: ""` 在本应用里表示“用服务端默认模型”，但服务端 pydantic 会把空串
 * 当成显式值，模型查找失败后请求直接 500。因此空模型必须整个省略 key。
 */
export function toChatRequestBody(req: ChatCompletionRequest): ChatRequestBody {
  const { model, ...rest } = req;
  return model && model.trim() ? { ...rest, model } : rest;
}

export function streamChatCompletion(
  req: ChatCompletionRequest,
  onEvent: (event: SseEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  return streamSse({
    path: "/chat/chat/completions",
    body: { ...toChatRequestBody(req), stream: true },
    onEvent,
    signal,
  });
}

/** 非流式（返回 OpenAI completion 结构）。 */
export async function chatCompletion(
  req: ChatCompletionRequest,
): Promise<{
  choices: { message: { role: string; content: string } }[];
  conversation_id?: string;
  message_id?: string;
}> {
  return postJson("/chat/chat/completions", { ...toChatRequestBody(req), stream: false });
}

/**
 * 视觉输入必须走 `/v1/chat/completions`：该路由把 `messages` 原样透传给模型
 * 平台，多模态 `content` 才能真正到达模型。
 *
 * `/chat/chat/completions` 不行：它把最后一条消息的 `content` 当字符串用于
 * 会话标题、落库 `query` 列和 prompt 模板，传数组会在写库时失败。
 *
 * 代价（界面需说明）：v1 直通不落库，因此这一轮没有会话历史、没有
 * `message_id`，也就不能点赞/点踩，且必须用 image2text 视觉模型。
 */
export function buildVisionContent(
  text: string,
  imageDataUrl: string,
): ChatContentPart[] {
  return [
    { type: "text", text },
    { type: "image_url", image_url: { url: imageDataUrl } },
  ];
}

export async function visionChatCompletion(req: {
  model: string;
  content: ChatContentPart[];
  temperature?: number;
  max_tokens?: number;
}): Promise<{
  choices: { message: { role: string; content: string } }[];
}> {
  return postJson("/v1/chat/completions", {
    model: req.model,
    messages: [{ role: "user", content: req.content }],
    stream: false,
    ...(req.temperature != null ? { temperature: req.temperature } : {}),
    ...(req.max_tokens != null ? { max_tokens: req.max_tokens } : {}),
  });
}

/** score 0-100（👍=100，👎=0），reason 可空。 */
export function sendFeedback(
  messageId: string,
  score: number,
  reason = "",
): Promise<unknown> {
  return postJson("/chat/feedback", { message_id: messageId, score, reason });
}
