import { classifyFrame } from "@/lib/sse/classify";
import { SseFrameParser } from "@/lib/sse/frame-parser";
import type { SseEvent } from "@/lib/sse/types";
import { apiUrl } from "@/lib/config";
import { ApiError } from "@/lib/errors";
import { AUTH_EVENT_EXPIRED, AUTH_EVENT_PASSWORD_REQUIRED } from "@/api/http";
import { clearAuth, getAuthToken } from "@/stores/auth";

export interface SseStreamOptions {
  path: string;
  body?: unknown;
  method?: "POST" | "GET";
  signal?: AbortSignal;
  onEvent: (event: SseEvent) => void;
}

/**
 * fetch + ReadableStream 版 SSE（EventSource 无法带 Authorization）。
 * 401 → 登出广播；403 请先修改密码 → 改密广播。
 */
export async function streamSse(options: SseStreamOptions): Promise<void> {
  const { path, body, method = "POST", signal, onEvent } = options;
  const headers = new Headers({ Accept: "text/event-stream" });
  const token = getAuthToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  // 必须显式声明 JSON：fetch 对字符串 body 默认打
  // `text/plain;charset=UTF-8`，FastAPI 据此不按 JSON 解析请求体，于是所有
  // 必填字段都报 422（model_attributes_type），聊天/RAG/重建/恢复四条流全挂。
  if (body !== undefined) headers.set("Content-Type", "application/json");

  const res = await fetch(apiUrl(path), {
    method,
    headers,
    credentials: "omit",
    signal,
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  if (res.status === 401) {
    clearAuth();
    window.dispatchEvent(new CustomEvent(AUTH_EVENT_EXPIRED));
    throw await ApiError.fromResponse(res);
  }
  if (!res.ok) {
    const err = await ApiError.fromResponse(res);
    if (err.isPasswordRequired()) {
      window.dispatchEvent(new CustomEvent(AUTH_EVENT_PASSWORD_REQUIRED));
    }
    throw err;
  }
  if (!res.body) throw new ApiError(204, "响应没有内容流");

  const reader = res.body.getReader();
  const decoderParser = new SseFrameParser();
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    if (!value) continue;
    for (const frame of decoderParser.push(value)) {
      onEvent(classifyFrame(frame));
    }
  }
  for (const frame of decoderParser.end()) {
    onEvent(classifyFrame(frame));
  }
}
