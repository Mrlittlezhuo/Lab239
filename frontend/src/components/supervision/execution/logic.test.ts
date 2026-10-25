import { describe, expect, it } from "vitest";

import type { Attachment, Task, TaskAc } from "@/api/supervision";
import {
  acsMissingEvidence,
  buildLinksPayload,
  buildSelfAssessmentPayload,
  canWithdraw,
  clientCompletenessHints,
  draftList,
  EMPTY_PROGRESS_DRAFT,
  initialAcDrafts,
  joinAcWithAssessment,
  parseAcSnapshot,
  parseCompletenessRows,
  parseLinkRows,
  parseMaterialRows,
  parseSelfAssessmentRows,
  progressHasContent,
  unassessedAcCodes,
} from "@/components/supervision/execution/logic";

function ac(code: string, required = true): TaskAc {
  return {
    ac_code: code,
    statement: `${code} 的验收陈述`,
    evidence_requirement: "需要证据",
    required,
    check_type: "manual",
    version: 1,
  };
}

function task(overrides: Partial<Task> = {}): Task {
  return {
    task_id: "t1",
    code: "T-001",
    title: "复现基线",
    purpose: "",
    description: "",
    status: "active",
    label: "进行中",
    priority: "high",
    version: 1,
    due_at: null,
    requires_code_review: false,
    requires_runtime: false,
    required_materials: [],
    milestone_id: null,
    acs: [ac("AC-1"), ac("AC-2")],
    cancel_reason: "",
    // 契约里这两个字段必填（可为 null）：FastAPI 一定会写出。
    published_at: null,
    create_time: null,
    ...overrides,
  };
}

function attachment(code: string, kind: string): Attachment {
  return {
    attachment_id: code,
    code,
    name: `${code}.zip`,
    ext: "zip",
    mime_type: "application/zip",
    size: 10,
    sha256: "x".repeat(64),
    version: 1,
    kind,
    status: "active",
    task_id: null,
    submission_id: null,
    uploaded_by: "u1",
    // 契约里 create_time 是必填（可为 null）：FastAPI 一定会写出该字段。
    // 之前 fixture 省掉它，等于假装 API 不返回这个字段。
    create_time: null,
  };
}

describe("initialAcDrafts / unassessedAcCodes", () => {
  it("初始草稿覆盖全部 AC，且都算未自评", () => {
    const acs = [ac("AC-1"), ac("AC-2")];
    const drafts = initialAcDrafts(acs);
    expect(draftList(drafts).map((d) => d.ac_code).sort()).toEqual(["AC-1", "AC-2"]);
    expect(unassessedAcCodes(acs, drafts)).toEqual(["AC-1", "AC-2"]);
  });

  it("选了结论的 AC 不再出现在缺自评列表", () => {
    const acs = [ac("AC-1"), ac("AC-2")];
    const drafts = initialAcDrafts(acs);
    drafts["AC-1"].self_assessment = "done";
    expect(unassessedAcCodes(acs, drafts)).toEqual(["AC-2"]);
  });
});

describe("acsMissingEvidence", () => {
  it("声称已完成但没写证据的 AC 被列出", () => {
    const acs = [ac("AC-1"), ac("AC-2"), ac("AC-3")];
    const drafts = initialAcDrafts(acs);
    drafts["AC-1"].self_assessment = "done";
    drafts["AC-2"].self_assessment = "partially_done";
    drafts["AC-2"].evidence = "  ";
    drafts["AC-3"].self_assessment = "not_done";
    expect(acsMissingEvidence(acs, drafts)).toEqual(["AC-1", "AC-2"]);
  });

  it("写了证据或自评不适用时不提醒", () => {
    const acs = [ac("AC-1"), ac("AC-2")];
    const drafts = initialAcDrafts(acs);
    drafts["AC-1"].self_assessment = "done";
    drafts["AC-1"].evidence = "见附件 A-001";
    drafts["AC-2"].self_assessment = "not_applicable";
    expect(acsMissingEvidence(acs, drafts)).toEqual([]);
  });
});

describe("buildSelfAssessmentPayload", () => {
  it("跳过没选结论的行，避免后端 400", () => {
    const drafts = initialAcDrafts([ac("AC-1"), ac("AC-2")]);
    drafts["AC-2"].self_assessment = "partially_done";
    drafts["AC-2"].evidence = "部分结果";
    const payload = buildSelfAssessmentPayload(drafts);
    expect(payload).toEqual([
      { ac_code: "AC-2", self_assessment: "partially_done", evidence: "部分结果", note: "" },
    ]);
  });
});

describe("progressHasContent", () => {
  it("全空时不允许保存进展", () => {
    expect(progressHasContent(EMPTY_PROGRESS_DRAFT)).toBe(false);
    expect(progressHasContent({ ...EMPTY_PROGRESS_DRAFT, done: "   " })).toBe(false);
  });

  it("任意一项非空即可保存", () => {
    expect(progressHasContent({ ...EMPTY_PROGRESS_DRAFT, problems: "卡住了" })).toBe(true);
  });
});

describe("buildLinksPayload", () => {
  it("丢弃没有 URL 的行，标题缺省时用 URL 兜底", () => {
    expect(
      buildLinksPayload([
        { title: "实验记录", url: " https://example.com/log " },
        { title: "", url: "https://example.com/plot" },
        { title: "只有标题", url: "  " },
      ]),
    ).toEqual([
      { title: "实验记录", url: "https://example.com/log" },
      { title: "https://example.com/plot", url: "https://example.com/plot" },
    ]);
  });
});

