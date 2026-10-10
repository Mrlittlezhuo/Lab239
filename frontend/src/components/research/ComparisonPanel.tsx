import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Sparkles, Trash2 } from "lucide-react";
import { toast } from "sonner";

import {
  generateComparisons,
  getDimensions,
  listComparisons,
  listEvidence,
  setDimensions,
} from "@/api/research";
import type { Dimension } from "@/api/research";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { EVIDENCE_TYPE_LABEL } from "@/components/research/labels";
import { cn } from "@/lib/utils";

/**
 * 比较矩阵：论文 × 维度。每个格子都必须挂着真实证据卡片，
 * 没有证据支撑的比较不会被后端写入，因此这里也不会凭空出现。
 */
export function ComparisonPanel({ projectId }: { projectId: string }) {
  const queryClient = useQueryClient();
  const [dims, setDims] = useState<Dimension[] | null>(null);
  const [newDim, setNewDim] = useState("");

  const { data: dimensions = [] } = useQuery({
    queryKey: ["research-dimensions", projectId],
    queryFn: () => getDimensions(projectId),
  });
  const { data: comparisons = [] } = useQuery({
    queryKey: ["research-comparisons", projectId],
    queryFn: () => listComparisons(projectId),
  });
  const { data: evidence = [] } = useQuery({
    queryKey: ["research-evidence", projectId, "for-comparison"],
    queryFn: () => listEvidence(projectId),
  });

  const working = dims ?? dimensions;

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["research-dimensions", projectId] });
    queryClient.invalidateQueries({ queryKey: ["research-comparisons", projectId] });
    queryClient.invalidateQueries({ queryKey: ["research-evidence", projectId] });
  };

  const saveDims = useMutation({
    mutationFn: () => setDimensions(projectId, working),
    onSuccess: () => {
      toast.success("比较维度已保存");
      setDims(null);
      invalidate();
    },
    onError: (err) => toast.error(err instanceof Error ? err.message : "保存维度失败"),
  });

  const generate = useMutation({
    mutationFn: () => generateComparisons(projectId),
    onSuccess: (r) => {
      toast.success(
        `比较已生成：写入 ${r.written} 条，跳过 ${r.skipped} 条，新增冲突 ${r.conflicts} 条`,
      );
      for (const item of r.rejected.slice(0, 5)) toast.warning(item);
      invalidate();
    },
    onError: (err) => toast.error(err instanceof Error ? err.message : "生成比较失败"),
  });

  const evidenceById = new Map(evidence.map((e) => [e.evidence_id, e]));
  const paperIds = Array.from(new Set(comparisons.map((c) => c.paper_id)));
  const dimIds = working.length > 0 ? working.map((d) => d.id) : Array.from(new Set(comparisons.map((c) => c.dimension_id)));
  const labelOf = (id: string) => working.find((d) => d.id === id)?.label || id;
  const titleOf = (paperId: string) =>
    comparisons.find((c) => c.paper_id === paperId)?.paper_title || paperId;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-base font-semibold">比较维度</h2>
        <div className="ml-auto flex gap-2">
          <Button
            size="sm"
            variant="outline"
            className="h-8 text-xs"
            onClick={() => saveDims.mutate()}
            disabled={saveDims.isPending || dims === null}
          >
            保存维度
          </Button>
          <Button
            size="sm"
            className="h-8 text-xs"
            onClick={() => generate.mutate()}
            disabled={generate.isPending}
          >
            <Sparkles className="mr-1 h-3 w-3" />
            生成比较与冲突检测
          </Button>
        </div>
      </div>

      <div className="flex flex-wrap gap-1.5">
        {working.map((d) => (
          <span key={d.id} className="inline-flex items-center gap-1 rounded-md border px-2 py-1 text-xs">
            <Input
              className="h-6 w-28 border-none bg-transparent p-0 text-xs"
              value={d.label}
              onChange={(e) =>
                setDims(
                  working.map((x) => (x.id === d.id ? { ...x, label: e.target.value } : x)),
                )
              }
            />
            <button
              type="button"
              className="text-muted-foreground hover:text-destructive"
              title="删除维度"
              onClick={() => setDims(working.filter((x) => x.id !== d.id))}
            >
              <Trash2 className="h-3 w-3" />
            </button>
          </span>
        ))}
        <span className="inline-flex items-center gap-1">
          <Input
            className="h-7 w-32 text-xs"
            placeholder="新维度 id（英文）"
            value={newDim}
            onChange={(e) => setNewDim(e.target.value)}
          />
          <Button
            size="sm"
            variant="ghost"
            className="h-7 text-xs"
            disabled={!newDim.trim()}
            onClick={() => {
              const id = newDim.trim();
              if (working.some((d) => d.id === id)) return;
              setDims([...working, { id, label: id }]);
              setNewDim("");
            }}
          >
            <Plus className="h-3 w-3" />
          </Button>
        </span>
      </div>

      {comparisons.length === 0 ? (
        <div className="py-6 text-center text-sm text-muted-foreground">
          还没有比较结论。先纳入论文并提取证据，再点击“生成比较”。
        </div>
      ) : (
        <div className="overflow-x-auto rounded-lg border">
          <table className="w-full border-collapse text-xs">
            <thead>
              <tr className="bg-muted/50">
                <th className="w-56 border-b p-2 text-left">论文</th>
                {dimIds.map((id) => (
                  <th key={id} className="border-b border-l p-2 text-left">
                    {labelOf(id)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {paperIds.map((paperId) => (
                <tr key={paperId}>
                  <td className="border-b p-2 align-top">
                    {titleOf(paperId)}
                    <div className="mt-1 text-[11px] text-muted-foreground">
                      {comparisons.filter((c) => c.paper_id === paperId)[0]?.level === "abstract"
                        ? "摘要级比较"
                        : "全文级比较"}
                    </div>
                  </td>
                  {dimIds.map((dimId) => {
                    const cell = comparisons.find(
                      (c) => c.paper_id === paperId && c.dimension_id === dimId,
                    );
                    return (
                      <td key={dimId} className="border-b border-l p-2 align-top">
                        {cell ? (
                          <div className="space-y-1">
                            <div>{cell.content}</div>
                            <div className="flex flex-wrap gap-1">
                              {cell.evidence_ids.map((eid) => {
                                const card = evidenceById.get(eid);
                                return (
                                  <Badge
                                    key={eid}
                                    variant="outline"
                                    className="max-w-full font-normal"
                                    title={card ? `${card.quote}` : "证据已不存在"}
                                  >
                                    {card
                                      ? `${EVIDENCE_TYPE_LABEL[card.evidence_type] ?? card.evidence_type}：${card.quote.slice(0, 40)}`
                                      : "证据已删除"}
                                  </Badge>
                                );
                              })}
                            </div>
                          </div>
                        ) : (
                          <span className={cn("text-muted-foreground")}>—</span>
                        )}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
