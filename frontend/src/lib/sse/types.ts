/** chatchat SSE 块分类后的判别联合。 */

export interface RouteResultData {
  path?: string;
  path_label?: string;
  intent?: string;
  intent_label?: string;
  complexity?: string;
  candidate_tools?: string[];
  confidence?: number;
  summary?: string;
  provider?: string;
  source?: string;
  fallback_reason?: string | null;
}

export interface AgentStepData {
  /** 服务端步骤标识（agent_step.seq，1 起）；step_started/step_finished 靠它合并成一行 */
  seq?: number;
  step?: number;
  type?: string; // tool_call | answer
  goal?: string;
  tool_name?: string;
  tool_input?: unknown;
  tool_output?: unknown;
  output_summary?: string;
  error_summary?: string;
  status?: string; // running | completed | failed | cancelled
  thought?: string;
  [key: string]: unknown;
}

export interface AgentPlanData {
  summary?: string;
  steps?: unknown[];
  [key: string]: unknown;
}

export type AgentEventType =
  | "run_created"
  | "plan_ready"
  | "step_started"
  | "step_finished"
  | "replanned"
  | "run_completed"
  | "run_failed"
  | "run_cancelled"
  // 生成中的正文预览（模型分片里的正文字段），只用于显示加速：
  // 最终答案以 plan_ready / run_completed 里的结构化结果为准
  | "plan_delta"
  | "answer_delta";

export interface ReferenceData {
  index?: number;
  knowledge_base?: string;
  file_name?: string;
  url?: string;
  content?: string;
  score?: number;
  page?: number;
  metadata?: Record<string, unknown>;
}

export interface ToolCallData {
  name?: string;
  args?: unknown;
  input?: unknown;
  tool_output?: unknown;
  is_error?: boolean;
  [key: string]: unknown;
}

/**
 * 生成状态机（第四期设计方案 §11.1）。`queued` 只在请求确实排队后才出现，
 * `partial / cancelled / failed` 表示这条回答没说完。
 */
export type GenerationStatus =
  | "prepared"
  | "queued"
  | "started"
  | "partial"
  | "cancelled"
  | "failed"
  | "completed";

/** 一条记忆的使用快照：内容也存下来，记忆后来被改删仍能还原当时注入了什么。 */
export interface MemoryUsageEntry {
  id?: string;
  content?: string;
  updated_at?: string;
  /** 召回理由：界面要能解释"为什么这条被用上" */
  reason?: string;
  bucket?: string;
  scope?: string;
  score?: number;
}

/**
 * 本轮实际使用的上下文快照（`memory_usage_manifest`，第四期 §11.1）。
 * 与助手消息 metadata 里那份同源：流式期间来自 SSE 元数据帧，历史会话从
 * 消息 metadata 恢复，两边显示的是同一份数据。
 */
export interface MemoryUsageManifest {
  conversation_id?: string;
  session_context_version?: number;
  summary_through_message_id?: string | null;
  summary_type?: string;
  summary_status?: string;
  history_boundary_message_id?: string | null;
  recent_message_ids?: string[];
  recent_chars?: number;
  preferences?: MemoryUsageEntry[];
  recalled_memories?: MemoryUsageEntry[];
  memory_disabled_for_turn?: boolean;
  degraded?: boolean;
  budget_source?: string;
  generation_status?: GenerationStatus;
  model_started_at?: string | null;
  model_finished_at?: string | null;
  failure_stage?: string | null;
  failure_reason?: string | null;
}

export type SseEvent =
  | {
      kind: "token";
      content: string;
      status: number | null;
      role?: string;
      messageId?: string;
      conversationId?: string;
    }
  | {
      kind: "tool";
      status: 7 | 8;
      toolCalls: ToolCallData[];
      conversationId?: string;
    }
  | {
      kind: "route";
      route: RouteResultData;
      executionMode?: string;
      executionModeLabel?: string;
      conversationId?: string;
    }
  | {
      kind: "agent";
      event: AgentEventType;
      runId?: string;
      plan?: AgentPlanData;
      step?: AgentStepData;
      steps?: AgentStepData[];
      finalResult?: string;
      error?: string;
      content: string;
      conversationId?: string;
    }
  | {
      kind: "reference";
      references: ReferenceData[];
      docs?: string[];
      retrievalStatus?: string;
      conversationId?: string;
    }
  | {
      kind: "memory";
      manifest: MemoryUsageManifest;
      status?: GenerationStatus;
      messageId?: string;
      conversationId?: string;
    }
  | { kind: "progress"; total: number; finished: number; doc?: string }
  | { kind: "error"; message: string; conversationId?: string }
  | { kind: "malformed"; raw: string };
