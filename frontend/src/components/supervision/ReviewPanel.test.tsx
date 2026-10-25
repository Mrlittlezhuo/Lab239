/**
 * 导师审核面板的离线打桩测试。
 *
 * 钉住三条：队列里能看到待审提交与快照信息；草稿与发布分离（未发布时出现草稿
 * 表单与「发布后结论不可修改」的说明）；学生视图不会去请求审核记录（后端没有
 * 「只返回已发布结论」的接口，前端不能把草稿取回浏览器再过滤）。
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";

import type { ProjectDetail, Review, Submission, Task } from "@/api/supervision";
import { ReviewPanel } from "@/components/supervision/ReviewPanel";

const api = vi.hoisted(() => ({
  listReviewQueue: vi.fn(),
  listTasks: vi.fn(),
  listSubmissions: vi.fn(),
  getReview: vi.fn(),
  latestAiReview: vi.fn(),
  listAiSuggestions: vi.fn(),
  saveReviewDraft: vi.fn(),
  publishReview: vi.fn(),
  addReviewNote: vi.fn(),
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
  acs: [],
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
  summary: "跑通训练脚本",
  git_repo: "",
  git_branch: "",
  git_commit: "abc1234",
  experiment_notes: "",
  known_issues: "",
  unfinished_items: "评测结果文件",
  self_assessment: [
    { ac_code: "AC-1", self_assessment: "done", evidence: "train.py L20", note: "" },
  ],
  ac_snapshot: [
    {
      ac_code: "AC-1",
      statement: "在同一测试集上给出指标",
      evidence_requirement: "结果文件",
      required: true,
      check_type: "result",
      version: 1,
    },
  ],
  materials: [{ attachment_code: "A-001", name: "train.py", kind: "code", version: 1, size: 10 }],
  completeness_check: [{ key: "runtime", passed: false, message: "缺少运行产物" }],
  author_id: "u-student",
  create_time: "2026-10-18T09:00:00",
  withdrawn_at: null,
  withdraw_reason: "",
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

function renderPanel(role: "mentor" | "student") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <ReviewPanel project={project(role)} refresh={() => {}} />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.clearAllMocks();
});

describe("ReviewPanel", () => {
  it("导师看到待审提交、快照信息与草稿表单，并被告知发布后不可修改", async () => {
    api.listReviewQueue.mockResolvedValue([SUBMISSION]);
    api.listTasks.mockResolvedValue([TASK]);
    api.listSubmissions.mockResolvedValue([]);
    api.getReview.mockResolvedValue(null);
    api.latestAiReview.mockResolvedValue(null);
    api.listAiSuggestions.mockResolvedValue([]);

    renderPanel("mentor");

    await waitFor(() => expect(screen.getAllByText("S-001-v1").length).toBeGreaterThan(0));
    expect(screen.getByText("在同一测试集上给出指标")).toBeInTheDocument();
    expect(screen.getByText("缺少运行产物")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "保存草稿" })).toBeInTheDocument();
    expect(
      screen.getByText(/发布后结论不可修改，只能追加补充意见或发起修订/),
    ).toBeInTheDocument();
    // AI 状态与导师结论是两个域
    expect(screen.getByText(/本次提交没有 AI 预审/)).toBeInTheDocument();
  });

  it("已发布审核只读展示并提供追加意见入口", async () => {
    const published: Review = {
      review_id: "r1",
      code: "MR-001",
      submission_id: "s1",
      task_id: "t1",
      status: "published",
      overall_decision: "revision_required",
      decision_reason: "评测口径不清",
      feedback: "先补结果文件",
      must_fix: ["补上评测结果文件"],
      suggestions: [],
      next_steps: "下周重看",
      reviewer_id: "u-mentor",
      first_touched_at: "2026-10-18T10:00:00",
      published_at: "2026-10-18T10:30:00",
      conclusions: [
        { ac_code: "AC-1", conclusion: "more_evidence_required", reason: "缺结果文件", ai_relation: "none" },
      ],
      notes: [],
    };
    api.listReviewQueue.mockResolvedValue([SUBMISSION]);
    api.listTasks.mockResolvedValue([TASK]);
    api.getReview.mockResolvedValue(published);
    api.latestAiReview.mockResolvedValue(null);

    renderPanel("mentor");

    await waitFor(() => expect(screen.getByText("MR-001")).toBeInTheDocument());
    expect(screen.getByText("已发布，结论不可修改")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "追加意见" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "保存草稿" })).not.toBeInTheDocument();
  });

  it("学生视图不请求审核记录，也不显示草稿", async () => {
    renderPanel("student");

    await waitFor(() =>
      expect(screen.getByText("导师审核只对导师开放")).toBeInTheDocument(),
    );
    expect(api.getReview).not.toHaveBeenCalled();
    expect(api.listReviewQueue).not.toHaveBeenCalled();
  });
});
