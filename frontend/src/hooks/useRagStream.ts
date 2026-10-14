import { useCallback, useRef, useState } from "react";

import type { RagChatRequest, RagMode } from "@/api/rag";
import { streamRagChat } from "@/api/rag";
import { detectProposals } from "@/lib/memoryIntents";
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
  /** 本轮是否使用个人记忆；false 对应"本次不使用个人偏好"（仅当前会话） */
  useMemory?: boolean;
  /** 临时对话：新建会话时带上，该会话不创建、不保留会话私有上下文（§4.4） */
  temporary?: boolean;
  /** 会话所在项目空间：新建会话时带上，项目范围记忆只在本会话召回（第三期 §4.4） */
  projectId?: string;
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

  const patchByKey = useCallback((key: string, patch: Partial<UiMessage>) => {
    setMessages((prev) =>
      prev.map((m) => (m.key === key ? { ...m, ...patch } : m)),
    );
  }, []);

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

      // 确认卡片只看用户本轮原话：检索到的知识库文档不会触发（P2-12），
      // 因为服务端这一步收到的只有下面这条用户消息。
      void detectProposals(trimmed, conversationIdRef.current).then(
        (proposals) => {
          if (proposals.length > 0) patchByKey(assistantMsg.key, { proposals });
        },
      );

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
      // 个人偏好只影响"怎么解释和呈现"，检索范围、引用与文档原文不受影响
      if (opts.useMemory === false) req.use_memory = false;
      // 临时对话同理：只在开启时带上，服务端据此标记新建的会话
      if (opts.temporary) req.temporary = true;
      // 项目空间：服务端只在要新建会话时用它，已有会话以服务端记录为准
      if (opts.projectId) req.project_id = opts.projectId;

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
    [handleEvent, patchByKey, streaming],
  );

  const stop = useCallback(() => abortRef.current?.abort(), []);
  const reset = useCallback(() => {
    abortRef.current?.abort();
    setMessages([]);
  }, []);
  const setInitialMessages = useCallback((msgs: UiMessage[]) => setMessages(msgs), []);
  const patchMessage = patchByKey;

  return { messages, streaming, send, stop, reset, setInitialMessages, patchMessage };
}
