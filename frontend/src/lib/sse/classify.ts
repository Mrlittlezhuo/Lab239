import type {
  AgentEventType,
  AgentPlanData,
  AgentStepData,
  GenerationStatus,
  MemoryUsageManifest,
  ReferenceData,
  RouteResultData,
  SseEvent,
  ToolCallData,
} from "@/lib/sse/types";

/**
 * chatchat SSE 块 JSON → 判别联合。判定顺序关键：
 * 裸 error 块 → AgentRunEvent/Result → RouteResult → MemoryUsageEvent →
 * 引用块 → recreate 进度 → status -1 错误 → status 7/8 工具 → token。
 */
export function classifyFrame(payload: string): SseEvent {
  let chunk: Record<string, unknown>;
  try {
    chunk = JSON.parse(payload) as Record<string, unknown>;
  } catch {
    return { kind: "malformed", raw: payload };
  }
  if (chunk === null || typeof chunk !== "object") {
    return { kind: "malformed", raw: payload };
  }

  const conversationId =
    typeof chunk.conversation_id === "string"
      ? chunk.conversation_id
      : undefined;

  // 裸错误块：{"error": "..."}，无 choices
  if (chunk.error !== undefined && chunk.choices === undefined) {
    return {
      kind: "error",
      message:
        typeof chunk.error === "string"
          ? chunk.error
          : JSON.stringify(chunk.error),
      conversationId,
    };
  }

  const className = chunk.class_name;
  if (className === "AgentRunEvent" || className === "AgentRunResult") {
    return {
      kind: "agent",
      event: (typeof chunk.agent_event === "string"
        ? chunk.agent_event
        : "run_created") as AgentEventType,
      runId: typeof chunk.run_id === "string" ? chunk.run_id : undefined,
      plan: chunk.agent_plan as AgentPlanData | undefined,
      step: chunk.agent_step as AgentStepData | undefined,
      steps: chunk.agent_steps as AgentStepData[] | undefined,
      finalResult:
        typeof chunk.agent_final_result === "string"
          ? chunk.agent_final_result
          : undefined,
      error: typeof chunk.agent_error === "string" ? chunk.agent_error : undefined,
      content: extractContent(chunk),
      conversationId,
    };
  }

  if (className === "RouteResult" || chunk.route_result !== undefined) {
    return {
      kind: "route",
      route: (chunk.route_result ?? {}) as RouteResultData,
      executionMode:
        typeof chunk.execution_mode === "string" ? chunk.execution_mode : undefined,
      executionModeLabel:
        typeof chunk.execution_mode_label === "string"
          ? chunk.execution_mode_label
          : undefined,
      conversationId,
    };
  }

  // 本轮记忆使用快照（MemoryUsageEvent）：正文为空，必须**在 token 兜底之前**
  // 识别，否则元数据会被当成一个空 token 甚至污染回答（第四期 §11.4 / §18.3.11）。
  if (className === "MemoryUsageEvent" || chunk.memory_usage_manifest !== undefined) {
    return {
      kind: "memory",
      manifest: (chunk.memory_usage_manifest ?? {}) as MemoryUsageManifest,
      status:
        typeof chunk.generation_status === "string"
          ? (chunk.generation_status as GenerationStatus)
          : undefined,
      messageId:
        typeof chunk.message_id === "string" ? chunk.message_id : undefined,
      conversationId,
    };
  }

  if (
    Array.isArray(chunk.references) ||
    typeof chunk.retrieval_status === "string"
  ) {
    return {
      kind: "reference",
      references: (chunk.references ?? []) as ReferenceData[],
      docs: Array.isArray(chunk.docs) ? (chunk.docs as string[]) : undefined,
      retrievalStatus:
        typeof chunk.retrieval_status === "string"
          ? chunk.retrieval_status
          : undefined,
      conversationId,
    };
  }

  // recreate_vector_store 进度块：{code,msg,total,finished,doc}
  if (
    typeof chunk.total === "number" &&
    typeof chunk.finished === "number" &&
    chunk.choices === undefined
  ) {
    return {
      kind: "progress",
      total: chunk.total,
      finished: chunk.finished,
      doc: typeof chunk.doc === "string" ? chunk.doc : undefined,
    };
  }

  const status = chunk.status as number | null | undefined;

  if (status === -1) {
    return {
      kind: "error",
      message: extractContent(chunk) || "服务端返回错误",
      conversationId,
    };
  }

  if (status === 7 || status === 8) {
    const toolCalls = extractToolCalls(chunk);
    return { kind: "tool", status, toolCalls, conversationId };
  }

  // token（含 status 1/2/3/4/5/-999/null；空 content 也发，由消费方决定忽略。
  // 3/4/5/-999 是 Agent 流的整段回放，追加与否由 applySseEvent 判定）
  return {
    kind: "token",
    content: extractContent(chunk),
    status: typeof status === "number" ? status : null,
    role: extractRole(chunk),
    messageId: typeof chunk.message_id === "string" ? chunk.message_id : undefined,
    conversationId,
  };
}

function extractContent(chunk: Record<string, unknown>): string {
  const choices = chunk.choices;
  if (Array.isArray(choices) && choices.length > 0) {
    const first = choices[0] as Record<string, unknown>;
    const delta = first.delta as Record<string, unknown> | undefined;
    if (delta && typeof delta.content === "string") return delta.content;
    const message = first.message as Record<string, unknown> | undefined;
    if (message && typeof message.content === "string") return message.content;
  }
  if (typeof chunk.content === "string") return chunk.content;
  return "";
}

function extractRole(chunk: Record<string, unknown>): string | undefined {
  const choices = chunk.choices;
  if (Array.isArray(choices) && choices.length > 0) {
    const first = choices[0] as Record<string, unknown>;
    const delta = first.delta as Record<string, unknown> | undefined;
    if (delta && typeof delta.role === "string") return delta.role;
    if (typeof first.role === "string") return first.role;
  }
  if (typeof chunk.role === "string") return chunk.role;
  return undefined;
}

function extractToolCalls(chunk: Record<string, unknown>): ToolCallData[] {
  const choices = chunk.choices;
  if (Array.isArray(choices) && choices.length > 0) {
    const first = choices[0] as Record<string, unknown>;
    const delta = first.delta as Record<string, unknown> | undefined;
    if (Array.isArray(delta?.tool_calls) && delta!.tool_calls.length > 0) {
      return (delta!.tool_calls as Record<string, unknown>[]).map(normalizeToolCall);
    }
    const message = first.message as Record<string, unknown> | undefined;
    if (Array.isArray(message?.tool_calls) && message!.tool_calls.length > 0) {
      return (message!.tool_calls as Record<string, unknown>[]).map(normalizeToolCall);
    }
  }
  if (Array.isArray(chunk.tool_calls))
    return (chunk.tool_calls as Record<string, unknown>[]).map(normalizeToolCall);
  return [];
}

/**
 * 后端 status 7/8 发的是 OpenAI 风格的嵌套结构
 * `{index, id, type, function: {name, arguments}, tool_output, is_error}`
 * （见 chatchat/server/chat/chat.py），而 UI 用的是扁平的 name/args。
 * 不摊平的话工具卡片拿不到名字和参数，只剩一句"工具调用"。
 */
function normalizeToolCall(raw: Record<string, unknown>): ToolCallData {
  const fn = raw.function as Record<string, unknown> | undefined;
  if (!fn) return raw as ToolCallData;
  return {
    ...raw,
    name: typeof fn.name === "string" ? fn.name : (raw.name as string | undefined),
    args: fn.arguments ?? raw.args ?? raw.input,
  };
}
