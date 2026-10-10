import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Check, FileText, Pencil, Play, RefreshCw, StopCircle } from "lucide-react";
import { useNavigate, useParams } from "react-router-dom";
import { toast } from "sonner";

import {
  cancelRun,
  confirmRun,
  createRun,
  generatePlan,
  getRun,
  resumeRun,
  runArtifacts,
  updatePlan,
} from "@/api/research";
import type { ResearchPlan } from "@/api/research";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";

const STATUS_LABEL: Record<string, { text: string; cls: string }> = {
  pending: { text: "待生成计划", cls: "bg-muted text-muted-foreground" },
  awaiting_confirmation: { text: "等待确认", cls: "bg-amber-100 text-amber-800" },
  running: { text: "执行中", cls: "bg-blue-100 text-blue-800" },
  completed: { text: "已完成", cls: "bg-emerald-100 text-emerald-800" },
  failed: { text: "失败", cls: "bg-destructive/10 text-destructive" },
  cancelled: { text: "已取消", cls: "bg-muted text-muted-foreground" },
};

const STAGE_LABEL: Record<string, string> = {
  draft: "草稿",
  plan: "计划",
  retrieval: "检索",
  dedup: "去重",
  screening: "筛选",
  persist: "入库",
  fulltext: "读取原文",
  evidence: "提取证据",
  evidence_review: "等待证据复核",
  coverage: "覆盖与冲突分析",
  comparison: "生成比较",
  report: "生成报告",
  report_review: "等待报告复核",
  done: "完成",
};

/** 人工确认点：状态 awaiting_confirmation，stage 表明正在复核什么（任务 018 阶段 E）。 */
const GATE_HINT: Record<string, string> = {
  plan: "请检查检索计划（可编辑子问题与检索词），确认后开始检索。",
  evidence_review:
    "证据已提取并逐字核对，请在“证据”页确认/修改/驳回后回来继续；未核对上的原文条目不会被采用。",
  report_review:
    "报告已生成（引用可反查证据），请在“报告”页查看或编辑后回来继续；保存编辑只会追加新版本。",
};

