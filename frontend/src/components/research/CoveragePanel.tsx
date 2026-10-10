import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { GitCompare, Trash2 } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";

import {
  deleteConflict,
  getCoverage,
  listConflicts,
  resolveConflict,
  supplementRun,
} from "@/api/research";
import type { ResearchConflict } from "@/api/research";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { COVERAGE_LABEL, CONFLICT_LABEL } from "@/components/research/labels";

/**
 * 证据覆盖与冲突：覆盖结果是纯计算（同样的证据必得同样的结论），
 * 补充检索必须回到研究运行里经用户确认，不在这里直接联网。
 */
export function CoveragePanel({
  projectId,
  papers,
  latestRunId,
}: {
  projectId: string;
  papers: { paper_id: string; title: string }[];
  latestRunId?: string;
}) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [resolving, setResolving] = useState<ResearchConflict | null>(null);
  const [resolution, setResolution] = useState("");

  const { data: coverage } = useQuery({
    queryKey: ["research-coverage", projectId],
    queryFn: () => getCoverage(projectId),
  });
  const { data: conflicts = [] } = useQuery({
    queryKey: ["research-conflicts", projectId],
    queryFn: () => listConflicts(projectId),
  });

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["research-coverage", projectId] });
    queryClient.invalidateQueries({ queryKey: ["research-conflicts", projectId] });
  };

  const resolveMutation = useMutation({
    mutationFn: ({ id, text }: { id: string; text: string }) => resolveConflict(id, text),
    onSuccess: () => {
      toast.success("冲突已裁决");
      setResolving(null);
      setResolution("");
      invalidate();
    },
    onError: (err) => toast.error(err instanceof Error ? err.message : "裁决失败"),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteConflict(id),
    onSuccess: () => {
      toast.success("冲突记录已删除");
      invalidate();
    },
    onError: (err) => toast.error(err instanceof Error ? err.message : "删除失败"),
  });

  const supplementMutation = useMutation({
    mutationFn: () => supplementRun(latestRunId ?? ""),
    onSuccess: (run) => {
      toast.success(`已创建第 ${run.round_no}/${run.max_rounds} 轮补充检索运行，请确认计划后执行`);
      navigate(`/research/${projectId}/runs/${run.run_id}`);
    },
    onError: (err) => toast.error(err instanceof Error ? err.message : "创建补充检索失败"),
  });

  const titleOf = (paperId: string) =>
    papers.find((p) => p.paper_id === paperId)?.title ?? paperId;
  const totals = coverage?.totals;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-base font-semibold">证据覆盖</h2>
        {totals && (
          <span className="text-xs text-muted-foreground">
            共 {totals.evidence} 条证据 · 子问题 {totals.covered} 充分 / {totals.thin} 偏薄 /{" "}
            {totals.missing} 缺失 · 未裁决冲突 {totals.open_conflicts}
          </span>
        )}
        <Button
          size="sm"
          variant="outline"
          className="ml-auto h-8 text-xs"
          disabled={!latestRunId || supplementMutation.isPending}
          title={latestRunId ? "基于最近一次运行创建补充检索" : "还没有可依据的运行"}
          onClick={() => supplementMutation.mutate()}
        >
          <GitCompare className="mr-1 h-3 w-3" />
          发起补充检索
        </Button>
      </div>

      <table className="w-full border-collapse text-xs">
        <thead>
          <tr className="bg-muted/50">
            <th className="border-b p-2 text-left">子问题</th>
            <th className="border-b p-2 text-left">状态</th>
            <th className="border-b p-2 text-left">证据 / 论文</th>
            <th className="border-b p-2 text-left">说明</th>
          </tr>
        </thead>
        <tbody>
          {(coverage?.sub_questions ?? []).map((row) => (
            <tr key={row.id}>
              <td className="border-b p-2 align-top">{row.question || row.id}</td>
              <td className="border-b p-2 align-top">
                <Badge
                  variant={
                    row.status === "covered" ? "default" : row.status === "thin" ? "secondary" : "destructive"
                  }
                >
                  {COVERAGE_LABEL[row.status ?? ""] ?? row.status}
                </Badge>
              </td>
              <td className="border-b p-2 align-top">
                {row.evidence_count} / {row.paper_count}
                <span className="ml-1 text-muted-foreground">
                  （已核对 {row.verified_count}）
                </span>
              </td>
              <td className="border-b p-2 align-top text-muted-foreground">
                {row.abstract_only ? "全部为摘要级证据；" : ""}
                {row.unverified_only ? "全部原文未能核对；" : ""}
              </td>
            </tr>
          ))}
          {(coverage?.sub_questions ?? []).length === 0 && (
            <tr>
              <td colSpan={4} className="p-3 text-center text-muted-foreground">
                还没有研究计划子问题，无法评估覆盖
              </td>
            </tr>
          )}
        </tbody>
      </table>

      {coverage && coverage.missing_dimensions.length > 0 && (
        <div className="text-xs">
          <span className="font-medium">完全没有证据的维度：</span>
          {coverage.missing_dimensions.map((d) => (
            <Badge key={d.id} variant="outline" className="ml-1">
              {d.label}
            </Badge>
          ))}
        </div>
      )}

      {coverage && coverage.papers_without_evidence.length > 0 && (
        <div className="text-xs text-muted-foreground">
          已纳入但还没有任何证据的论文：
          {coverage.papers_without_evidence.map(titleOf).join("、")}
        </div>
      )}

      {coverage && coverage.suggestions.length > 0 && (
        <div className="rounded-lg border p-3 text-xs">
          <div className="font-medium">补充检索建议（需用户确认，不自动执行）</div>
          <ul className="mt-1 space-y-1">
            {coverage.suggestions.map((s) => (
              <li key={s.sub_question_id}>
                「{s.question || s.sub_question_id}」：{s.reason}
                {s.queries.length > 0 && (
                  <span className="text-muted-foreground">（建议检索式：{s.queries.join("、")}）</span>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="space-y-2">
        <h3 className="text-sm font-semibold">冲突（{conflicts.length}）</h3>
        {conflicts.length === 0 && (
          <div className="py-4 text-center text-sm text-muted-foreground">未记录冲突</div>
        )}
        {conflicts.map((conflict) => (
          <div key={conflict.conflict_id} className="rounded-lg border p-3 text-sm">
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <Badge variant={conflict.status === "resolved" ? "default" : "destructive"}>
                {CONFLICT_LABEL[conflict.status] ?? conflict.status}
              </Badge>
              <span className="font-medium">{conflict.topic || "结论冲突"}</span>
              <Button
                size="sm"
                variant="ghost"
                className="ml-auto h-7 text-xs"
                onClick={() => deleteMutation.mutate(conflict.conflict_id)}
              >
                <Trash2 className="mr-1 h-3 w-3" />
                删除
              </Button>
            </div>
            {conflict.description && (
              <div className="mt-1 text-xs text-muted-foreground">{conflict.description}</div>
            )}
            <div className="mt-2 grid grid-cols-1 gap-2 md:grid-cols-2">
              {[
                { title: "甲方", side: conflict.side_a },
                { title: "乙方", side: conflict.side_b },
              ].map(({ title, side }) => (
                <div key={title} className="rounded-md border p-2 text-xs">
                  <div className="font-medium">{title}</div>
                  {side.map((item) => (
                    <blockquote
                      key={item.evidence_id}
                      className="mt-1 border-l-2 border-muted pl-2 text-muted-foreground"
                    >
                      {titleOf(item.paper_id)}：{item.quote}
                    </blockquote>
                  ))}
                </div>
              ))}
            </div>
            {conflict.resolution && (
              <div className="mt-2 text-xs">裁决：{conflict.resolution}</div>
            )}
            <Button
              size="sm"
              variant="outline"
              className="mt-2 h-7 text-xs"
              onClick={() => {
                setResolving(conflict);
                setResolution(conflict.resolution || "");
              }}
            >
              {conflict.status === "resolved" ? "修改裁决" : "人工裁决"}
            </Button>
          </div>
        ))}
      </div>

      <Dialog open={!!resolving} onOpenChange={(open) => !open && setResolving(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>裁决冲突</DialogTitle>
          </DialogHeader>
          <div className="space-y-3">
            <div className="space-y-1">
              <Label>裁决说明（依据哪一方证据、为什么）</Label>
              <Textarea value={resolution} onChange={(e) => setResolution(e.target.value)} rows={4} />
            </div>
            <Button
              className="w-full"
              disabled={resolveMutation.isPending || !resolution.trim()}
              onClick={() =>
                resolveMutation.mutate({ id: resolving?.conflict_id ?? "", text: resolution })
              }
            >
              保存裁决
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
