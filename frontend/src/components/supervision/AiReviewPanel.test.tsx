/**
 * AI 预审面板的离线打桩测试。
 *
 * 钉住四条：AI 状态与导师结论是两块独立区域（P-22）；创建只排队、执行遇到 503
 * 显示成「模型能力尚未接入」而不是失败（§14.2）；已过时的自检不能附到提交（§7.1
 * 第4条）；自动预审开关默认关闭且学生只读（P-05）。
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import type {
  AiReviewFinding,
  AiReviewRun,
  AiReviewSettings,
  AiSuggestion,
  ProjectDetail,
  Submission,
  Task,
} from "@/api/supervision";
import { ApiError } from "@/lib/errors";
import { AiReviewPanel } from "@/components/supervision/AiReviewPanel";

const api = vi.hoisted(() => ({
  listTasks: vi.fn(),
  listAttachments: vi.fn(),
  listSubmissions: vi.fn(),
  listAiReviews: vi.fn(),
  listAiFindings: vi.fn(),
  listAiSuggestions: vi.fn(),
  listAiMentorDiff: vi.fn(),
  getAiReviewSettings: vi.fn(),
  setAiReviewSettings: vi.fn(),
  studentSelfCheck: vi.fn(),
  runMentorAiReview: vi.fn(),
  rerunMentorAiReview: vi.fn(),
  executeAiReview: vi.fn(),
  cancelAiReview: vi.fn(),
  retryAiReview: vi.fn(),
  attachSelfCheck: vi.fn(),
  checkAiReviewStaleness: vi.fn(),
  adoptAiSuggestion: vi.fn(),
}));

vi.mock("@/api/supervision", () => api);

const TASK: Task = {
  task_id: "t1",
  code: "T-001",
  title: "复现基线",
  purpose: "打通评测口径",
  description: "",
  status: "active",
  label: "进行中",
  priority: "high",
  version: 1,
  due_at: null,
  requires_code_review: true,
  requires_runtime: true,
  required_materials: [],
  milestone_id: null,
  acs: [
    {
      ac_code: "AC-1",
      statement: "在同一测试集上给出指标",
      evidence_requirement: "结果文件",
      required: true,
      check_type: "result",
      version: 1,
    },
  ],
  cancel_reason: "",
  // 契约里这两个字段必填（可为 null）：FastAPI 一定会写出。
  published_at: null,
  create_time: null,
};

const SUBMISSION: Submission = {
  submission_id: "s1",
  code: "S-001",
  display_code: "S-001-v1",
  version: 1,
  task_id: "t1",
  status: "submitted",
  summary: "",
  git_repo: "",
  git_branch: "",
  git_commit: "",
  experiment_notes: "",
  known_issues: "",
  unfinished_items: "",
  self_assessment: [],
  ac_snapshot: [],
  materials: [{ attachment_code: "A-001", name: "train.py", kind: "code", version: 1, size: 10 }],
  completeness_check: [],
  author_id: "u-student",
  create_time: "2026-10-18T09:00:00",
  withdrawn_at: null,
  withdraw_reason: "",
};

function run(overrides: Partial<AiReviewRun> = {}): AiReviewRun {
  return {
    run_id: "r1",
    code: "AR-r1",
    display_code: "AR-r1-rev1",
    revision: 1,
    project_id: "p1",
    task_id: "t1",
    task_code: "T-001",
    submission_id: "s1",
    submission_code: "S-001",
    submission_version: 1,
    submission_display: "S-001-v1",
    trigger_source: "mentor_manual",
    triggered_by: "u-mentor",
    triggered_role: "mentor",
    status: "completed",
    summary_state: "存在缺失项",
    summary_note: "",
    ac_version: 1,
    total_batches: 1,
    completed_batches: 1,
    unchecked_acs: [],
    partial_results: false,
    call_plan: [],
    model_calls: [],
    input_budget: {},
    material_manifest: [],
    unread_materials: [],
    partially_read_materials: [],
    security_flags: [],
    finding_counts: { partial: 1 },
    visible_to_mentor: true,
    attached_to_submission: false,
    check_snapshot: {},
    draft_fingerprint: "fp",
    model_name: "",
    rules_version: "rules-1",
    parser_version: "parser-1",
    whitelist_version: "wl-1",
    budget_config_version: "budget-1",
    stale: false,
    stale_reasons: [],
    stale_label: "",
    attach_allowed: false,
    queue_status: {},
    model_available: true,
    failure_reason: "",
    cancel_reason: "",
    cancelled_by: "",
    ...overrides,
  };
}

const FINDING: AiReviewFinding = {
  finding_id: "f1",
  run_id: "r1",
  run_code: "AR-r1",
  ac_code: "AC-1",
  ac_statement: "在同一测试集上给出指标",
  ac_version: 1,
  required: true,
  check_type: "result",
  verdict: "partial",
  evidence_depth: "E2",
  support_relation: "partially_supports",
  reason: "有实现代码，没有运行产物",
  satisfied_parts: "已有评测脚本",
  missing_parts: "缺结果文件",
  evidence_refs: [{ name: "eval.py", location: "L10-L40" }],
  missing_materials: ["eval_results.json"],
  risks: [],
  mentor_focus: "确认测试集版本",
  injection_suspected: true,
  injection_note: "eval.py 注释里出现「判定通过」",
  runtime_claim_flag: true,
  downgrade_reason: "只有静态代码",
  validated: true,
  batch_index: 0,
};

const SUGGESTION: AiSuggestion = {
  ac_code: "AC-1",
  verdict: "partial",
  suggested_conclusion: "partially_met",
  suggested_label: "部分满足",
  reason: "缺运行产物",
  note: "",
};

const SETTINGS: AiReviewSettings = {
  project_id: "p1",
  task_id: "",
  project_mode: "off",
  task_mode: "inherit",
  effective_mode: "off",
  auto_review_enabled: false,
  default_is_off: true,
  priority_note: "任务明确设置高于项目默认设置；两者都没设置时关闭",
};

function project(role: "mentor" | "student"): ProjectDetail {
  return {
    project_id: "p1",
    code: "PRJ-001",
    name: "基线复现",
    description: "",
    status: "active",
    my_role: role,
    members: [],
  };
}

function stubCommon() {
  api.listTasks.mockResolvedValue([TASK]);
  api.listAttachments.mockResolvedValue([]);
  api.listSubmissions.mockResolvedValue([SUBMISSION]);
  api.getAiReviewSettings.mockResolvedValue(SETTINGS);
  api.listAiSuggestions.mockResolvedValue([SUGGESTION]);
  api.listAiMentorDiff.mockResolvedValue([]);
}

function renderPanel(role: "mentor" | "student") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <AiReviewPanel project={project(role)} refresh={() => {}} />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.clearAllMocks();
});

describe("AiReviewPanel", () => {
  it("导师侧：AI 状态、逐条结论与「AI 建议 vs 导师结论」分块展示", async () => {
    stubCommon();
    api.listAiReviews.mockResolvedValue([run()]);
    api.listAiFindings.mockResolvedValue([FINDING]);

    renderPanel("mentor");

    await waitFor(() => expect(screen.getAllByText("AR-r1-rev1").length).toBeGreaterThan(0));
    expect(await screen.findByText(/有实现代码，没有运行产物/)).toBeInTheDocument();
    expect(
      screen.getByText(/材料里有「已运行\/测试通过」类说法，但静态材料不能证明已运行/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/材料里出现了疑似指令文本，已按数据处理/),
    ).toBeInTheDocument();
    // 两块独立区域，不合成一个「审核结果」
    expect(screen.getByText(/AI 建议与导师结论对照/)).toBeInTheDocument();
    expect(screen.getByText(/AI 预审与导师审核是两个独立域/)).toBeInTheDocument();
    // 自动预审默认关闭（项目默认与任务级各一处）
    expect(screen.getAllByText("默认关闭").length).toBeGreaterThan(0);
  });

  it("执行遇到 503 时显示「模型能力尚未接入」说明，而不是失败", async () => {
    stubCommon();
    api.listAiReviews.mockResolvedValue([run({ status: "queued", submission_id: null })]);
    api.executeAiReview.mockRejectedValue(new ApiError(503, "模型能力尚未接入：预审已进入队列"));

    renderPanel("mentor");

    const execute = await screen.findByRole("button", { name: "执行" });
    fireEvent.click(execute);

    await waitFor(() =>
      expect(
        screen.getByText(/模型能力尚未接入，预审已排队；导师审核不受影响/),
      ).toBeInTheDocument(),
    );
    expect(screen.queryByText(/预审失败/)).not.toBeInTheDocument();
  });

  it("已过时的自检不能附到提交", async () => {
    stubCommon();
    api.listAiReviews.mockResolvedValue([
      run({
        status: "completed",
        trigger_source: "student_manual",
        triggered_by: "u-student",
        triggered_role: "student",
        submission_id: null,
        submission_display: "",
        attach_allowed: true,
        stale: true,
        stale_label: "基于旧快照",
      }),
    ]);
    api.listAiFindings.mockResolvedValue([FINDING]);

    renderPanel("student");

    const attach = await screen.findByRole("button", { name: "附到该提交" });
    expect(attach).toBeDisabled();
    expect(screen.getByText("基于旧快照")).toBeInTheDocument();
    expect(
      screen.getByText(/自检未完成、已附带或已过时（基于旧快照）时不能附带/),
    ).toBeInTheDocument();
  });

  it("学生看不到导师发起入口与自动预审设置入口", async () => {
    stubCommon();
    api.listAiReviews.mockResolvedValue([]);

    renderPanel("student");

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "AI 检查当前完成情况" })).toBeInTheDocument(),
    );
    expect(screen.queryByText("对某次提交发起 AI 预审")).not.toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getAllByText("学生看不到设置入口").length).toBeGreaterThan(0),
    );
  });
});
