/**
 * 任务面板的渲染约定（全部离线打桩 supervision 接口）。
 *
 * 钉住三条容易被改坏的口径：
 * 1. 列表上的状态标签就是后端 `label`（§5.6），前端不自己拼混合状态；
 * 2. 学生视角只读——不出现新建/编辑/发布/取消按钮（真正的权限在后端）；
 * 3. 详情四象限的标题与验收标准版本号来自后端字段。
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import type { ProjectDetail, Task } from "@/api/supervision";
import { TasksPanel } from "@/components/supervision/TasksPanel";

const api = vi.hoisted(() => ({
  listTasks: vi.fn(),
  listMilestones: vi.fn(),
  getTask: vi.fn(),
  listProgress: vi.fn(),
  listSubmissions: vi.fn(),
  getReview: vi.fn(),
  createTask: vi.fn(),
  updateTask: vi.fn(),
  publishTask: vi.fn(),
  cancelTask: vi.fn(),
}));
vi.mock("@/api/supervision", () => api);

function task(overrides: Partial<Task> = {}): Task {
  return {
    task_id: "t1",
    code: "T-001",
    title: "复现基线方法",
    purpose: "先有一个可比较的基线",
    description: "在公开数据集上跑通基线",
    status: "active",
    label: "等待重审",
    priority: "high",
    version: 2,
    due_at: "2026-10-20T18:30:00",
    requires_code_review: true,
    requires_runtime: false,
    required_materials: [{ key: "code", label: "代码归档", required: true }],
    milestone_id: "m1",
    acs: [
      {
        ac_code: "AC-1",
        statement: "固定随机种子下中位定位误差小于 1 m",
        evidence_requirement: "实验配置与结果文件",
        required: true,
        check_type: "experiment",
        version: 2,
      },
    ],
    published_at: "2026-10-10T09:00:00",
    cancel_reason: "",
    create_time: "2026-10-09T09:00:00",
    ...overrides,
  };
}

function project(role: "mentor" | "student"): ProjectDetail {
  return {
    project_id: "p1",
    code: "SP-001",
    name: "位置目标定位研究",
    description: "",
    status: "active",
    my_role: role,
    members: [{ user_id: "u1", role, status: "active" }],
  };
}

function renderPanel(role: "mentor" | "student") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <TasksPanel project={project(role)} refresh={() => undefined} />
    </QueryClientProvider>,
  );
}

describe("TasksPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listTasks.mockResolvedValue([task(), task({ task_id: "t2", code: "T-002", title: "写相关工作小节", status: "draft", label: "任务草稿", published_at: null })]);
    api.listMilestones.mockResolvedValue([
      {
        milestone_id: "m1",
        code: "MS-001",
        title: "基线复现",
        description: "",
        success_criteria: "",
        status: "active",
        owner_user_id: "u1",
        mentor_confirmation: "",
        mentor_confirmation_note: "",
      },
    ]);
    api.getTask.mockResolvedValue(task());
    api.listProgress.mockResolvedValue([]);
    api.listSubmissions.mockResolvedValue([]);
    api.getReview.mockResolvedValue(null);
  });

  it("状态标签直接用后端 label", async () => {
    renderPanel("student");
    expect(await screen.findByText("等待重审")).toBeInTheDocument();
    expect(screen.getByText("任务草稿")).toBeInTheDocument();
  });

  it("学生看不到新建、编辑、发布和取消按钮", async () => {
    renderPanel("student");
    await screen.findByText("复现基线方法");
    expect(screen.queryByRole("button", { name: "新建任务" })).toBeNull();
    expect(screen.queryAllByRole("button", { name: "编辑" })).toHaveLength(0);
    expect(screen.queryAllByRole("button", { name: "发布" })).toHaveLength(0);
    expect(screen.queryAllByRole("button", { name: "取消" })).toHaveLength(0);
  });

  it("导师对草稿任务能编辑、发布和取消", async () => {
    renderPanel("mentor");
    await screen.findByText("复现基线方法");
    expect(screen.getByRole("button", { name: "新建任务" })).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "编辑" })).toHaveLength(2);
    expect(screen.getAllByRole("button", { name: "发布" })).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: "取消" })).toHaveLength(2);
  });

  it("详情四象限用后端字段给标题", async () => {
    renderPanel("mentor");
    const details = await screen.findAllByRole("button", { name: "详情" });
    fireEvent.click(details[0]);
    await waitFor(() => expect(api.getTask).toHaveBeenCalledWith("p1", "T-001"));
    expect(await screen.findByText("任务要解决什么")).toBeInTheDocument();
    expect(screen.getByText("验收标准逐条（v2）")).toBeInTheDocument();
    expect(screen.getByText("学生进展")).toBeInTheDocument();
    expect(screen.getByText("提交与审核情况")).toBeInTheDocument();
    expect(screen.getByText("还没有正式提交")).toBeInTheDocument();
  });
});
