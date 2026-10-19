import { describe, expect, it } from "vitest";

import type {
  LatexDraftItem,
  LatexFinding,
  LatexProgressDraft,
  LatexSnapshot,
  LatexSourceLocation,
  ProjectDetail,
} from "@/api/supervision";
import {
  canConfirmDraft,
  canDiscardDraft,
  candidatePaths,
  countsLine,
  describeBlocked,
  draftSummaryLine,
  findingLine,
  itemLine,
  locationText,
  needsMainFileChoice,
  snapshotProblemLines,
} from "@/components/supervision/latex/latexModel";
import { ApiError } from "@/lib/errors";

function location(over: Partial<LatexSourceLocation> = {}): LatexSourceLocation {
  return {
    path: "sections/intro.tex",
    line: 12,
    end_line: 12,
    snippet: "...",
    ...over,
  };
}

function finding(over: Partial<LatexFinding> = {}): LatexFinding {
  return {
    kind: "undefined_reference",
    severity: "warning",
    message: "引用了不存在的标签 fig:result",
    path: "main.tex",
    line: 40,
    end_line: 41,
    snippet: "\\ref{fig:result}",
    target: "fig:result",
    detail: "",
    ...over,
  };
}

function snapshot(over: Partial<LatexSnapshot> = {}): LatexSnapshot {
  return {
    snapshot_id: "s1",
    code: "LX-001",
    project_id: "p1",
    attachment_code: "ATT-001",
    attachment_name: "paper.zip",
    attachment_sha256: "abc",
    attachment_version: 3,
    main_file: "main.tex",
    main_path: "main.tex",
    main_status: "resolved",
    main: {},
    candidates: [],
    parser_version: "v1",
    checker_version: "v1",
    zip_safety_version: "v1",
    limits: {},
    parse_budget: {},
    document_present: true,
    compiled: false,
    counts: { sections: 6, figures: 3 },
    outline: ["引言", "方法"],
    word_count: 4200,
    findings: [],
    finding_counts: {},
    dangerous_findings: [],
    partially_read: [],
    not_read: [],
    unreferenced_tex: [],
    available_files: [],
    extracted_files: [],
    file_count: 12,
    total_bytes: 1024,
    created_by: "u1",
    created_role: "student",
    create_time: null,
    ...over,
  };
}

function draft(over: Partial<LatexProgressDraft> = {}): LatexProgressDraft {
  return {
    draft_id: "d1",
    code: "LD-001",
    project_id: "p1",
    library_draft_id: "",
    status: "draft",
    published: false,
    requires_student_confirmation: true,
    baseline_only: false,
    draft_version: "v1",
    parser_version: "v1",
    checker_version: "v1",
    zip_safety_version: "v1",
    snapshot_code: "LX-002",
    base_snapshot_code: "LX-001",
    attachment_code: "ATT-002",
    attachment_sha256: "def",
    attachment_version: 4,
    base_attachment_code: "ATT-001",
    base_attachment_version: 3,
    base_attachment_sha256: "abc",
    main_file: "main.tex",
    base_main_file: "main.tex",
    summary: "",
    caveat: "静态差异不能证明实验已执行。",
    items: [],
    item_count: 0,
    truncated: false,
    dropped_items: 0,
    counts: { added_sections: 2, removed_sections: 0, modified_sections: 1, resource_changes: 3 },
    word_count_old: 4200,
    word_count_new: 4600,
    word_count_delta: 400,
    model_summary: "",
    model_summary_status: "",
    model_summary_note: "",
    model_name: "",
    confirmed_progress_code: "",
    confirmed_by: "",
    confirmed_role: "",
    confirmed_at: null,
    discarded_by: "",
    discarded_role: "",
    discarded_at: null,
    discard_reason: "",
    created_by: "u1",
    created_role: "student",
    create_time: null,
    update_time: null,
    ...over,
  };
}

function project(role: "mentor" | "student"): ProjectDetail {
  return {
    project_id: "p1",
    code: "PRJ-001",
    name: "论文投稿",
    description: "",
    status: "active",
    my_role: role,
    members: [],
  };
}

