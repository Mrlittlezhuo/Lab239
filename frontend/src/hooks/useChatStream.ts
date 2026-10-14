import { useCallback, useRef, useState } from "react";

import type { ChatCompletionRequest } from "@/api/chat";
import {
  buildVisionContent,
  streamChatCompletion,
  visionChatCompletion,
} from "@/api/chat";
import type { MessageRecord } from "@/api/conversations";
import type { MemoryProposal } from "@/api/memory";
import { detectProposals } from "@/lib/memoryIntents";
import { applySseEvent } from "@/lib/sse/applyEvent";
import type {
  AgentPlanData,
  AgentStepData,
  ReferenceData,
  RouteResultData,
  SseEvent,
} from "@/lib/sse/types";
import type { ToolMode } from "@/stores/chatPrefs";

export interface UiToolCall {
  name?: string;
  args?: unknown;
  output?: unknown;
  isError?: boolean;
  done: boolean;
}

export interface UiAgentRun {
  runId?: string;
  status: string;
  plan?: AgentPlanData;
  steps: AgentStepData[];
  finalResult?: string;
  error?: string;
  /** 生成中的正文预览（计划摘要 / 最终答案），终态后清空 */
  livePlan?: string;
  liveAnswer?: string;
}

export interface UiMessage {
  key: string;
  role: "user" | "assistant";
  content: string;
  route?: {
    route: RouteResultData;
    executionMode?: string;
    executionModeLabel?: string;
  };
  toolCalls: UiToolCall[];
  agent?: UiAgentRun;
  references?: ReferenceData[];
  error?: string;
  serverMessageId?: string;
  feedbackScore?: number;
  streaming?: boolean;
  imageDataUrl?: string; // 视觉输入附件展示
  /** 该轮的行为说明（例如图片解析不落库），渲染为浅色提示 */
  note?: string;
  /**
   * 本轮触发的“记住 / 忘记”确认卡片（第二期 §4.3）。
   * 只是待确认建议，未点「确认保存」前服务端没有任何写入；
   * 刷新页面后卡片会消失（未确认的内容本来就没保存）。
   */
  proposals?: MemoryProposal[];
}

export interface SendOptions {
  model: string;
  temperature: number | null;
  toolMode: ToolMode;
  selectedTools: string[];
  useMcp: boolean;
  imageDataUrl?: string;
  /** 图片解析轮使用的 image2text 模型（有 imageDataUrl 时必填） */
  visionModel?: string;
  /** 本轮是否使用个人记忆；false 对应"本次不使用个人偏好"（仅当前会话） */
  useMemory?: boolean;
  /** 临时对话：新建会话时带上，该会话不创建、不保留会话私有上下文（§4.4） */
  temporary?: boolean;
}

let keySeq = 0;
const nextKey = () => `m${Date.now().toString(36)}${(keySeq++).toString(36)}`;

export function historyToUiMessages(records: MessageRecord[]): UiMessage[] {
  const out: UiMessage[] = [];
  for (const r of records) {
    if (r.query) {
      out.push({ key: `${r.id}q`, role: "user", content: r.query, toolCalls: [] });
    }
    const refs = Array.isArray(r.metadata?.reference_metadata)
      ? (r.metadata.reference_metadata as ReferenceData[])
      : Array.isArray(r.metadata?.references)
        ? (r.metadata.references as ReferenceData[])
        : undefined;
    out.push({
      key: r.id,
      role: "assistant",
      content: r.response ?? "",
      toolCalls: [],
      references: refs,
      serverMessageId: r.id,
      feedbackScore: r.feedback_score ?? -1,
    });
  }
  return out;
}

