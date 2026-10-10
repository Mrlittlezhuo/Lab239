import type { SseEvent } from "@/lib/sse/types";
import type { UiAgentRun, UiMessage, UiToolCall } from "@/hooks/useChatStream";

/**
 * 把一条 SSE 事件应用到消息列表的最后一条 assistant 消息上（不可变更新）。
 * 聊天流与 RAG 流共用同一套 UiMessage 结构。
 */
export function applySseEvent(prev: UiMessage[], event: SseEvent): UiMessage[] {
  const idx = prev.length - 1;
  if (idx < 0 || prev[idx].role !== "assistant") return prev;
  const m = prev[idx];
  const copy = [...prev];

  switch (event.kind) {
    case "token":
      copy[idx] = {
        ...m,
        content: m.content + event.content,
        serverMessageId: event.messageId ?? m.serverMessageId,
      };
      break;
    case "route":
      copy[idx] = {
        ...m,
        route: {
          route: event.route,
          executionMode: event.executionMode,
          executionModeLabel: event.executionModeLabel,
        },
      };
      break;
    case "tool": {
      const calls = [...m.toolCalls];
      if (event.status === 7) {
        for (const tc of event.toolCalls) {
          calls.push({ name: tc.name, args: tc.args ?? tc.input, done: false });
        }
      } else {
        for (const tc of event.toolCalls) {
          const callIdx = calls.findLastIndex(
            (c) => !c.done && (tc.name === undefined || c.name === tc.name),
          );
          const patch: UiToolCall = {
            name: tc.name ?? calls[callIdx]?.name,
            args: calls[callIdx]?.args ?? tc.args ?? tc.input,
            output: tc.tool_output,
            isError: tc.is_error,
            done: true,
          };
          if (callIdx >= 0) calls[callIdx] = patch;
          else calls.push(patch);
        }
      }
      copy[idx] = { ...m, toolCalls: calls };
      break;
    }
    case "agent": {
      const agent: UiAgentRun = {
        runId: event.runId ?? m.agent?.runId,
        status: event.event,
        plan: event.plan ?? m.agent?.plan,
        steps: event.steps ?? m.agent?.steps ?? [],
        finalResult: event.finalResult ?? m.agent?.finalResult,
        error: event.error ?? m.agent?.error,
      };
      if (event.step && !event.steps) {
        agent.steps = [...(m.agent?.steps ?? []), event.step];
      }
      copy[idx] = { ...m, agent };
      break;
    }
    case "reference":
      copy[idx] = {
        ...m,
        references:
          event.references.length > 0 ? event.references : m.references,
      };
      break;
    case "error":
      copy[idx] = { ...m, error: event.message };
      break;
    case "malformed":
      console.warn("无法解析的 SSE 块:", event.raw.slice(0, 200));
      break;
    case "progress":
      break;
  }
  return copy;
}
