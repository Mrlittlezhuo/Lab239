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
  | { kind: "progress"; total: number; finished: number; doc?: string }
  | { kind: "error"; message: string; conversationId?: string }
  | { kind: "malformed"; raw: string };
