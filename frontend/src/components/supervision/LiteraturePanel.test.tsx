/**
 * 论文检索面板的离线打桩测试。
 *
 * 钉住四条口径：检索任务必须绑定已确认并共享的研究定义（没有时给出下一步而不是
 * 一个空表单）；检索方案没确认不能开始搜索，理由写在按钮旁边；来源失败逐条列出、
 * 不隐藏检索过程；结论把论文事实、我的判断与待验证假设分三块显示，导师只能给意见。
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

import type {
  LiteratureConclusion,
  LiteratureResult,
  LiteratureTask,
  ProjectDetail,
  ResearchBrief,
} from "@/api/supervision";
import { LiteraturePanel } from "@/components/supervision/LiteraturePanel";

const api = vi.hoisted(() => ({
  listLiteratureTasks: vi.fn(),
  listSharedBriefs: vi.fn(),
  listLiteratureSubtasks: vi.fn(),
  listLiteratureResults: vi.fn(),
  listLiteratureConclusions: vi.fn(),
  listLiteratureCitations: vi.fn(),
  createLiteratureTask: vi.fn(),
}));

vi.mock("@/api/supervision", () => api);

function brief(over: Partial<ResearchBrief> = {}): ResearchBrief {
  return {
    brief_id: "b1",
    code: "RB-001",
    display_code: "RB-001-v2",
    project_id: "p1",
    owner_user_id: "u1",
    owner_role: "student",
    kind: "research_brief",
    title: "中文摘要评估",
    status: "confirmed",
    version_status: "confirmed",
    entry_type: "mixed",
    current_version: 2,
    shared_version: 2,
    shown_version: 2,
    confirmed_by: "u1",
    confirmed_at: "2026-10-01T10:00:00",
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

function task(over: Partial<LiteratureTask> = {}): LiteratureTask {
  return {
    task_id: "t1",
    code: "LT-001",
    project_id: "p1",
    status: "pending",
    purpose: "verify",
    goal: "验证已有结论是否成立",
    keywords: ["中文摘要"],
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
    create_time: "2026-10-02T09:00:00",
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
    relevance_reason: "命中 1 个关键词",
    relevance_matched: [{ term: "中文摘要" }],
    relevance_missing: [],
    relevance_score_source: "rules",
    relevance_score: null,
    relevance_rules_version: "v1",
    recommended_action: "priority_read",
    semantic_analysis: {},
    decision: "included",
    decision_reason: "方法最接近",
    decided_by: "u1",
    decided_role: "student",
    decided_at: "2026-10-03T09:00:00",
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
    statement: "现有方法普遍缺少人工校验",
    supported_facts: [{ statement: "三篇论文都只做了自动指标", result_code: "P-001" }],
    student_judgements: [{ statement: "人工校验成本高是主要原因", basis_result_codes: ["P-001"] }],
    unverified_hypotheses: [{ statement: "众包标注能降低成本", how_to_verify: "小规模对照实验" }],
    research_gaps: ["缺少中文人工校验数据集"],
    literature_snapshot: [],
    literature_snapshot_hash: "deadbeefcafe",
    literature_count: 1,
    evidence_cards: [],
    status: "confirmed",
    confirmed_by: "u1",
    confirmed_at: "2026-10-04T09:00:00",
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
    current_included_count: 1,
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

function stubCommon() {
  api.listSharedBriefs.mockResolvedValue([brief()]);
  api.listLiteratureSubtasks.mockResolvedValue([]);
  api.listLiteratureConclusions.mockResolvedValue([]);
  api.listLiteratureCitations.mockResolvedValue([]);
}

function renderPanel(role: "mentor" | "student") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <LiteraturePanel project={project(role)} />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.clearAllMocks();
});

describe("LiteraturePanel", () => {
  it("没有已确认并共享的研究定义时，给出下一步而不是一个空表单", async () => {
    stubCommon();
    api.listSharedBriefs.mockResolvedValue([brief({ status: "draft", shared_version: null })]);
    api.listLiteratureTasks.mockResolvedValue([]);
    api.listLiteratureResults.mockResolvedValue([]);

    renderPanel("student");

    expect(
      await screen.findByText(/还没有已确认并共享的研究定义/),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "新建检索任务" })).toBeDisabled();
    expect(
      await screen.findByText(/检索任务从已确认并共享的研究定义生成/),
    ).toBeInTheDocument();
  });

  it("检索方案没确认就不能开始检索，理由写在按钮旁边", async () => {
    stubCommon();
    api.listLiteratureTasks.mockResolvedValue([task()]);
    api.listLiteratureResults.mockResolvedValue([]);

    renderPanel("student");

    // 任务编号同时出现在任务卡片与下方任务切换处。
    expect(await screen.findAllByText("LT-001")).not.toHaveLength(0);
    expect(
      screen.getByText("还没有检索方案：先生成并确认检索方案"),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "开始检索" })).toBeDisabled();
    expect(
      screen.getByText(/必须由你确认后才允许检索；系统不会自己决定搜什么/),
    ).toBeInTheDocument();
  });

  it("来源失败逐条列出，不隐藏检索过程；依据被更新时提示按当时版本解释", async () => {
    stubCommon();
    api.listLiteratureTasks.mockResolvedValue([
      task({
        source_failures: [{ source: "arxiv", message: "超时" }],
        brief_superseded: true,
        brief_version: 2,
        brief_shared_version: 3,
        brief_latest_version: 4,
      }),
    ]);
    api.listLiteratureResults.mockResolvedValue([]);

    renderPanel("student");

    expect(await screen.findByText("有来源检索失败")).toBeInTheDocument();
    expect(screen.getByText(/arxiv：超时/)).toBeInTheDocument();
    expect(screen.findByText("依据的研究定义已更新")).resolves.toBeInTheDocument();
  });

  it("导师只能查看：没有生成方案/检索/关闭任务的按钮", async () => {
    stubCommon();
    api.listLiteratureTasks.mockResolvedValue([task()]);
    api.listLiteratureResults.mockResolvedValue([]);

    renderPanel("mentor");

    // 任务编号同时出现在任务卡片与下方任务切换处。
    expect(await screen.findAllByText("LT-001")).not.toHaveLength(0);
    expect(screen.queryByRole("button", { name: "新建检索任务" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "生成检索方案" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "开始检索" })).not.toBeInTheDocument();
    expect(
      screen.getByText(/导师可以查看任务、方案与候选；生成方案、确认方案与检索由学生发起/),
    ).toBeInTheDocument();
  });

  it("结论把论文事实、我的判断与待验证假设分三块显示，导师只能给意见", async () => {
    stubCommon();
    api.listLiteratureTasks.mockResolvedValue([task({ status: "concluded", included_total: 1 })]);
    api.listLiteratureResults.mockResolvedValue([result()]);
    api.listLiteratureConclusions.mockResolvedValue([conclusion()]);

    renderPanel("mentor");

    expect(await screen.findByText("论文事实（文献支持）")).toBeInTheDocument();
    expect(screen.getByText("我的判断")).toBeInTheDocument();
    expect(screen.getByText("待验证假设")).toBeInTheDocument();
    // 论文事实要带它引用的论文编号（编号在陈述后面的括号里）。
    const factRow = screen.getByText("三篇论文都只做了自动指标");
    expect(factRow.textContent).toBe("三篇论文都只做了自动指标（P-001）");
    expect(factRow.querySelector("span")?.textContent).toBe("（P-001）");
    expect(screen.getByText("众包标注能降低成本")).toBeInTheDocument();
    expect(screen.getByText(/怎么验证：小规模对照实验/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "给出导师意见" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "新建结论" })).not.toBeInTheDocument();
  });

  it("学生没有已纳入的论文时不能写结论，并说明要先纳入", async () => {
    stubCommon();
    api.listLiteratureTasks.mockResolvedValue([task()]);
    api.listLiteratureResults.mockResolvedValue([]);
    api.listLiteratureConclusions.mockResolvedValue([]);

    renderPanel("student");

    const button = await screen.findByRole("button", { name: "新建结论" });
    expect(button).toBeDisabled();
    expect(
      screen.getByText(/先在候选列表里决定「纳入」，才能写论文事实/),
    ).toBeInTheDocument();
  });
});
