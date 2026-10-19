import { describe, expect, it } from "vitest";

import type {
  LiteratureConclusion,
  LiteratureResult,
  LiteratureSubtask,
  LiteratureTask,
  ProjectDetail,
} from "@/api/supervision";
import {
  briefSupersededNote,
  canCancelSubtask,
  canEditTask,
  canRetrySubtask,
  canSearch,
  conclusionStalenessNote,
  DECISION_OPTIONS,
  hasSearchPlan,
  isCapabilityUnavailable,
  isStudent,
  labelOf,
  planConfirmed,
  relevanceReasonLines,
  sourceFailureLines,
  subtaskLine,
  taskClosed,
  taskSummaryLine,
} from "@/components/supervision/literature/literatureModel";
import { ApiError } from "@/lib/errors";

function task(over: Partial<LiteratureTask> = {}): LiteratureTask {
  return {
    task_id: "t1",
    code: "LT-001",
    project_id: "p1",
    status: "pending",
    purpose: "verify",
    goal: "验证中文摘要评估的已有结论",
    keywords: ["中文摘要", "评估"],
    synonyms: [],
    exclude_terms: [],
    sources: [],
    year_from: null,
    year_to: null,
    require_fulltext: false,
    result_limit: 30,
    deep_extract_limit: 3,
    brief_id: "b1",
    brief_code: "RB-001",
    brief_version: 2,
    brief_kind: "research_brief",
    brief_confirmed_by: "u1",
    brief_confirmed_at: "2026-10-01T10:00:00",
    brief_snapshot: {},
    brief_superseded: false,
    brief_shared_version: 2,
    brief_latest_version: 2,
    search_plan: {},
    search_plan_note: "",
    search_plan_model: "",
    search_plan_prompt_version: "",
    search_plan_generated_at: null,
    search_plan_confirmed_at: null,
    search_plan_confirmed_by: "",
    plan_status: "",
    plan_error: "",
    hits_total: 0,
    deduped_total: 0,
    stored_total: 0,
    source_failures: [],
    searched_at: null,
    concluded_at: null,
    closed_at: null,
    close_reason: "",
    created_by: "u1",
    created_role: "student",
    create_time: null,
    update_time: null,
    result_total: 0,
    included_total: 0,
    excluded_total: 0,
    read_later_total: 0,
    undecided_total: 0,
    deep_extract_used: 0,
    conclusion_total: 0,
    subtask_total: 0,
    subtask_failed_total: 0,
    recent_subtasks: [],
    ...over,
  };
}

function subtask(over: Partial<LiteratureSubtask> = {}): LiteratureSubtask {
  return {
    subtask_id: "st1",
    code: "LS-001",
    kind: "search",
    status: "completed",
    attempt: 1,
    source: "arxiv",
    query: "中文摘要 评估",
    payload: {},
    result_total: 12,
    new_total: 9,
    duplicate_total: 3,
    error_code: "",
    error_message: "",
    failure_history: [],
    model_calls: [],
    consumer: "",
    model_name: "",
    prompt_version: "",
    queued_at: null,
    started_at: null,
    finished_at: null,
    cancelled_by: "",
    cancelled_at: null,
    cancel_reason: "",
    created_by: "u1",
    created_role: "student",
    create_time: null,
    update_time: null,
    ...over,
  };
}

function result(over: Partial<LiteratureResult> = {}): LiteratureResult {
  return {
    result_id: "r1",
    code: "P-001",
    project_id: "p1",
    search_task_id: "t1",
    subtask_id: "st1",
    dedup_key: "doi:10.1/1",
    doi: "10.1/1",
    arxiv_id: null,
    title_key: null,
    title: "Chinese abstract assessment",
    authors: "张三",
    year: 2024,
    abstract: "我们提出……",
    url: null,
    source: "arxiv",
    sources: [],
    fulltext_status: "abstract",
    relevance_level: "relevant",
    relevance_reason: "命中 2 个关键词，缺 1 个要素",
    relevance_matched: [{ term: "中文摘要" }, { keyword: "评估" }],
    relevance_missing: [{ term: "中文摘要评估" }],
    relevance_score_source: "rules",
    relevance_score: null,
    relevance_rules_version: "v1",
    recommended_action: "priority_read",
    semantic_analysis: {},
    decision: "pending",
    decision_reason: "",
    decided_by: "",
    decided_role: "",
    decided_at: null,
    decision_history: [],
    extract_state: "none",
    evidence_cards: [],
    latest_extraction_version: 0,
    first_seen_at: null,
    create_time: null,
    update_time: null,
    ...over,
  };
}