describe("clientCompletenessHints", () => {
  it("与后端同口径：缺自评、缺必填材料、缺未完成项都给出提醒", () => {
    const t = task({
      required_materials: [
        { key: "result", label: "结果文件", required: true },
        { key: "note", label: "笔记", required: false },
      ],
    });
    const drafts = initialAcDrafts(t.acs);
    drafts["AC-1"].self_assessment = "done";
    const hints = clientCompletenessHints(t, drafts, [], {
      git_commit: "",
      experiment_notes: "",
      unfinished_items: "",
    });
    const byKey = Object.fromEntries(hints.map((h) => [h.key, h]));
    expect(byKey.self_assessment.passed).toBe(false);
    expect(byKey.self_assessment.message).toContain("AC-2");
    expect(byKey.required_materials.passed).toBe(false);
    expect(byKey.required_materials.message).toContain("结果文件");
    expect(byKey.unfinished_items.passed).toBe(false);
    // 任务不要求代码/运行时检查时不出现这两项
    expect(byKey.code_material).toBeUndefined();
    expect(byKey.runtime_material).toBeUndefined();
  });

  it("要求代码检查时：commit 与代码类材料齐了才算通过", () => {
    const t = task({ requires_code_review: true });
    const drafts = initialAcDrafts(t.acs);
    for (const draft of draftList(drafts)) draft.self_assessment = "done";
    const withoutCommit = clientCompletenessHints(t, drafts, [attachment("A-001", "code")], {
      git_commit: "",
      experiment_notes: "",
      unfinished_items: "无",
    });
    expect(withoutCommit.find((h) => h.key === "code_material")?.passed).toBe(false);
    const withCommit = clientCompletenessHints(t, drafts, [attachment("A-001", "archive")], {
      git_commit: "abc123",
      experiment_notes: "",
      unfinished_items: "无",
    });
    expect(withCommit.find((h) => h.key === "code_material")?.passed).toBe(true);
  });

  it("要求运行时检查时：实验说明与日志/结果类材料缺一不可", () => {
    const t = task({ requires_runtime: true });
    const drafts = initialAcDrafts(t.acs);
    for (const draft of draftList(drafts)) draft.self_assessment = "done";
    const hints = clientCompletenessHints(t, drafts, [], {
      git_commit: "",
      experiment_notes: "环境：conda env x；命令：python run.py",
      unfinished_items: "无",
    });
    expect(hints.find((h) => h.key === "runtime_material")?.passed).toBe(false);
  });
});

describe("canWithdraw", () => {
  it("只有提交本人（学生）且状态为 submitted 时显示撤回", () => {
    const submitted = { status: "submitted" as const, author_id: "u1" };
    expect(canWithdraw(submitted, "u1", "student")).toBe(true);
    expect(canWithdraw(submitted, "u2", "student")).toBe(false);
    expect(canWithdraw(submitted, "u1", "mentor")).toBe(false);
    expect(canWithdraw({ status: "withdrawn", author_id: "u1" }, "u1", "student")).toBe(false);
    expect(canWithdraw({ status: "superseded", author_id: "u1" }, "u1", "student")).toBe(false);
    expect(canWithdraw(submitted, null, "student")).toBe(false);
  });
});

describe("后端快照字段解析", () => {
  it("ac_snapshot 缺字段时用默认值兜底，不抛异常", () => {
    const rows = parseAcSnapshot([
      { ac_code: "AC-1", statement: "s", required: true, version: 2 },
      { ac_code: "AC-2" },
    ]);
    expect(rows[0]).toEqual({
      ac_code: "AC-1",
      statement: "s",
      evidence_requirement: "",
      required: true,
      check_type: "",
      version: 2,
    });
    expect(rows[1].required).toBe(false);
    expect(rows[1].version).toBe(0);
  });

  it("self_assessment / materials / completeness_check / links 解析", () => {
    expect(
      parseSelfAssessmentRows([{ ac_code: "AC-1", self_assessment: "done", evidence: "e" }]),
    ).toEqual([{ ac_code: "AC-1", self_assessment: "done", evidence: "e", note: "" }]);
    expect(parseMaterialRows([{ attachment_code: "A-001", name: "n", size: "12" }])).toEqual([
      { attachment_code: "A-001", name: "n", kind: "", version: 0, size: 12 },
    ]);
    expect(
      parseCompletenessRows([
        { key: "self_assessment", passed: false, message: "缺自评" },
        { key: "unfinished_items", passed: true, message: "已列出" },
      ]),
    ).toEqual([
      { key: "self_assessment", passed: false, message: "缺自评" },
      { key: "unfinished_items", passed: true, message: "已列出" },
    ]);
    expect(parseLinkRows([{ title: "t", url: "u" }, { title: "无链接" }])).toEqual([
      { title: "t", url: "u" },
    ]);
    expect(parseAcSnapshot(undefined)).toEqual([]);
  });

  it("joinAcWithAssessment 按 ac_code 关联快照与自评", () => {
    const snapshot = parseAcSnapshot([{ ac_code: "AC-1" }, { ac_code: "AC-2" }]);
    const assessment = parseSelfAssessmentRows([
      { ac_code: "AC-2", self_assessment: "not_done", note: "还没做" },
    ]);
    const joined = joinAcWithAssessment(snapshot, assessment);
    expect(joined[0].assessment).toBeNull();
    expect(joined[1].assessment?.self_assessment).toBe("not_done");
  });
});
