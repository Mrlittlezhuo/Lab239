import { describe, expect, it } from "vitest";

import type { DialogueMessages, ResearchDialogue } from "@/api/supervision";
import {
  DIALOGUE_TITLE_MAX,
  MAX_MESSAGE_CHARS,
  defaultDialogueTitle,
  dialogueLabel,
  dialogueTurns,
  dialogueUpdatedText,
  isPendingReply,
  messageExcerpt,
  messageLengthIssue,
  selectableMessageIds,
} from "@/components/supervision/research/dialogueModel";

type Row = DialogueMessages["messages"][number];

function row(over: Partial<Row> = {}): Row {
  return {
    message_id: "m1",
    query: "我的发言",
    response: "AI 的回答",
    metadata: {},
    create_time: "2026-10-18T09:30:00",
    ...over,
  };
}

function dialogue(over: Partial<ResearchDialogue> = {}): ResearchDialogue {
  return {
    conversation_id: "c1",
    code: "CV-001",
    name: "主研究会话",
    chat_type: "agent_chat",
    supervision_project_id: "p1",
    message_count: 3,
    create_time: "2026-10-18T09:00:00",
    update_time: "2026-10-18T09:30:00",
    ...over,
  };
}

describe("dialogueTurns", () => {
  it("一条消息行拆成成员发言与 AI 回复两轮", () => {
    const turns = dialogueTurns([row()]);
    expect(turns.map((turn) => turn.role)).toEqual(["member", "assistant"]);
    expect(turns[0].text).toBe("我的发言");
    expect(turns[0].time).toBe("2026-10-18 09:30:00");
    expect(turns[1].text).toBe("AI 的回答");
  });

  it("回复未生成时给出待回复轮次，而不是空回答", () => {
    const turns = dialogueTurns([row({ response: "", metadata: { pending_reply: true } })]);
    expect(turns.map((turn) => turn.role)).toEqual(["member", "assistant"]);
    expect(turns[1].pendingReply).toBe(true);
    expect(turns[1].text).toBe("");
  });

  it("没有发言也没有回复的空行不产生轮次", () => {
    expect(dialogueTurns([row({ query: "  ", response: "" })])).toEqual([]);
  });

  it("回复元数据里的模型名、截断与上下文注入被保留", () => {
    const turns = dialogueTurns([
      row({
        metadata: {
          model_name: "strata",
          truncated_response: true,
          project_context_injected: false,
          reply_time: "2026-10-18T09:31:00.123456",
        },
      }),
    ]);
    const assistant = turns[1];
    expect(assistant.modelName).toBe("strata");
    expect(assistant.truncated).toBe(true);
    expect(assistant.contextInjected).toBe(false);
    expect(assistant.time).toBe("2026-10-18 09:31:00");
  });
});

describe("提取范围", () => {
  it("只有成员发言能被选作提取来源", () => {
    const rows = [row({ message_id: "m1" }), row({ message_id: "m2", query: "" })];
    expect(selectableMessageIds(rows)).toEqual(["m1"]);
  });

  it("摘要压成一行并截断", () => {
    const long = "第一段\n第二段 " + "啊".repeat(80);
    expect(messageExcerpt(row({ query: long }), 10)).toBe("第一段 第二段 啊啊…");
  });
});

describe("发送前的本地校验", () => {
  it("空正文与超长正文给出可读说明，正常正文返回 null", () => {
    expect(messageLengthIssue("   ")).toContain("先写下");
    expect(messageLengthIssue("研究问题是什么？")).toBeNull();
    const long = "啊".repeat(MAX_MESSAGE_CHARS + 1);
    expect(messageLengthIssue(long)).toContain(`${MAX_MESSAGE_CHARS} 字`);
    expect(messageLengthIssue("啊".repeat(MAX_MESSAGE_CHARS))).toBeNull();
  });

  it("回复为空的发言算待回复，已有回复的不算", () => {
    expect(isPendingReply(row({ response: "" }))).toBe(true);
    expect(isPendingReply(row())).toBe(false);
    expect(isPendingReply(row({ query: "", response: "" }))).toBe(false);
  });
});

describe("自动建会话的初始标题", () => {
  it("用项目名拼「研究讨论」并按后端上限截断", () => {
    expect(defaultDialogueTitle("中文摘要质量研究")).toBe("中文摘要质量研究 研究讨论");
    expect(defaultDialogueTitle("   ")).toBe("本项目 研究讨论");
    expect(defaultDialogueTitle("啊".repeat(80))).toHaveLength(DIALOGUE_TITLE_MAX);
  });
});

describe("会话文案", () => {
  it("编号与消息数、最近更新时间都来自后端字段", () => {
    expect(dialogueLabel(dialogue())).toBe("CV-001 · 3 条消息");
    expect(dialogueUpdatedText(dialogue())).toBe("更新于 2026-10-18 09:30:00");
    expect(
      dialogueUpdatedText(dialogue({ create_time: null, update_time: null })),
    ).toBe("尚无更新");
  });
});
