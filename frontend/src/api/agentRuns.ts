import { getJson, postJson } from "@/api/http";

export interface AgentRunSummary {
  run_id: string;
  conversation_id: string | null;
  source_message_id: string | null;
  goal: string;
  plan_text: string;
  status: string; // pending/running/completed/failed/cancelled
  current_step: number;
  max_steps: number;
  final_result: string | null;
  error: string | null;
  create_time: string | null;
  start_time: string | null;
  end_time: string | null;
}

export interface AgentRunStep {
  step_id: string;
  seq: number;
  type: string;
  status: string;
  goal: string;
  tool_name: string | null;
  tool_input: unknown;
  output_summary: string | null;
  error_summary: string | null;
  create_time: string | null;
  start_time: string | null;
  end_time: string | null;
}

export function listAgentRuns(
  conversationId?: string,
  limit = 20,
): Promise<AgentRunSummary[]> {
  return getJson<{ runs: AgentRunSummary[] }>("/agent-runs", {
    query: { conversation_id: conversationId, limit },
  }).then((r) => r.runs);
}

export function getAgentRun(runId: string): Promise<{
  run: AgentRunSummary;
  steps: AgentRunStep[];
}> {
  return getJson(`/agent-runs/${runId}`);
}

export function cancelAgentRun(runId: string): Promise<{ run: AgentRunSummary }> {
  return postJson(`/agent-runs/${runId}/cancel`);
}

/** 非流式恢复（stream=false，返回聚合结果；流式恢复走聊天页）。 */
export function resumeAgentRun(
  runId: string,
  useMcp = false,
): Promise<Record<string, unknown>> {
  return postJson(`/agent-runs/${runId}/resume?stream=false`, { use_mcp: useMcp });
}
