import { useQuery } from "@tanstack/react-query";
import { useParams, useSearchParams } from "react-router-dom";

import { getProject } from "@/api/supervision";
import type { ProjectDetail, ProjectRole } from "@/api/supervision";
import { Badge } from "@/components/ui/badge";
import { AlertPanel } from "@/components/supervision/AlertPanel";
import { AiReviewPanel } from "@/components/supervision/AiReviewPanel";
import { AttachmentsPanel } from "@/components/supervision/AttachmentsPanel";
import { AuditPanel } from "@/components/supervision/AuditPanel";
import { BriefPanel } from "@/components/supervision/BriefPanel";
import { DialoguePanel } from "@/components/supervision/DialoguePanel";
import { ExecutionPanel } from "@/components/supervision/ExecutionPanel";
import { LatexPanel } from "@/components/supervision/LatexPanel";
import { LiteraturePanel } from "@/components/supervision/LiteraturePanel";
import { MembersPanel } from "@/components/supervision/MembersPanel";
import { OverviewPanel } from "@/components/supervision/OverviewPanel";
import { ReviewPanel } from "@/components/supervision/ReviewPanel";
import { TasksPanel } from "@/components/supervision/TasksPanel";
import { cn } from "@/lib/utils";

/**
 * 项目内页签。`roles` 决定谁能看见：导师与学生看到的是同一份数据的不同侧面，
 * 但写操作权限由后端裁决（前端隐藏按钮只是体验，不是安全边界）。
 */
// 顺序照 §10 的页面清单：研究讨论是项目的默认首页，不是空白概览页
// （§16.3 第1条）。概览只做数量概览，排在内容页签之后。
const TABS = [
  { key: "dialogue", label: "研究讨论", roles: ["student", "mentor"] },
  { key: "brief", label: "研究定义", roles: ["student", "mentor"] },
  { key: "tasks", label: "任务", roles: ["student", "mentor"] },
  { key: "execution", label: "进展与提交", roles: ["student", "mentor"] },
  { key: "review", label: "导师审核", roles: ["mentor"] },
  { key: "ai-review", label: "AI 预审", roles: ["student", "mentor"] },
  { key: "literature", label: "论文检索", roles: ["student", "mentor"] },
  { key: "latex", label: "稿件分析", roles: ["student", "mentor"] },
  { key: "attachments", label: "附件", roles: ["student", "mentor"] },
  { key: "overview", label: "概览", roles: ["student", "mentor"] },
  { key: "members", label: "成员与设置", roles: ["student", "mentor"] },
  { key: "audit", label: "审计日志", roles: ["student", "mentor"] },
] as const;

type TabKey = (typeof TABS)[number]["key"];

const STATUS_LABEL: Record<string, string> = {
  draft: "草稿",
  active: "进行中",
  archived: "已归档",
};

const ROLE_LABEL: Record<string, string> = {
  student: "学生",
  mentor: "导师",
};

/**
 * 项目详情外壳：一次取项目与成员，页签写进 URL（?tab=review），
 * 每个页签是一个独立面板组件，共享同一份 `project` 与刷新回调。
 */
export function SupervisionProjectPage() {
  const { projectRef } = useParams<{ projectRef: string }>();
  const [searchParams, setSearchParams] = useSearchParams();
  const project = useQuery({
    queryKey: ["supervision", "project", projectRef],
    queryFn: () => getProject(projectRef ?? ""),
    enabled: Boolean(projectRef),
  });

  if (project.isLoading) {
    return <p className="text-sm text-muted-foreground">加载中…</p>;
  }
  if (project.isError || !project.data) {
    return (
      <AlertPanel
        title="打不开该项目"
        description={
          project.error instanceof Error
            ? project.error.message
            : "你不是该项目成员，或项目已被删除。"
        }
      />
    );
  }

  const detail = project.data;
  const role = (detail.my_role ?? "student") as ProjectRole;
  // 没有 ?tab= 时进研究讨论（§16.3 第1条）；学生看不到导师审核时同样回落研究讨论。
  const active = (searchParams.get("tab") ?? "dialogue") as TabKey;
  const visible = TABS.filter((tab) => (tab.roles as readonly string[]).includes(role));
  const current = visible.some((tab) => tab.key === active) ? active : "dialogue";

  return (
    <div className="space-y-4">
      <header className="space-y-1">
        <div className="flex items-center gap-2">
          <h1 className="text-xl font-semibold">{detail.name}</h1>
          <Badge variant="secondary">
            {STATUS_LABEL[detail.status] ?? detail.status}
          </Badge>
          <Badge variant="outline">我是{ROLE_LABEL[role]}</Badge>
        </div>
        <p className="text-xs text-muted-foreground">
          {detail.code} · {detail.members.length} 名在册成员
          {detail.description ? ` · ${detail.description}` : ""}
        </p>
      </header>

      <nav className="flex flex-wrap gap-1 border-b">
        {visible.map((tab) => (
          <button
            key={tab.key}
            type="button"
            onClick={() => setSearchParams({ tab: tab.key })}
            className={cn(
              "rounded-t-md px-3 py-2 text-sm font-medium hover:bg-accent hover:text-accent-foreground",
              current === tab.key
                ? "border-b-2 border-primary text-foreground"
                : "text-muted-foreground",
            )}
          >
            {tab.label}
          </button>
        ))}
      </nav>

      <Panel
        tab={current}
        project={detail}
        refresh={() => project.refetch()}
      />
    </div>
  );
}

function Panel({
  tab,
  project,
  refresh,
}: {
  tab: TabKey;
  project: ProjectDetail;
  refresh: () => void;
}) {
  const props = { project, refresh };
  switch (tab) {
    case "overview":
      return <OverviewPanel {...props} />;
    case "dialogue":
      return <DialoguePanel {...props} />;
    case "brief":
      return <BriefPanel {...props} />;
    case "tasks":
      return <TasksPanel {...props} />;
    case "execution":
      return <ExecutionPanel {...props} />;
    case "review":
      return <ReviewPanel {...props} />;
    case "ai-review":
      return <AiReviewPanel {...props} />;
    case "literature":
      return <LiteraturePanel project={project} />;
    case "latex":
      return <LatexPanel {...props} />;
    case "attachments":
      return <AttachmentsPanel {...props} />;
    case "members":
      return <MembersPanel {...props} />;
    case "audit":
      return <AuditPanel {...props} />;
    default:
      return null;
  }
}
