import { useCallback, useRef, useState } from "react";

import type { RagChatRequest, RagMode } from "@/api/rag";
import { streamRagChat } from "@/api/rag";
import { applySseEvent } from "@/lib/sse/applyEvent";
import type { SseEvent } from "@/lib/sse/types";
import type { UiMessage } from "@/hooks/useChatStream";

export interface RagSendOptions {
  mode: RagMode;
  param: string; // 知识库名 / 临时库 id / 搜索引擎名
  model: string;
  temperature: number | null;
  topK?: number;
  scoreThreshold?: number;
}

let keySeq = 0;
const nextKey = () => `r${Date.now().toString(36)}${(keySeq++).toString(36)}`;

export function useRagStream(options: {
  conversationId?: string;
  onConversationId: (id: string) => void;
}) {
  const { conversationId, onConversationId } = options;
  const [messages, setMessages] = useState<UiMessage[]>([]);
  const [streaming, setStreaming] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const conversationIdRef = useRef(conversationId);
  conversationIdRef.current = conversationId;

  const handleEvent = useCallback(
    (event: SseEvent) => {
      if ("conversationId" in event && event.conversationId) {
        if (!conversationIdRef.current) {
          conversationIdRef.current = event.conversationId;
          onConversationId(event.conversationId);
        }
      }
      setMessages((prev) => applySseEvent(prev, event));
    },
    [onConversationId],
  );

  const send = useCallback(
    async (text: string, opts: RagSendOptions) => {
      const trimmed = text.trim();
      if (!trimmed || streaming) return;
      if (!opts.param) return;
      const userMsg: UiMessage = {
        key: nextKey(),
        role: "user",
        content: trimmed,
        toolCalls: [],
      };
      const assistantMsg: UiMessage = {
        key: nextKey(),
        role: "assistant",
        content: "",
        toolCalls: [],
        streaming: true,
      };
      setMessages((prev) => [...prev, userMsg, assistantMsg]);
      setStreaming(true);

      const req: RagChatRequest = {
        messages: [{ role: "user", content: trimmed }],
        model: opts.model,
        stream: true,
      };
      if (opts.temperature !== null) req.temperature = opts.temperature;
      if (opts.topK !== undefined) req.top_k = opts.topK;
      if (opts.scoreThreshold !== undefined)
        req.score_threshold = opts.scoreThreshold;
      if (conversationIdRef.current)
        req.conversation_id = conversationIdRef.current;

      const controller = new AbortController();
      abortRef.current = controller;
      try {
        await streamRagChat(opts.mode, opts.param, req, handleEvent, controller.signal);
      } catch (err) {
        const message =
          (err as Error)?.name === "AbortError"
            ? "已停止生成"
            : err instanceof Error
              ? err.message
              : "请求失败";
        setMessages((prev) => {
          const idx = prev.length - 1;
          if (idx < 0 || prev[idx].role !== "assistant") return prev;
          const copy = [...prev];
          copy[idx] = { ...copy[idx], error: copy[idx].error ?? message };
          return copy;
        });
      } finally {
        setMessages((prev) => {
          const idx = prev.length - 1;
          if (idx < 0 || prev[idx].role !== "assistant") return prev;
          const copy = [...prev];
          copy[idx] = { ...copy[idx], streaming: false };
          return copy;
        });
        setStreaming(false);
        abortRef.current = null;
      }
    },
    [handleEvent, streaming],
  );

  const stop = useCallback(() => abortRef.current?.abort(), []);
  const reset = useCallback(() => {
    abortRef.current?.abort();
    setMessages([]);
  }, []);
  const setInitialMessages = useCallback((msgs: UiMessage[]) => setMessages(msgs), []);
  const patchMessage = useCallback(
    (key: string, patch: Partial<UiMessage>) =>
      setMessages((prev) => prev.map((m) => (m.key === key ? { ...m, ...patch } : m))),
    [],
  );

  return { messages, streaming, send, stop, reset, setInitialMessages, patchMessage };
}
