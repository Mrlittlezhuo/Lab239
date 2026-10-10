import { describe, expect, it } from "vitest";

import { classifyFrame } from "@/lib/sse/classify";

const chunk = (o: Record<string, unknown>) => JSON.stringify(o);

describe("classifyFrame", () => {
  it("非法 JSON → malformed", () => {
    expect(classifyFrame("{not json")).toEqual({ kind: "malformed", raw: "{not json" });
  });

  it("普通 token 块", () => {
    const e = classifyFrame(
      chunk({
        object: "chat.completion.chunk",
        status: 2,
        message_id: "m1",
        choices: [{ delta: { content: "你好", tool_calls: [] }, role: "assistant" }],
      }),
    );
    expect(e).toMatchObject({ kind: "token", content: "你好", status: 2, messageId: "m1" });
  });

  it("status null 的纯文本块也是 token", () => {
    const e = classifyFrame(
      chunk({ choices: [{ delta: { content: "x" } }], status: null }),
    );
    expect(e).toMatchObject({ kind: "token", content: "x", status: null });
  });

  it("裸 error 块优先于一切", () => {
    const e = classifyFrame(chunk({ error: "模型服务不可用" }));
    expect(e).toEqual({ kind: "error", message: "模型服务不可用" });
  });

  it("status -1 → error", () => {
    const e = classifyFrame(
      chunk({ status: -1, choices: [{ delta: { content: "出错了" } }] }),
    );
    expect(e).toMatchObject({ kind: "error", message: "出错了" });
  });

  it("AgentRunEvent 先于 token（status null 带 content）", () => {
    const e = classifyFrame(
      chunk({
        class_name: "AgentRunEvent",
        agent_event: "plan_ready",
        run_id: "r1",
        content: "计划摘要",
        agent_plan: { summary: "计划摘要" },
        status: null,
      }),
    );
    expect(e).toMatchObject({
      kind: "agent",
      event: "plan_ready",
      runId: "r1",
      content: "计划摘要",
    });
  });

  it("八种 agent_event 都能分类", () => {
    for (const ev of [
      "run_created",
      "plan_ready",
      "step_started",
      "step_finished",
      "replanned",
      "run_completed",
      "run_failed",
      "run_cancelled",
    ]) {
      const e = classifyFrame(chunk({ class_name: "AgentRunEvent", agent_event: ev }));
      expect(e).toMatchObject({ kind: "agent", event: ev });
    }
  });

  it("RouteResult 块", () => {
    const e = classifyFrame(
      chunk({
        class_name: "RouteResult",
        route_result: { path: "agent", intent: "tool_use", confidence: 0.9 },
        execution_mode: "single_tool_task",
        execution_mode_label: "单工具任务",
        conversation_id: "c1",
      }),
    );
    expect(e).toMatchObject({
      kind: "route",
      executionMode: "single_tool_task",
      conversationId: "c1",
      route: { confidence: 0.9 },
    });
  });

  it("RAG 引用首块", () => {
    const e = classifyFrame(
      chunk({
        references: [{ index: 1, file_name: "a.pdf", score: 0.5 }],
        docs: ["content"],
        retrieval_status: "matched",
        choices: [{ delta: { content: "" } }],
      }),
    );
    expect(e).toMatchObject({
      kind: "reference",
      retrievalStatus: "matched",
      references: [{ file_name: "a.pdf" }],
    });
  });

  it("recreate 进度块", () => {
    const e = classifyFrame(chunk({ code: 200, msg: "", total: 10, finished: 3, doc: "a.pdf" }));
    expect(e).toEqual({ kind: "progress", total: 10, finished: 3, doc: "a.pdf" });
  });

  it("status 7 工具开始（顶层 tool_calls）", () => {
    const e = classifyFrame(
      chunk({
        status: 7,
        tool_calls: [{ name: "search_internet", args: { query: "x" } }],
        choices: [{ delta: { content: "", tool_calls: [] } }],
      }),
    );
    expect(e).toMatchObject({ kind: "tool", status: 7, toolCalls: [{ name: "search_internet" }] });
  });

  it("status 8 工具结束带 tool_output/is_error", () => {
    const e = classifyFrame(
      chunk({
        status: 8,
        tool_calls: [{ name: "t", tool_output: "结果", is_error: false }],
      }),
    );
    expect(e).toMatchObject({
      kind: "tool",
      status: 8,
      toolCalls: [{ tool_output: "结果", is_error: false }],
    });
  });

  it("任意块上的 conversation_id 被采纳", () => {
    const e = classifyFrame(chunk({ status: 2, choices: [{ delta: { content: "a" } }], conversation_id: "c9" }));
    expect(e).toMatchObject({ kind: "token", conversationId: "c9" });
  });

  it("AgentRunResult 非流式终块（choices message）", () => {
    const e = classifyFrame(
      chunk({
        object: "chat.completion",
        class_name: "AgentRunResult",
        agent_event: "run_completed",
        run_id: "r2",
        choices: [{ message: { role: "assistant", content: "最终答案" } }],
        conversation_id: "c1",
      }),
    );
    expect(e).toMatchObject({ kind: "agent", event: "run_completed", content: "最终答案", conversationId: "c1" });
  });
});
