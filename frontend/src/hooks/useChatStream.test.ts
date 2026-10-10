import { describe, expect, it } from "vitest";

import { historyToUiMessages } from "@/hooks/useChatStream";
import { messagesToMarkdown } from "@/lib/exportMarkdown";
import type { MessageRecord } from "@/api/conversations";

const record = (over: Partial<MessageRecord>): MessageRecord => ({
  id: "m1",
  chat_type: "agent_chat",
  query: "问题",
  response: "回答",
  metadata: {},
  create_time: null,
  feedback_score: -1,
  feedback_reason: "",
  ...over,
});

describe("historyToUiMessages", () => {
  it("一条记录拆成 user+assistant 两条，assistant 带服务端 id 与反馈", () => {
    const msgs = historyToUiMessages([record({ feedback_score: 100 })]);
    expect(msgs).toHaveLength(2);
    expect(msgs[0]).toMatchObject({ role: "user", content: "问题" });
    expect(msgs[1]).toMatchObject({
      role: "assistant",
      content: "回答",
      serverMessageId: "m1",
      feedbackScore: 100,
    });
  });

  it("metadata.reference_metadata 恢复为引用列表", () => {
    const msgs = historyToUiMessages([
      record({ metadata: { reference_metadata: [{ file_name: "a.pdf" }] } }),
    ]);
    expect(msgs[1].references).toEqual([{ file_name: "a.pdf" }]);
  });

  it("空 response 也保留 assistant 占位（失败轮次）", () => {
    const msgs = historyToUiMessages([record({ response: "" })]);
    expect(msgs[1].content).toBe("");
  });
});

describe("messagesToMarkdown", () => {
  it("包含用户与助手段落", () => {
    const msgs = historyToUiMessages([record({})]);
    const md = messagesToMarkdown(msgs, "标题");
    expect(md).toContain("# 标题");
    expect(md).toContain("## 用户");
    expect(md).toContain("问题");
    expect(md).toContain("回答");
  });
});