describe("latexModel 文案与展示", () => {
  it("counts 已知键翻成中文，未知键原样保留（不猜含义）", () => {
    expect(countsLine({ sections: 6, figures: 3 })).toBe("章节 6 · 图 3");
    expect(countsLine({ future_key: 1 })).toBe("future_key 1");
    expect(countsLine({})).toBe("");
  });

  it("定位：同一行只写一个行号，跨行写区间，缺路径时显式打问号", () => {
    expect(locationText(location())).toBe("sections/intro.tex L12");
    expect(locationText(location({ line: 12, end_line: 18 }))).toBe("sections/intro.tex L12-L18");
    expect(locationText(location({ path: "" }))).toBe("? L12");
  });

  it("问题条目带严重级别中文与位置；没有路径就不编造位置", () => {
    expect(findingLine(finding())).toBe("提醒：引用了不存在的标签 fig:result（main.tex L40-L41）");
    expect(findingLine(finding({ severity: "error", path: "", line: 0, end_line: 0 }))).toBe(
      "错误：引用了不存在的标签 fig:result",
    );
  });

  it("差异条目把多个定位都列出来，不只显示第一个", () => {
    const item: LatexDraftItem = {
      item_type: "section",
      change: "modified",
      statement: "改写了「方法」一节",
      locations: [
        location({ line: 5, end_line: 5 }),
        location({ path: "method.tex", line: 9, end_line: 14 }),
      ],
      delta: {},
    };
    expect(itemLine(item)).toBe(
      "章节 · 修改：改写了「方法」一节　sections/intro.tex L5、method.tex L9-L14",
    );
  });

  it("主文件没判定下来时才要求学生选，已判定或用户指定过就不再追问", () => {
    for (const status of ["ambiguous", "not_found", "invalid_selection"]) {
      expect(needsMainFileChoice(snapshot({ main_status: status }))).toBe(true);
    }
    for (const status of ["resolved", "user_specified"]) {
      expect(needsMainFileChoice(snapshot({ main_status: status }))).toBe(false);
    }
  });

  it("候选主文件列表取 path，退到 name，丢掉空值", () => {
    expect(
      candidatePaths(
        snapshot({
          main_status: "ambiguous",
          candidates: [{ path: "main.tex" }, { name: "paper.tex" }, { path: "" }, {}],
        }),
      ),
    ).toEqual(["main.tex", "paper.tex"]);
  });

  it("草稿摘要行：字数没变就不显示字数，减少时带负号", () => {
    expect(draftSummaryLine(draft())).toBe("章节 +2 / −0 / 改1 · 资源 3 · 字数 +400");
    expect(draftSummaryLine(draft({ word_count_delta: 0 }))).toBe("章节 +2 / −0 / 改1 · 资源 3");
    expect(draftSummaryLine(draft({ word_count_delta: -120 }))).toBe(
      "章节 +2 / −0 / 改1 · 资源 3 · 字数 -120",
    );
  });

  it("快照如实列出危险命令、未读取与只读了一部分的文件", () => {
    const lines = snapshotProblemLines(
      snapshot({
        dangerous_findings: [finding({ severity: "error", message: "检测到 \\write18", path: "main.tex" })],
        not_read: [{ path: "appendix.tex" }, { path: "" }],
        partially_read: [{ path: "long.tex" }],
      }),
    );
    expect(lines).toEqual([
      "错误：检测到 \\write18（main.tex L40-L41）",
      "未读取：appendix.tex",
      "只读取了一部分：long.tex",
    ]);
  });
});

describe("latexModel 草稿处置权限", () => {
  it("只有学生本人能确认自己的草稿；导师、别人的草稿、已确认/已丢弃都不行", () => {
    expect(canConfirmDraft(draft(), project("student"), "u1")).toEqual({ allowed: true, reason: "" });
    expect(canConfirmDraft(draft(), project("mentor"), "u1").allowed).toBe(false);
    expect(canConfirmDraft(draft(), project("mentor"), "u1").reason).toContain("只有学生本人");
    expect(canConfirmDraft(draft({ created_by: "u2" }), project("student"), "u1").reason).toContain(
      "只能确认自己生成的草稿",
    );
    expect(canConfirmDraft(draft({ status: "confirmed" }), project("student"), "u1").reason).toContain(
      "重复确认",
    );
    expect(canConfirmDraft(draft({ status: "discarded" }), project("student"), "u1").allowed).toBe(false);
  });

  it("丢弃：已确认的不能丢；先问归属再问角色；导师只能查看", () => {
    expect(canDiscardDraft(draft(), project("student"), "u1")).toEqual({ allowed: true, reason: "" });
    expect(canDiscardDraft(draft({ status: "confirmed" }), project("student"), "u1").reason).toBe(
      "已确认的草稿不能丢弃",
    );
    expect(canDiscardDraft(draft({ status: "discarded" }), project("student"), "u1").reason).toBe(
      "已经丢弃过了",
    );
    // 不是自己的草稿：先给归属理由，不拿角色当借口。
    expect(canDiscardDraft(draft({ created_by: "u2" }), project("mentor"), "u1").reason).toBe(
      "只能丢弃自己生成的草稿",
    );
    // 自己的草稿但项目角色是导师（例如导师自己生成的草稿）。
    expect(canDiscardDraft(draft(), project("mentor"), "u1").reason).toBe(
      "导师只能查看，不能处置学生的草稿",
    );
  });
});

describe("latexModel 结构化错误还原", () => {
  it("危险归档：把 problems 逐条还原成带条目号的说明", () => {
    const error = new ApiError(409, "请求失败（HTTP 409）", {
      detail: {
        message: "压缩包里有条目不安全，已拒绝解析",
        problems: [
          { message: "路径穿越", entry: "../etc/passwd", code: "unsafe_path" },
          { message: "符号链接", code: "symlink" },
          "绝对链接",
        ],
      },
    });
    expect(describeBlocked(error)).toEqual({
      headline: "压缩包里有条目不安全，已拒绝解析",
      lines: ["路径穿越（条目 ../etc/passwd）", "符号链接（symlink）", "绝对链接"],
    });
  });

  it("主文件歧义：把 candidates 拼成一行候选清单", () => {
    const error = new ApiError(409, "请求失败（HTTP 409）", {
      detail: { message: "主文件不唯一，请指定", candidates: [{ path: "main.tex" }, { name: "paper.tex" }] },
    });
    expect(describeBlocked(error)).toEqual({
      headline: "主文件不唯一，请指定",
      lines: ["候选主文件：main.tex、paper.tex"],
    });
  });

  it("普通字符串 detail 交给通用错误显示，这里不重复渲染", () => {
    expect(describeBlocked(new ApiError(403, "无权访问", { detail: "无权访问" }))).toBeNull();
    expect(describeBlocked(new Error("boom"))).toBeNull();
    expect(describeBlocked(new ApiError(409, "请求失败（HTTP 409）", { detail: {} }))).toBeNull();
    expect(describeBlocked(new ApiError(409, "请求失败（HTTP 409）", { detail: [] }))).toBeNull();
  });
});
