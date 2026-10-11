import { describe, expect, it } from "vitest";

import { applySseEvent } from "@/lib/sse/applyEvent";
import { classifyFrame } from "@/lib/sse/classify";
import type { UiMessage } from "@/hooks/useChatStream";

/**
 * Agent 流（/chat/chat/completions）会把同一段文字重复推好几遍：逐 token、
 * llm_end 本轮全文、agent_action 的 log、agent_finish 与 chain_end 的最终答案。
 * 全部当正文追加就会出现"同样的内容展示了三份"，而库里只存一份，
 * 所以刷新后反而正常。这里钉住：正文只认逐 token，收尾块只兜底。
 */

const frame = (o: Record<string, unknown>) =>
  JSON.stringify({
    object: "chat.completion.chunk",
    role: "assistant",
    message_id: "m1",
    ...o,
  });

const delta = (content: string, status: number) =>
  frame({ status, choices: [{ delta: { content, tool_calls: [] }, role: "assistant" }] });

const history = (): UiMessage[] => [
  { key: "u1", role: "user", content: "最近什么剧最火", toolCalls: [] },
  { key: "a1", role: "assistant", content: "", toolCalls: [], streaming: true },
];

function run(frames: string[]): UiMessage {
  let messages = history();
  for (const f of frames) messages = applySseEvent(messages, classifyFrame(f));
  return messages[messages.length - 1];
}

describe("applySseEvent token 去重", () => {
  it("Agent 一轮完整流：正文只渲染一遍", () => {
    const answer = "最近最火的是《XX》";
    const msg = run([
      delta("最近最火的是", 2),
      delta("《XX》", 2),
      // status 3 llm_end：本轮全文回放
      delta(answer, 3),
      // status 5 agent_finish / status -999 chain_end：最终答案全文
      delta(answer, 5),
      delta(answer, -999),
    ]);
    expect(msg.content).toBe(answer);
  });

  it("多轮工具调用的 Thought 也不会重复", () => {
    const thought = "Thought: 需要联网搜索\n";
    const answer = "答案是《XX》";
    const msg = run([
      delta(thought, 2),
      delta(thought, 3),
      // status 4 agent_action：本轮 log
      delta(thought, 4),
      delta(answer, 2),
      delta(answer, 3),
      delta(answer, 5),
    ]);
    expect(msg.content).toBe(thought + answer);
  });

  it("整轮没流出一个 token 时，用 agent_finish 兜底，气泡不空白", () => {
    const msg = run([delta("", 2), delta("推理模型只回收尾块", 5)]);
    expect(msg.content).toBe("推理模型只回收尾块");
  });

  it("llm_end 与 agent_action 不参与兜底，避免把中间轮脚手架当正文", () => {
    const msg = run([
      delta("Thought: 调用工具\nAction: search_internet", 3),
      delta("Thought: 调用工具\nAction: search_internet", 4),
    ]);
    expect(msg.content).toBe("");
  });

  it("RAG 与直答流（status null）照常追加", () => {
    const msg = run([
      frame({ status: null, choices: [{ delta: { content: "一段" } }] }),
      frame({ status: null, choices: [{ delta: { content: "二段" } }] }),
    ]);
    expect(msg.content).toBe("一段二段");
  });

  it("message_id 仍被采纳，即使该帧的文字被忽略", () => {
    const msg = run([delta("正文", 2), frame({ status: 5, message_id: "m9", choices: [{ delta: { content: "正文" } }] })]);
    expect(msg.content).toBe("正文");
    expect(msg.serverMessageId).toBe("m9");
  });

  it("工具卡片按 status 7/8 建卡与收尾，不受正文去重影响", () => {
    const msg = run([
      delta("Thought: 搜索\n", 2),
      frame({
        status: 7,
        choices: [
          {
            delta: {
              content: "",
              tool_calls: [
                {
                  index: 0,
                  id: "run1",
                  type: "function",
                  function: { name: "search_internet", arguments: '{"query":"最火的电视剧"}' },
                  tool_output: null,
                  is_error: false,
                },
              ],
            },
          },
        ],
      }),
      frame({
        status: 8,
        choices: [
          {
            delta: {
              content: "",
              tool_calls: [
                {
                  index: 0,
                  id: "run1",
                  type: "function",
                  function: { name: "search_internet", arguments: '{"query":"最火的电视剧"}' },
                  tool_output: '{"results":[]}',
                  is_error: false,
                },
              ],
            },
          },
        ],
      }),
    ]);
    expect(msg.content).toBe("Thought: 搜索\n");
    expect(msg.toolCalls).toHaveLength(1);
    expect(msg.toolCalls[0]).toMatchObject({
      name: "search_internet",
      args: '{"query":"最火的电视剧"}',
      output: '{"results":[]}',
      done: true,
    });
  });
});

