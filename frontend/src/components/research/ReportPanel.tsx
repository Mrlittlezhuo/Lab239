import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, FileText, Pencil, Sparkles, Trash2 } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { toast } from "sonner";

import {
  deleteReport,
  editReport,
  exportReportBibtex,
  exportReportMarkdown,
  generateReport,
  getReport,
  listReports,
} from "@/api/research";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { prepareReport } from "@/components/research/reportText";
import { cn } from "@/lib/utils";
import { downloadText } from "@/lib/download";

/**
 * 版本化报告：生成与编辑都产生新版本，旧版本永不覆盖；
 * 每条事实引用都能反查到证据卡片与原文定位，无证据结论单列标注。
 */
export function ReportPanel({
  projectId,
  latestRunId,
}: {
  projectId: string;
  latestRunId?: string;
}) {
  const queryClient = useQueryClient();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [draft, setDraft] = useState<string | null>(null);

  const { data: reports = [] } = useQuery({
    queryKey: ["research-reports", projectId],
    queryFn: () => listReports(projectId, true),
  });
  const { data: report } = useQuery({
    queryKey: ["research-report", selectedId],
    queryFn: () => (selectedId ? getReport(selectedId) : Promise.resolve(null)),
    enabled: !!selectedId,
  });

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["research-reports", projectId] });
    queryClient.invalidateQueries({ queryKey: ["research-report"] });
  };

  const generateMutation = useMutation({
    mutationFn: () => generateReport(projectId, latestRunId ? { run_id: latestRunId } : {}),
    onSuccess: (r) => {
      toast.success(`报告 v${r.version} 已生成`);
      for (const w of r.warnings ?? []) toast.warning(w);
      setSelectedId(r.report_id);
      invalidate();
    },
    onError: (err) => toast.error(err instanceof Error ? err.message : "生成报告失败"),
  });

  const editMutation = useMutation({
    mutationFn: ({ id, content }: { id: string; content: string }) => editReport(id, { content }),
    onSuccess: (r) => {
      toast.success(`已保存为 v${r.version}（旧版本保留）`);
      for (const w of r.warnings ?? []) toast.warning(w);
      setDraft(null);
      setSelectedId(r.report_id);
      invalidate();
    },
    onError: (err) => toast.error(err instanceof Error ? err.message : "保存失败"),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteReport(id),
    onSuccess: () => {
      toast.success("该版本已删除");
      setSelectedId(null);
      invalidate();
    },
    onError: (err) => toast.error(err instanceof Error ? err.message : "删除失败"),
  });

  const exportMutation = useMutation({
    mutationFn: async ({ id, kind }: { id: string; kind: "md" | "bib" }) => {
      const text = kind === "md" ? await exportReportMarkdown(id) : await exportReportBibtex(id);
      return { text, kind };
    },
    onSuccess: ({ text, kind }) => {
      if (report) {
        downloadText(text, `${report.title || "研究报告"}-v${report.version}.${kind}`);
      }
    },
    onError: (err) => toast.error(err instanceof Error ? err.message : "导出失败"),
  });

  const view = report ? prepareReport(report.content, report.citations) : null;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-base font-semibold">研究报告</h2>
        <div className="ml-auto flex gap-2">
          <Button
            size="sm"
            className="h-8 text-xs"
            onClick={() => generateMutation.mutate()}
            disabled={generateMutation.isPending}
          >
            <Sparkles className="mr-1 h-3 w-3" />
            生成新报告
          </Button>
        </div>
      </div>

      {reports.length === 0 && (
        <div className="py-6 text-center text-sm text-muted-foreground">
          还没有报告。先完成证据提取与比较，再生成报告。
        </div>
      )}

      {reports.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {reports.map((r) => (
            <button
              key={r.report_id}
              type="button"
              className={cn(
                "rounded-md border px-2 py-1 text-xs hover:bg-accent",
                selectedId === r.report_id && "bg-accent",
              )}
              onClick={() => {
                setSelectedId(r.report_id);
                setDraft(null);
              }}
            >
              v{r.version}
              <span className="ml-1 text-muted-foreground">
                {r.status === "edited" ? "人工编辑" : "自动生成"} · {r.create_time?.slice(0, 16) ?? ""}
              </span>
            </button>
          ))}
        </div>
      )}

      {report && (
        <div className="space-y-3 rounded-lg border p-3">
          <div className="flex flex-wrap items-center gap-2">
            <FileText className="h-4 w-4" />
            <span className="font-medium">{report.title}</span>
            <Badge variant="secondary">v{report.version}</Badge>
            <Badge variant={report.status === "edited" ? "default" : "outline"}>
              {report.status === "edited" ? "人工编辑版本" : "自动生成版本"}
            </Badge>
            <div className="ml-auto flex gap-1">
              <Button
                size="sm"
                variant="outline"
                className="h-7 text-xs"
                onClick={() => setDraft(draft ?? report.content)}
              >
                <Pencil className="mr-1 h-3 w-3" />
                编辑
              </Button>
              <Button
                size="sm"
                variant="outline"
                className="h-7 text-xs"
                onClick={() => exportMutation.mutate({ id: report.report_id, kind: "md" })}
              >
                <Download className="mr-1 h-3 w-3" />
                Markdown
              </Button>
              <Button
                size="sm"
                variant="outline"
                className="h-7 text-xs"
                onClick={() => exportMutation.mutate({ id: report.report_id, kind: "bib" })}
              >
                <Download className="mr-1 h-3 w-3" />
                BibTeX
              </Button>
              <Button
                size="sm"
                variant="ghost"
                className="h-7 text-xs"
                onClick={() => deleteMutation.mutate(report.report_id)}
              >
                <Trash2 className="mr-1 h-3 w-3" />
                删除该版本
              </Button>
            </div>
          </div>

          {draft === null ? (
            <div className="markdown-body text-sm">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{view?.markdown ?? ""}</ReactMarkdown>
            </div>
          ) : (
            <div className="space-y-2">
              <Textarea
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                rows={24}
                className="font-mono text-xs"
              />
              <div className="flex gap-2">
                <Button
                  size="sm"
                  className="h-8 text-xs"
                  disabled={editMutation.isPending || !draft.trim()}
                  onClick={() => editMutation.mutate({ id: report.report_id, content: draft })}
                >
                  保存为新版本
                </Button>
                <Button size="sm" variant="outline" className="h-8 text-xs" onClick={() => setDraft(null)}>
                  取消
                </Button>
                <span className="self-center text-xs text-muted-foreground">
                  保存不会修改当前版本；正文里的 <code>[[e:证据id]]</code> 会被重新校验，
                  指向不存在证据的引用将被移除。
                </span>
              </div>
            </div>
          )}

          {report.unsupported && (
            <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-2 text-xs">
              <div className="font-medium">证据不足与推测</div>
              <pre className="mt-1 whitespace-pre-wrap font-sans">{report.unsupported}</pre>
            </div>
          )}

          {view && view.legend.length > 0 && (
            <div className="rounded-md border p-2 text-xs">
              <div className="font-medium">引用（可反查原文）</div>
              <ol className="mt-1 space-y-1">
                {view.legend.map((item) => (
                  <li key={item.citation.evidence_id}>
                    [{item.index}] {item.label}
                  </li>
                ))}
              </ol>
            </div>
          )}

          {view && view.orphan.length > 0 && (
            <div className="text-xs text-destructive">
              正文存在指向不存在证据的引用（已隐藏）：{view.orphan.join("、")}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
