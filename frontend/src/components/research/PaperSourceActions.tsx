import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlignLeft, FileText, ScanText, Stamp } from "lucide-react";
import { toast } from "sonner";

import { extractEvidence, ingestFulltext, listUnits } from "@/api/research";
import type { PaperUnit, ResearchPaper } from "@/api/research";
import {
  PaperAbstractDialog,
  isWebUrl,
} from "@/components/research/PaperAbstractDialog";
import { dehyphenate } from "@/components/research/paperText";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
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
 * 单篇论文的原文与证据入口。
 *
 * 「取原文」和「原文」是同一个按钮的两种状态：还没取到全文时只有
 * 「取原文」（幂等、可降级），取到之后变成「原文」，点击在新标签页里
 * 让浏览器直接渲染 PDF。摘要不再平铺在列表里，改为「摘要」按钮按需查看。
 * 原文可得性徽章本身就是「原文单元」的入口（有单元才可点），不再单独占一个按钮。
 * 取不到全文时后端会如实降级为摘要级证据，界面必须把降级原因显示出来，
 * 不能假装拿到了全文。
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
  const [showAbstract, setShowAbstract] = useState(false);
  const hasFulltext = paper.fulltext_state === "fulltext";

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
        {/* 原文可得性徽章本身就是原文单元入口，不再单独放一个按钮 */}
        {paper.unit_count > 0 ? (
          <button
            type="button"
            onClick={() => setShowUnits(true)}
            title="查看带页码/章节定位的原文单元（证据核对引用的就是这些）"
            className="inline-flex h-7 items-center rounded-md border px-2 text-xs hover:bg-accent"
          >
            {FULLTEXT_STATE_LABEL[paper.fulltext_state] ?? paper.fulltext_state}
            {` · ${paper.unit_count} 单元`}
          </button>
        ) : (
          <Badge variant="outline" title="该论文在本项目内的原文可得性">
            {FULLTEXT_STATE_LABEL[paper.fulltext_state] ?? paper.fulltext_state}
          </Badge>
        )}
        {hasFulltext ? (
          <Button
            size="sm"
            variant="outline"
            className="h-7 text-xs"
            onClick={() =>
              // 有可访问的链接就交给浏览器自己的 PDF 查看器（应用内 iframe 会被
              // 外部站点的 X-Frame-Options 挡掉）；原文只来自本地知识库时退回单元视图
              isWebUrl(paper.url)
                ? window.open(paper.url, "_blank", "noopener,noreferrer")
                : setShowUnits(true)
            }
            title={
              isWebUrl(paper.url)
                ? "在新标签页打开原文 PDF"
                : "该论文没有可打开的外部链接，显示已解析的原文单元"
            }
          >
            <FileText className="mr-1 h-3 w-3" />
            原文
          </Button>
        ) : (
          <Button
            size="sm"
            variant="outline"
            className="h-7 text-xs"
            onClick={() => ingestMutation.mutate()}
            disabled={ingestMutation.isPending}
            title="解析 PDF/知识库原文；取不到会降级为摘要级证据"
          >
            <ScanText className="mr-1 h-3 w-3" />
            {ingestMutation.isPending ? "取原文中…" : "取原文"}
          </Button>
        )}
        <Button
          size="sm"
          variant="outline"
          className="h-7 text-xs"
          onClick={() => setShowAbstract(true)}
          title="查看完整摘要与元数据"
        >
          <AlignLeft className="mr-1 h-3 w-3" />
          摘要
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
      <PaperAbstractDialog
        open={showAbstract}
        onClose={() => setShowAbstract(false)}
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

