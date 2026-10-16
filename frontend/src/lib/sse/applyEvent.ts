import type { AgentStepData, SseEvent } from "@/lib/sse/types";
import type { UiAgentRun, UiMessage, UiToolCall } from "@/hooks/useChatStream";

/**
 * Agent 流（`/chat/chat/completions`）会把同一段文字重复推好几遍，`status` 取自
 * 后端 `AgentStatus`（langchain_chatchat/callbacks/agent_callback_handler.py）：
 *   2 llm_new_token 逐 token 正文；3 llm_end 本轮全文回放；4 agent_action 本轮 log；
 *   5 agent_finish 最终答案全文；-999 chain_end 最终答案全文。
 * 3/4 与已经流出来的 token 完全重复，一律当正文追加就会渲染出两三份同样的内容
 * （库里只存一份，所以刷新后看起来恢复正常）。
 */
const REPLAY_STATUSES = new Set([3, 4]);
/** 收尾块：正文一个字都没流出来时才拿来兜底，否则同样重复。 */
const FINAL_STATUSES = new Set([5, -999]);

/** 服务端步骤标识（agent_step.seq）；旧载荷里的 step 作为兜底。 */
function stepKey(s: AgentStepData): number | undefined {
  return s.seq ?? s.step;
}

/**
 * 步骤时间线合并。
 *
 * 同一步会先发 ``step_started``（running）再发 ``step_finished``
 * （completed/failed），服务端用同一个 ``seq`` 标识，因此必须落到同一行。
 * 早先的写法是直接 append，界面上就变成"步骤 1 没勾 + 步骤 2 已勾"两行。
 * ``replanned`` 只带新补充的步骤，同样不能把已完成的历史步骤整组替换掉。
 */
function mergeSteps(
  existing: AgentStepData[],
  event: Extract<SseEvent, { kind: "agent" }>,
): AgentStepData[] {
  const out = [...existing];
  const put = (s: AgentStepData) => {
    const key = stepKey(s);
    const at = key === undefined ? -1 : out.findIndex((x) => stepKey(x) === key);
    if (at >= 0) out[at] = { ...out[at], ...s };
    else out.push(s);
  };
  if (event.step && !event.steps) put(event.step);
  else if (event.steps) for (const s of event.steps) put(s);
  return out;
}

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
    case "token": {
      // 见上面 REPLAY_STATUSES / FINAL_STATUSES 的说明：只有逐 token 帧无条件追加。
      // RAG 与直答流的 status 为 null，两个集合都不命中，行为不变。
      let added = event.content;
      if (event.status !== null && event.status !== undefined) {
        if (REPLAY_STATUSES.has(event.status)) added = "";
        else if (FINAL_STATUSES.has(event.status) && m.content) added = "";
      }
      copy[idx] = {
        ...m,
        content: m.content + added,
        serverMessageId: event.messageId ?? m.serverMessageId,
      };
      break;
    }
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
      // 正文预览：模型还在生成时把已解码的正文增量堆到运行面板上。它不
      // 改变运行状态，也不写进消息正文——正文只认 run_completed 的最终答案。
      if (event.event === "plan_delta" || event.event === "answer_delta") {
        const prev = m.agent;
        const preview: UiAgentRun = {
          runId: event.runId ?? prev?.runId,
          status: prev?.status ?? "run_created",
          plan: prev?.plan,
          steps: prev?.steps ?? [],
          finalResult: prev?.finalResult,
          error: prev?.error,
          livePlan: prev?.livePlan,
          liveAnswer: prev?.liveAnswer,
        };
        if (event.event === "plan_delta") {
          preview.livePlan = (preview.livePlan ?? "") + event.content;
        } else {
          preview.liveAnswer = (preview.liveAnswer ?? "") + event.content;
        }
        copy[idx] = { ...m, agent: preview };
        break;
      }
      const terminal =
        event.event === "run_completed" ||
        event.event === "run_failed" ||
        event.event === "run_cancelled";
      const agent: UiAgentRun = {
        runId: event.runId ?? m.agent?.runId,
        status: event.event,
        plan: event.plan ?? m.agent?.plan,
        steps: mergeSteps(m.agent?.steps ?? [], event),
        finalResult: event.finalResult ?? m.agent?.finalResult,
        error: event.error ?? m.agent?.error,
        // 到终态就丢掉预览：正文改用最终答案，同一段字不显示两遍
        livePlan: terminal ? undefined : m.agent?.livePlan,
        liveAnswer: terminal ? undefined : m.agent?.liveAnswer,
      };
      // Agent 轮的回答此前只有刷新后（从库里读）才出现在正文里，流式期间
      // 正文一直是空的；终态时把最终答案落到正文。
      const content =
        event.event === "run_completed" && event.finalResult && !m.content.trim()
          ? event.finalResult
          : m.content;
      copy[idx] = { ...m, content, agent };
      break;
    }
    case "memory":
      // 本轮实际使用的上下文快照。流式期间会收到两帧（模型调用前的 prepared
      // 与收尾时的最终状态），后一帧整体替换前一帧，状态文案因此总是跟随
      // 最新状态，不会停在"上下文已准备"（第四期 §11.1 / §18.3.3）。
      copy[idx] = {
        ...m,
        memoryUsage: event.manifest,
        serverMessageId: event.messageId ?? m.serverMessageId,
      };
      break;
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