/**
 * Agent 任务的正文藏在模型流出的 JSON 里，后端把已经能确定的部分作为
 * `plan_delta` / `answer_delta` 提前推过来。预览只显示在运行面板里，
 * 消息正文只认 `run_completed` 的最终答案（预览可能来自被重试掉的输出）。
 */
const agentFrame = (o: Record<string, unknown>) =>
  frame({
    class_name: "AgentRunEvent",
    run_id: "r1",
    ...o,
  });

const agentDelta = (event: string, content: string) =>
  agentFrame({ agent_event: event, choices: [{ delta: { content } }] });

describe("applySseEvent Agent 正文预览", () => {
  it("plan_delta / answer_delta 只累积预览，不写正文也不改状态", () => {
    const msg = run([
      agentFrame({ agent_event: "run_created" }),
      agentDelta("plan_delta", "查一下明天天气"),
      agentDelta("answer_delta", "上海明天"),
      agentDelta("answer_delta", "晴，24℃"),
    ]);
    expect(msg.content).toBe("");
    expect(msg.agent?.status).toBe("run_created");
    expect(msg.agent?.livePlan).toBe("查一下明天天气");
    expect(msg.agent?.liveAnswer).toBe("上海明天晴，24℃");
  });

  it("预览不打断步骤时间线", () => {
    const msg = run([
      agentFrame({
        agent_event: "step_started",
        agent_step: { seq: 1, tool_name: "search_internet", status: "running" },
      }),
      agentDelta("answer_delta", "边判断边出字"),
      agentFrame({
        agent_event: "step_finished",
        agent_step: { seq: 1, tool_name: "search_internet", status: "completed" },
      }),
    ]);
    expect(msg.agent?.steps).toHaveLength(2);
    expect(msg.agent?.liveAnswer).toBe("边判断边出字");
  });

  it("run_completed：最终答案落到正文，预览清空", () => {
    const answer = "上海明天晴，24℃，适合出门。";
    const msg = run([
      agentDelta("answer_delta", "上海明天晴，24℃"),
      agentFrame({
        agent_event: "run_completed",
        agent_final_result: answer,
        choices: [{ delta: { content: answer } }],
      }),
    ]);
    expect(msg.content).toBe(answer);
    expect(msg.agent?.liveAnswer).toBeUndefined();
    expect(msg.agent?.livePlan).toBeUndefined();
    expect(msg.agent?.status).toBe("run_completed");
  });

  it("正文已有内容时 run_completed 不再写一遍", () => {
    let messages: UiMessage[] = [
      { key: "u1", role: "user", content: "目标", toolCalls: [] },
      { key: "a1", role: "assistant", content: "已有正文", toolCalls: [], streaming: true },
    ];
    messages = applySseEvent(
      messages,
      classifyFrame(
        agentFrame({ agent_event: "run_completed", agent_final_result: "最终答案" }),
      ),
    );
    expect(messages[messages.length - 1].content).toBe("已有正文");
  });

  it("失败终态也清空预览，错误仍显示在面板里", () => {
    const msg = run([
      agentDelta("answer_delta", "写到一半"),
      agentFrame({ agent_event: "run_failed", agent_error: "模型输出无效" }),
    ]);
    expect(msg.agent?.liveAnswer).toBeUndefined();
    expect(msg.agent?.error).toBe("模型输出无效");
    expect(msg.content).toBe("");
  });
});