export function useChatStream(options: {
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

  const updateLastAssistant = useCallback(
    (fn: (m: UiMessage) => UiMessage) => {
      setMessages((prev) => {
        const idx = prev.length - 1;
        if (idx < 0 || prev[idx].role !== "assistant") return prev;
        const copy = [...prev];
        copy[idx] = fn(copy[idx]);
        return copy;
      });
    },
    [],
  );

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
    async (text: string, opts: SendOptions) => {
      const trimmed = text.trim();
      if ((!trimmed && !opts.imageDataUrl) || streaming) return;
      const userMsg: UiMessage = {
        key: nextKey(),
        role: "user",
        content: trimmed,
        toolCalls: [],
        imageDataUrl: opts.imageDataUrl,
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

      // “记住 / 忘记”确认卡片：与回答并行，只看用户本轮原话（P2-12）。
      // 检测失败不打断回答；未点「确认保存」前服务端没有任何写入。
      if (trimmed) {
        void detectProposals(trimmed, conversationIdRef.current).then(
          (proposals) => {
            if (proposals.length > 0)
              patchByKey(assistantMsg.key, { proposals });
          },
        );
      }

      const controller = new AbortController();
      abortRef.current = controller;
      try {
        if (opts.imageDataUrl) {
          // 图片解析走 /v1 直通接口（多模态 content 才能到达模型）：
          // 该轮不调用工具/Agent，也不写入会话历史。
          if (!opts.visionModel) {
            throw new Error(
              "请先在“聊天设置 → 视觉模型”中选择 image2text 模型",
            );
          }
          const res = await visionChatCompletion({
            model: opts.visionModel,
            content: buildVisionContent(
              trimmed || "请描述这张图片",
              opts.imageDataUrl,
            ),
            ...(opts.temperature !== null
              ? { temperature: opts.temperature }
              : {}),
          });
          updateLastAssistant((m) => ({
            ...m,
            content: res.choices?.[0]?.message?.content ?? "",
            note: "图片解析走 /v1 直通接口：本轮不调用工具，也不写入会话历史",
          }));
        } else {
          const req: ChatCompletionRequest = {
            messages: [{ role: "user", content: trimmed }],
            model: opts.model,
            stream: true,
          };
          if (opts.temperature !== null) req.temperature = opts.temperature;
          if (conversationIdRef.current) {
            req.conversation_id = conversationIdRef.current;
          }
          // 只在关闭时带上：默认（使用个人记忆）不新增字段，请求形状与旧版一致
          if (opts.useMemory === false) req.use_memory = false;
          // 临时对话同理：只在开启时带上，服务端据此标记新建的会话
          if (opts.temporary) req.temporary = true;
          switch (opts.toolMode) {
            case "auto":
              req.auto_agent = true;
              // 智能路由也带上 MCP 开关：服务端按请求决定本轮是否加载 MCP 工具
              req.use_mcp = opts.useMcp;
              break;
            case "plain":
              break;
            case "manual":
              req.tools = opts.selectedTools;
              req.use_mcp = opts.useMcp;
              break;
            case "agent_run":
              req.agent_run = true;
              req.use_mcp = opts.useMcp;
              break;
          }
          await streamChatCompletion(req, handleEvent, controller.signal);
        }
      } catch (err) {
        if ((err as Error)?.name === "AbortError") {
          updateLastAssistant((m) => ({
            ...m,
            error: m.error ?? "已停止生成",
          }));
        } else {
          const message = err instanceof Error ? err.message : "请求失败";
          updateLastAssistant((m) => ({ ...m, error: message }));
        }
      } finally {
        updateLastAssistant((m) => ({ ...m, streaming: false }));
        setStreaming(false);
        abortRef.current = null;
      }
    },
    [handleEvent, onConversationId, patchByKey, streaming, updateLastAssistant],
  );

  const stop = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  const reset = useCallback(() => {
    abortRef.current?.abort();
    setMessages([]);
  }, []);

  const setInitialMessages = useCallback((msgs: UiMessage[]) => {
    setMessages(msgs);
  }, []);

  const patchMessage = patchByKey;

  return {
    messages,
    streaming,
    send,
    stop,
    reset,
    setInitialMessages,
    patchMessage,
  };
}
