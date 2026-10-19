import { describe, expect, it } from "vitest";

import type {
  BriefComparison,
  BriefVersion,
  ResearchBrief,
} from "@/api/supervision";
import { ApiError } from "@/lib/errors";
import {
  briefContentPayload,
  canConfirmBrief,
  canDiscardBrief,
  canRejectBrief,
  changedContentFields,
  comparisonRows,
  confirmBlockedReason,
  contentFromVersion,
  emptyBriefContent,
  entryTypeLabel,
  isModelUnavailable,
  isNotFound,
  listToText,
  MODEL_UNAVAILABLE_HINT,
  sharedCannotRemoveNote,
  sharedVersionNote,
  shownVersionNote,
  splitComparisonRows,
  statusLabel,
  textToList,
  versionChoices,
} from "@/components/supervision/research/briefModel";

function brief(over: Partial<ResearchBrief> = {}): ResearchBrief {
  return {
    brief_id: "b1",
    code: "RB-001",
    display_code: "RB-001-v1",
    project_id: "p1",
    owner_user_id: "u1",
    owner_role: "student",
    kind: "research_brief",
    title: "中文摘要评估",
    status: "draft",
    version_status: "draft",
    entry_type: "mixed",
    current_version: 1,
    shared_version: null,
    shown_version: 1,
    confirmed_by: "",
    confirmed_at: null,
    confirm_note: "",
    create_time: null,
    update_time: null,
    is_owner: true,
    can_edit: true,
    source_conversation_id: null,
    source_message_ids: [],
    background: "",
    question: "",
    objectives: "",
    subjects: "",
    scope: "",
    prior_attempts: "",
    hypotheses: "",
    constraints: "",
    methods: "",
    metrics: "",
    expected_outcomes: "",
    to_verify: "",
    keywords: [],
    sub_questions: [],
    ...over,
  };
}

function version(no: number, over: Partial<BriefVersion> = {}): BriefVersion {
  return {
    version: no,
    status: "draft",
    entry_type: "mixed",
    edited_fields: [],
    confirmed_by: "",
    confirmed_at: null,
    confirm_note: "",
    superseded_at: null,
    create_time: null,
    source_message_ids: [],
    // 后端 BriefVersionOut 一定带这 14 个字段（缺省是空串/空数组），
    // 所以夹具也补齐；只有"库里存了 null"这种防御分支才另外造。
    ...emptyBriefContent(),
    ...over,
  };
}

describe("状态与类型标签", () => {
  it("只翻译后端返回值，未知值原样显示", () => {
    expect(statusLabel("draft")).toBe("草稿");
    expect(statusLabel("confirmed")).toBe("已确认");
    expect(statusLabel("rejected")).toBe("已否定");
    expect(statusLabel("superseded")).toBe("已被新版本替代");
    expect(statusLabel("weird")).toBe("weird");
    expect(entryTypeLabel("to_verify_judgement")).toBe("待验证判断");
    expect(entryTypeLabel("unknown_type")).toBe("unknown_type");
  });
});

describe("列表字段与文本互转", () => {
  it("一行一条，去空去重", () => {
    expect(textToList(" 召回率 \n\n召回率\n 命中率 ")).toEqual(["召回率", "命中率"]);
    expect(listToText(["a", "b"])).toBe("a\nb");
    expect(textToList("")).toEqual([]);
  });

  it("提交载荷保留 14 个内容字段", () => {
    const content = emptyBriefContent();
    content.question = "怎样评估中文摘要？";
    content.keywords = ["中文摘要", "评估"];
    const payload = briefContentPayload(content);
    expect(payload.question).toBe("怎样评估中文摘要？");
    expect(payload.keywords).toEqual(["中文摘要", "评估"]);
    expect(Object.keys(payload)).toHaveLength(14);
  });
});

describe("脏字段检测", () => {
  it("文本与列表字段都能发现变化", () => {
    const before = emptyBriefContent();
    const after = emptyBriefContent();
    expect(changedContentFields(before, after)).toEqual([]);
    after.question = "新问题";
    after.sub_questions = ["子问题一"];
    expect(changedContentFields(before, after)).toEqual(["question", "sub_questions"]);
  });

  it("只在末尾加空格不算改动（后端会 strip）", () => {
    const before = emptyBriefContent();
    const after = emptyBriefContent();
    after.scope = "中文论文  ";
    before.scope = "中文论文";
    expect(changedContentFields(before, after)).toEqual([]);
  });
});

describe("版本行内容收敛", () => {
  it("索引签名里的 unknown 字段被收敛成字符串与字符串数组", () => {
    // 故意造库里存了 null 的旧行：走 `as unknown as` 才能既测防御分支、
    // 又不放宽 API 类型（后端正常响应里这些字段不会是 null）。
    const row = version(2, {
      status: "confirmed",
      question: "核心问题",
      keywords: ["a", " ", "b"],
      sub_questions: null,
    } as unknown as Partial<BriefVersion>);
    const content = contentFromVersion(row);
    expect(content.question).toBe("核心问题");
    expect(content.keywords).toEqual(["a", "b"]);
    expect(content.sub_questions).toEqual([]);
  });
});