function conclusion(over: Partial<LiteratureConclusion> = {}): LiteratureConclusion {
  return {
    conclusion_id: "c1",
    code: "LC-001",
    version: 1,
    project_id: "p1",
    search_task_id: "t1",
    search_task_code: "LT-001",
    statement: "现有方法在中文摘要评估上普遍缺少人工校验",
    supported_facts: [],
    student_judgements: [],
    unverified_hypotheses: [],
    research_gaps: [],
    literature_snapshot: [],
    literature_snapshot_hash: "deadbeefcafe",
    literature_count: 8,
    evidence_cards: [],
    status: "confirmed",
    confirmed_by: "u1",
    confirmed_at: "2026-10-02T10:00:00",
    confirm_note: "",
    mentor_state: "",
    mentor_note: "",
    mentor_by: "",
    mentor_role: "",
    mentor_at: null,
    superseded_at: null,
    superseded_by_code: "",
    superseded_by_version: 0,
    literature_changed: false,
    current_included_count: 8,
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
    name: "中文摘要评估",
    description: "",
    status: "active",
    my_role: role,
    members: [],
  };
}

describe("literatureModel 检索门槛", () => {
  it("只有学生能做决定、建卡与确认结论", () => {
    expect(isStudent(project("student"))).toBe(true);
    expect(isStudent(project("mentor"))).toBe(false);
  });

  it("没方案或方案没确认都不能检索，理由要说清下一步做什么", () => {
    expect(canSearch(task())).toEqual({
      allowed: false,
      reason: "还没有检索方案：先生成并确认检索方案",
    });
    const planned = task({ search_plan: { keyword_batches: [["中文摘要"]] } });
    expect(canSearch(planned)).toEqual({
      allowed: false,
      reason: "检索方案还没经你确认：确认后才会真的去检索",
    });
    expect(hasSearchPlan(planned)).toBe(true);
    expect(planConfirmed(planned)).toBe(false);
    expect(canSearch(task({ search_plan: { x: 1 }, search_plan_confirmed_at: "2026-10-01T10:00:00" }))).toEqual({
      allowed: true,
      reason: "",
    });
  });

  it("已关闭的任务不能检索，也不能改检索输入", () => {
    const closed = task({ status: "closed", search_plan: { x: 1 }, search_plan_confirmed_at: "x" });
    expect(taskClosed(closed)).toBe(true);
    expect(canSearch(closed)).toEqual({
      allowed: false,
      reason: "任务已关闭，需要新建一个检索任务",
    });
    expect(canEditTask(closed)).toBe(false);
  });

  it("只有草稿与待检索阶段能改检索输入", () => {
    expect(canEditTask(task({ status: "draft" }))).toBe(true);
    expect(canEditTask(task({ status: "pending" }))).toBe(true);
    for (const status of ["searching", "triage", "concluded"]) {
      expect(canEditTask(task({ status }))).toBe(false);
    }
  });

  it("决定选项只有纳入/排除/稍后阅读，没有「默认纳入」", () => {
    expect(DECISION_OPTIONS.map((option) => option.value)).toEqual([
      "included",
      "excluded",
      "read_later",
    ]);
  });
});

