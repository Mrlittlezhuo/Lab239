import { describe, expect, it } from "vitest";

import type { Memory } from "@/api/memory";
import {
  MEMORY_CATEGORIES,
  UNCATEGORIZED_LABEL,
  categoryLabel,
  groupMemories,
} from "@/components/memory/categories";

function memory(id: string, category: string | null, content = id): Memory {
  return {
    id,
    memory_key: `manual:${id}`,
    type: category ? "preference" : "fact",
    category: category as Memory["category"],
    content,
    source: "user",
    importance: 3,
    enabled: true,
  };
}

describe("categoryLabel", () => {
  it("已知类别给出中文标签", () => {
    expect(categoryLabel("answer_style")).toBe("回答偏好");
    expect(categoryLabel("long_term_goal")).toBe("长期目标");
  });

  it("无类别/未知类别归到“其他记忆”", () => {
    expect(categoryLabel(null)).toBe(UNCATEGORIZED_LABEL);
    expect(categoryLabel("whatever")).toBe(UNCATEGORIZED_LABEL);
  });
});

describe("groupMemories", () => {
  it("按固定类别顺序分组，空组不出现", () => {
    const groups = groupMemories([
      memory("g1", "long_term_goal"),
      memory("a1", "answer_style"),
      memory("a2", "answer_style"),
    ]);
    expect(groups.map((g) => g.key)).toEqual(["answer_style", "long_term_goal"]);
    expect(groups[0].items.map((m) => m.id)).toEqual(["a1", "a2"]);
  });

  it("无类别的自动提取记忆排在最后并单独说明", () => {
    const groups = groupMemories([
      memory("u1", null),
      memory("w1", "work_style"),
    ]);
    expect(groups.map((g) => g.label)).toEqual(["工作偏好", UNCATEGORIZED_LABEL]);
    expect(groups[1].items.map((m) => m.id)).toEqual(["u1"]);
    expect(groups[1].hint).toContain("系统从对话中提取");
  });

  it("空列表不产生分组", () => {
    expect(groupMemories([])).toEqual([]);
  });

  it("类别清单与服务端 VALID_CATEGORIES 一致", () => {
    expect(MEMORY_CATEGORIES.map((c) => c.value)).toEqual([
      "answer_style",
      "technical_background",
      "work_style",
      "long_term_goal",
    ]);
  });
});
