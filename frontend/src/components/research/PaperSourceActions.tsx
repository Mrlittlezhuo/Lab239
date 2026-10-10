import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FileSearch, ScanText, Stamp } from "lucide-react";
import { toast } from "sonner";

import { extractEvidence, ingestFulltext, listUnits } from "@/api/research";
import type { PaperUnit, ResearchPaper } from "@/api/research";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { FULLTEXT_STATE_LABEL } from "@/components/research/labels";

export interface SubQuestionOption {
  id: string;
  question: string;
}

/**
 * 单篇论文的原文与证据入口：取原文（幂等、可降级）、查看原文单元、
 * 按子问题提取证据。取不到全文时后端会如实降级为摘要级证据，
 * 界面必须把降级原因显示出来，不能假装拿到了全文。
 */
export function PaperSourceActions({
  projectId,
  paper,
  subQuestions,
  latestRunId,
}: {
  projectId: string;
  paper: ResearchPaper;
  subQuestions: SubQuestionOption[];
  latestRunId?: string;
}) {
  const queryClient = useQueryClient();
  const [showUnits, setShowUnits] = useState(false);
  const [showExtract, setShowExtract] = useState(false);

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["research-papers", projectId] });
    queryClient.invalidateQueries({ queryKey: ["research-evidence", projectId] });
    queryClient.invalidateQueries({ queryKey: ["research-coverage", projectId] });
  };

  const ingestMutation = useMutation({
    mutationFn: () =>
      ingestFulltext(projectId, paper.paper_id, latestRunId ? { run_id: latestRunId } : {}),
    onSuccess: (r) => {
      if (r.state === "fulltext") {
        toast.success(`已入库 ${r.unit_count} 个原文单元`);
      } else {
        toast.warning(`未取得全文（当前：${FULLTEXT_STATE_LABEL[r.state] ?? r.state}）：${r.detail}`);
      }
      invalidate();
    },
    onError: (err) => toast.error(err instanceof Error ? err.message : "解析原文失败"),
  });

  return (
    <>
      <div className="flex flex-wrap items-center gap-1">
        <Badge variant="outline" title="该论文在本项目内的原文可得性">
          {FULLTEXT_STATE_LABEL[paper.fulltext_state] ?? paper.fulltext_state}
          {paper.unit_count > 0 ? ` · ${paper.unit_count} 单元` : ""}
        </Badge>
        <Button
          size="sm"
          variant="outline"
          className="h-7 text-xs"
          onClick={() => ingestMutation.mutate()}
          disabled={ingestMutation.isPending}
          title="解析 PDF/知识库原文；取不到会降级为摘要级证据"
        >
          <ScanText className="mr-1 h-3 w-3" />
          取原文
        </Button>
        <Button
          size="sm"
          variant="outline"
          className="h-7 text-xs"
          onClick={() => setShowUnits(true)}
          disabled={paper.unit_count === 0}
          title={paper.unit_count === 0 ? "还没有原文单元" : "查看带定位的原文单元"}
        >
          <FileSearch className="mr-1 h-3 w-3" />
          原文
        </Button>
        <Button
          size="sm"
          variant="outline"
          className="h-7 text-xs"
          onClick={() => setShowExtract(true)}
          title="按子问题提取证据（原文逐字核对）"
        >
          <Stamp className="mr-1 h-3 w-3" />
          提取证据
        </Button>
      </div>

      <UnitsDialog
        open={showUnits}
        onClose={() => setShowUnits(false)}
        projectId={projectId}
        paper={paper}
      />
      <ExtractDialog
        open={showExtract}
        onClose={() => setShowExtract(false)}
        subQuestions={subQuestions}
        projectId={projectId}
        paper={paper}
        latestRunId={latestRunId}
      />
    </>
  );
}

function useExtractMutation(
  projectId: string,
  paper: ResearchPaper,
  onDone?: () => void,
) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: { sub_question_id: string; sub_question: string; run_id?: string }) =>
      extractEvidence(projectId, paper.paper_id, body),
    onSuccess: (r) => {
      toast.success(
        `证据：新增 ${r.created}，更新 ${r.updated}，未变 ${r.unchanged}，保留人工审核 ${r.kept_reviewed}`,
      );
      for (const item of r.rejected.slice(0, 5)) toast.warning(item);
      queryClient.invalidateQueries({ queryKey: ["research-evidence", projectId] });
      queryClient.invalidateQueries({ queryKey: ["research-papers", projectId] });
      queryClient.invalidateQueries({ queryKey: ["research-coverage", projectId] });
      onDone?.();
    },
    onError: (err) => toast.error(err instanceof Error ? err.message : "提取证据失败"),
  });
}