describe("literatureModel 过程与失败显示", () => {
  it("任务摘要行把命中、去重、入库与决定分布都写出来", () => {
    expect(
      taskSummaryLine(
        task({
          hits_total: 60,
          deduped_total: 41,
          stored_total: 30,
          included_total: 8,
          excluded_total: 5,
          read_later_total: 4,
          undecided_total: 13,
        }),
      ),
    ).toBe("命中 60 · 去重后 41 · 入库 30 · 纳入 8 / 排除 5 / 稍后 4 / 未决定 13");
  });

  it("依据的研究定义被更新时给出当时与当前版本号", () => {
    expect(briefSupersededNote(task())).toBe("");
    expect(
      briefSupersededNote(
        task({ brief_superseded: true, brief_version: 2, brief_shared_version: 3, brief_latest_version: 4 }),
      ),
    ).toContain("RB-001 v2");
    expect(
      briefSupersededNote(
        task({ brief_superseded: true, brief_version: 2, brief_shared_version: 3, brief_latest_version: 4 }),
      ),
    ).toContain("当前共享 v3 / 最新 v4");
  });

  it("来源失败逐条列出：优先用来源名与消息，缺消息退到错误码", () => {
    expect(
      sourceFailureLines(
        task({
          source_failures: [
            { source: "arxiv", message: "超时" },
            { source: "crossref", code: "http_500" },
            { message: "没有实现" },
            {},
          ],
        }),
      ),
    ).toEqual(["arxiv：超时", "crossref：http_500", "某个来源：没有实现"]);
  });

  it("检索子任务把命中/新增/重复写出来，其它种类不编造计数", () => {
    expect(subtaskLine(subtask())).toBe(
      "检索 · 完成（第 1 次） · 来源 arxiv · 命中 12，新增 9，重复 3",
    );
    expect(subtaskLine(subtask({ kind: "deep_extract", source: "" }))).toBe(
      "深度提取 · 完成（第 1 次）",
    );
    expect(subtaskLine(subtask({ status: "failed", error_message: "来源不可达" }))).toBe(
      "检索 · 失败（第 1 次） · 来源 arxiv · 命中 12，新增 9，重复 3 · 来源不可达",
    );
  });

  it("只有失败或已取消的子任务能重试；排队中与执行中能取消", () => {
    for (const status of ["failed", "cancelled"]) {
      expect(canRetrySubtask(subtask({ status }))).toBe(true);
    }
    for (const status of ["pending", "queued", "running", "completed"]) {
      expect(canRetrySubtask(subtask({ status }))).toBe(false);
    }
    expect(canCancelSubtask(subtask({ status: "queued" }))).toBe(true);
    expect(canCancelSubtask(subtask({ status: "running" }))).toBe(true);
    expect(canCancelSubtask(subtask({ status: "completed" }))).toBe(false);
  });
});

describe("literatureModel 关联等级与结论依据", () => {
  it("关联等级显示规则理由、命中与缺失要素，并说明全文可用性与建议", () => {
    expect(relevanceReasonLines(result())).toEqual([
      "规则判定：命中 2 个关键词，缺 1 个要素",
      "命中关键词：中文摘要、评估",
      "缺少要素：中文摘要评估",
      "全文可用性：只有摘要",
      "建议：建议优先读",
    ]);
  });

  it("规则字段缺失时留空而不是编造理由", () => {
    expect(
      relevanceReasonLines(
        result({ relevance_reason: "", relevance_matched: [], relevance_missing: [] }),
      ),
    ).toEqual(["全文可用性：只有摘要", "建议：建议优先读"]);
  });

  it("结论绑定的文献集合变了要说清两边数量", () => {
    expect(conclusionStalenessNote(conclusion())).toBe("");
    expect(
      conclusionStalenessNote(conclusion({ literature_changed: true, literature_count: 8, current_included_count: 11 })),
    ).toContain("8 篇");
    expect(
      conclusionStalenessNote(conclusion({ literature_changed: true, literature_count: 8, current_included_count: 11 })),
    ).toContain("11 篇");
  });

  it("未知状态原样显示，不套一个错的中文标签", () => {
    expect(labelOf({ draft: "草稿" }, "brand_new_state")).toBe("brand_new_state");
  });
});

describe("literatureModel 503 与失败分开", () => {
  it("503 是能力未接入，其它状态码仍是失败", () => {
    expect(isCapabilityUnavailable(new ApiError(503, "外部来源尚未接入"))).toBe(true);
    expect(isCapabilityUnavailable(new ApiError(409, "检索方案还没确认"))).toBe(false);
    expect(isCapabilityUnavailable(new Error("network"))).toBe(false);
  });
});
