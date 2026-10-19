/**
 * 稿件分析面板的离线打桩测试。
 *
 * 钉住三条口径：只有 .zip 归档能被分析（其它格式连选择清单都进不去）；快照卡片
 * 永远声明"没有编译也没有运行"，危险命令与未读取文件原样显示；进展草稿只有学生
 * 本人能确认成正式进展，导师看到的是理由而不是按钮。
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

import type {
  Attachment,
  LatexProgressDraft,
  LatexSnapshot,
  ProjectDetail,
} from "@/api/supervision";
import { LatexPanel } from "@/components/supervision/LatexPanel";
import { useAuth } from "@/stores/auth";

const api = vi.hoisted(() => ({
  listAttachments: vi.fn(),
  listTasks: vi.fn(),
  listLatexSnapshots: vi.fn(),
  listLatexProgressDrafts: vi.fn(),
  analyzeLatexArchive: vi.fn(),
  createLatexProgressDraft: vi.fn(),
  confirmLatexProgressDraft: vi.fn(),
  discardLatexProgressDraft: vi.fn(),
}));

vi.mock("@/api/supervision", () => api);

function attachment(over: Partial<Attachment> = {}): Attachment {
  return {
    attachment_id: "a1",
    code: "ATT-001",
    name: "paper.zip",
    ext: ".zip",
    mime_type: "application/zip",
    size: 20480,
    sha256: "abc",
    version: 3,
    kind: "latex_archive",
    status: "active",
    task_id: null,
    submission_id: null,
    uploaded_by: "u1",
    create_time: null,
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
    total_bytes: 20480,
    created_by: "u1",
    created_role: "student",
    create_time: "2026-10-10T09:00:00",
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
    items: [
      {
        item_type: "section",
        change: "added",
        statement: "新增「结果与讨论」一节",
        locations: [{ path: "results.tex", line: 3, end_line: 20, snippet: "" }],
        delta: {},
      },
    ],
    item_count: 1,
    truncated: false,
    dropped_items: 0,
    counts: { added_sections: 1, removed_sections: 0, modified_sections: 0, resource_changes: 2 },
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
    create_time: "2026-10-11T09:00:00",
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

function renderPanel(role: "mentor" | "student") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <LatexPanel project={project(role)} refresh={() => {}} />
    </QueryClientProvider>,
  );
}

function signedInAs(userId: string) {
  useAuth.setState({
    token: "t",
    user: {
      id: userId,
      username: userId,
      role: "user",
      status: "active",
      must_change_password: false,
    },
    expiresAt: null,
  });
}

afterEach(() => {
  vi.clearAllMocks();
});

describe("LatexPanel", () => {
  it("只有 .zip 归档能被分析：非 zip 附件进不了清单", async () => {
    api.listAttachments.mockResolvedValue([attachment({ name: "draft.pdf", ext: ".pdf" })]);
    api.listTasks.mockResolvedValue([]);
    api.listLatexSnapshots.mockResolvedValue([]);
    api.listLatexProgressDrafts.mockResolvedValue([]);

    renderPanel("student");

    expect(await screen.findByText("还没有可分析的 .zip 稿件")).toBeInTheDocument();
    expect(screen.getByText(/请先到「附件」页上传 .zip 稿件归档/)).toBeInTheDocument();
  });

  it("快照卡片永远声明没有编译也没有运行，并把危险命令与未读取文件列出来", async () => {
    api.listAttachments.mockResolvedValue([attachment()]);
    api.listTasks.mockResolvedValue([]);
    api.listLatexProgressDrafts.mockResolvedValue([]);
    api.listLatexSnapshots.mockResolvedValue([
      snapshot({
        dangerous_findings: [
          {
            kind: "dangerous_command",
            severity: "error",
            message: "检测到 \\write18",
            path: "main.tex",
            line: 7,
            end_line: 7,
            snippet: "\\write18{rm -rf /}",
            target: "write18",
            detail: "",
          },
        ],
        not_read: [{ path: "appendix.tex" }],
        unreferenced_tex: ["old-draft.tex"],
      }),
    ]);

    renderPanel("student");

    expect(await screen.findByText("只做了静态解析，没有编译也没有运行")).toBeInTheDocument();
    // 危险命令与未读取文件在同一个提示块里按行显示，所以按片段匹配。
    expect(screen.getByText(/检测到 \\write18（main\.tex L7）/)).toBeInTheDocument();
    expect(screen.getByText(/未读取：appendix\.tex/)).toBeInTheDocument();
    expect(screen.getByText("有 .tex 文件没被主文件引用")).toBeInTheDocument();
    expect(screen.getByText(/章节 6 · 图 3/)).toBeInTheDocument();
  });

  it("主文件没判定下来的快照在清单上标出来，不假装结果可信", async () => {
    api.listAttachments.mockResolvedValue([attachment()]);
    api.listTasks.mockResolvedValue([]);
    api.listLatexProgressDrafts.mockResolvedValue([]);
    api.listLatexSnapshots.mockResolvedValue([
      snapshot({ main_status: "ambiguous", main_file: "", candidates: [{ path: "main.tex" }] }),
    ]);

    renderPanel("student");

    expect(await screen.findByText("LX-001 · ATT-001 v3 · 需指定主文件")).toBeInTheDocument();
    expect(screen.getByText("主文件没有唯一判定，差异结果不可信")).toBeInTheDocument();
  });

  it("进展草稿：学生本人能确认成正式进展，导师只看到理由", async () => {
    signedInAs("u1");
    api.listAttachments.mockResolvedValue([attachment()]);
    api.listTasks.mockResolvedValue([]);
    api.listLatexSnapshots.mockResolvedValue([]);
    api.listLatexProgressDrafts.mockResolvedValue([draft()]);

    renderPanel("student");
    expect(await screen.findByText("确认为正式进展")).toBeInTheDocument();
    expect(screen.getByText("这条说明必须一起看")).toBeInTheDocument();
    expect(screen.getByText("静态差异不能证明实验已执行。")).toBeInTheDocument();
    // 差异条目一行里要同时有类型、变化、陈述与源码定位。
    const itemRow = await screen.findByText(
      (_content, element) =>
        element?.tagName === "LI" && (element.textContent ?? "").includes("新增「结果与讨论」一节"),
    );
    expect(itemRow.textContent).toContain("章节 · 新增");
    expect(itemRow.textContent).toContain("results.tex L3-L20");

    renderPanel("mentor");
    expect(
      await screen.findByText("只有学生本人能把稿件差异确认为自己的进展"),
    ).toBeInTheDocument();
  });
});
