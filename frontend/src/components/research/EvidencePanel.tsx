import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Pencil, RefreshCw, ShieldAlert, Trash2, X } from "lucide-react";
import { toast } from "sonner";

import {
  deleteEvidence,
  listEvidence,
  reverifyEvidence,
  reviewEvidence,
} from "@/api/research";
import type { EvidenceCard } from "@/api/research";
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
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { EVIDENCE_TYPE_LABEL, REVIEW_LABEL, locatorText } from "@/components/research/labels";
import { cn } from "@/lib/utils";

const REVIEW_FILTERS = ["", "pending", "confirmed", "edited", "rejected"];

/**
 * 证据审核面板：逐条核对原文、确认/修改/驳回。
 * 修改原文时后端会在该论文的原文里逐字复核，找不到就拒绝保存，
 * 因此界面只负责如实转达错误，不做“看起来成功了”的乐观展示。
 */
export function EvidencePanel({
  projectId,
  papers,
}: {
  projectId: string;
  papers: { paper_id: string; title: string }[];
}) {
  const queryClient = useQueryClient();
  const [reviewStatus, setReviewStatus] = useState("");
  const [paperId, setPaperId] = useState("");
  const [includeRejected, setIncludeRejected] = useState(true);
  const [editing, setEditing] = useState<EvidenceCard | null>(null);

  const { data: cards = [], isLoading } = useQuery({
    queryKey: ["research-evidence", projectId, reviewStatus, paperId, includeRejected],
    queryFn: () =>
      listEvidence(projectId, {
        review_status: reviewStatus || undefined,
        paper_id: paperId || undefined,
        include_rejected: includeRejected,
      }),
  });

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ["research-evidence", projectId] });

  const reviewMutation = useMutation({
    mutationFn: ({
      evidenceId,
      body,
    }: {
      evidenceId: string;
      body: { review_status: string; summary?: string; quote?: string; note?: string };
    }) => reviewEvidence(evidenceId, body),
    onSuccess: (card) => {
      toast.success(`证据已标记为${REVIEW_LABEL[card.review_status] ?? card.review_status}`);
      setEditing(null);
      invalidate();
    },
    onError: (err) => toast.error(err instanceof Error ? err.message : "审核失败"),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteEvidence(id),
    onSuccess: () => {
      toast.success("证据已删除");
      invalidate();
    },
    onError: (err) => toast.error(err instanceof Error ? err.message : "删除失败"),
  });

  const reverifyMutation = useMutation({
    mutationFn: () => reverifyEvidence(projectId),
    onSuccess: (r) => {
      toast.success(`已按当前原文重新核对：${r.verified} 条可核对，${r.unverified} 条未能核对`);
      invalidate();
    },
    onError: (err) => toast.error(err instanceof Error ? err.message : "重新核对失败"),
  });

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-base font-semibold">证据卡片（{cards.length}）</h2>
        <div className="ml-auto flex items-center gap-2 text-xs">
          <Select value={paperId || "all"} onValueChange={(v) => setPaperId(v === "all" ? "" : v)}>
            <SelectTrigger className="h-8 w-56 text-xs">
              <SelectValue placeholder="全部论文" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">全部论文</SelectItem>
              {papers.map((p) => (
                <SelectItem key={p.paper_id} value={p.paper_id}>
                  {p.title}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <label className="flex items-center gap-1">
            <Switch
              checked={includeRejected}
              onCheckedChange={setIncludeRejected}
              className="h-5 w-9"
            />
            含已驳回
          </label>
          <Button
            size="sm"
            variant="outline"
            className="h-8 text-xs"
            onClick={() => reverifyMutation.mutate()}
            disabled={reverifyMutation.isPending}
          >
            <RefreshCw className="mr-1 h-3 w-3" />
            重新核对原文
          </Button>
        </div>
      </div>

      <div className="flex gap-1 text-xs">
        {REVIEW_FILTERS.map((s) => (
          <button
            key={s}
            type="button"
            className={cn("rounded-md border px-2 py-1", reviewStatus === s && "bg-accent")}
            onClick={() => setReviewStatus(s)}
          >
            {s ? REVIEW_LABEL[s] : "全部状态"}
          </button>
        ))}
      </div>

      {isLoading && <div className="text-sm text-muted-foreground">加载中…</div>}
      {!isLoading && cards.length === 0 && (
        <div className="py-6 text-center text-sm text-muted-foreground">
          还没有证据。在“候选论文”里取原文后按子问题提取证据。
        </div>
      )}
      {cards.map((card) => (
        <EvidenceCardRow
          key={card.evidence_id}
          card={card}
          onConfirm={() =>
            reviewMutation.mutate({
              evidenceId: card.evidence_id,
              body: { review_status: "confirmed", note: card.review_note },
            })
          }
          onReject={() =>
            reviewMutation.mutate({
              evidenceId: card.evidence_id,
              body: { review_status: "rejected", note: card.review_note },
            })
          }
          onEdit={() => setEditing(card)}
          onDelete={() => deleteMutation.mutate(card.evidence_id)}
        />
      ))}

      <EditEvidenceDialog
        card={editing}
        onClose={() => setEditing(null)}
        onSubmit={(body) =>
          reviewMutation.mutate({ evidenceId: editing?.evidence_id ?? "", body })
        }
        busy={reviewMutation.isPending}
      />
    </div>
  );
}

function EvidenceCardRow({
  card,
  onConfirm,
  onReject,
  onEdit,
  onDelete,
}: {
  card: EvidenceCard;
  onConfirm: () => void;
  onReject: () => void;
  onEdit: () => void;
  onDelete: () => void;
}) {
  return (
    <div className={cn("rounded-lg border p-3 text-sm", card.review_status === "rejected" && "opacity-60")}>
      <div className="flex flex-wrap items-center gap-1.5 text-xs">
        <Badge variant="secondary">{EVIDENCE_TYPE_LABEL[card.evidence_type] ?? card.evidence_type}</Badge>
        <Badge variant={card.level === "fulltext" ? "outline" : "secondary"}>
          {card.level === "fulltext" ? "全文级" : "摘要级"}
        </Badge>
        {!card.quote_verified && (
          <Badge variant="destructive">
            <ShieldAlert className="mr-1 h-3 w-3" />
            原文未核对
          </Badge>
        )}
        <Badge variant={card.review_status === "confirmed" ? "default" : "outline"}>
          {REVIEW_LABEL[card.review_status] ?? card.review_status}
        </Badge>
        <span className="text-muted-foreground">{locatorText(card)}</span>
        {card.version > 1 && <span className="text-muted-foreground">v{card.version}</span>}
        <span className="ml-auto text-muted-foreground">{card.paper_title || card.paper_id}</span>
      </div>

      <div className="mt-2 font-medium">{card.summary}</div>
      <blockquote className="mt-1 border-l-2 border-muted pl-2 text-xs text-muted-foreground">
        {card.quote}
      </blockquote>
      {card.review_note && (
        <div className="mt-1 text-xs text-muted-foreground">审核备注：{card.review_note}</div>
      )}
      <div className="mt-1 text-xs text-muted-foreground">
        子问题 {card.sub_question_id || "-"} · 来源 {card.source_kind || "-"} · 模型{" "}
        {card.extraction_model || "-"}
      </div>

      <div className="mt-2 flex gap-1">
        <Button size="sm" variant="outline" className="h-7 text-xs" onClick={onConfirm}>
          <Check className="mr-1 h-3 w-3" />
          确认
        </Button>
        <Button size="sm" variant="outline" className="h-7 text-xs" onClick={onEdit}>
          <Pencil className="mr-1 h-3 w-3" />
          修改
        </Button>
        <Button size="sm" variant="outline" className="h-7 text-xs" onClick={onReject}>
          <X className="mr-1 h-3 w-3" />
          驳回
        </Button>
        <Button size="sm" variant="ghost" className="h-7 text-xs" onClick={onDelete}>
          <Trash2 className="mr-1 h-3 w-3" />
          删除
        </Button>
      </div>
    </div>
  );
}

function EditEvidenceDialog({
  card,
  onClose,
  onSubmit,
  busy,
}: {
  card: EvidenceCard | null;
  onClose: () => void;
  onSubmit: (body: { review_status: string; summary?: string; quote?: string; note?: string }) => void;
  busy: boolean;
}) {
  const [summary, setSummary] = useState("");
  const [quote, setQuote] = useState("");
  const [note, setNote] = useState("");
  const [openedFor, setOpenedFor] = useState<string | null>(null);

  if (card && card.evidence_id !== openedFor) {
    setOpenedFor(card.evidence_id);
    setSummary(card.summary);
    setQuote(card.quote);
    setNote(card.review_note);
  }

  return (
    <Dialog open={!!card} onOpenChange={(open) => !open && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>修改证据</DialogTitle>
        </DialogHeader>
        <div className="space-y-3">
          <div className="space-y-1">
            <Label>概括</Label>
            <Textarea value={summary} onChange={(e) => setSummary(e.target.value)} />
          </div>
          <div className="space-y-1">
            <Label>原文（必须逐字出现在该论文的原文中）</Label>
            <Textarea value={quote} onChange={(e) => setQuote(e.target.value)} rows={5} />
          </div>
          <div className="space-y-1">
            <Label>审核备注</Label>
            <Textarea value={note} onChange={(e) => setNote(e.target.value)} rows={2} />
          </div>
          <div className="flex gap-2">
            <Button
              className="flex-1"
              disabled={busy}
              onClick={() => onSubmit({ review_status: "edited", summary, quote, note })}
            >
              保存为新版本
            </Button>
            <Button variant="outline" onClick={onClose}>
              取消
            </Button>
          </div>
          <p className="text-xs text-muted-foreground">
            保存会生成新的证据版本；若原文在该论文的原文里找不到，后端会拒绝保存。
          </p>
        </div>
      </DialogContent>
    </Dialog>
  );
}