describe("版本比较", () => {
  it("默认从最近一次确认的一版比到最新一版", () => {
    expect(
      versionChoices([version(1), version(2, { status: "confirmed" }), version(3, { status: "confirmed" })]),
    ).toEqual({ from: 2, to: 3 });
    // 已共享 v2、又改出草稿 v3：默认看 v2 → v3，也就是对方正在读的那一版差在哪。
    expect(
      versionChoices([
        version(1, { status: "confirmed" }),
        version(2, { status: "confirmed" }),
        version(3, { status: "draft" }),
      ]),
    ).toEqual({ from: 2, to: 3 });
    expect(versionChoices([version(1), version(2)])).toEqual({ from: 1, to: 2 });
    expect(versionChoices([version(1)])).toBeNull();
  });

  it("文本字段按整段显示，列表字段按条目显示增删", () => {
    // 后端 diff_brief_content 对列表字段返回数组，接口类型里声明的却是字符串。
    const comparison = {
      brief_id: "b1",
      code: "RB-001",
      from_version: 1,
      to_version: 2,
      from_status: "confirmed",
      to_status: "draft",
      changes: [
        { field: "question", label: "核心研究问题", from: "旧问题", to: "新问题" },
        {
          field: "keywords",
          label: "需要检索的关键词",
          from: ["中文摘要", "旧词"],
          to: ["中文摘要", "新词"],
          added: ["新词"],
          removed: ["旧词"],
        },
        { field: "sub_questions", from: ["甲"], to: ["甲", "乙"] },
      ],
    } as unknown as BriefComparison;

    const rows = comparisonRows(comparison);
    expect(rows[0]).toMatchObject({
      field: "question",
      label: "核心研究问题",
      kind: "text",
      fromText: "旧问题",
      toText: "新问题",
      added: [],
      removed: [],
    });
    expect(rows[1]).toMatchObject({
      field: "keywords",
      kind: "list",
      fromText: "中文摘要、旧词",
      toText: "中文摘要、新词",
      added: ["新词"],
      removed: ["旧词"],
    });
    // 后端没给 added/removed 时前端自己算差集。
    expect(rows[2].added).toEqual(["乙"]);
    expect(rows[2].removed).toEqual([]);

    const grouped = splitComparisonRows(rows);
    expect(grouped.textRows.map((row) => row.field)).toEqual(["question"]);
    expect(grouped.listRows.map((row) => row.field)).toEqual(["keywords", "sub_questions"]);
  });

  it("缺 label 时回退到前端字段标签", () => {
    const rows = comparisonRows({
      changes: [{ field: "metrics", from: "", to: "F1" }],
    } as unknown as BriefComparison);
    expect(rows[0].label).toBe("评价指标");
  });
});

describe("版本与共享说明", () => {
  it("当前版本与已共享版本不一致时提示另一方仍在读旧版", () => {
    expect(sharedVersionNote(brief({ current_version: 3, shared_version: 2 }))).toContain(
      "另一方读到的仍然是 v2",
    );
    expect(sharedVersionNote(brief({ current_version: 2, shared_version: 2 }))).toBeNull();
    expect(sharedVersionNote(brief())).toBeNull();
    // 另一方读不到 current_version 那一版，不该对他说「你当前的草稿」。
    expect(
      sharedVersionNote(brief({ current_version: 3, shared_version: 2, is_owner: false })),
    ).toBeNull();
  });

  it("非所有者只看到已共享的那一版", () => {
    const other = brief({ is_owner: false, shared_version: 2, shown_version: 2, status: "confirmed" });
    expect(shownVersionNote(other)).toContain("已确认共享的 v2");
    expect(shownVersionNote(brief())).toBeNull();
  });
});

describe("操作可用性只看后端状态", () => {
  it("只有草稿能确认，且必须先有核心研究问题", () => {
    expect(canConfirmBrief(brief())).toBe(true);
    expect(confirmBlockedReason(brief())).toBe("确认共享前必须填写核心研究问题。");
    expect(confirmBlockedReason(brief({ question: "问题" }))).toBeNull();
    expect(canConfirmBrief(brief({ status: "confirmed", question: "问题" }))).toBe(false);
    expect(confirmBlockedReason(brief({ is_owner: false, question: "问题" }))).toContain("只有创建者");
  });

  it("已共享的定义不能否定或丢弃", () => {
    expect(canRejectBrief(brief())).toBe(true);
    expect(canDiscardBrief(brief())).toBe(true);
    const shared = brief({ status: "confirmed", shared_version: 1, question: "问题" });
    expect(canRejectBrief(shared)).toBe(false);
    expect(canDiscardBrief(shared)).toBe(false);
    expect(sharedCannotRemoveNote(shared)).toContain("项目审计资产");
    expect(canRejectBrief(brief({ status: "rejected" }))).toBe(false);
  });
});

describe("后端错误分类", () => {
  it("503 显示成模型未接入说明，404 显示成无权访问", () => {
    expect(isModelUnavailable(new ApiError(503, "模型能力尚未接入"))).toBe(true);
    expect(isModelUnavailable(new ApiError(409, "已共享不能丢弃"))).toBe(false);
    expect(isNotFound(new ApiError(404, "研究定义不存在"))).toBe(true);
    expect(isNotFound(new Error("网络断了"))).toBe(false);
    expect(MODEL_UNAVAILABLE_HINT).toContain("手动填写研究定义");
  });
});
