/**
 * 研究定义面板的渲染约定（全部离线打桩 supervision 接口）。
 *
 * 钉住版本与可见性口径：
 * 1. 非所有者只读到已共享的那一版，来源消息与版本历史一律 404 → 显示成说明；
 * 2. 所有者改出未共享的新版本时，界面提醒「另一方可能仍在读旧版本」。
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import type { ProjectDetail, ResearchBrief } from "@/api/supervision";
import { ApiError } from "@/lib/errors";
import { BriefPanel } from "@/components/supervision/BriefPanel";

const api = vi.hoisted(() => ({
  listBriefs: vi.fn(),
  listSharedBriefs: vi.fn(),
  getBrief: vi.fn(),
  createBrief: vi.fn(),
  updateBrief: vi.fn(),
  confirmBrief: vi.fn(),
  rejectBrief: vi.fn(),
  deleteBrief: vi.fn(),
  getBriefSourceMessages: vi.fn(),
  listBriefVersions: vi.fn(),
  compareBriefVersions: vi.fn(),
}));
vi.mock("@/api/supervision", () => api);

function project(): ProjectDetail {
  return {
    project_id: "p1",
    code: "SP-001",
    name: "中文摘要质量研究",
    description: "",
    status: "active",
    my_role: "mentor",
    members: [],
  };
}

function brief(overrides: Partial<ResearchBrief> = {}): ResearchBrief {
  return {
    brief_id: "b1",
    code: "RB-001",
    display_code: "RB-001-v2",
    project_id: "p1",
    owner_user_id: "student-1",
    owner_role: "student",
    kind: "research_brief",
    title: "中文摘要质量评估",
    status: "confirmed",
    version_status: "confirmed",
    entry_type: "research_question",
    current_version: 3,
    shared_version: 2,
    shown_version: 2,
    confirmed_by: "student-1",
    confirmed_at: "2026-10-18T09:00:00",
    confirm_note: "",
    create_time: "2026-10-17T09:00:00",
    update_time: "2026-10-18T09:00:00",
    is_owner: false,
    can_edit: false,
    source_conversation_id: null,
    source_message_ids: [],
    background: "已有工作集中在英文摘要。",
    question: "怎样评估中文摘要的质量？",
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
    keywords: ["中文摘要", "质量评估"],
    sub_questions: [],
    ...overrides,
  };
}

function renderPanel() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <BriefPanel project={project()} refresh={() => undefined} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.resetAllMocks();
  api.listBriefs.mockResolvedValue([]);
  api.listSharedBriefs.mockResolvedValue([brief()]);
  api.getBrief.mockResolvedValue(brief());
  api.getBriefSourceMessages.mockRejectedValue(new ApiError(404, "研究定义不存在"));
  api.listBriefVersions.mockRejectedValue(new ApiError(404, "研究定义不存在"));
  api.compareBriefVersions.mockResolvedValue({ changes: [] });
});

describe("另一方读到的共享版本", () => {
  it("只读到已共享的那一版，来源消息与版本历史显示成说明", async () => {
    renderPanel();
    const row = await screen.findByRole("button", { name: /中文摘要质量评估/ });
    fireEvent.click(row);

    expect(await screen.findByText(/你看到的是对方已确认共享的 v2/)).toBeInTheDocument();
    expect(await screen.findByText("来源消息仅创建者可见")).toBeInTheDocument();
    expect(await screen.findByText("版本历史只对创建者开放")).toBeInTheDocument();
    // 草稿侧的编辑与确认入口对非所有者不出现（真正的权限仍在后端）。
    expect(screen.queryByRole("button", { name: "确认本版并共享" })).toBeNull();
    expect(screen.queryByRole("button", { name: "编辑" })).toBeNull();
  });
});

describe("所有者改出新版本", () => {
  it("提醒另一方可能仍在读旧版本", async () => {
    const mine = brief({
      is_owner: true,
      can_edit: true,
      owner_user_id: "mentor-1",
      owner_role: "mentor",
      status: "draft",
      display_code: "RB-001-v3",
    });
    api.listBriefs.mockResolvedValue([mine]);
    api.listSharedBriefs.mockResolvedValue([]);
    api.getBrief.mockResolvedValue(mine);
    api.getBriefSourceMessages.mockResolvedValue({
      conversation_id: "c1",
      code: "CV-001",
      messages: [
        {
          message_id: "m1",
          query: "先确认评估口径",
          response: "可以用人工评分加自动指标。",
          metadata: { model_name: "strata" },
          create_time: "2026-10-17T08:00:00",
        },
      ],
    });
    api.listBriefVersions.mockResolvedValue([
      {
        version: 1,
        status: "confirmed",
        entry_type: "research_question",
        edited_fields: [],
        confirmed_by: "mentor-1",
        confirmed_at: "2026-10-17T09:00:00",
        confirm_note: "",
        superseded_at: null,
        create_time: "2026-10-17T08:00:00",
        source_message_ids: ["m1"],
      },
      {
        version: 2,
        status: "confirmed",
        entry_type: "research_question",
        edited_fields: ["question"],
        confirmed_by: "mentor-1",
        confirmed_at: "2026-10-18T09:00:00",
        confirm_note: "",
        superseded_at: null,
        create_time: "2026-10-18T09:00:00",
        source_message_ids: ["m1"],
      },
      {
        version: 3,
        status: "draft",
        entry_type: "research_question",
        edited_fields: ["metrics"],
        confirmed_by: "",
        confirmed_at: null,
        confirm_note: "",
        superseded_at: null,
        create_time: "2026-10-19T09:00:00",
        source_message_ids: ["m1"],
      },
    ]);
    api.compareBriefVersions.mockResolvedValue({
      brief_id: "b1",
      code: "RB-001",
      from_version: 2,
      to_version: 3,
      from_status: "confirmed",
      to_status: "draft",
      changes: [
        { field: "metrics", label: "评价指标", from: "", to: "人工评分 + ROUGE" },
        {
          field: "keywords",
          label: "需要检索的关键词",
          from: ["中文摘要"],
          to: ["中文摘要", "人工评估"],
          added: ["人工评估"],
          removed: [],
        },
      ],
    });

    renderPanel();
    const row = await screen.findByRole("button", { name: /中文摘要质量评估/ });
    fireEvent.click(row);

    // 详情卡片与版本区都会提醒同一件事，两处都要在。
    const notes = await screen.findAllByText(/另一方读到的仍然是 v2/);
    expect(notes.length).toBeGreaterThanOrEqual(2);
    expect(await screen.findByText("另一方可能仍在读旧版本")).toBeInTheDocument();
    await waitFor(() => expect(api.compareBriefVersions).toHaveBeenCalledWith("p1", "b1", 2, 3));
    expect(await screen.findByText("评价指标")).toBeInTheDocument();
    expect(screen.getByText("+ 人工评估")).toBeInTheDocument();
  });
});
