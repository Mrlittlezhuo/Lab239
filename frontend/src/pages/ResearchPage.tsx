import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Search, Trash2 } from "lucide-react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { toast } from "sonner";

import {
  createProject,
  deleteProject,
  listProjects,
  listPapers,
  listRuns,
  setScreening,
} from "@/api/research";
import type { ResearchPaper } from "@/api/research";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { ComparisonPanel } from "@/components/research/ComparisonPanel";
import { CoveragePanel } from "@/components/research/CoveragePanel";
import { EvidencePanel } from "@/components/research/EvidencePanel";
import { PaperSourceActions } from "@/components/research/PaperSourceActions";
import type { SubQuestionOption } from "@/components/research/PaperSourceActions";
import { ReportPanel } from "@/components/research/ReportPanel";
import { cn } from "@/lib/utils";

const TABS = [
  { key: "papers", label: "候选论文" },
  { key: "evidence", label: "证据审核" },
  { key: "comparison", label: "比较矩阵" },
  { key: "coverage", label: "覆盖与冲突" },
  { key: "report", label: "研究报告" },
] as const;

type TabKey = (typeof TABS)[number]["key"];

const RUN_STATUS_LABEL: Record<string, string> = {
  pending: "待生成计划",
  awaiting_confirmation: "等待确认",
  running: "执行中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

const SCREENING_LABEL: Record<string, string> = {
  pending_review: "待筛选",
  included: "纳入",
  excluded: "排除",
};

export function ResearchPage() {
  const { projectId } = useParams<{ projectId?: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [createOpen, setCreateOpen] = useState(false);
  const [name, setName] = useState("");
  const [question, setQuestion] = useState("");
  const [scope, setScope] = useState("");
  const [screenFilter, setScreenFilter] = useState<string | undefined>();
  // 页签写进 URL：研究运行页的复核门禁可直达对应页签（?tab=evidence）
  const [searchParams, setSearchParams] = useSearchParams();
  const tabParam = searchParams.get("tab");
  const tab: TabKey = TABS.some((t) => t.key === tabParam) ? (tabParam as TabKey) : "papers";
  const setTab = (next: TabKey) => setSearchParams({ tab: next });

  const { data: projects = [] } = useQuery({
    queryKey: ["research-projects"],
    queryFn: () => listProjects(),
  });
  const { data: runs = [] } = useQuery({
    queryKey: ["research-runs", projectId],
    queryFn: () => (projectId ? listRuns(projectId) : Promise.resolve([])),
    enabled: !!projectId,
    refetchInterval: (query) =>
      query.state.data?.some((r) => r.status === "running" || r.status === "pending")
        ? 3000
        : false,
  });
  const { data: papers = [] } = useQuery({
    queryKey: ["research-papers", projectId, screenFilter],
    queryFn: () =>
      projectId ? listPapers(projectId, screenFilter) : Promise.resolve([]),
    enabled: !!projectId,
  });

  // 证据提取按子问题组织：取最近一次带计划的运行，id 规则与后端覆盖统计一致
  const subQuestions = useMemo<SubQuestionOption[]>(() => {
    const withPlan = runs.find((r) => (r.plan?.sub_questions ?? []).length > 0);
    return (withPlan?.plan?.sub_questions ?? []).map((s, i) => ({
      id: String(s.id ?? `sq${i + 1}`),
      question: String(s.question ?? ""),
    }));
  }, [runs]);
  const latestRunId = runs[0]?.run_id;

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["research-projects"] });
    queryClient.invalidateQueries({ queryKey: ["research-runs"] });
    queryClient.invalidateQueries({ queryKey: ["research-papers"] });
  };

  const createMutation = useMutation({
    mutationFn: () =>
      createProject({ name, research_question: question, scope }),
    onSuccess: (p) => {
      toast.success("研究项目已创建");
      setCreateOpen(false);
      setName("");
      setQuestion("");
      setScope("");
      invalidate();
      navigate(`/research/${p.project_id}`);
    },
    onError: (err) => toast.error(err instanceof Error ? err.message : "创建失败"),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteProject(id),
    onSuccess: () => {
      toast.success("项目已删除");
      if (projectId) navigate("/research");
      invalidate();
    },
    onError: (err) => toast.error(err instanceof Error ? err.message : "删除失败"),
  });

  const screeningMutation = useMutation({
    mutationFn: ({ id, status }: { id: string; status: string }) =>
      setScreening(id, status),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["research-papers", projectId] }),
    onError: (err) => toast.error(err instanceof Error ? err.message : "筛选失败"),
  });

  return (
    <div className="flex h-full">
      <div className="flex w-64 shrink-0 flex-col border-r">
        <div className="p-2">
          <Button size="sm" className="w-full" onClick={() => setCreateOpen(true)}>
            <Plus className="h-4 w-4" />
            新建研究项目
          </Button>
        </div>
        <div className="flex-1 space-y-1 overflow-y-auto px-2 pb-2">
          {projects.map((p) => (
            <div
              key={p.project_id}
              className={cn(
                "group flex cursor-pointer items-center gap-1 rounded-md px-2 py-1.5 text-sm hover:bg-accent",
                projectId === p.project_id && "bg-accent",
              )}
              onClick={() => navigate(`/research/${p.project_id}`)}
            >
              <span className="min-w-0 flex-1 truncate">{p.name}</span>
              <button
                type="button"
                className="hidden text-muted-foreground hover:text-destructive group-hover:block"
                title="删除项目"
                onClick={(e) => {
                  e.stopPropagation();
                  deleteMutation.mutate(p.project_id);
                }}
              >
                <Trash2 className="h-3.5 w-3.5" />
              </button>
            </div>
          ))}
          {projects.length === 0 && (
            <div className="px-2 py-4 text-center text-xs text-muted-foreground">
              暂无研究项目
            </div>
          )}
        </div>
      </div>

      <div className="min-w-0 flex-1 space-y-4 overflow-y-auto p-2">
        {!projectId ? (
          <div className="py-16 text-center text-muted-foreground">
            选择或创建一个研究项目
          </div>
        ) : (
          <>
            <div className="space-y-2">
              <h1 className="text-lg font-semibold">研究运行</h1>
              {runs.map((run) => (
                <div
                  key={run.run_id}
                  className="flex cursor-pointer items-center gap-2 rounded-lg border p-3 text-sm hover:bg-accent/40"
                  onClick={() =>
                    navigate(`/research/${projectId}/runs/${run.run_id}`)
                  }
                >
                  <Badge
                    variant={
                      run.status === "completed"
                        ? "default"
                        : run.status === "failed"
                          ? "destructive"
                          : "secondary"
                    }
                  >
                    {RUN_STATUS_LABEL[run.status] ?? run.status}
                  </Badge>
                  <span className="text-muted-foreground">
                    阶段：{run.stage ?? "-"}
                  </span>
                  <span className="ml-auto text-xs text-muted-foreground">
                    {run.create_time ?? ""}
                  </span>
                </div>
              ))}
              {runs.length === 0 && (
                <div className="py-6 text-center text-sm text-muted-foreground">
                  还没有运行。进入项目后从“研究运行”页创建。
                </div>
              )}
              <Button
                size="sm"
                variant="outline"
                onClick={() =>
                  navigate(`/research/${projectId}/runs/new`)
                }
              >
                创建新运行
              </Button>
            </div>

            <div className="flex gap-1 border-b text-sm">
              {TABS.map((t) => (
                <button
                  key={t.key}
                  type="button"
                  className={cn(
                    "border-b-2 px-3 py-1.5",
                    tab === t.key
                      ? "border-primary font-medium"
                      : "border-transparent text-muted-foreground hover:text-foreground",
                  )}
                  onClick={() => setTab(t.key)}
                >
                  {t.label}
                </button>
              ))}
            </div>

            {tab === "papers" && (
              <div className="space-y-2">
                <div className="flex items-center gap-2">
                  <h2 className="text-base font-semibold">候选论文（{papers.length}）</h2>
                  <div className="ml-auto flex gap-1 text-xs">
                    {["", "pending_review", "included", "excluded"].map((s) => (
                      <button
                        key={s}
                        type="button"
                        className={cn(
                          "rounded-md border px-2 py-1",
                          (screenFilter ?? "") === s && "bg-accent",
                        )}
                        onClick={() => setScreenFilter(s || undefined)}
                      >
                        {s ? SCREENING_LABEL[s] : "全部"}
                      </button>
                    ))}
                  </div>
                </div>
                {papers.map((paper) => (
                  <PaperRow
                    key={paper.project_paper_id}
                    paper={paper}
                    projectId={projectId}
                    subQuestions={subQuestions}
                    latestRunId={latestRunId}
                    onScreen={(status) =>
                      screeningMutation.mutate({ id: paper.project_paper_id, status })
                    }
                  />
                ))}
                {papers.length === 0 && (
                  <div className="py-6 text-center text-sm text-muted-foreground">
                    暂无候选论文
                  </div>
                )}
              </div>
            )}
            {tab === "evidence" && (
              <EvidencePanel
                projectId={projectId}
                papers={papers.map((p) => ({ paper_id: p.paper_id, title: p.title }))}
              />
            )}
            {tab === "comparison" && <ComparisonPanel projectId={projectId} />}
            {tab === "coverage" && (
              <CoveragePanel
                projectId={projectId}
                papers={papers.map((p) => ({ paper_id: p.paper_id, title: p.title }))}
                latestRunId={latestRunId}
              />
            )}
            {tab === "report" && <ReportPanel projectId={projectId} latestRunId={latestRunId} />}
          </>
        )}
      </div>

      <Dialog open={createOpen} onOpenChange={setCreateOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>新建研究项目</DialogTitle>
          </DialogHeader>
          <div className="space-y-3">
            <div className="space-y-1">
              <Label>项目名称</Label>
              <Input value={name} onChange={(e) => setName(e.target.value)} />
            </div>
            <div className="space-y-1">
              <Label>研究问题</Label>
              <Textarea
                value={question}
                onChange={(e) => setQuestion(e.target.value)}
              />
            </div>
            <div className="space-y-1">
              <Label>范围/约束</Label>
              <Textarea value={scope} onChange={(e) => setScope(e.target.value)} />
            </div>
            <Button
              className="w-full"
              onClick={() => createMutation.mutate()}
              disabled={createMutation.isPending || !name.trim() || !question.trim()}
            >
              创建
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function PaperRow({
  paper,
  projectId,
  subQuestions,
  latestRunId,
  onScreen,
}: {
  paper: ResearchPaper;
  projectId: string;
  subQuestions: SubQuestionOption[];
  latestRunId?: string;
  onScreen: (status: string) => void;
}) {
  return (
    <div className="rounded-lg border p-3 text-sm">
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">
          <div className="font-medium">{paper.title}</div>
          <div className="mt-0.5 text-xs text-muted-foreground">
            {paper.authors}
            {paper.year ? ` · ${paper.year}` : ""} · {paper.source}
            {paper.arxiv_id ? ` · arXiv:${paper.arxiv_id}` : ""}
          </div>
          {paper.abstract && (
            <p className="mt-1 line-clamp-2 text-xs text-muted-foreground">
              {paper.abstract}
            </p>
          )}
          <div className="mt-2">
            <PaperSourceActions
              projectId={projectId}
              paper={paper}
              subQuestions={subQuestions}
              latestRunId={latestRunId}
            />
          </div>
        </div>
        <div className="flex shrink-0 flex-col items-end gap-1">
          <Badge
            variant={
              paper.screening_status === "included"
                ? "default"
                : paper.screening_status === "excluded"
                  ? "destructive"
                  : "secondary"
            }
          >
            {SCREENING_LABEL[paper.screening_status] ?? paper.screening_status}
          </Badge>
          <div className="flex gap-1">
            <Button size="sm" variant="outline" className="h-7 text-xs" onClick={() => onScreen("included")}>
              纳入
            </Button>
            <Button size="sm" variant="outline" className="h-7 text-xs" onClick={() => onScreen("excluded")}>
              排除
            </Button>
            {paper.url && (
              <a
                href={paper.url}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex h-7 items-center rounded-md border px-2 text-xs hover:bg-accent"
              >
                <Search className="mr-1 h-3 w-3" />
                原文
              </a>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