export function ResearchRunPage() {
  const { projectId, runId } = useParams<{ projectId: string; runId: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const isNew = runId === "new";

  const createMutation = useMutation({
    mutationFn: () => createRun(projectId!),
    onSuccess: (run) => {
      queryClient.invalidateQueries({ queryKey: ["research-runs", projectId] });
      navigate(`/research/${projectId}/runs/${run.run_id}`, { replace: true });
    },
    onError: (err) => toast.error(err instanceof Error ? err.message : "创建运行失败"),
  });

  if (isNew) {
    return (
      <div className="space-y-4">
        <BackHeader projectId={projectId ?? ""} title="新建研究运行" />
        <div className="rounded-lg border p-6 text-center">
          <p className="mb-4 text-sm text-muted-foreground">
            将基于该项目的研究问题创建一次新的检索运行，创建后需生成并确认检索计划。
          </p>
          <Button onClick={() => createMutation.mutate()} disabled={createMutation.isPending}>
            创建运行
          </Button>
        </div>
      </div>
    );
  }

  return <RunDetail projectId={projectId ?? ""} runId={runId ?? ""} />;
}

function RunDetail({ projectId, runId }: { projectId: string; runId: string }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);

  const { data: run, error } = useQuery({
    queryKey: ["research-run", runId],
    queryFn: () => getRun(runId),
    refetchInterval: (query) => {
      const s = query.state.data?.status;
      return s === "running" || s === "pending" ? 3000 : false;
    },
  });

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["research-run", runId] });
    queryClient.invalidateQueries({ queryKey: ["research-runs", projectId] });
    queryClient.invalidateQueries({ queryKey: ["research-papers", projectId] });
  };
  const onError = (label: string) => (err: unknown) =>
    toast.error(err instanceof Error ? err.message : label);

  const planMutation = useMutation({
    mutationFn: () => generatePlan(runId),
    onSuccess: () => {
      toast.success("检索计划已生成，请检查后确认");
      setEditing(false);
      invalidate();
    },
    onError: onError("生成计划失败"),
  });
  const saveMutation = useMutation({
    mutationFn: (plan: ResearchPlan) => updatePlan(runId, plan),
    onSuccess: () => {
      toast.success("计划已保存");
      setEditing(false);
      invalidate();
    },
    onError: onError("保存计划失败"),
  });
  const confirmMutation = useMutation({
    mutationFn: () => confirmRun(runId),
    onSuccess: () => {
      toast.success("已确认，运行开始执行");
      invalidate();
    },
    onError: onError("确认失败"),
  });
  const resumeMutation = useMutation({
    mutationFn: () => resumeRun(runId),
    onSuccess: () => {
      toast.success("已恢复运行");
      invalidate();
    },
    onError: onError("恢复失败"),
  });
  const cancelMutation = useMutation({
    mutationFn: () => cancelRun(runId),
    onSuccess: () => {
      toast.success("已请求取消");
      invalidate();
    },
    onError: onError("取消失败"),
  });
  const artifactsMutation = useMutation({
    mutationFn: () => runArtifacts(runId),
    onSuccess: () => {
      toast.success("已基于纳入的论文继续产出证据/比较/报告");
      invalidate();
    },
    onError: onError("生成证据与报告失败"),
  });

  if (error) {
    return (
      <div className="space-y-4">
        <BackHeader projectId={projectId} title="研究运行" />
        <div className="rounded-lg border p-6 text-center text-sm text-destructive">
          {error instanceof Error ? error.message : "加载失败"}
        </div>
      </div>
    );
  }
  if (!run) {
    return (
      <div className="space-y-4">
        <BackHeader projectId={projectId} title="研究运行" />
        <div className="py-10 text-center text-muted-foreground">加载中…</div>
      </div>
    );
  }

  const status = STATUS_LABEL[run.status] ?? {
    text: run.status,
    cls: "bg-muted text-muted-foreground",
  };
  // awaiting_confirmation 可能是计划确认，也可能是证据/报告复核门禁
  const gate = run.status === "awaiting_confirmation" ? run.stage ?? "plan" : "";
  const isPlanGate = gate === "plan" || gate === "draft";
  const canEditPlan = run.status === "pending" || isPlanGate;
  const canGenerate = run.status === "pending";
  const canConfirm = run.status === "awaiting_confirmation";
  const canCancel = run.status === "pending" || run.status === "awaiting_confirmation" || run.status === "running";
  const canResume = run.status === "failed" || run.status === "cancelled";
  const canArtifacts = run.status === "completed";

  return (
    <div className="space-y-4">
      <BackHeader projectId={projectId} title={`运行 ${run.run_id.slice(0, 8)}`} />

      <div className="flex flex-wrap items-center gap-2">
        <span className={cn("rounded-md px-2 py-1 text-xs font-medium", status.cls)}>
          {status.text}
        </span>
        {run.stage && (
          <Badge variant="secondary">阶段：{STAGE_LABEL[run.stage] ?? run.stage}</Badge>
        )}
        <span className="text-xs text-muted-foreground">
          创建 {run.create_time ?? "-"}
          {run.start_time ? ` · 开始 ${run.start_time}` : ""}
          {run.end_time ? ` · 结束 ${run.end_time}` : ""}
        </span>
        <div className="ml-auto flex flex-wrap gap-2">
          {canGenerate && (
            <Button size="sm" onClick={() => planMutation.mutate()} disabled={planMutation.isPending}>
              <RefreshCw className={cn("h-4 w-4", planMutation.isPending && "animate-spin")} />
              生成检索计划
            </Button>
          )}
          {canEditPlan && run.plan && Object.keys(run.plan).length > 0 && !editing && (
            <Button size="sm" variant="outline" onClick={() => setEditing(true)}>
              <Pencil className="h-4 w-4" />
              编辑计划
            </Button>
          )}
          {canConfirm && (
            <Button size="sm" onClick={() => confirmMutation.mutate()} disabled={confirmMutation.isPending}>
              <Check className="h-4 w-4" />
              {isPlanGate ? "确认并开始" : "确认复核结果并继续"}
            </Button>
          )}
          {canArtifacts && (
            <Button size="sm" onClick={() => artifactsMutation.mutate()} disabled={artifactsMutation.isPending}>
              <FileText className={cn("h-4 w-4", artifactsMutation.isPending && "animate-spin")} />
              生成证据与报告
            </Button>
          )}
          {canResume && (
            <Button size="sm" variant="outline" onClick={() => resumeMutation.mutate()} disabled={resumeMutation.isPending}>
              <Play className="h-4 w-4" />
              恢复运行
            </Button>
          )}
          {canCancel && (
            <Button size="sm" variant="destructive" onClick={() => cancelMutation.mutate()} disabled={cancelMutation.isPending}>
              <StopCircle className="h-4 w-4" />
              取消
            </Button>
          )}
        </div>
      </div>

      {run.error && (
        <div className="rounded-lg border border-destructive/40 bg-destructive/5 p-3 text-sm text-destructive">
          {run.error}
        </div>
      )}

      {gate && GATE_HINT[gate] && (
        <div className="space-y-2 rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900">
          <div className="font-medium">
            等待复核：{STAGE_LABEL[gate] ?? gate}
          </div>
          <p>{GATE_HINT[gate]}</p>
          {(gate === "evidence_review" || gate === "report_review") && (
            <Button
              size="sm"
              variant="outline"
              onClick={() =>
                navigate(
                  `/research/${projectId}?tab=${gate === "evidence_review" ? "evidence" : "report"}`,
                )
              }
            >
              前往复核
            </Button>
          )}
        </div>
      )}

      <RunSummary summary={run.summary ?? {}} projectId={projectId} />

      {editing ? (
        <PlanEditor
          plan={run.plan ?? {}}
          saving={saveMutation.isPending}
          onCancel={() => setEditing(false)}
          onSave={(plan) => saveMutation.mutate(plan)}
        />
      ) : (
        <PlanView plan={run.plan} />
      )}

      {run.source_stats && run.source_stats.length > 0 && (
        <div className="space-y-2">
          <h2 className="text-base font-semibold">检索来源统计</h2>
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b text-left text-xs text-muted-foreground">
                <th className="py-2">来源</th>
                <th className="py-2">查询数</th>
                <th className="py-2">检索到</th>
                <th className="py-2">保留</th>
                <th className="py-2">错误</th>
              </tr>
            </thead>
            <tbody>
              {run.source_stats.map((s, i) => (
                <tr key={i} className="border-b last:border-0">
                  <td className="py-2">{s.source ?? "-"}</td>
                  <td>{s.queries ?? "-"}</td>
                  <td>{s.retrieved ?? "-"}</td>
                  <td>{s.kept ?? "-"}</td>
                  <td className="text-xs text-destructive">{s.error ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="flex gap-2">
        <Button
          size="sm"
          variant="outline"
          onClick={() => navigate(`/research/${projectId}`)}
        >
          查看候选论文
        </Button>
        {run.agent_run_id && (
          <Button
            size="sm"
            variant="outline"
            onClick={() => navigate(`/agent-runs/${run.agent_run_id}`)}
          >
            查看 Agent 运行
          </Button>
        )}
      </div>
    </div>
  );
}

function BackHeader({ projectId, title }: { projectId: string; title: string }) {
  const navigate = useNavigate();
  return (
    <div className="flex items-center gap-2">
      <Button variant="ghost" size="sm" onClick={() => navigate(`/research/${projectId}`)}>
        <ArrowLeft className="h-4 w-4" />
        返回项目
      </Button>
      <h1 className="text-lg font-semibold">{title}</h1>
    </div>
  );
}

/** 运行摘要：各阶段产物计数与如实说明（缺证据/被拒绝条目都显示）。 */
function RunSummary({
  summary,
  projectId,
}: {
  summary: Record<string, unknown>;
  projectId: string;
}) {
  const navigate = useNavigate();
  const num = (key: string) => {
    const v = summary[key];
    return typeof v === "number" ? v : undefined;
  };
  const artifacts = summary.artifacts as Record<string, number> | undefined;
  const evidenceCounts = summary.evidence_counts as Record<string, number> | undefined;
  const notes = [summary.artifact_note, summary.evidence_note].filter(
    (x): x is string => typeof x === "string" && x.length > 0,
  );
  const warnings = [
    ...((summary.evidence_rejected as string[] | undefined) ?? []),
    ...((summary.comparison_rejected as string[] | undefined) ?? []),
    ...((summary.report_warnings as string[] | undefined) ?? []),
  ];
  const missing = (summary.missing_dimensions as string[] | undefined) ?? [];

  const chips: string[] = [];
  const papersRead = num("papers_read");
  if (papersRead !== undefined) chips.push(`读取原文 ${papersRead} 篇`);
  if (evidenceCounts) {
    const total = Object.values(evidenceCounts).reduce((a, b) => a + (b || 0), 0);
    chips.push(`证据 ${total} 条`);
  }
  const comparisons = num("comparisons_written");
  if (comparisons !== undefined) chips.push(`比较 ${comparisons} 条`);
  const conflicts = num("conflicts_recorded");
  if (conflicts !== undefined) chips.push(`冲突 ${conflicts} 条`);
  const reportVersion = num("report_version");
  if (reportVersion !== undefined) chips.push(`报告 v${reportVersion}`);
  const citations = num("report_citations");
  if (citations !== undefined) chips.push(`引用 ${citations} 条`);
  if (artifacts) {
    chips.push(
      `汇总：论文 ${artifacts.papers ?? 0} · 证据 ${artifacts.evidence ?? 0} · 比较 ${artifacts.comparisons ?? 0} · 冲突 ${artifacts.conflicts ?? 0} · 报告 ${artifacts.reports ?? 0}`,
    );
  }

  if (chips.length === 0 && notes.length === 0 && warnings.length === 0 && missing.length === 0) {
    return null;
  }
  return (
    <div className="space-y-2 rounded-lg border p-3">
      <h2 className="text-base font-semibold">阶段产物</h2>
      {chips.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {chips.map((c, i) => (
            <Badge key={i} variant="secondary">
              {c}
            </Badge>
          ))}
        </div>
      )}
      {missing.length > 0 && (
        <p className="text-xs text-muted-foreground">
          缺证据的维度：{missing.join("、")}
          <button
            type="button"
            className="ml-2 underline"
            onClick={() => navigate(`/research/${projectId}?tab=coverage`)}
          >
            查看补充检索建议
          </button>
        </p>
      )}
      {notes.map((n, i) => (
        <p key={i} className="text-xs text-muted-foreground">
          {n}
        </p>
      ))}
      {warnings.length > 0 && (
        <ul className="list-inside list-disc space-y-1 text-xs text-destructive">
          {warnings.slice(0, 10).map((w, i) => (
            <li key={i}>{w}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

function PlanView({ plan }: { plan: ResearchPlan | undefined }) {
  const subQuestions = plan?.sub_questions ?? [];
  if (!plan || Object.keys(plan).length === 0) {
    return (
      <div className="rounded-lg border p-6 text-center text-sm text-muted-foreground">
        尚无计划。生成计划后可编辑确认。
      </div>
    );
  }
  return (
    <div className="space-y-3">
      <h2 className="text-base font-semibold">检索计划</h2>
      {plan.summary && (
        <p className="rounded-lg border bg-muted/30 p-3 text-sm">{plan.summary}</p>
      )}
      {subQuestions.length > 0 && (
        <div className="space-y-2">
          {subQuestions.map((sq, i) => (
            <div key={sq.id ?? i} className="rounded-lg border p-3 text-sm">
              <div className="font-medium">
                {i + 1}. {sq.question}
              </div>
              {sq.queries && sq.queries.length > 0 && (
                <ul className="mt-1 list-inside list-disc text-xs text-muted-foreground">
                  {sq.queries.map((q, j) => (
                    <li key={j}>{q}</li>
                  ))}
                </ul>
              )}
              {sq.sources && sq.sources.length > 0 && (
                <div className="mt-1 flex gap-1">
                  {sq.sources.map((s) => (
                    <Badge key={s} variant="outline">
                      {s}
                    </Badge>
                  ))}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
      {subQuestions.length === 0 && (
        <pre className="overflow-x-auto rounded-lg border bg-muted/30 p-3 text-xs">
          {JSON.stringify(plan, null, 2)}
        </pre>
      )}
    </div>
  );
}

interface DraftSubQuestion {
  question: string;
  queries: string; // 每行一条
  sources: string; // 逗号分隔
}

function PlanEditor({
  plan,
  saving,
  onSave,
  onCancel,
}: {
  plan: ResearchPlan;
  saving: boolean;
  onSave: (plan: ResearchPlan) => void;
  onCancel: () => void;
}) {
  const initial = useMemo<DraftSubQuestion[]>(
    () =>
      (plan.sub_questions ?? []).map((sq) => ({
        question: sq.question ?? "",
        queries: (sq.queries ?? []).join("\n"),
        sources: (sq.sources ?? []).join(", "),
      })),
    [plan],
  );
  const [summary, setSummary] = useState(plan.summary ?? "");
  const [items, setItems] = useState<DraftSubQuestion[]>(initial);

  // 计划异步刷新后重置本地草稿
  useEffect(() => {
    setSummary(plan.summary ?? "");
    setItems(initial);
  }, [plan, initial]);

  const patch = (i: number, next: Partial<DraftSubQuestion>) =>
    setItems((prev) => prev.map((it, j) => (j === i ? { ...it, ...next } : it)));

  const save = () => {
    const next: ResearchPlan = {
      ...plan,
      summary: summary || undefined,
      sub_questions: items
        .filter((it) => it.question.trim())
        .map((it, i) => ({
          id: plan.sub_questions?.[i]?.id ?? `q${i + 1}`,
          question: it.question.trim(),
          queries: it.queries
            .split("\n")
            .map((q) => q.trim())
            .filter(Boolean),
          sources: it.sources
            .split(",")
            .map((s) => s.trim())
            .filter(Boolean),
        })),
    };
    onSave(next);
  };

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <h2 className="text-base font-semibold">编辑检索计划</h2>
        <div className="flex gap-2">
          <Button
            size="sm"
            variant="outline"
            onClick={() =>
              setItems((prev) => [...prev, { question: "", queries: "", sources: "" }])
            }
          >
            添加子问题
          </Button>
          <Button size="sm" variant="ghost" onClick={onCancel}>
            取消
          </Button>
          <Button size="sm" onClick={save} disabled={saving}>
            {saving ? "保存中…" : "保存计划"}
          </Button>
        </div>
      </div>
      <div className="space-y-1">
        <Label>计划概述</Label>
        <Textarea value={summary} onChange={(e) => setSummary(e.target.value)} />
      </div>
      {items.map((it, i) => (
        <div key={i} className="space-y-2 rounded-lg border p-3">
          <div className="flex items-center gap-2">
            <span className="text-sm font-medium">子问题 {i + 1}</span>
            <button
              type="button"
              className="ml-auto text-xs text-muted-foreground hover:text-destructive"
              onClick={() => setItems((prev) => prev.filter((_, j) => j !== i))}
            >
              删除
            </button>
          </div>
          <Input
            value={it.question}
            onChange={(e) => patch(i, { question: e.target.value })}
            placeholder="子问题"
          />
          <div className="grid gap-2 md:grid-cols-2">
            <div className="space-y-1">
              <Label className="text-xs">检索词（每行一条）</Label>
              <Textarea
                rows={3}
                value={it.queries}
                onChange={(e) => patch(i, { queries: e.target.value })}
              />
            </div>
            <div className="space-y-1">
              <Label className="text-xs">来源（逗号分隔）</Label>
              <Input
                value={it.sources}
                onChange={(e) => patch(i, { sources: e.target.value })}
                placeholder="arxiv, crossref"
              />
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}