/**
 * 原文单元浏览视图。
 *
 * 一篇论文动辄上百块、每块最多 2000 字，全部摊开就是一堵墙：所以默认只露
 * 三行，配搜索框和页码过滤，定位信息做成一行标签。断词合并只发生在显示层，
 * 库内文本不变，证据核对仍是逐字比对。
 */
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
  const [query, setQuery] = useState("");
  const [page, setPage] = useState("all");
  // 一篇论文动辄上百个单元，默认只露三行，点开才展开（否则整屏都是墙）
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const pages = useMemo(
    () =>
      Array.from(new Set(units.map((u) => u.page).filter((p): p is number => !!p))).sort(
        (a, b) => a - b,
      ),
    [units],
  );
  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return units.filter((unit) => {
      if (page !== "all" && String(unit.page ?? "") !== page) return false;
      if (needle && !unit.text.toLowerCase().includes(needle)) return false;
      return true;
    });
  }, [units, query, page]);
  const totalChars = units.reduce((sum, u) => sum + (u.char_count || 0), 0);

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="max-w-4xl">
        <DialogHeader>
          <DialogTitle className="pr-6 text-left text-base">
            原文单元（{units.length}）
          </DialogTitle>
          <p className="line-clamp-1 text-xs text-muted-foreground" title={paper.title}>
            {paper.title}
          </p>
        </DialogHeader>

        <div className="flex flex-wrap items-center gap-2">
          <Input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="在单元内搜索…"
            className="h-8 w-56 text-sm"
          />
          <Select value={page} onValueChange={setPage}>
            <SelectTrigger className="h-8 w-32 text-xs">
              <SelectValue placeholder="全部页码" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">全部页码</SelectItem>
              {pages.map((p) => (
                <SelectItem key={p} value={String(p)}>
                  第 {p} 页
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <span className="ml-auto text-xs text-muted-foreground">
            {filtered.length}/{units.length} 块 · 共 {totalChars} 字
          </span>
        </div>

        <div className="max-h-[58vh] space-y-2 overflow-y-auto pr-1">
          {isLoading && <div className="text-sm text-muted-foreground">加载中…</div>}
          {filtered.map((unit) => (
            <UnitBlock
              key={unit.unit_id}
              unit={unit}
              expanded={expanded.has(unit.unit_id)}
              onToggle={() =>
                setExpanded((prev) => {
                  const next = new Set(prev);
                  if (next.has(unit.unit_id)) next.delete(unit.unit_id);
                  else next.add(unit.unit_id);
                  return next;
                })
              }
            />
          ))}
          {!isLoading && units.length > 0 && filtered.length === 0 && (
            <div className="py-6 text-center text-sm text-muted-foreground">
              没有匹配的单元，换个关键词或页码
            </div>
          )}
        </div>

        <p className="text-xs text-muted-foreground">
          显示时合并了 PDF 换行处的断词（be- yond → beyond）；证据核对仍按库内原文逐字比对。
        </p>
      </DialogContent>
    </Dialog>
  );
}

function UnitBlock({
  unit,
  expanded,
  onToggle,
}: {
  unit: PaperUnit;
  expanded: boolean;
  onToggle: () => void;
}) {
  // 章节标题检测偶尔会把整句当标题吞进来（现场："method for network resource
  // optimization combined with a"）：过长就不显示，避免定位行看起来像乱码
  const section =
    unit.section && unit.section.length <= 40 ? unit.section : "";
  return (
    <div className="rounded-md border">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 rounded-t-md border-b bg-muted/40 px-2.5 py-1.5 text-[11px] text-muted-foreground">
        <span className="rounded bg-background px-1.5 py-0.5 font-mono">
          #{unit.block_index}
        </span>
        <span>
          {unit.unit_kind === "abstract"
            ? "摘要级（无页码）"
            : unit.page
              ? `第 ${unit.page} 页`
              : "页码未知"}
        </span>
        {section ? (
          <span className="max-w-[18rem] truncate" title={section}>
            {section}
          </span>
        ) : null}
        <span>{unit.char_count} 字</span>
        <span>来源 {unit.source_kind}</span>
        <Button
          size="sm"
          variant="ghost"
          className="ml-auto h-6 px-2 text-xs"
          onClick={onToggle}
        >
          {expanded ? "收起" : "展开全文"}
        </Button>
      </div>
      <p
        className={cn(
          "whitespace-pre-wrap px-2.5 py-2 text-sm leading-6",
          !expanded && "line-clamp-3",
        )}
      >
        {dehyphenate(unit.text)}
      </p>
    </div>
  );
}