function ExtractDialog({
  open,
  onClose,
  subQuestions,
  projectId,
  paper,
  latestRunId,
}: {
  open: boolean;
  onClose: () => void;
  subQuestions: SubQuestionOption[];
  projectId: string;
  paper: ResearchPaper;
  latestRunId?: string;
}) {
  const [selected, setSelected] = useState("");
  const [manual, setManual] = useState("");
  const mutation = useExtractMutation(projectId, paper, onClose);

  const activeId = selected || subQuestions[0]?.id || "";
  // 自定义子问题优先于计划里的子问题（用户显式输入意图更明确）
  const option: SubQuestionOption | null = manual.trim()
    ? { id: "sq_manual", question: manual.trim() }
    : (subQuestions.find((s) => s.id === activeId) ?? null);

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>为「{paper.title}」提取证据</DialogTitle>
        </DialogHeader>
        <div className="space-y-3">
          {subQuestions.length > 0 ? (
            <div className="space-y-1">
              <Label>子问题（取自最近一次研究计划）</Label>
              <Select value={activeId} onValueChange={setSelected}>
                <SelectTrigger>
                  <SelectValue placeholder="选择子问题" />
                </SelectTrigger>
                <SelectContent>
                  {subQuestions.map((s) => (
                    <SelectItem key={s.id} value={s.id}>
                      {s.id}：{s.question}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          ) : (
            <p className="text-xs text-muted-foreground">
              该项目还没有研究计划子问题。可临时填写一个子问题（覆盖统计里会作为计划外子问题显示）。
            </p>
          )}
          <div className="space-y-1">
            <Label>或自定义子问题</Label>
            <Textarea value={manual} onChange={(e) => setManual(e.target.value)} rows={2} />
          </div>
          <Button
            className="w-full"
            disabled={mutation.isPending || !option}
            onClick={() =>
              option &&
              mutation.mutate({
                sub_question_id: option.id,
                sub_question: option.question,
                ...(latestRunId ? { run_id: latestRunId } : {}),
              })
            }
          >
            {mutation.isPending ? "提取中…" : "提取并核对原文"}
          </Button>
          <p className="text-xs text-muted-foreground">
            模型给出的原文必须逐字出现在该论文的原文单元中，否则该条会被拒绝并说明原因；
            已人工审核过的证据不会被自动覆盖。
          </p>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function UnitsDialog({
  open,
  onClose,
  projectId,
  paper,
}: {
  open: boolean;
  onClose: () => void;
  projectId: string;
  paper: ResearchPaper;
}) {
  const { data: units = [], isLoading } = useQuery({
    queryKey: ["research-units", projectId, paper.paper_id],
    queryFn: () => listUnits(projectId, paper.paper_id),
    enabled: open,
  });

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle>{paper.title} 的原文单元（{units.length}）</DialogTitle>
        </DialogHeader>
        <div className="max-h-[60vh] space-y-2 overflow-y-auto text-xs">
          {isLoading && <div className="text-muted-foreground">加载中…</div>}
          {units.map((unit) => (
            <UnitBlock key={unit.unit_id} unit={unit} />
          ))}
        </div>
      </DialogContent>
    </Dialog>
  );
}

function UnitBlock({ unit }: { unit: PaperUnit }) {
  const where =
    unit.unit_kind === "abstract"
      ? "摘要级"
      : [
          unit.page ? `第 ${unit.page} 页` : "",
          unit.section ?? "",
          `文本块 ${unit.block_index}`,
        ]
          .filter(Boolean)
          .join("，");
  return (
    <div className="rounded-md border p-2">
      <div className="text-[11px] text-muted-foreground">
        {where} · {unit.char_count} 字 · 来源 {unit.source_kind}
      </div>
      <p className="mt-1 whitespace-pre-wrap">{unit.text}</p>
    </div>
  );
}
