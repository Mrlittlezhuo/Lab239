/**
 * 单条回答的「本轮实际使用了什么」（第四期设计方案 §11.1 / §18.3.3）。
 *
 * 钉住文案必须跟随生成状态：只有模型真正开始生成过才能说"本轮使用了"；
 * prepared / queued 阶段只能说"已准备"。另外钉住快照里的偏好内容与召回理由
 * 都能显示出来——用户要能核对当时注入了什么、为什么这条记忆被用上。
 */
import { describe, expect, it } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

import {
  MemorySnapshotBadge,
  isIncompleteUsage,
  usageHeadline,
} from "@/components/chat/MemorySnapshotBadge";
import type { MemoryUsageManifest } from "@/lib/sse/types";

const SNAPSHOT: MemoryUsageManifest = {
  generation_status: "completed",
  model_started_at: "2026-10-16T10:00:00",
  model_finished_at: "2026-10-16T10:00:06",
  session_context_version: 7,
  summary_type: "rule",
  summary_through_message_id: "msg-100",
  history_boundary_message_id: "msg-101",
  recent_message_ids: ["msg-101", "msg-102"],
  preferences: [{ id: "mem-1", content: "默认使用中文回答" }],
  recalled_memories: [
    {
      id: "mem-9",
      content: "项目采用 LangGraph",
      reason: "与当前问题中的 LangGraph 架构相关",
    },
  ],
};

describe("usageHeadline", () => {
  it("completed：本轮实际使用了这些上下文", () => {
    expect(usageHeadline(SNAPSHOT)).toBe("本轮实际使用的记忆与上下文");
  });

  it("模型未开始生成：只说已准备，不说已使用", () => {
    expect(
      usageHeadline({ generation_status: "prepared", model_started_at: null }),
    ).toBe("上下文已准备，但模型未开始生成");
    expect(usageHeadline({ generation_status: "queued" })).toBe(
      "上下文已准备，但模型未开始生成",
    );
  });

  it("已开始但没完成：保留快照，说明未完成的回答用过", () => {
    expect(
      usageHeadline({
        generation_status: "cancelled",
        model_started_at: "2026-10-16T10:00:00",
      }),
    ).toBe("未完成的回答使用过这些上下文");
  });
});

describe("isIncompleteUsage", () => {
  it.each(["partial", "cancelled", "failed"] as const)(
    "%s 算未完成回答",
    (status) => {
      expect(isIncompleteUsage({ generation_status: status })).toBe(true);
    },
  );

  it("completed / prepared / 没有快照都不算未完成", () => {
    expect(isIncompleteUsage({ generation_status: "completed" })).toBe(false);
    expect(isIncompleteUsage({ generation_status: "prepared" })).toBe(false);
    expect(isIncompleteUsage(undefined)).toBe(false);
  });
});

describe("MemorySnapshotBadge", () => {
  it("展开后能看到偏好内容、召回理由与上下文版本", () => {
    render(<MemorySnapshotBadge usage={SNAPSHOT} />);
    fireEvent.click(screen.getByRole("button", { name: /偏好 1 条/ }));

    expect(screen.getByText("默认使用中文回答")).toBeInTheDocument();
    expect(
      screen.getByText("使用原因：与当前问题中的 LangGraph 架构相关"),
    ).toBeInTheDocument();
    expect(screen.getByText("版本：v7")).toBeInTheDocument();
    expect(
      screen.getByText(/已覆盖到消息 msg-100/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/从消息 msg-101 开始/),
    ).toBeInTheDocument();
  });

  it("本轮关闭个人记忆时如实说明，不显示成“没有记忆”", () => {
    render(
      <MemorySnapshotBadge
        usage={{ generation_status: "completed", memory_disabled_for_turn: true }}
      />,
    );
    expect(screen.getByText("本轮未使用个人记忆")).toBeInTheDocument();
    fireEvent.click(screen.getByText("本轮未使用个人记忆"));
    expect(
      screen.getByText(/本轮你选择了不使用个人记忆/),
    ).toBeInTheDocument();
  });

  it("失败时显示失败阶段与脱敏后的原因", () => {
    render(
      <MemorySnapshotBadge
        usage={{
          generation_status: "failed",
          failure_stage: "retrieval",
          failure_reason: "嵌入模型不可用",
        }}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /偏好 0 条/ }));
    expect(screen.getByText("失败阶段：知识库检索")).toBeInTheDocument();
    expect(screen.getByText("嵌入模型不可用")).toBeInTheDocument();
    expect(screen.getByText("回答失败")).toBeInTheDocument();
  });

  it("历史降级时给出提示，不把缺段当成正常情况", () => {
    render(<MemorySnapshotBadge usage={{ generation_status: "completed", degraded: true }} />);
    fireEvent.click(screen.getByRole("button", { name: /最近原文 0 轮/ }));
    expect(screen.getByText(/本轮历史有降级/)).toBeInTheDocument();
  });
});
